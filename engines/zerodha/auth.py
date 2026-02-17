"""
Zerodha Kite Connect Authentication Module

Handles:
- API session creation via KiteConnect
- Request token to access token exchange
- Access token persistence and validation
"""

import os
import json
import logging
from pathlib import Path
from datetime import datetime, date
from typing import Optional

from kiteconnect import KiteConnect
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


class KiteAuth:
    """Manages Kite Connect authentication lifecycle."""

    TOKEN_FILE = ".kite_token.json"

    def __init__(self, base_path: Optional[Path] = None):
        """
        Initialize KiteAuth.

        Args:
            base_path: Base directory for token storage. Defaults to project root.
        """
        load_dotenv()

        self.api_key = os.getenv("KITE_API_KEY")
        self.api_secret = os.getenv("KITE_API_SECRET")
        self.redirect_url = os.getenv("KITE_REDIRECT_URL", "")

        if not self.api_key or not self.api_secret:
            raise ValueError("KITE_API_KEY and KITE_API_SECRET must be set in .env")

        self.base_path = base_path or Path(__file__).parent.parent.parent
        self.token_path = self.base_path / self.TOKEN_FILE
        self.kite = KiteConnect(api_key=self.api_key)
        self._access_token: Optional[str] = None

    @property
    def login_url(self) -> str:
        """Get the Kite Connect login URL for browser-based auth."""
        return self.kite.login_url()

    def generate_session(self, request_token: str) -> str:
        """
        Exchange request_token for access_token and persist it.

        Args:
            request_token: Token received from redirect callback.

        Returns:
            The generated access token.
        """
        logger.info("Generating session from request token")
        data = self.kite.generate_session(request_token, api_secret=self.api_secret)
        access_token = data["access_token"]
        self._persist_token(access_token)
        self._access_token = access_token
        self.kite.set_access_token(access_token)
        logger.info("Session generated and access token persisted")
        return access_token

    def _persist_token(self, access_token: str) -> None:
        """Save access token with date to file."""
        token_data = {
            "access_token": access_token,
            "date": date.today().isoformat(),
            "timestamp": datetime.now().isoformat(),
        }
        with open(self.token_path, "w") as f:
            json.dump(token_data, f, indent=2)
        logger.debug(f"Token persisted to {self.token_path}")

    def _load_persisted_token(self) -> Optional[str]:
        """Load today's access token from file if available."""
        if not self.token_path.exists():
            return None

        try:
            with open(self.token_path, "r") as f:
                data = json.load(f)
            if data.get("date") == date.today().isoformat():
                return data.get("access_token")
            logger.info("Persisted token is from a previous day, ignoring")
            return None
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to load persisted token: {e}")
            return None

    def get_access_token(self) -> Optional[str]:
        """
        Get a valid access token.

        Checks in order:
        1. In-memory cached token
        2. Today's persisted token file
        3. KITE_ACCESS_TOKEN environment variable

        Returns:
            Access token if available, None otherwise.
        """
        # Check in-memory first
        if self._access_token:
            return self._access_token

        # Try loading from persisted file
        token = self._load_persisted_token()
        if token:
            self._access_token = token
            self.kite.set_access_token(token)
            logger.info("Loaded access token from persisted file")
            return token

        # Fall back to environment variable
        env_token = os.getenv("KITE_ACCESS_TOKEN")
        if env_token:
            self._access_token = env_token
            self.kite.set_access_token(env_token)
            self._persist_token(env_token)
            logger.info("Using access token from environment variable")
            return env_token

        return None

    def set_access_token(self, access_token: str, persist: bool = True) -> None:
        """
        Manually set and optionally persist an access token.

        Args:
            access_token: The access token to set.
            persist: Whether to save to file.
        """
        self._access_token = access_token
        self.kite.set_access_token(access_token)
        if persist:
            self._persist_token(access_token)
        logger.info("Access token set manually")

    def validate_session(self) -> bool:
        """
        Validate current session by making a lightweight API call.

        Returns:
            True if session is valid, False otherwise.
        """
        token = self.get_access_token()
        if not token:
            logger.warning("No access token available to validate")
            return False

        try:
            profile = self.kite.profile()
            logger.info(f"Session valid for user: {profile.get('user_name', 'unknown')}")
            return True
        except Exception as e:
            logger.error(f"Session validation failed: {e}")
            self._access_token = None
            return False

    def get_kite_client(self) -> KiteConnect:
        """
        Get a configured KiteConnect client.

        Returns:
            KiteConnect instance with access token set.

        Raises:
            RuntimeError: If no valid access token is available.
        """
        token = self.get_access_token()
        if not token:
            raise RuntimeError(
                "No access token available. Either:\n"
                "1. Set KITE_ACCESS_TOKEN in .env\n"
                "2. Run generate_session() with a request_token\n"
                f"3. Visit {self.login_url} to get request_token"
            )
        return self.kite

    def interactive_login(self) -> str:
        """
        Perform interactive login flow (for CLI use).

        Prints login URL and prompts for request token from redirect.

        Returns:
            Access token after successful login.
        """
        print("\n" + "=" * 60)
        print("KITE CONNECT LOGIN")
        print("=" * 60)
        print(f"\n1. Open this URL in your browser:\n\n   {self.login_url}\n")
        print("2. Log in with your Zerodha credentials")
        print("3. After redirect, copy the 'request_token' from the URL")
        print("   (It's the value after ?request_token= in the URL)\n")
        print("=" * 60)

        request_token = input("\nEnter the request_token: ").strip()
        if not request_token:
            raise ValueError("Request token cannot be empty")

        return self.generate_session(request_token)


def get_authenticated_client(base_path: Optional[Path] = None) -> KiteConnect:
    """
    Convenience function to get an authenticated KiteConnect client.

    Args:
        base_path: Base directory for token storage.

    Returns:
        Authenticated KiteConnect instance.
    """
    auth = KiteAuth(base_path)
    return auth.get_kite_client()


if __name__ == "__main__":
    # Interactive login for testing
    logging.basicConfig(level=logging.INFO)
    auth = KiteAuth()

    if auth.validate_session():
        print("Existing session is valid!")
    else:
        auth.interactive_login()
        if auth.validate_session():
            print("Login successful!")
        else:
            print("Login failed!")
