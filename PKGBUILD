# Maintainer: CloudDrive Contributors
pkgname=clouddrive
pkgver=0.1.9
pkgrel=1
pkgdesc="A modern, user-friendly OneDrive client for Linux"
arch=('any')
url="https://github.com/Cyclosaryn/CloudDrive"
license=('GPL-3.0-or-later')
depends=(
    'dbus'
)
makedepends=(
    'python-build'
    'python-installer'
    'python-setuptools'
    'python-setuptools-scm'
    'python-wheel'
    'python-pip'
)
# All Python dependencies are bundled in the wheel
optdepends=(
    'python-tomli-w: Saving configuration (required for settings changes)'
    'libnotify: Desktop notifications'
    'xdg-utils: Opening folders and URLs'
)
# For AUR builds, download from GitHub
source=("${pkgname}-${pkgver}.tar.gz::${url}/archive/v${pkgver}.tar.gz")
sha256sums=('7a741b601ebe8e4e2a63f39c8cecb45b0cc45f027e8015577cd3aed3523c46ad')

# GitHub archive extracts to CloudDrive-<version>/
_srcdir="CloudDrive-${pkgver}"

build() {
    cd "${_srcdir}"
    # Install Python dependencies and pyinstaller
    pip install --break-system-packages PySide6 msal httpx keyring watchdog platformdirs pydbus sqlalchemy aiosqlite humanize tomli_w pyinstaller
    # Build bundled executables
    ~/.local/bin/pyinstaller --onefile src/clouddrive/gui/app.py --name clouddrive-gui
    ~/.local/bin/pyinstaller --onefile src/clouddrive/cli/main.py --name clouddrive-cli
    ~/.local/bin/pyinstaller --onefile src/clouddrive/daemon/service.py --name clouddrive-daemon
}

package() {
    cd "${_srcdir}"
    # Install bundled executables
    install -Dm755 dist/clouddrive-gui "${pkgdir}/usr/bin/clouddrive-gui"
    install -Dm755 dist/clouddrive-cli "${pkgdir}/usr/bin/clouddrive-cli"
    install -Dm755 dist/clouddrive-daemon "${pkgdir}/usr/bin/clouddrive-daemon"

    # Install systemd user service
    install -Dm644 systemd/clouddrive.service \
        "${pkgdir}/usr/lib/systemd/user/clouddrive.service"

    # Install D-Bus service file
    install -Dm644 dbus/org.clouddrive.Daemon.service \
        "${pkgdir}/usr/share/dbus-1/services/org.clouddrive.Daemon.service"

    # Install desktop entry
    install -Dm644 desktop/clouddrive.desktop \
        "${pkgdir}/usr/share/applications/clouddrive.desktop"

    # Install icon
    install -Dm644 resources/clouddrive.svg \
        "${pkgdir}/usr/share/icons/hicolor/scalable/apps/clouddrive.svg"

    # Install license
    install -Dm644 LICENSE "${pkgdir}/usr/share/licenses/${pkgname}/LICENSE"
}
