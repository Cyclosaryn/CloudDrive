"""OAuth2 authentication via MSAL for Microsoft Graph API.

Supports both interactive (browser-based) and device-code flows.
Tokens are cached securely using the system keyring.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import msal  # type: ignore[import-untyped]

from clouddrive.core.config import AppConfig, SCOPES, sanitize_scopes

logger = logging.getLogger(__name__)


class TokenCache:
    """Persistent MSAL token cache backed by a file.

    The token cache file is stored in the user's data directory
    with restrictive permissions.
    """

    def __init__(self, cache_path: Path) -> None:
        self._path = cache_path
        self._cache = msal.SerializableTokenCache()
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            self._cache.deserialize(self._path.read_text(encoding="utf-8"))

    def save(self) -> None:
        if self._cache.has_state_changed:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                self._cache.serialize(), encoding="utf-8"
            )
            # Restrict permissions (owner read/write only)
            try:
                self._path.chmod(0o600)
            except OSError:
                pass  # Windows doesn't support chmod the same way

    @property
    def cache(self) -> msal.SerializableTokenCache:
        return self._cache


class AuthManager:
    """Manages authentication with Microsoft Graph API via MSAL."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._token_cache = TokenCache(config.token_cache_file)
        self._app: msal.PublicClientApplication | None = None

    @property
    def _msal_app(self) -> msal.PublicClientApplication:
        if self._app is None:
            if not self._config.auth.client_id:
                raise ValueError(
                    "No client_id configured. Register an application at "
                    "https://portal.azure.com and set auth.client_id in your config."
                )
            self._app = msal.PublicClientApplication(
                client_id=self._config.auth.client_id,
                authority=self._config.auth.authority,
                token_cache=self._token_cache.cache,
            )
        return self._app

    @property
    def is_authenticated(self) -> bool:
        """Check if we have valid cached credentials."""
        accounts = self._msal_app.get_accounts()
        if not accounts:
            return False
        result = self._msal_app.acquire_token_silent(
            scopes=self._config.auth.scopes,
            account=accounts[0],
        )
        return result is not None and "access_token" in result

    def get_access_token(self) -> str | None:
        """Get a valid access token, refreshing silently if needed."""
        accounts = self._msal_app.get_accounts()
        if accounts:
            result = self._msal_app.acquire_token_silent(
                scopes=self._config.auth.scopes,
                account=accounts[0],
            )
            if result and "access_token" in result:
                self._token_cache.save()
                return result["access_token"]

        logger.warning("No valid cached token available. Re-authentication required.")
        return None

    def authenticate_interactive(self) -> dict[str, Any] | None:
        """Initiate interactive browser-based authentication.

        Opens the system browser for the user to sign in.
        Returns the token result dict or None on failure.
        """
        try:
            scopes = sanitize_scopes(list(self._config.auth.scopes))
            logger.info(f"MSAL scopes used for authentication: {scopes}")
            result = self._msal_app.acquire_token_interactive(
                scopes=scopes,
                port=int(self._config.auth.redirect_uri.split(":")[-1]),
            )
            if "access_token" in result:
                self._token_cache.save()
                logger.info("Interactive authentication successful.")
                return result
            else:
                logger.error("Authentication failed: %s", result.get("error_description", "Unknown error"))
                return None
        except Exception:
            logger.exception("Interactive authentication failed")
            return None

    def authenticate_device_code(self, callback: Any = None) -> dict[str, Any] | None:
        """Initiate device-code flow (for headless/terminal environments).

        Args:
            callback: Optional callable that receives the device code info dict
                      (containing 'user_code' and 'verification_uri') for display.

        Returns the token result dict or None on failure.
        """
        scopes = sanitize_scopes(self._config.auth.scopes)
        flow = self._msal_app.initiate_device_flow(scopes=scopes)

        if "user_code" not in flow:
            logger.error("Device code flow initiation failed: %s", flow.get("error_description"))
            return None

        if callback:
            callback(flow)
        else:
            print(f"\n  To sign in, visit: {flow['verification_uri']}")
            print(f"  Enter the code:    {flow['user_code']}\n")

        result = self._msal_app.acquire_token_by_device_flow(flow)
        if "access_token" in result:
            self._token_cache.save()
            logger.info("Device code authentication successful.")
            return result
        else:
            logger.error("Device code auth failed: %s", result.get("error_description"))
            return None

    def get_account_info(self) -> dict[str, str] | None:
        """Get basic info about the currently signed-in account."""
        accounts = self._msal_app.get_accounts()
        if accounts:
            account = accounts[0]
            return {
                "username": account.get("username", "Unknown"),
                "name": account.get("name", account.get("username", "Unknown")),
                "environment": account.get("environment", ""),
            }
        return None

    def sign_out(self) -> None:
        """Remove all cached accounts and tokens."""
        accounts = self._msal_app.get_accounts()
        for account in accounts:
            self._msal_app.remove_account(account)
        self._token_cache.save()
        # Also remove the cache file
        if self._config.token_cache_file.exists():
            self._config.token_cache_file.unlink()
        self._app = None
        logger.info("Signed out successfully.")
