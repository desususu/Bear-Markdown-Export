"""
macOS platform helpers — AppKit / Foundation wrappers and Bear x-callback-url.

All public functions degrade gracefully when PyObjC is not installed or when
running on a non-macOS platform; they log a warning and return a safe default.

Import guard
------------
The top-level ``try`` block captures availability in ``HAS_APPKIT``.
Callers can check this flag before attempting platform-specific operations.
"""

from __future__ import annotations

import logging
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional AppKit / Foundation imports
# ---------------------------------------------------------------------------

try:
    from AppKit import (  # type: ignore[import]
        NSWorkspace,
        NSWorkspaceOpenConfiguration,
        NSURL,
    )
    from Foundation import (  # type: ignore[import]
        NSAutoreleasePool,
        NSDate,
        NSFileCreationDate,
        NSFileManager,
        NSRunLoop,
    )
    HAS_APPKIT = True
except ImportError:
    HAS_APPKIT = False

# Pre-built open configuration (activates=False so Bear stays in background)
_open_config: Optional[object] = None
if HAS_APPKIT:
    try:
        _open_config = NSWorkspaceOpenConfiguration.alloc().init()
        _open_config.setActivates_(False)
    except Exception:
        _open_config = None


# ---------------------------------------------------------------------------
# File creation date
# ---------------------------------------------------------------------------

def set_creation_date(filepath: Path, unix_timestamp: float) -> None:
    """
    Set the file's creation date (birthtime) using NSFileManager.

    Replaces the old ``SetFile -d`` subprocess call (~50 ms per invocation)
    with a direct NSFileManager attribute write (<1 ms, zero process overhead).
    """
    if not HAS_APPKIT:
        return
    try:
        ns_date = NSDate.dateWithTimeIntervalSince1970_(unix_timestamp)
        attrs = {NSFileCreationDate: ns_date}
        NSFileManager.defaultManager().setAttributes_ofItemAtPath_error_(
            attrs, str(filepath), None
        )
    except Exception as exc:
        log.warning("Native creation-date set failed for %s: %s", filepath, exc)


# ---------------------------------------------------------------------------
# Frontmost application detection
# ---------------------------------------------------------------------------

def get_frontmost_bundle_id() -> str:
    """
    Return the bundle ID of the currently frontmost application (lowercase).

    Tries three methods in order of speed:
      1. NSWorkspace  (<1 ms, PyObjC native)
      2. lsappinfo    (~50 ms, no special permissions)
      3. osascript    (~200 ms, most compatible)
    """
    # Method 1: PyObjC native
    if HAS_APPKIT:
        try:
            pool = NSAutoreleasePool.alloc().init()
            try:
                NSRunLoop.currentRunLoop().runUntilDate_(
                    NSDate.dateWithTimeIntervalSinceNow_(0.01)
                )
                app = NSWorkspace.sharedWorkspace().frontmostApplication()
                if app:
                    bid = app.bundleIdentifier()
                    if bid:
                        return bid.lower()
            finally:
                del pool
        except Exception:
            pass

    # Method 2: lsappinfo
    try:
        r = subprocess.run(
            ["lsappinfo", "info", "-only", "bundleid", "-app", "Front"],
            capture_output=True, text=True, timeout=3,
        )
        out = r.stdout.strip()
        if '="' in out:
            bid = out.split('="', 1)[1].strip('" \n')
            if bid:
                return bid.lower()
    except Exception:
        pass

    # Method 3: osascript
    try:
        r = subprocess.run(
            [
                "osascript", "-e",
                'tell application "System Events" to get bundle identifier '
                "of first application process whose frontmost is true",
            ],
            capture_output=True, text=True, timeout=3,
        )
        bid = r.stdout.strip()
        if bid:
            return bid.lower()
    except Exception:
        pass

    return ""


# ---------------------------------------------------------------------------
# Bear x-callback-url
# ---------------------------------------------------------------------------

def open_bear_url(url_string: str) -> bool:
    """
    Open a ``bear://`` URL via NSWorkspace (or ``open`` subprocess fallback).

    Returns ``True`` on apparent success, ``False`` otherwise.
    """
    if HAS_APPKIT and _open_config is not None:
        try:
            url = NSURL.URLWithString_(url_string)
            if url is None:
                log.warning("Could not build NSURL for: %.120s", url_string)
                return False
            NSWorkspace.sharedWorkspace().openURL_configuration_completionHandler_(
                url, _open_config, None
            )
            return True
        except Exception as exc:
            log.warning("NSWorkspace openURL failed: %s", exc)

    # Fallback: subprocess open
    try:
        subprocess.call(["open", url_string])
        return True
    except Exception as exc:
        log.warning("open subprocess failed: %s", exc)
        return False


def bear_x_callback(
    x_command: str,
    text: str,
    message: str = "",
    orig_title: str = "",  # unused — kept for API compatibility
) -> None:
    """
    Send *text* to Bear via an x-callback-url command.

    If *message* is non-empty it is inserted as the second line of *text*
    (used for sync-conflict notices).
    """
    if message:
        lines = text.splitlines()
        lines.insert(1, message)
        text = "\n".join(lines)

    url = x_command + "&text=" + urllib.parse.quote(text, safe="")
    if not open_bear_url(url):
        log.error("bear_x_callback: failed to send URL (%.80s…)", url)
    time.sleep(0.2)


def bear_add_file(
    note_uuid: str,
    filename: str,
    encoded_file: str,
) -> None:
    """Upload a base64-encoded file to Bear via x-callback-url."""
    safe_filename = urllib.parse.quote(filename)
    safe_file = urllib.parse.quote(encoded_file, safe="")
    url = (
        f"bear://x-callback-url/add-file?show_window=no&open_note=no"
        f"&id={note_uuid}&filename={safe_filename}&mode=append&file={safe_file}"
    )
    open_bear_url(url)
    time.sleep(0.5)


# ---------------------------------------------------------------------------
# macOS file tags (xattr)
# ---------------------------------------------------------------------------

def get_file_tags(filepath: Path) -> list[str]:
    """
    Return the macOS Finder tags (``com.apple.metadata:_kMDItemUserTags``)
    for *filepath* as a list of strings.

    Returns an empty list if the file has no tags or the read fails.
    """
    import json
    import re

    try:
        r = subprocess.run(
            ["xattr", "-p", "com.apple.metadata:_kMDItemUserTags", str(filepath)],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return []
        # xattr -p emits hex; pipe through xxd -r -p | plutil to get JSON
        r2 = subprocess.run(
            ["bash", "-c",
             f"xattr -p com.apple.metadata:_kMDItemUserTags {repr(str(filepath))} "
             "2>/dev/null | xxd -r -p | plutil -convert json - -o - 2>/dev/null"],
            capture_output=True, text=True, timeout=5,
        )
        if r2.returncode != 0 or not r2.stdout.strip():
            return []
        text = re.sub(r"\\n\d{1,2}", "", r2.stdout)
        return json.loads(text)
    except Exception:
        return []
