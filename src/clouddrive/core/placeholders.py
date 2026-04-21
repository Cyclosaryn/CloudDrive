"""Placeholder file management for Files On-Demand.

Creates lightweight stub files that appear in the file manager with
correct names, sizes, and timestamps — but contain no data until
the user actually opens them. This mirrors Windows OneDrive's
"Files On-Demand" cloud-only / locally-available state system.

Placeholder states:
  - cloud_only:  Metadata stub only, downloads on access
  - hydrating:   Currently being downloaded
  - available:   Fully downloaded, cached locally
  - pinned:      Always keep local (user marked "Always keep on this device")

Uses Linux extended attributes (xattr) to store state without
modifying file content.
"""

from __future__ import annotations

import json
import logging
import os
import struct
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Extended attribute names (user namespace)
XATTR_STATE = "user.clouddrive.state"
XATTR_REMOTE_ID = "user.clouddrive.remote_id"
XATTR_REMOTE_SIZE = "user.clouddrive.remote_size"
XATTR_REMOTE_HASH = "user.clouddrive.remote_hash"
XATTR_DOWNLOAD_URL = "user.clouddrive.download_url"
XATTR_ETAG = "user.clouddrive.etag"


class PlaceholderState(Enum):
    """Sync state of a placeholder file."""
    CLOUD_ONLY = "cloud_only"
    HYDRATING = "hydrating"
    AVAILABLE = "available"
    PINNED = "pinned"

    @property
    def icon_name(self) -> str:
        """Icon overlay name for file managers."""
        return {
            PlaceholderState.CLOUD_ONLY: "clouddrive-cloud",
            PlaceholderState.HYDRATING: "clouddrive-syncing",
            PlaceholderState.AVAILABLE: "clouddrive-available",
            PlaceholderState.PINNED: "clouddrive-pinned",
        }[self]

    @property
    def display_text(self) -> str:
        return {
            PlaceholderState.CLOUD_ONLY: "Available online",
            PlaceholderState.HYDRATING: "Downloading...",
            PlaceholderState.AVAILABLE: "Available on this device",
            PlaceholderState.PINNED: "Always available",
        }[self]


def _set_xattr(path: Path, name: str, value: str) -> bool:
    """Set an extended attribute on a file."""
    try:
        os.setxattr(str(path), name, value.encode("utf-8"))
        return True
    except OSError as e:
        logger.debug("Could not set xattr %s on %s: %s", name, path, e)
        return False


def _get_xattr(path: Path, name: str) -> str | None:
    """Get an extended attribute from a file."""
    try:
        return os.getxattr(str(path), name).decode("utf-8")
    except OSError:
        return None


def _remove_xattr(path: Path, name: str) -> None:
    """Remove an extended attribute from a file."""
    try:
        os.removexattr(str(path), name)
    except OSError:
        pass


def create_placeholder(
    local_path: Path,
    remote_id: str,
    remote_size: int,
    modified_time: datetime,
    created_time: datetime,
    etag: str = "",
    remote_hash: str = "",
    download_url: str = "",
) -> bool:
    """Create a placeholder (cloud-only) file at the given path.

    Creates a sparse file with the correct apparent size but no actual
    disk usage, then tags it with extended attributes for on-demand hydration.
    """
    try:
        local_path.parent.mkdir(parents=True, exist_ok=True)

        # Create a sparse file with the correct apparent size
        # This means `ls -l` shows the real size, but `du` shows 0
        with open(local_path, "wb") as f:
            if remote_size > 0:
                f.seek(remote_size - 1)
                f.write(b"\0")
            # For zero-byte files, just create empty

        # Set timestamps to match remote
        mtime = modified_time.timestamp()
        ctime = created_time.timestamp()
        os.utime(local_path, (ctime, mtime))

        # Tag with extended attributes
        _set_xattr(local_path, XATTR_STATE, PlaceholderState.CLOUD_ONLY.value)
        _set_xattr(local_path, XATTR_REMOTE_ID, remote_id)
        _set_xattr(local_path, XATTR_REMOTE_SIZE, str(remote_size))
        if etag:
            _set_xattr(local_path, XATTR_ETAG, etag)
        if remote_hash:
            _set_xattr(local_path, XATTR_REMOTE_HASH, remote_hash)
        if download_url:
            _set_xattr(local_path, XATTR_DOWNLOAD_URL, download_url)

        logger.debug("Created placeholder: %s (%d bytes)", local_path, remote_size)
        return True

    except OSError as e:
        logger.error("Failed to create placeholder %s: %s", local_path, e)
        return False


def get_placeholder_state(path: Path) -> PlaceholderState | None:
    """Get the placeholder state of a file, or None if not a placeholder."""
    state_str = _get_xattr(path, XATTR_STATE)
    if state_str is None:
        return None
    try:
        return PlaceholderState(state_str)
    except ValueError:
        return None


def is_placeholder(path: Path) -> bool:
    """Check if a file is a cloud-only placeholder."""
    state = get_placeholder_state(path)
    return state == PlaceholderState.CLOUD_ONLY


def is_hydrated(path: Path) -> bool:
    """Check if a file has been fully downloaded."""
    state = get_placeholder_state(path)
    return state in (PlaceholderState.AVAILABLE, PlaceholderState.PINNED)


def get_remote_id(path: Path) -> str | None:
    """Get the OneDrive item ID from a placeholder file."""
    return _get_xattr(path, XATTR_REMOTE_ID)


def get_remote_size(path: Path) -> int:
    """Get the real remote file size from a placeholder."""
    size_str = _get_xattr(path, XATTR_REMOTE_SIZE)
    return int(size_str) if size_str else 0


def mark_hydrating(path: Path) -> None:
    """Mark a placeholder as currently being downloaded."""
    _set_xattr(path, XATTR_STATE, PlaceholderState.HYDRATING.value)


def mark_available(path: Path) -> None:
    """Mark a file as fully downloaded and available locally."""
    _set_xattr(path, XATTR_STATE, PlaceholderState.AVAILABLE.value)


def mark_pinned(path: Path) -> None:
    """Mark a file to always keep on this device."""
    _set_xattr(path, XATTR_STATE, PlaceholderState.PINNED.value)


def mark_cloud_only(path: Path) -> None:
    """Free up space by converting back to a cloud-only placeholder."""
    remote_size = get_remote_size(path)
    if remote_size > 0:
        # Truncate and recreate as sparse
        with open(path, "wb") as f:
            f.seek(remote_size - 1)
            f.write(b"\0")
    _set_xattr(path, XATTR_STATE, PlaceholderState.CLOUD_ONLY.value)


def clear_placeholder_attrs(path: Path) -> None:
    """Remove all CloudDrive extended attributes from a file."""
    for attr in (XATTR_STATE, XATTR_REMOTE_ID, XATTR_REMOTE_SIZE,
                 XATTR_REMOTE_HASH, XATTR_DOWNLOAD_URL, XATTR_ETAG):
        _remove_xattr(path, attr)


def get_placeholder_info(path: Path) -> dict[str, Any] | None:
    """Get all placeholder metadata for a file."""
    state = get_placeholder_state(path)
    if state is None:
        return None
    return {
        "state": state,
        "remote_id": _get_xattr(path, XATTR_REMOTE_ID) or "",
        "remote_size": get_remote_size(path),
        "remote_hash": _get_xattr(path, XATTR_REMOTE_HASH) or "",
        "etag": _get_xattr(path, XATTR_ETAG) or "",
    }
