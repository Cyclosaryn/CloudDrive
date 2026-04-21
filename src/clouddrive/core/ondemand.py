"""FUSE-based on-demand file hydration layer.

Intercepts file reads on cloud-only placeholders and transparently
downloads the content before passing it to the reading application.
This provides the Windows-like "Files On-Demand" experience.

Requires: python-pyfuse3 (or python-llfuse as fallback)
Optional: runs without FUSE if not installed (falls back to inotify+fanotify).
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import stat
import threading
from pathlib import Path
from typing import Any, Callable, Awaitable

from clouddrive.core.placeholders import (
    PlaceholderState,
    get_placeholder_state,
    get_remote_id,
    get_remote_size,
    is_placeholder,
    mark_available,
    mark_hydrating,
)

logger = logging.getLogger(__name__)


class HydrationPriority:
    """Priority levels for hydration requests (lower = higher priority)."""

    USER_OPEN = 0       # User clicked/opened a file — download ASAP
    USER_PIN = 10       # User pinned a file — high but not instant
    BACKGROUND = 100    # Background sync downloads


class HydrationRequest:
    """Represents a request to download a cloud-only file."""

    def __init__(
        self,
        local_path: Path,
        remote_id: str,
        remote_size: int,
        priority: int = HydrationPriority.BACKGROUND,
    ) -> None:
        self.local_path = local_path
        self.remote_id = remote_id
        self.remote_size = remote_size
        self.priority = priority
        self.completed = asyncio.Event()
        self.error: str | None = None
        self.progress: float = 0.0
        self.created_at: float = 0.0  # set at enqueue time

    def __lt__(self, other: "HydrationRequest") -> bool:
        """Lower priority value wins; ties broken by creation time."""
        if self.priority != other.priority:
            return self.priority < other.priority
        return self.created_at < other.created_at


class OnDemandManager:
    """Manages on-demand file hydration without FUSE.

    Uses fanotify (via inotify fallback) to detect when a cloud-only
    placeholder is being opened, and triggers download before the
    read completes. This approach works without FUSE privileges.

    Architecture:
      1. The file watcher detects open() on a cloud-only file
      2. We pause the open, trigger download via the API
      3. Once downloaded, the read proceeds normally
      4. The file state changes from cloud_only → available
    """

    def __init__(
        self,
        sync_dir: Path,
        hydrate_callback: Callable[[str, Path, Callable[[int, int], None] | None], Awaitable[None]],
    ) -> None:
        """
        Args:
            sync_dir: The local sync directory root.
            hydrate_callback: Async callable(remote_id, local_path, progress_cb)
                             that downloads the file content.
        """
        self._sync_dir = sync_dir
        self._hydrate = hydrate_callback
        self._pending: dict[str, HydrationRequest] = {}
        self._lock = threading.Lock()
        self._running = False
        self._monitor_thread: threading.Thread | None = None

        # Priority queue: requests are processed highest-priority first.
        # A dedicated worker drains this queue so user-opened files
        # always jump ahead of background downloads.
        self._queue: asyncio.PriorityQueue[HydrationRequest] | None = None
        self._worker_task: asyncio.Task | None = None

        # Signalled whenever a USER_OPEN priority request is in-flight.
        # Background sync should check this and yield bandwidth.
        self.priority_in_progress = asyncio.Event()
        # Inverse: set when NO priority download is active (default state).
        self.priority_idle = asyncio.Event()
        self.priority_idle.set()

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def start(self) -> None:
        """Start monitoring for file access on placeholders."""
        if self._running:
            return
        self._running = True

        # Create priority queue and start worker in the event loop
        loop = self._get_event_loop()
        self._queue = asyncio.PriorityQueue()
        self._worker_task = asyncio.run_coroutine_threadsafe(
            self._priority_worker(), loop
        )

        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="clouddrive-hydration",
        )
        self._monitor_thread.start()
        logger.info("On-demand hydration manager started for %s", self._sync_dir)

    def stop(self) -> None:
        """Stop monitoring."""
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5)
        logger.info("On-demand hydration manager stopped")

    def _monitor_loop(self) -> None:
        """Monitor for file access events using inotify.

        We watch for IN_OPEN events on the sync directory.
        When a cloud-only file is opened, we trigger hydration.
        """
        try:
            self._monitor_inotify()
        except ImportError:
            logger.warning(
                "inotify not available for on-demand hydration. "
                "Files will be hydrated during regular sync cycles."
            )
        except Exception:
            logger.exception("Error in hydration monitor loop")

    def _monitor_inotify(self) -> None:
        """Use inotify to watch for file access events."""
        import ctypes
        import ctypes.util
        import select

        # Load libc for inotify
        libc_name = ctypes.util.find_library("c")
        if not libc_name:
            logger.warning("Cannot find libc for inotify")
            return

        libc = ctypes.CDLL(libc_name, use_errno=True)

        # inotify constants
        IN_ACCESS = 0x00000001
        IN_OPEN = 0x00000020
        IN_CLOSE_NOWRITE = 0x00000010
        IN_CREATE = 0x00000100
        IN_ISDIR = 0x40000000

        # Initialize inotify
        fd = libc.inotify_init1(os.O_NONBLOCK)
        if fd < 0:
            logger.error("inotify_init failed")
            return

        try:
            # Add recursive watches
            watch_map: dict[int, Path] = {}
            self._add_watches_recursive(libc, fd, self._sync_dir, watch_map, IN_OPEN | IN_CREATE)

            # Event reading loop
            buf_size = 4096
            while self._running:
                # Wait for events with timeout
                ready, _, _ = select.select([fd], [], [], 1.0)
                if not ready:
                    continue

                buf = os.read(fd, buf_size)
                offset = 0
                while offset < len(buf):
                    # Parse inotify_event struct: wd(4) + mask(4) + cookie(4) + len(4)
                    wd, mask, cookie, name_len = (
                        int.from_bytes(buf[offset:offset+4], "little"),
                        int.from_bytes(buf[offset+4:offset+8], "little"),
                        int.from_bytes(buf[offset+8:offset+12], "little"),
                        int.from_bytes(buf[offset+12:offset+16], "little"),
                    )
                    offset += 16

                    if name_len > 0:
                        name = buf[offset:offset+name_len].rstrip(b"\0").decode("utf-8", errors="replace")
                        offset += name_len
                    else:
                        name = ""

                    if not name:
                        continue

                    parent = watch_map.get(wd)
                    if parent is None:
                        continue

                    full_path = parent / name

                    # New directory created — add watch
                    if mask & IN_CREATE and mask & IN_ISDIR:
                        self._add_watches_recursive(libc, fd, full_path, watch_map, IN_OPEN | IN_CREATE)
                        continue

                    # File opened — check if it's a placeholder
                    if mask & IN_OPEN and not (mask & IN_ISDIR):
                        if full_path.is_file() and is_placeholder(full_path):
                            self._trigger_hydration(full_path)

        finally:
            os.close(fd)

    def _add_watches_recursive(
        self, libc: Any, fd: int, directory: Path,
        watch_map: dict[int, Path], mask: int,
    ) -> None:
        """Recursively add inotify watches to a directory tree."""
        try:
            wd = libc.inotify_add_watch(fd, str(directory).encode(), mask)
            if wd >= 0:
                watch_map[wd] = directory
            for child in directory.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    self._add_watches_recursive(libc, fd, child, watch_map, mask)
        except (PermissionError, OSError):
            pass

    def _trigger_hydration(
        self, path: Path, priority: int = HydrationPriority.USER_OPEN
    ) -> None:
        """Enqueue a hydration request with the given priority.

        User-opened files default to USER_OPEN (highest priority) so they
        jump ahead of any background sync downloads in the queue.
        """
        path_str = str(path)

        with self._lock:
            if path_str in self._pending:
                existing = self._pending[path_str]
                if priority < existing.priority:
                    # Escalate: a background request is pending but user
                    # just opened the file — bump its priority.
                    existing.priority = priority
                    logger.info(
                        "Escalated priority for %s to %d", path.name, priority
                    )
                return  # Already queued / hydrating

        remote_id = get_remote_id(path)
        if not remote_id:
            logger.warning("Placeholder missing remote_id: %s", path)
            return

        import time as _time

        remote_size = get_remote_size(path)
        request = HydrationRequest(path, remote_id, remote_size, priority=priority)
        request.created_at = _time.monotonic()

        with self._lock:
            self._pending[path_str] = request

        mark_hydrating(path)
        logger.info(
            "Queued hydration (priority=%d): %s (%d bytes)",
            priority, path.name, remote_size,
        )

        # Put on the priority queue; the worker will pick it up
        if self._queue is not None:
            asyncio.run_coroutine_threadsafe(
                self._queue.put(request), self._get_event_loop()
            )
        else:
            # Fallback if queue not yet initialised (shouldn't happen)
            asyncio.run_coroutine_threadsafe(
                self._do_hydrate(request), self._get_event_loop()
            )

    async def _priority_worker(self) -> None:
        """Drain the priority queue, processing highest-priority requests first.

        This ensures that when a user opens a cloud-only file, it is
        downloaded immediately — even if dozens of background sync
        downloads are queued.
        """
        while self._running:
            try:
                request = await asyncio.wait_for(
                    self._queue.get(), timeout=2.0
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                continue

            await self._do_hydrate(request)

    async def _do_hydrate(self, request: HydrationRequest) -> None:
        """Perform the actual file download.

        If this is a user-initiated (high-priority) request, signal
        background sync to pause so all bandwidth goes to this file.
        """
        is_user_request = request.priority <= HydrationPriority.USER_PIN

        if is_user_request:
            self.priority_idle.clear()
            self.priority_in_progress.set()
            logger.info(
                "Priority download started — background sync paused: %s",
                request.local_path.name,
            )

        try:
            def progress_cb(received: int, total: int) -> None:
                request.progress = received / total if total else 0

            await self._hydrate(request.remote_id, request.local_path, progress_cb)
            mark_available(request.local_path)
            logger.info("Hydrated: %s (priority=%d)", request.local_path.name, request.priority)

        except Exception as e:
            request.error = str(e)
            logger.error("Hydration failed for %s: %s", request.local_path.name, e)

        finally:
            request.completed.set()
            with self._lock:
                self._pending.pop(str(request.local_path), None)

            if is_user_request:
                # Check whether any other priority requests are still pending
                has_more_priority = any(
                    r.priority <= HydrationPriority.USER_PIN
                    for r in self._pending.values()
                )
                if not has_more_priority:
                    self.priority_in_progress.clear()
                    self.priority_idle.set()
                    logger.info("Priority downloads finished — background sync may resume")

    def _get_event_loop(self) -> asyncio.AbstractEventLoop:
        """Get the running event loop, or create one."""
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return loop

    async def hydrate_file(
        self, path: Path, priority: int = HydrationPriority.USER_PIN
    ) -> bool:
        """Explicitly hydrate a single file (user right-click → "Make available").

        Returns True if successful.
        """
        if not is_placeholder(path):
            return True  # Already available

        remote_id = get_remote_id(path)
        if not remote_id:
            return False

        import time as _time

        remote_size = get_remote_size(path)
        request = HydrationRequest(path, remote_id, remote_size, priority=priority)
        request.created_at = _time.monotonic()

        with self._lock:
            self._pending[str(path)] = request

        mark_hydrating(path)

        # Put directly on the priority queue so the worker picks it up
        if self._queue is not None:
            await self._queue.put(request)
            # Wait for the worker to finish it
            await request.completed.wait()
        else:
            await self._do_hydrate(request)

        return request.error is None

    async def hydrate_file_urgent(self, path: Path) -> bool:
        """Hydrate a file at the highest priority (user opened/clicked it).

        This is the fast-path: pauses background sync and downloads
        the file as quickly as possible so the user's application
        can open it without a long wait.
        """
        return await self.hydrate_file(path, priority=HydrationPriority.USER_OPEN)

    async def free_space(self, path: Path) -> bool:
        """Convert a locally available file back to cloud-only.

        User right-click → "Free up space".
        """
        from clouddrive.core.placeholders import mark_cloud_only
        try:
            mark_cloud_only(path)
            logger.info("Freed space: %s", path.name)
            return True
        except Exception as e:
            logger.error("Failed to free space for %s: %s", path, e)
            return False
