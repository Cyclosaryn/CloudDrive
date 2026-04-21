"""Sync engine — the heart of CloudDrive.

Coordinates bi-directional synchronization between a local directory
and Microsoft OneDrive, using delta queries for efficiency.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable

from clouddrive.core.api import DriveItem, OneDriveClient
from clouddrive.core.config import AppConfig
from clouddrive.core.database import SyncDatabase, SyncItem

logger = logging.getLogger(__name__)

# Re-export for daemon convenience
try:
    from clouddrive.core.ondemand import OnDemandManager, HydrationPriority  # noqa: F401
except ImportError:
    OnDemandManager = None  # type: ignore[misc,assignment]
    HydrationPriority = None  # type: ignore[misc,assignment]


class SyncStatus(Enum):
    IDLE = auto()
    SYNCING = auto()
    PAUSED = auto()
    ERROR = auto()
    OFFLINE = auto()


class SyncDirection(Enum):
    BIDIRECTIONAL = auto()
    UPLOAD_ONLY = auto()
    DOWNLOAD_ONLY = auto()


class SyncEvent:
    """Emitted during sync to notify listeners of progress."""

    def __init__(
        self,
        event_type: str,
        item_name: str = "",
        item_path: str = "",
        progress: float = 0.0,
        message: str = "",
        error: str = "",
    ) -> None:
        self.event_type = event_type  # started, progress, completed, error, file_synced, file_deleted
        self.item_name = item_name
        self.item_path = item_path
        self.progress = progress
        self.message = message
        self.error = error
        self.timestamp = datetime.now(timezone.utc)


class SyncEngine:
    """Bi-directional sync engine for OneDrive."""

    def __init__(
        self,
        config: AppConfig,
        api: OneDriveClient,
        db: SyncDatabase,
        on_demand: "OnDemandManager | None" = None,
    ) -> None:
        self._config = config
        self._api = api
        self._db = db
        self._on_demand = on_demand
        self._status = SyncStatus.IDLE
        self._listeners: list[Callable[[SyncEvent], None]] = []
        self._cancel_event = asyncio.Event()

        # Stats for current sync cycle
        self._files_uploaded = 0
        self._files_downloaded = 0
        self._files_deleted = 0
        self._bytes_transferred = 0
        self._errors: list[str] = []

    @property
    def status(self) -> SyncStatus:
        return self._status

    @property
    def sync_dir(self) -> Path:
        return self._config.sync.sync_dir_path

    def add_listener(self, callback: Callable[[SyncEvent], None]) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[SyncEvent], None]) -> None:
        self._listeners.remove(callback)

    def _emit(self, event: SyncEvent) -> None:
        for listener in self._listeners:
            try:
                listener(event)
            except Exception:
                logger.exception("Error in sync event listener")

    def pause(self) -> None:
        self._status = SyncStatus.PAUSED
        self._cancel_event.set()
        self._emit(SyncEvent("paused", message="Sync paused"))

    def resume(self) -> None:
        self._cancel_event.clear()
        self._emit(SyncEvent("resumed", message="Sync resumed"))

    async def sync(self) -> None:
        """Run a complete sync cycle."""
        if self._status == SyncStatus.SYNCING:
            logger.warning("Sync already in progress")
            return

        if self._status == SyncStatus.PAUSED:
            logger.info("Sync is paused")
            return

        self._status = SyncStatus.SYNCING
        self._files_uploaded = 0
        self._files_downloaded = 0
        self._files_deleted = 0
        self._bytes_transferred = 0
        self._errors = []

        self._emit(SyncEvent("started", message="Sync started"))

        try:
            direction = self._get_sync_direction()

            if direction in (SyncDirection.BIDIRECTIONAL, SyncDirection.DOWNLOAD_ONLY):
                await self._sync_remote_changes()

            if direction in (SyncDirection.BIDIRECTIONAL, SyncDirection.UPLOAD_ONLY):
                await self._sync_local_changes()

            self._status = SyncStatus.IDLE
            self._emit(SyncEvent(
                "completed",
                message=(
                    f"Sync complete: {self._files_uploaded} uploaded, "
                    f"{self._files_downloaded} downloaded, "
                    f"{self._files_deleted} deleted"
                ),
            ))

        except Exception as e:
            self._status = SyncStatus.ERROR
            logger.exception("Sync failed")
            self._emit(SyncEvent("error", error=str(e), message="Sync failed"))

    def _get_sync_direction(self) -> SyncDirection:
        if self._config.sync.upload_only:
            return SyncDirection.UPLOAD_ONLY
        if self._config.sync.download_only:
            return SyncDirection.DOWNLOAD_ONLY
        return SyncDirection.BIDIRECTIONAL

    async def _sync_remote_changes(self) -> None:
        """Use delta queries to pull remote changes."""
        delta_link = self._db.get_state("delta_link")

        self._emit(SyncEvent("progress", message="Checking for remote changes..."))

        page = await self._api.get_delta(delta_link)

        # Process changed/new items
        for item in page.items:
            if self._cancel_event.is_set():
                return

            # Yield to priority on-demand downloads so the user
            # doesn't wait while background sync hogs bandwidth.
            await self._yield_to_priority()

            try:
                await self._process_remote_item(item)
            except Exception as e:
                logger.error("Error processing remote item %s: %s", item.path, e)
                self._errors.append(f"Remote: {item.path}: {e}")

        # Process deletions
        for deleted_id in page.deleted_ids:
            if self._cancel_event.is_set():
                return

            try:
                self._process_remote_deletion(deleted_id)
            except Exception as e:
                logger.error("Error processing remote deletion %s: %s", deleted_id, e)
                self._errors.append(f"Delete: {deleted_id}: {e}")

        # Save the delta link for next sync
        if page.delta_link:
            self._db.set_state("delta_link", page.delta_link)

        self._db.set_state("last_remote_sync", datetime.now(timezone.utc).isoformat())

    async def _process_remote_item(self, remote: DriveItem) -> None:
        """Process a single remote item (download if needed)."""
        local_path = self.sync_dir / remote.path.lstrip("/")

        # PATH TRAVERSAL PROTECTION: ensure the resolved path stays
        # inside the sync directory.  A malicious API response could
        # contain ".." components or absolute paths.
        if not self._is_safe_path(local_path):
            logger.warning(
                "Blocked path traversal attempt: %s -> %s",
                remote.path, local_path,
            )
            return

        if remote.is_folder:
            local_path.mkdir(parents=True, exist_ok=True)
            self._db.upsert_item(SyncItem(
                id=remote.id,
                name=remote.name,
                local_path=str(local_path),
                remote_path=remote.path,
                parent_id=remote.parent_id,
                is_folder=True,
                remote_modified=remote.modified_time,
                sync_status="synced",
                last_synced=datetime.now(timezone.utc),
            ))
            return

        # Check if file needs downloading
        existing = self._db.get_item(remote.id)

        needs_download = False
        if existing is None:
            needs_download = True
        elif not local_path.exists():
            needs_download = True
        elif existing.c_tag != remote.c_tag:
            # Content has changed remotely
            if existing.local_modified and local_path.exists():
                local_mtime = datetime.fromtimestamp(
                    local_path.stat().st_mtime, tz=timezone.utc
                )
                if local_mtime > existing.last_synced:
                    # Both local and remote changed — conflict!
                    logger.warning("Conflict detected: %s", remote.path)
                    self._handle_conflict(local_path, existing)
                    return
            needs_download = True

        if needs_download:
            self._emit(SyncEvent(
                "file_synced",
                item_name=remote.name,
                item_path=remote.path,
                message=f"Downloading {remote.name}",
            ))

            await self._api.download_file(
                remote.id,
                local_path,
                progress_callback=lambda received, total: self._emit(SyncEvent(
                    "progress",
                    item_name=remote.name,
                    progress=received / total if total else 0,
                )),
            )

            # Set local mtime to match remote
            mtime = remote.modified_time.timestamp()
            os.utime(local_path, (mtime, mtime))

            self._db.upsert_item(SyncItem(
                id=remote.id,
                name=remote.name,
                local_path=str(local_path),
                remote_path=remote.path,
                parent_id=remote.parent_id,
                is_folder=False,
                size=remote.size,
                local_modified=remote.modified_time,
                remote_modified=remote.modified_time,
                sha256_hash=remote.sha256_hash,
                quick_xor_hash=remote.quick_xor_hash,
                etag=remote.etag,
                c_tag=remote.c_tag,
                sync_status="synced",
                last_synced=datetime.now(timezone.utc),
            ))

            self._db.log_activity(
                "downloaded", remote.name, remote.path, remote.size
            )
            self._files_downloaded += 1
            self._bytes_transferred += remote.size

    async def _yield_to_priority(self) -> None:
        """Pause background work while a user-triggered file is downloading.

        When the user clicks a cloud-only file, OnDemandManager signals
        ``priority_in_progress``.  This method blocks until that download
        finishes so all bandwidth goes to the user's file.
        """
        if self._on_demand is None:
            return
        if self._on_demand.priority_in_progress.is_set():
            logger.debug("Yielding to priority download...")
            # Wait until the user's file is done (with a safety timeout)
            try:
                await asyncio.wait_for(
                    self._on_demand.priority_idle.wait(), timeout=300
                )
            except asyncio.TimeoutError:
                logger.warning("Timed out waiting for priority download")

    def _process_remote_deletion(self, item_id: str) -> None:
        """Handle a remotely deleted item."""
        existing = self._db.get_item(item_id)
        if existing is None:
            return

        local_path = Path(existing.local_path)
        if local_path.exists():
            # Move to trash instead of hard delete (FreeDesktop Trash spec)
            self._move_to_trash(local_path)
            self._db.log_activity(
                "deleted", existing.name, existing.remote_path, details="Deleted remotely"
            )

        self._db.delete_item(item_id)
        self._files_deleted += 1

    async def _sync_local_changes(self) -> None:
        """Scan local directory for changes and upload."""
        self._emit(SyncEvent("progress", message="Checking for local changes..."))

        known_items = {item.local_path: item for item in self._db.get_all_items()}

        for root, dirs, files in os.walk(self.sync_dir, followlinks=False):
            if self._cancel_event.is_set():
                return

            root_path = Path(root)

            # Security: ensure we haven't escaped sync_dir via symlinks
            if not self._is_safe_path(root_path):
                dirs[:] = []
                continue

            # Skip dotfiles/dirs if configured
            if self._config.sync.skip_dotfiles:
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                files = [f for f in files if not f.startswith(".")]

            # Skip symlinks if configured
            if self._config.sync.skip_symlinks:
                dirs[:] = [d for d in dirs if not (root_path / d).is_symlink()]
                files = [f for f in files if not (root_path / f).is_symlink()]

            # Skip temp files from our own downloads
            files = [f for f in files if not f.endswith(".clouddrive-tmp")]

            for filename in files:
                local_path = root_path / filename
                local_path_str = str(local_path)

                existing = known_items.pop(local_path_str, None)

                # Yield to priority on-demand downloads
                await self._yield_to_priority()

                try:
                    await self._process_local_file(local_path, existing)
                except Exception as e:
                    logger.error("Error processing local file %s: %s", local_path, e)
                    self._errors.append(f"Local: {local_path}: {e}")

        # Items remaining in known_items were deleted locally
        for path_str, item in known_items.items():
            if self._cancel_event.is_set():
                return
            if not item.is_folder and not Path(path_str).exists():
                try:
                    await self._process_local_deletion(item)
                except Exception as e:
                    logger.error("Error processing local deletion %s: %s", path_str, e)

    async def _process_local_file(
        self, local_path: Path, existing: SyncItem | None
    ) -> None:
        """Check if a local file needs uploading."""
        stat = local_path.stat()
        local_mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        local_size = stat.st_size

        if existing is not None:
            # File already tracked — check if it changed
            if (
                existing.size == local_size
                and existing.local_modified
                and abs((local_mtime - existing.local_modified).total_seconds()) < 2
            ):
                return  # No change

            if existing.last_synced and local_mtime <= existing.last_synced:
                return  # Not modified since last sync

        # Need to upload
        relative = local_path.relative_to(self.sync_dir)
        remote_path = "/" + str(relative).replace(os.sep, "/")

        self._emit(SyncEvent(
            "file_synced",
            item_name=local_path.name,
            item_path=remote_path,
            message=f"Uploading {local_path.name}",
        ))

        # Determine parent folder ID
        parent_id = await self._ensure_remote_parent(local_path.parent)

        result = await self._api.upload_file(
            parent_id,
            local_path.name,
            local_path,
            progress_callback=lambda sent, total: self._emit(SyncEvent(
                "progress",
                item_name=local_path.name,
                progress=sent / total if total else 0,
            )),
        )

        self._db.upsert_item(SyncItem(
            id=result.id,
            name=result.name,
            local_path=str(local_path),
            remote_path=remote_path,
            parent_id=result.parent_id,
            is_folder=False,
            size=local_size,
            local_modified=local_mtime,
            remote_modified=result.modified_time,
            sha256_hash=result.sha256_hash,
            quick_xor_hash=result.quick_xor_hash,
            etag=result.etag,
            c_tag=result.c_tag,
            sync_status="synced",
            last_synced=datetime.now(timezone.utc),
        ))

        self._db.log_activity("uploaded", local_path.name, remote_path, local_size)
        self._files_uploaded += 1
        self._bytes_transferred += local_size

    async def _ensure_remote_parent(self, local_dir: Path) -> str:
        """Ensure all parent folders exist remotely, return the parent folder ID."""
        if local_dir == self.sync_dir:
            return "root"

        relative = local_dir.relative_to(self.sync_dir)
        parts = relative.parts

        current_id = "root"
        current_path = "/"

        for part in parts:
            current_path = current_path.rstrip("/") + "/" + part
            existing = self._db.get_item_by_remote_path(current_path)

            if existing:
                current_id = existing.id
            else:
                try:
                    folder = await self._api.create_folder(current_id, part)
                    current_id = folder.id
                    self._db.upsert_item(SyncItem(
                        id=folder.id,
                        name=part,
                        local_path=str(self.sync_dir / current_path.lstrip("/")),
                        remote_path=current_path,
                        parent_id=folder.parent_id,
                        is_folder=True,
                        sync_status="synced",
                        last_synced=datetime.now(timezone.utc),
                    ))
                except Exception:
                    # Folder might already exist — try to get it
                    try:
                        item = await self._api.get_item_by_path(current_path)
                        current_id = item.id
                    except Exception:
                        raise

        return current_id

    async def _process_local_deletion(self, item: SyncItem) -> None:
        """Delete a file from OneDrive that was deleted locally."""
        logger.info("Local deletion detected: %s", item.remote_path)
        self._emit(SyncEvent(
            "file_deleted",
            item_name=item.name,
            item_path=item.remote_path,
            message=f"Deleting {item.name} from OneDrive",
        ))

        await self._api.delete_item(item.id)
        self._db.delete_item(item.id)
        self._db.log_activity(
            "deleted", item.name, item.remote_path, details="Deleted locally"
        )
        self._files_deleted += 1

    def _handle_conflict(self, local_path: Path, existing: SyncItem) -> None:
        """Handle a sync conflict by keeping both versions."""
        conflict_name = (
            f"{local_path.stem} (conflict {datetime.now().strftime('%Y%m%d-%H%M%S')})"
            f"{local_path.suffix}"
        )
        conflict_path = local_path.parent / conflict_name
        local_path.rename(conflict_path)

        existing.sync_status = "conflict"
        existing.error_message = f"Conflict — local copy saved as {conflict_name}"
        self._db.upsert_item(existing)

        self._db.log_activity(
            "conflict", existing.name, existing.remote_path,
            details=f"Local copy saved as {conflict_name}",
        )
        logger.warning("Conflict: %s → %s", local_path, conflict_path)

    def _is_safe_path(self, path: Path) -> bool:
        """Verify a path resolves to within the sync directory.

        Prevents path traversal attacks from malicious remote paths
        that contain '..' components or symlinks escaping the sync root.
        """
        try:
            resolved = path.resolve()
            sync_resolved = self.sync_dir.resolve()
            # Ensure the resolved path is under (or is) the sync dir
            return resolved == sync_resolved or str(resolved).startswith(
                str(sync_resolved) + os.sep
            )
        except (OSError, ValueError):
            return False

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        """Remove or replace dangerous characters from filenames.

        Prevents null bytes, path separators, and control characters
        from being used in file/folder names.
        """
        # Strip null bytes and control characters
        name = "".join(c for c in name if c.isprintable() and c != "\\x00")
        # Replace path separators that shouldn't be in a single name
        name = name.replace("/", "_").replace("\\\\", "_")
        # Strip leading/trailing dots and spaces (problematic on some FS)
        name = name.strip(". ")
        return name or "unnamed"

    @staticmethod
    def _move_to_trash(path: Path) -> None:
        """Move a file/folder to the FreeDesktop Trash.

        Falls back to simple deletion if trash is not available.
        """
        try:
            # Try using gio trash (available on most desktop Linux)
            import subprocess
            result = subprocess.run(
                ["gio", "trash", str(path)],
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0:
                return
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # Fallback: move to ~/.local/share/Trash
        trash_dir = Path.home() / ".local" / "share" / "Trash"
        trash_files = trash_dir / "files"
        trash_files.mkdir(parents=True, exist_ok=True)

        dest = trash_files / path.name
        counter = 1
        while dest.exists():
            dest = trash_files / f"{path.stem}.{counter}{path.suffix}"
            counter += 1

        path.rename(dest)
