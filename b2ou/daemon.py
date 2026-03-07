"""
Sync orchestration — run-once mode and FSEvents-driven daemon (v5).

Run-once mode
-------------
Called by launchd on a timer.  Checks the editing guard, runs one sync
cycle if safe, and exits.

Daemon mode (--daemon)
----------------------
A resident process kept alive by launchd.  Watches the filesystem via
``watchdog`` (FSEvents on macOS) and runs sync cycles only when something
actually changes.  Falls back to polling if ``watchdog`` is not installed.

Architecture (two-stage protection):

  Stage 1 — debounce (``daemon_debounce_seconds``, default 3 s)
    Batches rapid FSEvents into a single "something changed" signal.

  Stage 2 — editing guard (``write_quiet_seconds``, default 30 s)
    Identical to run-once mode.  Ensures Bear-DB changes sync in ~3–5 s
    while file edits wait until the user has stopped typing for 30 s.

Self-event suppression: after a sync completes a cooldown window
(debounce + 1 s, min 5 s) silently drops incoming FSEvents to absorb
the daemon's own write echoes.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from b2ou.config import SyncGateConfig
from b2ou.constants import CLOUD_JUNK_RE
from b2ou.db import bear_db_signature, db_is_quiet
from b2ou.guard import check_editing_guard
from b2ou.snapshot import (
    ChangeDetector,
    VaultSnapshot,
    build_snapshots,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional watchdog
# ---------------------------------------------------------------------------

try:
    from watchdog.observers import Observer as _WatchdogObserver  # type: ignore
    from watchdog.events import FileSystemEventHandler as _FSEventHandler  # type: ignore
    _HAS_WATCHDOG = True
except ImportError:
    _WatchdogObserver = None
    _FSEventHandler = object
    _HAS_WATCHDOG = False

_SENTINEL_FILES = frozenset({
    ".DS_Store", ".sync-time.log", ".export-time.log",
    ".b2ou_state.json", ".b2ou_state.json.tmp",
    ".b2ou.lock",
})
_IGNORE_DIRS = frozenset({"BearImages", ".obsidian", "__pycache__", ".git"})
_NOTE_EXTS = frozenset((".md", ".txt", ".markdown"))


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _load_state(state_file: Path) -> dict:
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict, state_file: Path) -> None:
    tmp = Path(str(state_file) + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, state_file)


# ---------------------------------------------------------------------------
# Lock file
# ---------------------------------------------------------------------------

def _acquire_lock(lock_file: Path) -> Optional[int]:
    """Return an open file descriptor for *lock_file* (exclusive), or None."""
    try:
        import fcntl
        fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except (ImportError, OSError):
        return None


def _release_lock(fd: Optional[int], lock_file: Path) -> None:
    if fd is None:
        return
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        try:
            lock_file.unlink()
        except OSError:
            pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Sync runner (calls CLI subprocesses)
# ---------------------------------------------------------------------------

def run_sync(
    cfg: SyncGateConfig,
    script_dir: Path,
    export_only: bool = False,
    files_changed: bool = False,
    pre_snapshots: Optional[dict[Path, VaultSnapshot]] = None,
) -> dict[Path, VaultSnapshot]:
    """
    Execute one complete sync cycle.

    Calls ``python -m b2ou export`` and (optionally) ``python -m b2ou import``
    as subprocesses, then returns post-sync VaultSnapshots.
    """
    python = cfg.python
    folder_md = _resolve(cfg.folder_md, script_dir)
    folder_tb = _resolve(cfg.folder_tb, script_dir)
    backup_md = _resolve(cfg.backup_md, script_dir)
    backup_tb = _resolve(cfg.backup_tb, script_dir)

    conflict_dir = cfg.resolved_conflict_dir
    if conflict_dir:
        conflict_dir = _resolve(conflict_dir, script_dir)
        conflict_dir.mkdir(parents=True, exist_ok=True)

    bear_settle = max(1, float(cfg.bear_settle_seconds))

    for d in (folder_md, folder_tb, backup_md, backup_tb):
        d.mkdir(parents=True, exist_ok=True)

    # Junk cleaning
    for folder in (folder_md, folder_tb):
        snap = pre_snapshots.get(folder) if pre_snapshots else None
        n = snap.clean_junk() if snap else _clean_junk(folder)
        if n:
            log.debug("Cleaned %d junk items from %s", n, folder)

    need_import = files_changed and not export_only
    plan = "import+export" if need_import else "export-only"
    t0 = time.monotonic()
    log.info("── Sync [%s] ──────────────────────────────", plan)

    # Pre-sync conflict hashes
    pre_md: dict = {}
    pre_tb: dict = {}
    if conflict_dir:
        snap_md = pre_snapshots.get(folder_md) if pre_snapshots else None
        snap_tb = pre_snapshots.get(folder_tb) if pre_snapshots else None
        from b2ou.snapshot import _hash_folder
        pre_md = snap_md.compute_hashes() if snap_md else _hash_folder(folder_md)
        pre_tb = snap_tb.compute_hashes() if snap_tb else _hash_folder(folder_tb)

    def _run(fmt: str, out: Path, backup: Path,
             skip_import: bool = False, skip_export: bool = False) -> None:
        cmd = [
            python, "-m", "b2ou",
            "export" if skip_import else "import",
            "--out", str(out),
            "--backup", str(backup),
            "--format", fmt,
        ]
        if skip_import:
            cmd = [python, "-m", "b2ou", "export",
                   "--out", str(out), "--backup", str(backup),
                   "--format", fmt]
        if skip_export:
            cmd = [python, "-m", "b2ou", "import",
                   "--out", str(out), "--backup", str(backup),
                   "--format", fmt]

        phase = "export" if skip_import else "import"
        tag = f"{fmt.upper()}-{phase}"
        t_start = time.monotonic()
        try:
            r = subprocess.run(
                cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            elapsed = time.monotonic() - t_start
            stderr = r.stderr.decode(errors="replace").strip()
            if r.returncode in (0, 1):
                log.info("[%s] exit=%d  %.1fs", tag, r.returncode, elapsed)
            else:
                log.error("[%s] exit=%d %.1fs  stderr: %s",
                          tag, r.returncode, elapsed, stderr[:500])
        except Exception as exc:
            log.error("[%s] %s", tag, exc)

    if need_import:
        _run("md", folder_md, backup_md, skip_export=True)
        _run("tb", folder_tb, backup_tb, skip_export=True)
        # Wait for Bear to process the import
        from b2ou.config import DEFAULT_BEAR_DB as _BEAR_DB_PATH
        pre_mod, _ = bear_db_signature(_BEAR_DB_PATH)
        deadline = time.time() + bear_settle
        while time.time() < deadline:
            time.sleep(0.5)
            cur_mod, _ = bear_db_signature(_BEAR_DB_PATH)
            if cur_mod > pre_mod:
                time.sleep(0.5)
                break

    # Export always runs
    _run("md", folder_md, backup_md, skip_import=True)
    _run("tb", folder_tb, backup_tb, skip_import=True)

    post_snapshots = build_snapshots([folder_md, folder_tb])

    if conflict_dir:
        from b2ou.snapshot import _hash_folder
        c = 0
        for folder, pre_h in ((folder_md, pre_md), (folder_tb, pre_tb)):
            snap = post_snapshots.get(folder)
            post_h = snap.compute_hashes() if snap else _hash_folder(folder)
            for rel, pre_val in pre_h.items():
                post_val = post_h.get(rel)
                if pre_val and post_val and pre_val != post_val:
                    log.warning("Export overwrote: %s", rel)
                    c += 1
        if c:
            log.warning("Overwrites: %d (backups in %s)", c, backup_md)

    log.info("── Sync complete  %.1fs ─────────────────────", time.monotonic() - t0)
    return post_snapshots


def _resolve(path: Path, base: Path) -> Path:
    if path.is_absolute():
        return path
    return (base / path).resolve()


def _clean_junk(folder: Path) -> int:
    removed = 0
    if not folder.is_dir():
        return 0
    for root, dirs, files in os.walk(folder, topdown=True):
        from b2ou.constants import CLOUD_JUNK_DIRS
        for d in list(dirs):
            if d in CLOUD_JUNK_DIRS:
                try:
                    import shutil
                    shutil.rmtree(Path(root) / d)
                    removed += 1
                except OSError:
                    pass
                dirs.remove(d)
        for fname in files:
            fpath = Path(root) / fname
            if CLOUD_JUNK_RE.match(fname):
                try:
                    fpath.unlink()
                    removed += 1
                except OSError:
                    pass
    return removed


# ---------------------------------------------------------------------------
# FSEvent handler
# ---------------------------------------------------------------------------

class _SyncEventHandler(_FSEventHandler):
    """Receives FSEvents and triggers debounced sync via SyncDaemon."""

    def __init__(self, daemon: "SyncDaemon", source_tag: str) -> None:
        super().__init__()
        self.daemon = daemon
        self.source_tag = source_tag

    def on_any_event(self, event) -> None:
        if getattr(event, "is_directory", False):
            return
        event_type = getattr(event, "event_type", "")
        if event_type in ("opened", "closed", "closed_no_write"):
            return
        path = getattr(event, "src_path", "")
        if not path:
            return
        basename = os.path.basename(path)
        if basename in _SENTINEL_FILES or CLOUD_JUNK_RE.match(basename):
            return
        parts = path.split(os.sep)
        if any(p in _IGNORE_DIRS for p in parts):
            return

        if self.source_tag == "bear_db":
            if not basename.startswith("database.sqlite"):
                return
        else:
            if not (
                any(basename.endswith(ext) for ext in _NOTE_EXTS)
                or basename == "text.md"
            ):
                return

        self.daemon.schedule_sync(self.source_tag)


# ---------------------------------------------------------------------------
# SyncDaemon
# ---------------------------------------------------------------------------

class SyncDaemon:
    """
    Resident daemon: watches for filesystem changes and runs sync cycles.

    Uses ``watchdog`` (FSEvents) for near-instant detection with debouncing.
    Falls back to polling when ``watchdog`` is unavailable.
    """

    def __init__(
        self,
        cfg: SyncGateConfig,
        script_dir: Path,
        export_only: bool = False,
        state_file: Optional[Path] = None,
        lock_file: Optional[Path] = None,
    ) -> None:
        self.cfg = cfg
        self.script_dir = script_dir
        self.export_only = export_only
        self._state_file = state_file or (script_dir / ".b2ou_state.json")
        self._lock_file = lock_file or (script_dir / ".b2ou.lock")
        self._lock_fd: Optional[int] = None

        self.state = _load_state(self._state_file)

        self.folder_md = _resolve(cfg.folder_md, script_dir)
        self.folder_tb = _resolve(cfg.folder_tb, script_dir)
        self.folders = [self.folder_md, self.folder_tb]

        from b2ou.config import DEFAULT_BEAR_DB as _BEAR_DB
        self._bear_db = _BEAR_DB
        self._bear_db_dir = self._bear_db.parent

        self.debounce_s = max(1.0, float(cfg.daemon_debounce_seconds))
        self.write_quiet_s = max(5.0, float(cfg.write_quiet_seconds))
        self.retry_s = max(1.0, float(cfg.daemon_retry_seconds))
        self.min_interval_s = max(5.0, float(cfg.sync_interval_seconds))
        self.db_settle_s = min(5.0, float(cfg.bear_settle_seconds))
        self._cooldown_s = max(self.debounce_s + 1.0, 5.0)
        self._cooldown_until = 0.0

        self._timer: Optional[threading.Timer] = None
        self._timer_lock = threading.Lock()
        self._syncing = False
        self._sync_requested = threading.Event()
        self._stop = threading.Event()
        self._observers: list = []
        self._cycle_count = 0

        h = self.state.get("hashes", {})
        try:
            self._last_bear_sig = (
                float(h.get("bear_max_mod", 0.0) or 0.0),
                int(h.get("bear_note_count", -1) or -1),
            )
        except (TypeError, ValueError):
            self._last_bear_sig = (0.0, -1)

    # ── public API ──────────────────────────────────────────────────────

    def run(self) -> None:
        """Block until SIGTERM / SIGINT."""
        self._lock_fd = _acquire_lock(self._lock_file)
        if self._lock_fd is None:
            log.warning(
                "Could not acquire lock (%s) — another sync process is active.",
                self._lock_file,
            )
            return

        log.info(
            "══ Daemon starting ══  debounce=%.1fs  write_quiet=%.0fs  "
            "min_interval=%.0fs  cooldown=%.1fs  watchdog=%s",
            self.debounce_s, self.write_quiet_s,
            self.min_interval_s, self._cooldown_s,
            "yes" if _HAS_WATCHDOG else "NO (polling)",
        )

        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

        if _HAS_WATCHDOG:
            self._start_observers()
            self._schedule(0.5)  # immediate startup cycle

            while not self._stop.is_set():
                self._sync_requested.wait(timeout=60.0)
                if self._stop.is_set():
                    break
                if not self._sync_requested.is_set():
                    continue
                self._sync_requested.clear()
                try:
                    self._syncing = True
                    self._run_cycle()
                except Exception:
                    log.exception("Sync cycle failed unexpectedly")
                finally:
                    self._syncing = False
        else:
            log.warning(
                "watchdog not installed — falling back to %.0fs polling.  "
                "Install: pip install watchdog",
                float(self.cfg.sync_interval_seconds),
            )
            self._poll_loop()

        self._cleanup()
        log.info("══ Daemon stopped ══  (%d cycles completed)", self._cycle_count)

    def schedule_sync(self, source_tag: str) -> None:
        """Called by event handlers; resets the debounce timer."""
        if self._syncing:
            return
        if time.time() < self._cooldown_until:
            return
        if source_tag == "bear_db" and self._should_skip_bear_event():
            return
        self._schedule(self.debounce_s)
        log.debug("Debounce reset → %.1fs  (trigger: %s)", self.debounce_s, source_tag)

    # ── internal scheduling ─────────────────────────────────────────────

    def _schedule(self, delay: float) -> None:
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(delay, self._on_timer)
            self._timer.daemon = True
            self._timer.start()

    def _on_timer(self) -> None:
        if not self._stop.is_set():
            self._sync_requested.set()

    def _schedule_retry(self, reason: str) -> None:
        log.debug("Guard blocked (%s) — retry in %.0fs", reason, self.retry_s)
        self._schedule(self.retry_s)

    # ── core sync cycle ─────────────────────────────────────────────────

    def _run_cycle(self) -> None:
        self._cycle_count += 1

        # Guard 1: minimum interval
        elapsed = time.time() - self.state.get("last_sync_end", 0)
        if elapsed < self.min_interval_s:
            remaining = self.min_interval_s - elapsed
            self._schedule(remaining + 1)
            return

        snapshots = build_snapshots(self.folders)
        last_sync_end = self.state.get("last_sync_end", 0)

        # Guard 2a: editing guard — first check
        reason = check_editing_guard(
            self.folders, self.write_quiet_s, last_sync_end,
            log_all=True, snapshots=snapshots,
        )
        if reason:
            self._schedule_retry(reason)
            return

        # Guard 3: DB settle
        waited = 0.0
        max_wait = self.db_settle_s * 3
        while not db_is_quiet(self._bear_db, self.db_settle_s) and waited < max_wait:
            if self._stop.is_set():
                return
            time.sleep(0.5)
            waited += 0.5
        if waited:
            log.debug("DB settled after %.1fs", waited)

        # Guard 2b: editing guard — recheck after DB settle
        for snap in snapshots.values():
            snap.refresh_mtimes()
        reason = check_editing_guard(
            self.folders, self.write_quiet_s, last_sync_end,
            log_all=True, snapshots=snapshots,
        )
        if reason:
            self._schedule_retry(reason)
            return

        # Guard 4: change detection
        detector = ChangeDetector(
            self.state, self.folder_md, self.folder_tb,
            self._bear_db, snapshots=snapshots,
        )
        bear_changed = detector.bear_changed()
        md_changed, tb_changed = detector.files_changed()
        files_changed = md_changed or tb_changed

        if not bear_changed and not files_changed:
            log.debug("No real changes.")
            self.state["last_sync"] = time.time()
            _save_state(self.state, self._state_file)
            return

        log.info("Changes: bear=%s  md=%s  tb=%s", bear_changed, md_changed, tb_changed)

        # Guard 2c: final check before sync
        for snap in snapshots.values():
            snap.refresh_mtimes()
        reason = check_editing_guard(
            self.folders, self.write_quiet_s, last_sync_end,
            log_all=True, snapshots=snapshots,
        )
        if reason:
            self._schedule_retry(reason)
            return

        # Snapshot pre-sync hashes for post-export verification
        old_bear_mod = detector._hash_state.get("bear_max_mod", 0.0)
        old_bear_count = detector._hash_state.get("bear_note_count", -1)
        old_md = {k: tuple(v) if isinstance(v, list) else v
                  for k, v in detector._hash_state.get("md_hashes", {}).items()}
        old_tb = {k: tuple(v) if isinstance(v, list) else v
                  for k, v in detector._hash_state.get("tb_hashes", {}).items()}

        # ── SYNC ────────────────────────────────────────────────────────
        post_snaps = run_sync(
            self.cfg, self.script_dir,
            export_only=self.export_only,
            files_changed=files_changed,
            pre_snapshots=snapshots,
        )

        now = time.time()
        self.state["last_sync"] = now
        self.state["last_sync_end"] = now
        self.state.pop("last_editor_left", None)
        detector.snapshot(self.state, post_snapshots=post_snaps)
        self._last_bear_sig = bear_db_signature(self._bear_db)

        # Post-export verification: retry if Bear changed but no files moved
        if bear_changed and not files_changed:
            h = self.state.get("hashes", {})
            if h.get("md_hashes") == old_md and h.get("tb_hashes") == old_tb:
                retry = self.state.get("bear_export_retry", 0) + 1
                if retry <= 5:
                    log.warning(
                        "Bear changed but export produced no file changes "
                        "— will retry (%d/5)", retry
                    )
                    h["bear_max_mod"] = old_bear_mod
                    h["bear_note_count"] = old_bear_count
                    self.state["bear_export_retry"] = retry
                else:
                    log.warning(
                        "Bear changed but export produced no file changes "
                        "after 5 retries — accepting"
                    )
                    self.state.pop("bear_export_retry", None)
            else:
                self.state.pop("bear_export_retry", None)
        else:
            self.state.pop("bear_export_retry", None)

        _save_state(self.state, self._state_file)
        self._cooldown_until = time.time() + self._cooldown_s
        log.debug("Post-sync cooldown: %.1fs", self._cooldown_s)

    # ── Bear event filtering ─────────────────────────────────────────────

    def _should_skip_bear_event(self) -> bool:
        """True when Bear DB activity did not change note content."""
        sig = bear_db_signature(self._bear_db)
        if sig[0] <= 0 and sig[1] < 0:
            return False  # fail open
        if sig == self._last_bear_sig:
            return True
        self._last_bear_sig = sig
        return False

    # ── observers ───────────────────────────────────────────────────────

    def _start_observers(self) -> None:
        specs = [
            (self._bear_db_dir, "bear_db",   False),
            (self.folder_md,    "folder_md", True),
            (self.folder_tb,    "folder_tb", True),
        ]
        for path, tag, recursive in specs:
            if not path.is_dir():
                log.warning("Watch path does not exist (skipped): %s", path)
                continue
            handler = _SyncEventHandler(self, tag)
            obs = _WatchdogObserver()
            obs.schedule(handler, str(path), recursive=recursive)
            obs.daemon = True
            obs.start()
            self._observers.append(obs)
            log.info(
                "  watching: %s  (%s, %s)",
                path, tag, "recursive" if recursive else "top-level only",
            )

    # ── polling fallback ────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        interval = max(10, int(self.cfg.sync_interval_seconds))
        while not self._stop.is_set():
            try:
                self._syncing = True
                self._run_cycle()
            except Exception:
                log.exception("Sync cycle failed")
            finally:
                self._syncing = False
            self._stop.wait(interval)

    # ── lifecycle ───────────────────────────────────────────────────────

    def _on_signal(self, signum, _frame) -> None:
        name = signal.Signals(signum).name
        log.info("Received %s — shutting down.", name)
        self._stop.set()
        self._sync_requested.set()
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()

    def _cleanup(self) -> None:
        for obs in self._observers:
            obs.stop()
        for obs in self._observers:
            obs.join(timeout=5)
        _save_state(self.state, self._state_file)
        _release_lock(self._lock_fd, self._lock_file)


# ---------------------------------------------------------------------------
# Run-once mode
# ---------------------------------------------------------------------------

def run_once(
    cfg: SyncGateConfig,
    script_dir: Path,
    force: bool = False,
    export_only: bool = False,
    dry_run: bool = False,
) -> int:
    """
    Check guards and run one sync cycle.

    Returns 0 (no changes / guard blocked) or 1 (sync ran).
    """
    state_file = script_dir / ".b2ou_state.json"
    lock_file = script_dir / ".b2ou.lock"

    lock_fd = _acquire_lock(lock_file)
    if lock_fd is None:
        log.warning(
            "Could not acquire lock (%s) — another sync process is active.",
            lock_file,
        )
        return 0

    state = _load_state(state_file)

    folder_md = _resolve(cfg.folder_md, script_dir)
    folder_tb = _resolve(cfg.folder_tb, script_dir)
    folders = [folder_md, folder_tb]
    snapshots = build_snapshots(folders)

    from b2ou.config import DEFAULT_BEAR_DB as _BEAR_DB
    bear_db = _BEAR_DB

    last_sync_end = state.get("last_sync_end", 0)

    if not force:
        reason = check_editing_guard(
            folders,
            float(cfg.write_quiet_seconds),
            last_sync_end,
            snapshots=snapshots,
        )
        if reason:
            log.info("Guard blocked: %s", reason)
            _release_lock(lock_fd, lock_file)
            return 0

    detector = ChangeDetector(
        state, folder_md, folder_tb, bear_db, snapshots=snapshots
    )
    bear_changed = detector.bear_changed()
    md_changed, tb_changed = detector.files_changed()
    files_changed = md_changed or tb_changed

    if not force and not bear_changed and not files_changed:
        log.debug("No changes detected.")
        _release_lock(lock_fd, lock_file)
        return 0

    log.info("Changes: bear=%s  md=%s  tb=%s  [%s]",
             bear_changed, md_changed, tb_changed,
             "FORCE" if force else "normal")

    if dry_run:
        log.info("[dry-run] would sync — exiting without action.")
        _release_lock(lock_fd, lock_file)
        return 1

    post_snaps = run_sync(
        cfg, script_dir,
        export_only=export_only,
        files_changed=files_changed,
        pre_snapshots=snapshots,
    )

    now = time.time()
    state["last_sync"] = now
    state["last_sync_end"] = now
    detector.snapshot(state, post_snapshots=post_snaps)
    _save_state(state, state_file)
    _release_lock(lock_fd, lock_file)
    return 1
