"""
Option Universe Selector

Builds the set of option contracts to stream based on:
- Configured underlyings
- Expiry selection rules (nearest, weekly, monthly, explicit)
- Strike band around ATM
"""

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Set

import pandas as pd
from kiteconnect import KiteConnect

from engines.zerodha.instruments import InstrumentLoader

logger = logging.getLogger(__name__)


class OptionUniverse:
    """Selects and manages the option contracts universe for streaming."""

    def __init__(
        self,
        instrument_loader: InstrumentLoader,
        underlyings: List[str],
        expiries_mode: str = "nearest",
        expiry_list: Optional[List[str]] = None,
        max_strike_distance: float = 2500,
        strike_step_overrides: Optional[Dict[str, float]] = None,
    ):
        """
        Initialize OptionUniverse.

        Args:
            instrument_loader: InstrumentLoader instance for fetching instruments.
            underlyings: List of underlying symbols (e.g., ["NIFTY", "BANKNIFTY"]).
            expiries_mode: How to select expiries: "nearest", "weekly", "monthly", "explicit_list".
            expiry_list: Explicit list of expiry dates (YYYY-MM-DD format) when mode is explicit_list.
            max_strike_distance: Maximum distance from ATM in points.
            strike_step_overrides: Override strike step per underlying.
        """
        self.loader = instrument_loader
        self.underlyings = underlyings
        self.expiries_mode = expiries_mode
        self.expiry_list = expiry_list or []
        self.max_strike_distance = max_strike_distance
        self.strike_step_overrides = strike_step_overrides or {}

        # token -> contract metadata
        self._universe: Dict[int, Dict[str, Any]] = {}
        # underlying -> spot price (updated dynamically)
        self._spot_prices: Dict[str, float] = {}
        # underlying -> selected expiry dates
        self._selected_expiries: Dict[str, List[date]] = {}

    def set_spot_price(self, underlying: str, spot: float) -> None:
        """Update spot price for an underlying (used for ATM calculation)."""
        self._spot_prices[underlying] = spot

    def get_spot_price(self, underlying: str) -> Optional[float]:
        """Get the current spot price for an underlying."""
        return self._spot_prices.get(underlying)

    def _select_expiries(self, underlying: str) -> List[date]:
        """
        Select expiries for an underlying based on mode.

        Returns:
            List of selected expiry dates.
        """
        all_expiries = self.loader.get_unique_expiries(underlying)
        today = date.today()

        # Filter to valid (future) expiries
        valid_expiries = [e for e in all_expiries if e >= today]

        if not valid_expiries:
            logger.warning(f"No valid expiries found for {underlying}")
            return []

        if self.expiries_mode == "nearest":
            return valid_expiries[:1]

        elif self.expiries_mode == "weekly":
            # Return all weekly expiries (typically the nearest few)
            return valid_expiries[:4]

        elif self.expiries_mode == "monthly":
            # Return only monthly expiries (last Thursday of month)
            monthly = []
            seen_months: Set[tuple] = set()
            for exp in valid_expiries:
                key = (exp.year, exp.month)
                if key not in seen_months:
                    # Check if this is likely a monthly expiry (approximation)
                    # Monthly expiries are typically the last Thursday
                    if exp.day >= 22:
                        monthly.append(exp)
                        seen_months.add(key)
            return monthly[:3]

        elif self.expiries_mode == "explicit_list":
            explicit_dates = []
            for exp_str in self.expiry_list:
                try:
                    exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
                    if exp_date in valid_expiries:
                        explicit_dates.append(exp_date)
                except ValueError:
                    logger.warning(f"Invalid expiry date format: {exp_str}")
            return explicit_dates

        else:
            logger.warning(f"Unknown expiries_mode: {self.expiries_mode}, defaulting to nearest")
            return valid_expiries[:1]

    def build_universe(
        self,
        spot_prices: Optional[Dict[str, float]] = None,
    ) -> Dict[int, Dict[str, Any]]:
        """
        Build the option universe based on configuration.

        Args:
            spot_prices: Optional dict of underlying -> spot price for ATM calculation.

        Returns:
            Dict mapping instrument_token to contract metadata.
        """
        if spot_prices:
            self._spot_prices.update(spot_prices)

        self._universe.clear()
        self._selected_expiries.clear()

        for underlying in self.underlyings:
            spot = self._spot_prices.get(underlying)
            if spot is None:
                logger.warning(f"No spot price for {underlying}, using wide strike band")
                # Use a very large band if no spot price
                effective_spot = 25000 if underlying == "NIFTY" else 50000
            else:
                effective_spot = spot

            # Select expiries
            expiries = self._select_expiries(underlying)
            self._selected_expiries[underlying] = expiries

            if not expiries:
                logger.warning(f"No expiries selected for {underlying}")
                continue

            logger.info(f"{underlying}: selected expiries {expiries}")

            # Get options for each expiry
            for expiry in expiries:
                options_df = self.loader.filter_options(underlying, expiry)

                if options_df.empty:
                    logger.warning(f"No options found for {underlying} expiry {expiry}")
                    continue

                # Filter by strike distance
                options_df = options_df[
                    (options_df["strike"] >= effective_spot - self.max_strike_distance) &
                    (options_df["strike"] <= effective_spot + self.max_strike_distance)
                ]

                # Add to universe
                for _, row in options_df.iterrows():
                    token = int(row["instrument_token"])
                    expiry_date = row["expiry"]
                    if isinstance(expiry_date, pd.Timestamp):
                        expiry_str = expiry_date.strftime("%Y-%m-%d")
                        expiry_yyyymmdd = expiry_date.strftime("%Y%m%d")
                    else:
                        expiry_str = str(expiry_date)
                        expiry_yyyymmdd = expiry_str.replace("-", "")

                    strike = float(row["strike"])
                    opt_type = row["instrument_type"]  # CE or PE

                    # Build deterministic instrument_id
                    instrument_id = f"{underlying}_{expiry_yyyymmdd}_{int(strike)}{opt_type}"

                    self._universe[token] = {
                        "instrument_token": token,
                        "tradingsymbol": row["tradingsymbol"],
                        "underlying": underlying,
                        "expiry": expiry_str,
                        "expiry_yyyymmdd": expiry_yyyymmdd,
                        "strike": strike,
                        "option_type": opt_type,
                        "option_type_short": "C" if opt_type == "CE" else "P",
                        "instrument_id": instrument_id,
                        "lot_size": row.get("lot_size", 1),
                    }

        logger.info(f"Built universe with {len(self._universe)} option contracts")
        return self._universe

    def get_tokens(self) -> List[int]:
        """Get list of all instrument tokens in universe."""
        return list(self._universe.keys())

    def get_universe(self) -> Dict[int, Dict[str, Any]]:
        """Get the full universe dictionary."""
        return self._universe

    def get_contract_meta(self, token: int) -> Optional[Dict[str, Any]]:
        """Get metadata for a specific token."""
        return self._universe.get(token)

    def get_selected_expiries(self, underlying: str) -> List[date]:
        """Get the selected expiries for an underlying."""
        return self._selected_expiries.get(underlying, [])

    def get_underlyings(self) -> List[str]:
        """Get list of configured underlyings."""
        return self.underlyings

    def refresh_universe(
        self,
        spot_prices: Optional[Dict[str, float]] = None,
    ) -> Dict[int, Dict[str, Any]]:
        """
        Refresh the universe (e.g., when spot price changes significantly).

        Args:
            spot_prices: Updated spot prices.

        Returns:
            Updated universe dictionary.
        """
        return self.build_universe(spot_prices)

    def get_tokens_by_underlying(self, underlying: str) -> List[int]:
        """Get tokens for a specific underlying."""
        return [
            token for token, meta in self._universe.items()
            if meta["underlying"] == underlying
        ]

    def summary(self) -> str:
        """Get a summary string of the universe."""
        lines = [f"Option Universe: {len(self._universe)} contracts"]
        for underlying in self.underlyings:
            tokens = self.get_tokens_by_underlying(underlying)
            expiries = self._selected_expiries.get(underlying, [])
            lines.append(f"  {underlying}: {len(tokens)} contracts, expiries: {expiries}")
        return "\n".join(lines)


if __name__ == "__main__":
    # Test building universe
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))

    from engines.zerodha.auth import get_authenticated_client
    from engines.zerodha.instruments import InstrumentLoader

    logging.basicConfig(level=logging.INFO)

    kite = get_authenticated_client()
    loader = InstrumentLoader(
        kite=kite,
        cache_dir=Path(__file__).parent.parent.parent / "archive" / "instruments",
    )
    loader.fetch_instruments()

    universe = OptionUniverse(
        instrument_loader=loader,
        underlyings=["NIFTY", "BANKNIFTY"],
        expiries_mode="nearest",
        max_strike_distance=2500,
    )

    # Simulate spot prices
    spot_prices = {"NIFTY": 26200, "BANKNIFTY": 55000}
    universe.build_universe(spot_prices)

    print(universe.summary())
    print(f"\nSample tokens: {universe.get_tokens()[:5]}")
