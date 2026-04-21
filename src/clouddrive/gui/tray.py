"""System tray icon and context menu — the primary user-facing UI.

Mirrors the Windows OneDrive system tray experience:
- Cloud icon with sync status overlay
- Click to open activity center
- Right-click for context menu
- Pause/resume, settings, sign out
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from PySide6.QtWidgets import (
    QSystemTrayIcon,
    QMenu,
    QWidget,
    QApplication,
)
from PySide6.QtGui import QIcon, QAction, QPixmap, QPainter, QColor
from PySide6.QtCore import QTimer, Signal, QObject

from clouddrive.core.config import AppConfig
from clouddrive.core.sync_engine import SyncStatus

logger = logging.getLogger(__name__)


class TraySignals(QObject):
    """Signals emitted by the tray for cross-thread communication."""

    sync_status_changed = Signal(str)
    open_settings = Signal()
    open_activity = Signal()
    open_folder = Signal()
    quit_app = Signal()


class SystemTrayManager:
    """Manages the system tray icon and its context menu."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._signals = TraySignals()
        self._sync_status = SyncStatus.IDLE
        self._is_paused = False
        self._dbus_proxy = None

        # Hidden parent widget required for KDE Plasma SNI tray menus
        self._parent_widget = QWidget()
        self._parent_widget.hide()

        # Create tray icon
        self._tray = QSystemTrayIcon(self._parent_widget)
        self._tray.setToolTip("CloudDrive — OneDrive for Linux")
        self._update_icon()

        # Build context menu
        self._menu = self._build_menu()
        self._tray.setContextMenu(self._menu)

        # Click behaviour (left click → activity center)
        self._tray.activated.connect(self._on_activated)

        # Lazy-load windows
        self._settings_window = None
        self._activity_window = None
        self._wizard_window = None

    def show(self) -> None:
        self._tray.show()

        # Update account info if already authenticated
        self._refresh_account_info()

        # Check if first run (no accounts configured)
        if not self._config.accounts:
            QTimer.singleShot(500, self._show_setup_wizard)

    def _refresh_account_info(self) -> None:
        """Update the account display from auth state."""
        try:
            from clouddrive.core.auth import AuthManager
            auth = AuthManager(self._config)
            account = auth.get_account_info()
            if account:
                name = account.get("name", "User")
                email = account.get("username", "")
                self.update_account_info(name, email)
        except Exception:
            pass

    def _build_menu(self) -> QMenu:
        menu = QMenu(self._parent_widget)

        # Account info header
        self._account_action = menu.addAction("Not signed in")
        self._account_action.setEnabled(False)

        # Storage info
        self._storage_action = menu.addAction("Storage: —")
        self._storage_action.setEnabled(False)

        menu.addSeparator()

        # Open OneDrive folder
        self._open_folder_action = menu.addAction("Open OneDrive folder")
        self._open_folder_action.triggered.connect(self._open_sync_folder)

        # View online
        self._view_online_action = menu.addAction("View online")
        self._view_online_action.triggered.connect(self._open_online)

        menu.addSeparator()

        # Activity center
        self._activity_action = menu.addAction("Recent activity")
        self._activity_action.triggered.connect(self._show_activity)

        menu.addSeparator()

        # Sync controls
        self._sync_now_action = menu.addAction("Sync now")
        self._sync_now_action.triggered.connect(self._trigger_sync)

        self._pause_action = menu.addAction("Pause syncing")
        self._pause_action.triggered.connect(self._toggle_pause)

        menu.addSeparator()

        # Settings
        self._settings_action = menu.addAction("Settings")
        self._settings_action.triggered.connect(self._show_settings)

        # Help
        self._help_action = menu.addAction("Help && About")
        self._help_action.triggered.connect(self._show_about)

        menu.addSeparator()

        # Quit
        self._quit_action = menu.addAction("Quit CloudDrive")
        self._quit_action.triggered.connect(self._quit)

        return menu

    def _update_icon(self) -> None:
        """Update the tray icon to reflect current sync status."""
        # Generate a simple status-colored icon
        # In production, this would use proper SVG icons
        size = 64
        pixmap = QPixmap(size, size)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Base cloud icon color
        status_colors = {
            SyncStatus.IDLE: QColor(0, 120, 212),      # Microsoft blue
            SyncStatus.SYNCING: QColor(0, 120, 212),
            SyncStatus.PAUSED: QColor(128, 128, 128),   # Gray
            SyncStatus.ERROR: QColor(220, 50, 50),       # Red
            SyncStatus.OFFLINE: QColor(128, 128, 128),
        }
        color = status_colors.get(self._sync_status, QColor(0, 120, 212))

        # Draw a simple cloud shape
        painter.setBrush(color)
        painter.setPen(color)
        painter.drawEllipse(8, 16, 28, 28)
        painter.drawEllipse(22, 10, 24, 24)
        painter.drawEllipse(36, 18, 20, 20)
        painter.drawRect(8, 30, 48, 14)

        # Sync animation indicator
        if self._sync_status == SyncStatus.SYNCING:
            painter.setBrush(QColor(255, 255, 255))
            painter.drawEllipse(28, 28, 8, 8)

        # Pause indicator
        if self._sync_status == SyncStatus.PAUSED:
            painter.setBrush(QColor(255, 255, 255))
            painter.drawRect(26, 26, 4, 12)
            painter.drawRect(34, 26, 4, 12)

        # Error indicator
        if self._sync_status == SyncStatus.ERROR:
            painter.setBrush(QColor(255, 255, 255))
            painter.drawText(28, 38, "!")

        painter.end()
        self._tray.setIcon(QIcon(pixmap))

    def update_sync_status(self, status: SyncStatus) -> None:
        self._sync_status = status
        self._update_icon()

        tooltips = {
            SyncStatus.IDLE: "CloudDrive — Up to date",
            SyncStatus.SYNCING: "CloudDrive — Syncing...",
            SyncStatus.PAUSED: "CloudDrive — Paused",
            SyncStatus.ERROR: "CloudDrive — Sync error",
            SyncStatus.OFFLINE: "CloudDrive — Offline",
        }
        self._tray.setToolTip(tooltips.get(status, "CloudDrive"))

    def update_account_info(self, name: str, email: str) -> None:
        self._account_action.setText(f"{name} ({email})")

    def update_storage_info(self, used_gb: float, total_gb: float) -> None:
        self._storage_action.setText(
            f"Storage: {used_gb:.1f} GB / {total_gb:.1f} GB"
        )

    def show_notification(self, title: str, message: str) -> None:
        if self._config.notifications.enabled:
            self._tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 5000)

    def set_dbus_proxy(self, proxy) -> None:
        """Set the D-Bus proxy for daemon communication."""
        self._dbus_proxy = proxy

    # === Action handlers ===

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._show_activity()

    def _open_sync_folder(self) -> None:
        folder = str(self._config.sync.sync_dir_path)
        try:
            subprocess.Popen(["xdg-open", folder])
        except FileNotFoundError:
            logger.error("xdg-open not found")

    def _open_online(self) -> None:
        try:
            subprocess.Popen(["xdg-open", "https://onedrive.live.com"])
        except FileNotFoundError:
            pass

    def _trigger_sync(self) -> None:
        if self._dbus_proxy:
            try:
                self._dbus_proxy.SyncNow()
                self.show_notification("CloudDrive", "Sync triggered")
            except Exception as e:
                logger.error("Failed to trigger sync: %s", e)
                self.show_notification("CloudDrive", "Failed to trigger sync")
        else:
            self.show_notification("CloudDrive", "Daemon not connected")

    def _toggle_pause(self) -> None:
        self._is_paused = not self._is_paused
        if self._is_paused:
            if self._dbus_proxy:
                try:
                    self._dbus_proxy.Pause()
                except Exception:
                    pass
            self._pause_action.setText("Resume syncing")
            self.update_sync_status(SyncStatus.PAUSED)
        else:
            if self._dbus_proxy:
                try:
                    self._dbus_proxy.Resume()
                except Exception:
                    pass
            self._pause_action.setText("Pause syncing")
            self.update_sync_status(SyncStatus.IDLE)

    def _show_settings(self) -> None:
        from clouddrive.gui.settings import SettingsWindow
        if self._settings_window is None:
            self._settings_window = SettingsWindow(self._config)
        self._settings_window.show()
        self._settings_window.raise_()

    def _show_activity(self) -> None:
        from clouddrive.gui.activity import ActivityWindow
        if self._activity_window is None:
            self._activity_window = ActivityWindow(self._config)
        self._activity_window.show()
        self._activity_window.raise_()

    def _show_setup_wizard(self) -> None:
        from clouddrive.gui.wizard import SetupWizard
        if self._wizard_window is None:
            self._wizard_window = SetupWizard(self._config)
            self._wizard_window.finished.connect(self._on_wizard_finished)
        self._wizard_window.show()

    def _on_wizard_finished(self, result: int) -> None:
        """Update tray after wizard completes."""
        self._refresh_account_info()

    def _show_about(self) -> None:
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.about(
            None,
            "About CloudDrive",
            "<h2>CloudDrive</h2>"
            "<p>A modern OneDrive client for Linux</p>"
            "<p>Version 0.1.9</p>"
            "<p>Licensed under GPL-3.0</p>"
            "<p><a href='https://github.com/Cyclosaryn/CloudDrive'>GitHub</a></p>",
        )

    def _quit(self) -> None:
        self._tray.hide()
        QApplication.quit()
