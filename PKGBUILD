# Maintainer: CloudDrive Contributors
pkgname=clouddrive
pkgver=0.1.0
pkgrel=1
pkgdesc="A modern, user-friendly OneDrive client for Linux"
arch=('any')
url="https://github.com/clouddrive-linux/clouddrive"
license=('GPL-3.0-or-later')
depends=(
    'python>=3.11'
    'python-pyside6'
    'python-httpx'
    'python-msal'
    'python-keyring'
    'python-watchdog'
    'python-platformdirs'
    'python-pydbus'
    'python-sqlalchemy'
    'python-humanize'
    'dbus'
)
makedepends=(
    'python-build'
    'python-installer'
    'python-setuptools'
    'python-wheel'
)
optdepends=(
    'python-tomli-w: Saving configuration (required for settings changes)'
    'libnotify: Desktop notifications'
    'xdg-utils: Opening folders and URLs'
)
source=("${pkgname}-${pkgver}.tar.gz::${url}/archive/v${pkgver}.tar.gz")
sha256sums=('SKIP')

build() {
    cd "${pkgname}-${pkgver}"
    python -m build --wheel --no-isolation
}

package() {
    cd "${pkgname}-${pkgver}"
    python -m installer --destdir="${pkgdir}" dist/*.whl

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
