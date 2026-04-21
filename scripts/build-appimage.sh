#!/usr/bin/env bash
# Build an AppImage for CloudDrive.
#
# Prerequisites (Arch Linux):
#   sudo pacman -S python python-pip fuse2
#   pip install pyinstaller
#   wget https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-x86_64.AppImage
#   chmod +x linuxdeploy-x86_64.AppImage
#
# Usage:
#   ./scripts/build-appimage.sh
#
# Output:
#   CloudDrive-x86_64.AppImage
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
APP_DIR="${PROJECT_DIR}/AppDir"
VERSION="0.1.0"

echo "=== Building CloudDrive AppImage v${VERSION} ==="

# Clean previous build
rm -rf "$APP_DIR" "${PROJECT_DIR}"/CloudDrive-*.AppImage

# Step 1: Build with PyInstaller (one-dir mode for AppImage)
echo "--- PyInstaller build ---"
cd "$PROJECT_DIR"
pyinstaller \
    --noconfirm \
    --clean \
    --distpath="${APP_DIR}/usr/bin" \
    --specpath=build \
    --name=clouddrive \
    --onedir \
    --add-data="resources/*.svg:resources" \
    --hidden-import=sqlalchemy.dialects.sqlite \
    --hidden-import=aiosqlite \
    --hidden-import=msal \
    --hidden-import=keyring.backends.SecretService \
    --hidden-import=pydbus \
    --strip \
    src/clouddrive/cli/main.py

pyinstaller \
    --noconfirm \
    --clean \
    --distpath="${APP_DIR}/usr/bin" \
    --specpath=build \
    --name=clouddrive-gui \
    --onedir \
    --windowed \
    --add-data="resources/*.svg:resources" \
    --hidden-import=sqlalchemy.dialects.sqlite \
    --hidden-import=aiosqlite \
    --hidden-import=msal \
    --hidden-import=keyring.backends.SecretService \
    --hidden-import=pydbus \
    --strip \
    src/clouddrive/gui/app.py

# Step 2: Set up AppDir structure
echo "--- Setting up AppDir ---"
mkdir -p "${APP_DIR}/usr/share/applications"
mkdir -p "${APP_DIR}/usr/share/icons/hicolor/scalable/apps"

cp desktop/clouddrive.desktop "${APP_DIR}/usr/share/applications/"
cp resources/clouddrive.svg "${APP_DIR}/usr/share/icons/hicolor/scalable/apps/"

# AppRun entry point
cat > "${APP_DIR}/AppRun" << 'APPRUN'
#!/bin/bash
SELF="$(readlink -f "$0")"
APPDIR="$(dirname "$SELF")"
export PATH="${APPDIR}/usr/bin/clouddrive-gui:${APPDIR}/usr/bin/clouddrive:${PATH}"
export LD_LIBRARY_PATH="${APPDIR}/usr/lib:${LD_LIBRARY_PATH:-}"

if [ "$1" = "--cli" ] || [ "$(basename "$0")" = "clouddrive" ]; then
    exec "${APPDIR}/usr/bin/clouddrive/clouddrive" "${@:2}"
else
    exec "${APPDIR}/usr/bin/clouddrive-gui/clouddrive-gui" "$@"
fi
APPRUN
chmod +x "${APP_DIR}/AppRun"

# Symlink icon for linuxdeploy
ln -sf usr/share/icons/hicolor/scalable/apps/clouddrive.svg "${APP_DIR}/clouddrive.svg"
ln -sf usr/share/applications/clouddrive.desktop "${APP_DIR}/clouddrive.desktop"

# Step 3: Build AppImage
echo "--- Creating AppImage ---"
if command -v linuxdeploy-x86_64.AppImage &>/dev/null; then
    LINUXDEPLOY=linuxdeploy-x86_64.AppImage
elif [ -f "${PROJECT_DIR}/linuxdeploy-x86_64.AppImage" ]; then
    LINUXDEPLOY="${PROJECT_DIR}/linuxdeploy-x86_64.AppImage"
else
    echo "linuxdeploy not found. Downloading..."
    wget -q "https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-x86_64.AppImage" \
        -O "${PROJECT_DIR}/linuxdeploy-x86_64.AppImage"
    chmod +x "${PROJECT_DIR}/linuxdeploy-x86_64.AppImage"
    LINUXDEPLOY="${PROJECT_DIR}/linuxdeploy-x86_64.AppImage"
fi

export VERSION
"$LINUXDEPLOY" --appdir="$APP_DIR" --output=appimage

echo ""
echo "=== Done! ==="
echo "Output: CloudDrive-${VERSION}-x86_64.AppImage"
echo ""
echo "Users run:  chmod +x CloudDrive-*.AppImage && ./CloudDrive-*.AppImage"
echo "CLI mode:   ./CloudDrive-*.AppImage --cli status"
