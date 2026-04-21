"""Application-wide configuration management.

Provides a single source of truth for all settings, with defaults
that can be overridden via config file (~/.config/clouddrive/config.toml)
or environment variables.

Supports:
  - Multi-account configurations
  - Files On-Demand (placeholder) settings
  - Selective folder sync rules
  - Bandwidth scheduling (time-of-day limits)
  - SharePoint library sync
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir, user_data_dir, user_cache_dir


APP_NAME = "clouddrive"

# Microsoft Graph API endpoints
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_UPLOAD_URL = "https://graph.microsoft.com/v1.0/me/drive"
AUTH_AUTHORITY = "https://login.microsoftonline.com/common"

# Default Microsoft application registration
# This client ID is shipped with CloudDrive so end users can sign in
# without registering their own Azure app.  Users may override it in
# config.toml if they prefer to use their own registration.
DEFAULT_CLIENT_ID = "a944ee72-00b7-4d53-84f3-ebb93283545b"
REDIRECT_URI = "http://localhost:8400"

# Ensure reserved scopes are always last and not duplicated
def get_scopes() -> list[str]:
    base_scopes = [
        "Files.ReadWrite.All",
        "Sites.Read.All",
        "User.Read",
    ]
    reserved = ["offline_access"]
    return base_scopes + reserved

def sanitize_scopes(scopes: list[str]) -> list[str]:
    # Remove duplicates, preserve order, and ensure reserved scopes are last
    reserved = ["offline_access", "openid", "profile"]
    base = [s for s in scopes if s not in reserved]
    present_reserved = [s for s in reserved if s in scopes]
    return base + present_reserved

SCOPES = sanitize_scopes([
    "Files.ReadWrite.All",
    "Sites.Read.All",
    "User.Read",
    "offline_access",
])


@dataclass
class BandwidthScheduleRule:
    """A single bandwidth schedule rule (time-of-day based limits)."""

    start_hour: int = 9     # 24-hour format
    end_hour: int = 17
    days: list[str] = field(default_factory=lambda: ["mon", "tue", "wed", "thu", "fri"])
    upload_limit_kbps: int = 500
    download_limit_kbps: int = 0  # 0 = unlimited


@dataclass
class SyncConfig:
    """Sync behaviour settings."""

    sync_dir: str = "~/OneDrive"
    sync_interval_seconds: int = 300
    monitor_real_time: bool = True
    upload_only: bool = False
    download_only: bool = False
    skip_dotfiles: bool = True
    skip_symlinks: bool = True
    max_upload_speed_kbps: int = 0  # 0 = unlimited
    max_download_speed_kbps: int = 0
    file_size_limit_mb: int = 250000  # 250 GB OneDrive limit
    concurrent_transfers: int = 4

    # Files On-Demand
    files_on_demand: bool = True  # Fetch metadata first, download on access
    auto_free_space_gb: int = 0   # Auto-free files when disk < this (0 = disabled)

    # Selective sync — folders to include (empty = sync all)
    selected_folders: list[str] = field(default_factory=list)

    # Bandwidth scheduling
    bandwidth_schedule_enabled: bool = False
    bandwidth_schedule: list[dict[str, Any]] = field(default_factory=list)

    @property
    def sync_dir_path(self) -> Path:
        return Path(self.sync_dir).expanduser().resolve()

    def get_active_bandwidth_limits(self) -> tuple[int, int]:
        """Returns (upload_kbps, download_kbps) based on current time and schedule."""
        if not self.bandwidth_schedule_enabled or not self.bandwidth_schedule:
            return self.max_upload_speed_kbps, self.max_download_speed_kbps

        from datetime import datetime
        now = datetime.now()
        day_name = now.strftime("%a").lower()
        hour = now.hour

        for rule_dict in self.bandwidth_schedule:
            rule_days = rule_dict.get("days", [])
            start = rule_dict.get("start_hour", 0)
            end = rule_dict.get("end_hour", 24)
            if day_name in rule_days and start <= hour < end:
                return (
                    rule_dict.get("upload_limit_kbps", 0),
                    rule_dict.get("download_limit_kbps", 0),
                )

        return self.max_upload_speed_kbps, self.max_download_speed_kbps


@dataclass
class AuthConfig:
    """Authentication settings."""

    client_id: str = DEFAULT_CLIENT_ID
    authority: str = AUTH_AUTHORITY
    redirect_uri: str = REDIRECT_URI
    scopes: list[str] = field(default_factory=lambda: sanitize_scopes([
        "Files.ReadWrite.All",
        "Sites.Read.All",
        "User.Read",
        "offline_access",
    ]))


@dataclass
class AccountConfig:
    """Configuration for a single OneDrive account."""

    name: str = ""           # Display name (e.g. "Personal", "Work")
    account_type: str = "personal"  # personal, business
    client_id: str = DEFAULT_CLIENT_ID
    authority: str = AUTH_AUTHORITY
    sync_dir: str = "~/OneDrive"
    selected_folders: list[str] = field(default_factory=list)
    enabled: bool = True

    # SharePoint library sync
    sharepoint_sites: list[dict[str, str]] = field(default_factory=list)
    # e.g. [{"site_url": "...", "library": "Documents", "local_dir": "SharePoint/Site"}]

    @property
    def sync_dir_path(self) -> Path:
        return Path(self.sync_dir).expanduser().resolve()


@dataclass
class NotificationConfig:
    """Desktop notification settings."""

    enabled: bool = True
    show_sync_complete: bool = True
    show_errors: bool = True
    show_file_changes: bool = False  # Can be noisy


@dataclass
class AppConfig:
    """Top-level application configuration."""

    sync: SyncConfig = field(default_factory=SyncConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    log_level: str = "INFO"
    minimize_to_tray: bool = True
    start_minimized: bool = False
    autostart: bool = True

    # Multi-account support
    accounts: list[dict[str, Any]] = field(default_factory=list)

    _config_dir: Path = field(default_factory=lambda: Path(user_config_dir(APP_NAME)))
    _data_dir: Path = field(default_factory=lambda: Path(user_data_dir(APP_NAME)))
    _cache_dir: Path = field(default_factory=lambda: Path(user_cache_dir(APP_NAME)))

    def get_account_configs(self) -> list[AccountConfig]:
        """Parse account configs from the accounts list."""
        configs = []
        for acct_dict in self.accounts:
            acct = AccountConfig()
            for key, value in acct_dict.items():
                if hasattr(acct, key):
                    setattr(acct, key, value)
            configs.append(acct)
        return configs

    def get_account_data_dir(self, account_name: str) -> Path:
        """Get the data directory for a specific account."""
        import re
        # Strip everything except alphanumeric, dash, underscore
        safe_name = re.sub(r"[^a-zA-Z0-9_\-]", "_", account_name).strip("_") or "default"
        d = self.data_dir / "accounts" / safe_name
        # Ensure the resolved path is inside data_dir (prevent traversal)
        if not str(d.resolve()).startswith(str(self.data_dir.resolve())):
            raise ValueError(f"Invalid account name: {account_name}")
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def config_dir(self) -> Path:
        self._config_dir.mkdir(parents=True, exist_ok=True)
        _secure_directory(self._config_dir)
        return self._config_dir

    @property
    def data_dir(self) -> Path:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        _secure_directory(self._data_dir)
        return self._data_dir

    @property
    def cache_dir(self) -> Path:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        return self._cache_dir

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "sync_state.db"

    @property
    def log_file(self) -> Path:
        return self.data_dir / "clouddrive.log"

    @property
    def token_cache_file(self) -> Path:
        return self.data_dir / "token_cache.json"


def _secure_directory(path: Path) -> None:
    """Set directory permissions to owner-only (0o700) on POSIX systems."""
    try:
        path.chmod(0o700)
    except OSError:
        pass  # Windows or permission denied


def _secure_directory(path: Path) -> None:
    """Set directory permissions to owner-only (0o700) on POSIX systems."""
    try:
        path.chmod(0o700)
    except OSError:
        pass  # Windows or permission denied


def _deep_update(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Recursively update a dict with another dict."""
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def _apply_dict_to_dataclass(dc: Any, data: dict[str, Any]) -> None:
    """Apply dictionary values to a dataclass instance."""
    for key, value in data.items():
        if hasattr(dc, key):
            current = getattr(dc, key)
            if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
                _apply_dict_to_dataclass(current, value)
            else:
                setattr(dc, key, value)


def load_config(config_path: Path | None = None) -> AppConfig:
    """Load configuration from TOML file, falling back to defaults.

    Priority: environment variables > config file > defaults
    """
    config = AppConfig()

    path = config_path or config.config_file
    if path.exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)
        _apply_dict_to_dataclass(config, data)

    # Environment variable overrides
    env_map = {
        "CLOUDDRIVE_CLIENT_ID": ("auth", "client_id"),
        "CLOUDDRIVE_SYNC_DIR": ("sync", "sync_dir"),
        "CLOUDDRIVE_LOG_LEVEL": ("log_level", None),
    }
    for env_key, (section, attr) in env_map.items():
        value = os.environ.get(env_key)
        if value is not None:
            if attr is None:
                setattr(config, section, value)
            else:
                setattr(getattr(config, section), attr, value)

    # Always sanitize scopes after loading config
    config.auth.scopes = sanitize_scopes(config.auth.scopes)

    return config


def save_config(config: AppConfig) -> None:
    """Save configuration to TOML file."""
    import tomli_w  # type: ignore[import-untyped]

    data: dict[str, Any] = {
        "log_level": config.log_level,
        "minimize_to_tray": config.minimize_to_tray,
        "start_minimized": config.start_minimized,
        "autostart": config.autostart,
        "sync": {
            "sync_dir": config.sync.sync_dir,
            "sync_interval_seconds": config.sync.sync_interval_seconds,
            "monitor_real_time": config.sync.monitor_real_time,
            "upload_only": config.sync.upload_only,
            "download_only": config.sync.download_only,
            "skip_dotfiles": config.sync.skip_dotfiles,
            "skip_symlinks": config.sync.skip_symlinks,
            "max_upload_speed_kbps": config.sync.max_upload_speed_kbps,
            "max_download_speed_kbps": config.sync.max_download_speed_kbps,
            "concurrent_transfers": config.sync.concurrent_transfers,
            "files_on_demand": config.sync.files_on_demand,
            "auto_free_space_gb": config.sync.auto_free_space_gb,
            "selected_folders": config.sync.selected_folders,
            "bandwidth_schedule_enabled": config.sync.bandwidth_schedule_enabled,
            "bandwidth_schedule": config.sync.bandwidth_schedule,
        },
        "auth": {
            "client_id": config.auth.client_id,
        },
        "notifications": {
            "enabled": config.notifications.enabled,
            "show_sync_complete": config.notifications.show_sync_complete,
            "show_errors": config.notifications.show_errors,
            "show_file_changes": config.notifications.show_file_changes,
        },
    }

    # Multi-account configs
    if config.accounts:
        data["accounts"] = config.accounts

    config.config_dir.mkdir(parents=True, exist_ok=True)
    with open(config.config_file, "wb") as f:
        tomli_w.dump(data, f)

    # Restrict config file to owner-only (contains client_id)
    try:
        config.config_file.chmod(0o600)
    except OSError:
        pass
