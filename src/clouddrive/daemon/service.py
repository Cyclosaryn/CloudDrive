"""CloudDrive background sync daemon.

Runs as a systemd user service, handling:
- Periodic sync cycles
- Real-time file watching
- D-Bus interface for GUI communication
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clouddrive.core.auth import AuthManager
from clouddrive.core.api import OneDriveClient
from clouddrive.core.config import load_config, AppConfig
from clouddrive.core.database import SyncDatabase
from clouddrive.core.sync_engine import SyncEngine, SyncEvent, SyncStatus
from clouddrive.core.watcher import FileWatcher, LocalChangeEvent
from clouddrive.core.ondemand import OnDemandManager, HydrationPriority

logger = logging.getLogger(__name__)

# D-Bus service name
DBUS_SERVICE_NAME = "org.clouddrive.Daemon"
DBUS_OBJECT_PATH = "/org/clouddrive/Daemon"

# D-Bus interface XML
DBUS_INTERFACE = """
<node>
  <interface name="org.clouddrive.Daemon">
    <method name="SyncNow">
      <arg direction="out" type="b" name="success"/>
    </method>
    <method name="Pause">
      <arg direction="out" type="b" name="success"/>
    </method>
    <method name="Resume">
      <arg direction="out" type="b" name="success"/>
    </method>
    <method name="GetStatus">
      <arg direction="out" type="s" name="status"/>
    </method>
    <method name="GetLastSync">
      <arg direction="out" type="s" name="timestamp"/>
    </method>
    <method name="GetRecentActivity">
      <arg direction="in" type="i" name="limit"/>
      <arg direction="out" type="s" name="activity_json"/>
    </method>
    <method name="GetQuota">
      <arg direction="out" type="s" name="quota_json"/>
    </method>
    <signal name="SyncStatusChanged">
      <arg type="s" name="status"/>
    </signal>
    <signal name="FileSynced">
      <arg type="s" name="action"/>
      <arg type="s" name="filename"/>
      <arg type="s" name="path"/>
    </signal>
    <signal name="SyncError">
      <arg type="s" name="message"/>
    </signal>
  </interface>
</node>
"""


class DaemonDBusService:
    """D-Bus interface for the sync daemon.

    Allows the GUI and CLI to control the daemon.
    """

    dbus = DBUS_INTERFACE

    def __init__(self, daemon: SyncDaemon) -> None:
        self._daemon = daemon

    def SyncNow(self) -> bool:
        asyncio.ensure_future(self._daemon.trigger_sync())
        return True

    def Pause(self) -> bool:
        self._daemon.pause()
        return True

    def Resume(self) -> bool:
        self._daemon.resume()
        return True

    def GetStatus(self) -> str:
        return self._daemon.status.name

    def GetLastSync(self) -> str:
        ts = self._daemon.last_sync_time
        return ts.isoformat() if ts else ""

    def GetRecentActivity(self, limit: int) -> str:
        import json
        # Cap the limit to prevent abuse via D-Bus
        limit = max(1, min(limit, 500))
        activities = self._daemon.db.get_recent_activity(limit)
        return json.dumps([
            {
                "action": a.action,
                "name": a.item_name,
                "path": a.item_path,
                "size": a.size,
                "timestamp": a.timestamp.isoformat() if a.timestamp else "",
                "details": a.details,
            }
            for a in activities
        ])

    def GetQuota(self) -> str:
        import json
        quota = self._daemon.cached_quota
        if quota:
            return json.dumps({
                "total": quota.total,
                "used": quota.used,
                "remaining": quota.remaining,
                "state": quota.state,
            })
        return "{}"


class SyncDaemon:
    """Main daemon process that orchestrates sync."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._auth = AuthManager(config)
        self._api = OneDriveClient(self._auth)
        self._db = SyncDatabase(config.db_path)

        # On-demand hydration manager — downloads files when the user opens them.
        # The hydrate callback uses the API client directly for minimum latency.
        self._on_demand = OnDemandManager(
            sync_dir=config.sync.sync_dir_path,
            hydrate_callback=self._hydrate_callback,
        )

        # Pass the on-demand manager into the engine so background sync
        # yields bandwidth whenever a user-triggered download is active.
        self._engine = SyncEngine(config, self._api, self._db, self._on_demand)
        self._watcher: FileWatcher | None = None
        self._running = False
        self._last_sync: datetime | None = None
        self._cached_quota = None

        # Connect sync engine events
        self._engine.add_listener(self._on_sync_event)

    async def _hydrate_callback(
        self, remote_id: str, local_path: "Path", progress_cb: Any = None
    ) -> None:
        """Download a file on behalf of the on-demand manager."""
        await self._api.download_file(
            remote_id, local_path, progress_callback=progress_cb,
        )

    @property
    def status(self) -> SyncStatus:
        return self._engine.status

    @property
    def db(self) -> SyncDatabase:
        return self._db

    @property
    def last_sync_time(self) -> datetime | None:
        return self._last_sync

    @property
    def cached_quota(self):
        return self._cached_quota

    def pause(self) -> None:
        self._engine.pause()
        if self._watcher and self._watcher.is_running:
            self._watcher.stop()

    def resume(self) -> None:
        self._engine.resume()
        if self._config.sync.monitor_real_time:
            self._start_watcher()

    async def trigger_sync(self) -> None:
        await self._engine.sync()
        self._last_sync = datetime.now(timezone.utc)

    async def run(self) -> None:
        """Main daemon loop."""
        self._running = True
        logger.info("CloudDrive daemon starting")

        # Verify authentication
        if not self._auth.is_authenticated:
            logger.error(
                "Not authenticated. Run 'clouddrive auth' or the setup wizard first."
            )
            return

        # Create sync directory
        self._config.sync.sync_dir_path.mkdir(parents=True, exist_ok=True)

        # Start on-demand hydration monitor (inotify-based)
        if self._config.sync.files_on_demand:
            self._on_demand.start()
            logger.info("On-demand file hydration enabled")

        # Start file watcher for real-time monitoring
        if self._config.sync.monitor_real_time:
            self._start_watcher()

        # Initial sync
        await self.trigger_sync()

        # Refresh quota
        try:
            self._cached_quota = await self._api.get_quota()
        except Exception:
            logger.warning("Could not fetch quota info")

        # Periodic sync loop
        while self._running:
            try:
                await asyncio.sleep(self._config.sync.sync_interval_seconds)
                if self._running and self._engine.status != SyncStatus.PAUSED:
                    await self.trigger_sync()
                    # Refresh quota periodically
                    try:
                        self._cached_quota = await self._api.get_quota()
                    except Exception:
                        pass
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in sync loop")
                await asyncio.sleep(30)  # Back off on error

        await self.shutdown()

    async def shutdown(self) -> None:
        """Clean shutdown."""
        logger.info("CloudDrive daemon shutting down")
        self._running = False

        self._on_demand.stop()

        if self._watcher and self._watcher.is_running:
            self._watcher.stop()

        await self._api.close()
        logger.info("Daemon stopped")

    def _start_watcher(self) -> None:
        if self._watcher and self._watcher.is_running:
            return

        self._watcher = FileWatcher(
            self._config.sync.sync_dir_path,
            self._on_local_change,
            skip_dotfiles=self._config.sync.skip_dotfiles,
        )
        self._watcher.start()

    def _on_local_change(self, event: LocalChangeEvent) -> None:
        """Handle a local file change detected by the watcher."""
        logger.debug("Local change: %s", event)
        # Schedule a sync (debounced — the watcher already debounces)
        if self._engine.status == SyncStatus.IDLE:
            asyncio.ensure_future(self.trigger_sync())

    def _on_sync_event(self, event: SyncEvent) -> None:
        """Handle sync engine events (for D-Bus signal emission)."""
        logger.info("Sync event: %s — %s", event.event_type, event.message)


def setup_logging(config: AppConfig) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)

    # Restrict log file permissions (may contain file paths / account info)
    log_path = config.log_file
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stderr),
        ],
    )
    try:
        log_path.chmod(0o600)
    except OSError:
        pass


def main() -> None:
    """Entry point for the daemon (systemd service)."""
    config = load_config()
    setup_logging(config)

    daemon = SyncDaemon(config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Handle signals for clean shutdown
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: loop.create_task(daemon.shutdown()))

    try:
        # Try to register D-Bus service
        try:
            from pydbus import SessionBus
            bus = SessionBus()
            bus.publish(DBUS_SERVICE_NAME, DaemonDBusService(daemon))
            logger.info("D-Bus service registered: %s", DBUS_SERVICE_NAME)
        except Exception:
            logger.warning("Could not register D-Bus service (running without D-Bus)")

        loop.run_until_complete(daemon.run())
    except KeyboardInterrupt:
        loop.run_until_complete(daemon.shutdown())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
