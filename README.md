# CloudDrive — A Modern OneDrive Client for Linux

A user-friendly, full-featured Microsoft OneDrive client designed for Arch Linux and other Linux distributions. Built to provide an experience close to the official Windows OneDrive client, with a native system tray, setup wizard, and graphical settings.

## Why CloudDrive?

The existing `abraunegg/onedrive` client is powerful but CLI-heavy and difficult for casual users. CloudDrive fills the gap with:

| Feature | abraunegg/onedrive | CloudDrive |
|---|---|---|
| System tray icon with status | ✗ (external add-on) | ✓ Built-in |
| Setup wizard | ✗ | ✓ Guided first-run |
| GUI settings | ✗ (external add-on) | ✓ Built-in |
| Activity center | ✗ | ✓ Recent file history |
| Pause/resume from tray | ✗ | ✓ One click |
| Desktop notifications | Partial (libnotify) | ✓ Native Qt |
| Files On-Demand | ✗ | ✓ Metadata-first, download on open |
| Priority download on open | ✗ | ✓ User clicks bypass background sync |
| Selective folder sync | Partial | ✓ Per-folder selection |
| SharePoint / Shared folders | ✗ | ✓ Sites, libraries, shared items |
| File version history | ✗ | ✓ View, download, restore versions |
| Bandwidth scheduling | ✗ | ✓ Time-of-day rules |
| Multi-account support | ✗ | ✓ Multiple OneDrive accounts |
| CLI for scripting | ✓ | ✓ Full CLI too |
| Real-time sync | ✓ (inotify) | ✓ (watchdog/inotify) |
| Delta queries (efficient) | ✓ | ✓ |
| Conflict resolution | ✓ | ✓ Keep both versions |
| Written in | D | Python (easier to contribute) |

## Features

- **System tray** — Cloud icon with live sync status, just like Windows
- **Setup wizard** — Guided Azure app registration + Microsoft sign-in
- **Activity center** — See recently synced files, uploads, downloads
- **Settings UI** — Tabbed dialog: Account, Sync, Network, Notifications
- **Files On-Demand** — Fetches metadata first so files appear instantly in the file manager; actual content downloads on-demand when you open a file (xattr-based placeholders with zero disk usage)
- **Priority download** — When you open a cloud-only file, it immediately gets highest priority and background sync pauses to give it full bandwidth
- **Selective folder sync** — Choose which OneDrive folders to sync locally
- **SharePoint & shared folders** — Browse and sync SharePoint document libraries and items shared with you
- **File version history** — View, download, or restore previous versions of any file via Microsoft Graph
- **Bandwidth scheduling** — Set upload/download speed limits by time of day and day of week
- **Multi-account support** — Manage multiple OneDrive personal or business accounts
- **Bi-directional sync** — Supports upload-only, download-only, or both
- **Real-time monitoring** — Instant sync on local file changes via inotify
- **Delta queries** — Only fetches what changed from OneDrive (efficient)
- **Resumable uploads** — Large files use chunked upload sessions
- **Conflict handling** — Keeps both versions with clear naming
- **Trash integration** — Remotely deleted files go to FreeDesktop Trash
- **D-Bus control** — GUI and CLI talk to daemon over D-Bus
- **systemd service** — Runs as a user service, starts on login
- **Arch Linux PKGBUILD** — Native packaging for pacman/AUR

## Architecture

```
┌──────────────────┐    D-Bus    ┌───────────────────────────┐
│   clouddrive-gui │◄──────────►│    clouddrive-daemon      │
│   (System Tray)  │            │    (Sync Service)         │
│   PySide6 / Qt6  │            │                           │
└──────────────────┘            │  ┌───────────────────┐    │
                                │  │   Sync Engine      │    │
┌──────────────────┐            │  │   (metadata-first) │    │
│   clouddrive     │◄──────────►│  └───────┬───────────┘    │
│   (CLI)          │   D-Bus    │          │                │
└──────────────────┘            │  ┌───────▼───────────┐    │
                                │  │  OnDemand Manager  │    │
                                │  │  (priority queue,  │    │
                                │  │   inotify watch)   │    │
                                │  └───────┬───────────┘    │
                                │          │                │
                                │  ┌───────▼───┐ ┌───────┐ │
                                │  │ Graph API  │ │SQLite │ │
                                │  │ (httpx)    │ │  DB   │ │
                                │  └────────────┘ └───────┘ │
                                │  ┌────────────┐ ┌───────┐ │
                                │  │ Placeholders│ │File   │ │
                                │  │ (xattr)    │ │Watcher│ │
                                │  └────────────┘ └───────┘ │
                                └───────────────────────────┘
```

## Installation

### Arch Linux (PKGBUILD)

```bash
# Update your system and package database
pacman -Syu

# Install build dependencies
pacman -S python-build python-installer python-setuptools python-setuptools-scm python-wheel python-pip

# Clone the repository
git clone https://github.com/Cyclosaryn/CloudDrive.git
cd CloudDrive

# Build and install the package
makepkg -si
```

### From Source (pip)

```bash
# Clone the repository
git clone https://github.com/Cyclosaryn/CloudDrive.git
cd CloudDrive

# Install in a virtual environment
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Dependencies (Arch Linux)

```bash
pacman -S python python-pip python-pyside6 python-sqlalchemy python-httpx dbus
pip install msal keyring watchdog platformdirs pydbus humanize
```

## Quick Start

CloudDrive ships with a built-in Azure app registration, so users can sign in immediately. No Azure portal setup required.

### 1. First Run (GUI)

```bash
clouddrive-gui
```

The setup wizard will guide you through signing in with your Microsoft account and choosing your sync folder. No Azure registration needed — a default app ID is built in.

### 2. First Run (CLI)

```bash
# Sign in
clouddrive auth

# Start the daemon
clouddrive-daemon
```

Optionally, to use your own Azure app registration:
```bash
clouddrive config --set auth.client_id=YOUR_CLIENT_ID
```

### 3. Enable Auto-Start

```bash
# Enable the systemd user service
systemctl --user enable --now clouddrive

# The GUI can also be added to autostart via the setup wizard
```

## CLI Reference

```
clouddrive auth           Sign in to Microsoft OneDrive
clouddrive auth --device-code   Sign in using device code (headless)
clouddrive status         Show sync status and storage info
clouddrive sync           Trigger an immediate sync
clouddrive pause          Pause syncing
clouddrive resume         Resume syncing
clouddrive activity       Show recent sync activity
clouddrive config         Show current configuration
clouddrive config --set KEY=VALUE   Change a setting
clouddrive version        Show version
```

## Configuration

Config file: `~/.config/clouddrive/config.toml`

```toml
log_level = "INFO"
minimize_to_tray = true
start_minimized = false
autostart = true

[sync]
sync_dir = "~/OneDrive"
sync_interval_seconds = 300
monitor_real_time = true
upload_only = false
download_only = false
skip_dotfiles = true
skip_symlinks = true
max_upload_speed_kbps = 0          # 0 = unlimited
max_download_speed_kbps = 0
concurrent_transfers = 4
files_on_demand = true              # Show files before downloading
selected_folders = []               # Empty = sync everything
auto_free_space_gb = 0              # Auto-reclaim space threshold (0 = off)
bandwidth_schedule_enabled = false

[[sync.bandwidth_schedule]]
days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
start_hour = 9
end_hour = 17
max_upload_kbps = 5000
max_download_kbps = 10000

[auth]
client_id = "your-azure-app-client-id"

# Multi-account: add additional [[accounts]] sections
# [[accounts]]
# name = "Work"
# account_type = "business"
# client_id = "another-client-id"
# sync_dir = "~/OneDrive-Work"

[notifications]
enabled = true
show_sync_complete = true
show_errors = true
show_file_changes = false
```

## File Locations

| Purpose | Path |
|---|---|
| Config | `~/.config/clouddrive/config.toml` |
| Database | `~/.local/share/clouddrive/sync_state.db` |
| Token cache | `~/.local/share/clouddrive/token_cache.json` |
| Log file | `~/.local/share/clouddrive/clouddrive.log` |
| Cache | `~/.cache/clouddrive/` |
| Sync folder | `~/OneDrive` (configurable) |

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run linter
ruff check src/

# Run type checker
mypy src/clouddrive/

# Run tests
pytest

# Build package
python -m build
```

## Project Structure

```
src/clouddrive/
├── __init__.py          # Version info
├── core/
│   ├── config.py        # Configuration management (TOML)
│   ├── auth.py          # MSAL OAuth2 authentication
│   ├── api.py           # Microsoft Graph API client (OneDrive, SharePoint, shared, versions)
│   ├── database.py      # SQLite sync state database (placeholder tracking)
│   ├── sync_engine.py   # Metadata-first bi-directional sync engine
│   ├── placeholders.py  # xattr-based placeholder file system (Files On-Demand)
│   ├── ondemand.py      # Priority-queue hydration manager (inotify-based)
│   └── watcher.py       # Local filesystem watcher
├── gui/
│   ├── app.py           # Qt application entry point
│   ├── tray.py          # System tray icon & menu
│   ├── settings.py      # Settings dialog (tabbed)
│   ├── activity.py      # Activity center window
│   └── wizard.py        # First-run setup wizard
├── daemon/
│   └── service.py       # Background sync daemon + D-Bus + on-demand manager
└── cli/
    └── main.py          # Command-line interface
```

## Roadmap

### Implemented

- [x] Files On-Demand — metadata-first sync with xattr placeholders
- [x] Priority on-demand download — user-opened files jump the queue
- [x] SharePoint document library sync (API layer)
- [x] Selective folder sync (config + sync engine filtering)
- [x] Shared folder support (API layer)
- [x] File version history — view, download, restore (API layer)
- [x] Bandwidth scheduling — time-of-day rules (config layer)
- [x] Multi-account support (config layer)

### Planned

- [ ] File manager overlay icons (Nautilus/Dolphin/Thunar extension plugins)
- [ ] GUI for selective folder sync, versioning, bandwidth schedule, multi-account
- [ ] Flatpak packaging

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE)

## Contributing

Contributions welcome! Please open an issue or PR on GitHub. The codebase is Python so it's easy to dive in.

