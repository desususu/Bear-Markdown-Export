"""
Export phase: Bear SQLite database → Markdown / TextBundle files on disk.

Entry point: ``export_notes(config)``

The export is incremental: notes whose on-disk file is already at or newer
than the Bear modification timestamp are skipped (zero I/O).  At the end,
stale files (no longer present in Bear) are removed.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import shutil
import urllib.parse
from pathlib import Path
from typing import Optional

from b2ou.config import ExportConfig
from b2ou.constants import (
    EXPORT_SKIP_DIR_PREFIXES,
    EXPORT_SKIP_DIRS,
    IMAGE_EXTENSIONS,
    RE_BEAR_ID_FIND_NEW,
    SENTINEL_FILES,
)
from b2ou.db import BearNote, copy_and_open, core_data_to_unix, iter_notes
from b2ou.images import (
    collect_referenced_local_images,
    copy_incremental,
    process_export_images,
    process_export_images_textbundle,
)
from b2ou.markdown import (
    clean_title,
    hide_tags,
    inject_bear_id,
    sub_path_from_tag,
)
from b2ou.platform_macos import set_creation_date

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------

def write_note_file(
    filepath: Path,
    content: str,
    modified_unix: float,
    created_core_data: float,
) -> None:
    """Write *content* to *filepath*, preserving Bear timestamps."""
    is_new = not filepath.exists()
    filepath.parent.mkdir(parents=True, exist_ok=True)
    tmp = filepath.with_name(f".{filepath.name}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, filepath)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    if modified_unix > 0:
        os.utime(filepath, (-1, modified_unix))
    if created_core_data > 0 and is_new:
        set_creation_date(filepath, core_data_to_unix(created_core_data))


# ---------------------------------------------------------------------------
# Stale-file cleanup
# ---------------------------------------------------------------------------

def cleanup_stale_notes(
    export_path: Path, expected_paths: set[Path]
) -> int:
    """
    Remove exported note files / bundles no longer present in Bear.

    Skips ``BearImages``, ``.obsidian``, ``.Ulysses*`` directories and
    sentinel files.  Returns the count of removed items.
    """
    if not export_path.is_dir():
        return 0

    removed = 0
    empty_dirs: list[Path] = []

    for root, dirs, files in os.walk(export_path, topdown=True):
        root_path = Path(root)
        keep_dirs: list[str] = []
        for d in dirs:
            if d in EXPORT_SKIP_DIRS:
                continue
            if any(d.startswith(pfx) for pfx in EXPORT_SKIP_DIR_PREFIXES):
                continue
            if d.endswith(".Ulysses_Public_Filter"):
                continue
            if d.endswith(".textbundle"):
                bundle = root_path / d
                if bundle not in expected_paths:
                    try:
                        shutil.rmtree(bundle)
                        removed += 1
                    except OSError:
                        pass
                continue
            keep_dirs.append(d)
        dirs[:] = keep_dirs

        for fname in files:
            if fname in SENTINEL_FILES:
                continue
            fpath = root_path / fname
            if fpath in expected_paths:
                continue
            if any(fname.endswith(ext) for ext in (".md", ".txt", ".markdown")):
                try:
                    fpath.unlink()
                    removed += 1
                except OSError:
                    pass

        if root_path != export_path:
            empty_dirs.append(root_path)

    # Remove empty tag subdirectories (deepest first)
    for d in sorted(empty_dirs, reverse=True):
        try:
            if d.is_dir() and not list(d.iterdir()):
                d.rmdir()
        except OSError:
            pass

    return removed


def cleanup_orphan_root_images(config: ExportConfig) -> int:
    """
    Remove root-level images that are no longer referenced by any note
    and already have a canonical copy in the BearImages assets folder.
    """
    if not config.export_path.is_dir():
        return 0

    referenced = collect_referenced_local_images(
        config.export_path, EXPORT_SKIP_DIRS
    )
    asset_basenames: set[str] = set()
    if config.assets_path and config.assets_path.is_dir():
        for _, _, files in os.walk(config.assets_path):
            asset_basenames.update(files)

    removed = 0
    try:
        root_files = list(config.export_path.iterdir())
    except OSError:
        return 0

    for fpath in root_files:
        if not fpath.is_file():
            continue
        if fpath.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        if fpath in referenced:
            continue
        if fpath.name not in asset_basenames:
            continue
        try:
            fpath.unlink()
            removed += 1
            log.debug("Removed orphan root image: %s", fpath.name)
        except OSError:
            pass

    return removed


# ---------------------------------------------------------------------------
# TextBundle export
# ---------------------------------------------------------------------------

def make_text_bundle(
    text: str,
    filepath: Path,
    mod_unix: float,
    created_core_data: float,
    conn,
    note_pk: int,
    bear_image_path: Path,
    clean_export: bool = False,
) -> None:
    """Write a ``.textbundle`` for *text* at *filepath* (without extension)."""
    bundle_path = Path(str(filepath) + ".textbundle")
    bundle_assets = bundle_path / "assets"
    bundle_assets.mkdir(parents=True, exist_ok=True)

    uuid_match = RE_BEAR_ID_FIND_NEW.search(text)
    uuid_str = uuid_match.group(1) if uuid_match else ""

    info = json.dumps(
        {
            "transient": True,
            "type": "net.daringfireball.markdown",
            "version": 2,
            "creatorIdentifier": "net.shinyfrog.bear",
            "bear_uuid": uuid_str,
        }
    )

    if uuid_str and not clean_export:
        write_note_file(bundle_path / ".bearid", uuid_str, mod_unix, 0)

    text = process_export_images_textbundle(
        text, bundle_assets, conn, note_pk, bear_image_path
    )

    write_note_file(bundle_path / "text.md", text, mod_unix, 0)
    write_note_file(bundle_path / "info.json", info, mod_unix, 0)
    os.utime(bundle_path, (-1, mod_unix))


# ---------------------------------------------------------------------------
# Timestamp files
# ---------------------------------------------------------------------------

def write_timestamps(config: ExportConfig) -> None:
    """Write the ``export-time.log`` and ``sync-time.log`` sentinel files."""
    msg = "Markdown from Bear written at: " + datetime.datetime.now().strftime(
        "%Y-%m-%d at %H:%M:%S"
    )
    for path in (config.export_ts_file, config.sync_ts_file):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(msg, encoding="utf-8")


def check_db_modified(config: ExportConfig) -> bool:
    """Return True if Bear's database is newer than the last export timestamp."""
    if not config.export_ts_file.exists():
        return True
    try:
        db_mtime = config.bear_db.stat().st_mtime
        ts_mtime = config.export_ts_file.stat().st_mtime
        return db_mtime > ts_mtime
    except OSError:
        return True


# ---------------------------------------------------------------------------
# Main export entry point
# ---------------------------------------------------------------------------

def export_notes(config: ExportConfig) -> tuple[int, set[Path]]:
    """
    Export all non-trashed, non-archived Bear notes to *config.export_path*.

    Returns ``(note_count, expected_paths)`` where *expected_paths* is the
    set of absolute paths that should exist on disk after the export.
    This set is passed to ``cleanup_stale_notes`` to remove deletions.
    """
    conn, tmp_path = copy_and_open(config.bear_db)
    note_count = 0
    expected_paths: set[Path] = set()
    reserved_targets: set[Path] = set()

    def _target_for(base_path: Path, as_textbundle: bool) -> Path:
        suffix = ".textbundle" if as_textbundle else ".md"
        return Path(str(base_path) + suffix)

    def _unique_base_path(
        base_path: Path,
        as_textbundle: bool,
        note_uuid: str,
    ) -> Path:
        target = _target_for(base_path, as_textbundle)
        if target not in reserved_targets:
            reserved_targets.add(target)
            return base_path

        tagged = base_path.parent / f"{base_path.name} - {note_uuid[:8]}"
        tagged_target = _target_for(tagged, as_textbundle)
        if tagged_target not in reserved_targets:
            reserved_targets.add(tagged_target)
            return tagged

        count = 2
        while True:
            candidate = base_path.parent / f"{tagged.name} - {count:02d}"
            candidate_target = _target_for(candidate, as_textbundle)
            if candidate_target not in reserved_targets:
                reserved_targets.add(candidate_target)
                return candidate
            count += 1

    try:
        config.export_path.mkdir(parents=True, exist_ok=True)

        for note in iter_notes(conn):
            filename = clean_title(note.title)
            mod_unix = core_data_to_unix(note.modified_date)
            text = note.text

            if config.hide_tags:
                text = hide_tags(text)

            if config.make_tag_folders:
                file_list = sub_path_from_tag(
                    str(config.export_path),
                    filename,
                    text,
                    make_tag_folders=True,
                    multi_tag_folders=config.multi_tag_folders,
                    only_export_tags=config.only_export_tags,
                    exclude_tags=config.exclude_tags,
                )
            else:
                is_excluded = any(
                    ("#" + tag) in text for tag in config.exclude_tags
                )
                file_list = (
                    []
                    if is_excluded
                    else [str(config.export_path / filename)]
                )

            if not file_list:
                continue

            if not config.clean_export:
                text = inject_bear_id(text, note.uuid)

            seen_paths: set[str] = set()
            for filepath_str in file_list:
                if filepath_str in seen_paths:
                    continue
                seen_paths.add(filepath_str)

                filepath = Path(filepath_str)
                note_count += 1
                as_textbundle = (
                    config.export_as_textbundles
                    and _should_use_textbundle(text, filepath, config)
                )
                filepath = _unique_base_path(filepath, as_textbundle, note.uuid)
                target = _target_for(filepath, as_textbundle)

                # ── Incremental skip ──────────────────────────────────────
                if target.exists() and target.stat().st_mtime >= mod_unix:
                    expected_paths.add(target)
                    continue

                # ── Full export ───────────────────────────────────────────
                if as_textbundle:
                    make_text_bundle(
                        text, filepath, mod_unix, note.creation_date,
                        conn, note.pk, config.bear_image_path,
                        clean_export=config.clean_export
                    )
                    expected_paths.add(target)
                elif config.export_image_repository:
                    processed = process_export_images(
                        text, filepath, conn, note.pk,
                        config.bear_image_path,
                        config.assets_path,
                        config.export_path,
                    )
                    write_note_file(
                        target, processed, mod_unix, note.creation_date
                    )
                    expected_paths.add(target)

                else:
                    write_note_file(
                        target, text, mod_unix, note.creation_date
                    )
                    expected_paths.add(target)

    finally:
        conn.close()
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    return note_count, expected_paths


def _should_use_textbundle(
    text: str, filepath: Path, config: ExportConfig
) -> bool:
    """True if this note should be exported as a .textbundle."""
    if not config.export_as_hybrids:
        return True
    tb = Path(str(filepath) + ".textbundle")
    if tb.exists():
        return True
    from b2ou.constants import RE_BEAR_IMAGE
    return bool(RE_BEAR_IMAGE.search(text) or __import__("re").search(r"!\[", text))
