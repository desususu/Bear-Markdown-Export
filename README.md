# Bear Markdown Export & Sync

> **A complete refactor of the original [rovest/Bear-Markdown-Export](https://github.com/rovest/Bear-Markdown-Export), updated for Bear 2.0, with major performance improvements, a real-time sync daemon, and improved image handling.**

**中文文档请见 [README_zh.md](README_zh.md)**

---

## ⚠️ Important Warning — Back Up First

**Before running these scripts for the first time, back up your Bear notes.**

Go to **Bear → File → Backup Notes…** and save the archive somewhere safe.
Also back up your Mac with Time Machine or your preferred tool.

Both `rsync` and `shutil.rmtree` are used internally. An incorrectly configured path could overwrite or delete files. Always verify your config before the first run.

---

## Overview

This project provides two scripts that work together:

| Script | Role |
|---|---|
| `bear_export_sync.py` | Core engine — exports Bear notes to Markdown / Textbundle, syncs external edits back into Bear |
| `dual_sync.py` | Daemon — runs the core engine automatically, triggered by real-time file-system events |

### What it does

- Exports all Bear notes to plain `.md` files or `.textbundle` packages (images included)
- Watches for edits made in external editors (Obsidian, Typora, Ulysses, etc.) and syncs them back into Bear
- Runs as a background daemon with near-instant reaction to Bear note changes (~1–2 seconds)
- Supports both Markdown and Textbundle formats simultaneously via dual export folders

---

## Compatibility

- **macOS only** — uses macOS-native frameworks (`AppKit`, `NSWorkspace`, `FSEvents`)
- **Bear 2.0** — reads Bear's current SQLite database schema and Group Container paths
- **Python 3.9+** recommended (3.6+ minimum)

---

## What's New in This Refactor

### Performance Optimizations

- **Pre-compiled regex patterns** — all regular expressions are compiled once at module load rather than on every note, yielding a significant speed-up on large vaults
- **Incremental image copying** — images are now copied directly and incrementally during the export loop itself; the previous separate rsync pass over the entire image store has been eliminated
- **Timestamp-based change detection** — the script checks the modification time of Bear's `database.sqlite` before doing any work; if nothing has changed, it exits immediately without touching the disk
- **Fast-exit on no changes** — both the MD and TB export phases return exit code `0` when nothing needs syncing, so the daemon can skip unnecessary work

### Real-Time Sync Daemon (`dual_sync.py`)

- **FSEvents-triggered export** — watches Bear's live SQLite WAL file; when Bear commits a note save, an export cycle fires within 1–2 seconds instead of waiting for the next polling interval
- **File-write quiesce guard** — monitors the export folders with `watchdog`; if an external editor is actively writing files, the sync timer pauses and resumes only after the folder has been quiet for a configurable number of seconds (default: 5 s), preventing a half-written file from being imported back into Bear
- **Sync window** — configurable active hours (e.g., 06:00–23:20); the daemon sleeps outside this window
- **Editor-active detection** — detects if Bear, Obsidian, Typora, or Ulysses is the frontmost app and defers sync to avoid conflicts
- **Manual trigger** — send `SIGUSR1` (or run `python3 dual_sync.py --trigger`) to force an immediate sync cycle bypassing all guards
- **Hot config reload** — re-reads `sync_config.json` on every loop tick; no restart required for config changes

### Improved Image Handling

- Images are resolved from Bear's internal image store and copied to the export folder during export
- Textbundle exports include images as `assets/` inside the `.textbundle` package
- Markdown exports link to a shared `BearImages/` repository (or a custom `--images` path)
- UUID prefixes are stripped from image filenames for cleaner output
- Both `![alt](url)` and `![[wikilink]]` image syntaxes are handled correctly on round-trip sync back into Bear

### Bear 2.0 Compatibility

- Reads the current Group Container path: `~/Library/Group Containers/9K33E3U3T4.net.shinyfrog.bear/Application Data/database.sqlite`
- Supports the current Bear note image storage layout
- Bear IDs embedded in notes use the modern `[//]: # ({BearID:...})` format (legacy HTML comment format is also recognized for backward compatibility)

---

## Requirements

### System

- macOS 12 Monterey or later (recommended)
- Bear app installed and signed in

### Python Packages

Install all dependencies with:

```bash
pip install pyobjc-framework-Cocoa watchdog
```

| Package | Purpose | Required? |
|---|---|---|
| `pyobjc-framework-Cocoa` | `AppKit` / `NSWorkspace` — opens Bear via URL scheme | **Required** |
| `watchdog` | FSEvents observer for real-time DB and folder monitoring | Strongly recommended |

> If `watchdog` is not installed, the daemon falls back to interval-only polling and the file-write quiesce guard is disabled. A warning is printed at startup.

### Standard Library (no install needed)

`sqlite3`, `re`, `subprocess`, `shutil`, `argparse`, `json`, `threading`, `signal`, `logging`

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/desususu/Bear-Markdown-Export.git
cd Bear-Markdown-Export

# 2. (Recommended) Create a virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install pyobjc-framework-Cocoa watchdog

# 4. Back up your Bear notes before first run!
#    Bear → File → Backup Notes…
```

---

## Configuration

### `sync_config.json`

On first run, `dual_sync.py` creates a default `sync_config.json` next to itself and exits, asking you to review the paths.

```json
{
    "script_path":            "./bear_export_sync.py",
    "folder_md":              "./Export/MD_Export",
    "folder_tb":              "./Export/TB_Export",
    "backup_md":              "./Backup/MD_Backup",
    "backup_tb":              "./Backup/TB_Backup",
    "sync_interval_seconds":  180,
    "sync_on_startup":        false,
    "write_quiet_seconds":    5,
    "fast_trigger_on_db_change": true,
    "sync_window": {
        "start_hour":  6,  "start_minute":  0,
        "end_hour":   23,  "end_minute":   20
    }
}
```

| Key | Description |
|---|---|
| `script_path` | Path to `bear_export_sync.py` (relative or absolute) |
| `folder_md` | Where Markdown exports are written |
| `folder_tb` | Where Textbundle exports are written |
| `backup_md` / `backup_tb` | Backup folders for conflict resolution (must be outside `folder_md` / `folder_tb`) |
| `sync_interval_seconds` | Fallback polling interval in seconds (minimum 30) |
| `sync_on_startup` | Run a full sync immediately when the daemon starts |
| `write_quiet_seconds` | Seconds of folder inactivity required before syncing (prevents importing half-written files) |
| `fast_trigger_on_db_change` | Enable FSEvents-based instant export when Bear saves a note |
| `sync_window` | Active hours; daemon sleeps outside this range |

---

## Usage

### Run the daemon (recommended)

```bash
python3 dual_sync.py
```

The daemon runs in the foreground. Use a terminal multiplexer (`tmux`, `screen`) or a launchd plist to keep it running in the background.

### Run one sync cycle and exit (for cron / launchd)

```bash
python3 dual_sync.py --once
```

### Trigger an immediate sync on the running daemon

```bash
python3 dual_sync.py --trigger
```

### Check daemon status

```bash
python3 dual_sync.py --status
```

### Run the core script directly

```bash
# Export only (Markdown)
python3 bear_export_sync.py --out ~/Notes/Bear --backup ~/Notes/Backup --format md

# Export only (Textbundle)
python3 bear_export_sync.py --out ~/Notes/Bear --backup ~/Notes/Backup --format tb

# Skip export, import only
python3 bear_export_sync.py --out ~/Notes/Bear --backup ~/Notes/Backup --skipExport

# Skip import, export only
python3 bear_export_sync.py --out ~/Notes/Bear --backup ~/Notes/Backup --skipImport

# Exclude notes with a specific tag
python3 bear_export_sync.py --out ~/Notes/Bear --backup ~/Notes/Backup --excludeTag private

# Use a custom image folder
python3 bear_export_sync.py --out ~/Notes/Bear --backup ~/Notes/Backup --images ~/Notes/Images
```

---

## How Sync Works

### Export (Bear → disk)

1. Checks `database.sqlite` modification time; exits immediately if nothing changed
2. Queries all notes from Bear's SQLite database
3. Writes each note as `.md` or `.textbundle` to a temp folder
4. Strips Bear-specific syntax (image references, tag formatting) for external compatibility
5. Appends a `BearID` footer so the note can be matched on re-import
6. Uses `rsync` to copy only changed files from the temp folder to the export folder (Dropbox, Obsidian vault, etc.)

### Import (disk → Bear)

1. Scans the export folder for `.md` / `.textbundle` files modified since last sync
2. Matches each file to its original Bear note via the embedded `BearID`
3. Uses the `bear://x-callback-url/add-text?mode=replace` URL scheme to update the note body, preserving the original creation date and note ID
4. On sync conflict, both versions are kept in Bear with a conflict notice
5. New files without a `BearID` are created as new Bear notes

---

## Notes & Caveats

### Obsidian Users — Required Heading Format

If you are using the export folder as an Obsidian vault, **every note must start with a `#` heading on the first line**.

```markdown
# My Note Title

Note body here...
```

If a note does not start with `# `, Obsidian cannot derive the filename from the title, which causes a bug where file links break and notes cannot be properly recognized. Bear normally uses the first line as the title — make sure it is formatted as a Markdown heading.

### Tag Handling

- Tags are reformatted on export so they do not render as `H1` headings in other editors
- If `hide_tags_in_comment_block = True` in the script, tags are wrapped in HTML comments (`<!-- #tag -->`) and restored transparently on import

### Conflict Resolution

- If the same note is edited in both Bear and an external editor between sync cycles, both versions are preserved in Bear
- The newer version gets a sync-conflict notice with a link to the original

### Ulysses External Folders

- Set the Ulysses external folder format to **Textbundle** and **Inline Links**
- The manual sort order you set in Ulysses is preserved across syncs unless the note title changes

### Large Vaults

- First export of a large Bear library may take a minute or two
- Subsequent syncs are fast because only changed notes are processed

### `sync_config.json` is excluded from git

The config file contains local paths and is listed in `.gitignore`. Do not commit it to version control.

---

## Project Structure

```
Bear-Markdown-Export/
├── bear_export_sync.py   # Core export/import engine
├── dual_sync.py          # Real-time sync daemon
├── sync_config.json      # Local config (not committed)
├── LICENSE
└── README.md
```

---

## Credits

- Original author: [rovest](https://github.com/rovest) ([@rorves](https://twitter.com/rorves))
- Modified by: [andymatuschak](https://github.com/andymatuschak) ([@andy_matuschak](https://twitter.com/andy_matuschak))
- Further refactored and maintained by: [desususu](https://github.com/desususu)

---

## License

MIT License — see [LICENSE](LICENSE) for details.
