# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec file for CloudDrive.

Produces a single-file executable that bundles Python, Qt6, and all
dependencies. End users just download and run — no pip, no venv.

Build (on Arch Linux):
    pip install pyinstaller
    pyinstaller clouddrive.spec

Output:
    dist/clouddrive          — CLI + daemon binary
    dist/clouddrive-gui      — GUI binary (system tray)
"""

import sys
from pathlib import Path

block_cipher = None

# Shared analysis for all entry points
common_kwargs = dict(
    pathex=[str(Path("src"))],
    binaries=[],
    datas=[
        ("resources/*.svg", "resources"),
        ("systemd/*", "systemd"),
        ("dbus/*", "dbus"),
        ("desktop/*", "desktop"),
        ("LICENSE", "."),
    ],
    hiddenimports=[
        "clouddrive.core.config",
        "clouddrive.core.auth",
        "clouddrive.core.api",
        "clouddrive.core.database",
        "clouddrive.core.sync_engine",
        "clouddrive.core.watcher",
        "clouddrive.core.placeholders",
        "clouddrive.core.ondemand",
        "clouddrive.daemon.service",
        "clouddrive.cli.main",
        "clouddrive.gui.app",
        "clouddrive.gui.tray",
        "clouddrive.gui.settings",
        "clouddrive.gui.activity",
        "clouddrive.gui.wizard",
        "sqlalchemy.dialects.sqlite",
        "aiosqlite",
        "msal",
        "httpx",
        "httpx._transports",
        "httpx._transports.default",
        "httpcore",
        "httpcore._async",
        "keyring.backends",
        "keyring.backends.SecretService",
        "platformdirs",
        "humanize",
        "tomllib",
        "pydbus",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "test",
        "unittest",
        "pytest",
    ],
    cipher=block_cipher,
    noarchive=False,
)

# --- CLI / Daemon binary ---
cli_a = Analysis(
    ["src/clouddrive/cli/main.py"],
    **common_kwargs,
)
cli_pyz = PYZ(cli_a.pure, cipher=block_cipher)
cli_exe = EXE(
    cli_pyz,
    cli_a.scripts,
    cli_a.binaries,
    cli_a.datas,
    [],
    name="clouddrive",
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=True,
    console=True,
    icon="resources/clouddrive.svg",
)

# --- GUI binary ---
gui_a = Analysis(
    ["src/clouddrive/gui/app.py"],
    **common_kwargs,
)
gui_pyz = PYZ(gui_a.pure, cipher=block_cipher)
gui_exe = EXE(
    gui_pyz,
    gui_a.scripts,
    gui_a.binaries,
    gui_a.datas,
    [],
    name="clouddrive-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=True,
    console=False,
    icon="resources/clouddrive.svg",
)
