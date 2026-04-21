"""First-run setup wizard.

Guides the user through:
1. Welcome / app registration
2. Signing in with their Microsoft account
3. Choosing the sync folder location
4. Basic settings
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from PySide6.QtWidgets import (
    QWizard,
    QWizardPage,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QFileDialog,
    QCheckBox,
    QGroupBox,
    QFormLayout,
    QMessageBox,
    QProgressBar,
    QWidget,
)
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont

from clouddrive.core.config import AppConfig, save_config, DEFAULT_CLIENT_ID

logger = logging.getLogger(__name__)


class WelcomePage(QWizardPage):
    """Welcome page with app registration instructions."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setTitle("Welcome to CloudDrive")
        self.setSubTitle("A modern OneDrive client for Linux")

        layout = QVBoxLayout(self)
        layout.setSpacing(16)

        welcome = QLabel(
            "<p>CloudDrive syncs your files with Microsoft OneDrive, "
            "just like the official client on Windows.</p>"
            "<p>CloudDrive includes a built-in app registration, so you can sign in right away. "
            "If you prefer to use your own Azure app, you can provide a custom client ID below.</p>"
        )
        welcome.setWordWrap(True)
        layout.addWidget(welcome)

        # Step-by-step instructions
        steps_group = QGroupBox("How to register your app")
        steps_layout = QVBoxLayout(steps_group)

        instructions = QLabel(
            "<b>Optional: Use a custom app registration</b><br/>"
            "<ol>"
            "<li>Visit the <a href='https://portal.azure.com/#blade/Microsoft_AAD_RegisteredApps/ApplicationsListBlade'>"
            "Azure App Registrations</a> page</li>"
            "<li>Click <b>New registration</b></li>"
            "<li>Name: <b>CloudDrive</b> (or any name you like)</li>"
            "<li>Supported account types: <b>Personal Microsoft accounts only</b><br/>"
            "   (or 'Accounts in any organizational directory and personal' for Business)</li>"
            "<li>Redirect URI: leave blank for now, click <b>Register</b></li>"
            "<li>Go to <b>Authentication</b> → <b>Add a platform</b> → "
            "<b>Mobile and desktop applications</b></li>"
            "<li>Add <code>http://localhost:8400</code> as the redirect URI</li>"
            "<li>Under <b>Advanced settings</b>, set <b>Allow public client flows</b> to <b>Yes</b></li>"
            "<li>Click <b>Save</b></li>"
            "<li>Copy the <b>Application (client) ID</b> from the Overview page and paste it below</li>"
            "</ol>"
        )
        instructions.setWordWrap(True)
        instructions.setOpenExternalLinks(True)
        steps_layout.addWidget(instructions)

        layout.addWidget(steps_group)

        # Client ID input
        id_layout = QFormLayout()
        self._client_id_edit = QLineEdit()
        self._client_id_edit.setText(DEFAULT_CLIENT_ID)
        self._client_id_edit.setPlaceholderText("xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
        self.registerField("client_id", self._client_id_edit)
        id_layout.addRow("Application (client) ID (optional):", self._client_id_edit)
        layout.addLayout(id_layout)

        # Open Azure Portal button
        azure_btn = QPushButton("Open Azure Portal")
        azure_btn.setMaximumWidth(200)
        azure_btn.clicked.connect(
            lambda: subprocess.Popen(
                ["xdg-open", "https://portal.azure.com/#blade/Microsoft_AAD_RegisteredApps/ApplicationsListBlade"]
            )
        )
        layout.addWidget(azure_btn)

        layout.addStretch()


class SignInPage(QWizardPage):
    """Authentication page — sign in with Microsoft account."""

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._authenticated = False

        self.setTitle("Sign in to OneDrive")
        self.setSubTitle("Click the button below to sign in with your Microsoft account")

        layout = QVBoxLayout(self)
        layout.setSpacing(16)

        self._status_label = QLabel("Not signed in")
        self._status_label.setStyleSheet("font-size: 12px; padding: 8px;")
        layout.addWidget(self._status_label)

        self._sign_in_btn = QPushButton("Sign in with Microsoft")
        self._sign_in_btn.setMinimumHeight(40)
        self._sign_in_btn.setStyleSheet(
            "QPushButton { background-color: #0078d4; color: white; "
            "font-size: 14px; border-radius: 4px; padding: 8px 24px; }"
            "QPushButton:hover { background-color: #106ebe; }"
        )
        self._sign_in_btn.clicked.connect(self._do_sign_in)
        layout.addWidget(self._sign_in_btn)

        # Device code fallback
        self._device_code_btn = QPushButton("Use device code (for terminals / headless)")
        self._device_code_btn.clicked.connect(self._do_device_code)
        layout.addWidget(self._device_code_btn)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)  # Indeterminate
        self._progress.hide()
        layout.addWidget(self._progress)

        self._result_label = QLabel("")
        self._result_label.setWordWrap(True)
        layout.addWidget(self._result_label)

        layout.addStretch()

    def initializePage(self) -> None:
        # Pick up client_id from previous page
        client_id = self.field("client_id")
        if client_id:
            self._config.auth.client_id = client_id

    def isComplete(self) -> bool:
        return self._authenticated

    def _do_sign_in(self) -> None:
        from clouddrive.core.auth import AuthManager

        self._progress.show()
        self._status_label.setText("Opening browser for sign-in...")

        try:
            auth = AuthManager(self._config)
            result = auth.authenticate_interactive()
            if result and "access_token" in result:
                self._authenticated = True
                account = auth.get_account_info()
                name = account.get("name", "User") if account else "User"
                self._status_label.setText(f"✓ Signed in as {name}")
                self._status_label.setStyleSheet(
                    "color: #2ecc71; font-size: 12px; padding: 8px; font-weight: bold;"
                )
                self._sign_in_btn.setEnabled(False)
                self._result_label.setText("Authentication successful!")
            else:
                self._status_label.setText("✗ Sign-in failed")
                self._status_label.setStyleSheet(
                    "color: #e74c3c; font-size: 12px; padding: 8px;"
                )
                self._result_label.setText(
                    "Could not complete sign-in. Please try again."
                )
        except Exception as e:
            self._status_label.setText("✗ Error")
            self._result_label.setText(str(e))
            logger.exception("Sign-in failed")
        finally:
            self._progress.hide()
            self.completeChanged.emit()

    def _do_device_code(self) -> None:
        from clouddrive.core.auth import AuthManager

        self._progress.show()
        self._status_label.setText("Getting device code...")

        try:
            auth = AuthManager(self._config)

            def on_device_code(flow: dict) -> None:
                url = flow.get("verification_uri", "")
                code = flow.get("user_code", "")
                self._status_label.setText(
                    f"Visit: {url}\nEnter code: {code}"
                )
                self._result_label.setText(
                    f"<b>Go to:</b> <a href='{url}'>{url}</a><br/>"
                    f"<b>Enter code:</b> {code}"
                )
                self._result_label.setOpenExternalLinks(True)

            result = auth.authenticate_device_code(callback=on_device_code)
            if result and "access_token" in result:
                self._authenticated = True
                account = auth.get_account_info()
                name = account.get("name", "User") if account else "User"
                self._status_label.setText(f"✓ Signed in as {name}")
                self._status_label.setStyleSheet(
                    "color: #2ecc71; font-size: 12px; padding: 8px; font-weight: bold;"
                )
        except Exception as e:
            self._status_label.setText("✗ Error")
            self._result_label.setText(str(e))
            logger.exception("Device code auth failed")
        finally:
            self._progress.hide()
            self.completeChanged.emit()


class FolderPage(QWizardPage):
    """Choose the sync folder location."""

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self.setTitle("Choose your OneDrive folder")
        self.setSubTitle("Select where to sync your OneDrive files on this computer")

        layout = QVBoxLayout(self)
        layout.setSpacing(16)

        info = QLabel(
            "Files in this folder will be kept in sync with your OneDrive. "
            "Choose an existing folder or create a new one."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        # Folder selection
        folder_layout = QHBoxLayout()
        self._folder_edit = QLineEdit(self._config.sync.sync_dir)
        self.registerField("sync_dir", self._folder_edit)
        folder_layout.addWidget(self._folder_edit)

        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse)
        folder_layout.addWidget(browse_btn)

        layout.addLayout(folder_layout)

        # Default location hint
        default_label = QLabel(
            f"<small>Default: ~/OneDrive</small>"
        )
        default_label.setStyleSheet("color: #888;")
        layout.addWidget(default_label)

        layout.addStretch()

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choose OneDrive Folder", str(Path.home()),
            QFileDialog.Option.DontUseNativeDialog,
        )
        if folder:
            self._folder_edit.setText(folder)


class OptionsPage(QWizardPage):
    """Basic settings before finishing."""

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self.setTitle("Preferences")
        self.setSubTitle("Configure your initial sync preferences")

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        self._autostart_check = QCheckBox("Start CloudDrive automatically when you log in")
        self._autostart_check.setChecked(True)
        layout.addWidget(self._autostart_check)

        self._notifications_check = QCheckBox("Show desktop notifications for sync events")
        self._notifications_check.setChecked(True)
        layout.addWidget(self._notifications_check)

        self._realtime_check = QCheckBox("Sync files immediately when they change (recommended)")
        self._realtime_check.setChecked(True)
        layout.addWidget(self._realtime_check)

        self._dotfiles_check = QCheckBox("Skip hidden files and folders (starting with '.')")
        self._dotfiles_check.setChecked(True)
        layout.addWidget(self._dotfiles_check)

        layout.addStretch()

        done_label = QLabel(
            "<p>Click <b>Finish</b> to start syncing your files!</p>"
            "<p>You can change these settings anytime from the system tray icon.</p>"
        )
        done_label.setWordWrap(True)
        layout.addWidget(done_label)


class SetupWizard(QWizard):
    """First-run setup wizard."""

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self.setWindowTitle("CloudDrive Setup")
        self.setMinimumSize(640, 520)
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)

        self.addPage(WelcomePage())
        self.addPage(SignInPage(config))
        self.addPage(FolderPage(config))
        self._options_page = OptionsPage(config)
        self.addPage(self._options_page)

        self.finished.connect(self._on_finish)

    def _on_finish(self, result: int) -> None:
        if result != QWizard.DialogCode.Accepted:
            return

        # Apply wizard settings to config
        self._config.auth.client_id = self.field("client_id") or ""
        sync_dir = self.field("sync_dir")
        if sync_dir:
            self._config.sync.sync_dir = sync_dir

        self._config.autostart = self._options_page._autostart_check.isChecked()
        self._config.notifications.enabled = self._options_page._notifications_check.isChecked()
        self._config.sync.monitor_real_time = self._options_page._realtime_check.isChecked()
        self._config.sync.skip_dotfiles = self._options_page._dotfiles_check.isChecked()

        # Register the authenticated account so the wizard won't show again
        if not self._config.accounts:
            self._config.accounts.append({
                "name": "Personal",
                "account_type": "personal",
                "client_id": self._config.auth.client_id,
                "sync_dir": self._config.sync.sync_dir,
                "enabled": True,
            })

        # Save config
        try:
            save_config(self._config)
            logger.info("Setup wizard completed, config saved")
        except Exception as e:
            logger.exception("Failed to save config from wizard")
            QMessageBox.critical(
                self, "Error", f"Failed to save settings:\n{e}"
            )

        # Create sync directory
        sync_path = self._config.sync.sync_dir_path
        sync_path.mkdir(parents=True, exist_ok=True)

        # Install autostart desktop entry if enabled
        if self._config.autostart:
            self._install_autostart()

    def _install_autostart(self) -> None:
        """Install XDG autostart desktop entry."""
        autostart_dir = Path.home() / ".config" / "autostart"
        autostart_dir.mkdir(parents=True, exist_ok=True)

        desktop_entry = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=CloudDrive\n"
            "Comment=OneDrive sync client for Linux\n"
            "Exec=clouddrive-gui\n"
            "Icon=clouddrive\n"
            "Terminal=false\n"
            "StartupNotify=false\n"
            "Categories=Network;FileTransfer;\n"
            "X-GNOME-Autostart-enabled=true\n"
        )

        entry_path = autostart_dir / "clouddrive.desktop"
        entry_path.write_text(desktop_entry, encoding="utf-8")
        logger.info("Autostart entry installed: %s", entry_path)
