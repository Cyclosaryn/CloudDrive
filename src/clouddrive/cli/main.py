"""CloudDrive CLI — command-line interface.

Provides commands for:
  clouddrive status    — Show sync status
  clouddrive sync      — Trigger immediate sync
  clouddrive auth      — Authenticate with Microsoft
  clouddrive pause     — Pause syncing
  clouddrive resume    — Resume syncing
  clouddrive config    — Show/edit configuration
  clouddrive activity  — Show recent activity
  clouddrive version   — Show version
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from clouddrive import __version__, __app_name__
from clouddrive.core.config import load_config, save_config


def cmd_auth(args: argparse.Namespace) -> int:
    """Authenticate with Microsoft OneDrive."""
    config = load_config()

    from clouddrive.core.auth import AuthManager
    auth = AuthManager(config)

    if args.device_code:
        result = auth.authenticate_device_code()
    else:
        print("Opening browser for sign-in...")
        result = auth.authenticate_interactive()

    if result and "access_token" in result:
        account = auth.get_account_info()
        name = account.get("name", "User") if account else "User"
        print(f"✓ Signed in as {name}")
        return 0
    else:
        print("✗ Authentication failed")
        return 1


def cmd_status(args: argparse.Namespace) -> int:
    """Show current sync status."""
    try:
        from pydbus import SessionBus
        bus = SessionBus()
        daemon = bus.get("org.clouddrive.Daemon", "/org/clouddrive/Daemon")

        status = daemon.GetStatus()
        last_sync = daemon.GetLastSync()
        quota_json = daemon.GetQuota()

        print(f"Status:    {status}")
        print(f"Last sync: {last_sync or 'Never'}")

        if quota_json:
            quota = json.loads(quota_json)
            if quota:
                used_gb = quota["used"] / (1024 ** 3)
                total_gb = quota["total"] / (1024 ** 3)
                print(f"Storage:   {used_gb:.1f} GB / {total_gb:.1f} GB ({quota['state']})")

        return 0
    except Exception as e:
        print(f"Could not connect to daemon: {e}")
        print("Is the CloudDrive daemon running?")
        print("  Start it with: systemctl --user start clouddrive")
        return 1


def cmd_sync(args: argparse.Namespace) -> int:
    """Trigger an immediate sync."""
    try:
        from pydbus import SessionBus
        bus = SessionBus()
        daemon = bus.get("org.clouddrive.Daemon", "/org/clouddrive/Daemon")

        if daemon.SyncNow():
            print("Sync triggered")
            return 0
        else:
            print("Failed to trigger sync")
            return 1
    except Exception as e:
        print(f"Could not connect to daemon: {e}")
        return 1


def cmd_pause(args: argparse.Namespace) -> int:
    """Pause syncing."""
    try:
        from pydbus import SessionBus
        bus = SessionBus()
        daemon = bus.get("org.clouddrive.Daemon", "/org/clouddrive/Daemon")
        daemon.Pause()
        print("Syncing paused")
        return 0
    except Exception as e:
        print(f"Could not connect to daemon: {e}")
        return 1


def cmd_resume(args: argparse.Namespace) -> int:
    """Resume syncing."""
    try:
        from pydbus import SessionBus
        bus = SessionBus()
        daemon = bus.get("org.clouddrive.Daemon", "/org/clouddrive/Daemon")
        daemon.Resume()
        print("Syncing resumed")
        return 0
    except Exception as e:
        print(f"Could not connect to daemon: {e}")
        return 1


def cmd_activity(args: argparse.Namespace) -> int:
    """Show recent sync activity."""
    try:
        from pydbus import SessionBus
        bus = SessionBus()
        daemon = bus.get("org.clouddrive.Daemon", "/org/clouddrive/Daemon")

        limit = args.limit if hasattr(args, "limit") else 20
        activity_json = daemon.GetRecentActivity(limit)
        activities = json.loads(activity_json)

        if not activities:
            print("No recent activity")
            return 0

        for a in activities:
            icon = {"uploaded": "↑", "downloaded": "↓", "deleted": "✕",
                    "conflict": "⚠"}.get(a["action"], "•")
            size = ""
            if a["size"] > 0:
                size = f" ({_format_size(a['size'])})"
            print(f"  {icon} {a['action']:12} {a['name']}{size}")
            if a["details"]:
                print(f"    {a['details']}")

        return 0
    except Exception as e:
        print(f"Could not connect to daemon: {e}")
        return 1


def cmd_config(args: argparse.Namespace) -> int:
    """Show or modify configuration."""
    config = load_config()

    if args.set:
        key, _, value = args.set.partition("=")
        if not value:
            print(f"Usage: clouddrive config --set key=value")
            return 1

        parts = key.split(".")

        # Security: reject private/internal attributes
        if any(part.startswith("_") for part in parts):
            print(f"Invalid config key: {key}")
            return 1

        # Security: only allow known top-level sections
        _ALLOWED_SECTIONS = {"sync", "auth", "notifications", "log_level",
                             "minimize_to_tray", "start_minimized", "autostart"}
        if parts[0] not in _ALLOWED_SECTIONS:
            print(f"Unknown config section: {parts[0]}")
            return 1

        obj = config
        for part in parts[:-1]:
            if hasattr(obj, part):
                obj = getattr(obj, part)
            else:
                print(f"Unknown config key: {key}")
                return 1

        attr = parts[-1]
        if not hasattr(obj, attr):
            print(f"Unknown config key: {key}")
            return 1

        current = getattr(obj, attr)
        # Type coercion
        if isinstance(current, bool):
            value = value.lower() in ("true", "1", "yes")
        elif isinstance(current, int):
            value = int(value)

        setattr(obj, attr, value)
        save_config(config)
        print(f"Set {key} = {value}")
        return 0

    # Show current config
    print(f"Config file: {config.config_file}")
    print(f"Sync dir:    {config.sync.sync_dir}")
    print(f"Interval:    {config.sync.sync_interval_seconds}s")
    print(f"Sync mode:   {'upload_only' if config.sync.upload_only else 'download_only' if config.sync.download_only else 'bidirectional'}")
    print(f"Real-time:   {config.sync.monitor_real_time}")
    print(f"Concurrent:  {config.sync.concurrent_transfers}")
    print(f"Client ID:   {config.auth.client_id[:8]}..." if config.auth.client_id else "Client ID:   (not set)")
    print(f"Autostart:   {config.autostart}")
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    """Show version."""
    print(f"{__app_name__} {__version__}")
    return 0


def _format_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="clouddrive",
        description="CloudDrive — A modern OneDrive client for Linux",
    )
    subparsers = parser.add_subparsers(dest="command")

    # auth
    auth_parser = subparsers.add_parser("auth", help="Sign in to OneDrive")
    auth_parser.add_argument(
        "--device-code", action="store_true",
        help="Use device code flow (for headless environments)",
    )
    auth_parser.set_defaults(func=cmd_auth)

    # status
    status_parser = subparsers.add_parser("status", help="Show sync status")
    status_parser.set_defaults(func=cmd_status)

    # sync
    sync_parser = subparsers.add_parser("sync", help="Trigger immediate sync")
    sync_parser.set_defaults(func=cmd_sync)

    # pause
    pause_parser = subparsers.add_parser("pause", help="Pause syncing")
    pause_parser.set_defaults(func=cmd_pause)

    # resume
    resume_parser = subparsers.add_parser("resume", help="Resume syncing")
    resume_parser.set_defaults(func=cmd_resume)

    # activity
    activity_parser = subparsers.add_parser("activity", help="Show recent activity")
    activity_parser.add_argument(
        "-n", "--limit", type=int, default=20, help="Number of items to show"
    )
    activity_parser.set_defaults(func=cmd_activity)

    # config
    config_parser = subparsers.add_parser("config", help="Show/edit configuration")
    config_parser.add_argument(
        "--set", metavar="KEY=VALUE", help="Set a config value (e.g. sync.sync_dir=~/MyDrive)"
    )
    config_parser.set_defaults(func=cmd_config)

    # version
    version_parser = subparsers.add_parser("version", help="Show version")
    version_parser.set_defaults(func=cmd_version)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 0

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
