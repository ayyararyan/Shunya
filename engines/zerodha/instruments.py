"""
Zerodha Instrument Master Loader

Handles:
- Fetching NFO instruments from Kite Connect
- Caching daily instrument dumps to CSV
- Building lookup dictionaries by instrument_token and tradingsymbol
"""

import logging
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Any

import pandas as pd
from kiteconnect import KiteConnect

logger = logging.getLogger(__name__)


class InstrumentLoader:
    """Loads and caches Zerodha instrument master data."""

    def __init__(
        self,
        kite: KiteConnect,
        cache_dir: Path,
        exchange: str = "NFO",
    ):
        """
        Initialize InstrumentLoader.

        Args:
            kite: Authenticated KiteConnect client.
            cache_dir: Directory to store instrument dump CSVs.
            exchange: Exchange to fetch instruments for.
        """
        self.kite = kite
        self.cache_dir = Path(cache_dir)
        self.exchange = exchange
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self._instruments_df: Optional[pd.DataFrame] = None
        self._token_to_meta: Dict[int, Dict[str, Any]] = {}
        self._symbol_to_token: Dict[str, int] = {}

    def _get_cache_path(self, for_date: Optional[date] = None) -> Path:
        """Get cache file path for a given date."""
        target_date = for_date or date.today()
        filename = f"instruments_{self.exchange}_{target_date.strftime('%Y%m%d')}.csv"
        return self.cache_dir / filename

    def _load_from_cache(self, for_date: Optional[date] = None) -> Optional[pd.DataFrame]:
        """Load instruments from cache if available."""
        cache_path = self._get_cache_path(for_date)
        if cache_path.exists():
            logger.info(f"Loading instruments from cache: {cache_path}")
            df = pd.read_csv(cache_path, parse_dates=["expiry"])
            return df
        return None

    def _save_to_cache(self, df: pd.DataFrame, for_date: Optional[date] = None) -> None:
        """Save instruments to cache."""
        cache_path = self._get_cache_path(for_date)
        df.to_csv(cache_path, index=False)
        logger.info(f"Saved {len(df)} instruments to cache: {cache_path}")

    def fetch_instruments(self, force_refresh: bool = False) -> pd.DataFrame:
        """
        Fetch instruments from Kite API or cache.

        Args:
            force_refresh: If True, bypass cache and fetch from API.

        Returns:
            DataFrame of instruments.
        """
        if not force_refresh:
            cached = self._load_from_cache()
            if cached is not None:
                self._instruments_df = cached
                self._build_lookups()
                return cached

        logger.info(f"Fetching {self.exchange} instruments from Kite API")
        instruments = self.kite.instruments(self.exchange)

        if not instruments:
            raise RuntimeError(f"No instruments returned for exchange {self.exchange}")

        df = pd.DataFrame(instruments)

        # Convert expiry to datetime
        if "expiry" in df.columns:
            df["expiry"] = pd.to_datetime(df["expiry"])

        self._save_to_cache(df)
        self._instruments_df = df
        self._build_lookups()

        logger.info(f"Loaded {len(df)} instruments from {self.exchange}")
        return df

    def _build_lookups(self) -> None:
        """Build lookup dictionaries from instruments DataFrame."""
        if self._instruments_df is None:
            return

        self._token_to_meta.clear()
        self._symbol_to_token.clear()

        for _, row in self._instruments_df.iterrows():
            token = int(row["instrument_token"])
            symbol = row["tradingsymbol"]

            self._token_to_meta[token] = {
                "tradingsymbol": symbol,
                "name": row.get("name", ""),
                "expiry": row.get("expiry"),
                "strike": row.get("strike"),
                "instrument_type": row.get("instrument_type", ""),
                "lot_size": row.get("lot_size", 1),
                "tick_size": row.get("tick_size", 0.05),
                "exchange": row.get("exchange", self.exchange),
                "segment": row.get("segment", ""),
            }
            self._symbol_to_token[symbol] = token

        logger.debug(f"Built lookups: {len(self._token_to_meta)} tokens, {len(self._symbol_to_token)} symbols")

    def get_by_token(self, token: int) -> Optional[Dict[str, Any]]:
        """Get instrument metadata by token."""
        return self._token_to_meta.get(token)

    def get_token_by_symbol(self, symbol: str) -> Optional[int]:
        """Get instrument token by tradingsymbol."""
        return self._symbol_to_token.get(symbol)

    def get_instruments_df(self) -> pd.DataFrame:
        """Get the full instruments DataFrame."""
        if self._instruments_df is None:
            return self.fetch_instruments()
        return self._instruments_df

    def filter_options(
        self,
        underlying: str,
        expiry: Optional[date] = None,
        option_types: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        """
        Filter instruments to options for a given underlying.

        Args:
            underlying: Underlying name (e.g., "NIFTY", "BANKNIFTY").
            expiry: Filter to specific expiry date.
            option_types: Filter to specific option types (e.g., ["CE", "PE"]).

        Returns:
            Filtered DataFrame of option instruments.
        """
        df = self.get_instruments_df()

        # Filter for options (CE or PE)
        mask = df["instrument_type"].isin(["CE", "PE"])

        # Filter by underlying name
        mask &= df["name"] == underlying

        if expiry is not None:
            if isinstance(expiry, str):
                expiry = pd.to_datetime(expiry).date()
            mask &= pd.to_datetime(df["expiry"]).dt.date == expiry

        if option_types:
            mask &= df["instrument_type"].isin(option_types)

        return df[mask].copy()

    def get_unique_expiries(self, underlying: str) -> List[date]:
        """
        Get sorted list of unique expiry dates for an underlying.

        Args:
            underlying: Underlying name.

        Returns:
            List of expiry dates, sorted ascending.
        """
        df = self.filter_options(underlying)
        if df.empty:
            return []

        expiries = pd.to_datetime(df["expiry"]).dt.date.unique()
        return sorted(expiries)

    def get_nearest_expiry(self, underlying: str, after_date: Optional[date] = None) -> Optional[date]:
        """
        Get the nearest expiry date for an underlying.

        Args:
            underlying: Underlying name.
            after_date: Only consider expiries after this date. Defaults to today.

        Returns:
            Nearest expiry date or None if no valid expiries.
        """
        reference = after_date or date.today()
        expiries = self.get_unique_expiries(underlying)

        for exp in expiries:
            if exp >= reference:
                return exp

        return None

    def get_strikes_around_atm(
        self,
        underlying: str,
        expiry: date,
        atm_price: float,
        max_distance: float,
        step: Optional[float] = None,  # noqa: ARG002 - reserved for future use
    ) -> List[float]:
        """
        Get strikes within a distance from ATM.

        Args:
            underlying: Underlying name.
            expiry: Expiry date.
            atm_price: Current ATM price.
            max_distance: Maximum distance from ATM in points.
            step: Strike step size (inferred from data if None).

        Returns:
            List of strike prices within range.
        """
        df = self.filter_options(underlying, expiry)
        if df.empty:
            return []

        strikes = sorted(df["strike"].unique())

        lower_bound = atm_price - max_distance
        upper_bound = atm_price + max_distance

        filtered = [s for s in strikes if lower_bound <= s <= upper_bound]
        return filtered


if __name__ == "__main__":
    # Test loading instruments
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))

    from engines.zerodha.auth import get_authenticated_client

    logging.basicConfig(level=logging.INFO)

    kite = get_authenticated_client()
    loader = InstrumentLoader(
        kite=kite,
        cache_dir=Path(__file__).parent.parent.parent / "archive" / "instruments",
    )

    df = loader.fetch_instruments()
    print(f"Total instruments: {len(df)}")

    for underlying in ["NIFTY", "BANKNIFTY", "FINNIFTY"]:
        expiries = loader.get_unique_expiries(underlying)
        nearest = loader.get_nearest_expiry(underlying)
        print(f"{underlying}: {len(expiries)} expiries, nearest: {nearest}")
