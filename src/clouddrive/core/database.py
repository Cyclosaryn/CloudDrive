"""SQLite database for local sync state tracking.

Stores metadata about every synced file/folder so the sync engine
can detect local and remote changes efficiently.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    Column,
    String,
    Integer,
    Boolean,
    DateTime,
    Float,
    Index,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class SyncItem(Base):
    """Represents a tracked file or folder in the sync database."""

    __tablename__ = "sync_items"

    id = Column(String, primary_key=True)  # OneDrive item ID
    name = Column(String, nullable=False)
    local_path = Column(String, nullable=False, unique=True)
    remote_path = Column(String, nullable=False)
    parent_id = Column(String, index=True)
    is_folder = Column(Boolean, default=False)
    size = Column(Integer, default=0)
    local_modified = Column(DateTime(timezone=True))
    remote_modified = Column(DateTime(timezone=True))
    sha256_hash = Column(String, default="")
    quick_xor_hash = Column(String, default="")
    etag = Column(String, default="")
    c_tag = Column(String, default="")
    sync_status = Column(String, default="synced")  # synced, pending_upload, pending_download, conflict, error, placeholder, hydrating
    last_synced = Column(DateTime(timezone=True))
    error_message = Column(String, default="")

    # Files On-Demand fields
    placeholder_state = Column(String, default="")  # cloud_only, hydrating, available, pinned, ""
    is_pinned = Column(Boolean, default=False)       # Always keep on device
    download_url = Column(String, default="")         # Cached download URL for hydration

    # Account/drive tracking (for multi-account and SharePoint)
    account_name = Column(String, default="")
    drive_id = Column(String, default="")

    __table_args__ = (
        Index("ix_remote_path", "remote_path"),
        Index("ix_sync_status", "sync_status"),
        Index("ix_placeholder_state", "placeholder_state"),
        Index("ix_account_name", "account_name"),
    )


class SyncState(Base):
    """Global sync state (delta link, last sync time, etc.)."""

    __tablename__ = "sync_state"

    key = Column(String, primary_key=True)
    value = Column(String, nullable=False)
    updated = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ActivityLog(Base):
    """Tracks recent sync activity for the Activity Center UI."""

    __tablename__ = "activity_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    action = Column(String, nullable=False)  # uploaded, downloaded, deleted, renamed, conflict
    item_name = Column(String, nullable=False)
    item_path = Column(String, nullable=False)
    size = Column(Integer, default=0)
    details = Column(String, default="")

    __table_args__ = (
        Index("ix_activity_timestamp", "timestamp"),
    )


class SyncDatabase:
    """Manages the local sync state database."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(
            f"sqlite:///{db_path}",
            echo=False,
            pool_pre_ping=True,
        )

        # Enable WAL mode for better concurrent access
        @event.listens_for(self._engine, "connect")
        def set_sqlite_pragma(dbapi_conn: any, connection_record: any) -> None:
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(self._engine)
        self._Session = sessionmaker(bind=self._engine)

        # Restrict database file to owner-only
        try:
            db_path.chmod(0o600)
        except OSError:
            pass

    def session(self) -> Session:
        return self._Session()

    # === SyncItem operations ===

    def get_item(self, item_id: str) -> SyncItem | None:
        with self.session() as s:
            return s.get(SyncItem, item_id)

    def get_item_by_local_path(self, local_path: str) -> SyncItem | None:
        with self.session() as s:
            return s.query(SyncItem).filter_by(local_path=local_path).first()

    def get_item_by_remote_path(self, remote_path: str) -> SyncItem | None:
        with self.session() as s:
            return s.query(SyncItem).filter_by(remote_path=remote_path).first()

    def upsert_item(self, item: SyncItem) -> None:
        with self.session() as s:
            s.merge(item)
            s.commit()

    def delete_item(self, item_id: str) -> None:
        with self.session() as s:
            item = s.get(SyncItem, item_id)
            if item:
                s.delete(item)
                s.commit()

    def get_pending_uploads(self) -> list[SyncItem]:
        with self.session() as s:
            return list(s.query(SyncItem).filter_by(sync_status="pending_upload").all())

    def get_pending_downloads(self) -> list[SyncItem]:
        with self.session() as s:
            return list(s.query(SyncItem).filter_by(sync_status="pending_download").all())

    def get_conflicts(self) -> list[SyncItem]:
        with self.session() as s:
            return list(s.query(SyncItem).filter_by(sync_status="conflict").all())

    def get_all_items(self) -> list[SyncItem]:
        with self.session() as s:
            return list(s.query(SyncItem).all())

    def get_children(self, parent_id: str) -> list[SyncItem]:
        with self.session() as s:
            return list(s.query(SyncItem).filter_by(parent_id=parent_id).all())

    def get_items_by_account(self, account_name: str) -> list[SyncItem]:
        with self.session() as s:
            return list(s.query(SyncItem).filter_by(account_name=account_name).all())

    # === Placeholder / On-Demand operations ===

    def get_placeholders(self) -> list[SyncItem]:
        """Get all cloud-only placeholder items."""
        with self.session() as s:
            return list(s.query(SyncItem).filter_by(placeholder_state="cloud_only").all())

    def get_hydrating(self) -> list[SyncItem]:
        """Get items currently being hydrated (downloaded on demand)."""
        with self.session() as s:
            return list(s.query(SyncItem).filter_by(placeholder_state="hydrating").all())

    def get_pinned(self) -> list[SyncItem]:
        """Get items pinned to always keep on device."""
        with self.session() as s:
            return list(s.query(SyncItem).filter_by(is_pinned=True).all())

    def set_placeholder_state(self, item_id: str, state: str) -> None:
        """Update the placeholder state of an item."""
        with self.session() as s:
            item = s.get(SyncItem, item_id)
            if item:
                item.placeholder_state = state
                if state == "available" or state == "pinned":
                    item.sync_status = "synced"
                elif state == "cloud_only":
                    item.sync_status = "placeholder"
                elif state == "hydrating":
                    item.sync_status = "hydrating"
                s.commit()

    def bulk_upsert_items(self, items: list[SyncItem]) -> None:
        """Efficiently insert/update many items at once (for metadata-first sync)."""
        with self.session() as s:
            for item in items:
                s.merge(item)
            s.commit()

    def get_folder_tree(self, root_path: str = "/") -> list[SyncItem]:
        """Get all folders for the selective sync tree view."""
        with self.session() as s:
            return list(
                s.query(SyncItem)
                .filter_by(is_folder=True)
                .filter(SyncItem.remote_path.startswith(root_path))
                .order_by(SyncItem.remote_path)
                .all()
            )

    def get_total_placeholder_size(self) -> int:
        """Get total size of all cloud-only placeholders (space savings)."""
        with self.session() as s:
            from sqlalchemy import func
            result = s.query(func.sum(SyncItem.size)).filter_by(
                placeholder_state="cloud_only"
            ).scalar()
            return result or 0

    # === SyncState (key-value) ===

    def get_state(self, key: str) -> str | None:
        with self.session() as s:
            row = s.get(SyncState, key)
            return row.value if row else None

    def set_state(self, key: str, value: str) -> None:
        with self.session() as s:
            s.merge(SyncState(key=key, value=value, updated=datetime.now(timezone.utc)))
            s.commit()

    # === ActivityLog ===

    def log_activity(
        self,
        action: str,
        item_name: str,
        item_path: str,
        size: int = 0,
        details: str = "",
    ) -> None:
        with self.session() as s:
            s.add(ActivityLog(
                action=action,
                item_name=item_name,
                item_path=item_path,
                size=size,
                details=details,
            ))
            s.commit()

    def get_recent_activity(self, limit: int = 50) -> list[ActivityLog]:
        with self.session() as s:
            return list(
                s.query(ActivityLog)
                .order_by(ActivityLog.timestamp.desc())
                .limit(limit)
                .all()
            )

    def clear_old_activity(self, days: int = 30) -> None:
        cutoff = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        from datetime import timedelta
        cutoff -= timedelta(days=days)
        with self.session() as s:
            s.query(ActivityLog).filter(ActivityLog.timestamp < cutoff).delete()
            s.commit()
