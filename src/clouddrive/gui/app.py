"""CloudDrive Qt6 GUI application entry point.

Launches the system tray icon and manages the main event loop.
"""

from __future__ import annotations

import logging
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

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
# Test
