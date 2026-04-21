"""CloudDrive Qt6 GUI application entry point.

Launches the system tray icon, starts the background sync daemon,
and manages the main event loop.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon
from PySide6.QtCore import QTimer

from clouddrive.core.config import load_config
from clouddrive.gui.tray import SystemTrayManager

logger = logging.getLogger(__name__)

ICON_DIR = Path(__file__).parent / "resources"


def setup_logging(log_level: str, log_file: Path) -> None:
    """Configure logging to file and console."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stderr),
        ],
    )


def _start_daemon() -> subprocess.Popen | None:
    """Start the sync daemon as a subprocess."""
    import shutil
    daemon_path = shutil.which("clouddrive-daemon")
    if daemon_path:
        logger.info("Starting daemon: %s", daemon_path)
        proc = subprocess.Popen(
            [daemon_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc
    else:
        logger.warning("clouddrive-daemon not found in PATH, sync will not work")
        return None


def _connect_dbus():
    """Connect to the daemon's D-Bus interface. Returns proxy or None."""
    try:
        from pydbus import SessionBus
        bus = SessionBus()
        proxy = bus.get("org.clouddrive.Daemon", "/org/clouddrive/Daemon")
        return proxy
    except Exception:
        return None


def main() -> None:
    """Main entry point for the CloudDrive GUI."""
    config = load_config()
    setup_logging(config.log_level, config.log_file)
    logger.info("CloudDrive starting...")

    app = QApplication(sys.argv)
    app.setApplicationName("CloudDrive")
    app.setOrganizationName("CloudDrive")
    app.setQuitOnLastWindowClosed(False)

    # Set application icon
    icon_path = ICON_DIR / "clouddrive.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    tray = SystemTrayManager(config)
    tray.show()

    # Start daemon if user is authenticated
    daemon_proc = None
    if config.accounts:
        daemon_proc = _start_daemon()

        # Poll for D-Bus connection and status updates
        def poll_daemon():
            proxy = _connect_dbus()
            if proxy:
                try:
                    status = proxy.GetStatus()
                    logger.debug("Daemon status: %s", status)
                    from clouddrive.core.sync_engine import SyncStatus
                    status_map = {s.name: s for s in SyncStatus}
                    if status in status_map:
                        tray.update_sync_status(status_map[status])
                except Exception:
                    pass

                # Wire tray actions to D-Bus
                tray.set_dbus_proxy(proxy)

        # Give daemon time to start, then connect
        QTimer.singleShot(3000, poll_daemon)

        # Periodic status polling
        status_timer = QTimer()
        status_timer.timeout.connect(poll_daemon)
        status_timer.start(10000)  # Poll every 10 seconds

    def cleanup():
        if daemon_proc and daemon_proc.poll() is None:
            logger.info("Stopping daemon...")
            daemon_proc.terminate()
            daemon_proc.wait(timeout=5)

    app.aboutToQuit.connect(cleanup)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
