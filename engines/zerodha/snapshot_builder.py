"""
Snapshot Builder

Transforms raw Kite Connect tick data into normalized option chain snapshots
matching the sample.csv schema.

Output columns:
ts, venue, underlying_symbol, underlying_spot, instrument_id, option_symbol,
expiry_date, strike, option_type, best_bid_px, best_bid_sz, best_ask_px,
best_ask_sz, mid_px, spread, last_trade_px, last_trade_sz, bid_px_1, bid_sz_1,
bid_px_2, bid_sz_2, bid_px_3, bid_sz_3, ask_px_1, ask_sz_1, ask_px_2, ask_sz_2,
ask_px_3, ask_sz_3
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple

import pytz

logger = logging.getLogger(__name__)

# Column order matching sample.csv
COLUMNS = [
    "ts",
    "venue",
    "underlying_symbol",
    "underlying_spot",
    "instrument_id",
    "option_symbol",
    "expiry_date",
    "strike",
    "option_type",
    "best_bid_px",
    "best_bid_sz",
    "best_ask_px",
    "best_ask_sz",
    "mid_px",
    "spread",
    "last_trade_px",
    "last_trade_sz",
    "bid_px_1",
    "bid_sz_1",
    "bid_px_2",
    "bid_sz_2",
    "bid_px_3",
    "bid_sz_3",
    "ask_px_1",
    "ask_sz_1",
    "ask_px_2",
    "ask_sz_2",
    "ask_px_3",
    "ask_sz_3",
]


class SnapshotBuilder:
    """Builds normalized snapshots from raw tick data."""

    def __init__(
        self,
        venue_label: str = "NSE-FO",
        timezone: str = "Asia/Kolkata",
    ):
        """
        Initialize SnapshotBuilder.

        Args:
            venue_label: Venue string for output (e.g., "NSE-FO").
            timezone: Timezone for timestamp conversion.
        """
        self.venue_label = venue_label
        self.tz = pytz.timezone(timezone)

        # underlying -> latest spot price
        self._spot_prices: Dict[str, float] = {}

    def set_spot_price(self, underlying: str, spot: float) -> None:
        """Update spot price for an underlying."""
        self._spot_prices[underlying] = spot

    def get_spot_price(self, underlying: str) -> Optional[float]:
        """Get current spot price for an underlying."""
        return self._spot_prices.get(underlying)

    def _ts_to_micros(self, dt: Optional[datetime] = None) -> int:
        """
        Convert datetime to microseconds since Unix epoch.

        Args:
            dt: Datetime object. Uses current time if None.

        Returns:
            Microseconds since epoch.
        """
        if dt is None:
            dt = datetime.now(self.tz)
        elif dt.tzinfo is None:
            dt = self.tz.localize(dt)

        epoch = datetime(1970, 1, 1, tzinfo=pytz.UTC)
        delta = dt.astimezone(pytz.UTC) - epoch
        return int(delta.total_seconds() * 1_000_000)

    def _extract_depth(
        self,
        tick: Dict[str, Any],
        side: str,
        levels: int = 3,
    ) -> List[Tuple[Optional[float], Optional[int]]]:
        """
        Extract bid/ask depth from tick.

        Args:
            tick: Raw tick data.
            side: "buy" for bids, "sell" for asks.
            levels: Number of levels to extract.

        Returns:
            List of (price, quantity) tuples.
        """
        depth = tick.get("depth", {}).get(side, [])
        result = []

        for i in range(levels):
            if i < len(depth):
                level = depth[i]
                price = level.get("price")
                qty = level.get("quantity")
                # Convert 0 price to None for empty levels
                if price == 0:
                    price = None
                    qty = None
                result.append((price, qty))
            else:
                result.append((None, None))

        return result

    def _compute_mid_spread(
        self,
        best_bid: Optional[float],
        best_ask: Optional[float],
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Compute mid price and spread.

        Returns:
            (mid_px, spread) tuple.
        """
        if best_bid is not None and best_ask is not None:
            mid = (best_bid + best_ask) / 2
            spread = best_ask - best_bid
            return mid, spread
        return None, None

    def build_row(
        self,
        tick: Dict[str, Any],
        contract_meta: Dict[str, Any],
        ts_micros: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Build a single snapshot row from tick and metadata.

        Args:
            tick: Raw tick data from KiteTicker.
            contract_meta: Contract metadata from OptionUniverse.
            ts_micros: Optional timestamp in microseconds. Uses current time if None.

        Returns:
            Dictionary with column values.
        """
        underlying = contract_meta.get("underlying", "")
        spot = self._spot_prices.get(underlying)

        # Timestamp
        if ts_micros is None:
            # Try to use tick timestamp if available
            tick_ts = tick.get("exchange_timestamp") or tick.get("timestamp")
            if tick_ts and isinstance(tick_ts, datetime):
                ts_micros = self._ts_to_micros(tick_ts)
            else:
                ts_micros = self._ts_to_micros()

        # Extract depth
        bids = self._extract_depth(tick, "buy", 3)
        asks = self._extract_depth(tick, "sell", 3)

        # Best bid/ask
        best_bid_px = bids[0][0] if bids else None
        best_bid_sz = bids[0][1] if bids else None
        best_ask_px = asks[0][0] if asks else None
        best_ask_sz = asks[0][1] if asks else None

        # Mid and spread
        mid_px, spread = self._compute_mid_spread(best_bid_px, best_ask_px)

        # Last trade
        last_trade_px = tick.get("last_price")
        last_trade_sz = tick.get("last_quantity", 0)

        # Build row
        row = {
            "ts": ts_micros,
            "venue": self.venue_label,
            "underlying_symbol": underlying,
            "underlying_spot": spot,
            "instrument_id": contract_meta.get("instrument_id", ""),
            "option_symbol": contract_meta.get("tradingsymbol", ""),
            "expiry_date": contract_meta.get("expiry", ""),
            "strike": contract_meta.get("strike"),
            "option_type": contract_meta.get("option_type_short", ""),
            "best_bid_px": best_bid_px,
            "best_bid_sz": best_bid_sz,
            "best_ask_px": best_ask_px,
            "best_ask_sz": best_ask_sz,
            "mid_px": mid_px,
            "spread": spread,
            "last_trade_px": last_trade_px,
            "last_trade_sz": last_trade_sz,
            # Bid levels
            "bid_px_1": bids[0][0] if len(bids) > 0 else None,
            "bid_sz_1": bids[0][1] if len(bids) > 0 else None,
            "bid_px_2": bids[1][0] if len(bids) > 1 else None,
            "bid_sz_2": bids[1][1] if len(bids) > 1 else None,
            "bid_px_3": bids[2][0] if len(bids) > 2 else None,
            "bid_sz_3": bids[2][1] if len(bids) > 2 else None,
            # Ask levels
            "ask_px_1": asks[0][0] if len(asks) > 0 else None,
            "ask_sz_1": asks[0][1] if len(asks) > 0 else None,
            "ask_px_2": asks[1][0] if len(asks) > 1 else None,
            "ask_sz_2": asks[1][1] if len(asks) > 1 else None,
            "ask_px_3": asks[2][0] if len(asks) > 2 else None,
            "ask_sz_3": asks[2][1] if len(asks) > 2 else None,
        }

        return row

    def build_snapshot(
        self,
        ticks: Dict[int, Dict[str, Any]],
        universe: Dict[int, Dict[str, Any]],
        ts_micros: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Build a full snapshot from all current ticks.

        Args:
            ticks: Dict mapping token -> latest tick.
            universe: Dict mapping token -> contract metadata.
            ts_micros: Optional timestamp in microseconds.

        Returns:
            List of row dictionaries.
        """
        if ts_micros is None:
            ts_micros = self._ts_to_micros()

        rows = []
        for token, meta in universe.items():
            tick = ticks.get(token, {})
            row = self.build_row(tick, meta, ts_micros)
            rows.append(row)

        return rows

    def format_row_for_csv(self, row: Dict[str, Any]) -> List[Any]:
        """
        Format a row dictionary for CSV writing.

        Args:
            row: Row dictionary.

        Returns:
            List of values in column order.
        """
        result = []
        for col in COLUMNS:
            val = row.get(col)
            # Handle None values - leave empty for optional fields
            if val is None:
                result.append("")
            else:
                result.append(val)
        return result

    @staticmethod
    def get_header() -> List[str]:
        """Get the CSV header row."""
        return COLUMNS.copy()


def format_csv_value(val: Any) -> str:
    """Format a value for CSV output."""
    if val is None or val == "":
        return ""
    if isinstance(val, float):
        # Avoid scientific notation for large numbers
        if abs(val) > 1e10:
            return str(int(val))
        return str(val)
    return str(val)
