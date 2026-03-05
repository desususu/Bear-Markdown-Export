"""
Three-layer editing guard — prevents syncing while the user is editing.

Layer 1 — lsof (file-open check)
    Are note files held open by any editor process?

Layer 2 — write-settle (mtime)
    Was any note file modified within the last N seconds?
    In daemon mode the debounce timer handles this role for Bear-DB events.

Layer 3 — frontmost app (NSWorkspace native API, lsappinfo / osascript fallback)
    Is a known editor application currently in the foreground?

The guard is intentionally checked cheapest-first (Layer 3 < Layer 2 < Layer 1)
to avoid running the expensive ``lsof +D`` scan when a quick API call already
detects editing activity.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from b2ou.constants import (
    EDITOR_BUNDLE_IDS_LOWER,
    EDITOR_KEYWORDS,
    SENTINEL_FILES,
    SYSTEM_PROCESS_PREFIXES,
)
from b2ou.snapshot import VaultSnapshot

log = logging.getLogger(__name__)

_NOTE_EXTS = frozenset((".md", ".txt", ".markdown"))


def _is_note_file(fname: str) -> bool:
    return (
        any(fname.endswith(ext) for ext in _NOTE_EXTS)
        and fname not in SENTINEL_FILES
    )


def _is_system_process(name: str) -> bool:
    return any(name.startswith(p) for p in SYSTEM_PROCESS_PREFIXES)


# ---------------------------------------------------------------------------
# GuardResult
# ---------------------------------------------------------------------------

@dataclass
class GuardResult:
    blocked: bool
    layer: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.layer}] {self.detail}" if self.blocked else ""


# ---------------------------------------------------------------------------
# Layer 1: lsof file-open check
# ---------------------------------------------------------------------------

def check_lsof(folders: list[Path]) -> GuardResult:
    """
    Check whether any editor process holds note files open in *folders*.

    Uses ``lsof +D <dir>`` which recursively scans the directory for open
    file descriptors.  Returns a blocked ``GuardResult`` with the offending
    process name, or a clear result.
    """
    lsof_args = ["lsof", "-F", "pcn"]
    valid_folders = [f for f in folders if f.is_dir()]
    for folder in valid_folders:
        lsof_args.extend(["+D", str(folder)])

    if len(lsof_args) == 3:
        return GuardResult(blocked=False, layer="lsof", detail="no valid folders")

    try:
        r = subprocess.run(
            lsof_args, capture_output=True, text=True, timeout=8
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return GuardResult(blocked=False, layer="lsof", detail="lsof unavailable")

    current_cmd = ""
    for line in r.stdout.splitlines():
        if line.startswith("c"):
            current_cmd = line[1:]
        elif line.startswith("n") and current_cmd:
            fname = os.path.basename(line[1:])
            if _is_note_file(fname) and not _is_system_process(current_cmd):
                return GuardResult(
                    blocked=True,
                    layer="lsof",
                    detail=f"process '{current_cmd}'",
                )

    return GuardResult(blocked=False, layer="lsof", detail="no editor processes")


# ---------------------------------------------------------------------------
# Layer 2: write-settle (mtime)
# ---------------------------------------------------------------------------

def check_write_settle(
    folders: list[Path],
    quiet_seconds: float,
    last_sync_end: float,
    snapshots: Optional[dict[Path, VaultSnapshot]] = None,
) -> GuardResult:
    """
    Check whether any note file was modified within the last *quiet_seconds*.

    Writes that occurred at approximately the same time as the last sync
    (within 2 s) are treated as our own output and ignored.

    If *snapshots* is provided the pre-computed ``newest_mtime`` is used
    instead of walking the directories again.
    """
    now = time.time()
    folder_ages: dict[Path, float] = {}

    for folder in folders:
        if snapshots and folder in snapshots:
            newest = snapshots[folder].newest_mtime
        else:
            newest = _newest_note_mtime(folder)

        if newest > 0:
            if last_sync_end > 0 and abs(newest - last_sync_end) < 2.0:
                folder_ages[folder] = -1.0  # own output — ignore
            else:
                folder_ages[folder] = now - newest

    unsettled = {
        f: age for f, age in folder_ages.items() if 0 <= age < quiet_seconds
    }

    if unsettled:
        age_strs = [
            f"{f.name}={age:.0f}s" for f, age in unsettled.items()
        ]
        return GuardResult(
            blocked=True,
            layer="write-settle",
            detail=f"ages: {', '.join(age_strs)}, need {quiet_seconds:.0f}s",
        )

    settled_strs = []
    for f, age in folder_ages.items():
        if age < 0:
            settled_strs.append(f"{f.name}=own-output")
        else:
            settled_strs.append(f"{f.name}={age:.0f}s")
    detail = (
        f"settled ({', '.join(settled_strs)})" if settled_strs else "no files"
    )
    return GuardResult(blocked=False, layer="write-settle", detail=detail)


def _newest_note_mtime(folder: Path) -> float:
    """Return the mtime of the most recently modified note in *folder*."""
    from b2ou.constants import CLOUD_JUNK_DIRS, CLOUD_JUNK_RE

    newest = 0.0
    if not folder.is_dir():
        return newest

    for root, dirs, files in os.walk(folder):
        dirs[:] = [
            d for d in dirs
            if d not in CLOUD_JUNK_DIRS
            and d not in ("BearImages", ".obsidian")
        ]
        # Handle .textbundle dirs inline
        for d in list(dirs):
            if d.endswith(".textbundle"):
                try:
                    t = (Path(root) / d / "text.md").stat().st_mtime
                    if t > newest:
                        newest = t
                except OSError:
                    pass
                dirs.remove(d)

        for fname in files:
            if CLOUD_JUNK_RE.match(fname) or not _is_note_file(fname):
                continue
            try:
                t = (Path(root) / fname).stat().st_mtime
                if t > newest:
                    newest = t
            except OSError:
                pass

    return newest


# ---------------------------------------------------------------------------
# Layer 3: frontmost application
# ---------------------------------------------------------------------------

def check_frontmost() -> GuardResult:
    """
    Check whether a known editor application is currently frontmost.

    Uses ``b2ou.platform_macos.get_frontmost_bundle_id`` (PyObjC preferred,
    lsappinfo / osascript fallback).
    """
    from b2ou.platform_macos import HAS_APPKIT, get_frontmost_bundle_id

    bid = get_frontmost_bundle_id()
    method = "PyObjC" if HAS_APPKIT else "subprocess"

    if bid and (bid in EDITOR_BUNDLE_IDS_LOWER
                or any(kw in bid for kw in EDITOR_KEYWORDS)):
        return GuardResult(blocked=True, layer="frontmost", detail=bid)

    return GuardResult(
        blocked=False,
        layer="frontmost",
        detail=f"{bid or '(none)'} via {method}",
    )


# ---------------------------------------------------------------------------
# Combined guard
# ---------------------------------------------------------------------------

def check_editing_guard(
    folders: list[Path],
    quiet_seconds: float,
    last_sync_end: float,
    verbose: bool = False,
    log_all: bool = False,
    snapshots: Optional[dict[Path, VaultSnapshot]] = None,
) -> str:
    """
    Run all three layers cheapest-first.

    Returns a non-empty reason string if editing is detected (sync should
    NOT run) or an empty string if all layers are clear.

    Parameters
    ----------
    folders        : vault export folders to monitor
    quiet_seconds  : Layer 2 write-settle threshold
    last_sync_end  : Unix timestamp of the last completed sync
    verbose        : Log each layer's result at INFO level (--guard-test)
    log_all        : Log each layer at DEBUG level (daemon diagnostics)
    snapshots      : Pre-built ``{folder: VaultSnapshot}`` (avoids re-walks)
    """
    results: dict[str, GuardResult] = {}

    # Layer 3 (cheapest — <1 ms with PyObjC)
    results["frontmost"] = check_frontmost()

    # Layer 2 (fast — mtime checks)
    results["write-settle"] = check_write_settle(
        folders, quiet_seconds, last_sync_end, snapshots
    )

    # Layer 1 (most reliable — lsof +D, slowest)
    results["lsof"] = check_lsof(folders)

    order = ("frontmost", "write-settle", "lsof")

    if verbose:
        for layer in order:
            r = results[layer]
            status = "BLOCKED" if r.blocked else "ok"
            log.info("  [%-13s] %s  %s", layer, status, r.detail)

    if log_all:
        parts = [
            f"[{layer}]={'BLOCK' if results[layer].blocked else 'ok'}"
            f"({results[layer].detail})"
            for layer in order
        ]
        log.debug("Guard: %s", "  ".join(parts))

    for layer in order:
        r = results[layer]
        if r.blocked:
            return str(r)

    return ""
