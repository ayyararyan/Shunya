#!/usr/bin/env python3
"""
Zerodha Option Chain Streamer - Main Orchestrator

Streams live options data from Kite Connect WebSocket and saves
1-second option-chain snapshots to CSV.

Usage:
    python -m engines.zerodha.run_option_chain [--config CONFIG_PATH] [--login]
"""

import argparse
import logging
import os
import signal
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Any, Optional

import yaml
from dotenv import load_dotenv

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engines.zerodha.auth import KiteAuth
from engines.zerodha.instruments import InstrumentLoader
from engines.zerodha.option_universe import OptionUniverse
from engines.zerodha.ticker_stream import TickerStream, MultiTickerStream
from engines.zerodha.snapshot_builder import SnapshotBuilder
from engines.zerodha.csv_writer import MultiCSVWriter


def setup_logging(log_dir: Path, log_level: str = "INFO") -> None:
    """Configure logging with file and console handlers."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"option_chain_{date.today().strftime('%Y%m%d')}.log"

    # Create formatters
    file_formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )

    # File handler
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(file_formatter)
    file_handler.setLevel(logging.DEBUG)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(console_formatter)
    console_handler.setLevel(getattr(logging, log_level.upper()))

    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    # Suppress noisy loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("websocket").setLevel(logging.WARNING)


def load_config(config_path: Path) -> Dict[str, Any]:
    """Load YAML configuration file."""
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path) as f:
        config = yaml.safe_load(f)

    return config


class OptionChainStreamer:
    """Main orchestrator for option chain streaming."""

    def __init__(self, config: Dict[str, Any], base_path: Path):
        """
        Initialize the streamer.

        Args:
            config: Configuration dictionary.
            base_path: Project base path.
        """
        self.config = config
        self.base_path = base_path
        self.logger = logging.getLogger(__name__)

        self._running = False
        self._shutdown_event = threading.Event()

        # Components (initialized in setup)
        self.auth: Optional[KiteAuth] = None
        self.loader: Optional[InstrumentLoader] = None
        self.universe: Optional[OptionUniverse] = None
        self.ticker: Optional[TickerStream] = None
        self.builder: Optional[SnapshotBuilder] = None
        self.writer: Optional[MultiCSVWriter] = None

        # Stats
        self._stats = {
            "snapshots_taken": 0,
            "rows_written": 0,
            "start_time": None,
            "last_snapshot_time": None,
        }

    def setup(self) -> None:
        """Initialize all components."""
        self.logger.info("Setting up option chain streamer...")

        # Auth
        self.auth = KiteAuth(self.base_path)
        if not self.auth.validate_session():
            raise RuntimeError(
                "Invalid or missing session. Run with --login to authenticate."
            )
        kite = self.auth.get_kite_client()
        self.logger.info("Authentication validated")

        # Instruments
        cache_dir = self.base_path / self.config.get("instrument_dump_dir", "archive/instruments")
        self.loader = InstrumentLoader(kite=kite, cache_dir=cache_dir)
        self.loader.fetch_instruments()
        self.logger.info("Instruments loaded")

        # Universe
        self.universe = OptionUniverse(
            instrument_loader=self.loader,
            underlyings=self.config.get("underlyings", ["NIFTY"]),
            expiries_mode=self.config.get("expiries_mode", "nearest"),
            expiry_list=self.config.get("expiry_list", []),
            max_strike_distance=self.config.get("max_strike_distance", 2500),
            strike_step_overrides=self.config.get("strike_step_overrides", {}),
        )

        # Get initial spot prices from index quotes
        spot_prices = self._fetch_spot_prices(kite)
        self.universe.build_universe(spot_prices)
        self.logger.info(self.universe.summary())

        # Ticker
        tokens = self.universe.get_tokens()
        if len(tokens) > 3000:
            # Use multi-stream for large token sets
            self.ticker = MultiTickerStream(
                api_key=self.auth.api_key,
                access_token=self.auth.get_access_token(),
                reconnect=True,
                reconnect_max_tries=self.config.get("reconnect_max_tries", 50),
                reconnect_max_delay=self.config.get("reconnect_max_delay", 30),
            )
        else:
            self.ticker = TickerStream(
                api_key=self.auth.api_key,
                access_token=self.auth.get_access_token(),
                reconnect=True,
                reconnect_max_tries=self.config.get("reconnect_max_tries", 50),
                reconnect_max_delay=self.config.get("reconnect_max_delay", 30),
            )
        self.ticker.set_tokens(tokens)
        self.logger.info(f"Ticker configured with {len(tokens)} tokens")

        # Snapshot builder
        self.builder = SnapshotBuilder(
            venue_label=self.config.get("venue_label", "NSE-FO"),
            timezone=self.config.get("timezone", "Asia/Kolkata"),
        )
        for underlying, spot in spot_prices.items():
            self.builder.set_spot_price(underlying, spot)

        # CSV writer
        output_dir = self.base_path / self.config.get("output_dir", "archive/option_chain")
        self.writer = MultiCSVWriter(
            output_dir=output_dir,
            underlyings=self.config.get("underlyings", ["NIFTY"]),
            venue="NSEFO",
            flush_rows=self.config.get("flush_rows_per_write", 500),
            flush_interval_seconds=1.0,
        )
        self.logger.info(f"CSV writer initialized, output dir: {output_dir}")

    def _fetch_spot_prices(self, kite) -> Dict[str, float]:
        """Fetch current spot prices for underlyings."""
        spot_prices = {}
        underlyings = self.config.get("underlyings", [])

        # Map underlying to index instrument
        index_map = {
            "NIFTY": "NSE:NIFTY 50",
            "BANKNIFTY": "NSE:NIFTY BANK",
            "FINNIFTY": "NSE:NIFTY FIN SERVICE",
        }

        instruments = [index_map.get(u) for u in underlyings if u in index_map]
        instruments = [i for i in instruments if i]

        if instruments:
            try:
                quotes = kite.quote(instruments)
                for underlying in underlyings:
                    key = index_map.get(underlying)
                    if key and key in quotes:
                        spot_prices[underlying] = quotes[key].get("last_price", 0)
                        self.logger.info(f"{underlying} spot: {spot_prices[underlying]}")
            except Exception as e:
                self.logger.error(f"Error fetching spot prices: {e}")

        return spot_prices

    def _update_spot_prices(self) -> None:
        """Update spot prices from latest ticks or quotes."""
        # For now, we keep initial spot prices
        # In production, you might want to subscribe to index ticks too
        pass

    def _take_snapshot(self) -> None:
        """Take a snapshot of current state and write to CSV."""
        if not self.ticker or not self.universe or not self.builder or not self.writer:
            return

        ticks = self.ticker.get_all_latest_ticks()
        universe = self.universe.get_universe()

        if not ticks:
            self.logger.debug("No ticks available for snapshot")
            return

        # Build snapshot rows
        rows = self.builder.build_snapshot(ticks, universe)

        # Write to CSV
        self.writer.write_rows(rows)

        self._stats["snapshots_taken"] += 1
        self._stats["rows_written"] += len(rows)
        self._stats["last_snapshot_time"] = datetime.now()

        if self._stats["snapshots_taken"] % 60 == 0:
            self.logger.info(
                f"Stats: {self._stats['snapshots_taken']} snapshots, "
                f"{self._stats['rows_written']} rows written"
            )

    def _snapshot_loop(self) -> None:
        """Main loop for taking periodic snapshots."""
        interval = self.config.get("sampling_interval_seconds", 1)
        next_snapshot = time.time() + interval

        while self._running and not self._shutdown_event.is_set():
            now = time.time()

            if now >= next_snapshot:
                try:
                    self._take_snapshot()
                except Exception as e:
                    self.logger.error(f"Error taking snapshot: {e}")

                # Schedule next snapshot
                next_snapshot = now + interval

            # Also check for time-based flush
            if self.writer:
                self.writer.check_time_flush()

            # Sleep until next snapshot (with small intervals for shutdown check)
            sleep_time = min(0.1, next_snapshot - time.time())
            if sleep_time > 0:
                self._shutdown_event.wait(sleep_time)

    def run(self) -> None:
        """Start streaming and snapshot loop."""
        self.logger.info("Starting option chain streamer...")
        self._running = True
        self._stats["start_time"] = datetime.now()

        # Start ticker
        self.ticker.start(threaded=True)
        self.logger.info("Ticker started, waiting for connection...")

        # Give ticker time to connect
        time.sleep(2)

        # Start snapshot loop
        self.logger.info("Starting snapshot loop...")
        try:
            self._snapshot_loop()
        except KeyboardInterrupt:
            self.logger.info("Interrupted by user")
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop all components gracefully."""
        self.logger.info("Stopping option chain streamer...")
        self._running = False
        self._shutdown_event.set()

        if self.ticker:
            self.ticker.stop()

        if self.writer:
            self.writer.close()

        # Log final stats
        elapsed = (datetime.now() - self._stats["start_time"]).total_seconds() if self._stats["start_time"] else 0
        self.logger.info(
            f"Final stats: {self._stats['snapshots_taken']} snapshots, "
            f"{self._stats['rows_written']} rows in {elapsed:.1f}s"
        )

    def get_stats(self) -> Dict[str, Any]:
        """Get current statistics."""
        stats = dict(self._stats)
        if self.ticker:
            stats["ticker"] = self.ticker.get_stats()
        if self.writer:
            stats["writer"] = self.writer.get_stats()
        return stats


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Stream live options data from Zerodha Kite Connect"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "configs" / "zerodha_option_chain.yaml",
        help="Path to config file",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Run interactive login flow",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )
    args = parser.parse_args()

    # Load environment
    load_dotenv(PROJECT_ROOT / ".env")

    # Load config
    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)

    # Setup logging
    log_dir = PROJECT_ROOT / config.get("log_dir", "logs/zerodha")
    setup_logging(log_dir, args.log_level)
    logger = logging.getLogger(__name__)

    logger.info("=" * 60)
    logger.info("ZERODHA OPTION CHAIN STREAMER")
    logger.info("=" * 60)

    # Handle login flow
    if args.login:
        auth = KiteAuth(PROJECT_ROOT)
        auth.interactive_login()
        logger.info("Login complete!")
        return

    # Create and run streamer
    streamer = OptionChainStreamer(config, PROJECT_ROOT)

    # Handle signals for graceful shutdown
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}")
        streamer.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        streamer.setup()
        streamer.run()
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
