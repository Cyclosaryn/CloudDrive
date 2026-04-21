"""Local filesystem watcher for real-time sync.

Uses watchdog to monitor the sync directory for changes and
triggers immediate uploads/deletes as needed.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from watchdog.observers import Observer
from watchdog.events import (
    FileSystemEventHandler,
    FileCreatedEvent,
    FileModifiedEvent,
    FileDeletedEvent,
    FileMovedEvent,
    DirCreatedEvent,
    DirDeletedEvent,
    DirMovedEvent,
)

logger = logging.getLogger(__name__)


class LocalChangeEvent:
    """Represents a detected local file system change."""

    def __init__(self, event_type: str, path: str, dest_path: str = "") -> None:
        self.event_type = event_type  # created, modified, deleted, moved
        self.path = path
        self.dest_path = dest_path
        self.timestamp = time.time()

    def __repr__(self) -> str:
        if self.dest_path:
            return f"LocalChangeEvent({self.event_type}, {self.path} → {self.dest_path})"
        return f"LocalChangeEvent({self.event_type}, {self.path})"


class _WatchdogHandler(FileSystemEventHandler):
    """Translates watchdog events to LocalChangeEvents with debouncing."""

    def __init__(
        self,
        callback: Callable[[LocalChangeEvent], None],
        skip_dotfiles: bool = True,
        skip_patterns: list[str] | None = None,
    ) -> None:
        super().__init__()
        self._callback = callback
        self._skip_dotfiles = skip_dotfiles
        self._skip_patterns = skip_patterns or [".clouddrive-tmp"]
        self._debounce: dict[str, float] = {}
        self._debounce_interval = 1.0  # seconds

    def _should_skip(self, path: str) -> bool:
        p = Path(path)
        if self._skip_dotfiles and any(part.startswith(".") for part in p.parts):
            return True
        for pattern in self._skip_patterns:
            if pattern in p.name:
                return True
        return False

    def _debounced(self, path: str) -> bool:
        now = time.time()
        last = self._debounce.get(path, 0)
        if now - last < self._debounce_interval:
            return True
        self._debounce[path] = now
        return False

    def on_created(self, event: FileCreatedEvent | DirCreatedEvent) -> None:
        if self._should_skip(event.src_path) or self._debounced(event.src_path):
            return
        self._callback(LocalChangeEvent("created", event.src_path))

    def on_modified(self, event: FileModifiedEvent) -> None:
        if event.is_directory:
            return
        if self._should_skip(event.src_path) or self._debounced(event.src_path):
            return
        self._callback(LocalChangeEvent("modified", event.src_path))

    def on_deleted(self, event: FileDeletedEvent | DirDeletedEvent) -> None:
        if self._should_skip(event.src_path):
            return
        self._callback(LocalChangeEvent("deleted", event.src_path))

    def on_moved(self, event: FileMovedEvent | DirMovedEvent) -> None:
        if self._should_skip(event.src_path) and self._should_skip(event.dest_path):
            return
        self._callback(LocalChangeEvent("moved", event.src_path, event.dest_path))


class FileWatcher:
    """Monitors a directory for changes using inotify (Linux) via watchdog."""

    def __init__(
        self,
        watch_dir: Path,
        callback: Callable[[LocalChangeEvent], None],
        skip_dotfiles: bool = True,
    ) -> None:
        self._watch_dir = watch_dir
        self._handler = _WatchdogHandler(callback, skip_dotfiles=skip_dotfiles)
        self._observer = Observer()
        self._running = False

    def start(self) -> None:
        """Start watching for filesystem changes."""
        if self._running:
            return

        self._watch_dir.mkdir(parents=True, exist_ok=True)
        self._observer.schedule(
            self._handler,
            str(self._watch_dir),
            recursive=True,
        )
        self._observer.start()
        self._running = True
        logger.info("File watcher started for %s", self._watch_dir)

    def stop(self) -> None:
        """Stop watching."""
        if not self._running:
            return

        self._observer.stop()
        self._observer.join(timeout=5)
        self._running = False
        logger.info("File watcher stopped")

    @property
    def is_running(self) -> bool:
        return self._running
