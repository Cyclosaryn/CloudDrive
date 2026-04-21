"""Activity Center — shows recent sync activity.

Similar to the Windows OneDrive "Activity Center" that shows
recently synced files and current transfer progress.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QFrame,
)
from PySide6.QtCore import Qt, QTimer, QSize
from PySide6.QtGui import QFont, QColor, QIcon

from clouddrive.core.config import AppConfig

logger = logging.getLogger(__name__)


# Action to icon/color mapping
ACTION_STYLES = {
    "uploaded": ("↑", "#2ecc71"),     # Green
    "downloaded": ("↓", "#3498db"),    # Blue
    "deleted": ("✕", "#e74c3c"),      # Red
    "renamed": ("→", "#f39c12"),       # Orange
    "conflict": ("⚠", "#e67e22"),     # Dark orange
}


class ActivityItem(QFrame):
    """A single activity entry in the list."""

    def __init__(
        self,
        action: str,
        name: str,
        path: str,
        size: int,
        timestamp: datetime,
        details: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet("ActivityItem { padding: 4px; }")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)

        # Action indicator
        icon_text, color = ACTION_STYLES.get(action, ("•", "#95a5a6"))
        icon_label = QLabel(icon_text)
        icon_label.setStyleSheet(f"color: {color}; font-size: 16px; font-weight: bold;")
        icon_label.setFixedWidth(24)
        layout.addWidget(icon_label)

        # File info
        info_layout = QVBoxLayout()
        info_layout.setSpacing(2)

        name_label = QLabel(name)
        name_label.setFont(QFont("", 10))
        info_layout.addWidget(name_label)

        detail_parts = []
        if size > 0:
            detail_parts.append(self._format_size(size))
        if details:
            detail_parts.append(details)

        path_label = QLabel(path)
        path_label.setStyleSheet("color: #888; font-size: 9px;")
        info_layout.addWidget(path_label)

        if detail_parts:
            detail_label = QLabel(" · ".join(detail_parts))
            detail_label.setStyleSheet("color: #aaa; font-size: 9px;")
            info_layout.addWidget(detail_label)

        layout.addLayout(info_layout, stretch=1)

        # Timestamp
        time_label = QLabel(self._format_time(timestamp))
        time_label.setStyleSheet("color: #999; font-size: 9px;")
        time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        layout.addWidget(time_label)

    @staticmethod
    def _format_size(size: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024:
                return f"{size:.1f} {unit}" if unit != "B" else f"{size} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    @staticmethod
    def _format_time(dt: datetime) -> str:
        now = datetime.now(timezone.utc)
        diff = now - dt
        if diff.total_seconds() < 60:
            return "Just now"
        if diff.total_seconds() < 3600:
            mins = int(diff.total_seconds() / 60)
            return f"{mins}m ago"
        if diff.total_seconds() < 86400:
            hours = int(diff.total_seconds() / 3600)
            return f"{hours}h ago"
        return dt.strftime("%b %d")


class TransferProgress(QFrame):
    """Shows current file transfer progress."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)

        self._label = QLabel("Syncing...")
        self._label.setFont(QFont("", 10))
        layout.addWidget(self._label)

        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setTextVisible(True)
        layout.addWidget(self._bar)

        self._detail = QLabel("")
        self._detail.setStyleSheet("color: #888; font-size: 9px;")
        layout.addWidget(self._detail)

        self.hide()

    def update_progress(self, filename: str, progress: float, detail: str = "") -> None:
        self._label.setText(f"Syncing: {filename}")
        self._bar.setValue(int(progress * 100))
        self._detail.setText(detail)
        self.show()

    def finish(self) -> None:
        self.hide()


class ActivityWindow(QWidget):
    """Activity center showing recent sync operations and current progress."""

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self.setWindowTitle("CloudDrive — Activity")
        self.setMinimumSize(420, 520)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        # Header
        header = QHBoxLayout()
        title = QLabel("Recent Activity")
        title.setFont(QFont("", 14, QFont.Weight.Bold))
        header.addWidget(title)
        header.addStretch()

        sync_btn = QPushButton("Sync now")
        sync_btn.clicked.connect(self._trigger_sync)
        header.addWidget(sync_btn)

        layout.addLayout(header)

        # Current transfer progress
        self._transfer_progress = TransferProgress()
        layout.addWidget(self._transfer_progress)

        # Status line
        self._status_label = QLabel("Up to date")
        self._status_label.setStyleSheet(
            "color: #2ecc71; font-weight: bold; padding: 4px 8px;"
        )
        layout.addWidget(self._status_label)

        # Activity list (scrollable)
        self._activity_list = QVBoxLayout()
        self._activity_list.setSpacing(0)

        # Container for activity items
        activity_container = QWidget()
        activity_container.setLayout(self._activity_list)

        from PySide6.QtWidgets import QScrollArea
        scroll = QScrollArea()
        scroll.setWidget(activity_container)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        layout.addWidget(scroll, stretch=1)

        # Placeholder for empty state
        self._empty_label = QLabel("No recent activity")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setStyleSheet("color: #888; padding: 40px;")
        self._activity_list.addWidget(self._empty_label)

    def add_activity(
        self,
        action: str,
        name: str,
        path: str,
        size: int = 0,
        timestamp: datetime | None = None,
        details: str = "",
    ) -> None:
        """Add an activity entry to the list."""
        if self._empty_label.isVisible():
            self._empty_label.hide()

        item = ActivityItem(
            action=action,
            name=name,
            path=path,
            size=size,
            timestamp=timestamp or datetime.now(timezone.utc),
            details=details,
        )

        # Insert at the top
        self._activity_list.insertWidget(0, item)

        # Keep max 100 items
        if self._activity_list.count() > 100:
            old = self._activity_list.takeAt(self._activity_list.count() - 1)
            if old and old.widget():
                old.widget().deleteLater()

    def update_status(self, status: str, color: str = "#2ecc71") -> None:
        self._status_label.setText(status)
        self._status_label.setStyleSheet(
            f"color: {color}; font-weight: bold; padding: 4px 8px;"
        )

    def _trigger_sync(self) -> None:
        # Will connect via D-Bus to daemon
        self.update_status("Syncing...", "#3498db")
