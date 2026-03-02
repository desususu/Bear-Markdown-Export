# dual_sync.py  —  Bear Export Sync daemon  (v2)
# Coordinates dual-format (MD + Textbundle) sync with bear_export_sync.py
#
# New in v2
# ──────────────────────────────────────────────────────────────────────────
#  • FSEvents-triggered sync  — watches Bear's live SQLite DB file for
#    changes and fires an export cycle immediately (latency: ~1-2 s after
#    you stop typing in Bear, vs waiting a full polling interval).
#
#  • File-write quiesce guard  — uses watchdog to monitor the md / tb
#    output folders. While an external editor is actively writing files
#    the sync timer is paused; it resumes only after the folder has been
#    quiet for WRITE_QUIET_SECONDS (default 5 s). This prevents importing
#    a half-written file back into Bear.
#
#  • Interval sync is kept as a safety net for changes that don't produce
#    FS events (e.g. network-mounted folders, some editors).
#
# Run modes
# ──────────────────────────────────────────────────────────────────────────
#   python3 dual_sync.py            — start the background daemon
#   python3 dual_sync.py --once     — one cycle and exit (cron / launchd)
#   python3 dual_sync.py --trigger  — ask running daemon to sync immediately
#   python3 dual_sync.py --status   — show daemon status + last log lines
#
# Dependencies
# ──────────────────────────────────────────────────────────────────────────
#   pip install watchdog          (FSEvents observer, macOS native)

import argparse
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time
import threading
from datetime import datetime, time as dt_time

# watchdog — graceful degradation if not installed
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _WATCHDOG_AVAILABLE = True
except ImportError:
    _WATCHDOG_AVAILABLE = False

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, 'sync_config.json')
LOG_FILE    = os.path.join(SCRIPT_DIR, 'dual_sync.log')
PID_FILE    = os.path.join(SCRIPT_DIR, 'dual_sync.pid')

# Bear's live SQLite database (triggers fast export when Bear saves a note)
BEAR_DB_PATH = os.path.expanduser(
    "~/Library/Group Containers/9K33E3U3T4.net.shinyfrog.bear"
    "/Application Data/database.sqlite"
)
# Companion WAL file — Bear writes here first; watching it fires earlier
BEAR_DB_WAL  = BEAR_DB_PATH + "-wal"

DEFAULT_CONFIG: dict = {
    "script_path":            "./bear_export_sync.py",
    "folder_md":              "./Export/MD_Export",
    "folder_tb":              "./Export/TB_Export",
    "backup_md":              "./Backup/MD_Backup",
    "backup_tb":              "./Backup/TB_Backup",
    "sync_interval_seconds":  180,
    "sync_on_startup":        False,
    # How many seconds of folder quiet before sync is allowed (file-write guard)
    "write_quiet_seconds":    5,
    # If True, a Bear DB change triggers an immediate export cycle
    "fast_trigger_on_db_change": True,
    "sync_window": {
        "start_hour":   6,  "start_minute":  0,
        "end_hour":    23,  "end_minute":   20
    }
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _setup_logging() -> logging.Logger:
    log = logging.getLogger("dual_sync")
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)
    return log

log = _setup_logging()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_CONFIG, f, indent=4, ensure_ascii=False)
        log.warning("Config not found — created default at %s", CONFIG_FILE)
        log.warning("Please review paths and restart.")
        sys.exit(1)
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        cfg = json.load(f)
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    return cfg


def resolve_path(raw: str) -> str:
    return raw if os.path.isabs(raw) else os.path.normpath(os.path.join(SCRIPT_DIR, raw))


def _parse_config(cfg: dict) -> dict:
    win = cfg.get("sync_window", DEFAULT_CONFIG["sync_window"])
    return {
        "script_path":      resolve_path(cfg["script_path"]),
        "folder_md":        resolve_path(cfg["folder_md"]),
        "folder_tb":        resolve_path(cfg["folder_tb"]),
        "backup_md":        resolve_path(cfg["backup_md"]),
        "backup_tb":        resolve_path(cfg["backup_tb"]),
        "interval":         max(30, int(cfg["sync_interval_seconds"])),
        "on_startup":       bool(cfg["sync_on_startup"]),
        "quiet_seconds":    float(cfg.get("write_quiet_seconds", 5)),
        "fast_trigger":     bool(cfg.get("fast_trigger_on_db_change", True)),
        "start_time":       dt_time(win["start_hour"],  win["start_minute"]),
        "end_time":         dt_time(win["end_hour"],    win["end_minute"]),
    }

# ---------------------------------------------------------------------------
# File-write quiesce watcher
# ---------------------------------------------------------------------------
class _FolderWriteWatcher(FileSystemEventHandler if _WATCHDOG_AVAILABLE else object):
    """
    Watches one or more directories. Records the timestamp of the most recent
    file modification so the daemon can tell whether writing is still active.

    Thread-safe: last_write_time is read from the main thread, written from
    the watchdog observer thread.
    """

    def __init__(self, quiet_seconds: float) -> None:
        if _WATCHDOG_AVAILABLE:
            super().__init__()
        self._lock            = threading.Lock()
        self._last_write_time = 0.0
        self.quiet_seconds    = quiet_seconds
        self._observer        = None

    # watchdog callback (runs on observer thread)
    def on_modified(self, event):
        if not event.is_directory:
            with self._lock:
                self._last_write_time = time.monotonic()
                log.debug("File activity detected: %s", event.src_path)

    on_created = on_modified   # treat new files the same way

    def start(self, *dirs: str) -> None:
        if not _WATCHDOG_AVAILABLE:
            log.warning(
                "watchdog is not installed — file-write quiesce disabled.\n"
                "  Install with:  pip install watchdog"
            )
            return
        self._observer = Observer()
        for d in dirs:
            if os.path.isdir(d):
                self._observer.schedule(self, d, recursive=True)
                log.debug("Watching folder for writes: %s", d)
            else:
                log.warning("Watch folder does not exist (skipping): %s", d)
        self._observer.start()

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join()

    def is_quiet(self) -> bool:
        """Return True if no file activity for at least quiet_seconds."""
        if not _WATCHDOG_AVAILABLE:
            return True          # can't tell — assume safe to sync
        with self._lock:
            age = time.monotonic() - self._last_write_time
        return age >= self.quiet_seconds

    def seconds_since_last_write(self) -> float:
        with self._lock:
            return time.monotonic() - self._last_write_time


# ---------------------------------------------------------------------------
# Bear DB change watcher (fast-trigger)
# ---------------------------------------------------------------------------
class _BearDbWatcher(FileSystemEventHandler if _WATCHDOG_AVAILABLE else object):
    """
    Watches Bear's SQLite WAL / DB file. When Bear commits a note the WAL
    file is modified; we set a flag so the daemon can start an export cycle
    immediately rather than waiting for the next polling interval.
    """

    def __init__(self) -> None:
        if _WATCHDOG_AVAILABLE:
            super().__init__()
        self._lock    = threading.Lock()
        self._dirty   = False
        self._observer = None

    def on_modified(self, event):
        if not event.is_directory and (
            event.src_path.endswith(".sqlite") or
            event.src_path.endswith(".sqlite-wal")
        ):
            with self._lock:
                if not self._dirty:
                    log.debug("Bear DB change detected — queuing fast export")
                self._dirty = True

    on_created = on_modified

    def start(self) -> None:
        if not _WATCHDOG_AVAILABLE:
            return
        db_dir = os.path.dirname(BEAR_DB_PATH)
        if not os.path.isdir(db_dir):
            log.warning("Bear DB directory not found — fast-trigger disabled: %s", db_dir)
            return
        self._observer = Observer()
        self._observer.schedule(self, db_dir, recursive=False)
        self._observer.start()
        log.debug("Watching Bear DB for changes: %s", db_dir)

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join()

    def consume(self) -> bool:
        """Return True (and clear the flag) if a DB change was detected."""
        with self._lock:
            dirty, self._dirty = self._dirty, False
        return dirty


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_EDITOR_KEYWORDS = ("ulysses", "obsidian", "typora", "bear")


def is_in_sync_window(cfg: dict) -> bool:
    return cfg["start_time"] <= datetime.now().time() < cfg["end_time"]


def is_editor_active() -> bool:
    """
    Return True if a watched editor is the frontmost app.
    Matched by Bundle ID to avoid false positives from browser tab titles.
    Defaults to False on any failure (don't block sync).
    """
    script = ('tell application "System Events" to get bundle identifier '
              'of first process whose frontmost is true')
    try:
        r = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True, text=True, timeout=3
        )
        active = r.stdout.strip().lower()
        return bool(active) and any(kw in active for kw in _EDITOR_KEYWORDS)
    except Exception:
        return False


def _run(cmd: list, label: str) -> int:
    """
    Run cmd, log timing and exit code.
    Returns: 0 (no changes), 1 (changes processed), -1 (error).
    """
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, check=False, stderr=subprocess.PIPE)
        elapsed = time.monotonic() - t0
        if r.returncode not in (0, 1) and r.stderr:
            log.warning("[%s] stderr (exit=%d, %.1fs): %s",
                        label, r.returncode, elapsed,
                        r.stderr.decode(errors='replace').strip())
        else:
            log.info("[%s] exit=%d  %.1fs", label, r.returncode, elapsed)
        return r.returncode
    except FileNotFoundError:
        log.error("Script not found: %s", cmd[1] if len(cmd) > 1 else cmd)
        return -1
    except Exception as exc:
        log.error("[%s] Unexpected error: %s", label, exc)
        return -1


def _sync_cmd(cfg: dict, fmt: str, out: str, backup: str,
              skip_import: bool = False, skip_export: bool = False) -> list:
    cmd = [sys.executable, cfg["script_path"],
           "--out", out, "--backup", backup, "--format", fmt]
    if skip_import:
        cmd.append("--skipImport")
    if skip_export:
        cmd.append("--skipExport")
    return cmd

# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------
def execute_sync_cycle(cfg: dict, export_only: bool = False) -> None:
    """
    Full sync cycle with two strictly-sequential phases.

    WHY SEQUENTIAL:
      bear_export_sync.py uses a shared temp directory (~/Temp/BearExportTemp).
      Running MD and TB concurrently causes them to overwrite each other's
      temp files. Sequential execution is required for correctness; it's still
      fast because bear_export_sync.py fast-exits when nothing has changed.

    export_only=True:
      Skips the import phase — used for fast-trigger cycles that were fired
      because Bear's DB changed (Bear is the authoritative source; no need
      to re-import from the folders first).
    """
    for d in (cfg["folder_md"], cfg["folder_tb"],
              cfg["backup_md"], cfg["backup_tb"]):
        os.makedirs(d, exist_ok=True)

    t0 = time.monotonic()
    label = "export-only" if export_only else "full"
    log.info("── Sync cycle start [%s] ─────────────────────────────────────", label)

    if not export_only:
        # Phase 1 — import from folders → Bear (sequential)
        _run(_sync_cmd(cfg, "md", cfg["folder_md"], cfg["backup_md"], skip_export=True), "MD-import")
        _run(_sync_cmd(cfg, "tb", cfg["folder_tb"], cfg["backup_tb"], skip_export=True), "TB-import")

    # Phase 2 — export from Bear → folders (sequential)
    _run(_sync_cmd(cfg, "md", cfg["folder_md"], cfg["backup_md"], skip_import=True), "MD-export")
    _run(_sync_cmd(cfg, "tb", cfg["folder_tb"], cfg["backup_tb"], skip_import=True), "TB-export")

    log.info("── Sync cycle complete  total=%.1fs ───────────────────────────",
             time.monotonic() - t0)

# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------
_shutdown     = False
_trigger_now  = False   # set by SIGUSR1 — fires an immediate sync cycle


def _handle_signal(signum, _frame) -> None:
    global _shutdown, _trigger_now
    name = signal.Signals(signum).name
    if signum == signal.SIGUSR1:
        log.info("Received signal %s — immediate sync cycle requested.", name)
        _trigger_now = True
        return
    log.info("Received signal %s — shutting down after current cycle.", name)
    _shutdown = True


def _seconds_until_window_opens(cfg: dict) -> float:
    from datetime import timedelta
    now    = datetime.now()
    target = now.replace(hour=cfg["start_time"].hour,
                         minute=cfg["start_time"].minute,
                         second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return min(max((target - now).total_seconds(), 10), 600)


def _interruptible_sleep(seconds: float) -> None:
    """Sleep in short chunks so signals and DB triggers wake the loop quickly."""
    deadline = time.monotonic() + seconds
    while not _shutdown:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(remaining, 1.0))   # 1 s chunks — responsive to fast-trigger


def run_daemon(cfg: dict) -> None:
    global _shutdown, _trigger_now
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT,  _handle_signal)
    signal.signal(signal.SIGUSR1, _handle_signal)

    # --- write PID file so --trigger / --status can find us ------------------
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))

    # --- start file watchers --------------------------------------------------
    folder_watcher = _FolderWriteWatcher(quiet_seconds=cfg["quiet_seconds"])
    folder_watcher.start(cfg["folder_md"], cfg["folder_tb"])

    db_watcher = _BearDbWatcher()
    if cfg["fast_trigger"]:
        db_watcher.start()

    # --- announce startup -----------------------------------------------------
    watchdog_status = "enabled" if _WATCHDOG_AVAILABLE else "disabled (install watchdog)"
    log.info(
        "Daemon started  pid=%d  |  interval=%ds  |  window=%s–%s  |  "
        "fast-trigger=%s  |  write-quiesce=%.0fs  |  watchdog=%s",
        os.getpid(),
        cfg["interval"],
        cfg["start_time"].strftime("%H:%M"),
        cfg["end_time"].strftime("%H:%M"),
        "on" if cfg["fast_trigger"] else "off",
        cfg["quiet_seconds"],
        watchdog_status,
    )

    last_sync = 0.0 if cfg["on_startup"] else time.monotonic()

    try:
        while not _shutdown:
            # Hot-reload config each outer loop tick
            try:
                cfg = _parse_config(load_config())
                folder_watcher.quiet_seconds = cfg["quiet_seconds"]
            except Exception as exc:
                log.warning("Config reload failed (%s) — keeping previous config.", exc)

            # ── Manual trigger (SIGUSR1 / --trigger) ──────────────────────
            # Bypasses all guards: sync window, editor check, quiesce, interval.
            if _trigger_now:
                _trigger_now = False
                log.info("Manual trigger: running immediate full sync cycle")
                execute_sync_cycle(cfg)
                last_sync = time.monotonic()
                continue

            # ── Sync window check ──────────────────────────────────────────
            if not is_in_sync_window(cfg):
                sleep_s = _seconds_until_window_opens(cfg)
                log.debug("Outside sync window — sleeping %.0fs", sleep_s)
                last_sync = time.monotonic()
                _interruptible_sleep(sleep_s)
                continue

            # ── File-write quiesce guard ───────────────────────────────────
            # If an external editor is actively writing to the export folders,
            # pause the timer and retry after a short wait. This prevents
            # importing a partially-written file back into Bear.
            if not folder_watcher.is_quiet():
                age = folder_watcher.seconds_since_last_write()
                remaining = cfg["quiet_seconds"] - age
                log.info(
                    "Folder write activity detected (%.1fs ago) — "
                    "pausing sync timer, retrying in %.0fs",
                    age, max(remaining, 1)
                )
                _interruptible_sleep(max(remaining, 1))
                continue

            # ── Fast-trigger: Bear DB changed → export immediately ─────────
            if cfg["fast_trigger"] and db_watcher.consume():
                if is_editor_active():
                    log.info("Bear DB changed but editor active — will catch on next interval")
                else:
                    log.info("Fast-trigger: Bear DB changed — running export cycle")
                    execute_sync_cycle(cfg, export_only=True)
                    last_sync = time.monotonic()
                    continue

            # ── Interval-based full sync ───────────────────────────────────
            elapsed = time.monotonic() - last_sync
            if elapsed < cfg["interval"]:
                _interruptible_sleep(min(cfg["interval"] - elapsed, 1.0))
                continue

            if is_editor_active():
                log.info("Interval elapsed but editor is active — retrying in 15s")
                _interruptible_sleep(15)
                continue

            execute_sync_cycle(cfg)
            last_sync = time.monotonic()

    finally:
        folder_watcher.stop()
        db_watcher.stop()
        # Clean up PID file
        try:
            os.remove(PID_FILE)
        except OSError:
            pass

    log.info("Daemon stopped.")

# ---------------------------------------------------------------------------
# PID file helpers (used by --trigger and --status)
# ---------------------------------------------------------------------------
def _read_pid() -> int | None:
    """Return the PID from the PID file, or None if missing / stale."""
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        # Verify the process is actually running
        os.kill(pid, 0)
        return pid
    except (FileNotFoundError, ValueError):
        return None
    except ProcessLookupError:
        # PID file exists but process is gone — clean it up
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
        return None
    except PermissionError:
        # Process exists but we can't signal it (shouldn't happen for own procs)
        return None


def cmd_trigger() -> None:
    """Send SIGUSR1 to the running daemon to fire an immediate sync cycle."""
    pid = _read_pid()
    if pid is None:
        print("No running daemon found (is dual_sync.py started without --once?).")
        sys.exit(1)
    os.kill(pid, signal.SIGUSR1)
    print(f"Sent immediate-sync request to daemon (pid={pid}).")
    print(f"Check {LOG_FILE} for results.")


def cmd_status() -> None:
    """Print a short human-readable status of the daemon."""
    pid = _read_pid()
    if pid is None:
        print("Daemon: NOT running")
        # Show last few log lines if the log file exists
        if os.path.exists(LOG_FILE):
            try:
                with open(LOG_FILE, 'rb') as f:
                    # Read last ~4 KB for a quick tail
                    f.seek(0, 2)
                    size = f.tell()
                    f.seek(max(0, size - 4096))
                    tail = f.read().decode(errors='replace')
                lines = [l for l in tail.splitlines() if l.strip()][-6:]
                print("\nLast log entries:")
                for line in lines:
                    print(" ", line)
            except OSError:
                pass
        sys.exit(1)
    else:
        print(f"Daemon: RUNNING  (pid={pid})")
        print(f"PID file : {PID_FILE}")
        print(f"Log file : {LOG_FILE}")
        print()
        print("To trigger an immediate sync:  python3 dual_sync.py --trigger")
        print("To stop the daemon:            kill", pid)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description="Bear Export Sync daemon v2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Run modes:
  (no flag)    Start the background daemon (loops forever)
  --once       Run one full sync cycle and exit  (for cron / launchd)
  --trigger    Send an immediate sync request to the running daemon
  --status     Show whether the daemon is running and its last log lines
        """.strip()
    )
    ap.add_argument("--once",    action="store_true",
                    help="Run one sync cycle and exit")
    ap.add_argument("--trigger", action="store_true",
                    help="Ask the running daemon to sync immediately (bypasses all guards)")
    ap.add_argument("--status",  action="store_true",
                    help="Show daemon status and last log lines")
    args = ap.parse_args()

    if args.trigger:
        cmd_trigger()
        sys.exit(0)

    if args.status:
        cmd_status()
        # exit code already set inside cmd_status
        sys.exit(0)

    cfg = _parse_config(load_config())

    if args.once:
        log.info("--once mode: running one cycle.")
        execute_sync_cycle(cfg)
        log.info("--once mode: done.")
        sys.exit(0)

    run_daemon(cfg)
