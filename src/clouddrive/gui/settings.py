"""Settings window — comprehensive configuration UI.

Provides a tabbed settings dialog similar to the Windows OneDrive
settings panel, with sections for Account, Sync, Network, and About.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QTabWidget,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QWidget,
    QLabel,
    QLineEdit,
    QSpinBox,
    QCheckBox,
    QComboBox,
    QPushButton,
    QFileDialog,
    QGroupBox,
    QProgressBar,
    QMessageBox,
    QDialogButtonBox,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from clouddrive.core.config import AppConfig, save_config

logger = logging.getLogger(__name__)


class SettingsWindow(QDialog):
    """Main settings dialog with tabs."""

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self.setWindowTitle("CloudDrive Settings")
        self.setMinimumSize(560, 480)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        tabs = QTabWidget()
        tabs.addTab(self._create_account_tab(), "Account")
        tabs.addTab(self._create_sync_tab(), "Sync")
        tabs.addTab(self._create_network_tab(), "Network")
        tabs.addTab(self._create_notifications_tab(), "Notifications")
        tabs.addTab(self._create_about_tab(), "About")
        layout.addWidget(tabs)

        # OK / Cancel / Apply buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Apply
        )
        buttons.accepted.connect(self._save_and_close)
        buttons.rejected.connect(self.reject)
        apply_btn = buttons.button(QDialogButtonBox.StandardButton.Apply)
        if apply_btn:
            apply_btn.clicked.connect(self._save)
        layout.addWidget(buttons)

    def _create_account_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # Account info group
        account_group = QGroupBox("Account")
        account_layout = QFormLayout(account_group)

        self._account_label = QLabel("Not signed in")
        self._account_label.setFont(QFont("", 11, QFont.Weight.Bold))
        account_layout.addRow("Account:", self._account_label)

        self._email_label = QLabel("—")
        account_layout.addRow("Email:", self._email_label)

        sign_out_btn = QPushButton("Sign out")
        sign_out_btn.setMaximumWidth(120)
        sign_out_btn.clicked.connect(self._sign_out)
        account_layout.addRow("", sign_out_btn)

        layout.addWidget(account_group)

        # Storage group
        storage_group = QGroupBox("Storage")
        storage_layout = QVBoxLayout(storage_group)

        self._storage_bar = QProgressBar()
        self._storage_bar.setRange(0, 100)
        self._storage_bar.setValue(0)
        storage_layout.addWidget(self._storage_bar)

        self._storage_label = QLabel("— used of — available")
        storage_layout.addWidget(self._storage_label)

        manage_btn = QPushButton("Manage storage online")
        manage_btn.setMaximumWidth(200)
        manage_btn.clicked.connect(lambda: self._open_url("https://onedrive.live.com/?v=ManageStorage"))
        storage_layout.addWidget(manage_btn)

        layout.addWidget(storage_group)
        layout.addStretch()

        return widget

    def _create_sync_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # Sync directory
        dir_group = QGroupBox("Sync Location")
        dir_layout = QHBoxLayout(dir_group)

        self._sync_dir_edit = QLineEdit(self._config.sync.sync_dir)
        dir_layout.addWidget(self._sync_dir_edit)

        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_sync_dir)
        dir_layout.addWidget(browse_btn)

        layout.addWidget(dir_group)

        # Sync behaviour
        behaviour_group = QGroupBox("Sync Behaviour")
        behaviour_layout = QFormLayout(behaviour_group)

        self._sync_mode_combo = QComboBox()
        self._sync_mode_combo.addItems(["Bi-directional", "Upload only", "Download only"])
        if self._config.sync.upload_only:
            self._sync_mode_combo.setCurrentIndex(1)
        elif self._config.sync.download_only:
            self._sync_mode_combo.setCurrentIndex(2)
        behaviour_layout.addRow("Sync mode:", self._sync_mode_combo)

        self._interval_spin = QSpinBox()
        self._interval_spin.setRange(30, 3600)
        self._interval_spin.setSuffix(" seconds")
        self._interval_spin.setValue(self._config.sync.sync_interval_seconds)
        behaviour_layout.addRow("Sync interval:", self._interval_spin)

        self._concurrent_spin = QSpinBox()
        self._concurrent_spin.setRange(1, 16)
        self._concurrent_spin.setValue(self._config.sync.concurrent_transfers)
        behaviour_layout.addRow("Concurrent transfers:", self._concurrent_spin)

        self._realtime_check = QCheckBox("Monitor for real-time changes")
        self._realtime_check.setChecked(self._config.sync.monitor_real_time)
        behaviour_layout.addRow("", self._realtime_check)

        self._skip_dotfiles_check = QCheckBox("Skip dotfiles and hidden files")
        self._skip_dotfiles_check.setChecked(self._config.sync.skip_dotfiles)
        behaviour_layout.addRow("", self._skip_dotfiles_check)

        self._skip_symlinks_check = QCheckBox("Skip symbolic links")
        self._skip_symlinks_check.setChecked(self._config.sync.skip_symlinks)
        behaviour_layout.addRow("", self._skip_symlinks_check)

        layout.addWidget(behaviour_group)
        layout.addStretch()

        return widget

    def _create_network_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        bandwidth_group = QGroupBox("Bandwidth Limits")
        bandwidth_layout = QFormLayout(bandwidth_group)

        self._upload_limit_spin = QSpinBox()
        self._upload_limit_spin.setRange(0, 1000000)
        self._upload_limit_spin.setSuffix(" KB/s")
        self._upload_limit_spin.setSpecialValueText("Unlimited")
        self._upload_limit_spin.setValue(self._config.sync.max_upload_speed_kbps)
        bandwidth_layout.addRow("Upload limit:", self._upload_limit_spin)

        self._download_limit_spin = QSpinBox()
        self._download_limit_spin.setRange(0, 1000000)
        self._download_limit_spin.setSuffix(" KB/s")
        self._download_limit_spin.setSpecialValueText("Unlimited")
        self._download_limit_spin.setValue(self._config.sync.max_download_speed_kbps)
        bandwidth_layout.addRow("Download limit:", self._download_limit_spin)

        layout.addWidget(bandwidth_group)

        # Auth settings
        auth_group = QGroupBox("Authentication")
        auth_layout = QFormLayout(auth_group)

        self._client_id_edit = QLineEdit(self._config.auth.client_id)
        self._client_id_edit.setPlaceholderText("Azure Application (client) ID")
        auth_layout.addRow("Client ID:", self._client_id_edit)

        help_label = QLabel(
            '<a href="https://portal.azure.com/#blade/Microsoft_AAD_RegisteredApps">'
            "Register an app in Azure Portal</a>"
        )
        help_label.setOpenExternalLinks(True)
        auth_layout.addRow("", help_label)

        layout.addWidget(auth_group)
        layout.addStretch()

        return widget

    def _create_notifications_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        notif_group = QGroupBox("Desktop Notifications")
        notif_layout = QVBoxLayout(notif_group)

        self._notif_enabled_check = QCheckBox("Enable desktop notifications")
        self._notif_enabled_check.setChecked(self._config.notifications.enabled)
        notif_layout.addWidget(self._notif_enabled_check)

        self._notif_sync_check = QCheckBox("Show notification when sync completes")
        self._notif_sync_check.setChecked(self._config.notifications.show_sync_complete)
        notif_layout.addWidget(self._notif_sync_check)

        self._notif_error_check = QCheckBox("Show notification on errors")
        self._notif_error_check.setChecked(self._config.notifications.show_errors)
        notif_layout.addWidget(self._notif_error_check)

        self._notif_files_check = QCheckBox("Show notification for individual file changes")
        self._notif_files_check.setChecked(self._config.notifications.show_file_changes)
        notif_layout.addWidget(self._notif_files_check)

        layout.addWidget(notif_group)

        # Startup
        startup_group = QGroupBox("Startup")
        startup_layout = QVBoxLayout(startup_group)

        self._autostart_check = QCheckBox("Start CloudDrive when you sign in")
        self._autostart_check.setChecked(self._config.autostart)
        startup_layout.addWidget(self._autostart_check)

        self._minimize_check = QCheckBox("Start minimized to system tray")
        self._minimize_check.setChecked(self._config.start_minimized)
        startup_layout.addWidget(self._minimize_check)

        self._minimize_tray_check = QCheckBox("Minimize to tray instead of closing")
        self._minimize_tray_check.setChecked(self._config.minimize_to_tray)
        startup_layout.addWidget(self._minimize_tray_check)

        layout.addWidget(startup_group)
        layout.addStretch()

        return widget

    def _create_about_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        title = QLabel("CloudDrive")
        title.setFont(QFont("", 18, QFont.Weight.Bold))
        layout.addWidget(title)

        layout.addWidget(QLabel("Version 0.1.0"))
        layout.addWidget(QLabel("A modern OneDrive client for Linux"))
        layout.addWidget(QLabel(""))
        layout.addWidget(QLabel("Licensed under GNU General Public License v3.0"))
        layout.addWidget(QLabel(""))

        link = QLabel(
            '<a href="https://github.com/clouddrive-linux/clouddrive">GitHub Repository</a>'
        )
        link.setOpenExternalLinks(True)
        layout.addWidget(link)

        layout.addStretch()
        return widget

    # === Action handlers ===

    def _browse_sync_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choose OneDrive Folder", str(Path.home())
        )
        if folder:
            self._sync_dir_edit.setText(folder)

    def _save(self) -> None:
        """Apply current settings to config and save."""
        self._config.sync.sync_dir = self._sync_dir_edit.text()
        self._config.sync.sync_interval_seconds = self._interval_spin.value()
        self._config.sync.concurrent_transfers = self._concurrent_spin.value()
        self._config.sync.monitor_real_time = self._realtime_check.isChecked()
        self._config.sync.skip_dotfiles = self._skip_dotfiles_check.isChecked()
        self._config.sync.skip_symlinks = self._skip_symlinks_check.isChecked()
        self._config.sync.max_upload_speed_kbps = self._upload_limit_spin.value()
        self._config.sync.max_download_speed_kbps = self._download_limit_spin.value()
        self._config.auth.client_id = self._client_id_edit.text()

        mode = self._sync_mode_combo.currentIndex()
        self._config.sync.upload_only = mode == 1
        self._config.sync.download_only = mode == 2

        self._config.notifications.enabled = self._notif_enabled_check.isChecked()
        self._config.notifications.show_sync_complete = self._notif_sync_check.isChecked()
        self._config.notifications.show_errors = self._notif_error_check.isChecked()
        self._config.notifications.show_file_changes = self._notif_files_check.isChecked()

        self._config.autostart = self._autostart_check.isChecked()
        self._config.start_minimized = self._minimize_check.isChecked()
        self._config.minimize_to_tray = self._minimize_tray_check.isChecked()

        try:
            save_config(self._config)
            logger.info("Settings saved")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save settings:\n{e}")

    def _save_and_close(self) -> None:
        self._save()
        self.accept()

    def _sign_out(self) -> None:
        reply = QMessageBox.question(
            self,
            "Sign Out",
            "Are you sure you want to sign out?\nYour files will remain on this computer.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            # Will be connected to auth manager
            pass

    @staticmethod
    def _open_url(url: str) -> None:
        import subprocess
        try:
            subprocess.Popen(["xdg-open", url])
        except FileNotFoundError:
            pass
