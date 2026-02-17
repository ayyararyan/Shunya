"""
Zerodha WebSocket Ticker Stream

Handles:
- WebSocket connection via KiteTicker
- Subscribing to instrument tokens in FULL mode
- Maintaining in-memory latest tick state
- Reconnection and error handling
"""

import logging
import threading
from datetime import datetime
from typing import Callable, Dict, List, Optional, Any

from kiteconnect import KiteTicker

logger = logging.getLogger(__name__)


class TickerStream:
    """Manages WebSocket streaming of tick data from Kite Connect."""

    MAX_TOKENS_PER_CONNECTION = 3000

    def __init__(
        self,
        api_key: str,
        access_token: str,
        reconnect: bool = True,
        reconnect_max_tries: int = 50,
        reconnect_max_delay: int = 30,
    ):
        """
        Initialize TickerStream.

        Args:
            api_key: Kite Connect API key.
            access_token: Valid access token.
            reconnect: Enable auto-reconnect.
            reconnect_max_tries: Maximum reconnection attempts.
            reconnect_max_delay: Maximum delay between reconnects in seconds.
        """
        self.api_key = api_key
        self.access_token = access_token
        self.reconnect = reconnect
        self.reconnect_max_tries = reconnect_max_tries
        self.reconnect_max_delay = reconnect_max_delay

        self._ticker: Optional[KiteTicker] = None
        self._subscribed_tokens: List[int] = []
        self._latest_ticks: Dict[int, Dict[str, Any]] = {}
        self._tick_lock = threading.RLock()
        self._running = False

        # Callbacks
        self._on_tick_callback: Optional[Callable[[List[Dict]], None]] = None
        self._on_connect_callback: Optional[Callable[[], None]] = None
        self._on_close_callback: Optional[Callable[[int, str], None]] = None

        # Stats
        self._stats = {
            "ticks_received": 0,
            "reconnect_count": 0,
            "errors": 0,
            "last_tick_time": None,
        }

    def _create_ticker(self) -> KiteTicker:
        """Create and configure a new KiteTicker instance."""
        ticker = KiteTicker(
            api_key=self.api_key,
            access_token=self.access_token,
            reconnect=self.reconnect,
            reconnect_max_tries=self.reconnect_max_tries,
            reconnect_max_delay=self.reconnect_max_delay,
        )

        ticker.on_ticks = self._handle_ticks
        ticker.on_connect = self._handle_connect
        ticker.on_close = self._handle_close
        ticker.on_error = self._handle_error
        ticker.on_reconnect = self._handle_reconnect
        ticker.on_noreconnect = self._handle_noreconnect

        return ticker

    def _handle_ticks(self, ws: Any, ticks: List[Dict]) -> None:
        """Handle incoming tick data."""
        if not ticks:
            return

        with self._tick_lock:
            for tick in ticks:
                token = tick.get("instrument_token")
                if token is not None:
                    self._latest_ticks[token] = tick
                    self._stats["ticks_received"] += 1

            self._stats["last_tick_time"] = datetime.now()

        # Call user callback if registered
        if self._on_tick_callback:
            try:
                self._on_tick_callback(ticks)
            except Exception as e:
                logger.error(f"Error in tick callback: {e}")

    def _handle_connect(self, ws: Any, response: Any) -> None:
        """Handle WebSocket connection."""
        logger.info("WebSocket connected")

        # Subscribe and set mode
        if self._subscribed_tokens:
            self._do_subscribe(ws)

        if self._on_connect_callback:
            try:
                self._on_connect_callback()
            except Exception as e:
                logger.error(f"Error in connect callback: {e}")

    def _do_subscribe(self, ws: Any) -> None:
        """Perform subscription to tokens."""
        if not self._subscribed_tokens:
            return

        # Check token limit
        if len(self._subscribed_tokens) > self.MAX_TOKENS_PER_CONNECTION:
            logger.warning(
                f"Token count ({len(self._subscribed_tokens)}) exceeds limit "
                f"({self.MAX_TOKENS_PER_CONNECTION}). Using first {self.MAX_TOKENS_PER_CONNECTION}."
            )
            tokens = self._subscribed_tokens[:self.MAX_TOKENS_PER_CONNECTION]
        else:
            tokens = self._subscribed_tokens

        logger.info(f"Subscribing to {len(tokens)} tokens in FULL mode")
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_FULL, tokens)

    def _handle_close(self, ws: Any, code: int, reason: str) -> None:
        """Handle WebSocket close."""
        logger.warning(f"WebSocket closed: code={code}, reason={reason}")

        if self._on_close_callback:
            try:
                self._on_close_callback(code, reason)
            except Exception as e:
                logger.error(f"Error in close callback: {e}")

    def _handle_error(self, ws: Any, code: int, reason: str) -> None:
        """Handle WebSocket error."""
        logger.error(f"WebSocket error: code={code}, reason={reason}")
        self._stats["errors"] += 1

    def _handle_reconnect(self, ws: Any, attempts_count: int) -> None:
        """Handle reconnection attempt."""
        logger.info(f"WebSocket reconnecting, attempt {attempts_count}")
        self._stats["reconnect_count"] += 1

    def _handle_noreconnect(self, ws: Any) -> None:
        """Handle max reconnection attempts reached."""
        logger.error("WebSocket max reconnection attempts reached")
        self._running = False

    def set_tokens(self, tokens: List[int]) -> None:
        """
        Set the tokens to subscribe to.

        Args:
            tokens: List of instrument tokens.
        """
        self._subscribed_tokens = tokens
        logger.info(f"Set {len(tokens)} tokens for subscription")

    def on_tick(self, callback: Callable[[List[Dict]], None]) -> None:
        """Register callback for tick events."""
        self._on_tick_callback = callback

    def on_connect(self, callback: Callable[[], None]) -> None:
        """Register callback for connect events."""
        self._on_connect_callback = callback

    def on_close(self, callback: Callable[[int, str], None]) -> None:
        """Register callback for close events."""
        self._on_close_callback = callback

    def start(self, threaded: bool = True) -> None:
        """
        Start the WebSocket connection.

        Args:
            threaded: If True, run in a background thread.
        """
        if self._running:
            logger.warning("Ticker stream already running")
            return

        self._ticker = self._create_ticker()
        self._running = True

        logger.info("Starting ticker stream")
        self._ticker.connect(threaded=threaded)

    def stop(self) -> None:
        """Stop the WebSocket connection."""
        if not self._running or not self._ticker:
            return

        logger.info("Stopping ticker stream")
        self._running = False

        try:
            self._ticker.close()
        except Exception as e:
            logger.error(f"Error closing ticker: {e}")

        self._ticker = None

    def is_running(self) -> bool:
        """Check if the ticker is running."""
        return self._running

    def get_latest_tick(self, token: int) -> Optional[Dict[str, Any]]:
        """
        Get the latest tick for a token.

        Args:
            token: Instrument token.

        Returns:
            Latest tick data or None.
        """
        with self._tick_lock:
            return self._latest_ticks.get(token)

    def get_all_latest_ticks(self) -> Dict[int, Dict[str, Any]]:
        """
        Get all latest ticks.

        Returns:
            Copy of latest ticks dictionary.
        """
        with self._tick_lock:
            return dict(self._latest_ticks)

    def get_stats(self) -> Dict[str, Any]:
        """Get streaming statistics."""
        return dict(self._stats)

    def clear_ticks(self) -> None:
        """Clear the latest ticks cache."""
        with self._tick_lock:
            self._latest_ticks.clear()


class MultiTickerStream:
    """
    Manages multiple WebSocket connections for large token sets.

    Kite Connect limits 3000 tokens per connection and 3 connections per API key.
    """

    MAX_CONNECTIONS = 3

    def __init__(
        self,
        api_key: str,
        access_token: str,
        reconnect: bool = True,
        reconnect_max_tries: int = 50,
        reconnect_max_delay: int = 30,
    ):
        """Initialize MultiTickerStream."""
        self.api_key = api_key
        self.access_token = access_token
        self.reconnect = reconnect
        self.reconnect_max_tries = reconnect_max_tries
        self.reconnect_max_delay = reconnect_max_delay

        self._streams: List[TickerStream] = []
        self._token_to_stream: Dict[int, TickerStream] = {}
        self._combined_ticks: Dict[int, Dict[str, Any]] = {}
        self._tick_lock = threading.RLock()

        self._on_tick_callback: Optional[Callable[[List[Dict]], None]] = None

    def _handle_combined_ticks(self, ticks: List[Dict]) -> None:
        """Handle ticks from any stream."""
        with self._tick_lock:
            for tick in ticks:
                token = tick.get("instrument_token")
                if token is not None:
                    self._combined_ticks[token] = tick

        if self._on_tick_callback:
            try:
                self._on_tick_callback(ticks)
            except Exception as e:
                logger.error(f"Error in combined tick callback: {e}")

    def set_tokens(self, tokens: List[int]) -> None:
        """
        Set tokens and distribute across connections.

        Args:
            tokens: List of all instrument tokens.
        """
        # Calculate how to split tokens
        max_per_stream = TickerStream.MAX_TOKENS_PER_CONNECTION
        max_total = max_per_stream * self.MAX_CONNECTIONS

        if len(tokens) > max_total:
            logger.warning(
                f"Token count ({len(tokens)}) exceeds max capacity "
                f"({max_total}). Truncating."
            )
            tokens = tokens[:max_total]

        # Split tokens into chunks
        chunks = []
        for i in range(0, len(tokens), max_per_stream):
            chunk = tokens[i:i + max_per_stream]
            chunks.append(chunk)

        # Create streams for each chunk
        self._streams.clear()
        self._token_to_stream.clear()

        for chunk in chunks:
            stream = TickerStream(
                api_key=self.api_key,
                access_token=self.access_token,
                reconnect=self.reconnect,
                reconnect_max_tries=self.reconnect_max_tries,
                reconnect_max_delay=self.reconnect_max_delay,
            )
            stream.set_tokens(chunk)
            stream.on_tick(self._handle_combined_ticks)
            self._streams.append(stream)

            for token in chunk:
                self._token_to_stream[token] = stream

        logger.info(f"Configured {len(self._streams)} streams for {len(tokens)} tokens")

    def on_tick(self, callback: Callable[[List[Dict]], None]) -> None:
        """Register callback for tick events."""
        self._on_tick_callback = callback

    def start(self) -> None:
        """Start all WebSocket connections."""
        for i, stream in enumerate(self._streams):
            logger.info(f"Starting stream {i + 1}/{len(self._streams)}")
            stream.start(threaded=True)

    def stop(self) -> None:
        """Stop all WebSocket connections."""
        for stream in self._streams:
            stream.stop()

    def get_latest_tick(self, token: int) -> Optional[Dict[str, Any]]:
        """Get the latest tick for a token."""
        with self._tick_lock:
            return self._combined_ticks.get(token)

    def get_all_latest_ticks(self) -> Dict[int, Dict[str, Any]]:
        """Get all latest ticks."""
        with self._tick_lock:
            return dict(self._combined_ticks)

    def get_stats(self) -> Dict[str, Any]:
        """Get combined stats from all streams."""
        total_stats = {
            "ticks_received": 0,
            "reconnect_count": 0,
            "errors": 0,
            "streams": len(self._streams),
        }
        for stream in self._streams:
            stats = stream.get_stats()
            total_stats["ticks_received"] += stats.get("ticks_received", 0)
            total_stats["reconnect_count"] += stats.get("reconnect_count", 0)
            total_stats["errors"] += stats.get("errors", 0)
        return total_stats
