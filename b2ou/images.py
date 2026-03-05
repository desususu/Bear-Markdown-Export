"""
Image copy, upload, and path-resolution helpers.

Export side
-----------
``process_export_images`` rewrites Bear image references in the exported
Markdown and copies the actual image files to the assets directory
(incremental — only newer sources are copied).

Import side
-----------
``process_import_images`` rewrites image references in a note being
imported back to Bear and uploads new images via x-callback-url.
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
import sqlite3
import urllib.parse
from pathlib import Path
from typing import Optional

from b2ou.constants import (
    RE_BEAR_IMAGE,
    RE_BEAR_IMG_SUB,
    RE_MD_IMAGE,
    RE_UUID_ASSET,
    RE_UUID_DIR,
    RE_WIKI_IMAGE,
)
from b2ou.markdown import html_img_to_markdown, normalize_local_image_ref

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Low-level file helpers
# ---------------------------------------------------------------------------

def copy_incremental(source: Path, dest: Path) -> None:
    """Copy *source* → *dest* only when *source* is newer; create dirs as needed."""
    if not source.exists():
        return
    if dest.exists() and dest.stat().st_mtime >= source.stat().st_mtime:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)


def collect_referenced_local_images(root_path: Path, skip_dirs: frozenset) -> set[Path]:
    """
    Walk *root_path* and return the set of absolute local image paths
    referenced by all note files found there.
    """
    from b2ou.constants import RE_WIKI_IMAGE  # local to avoid circular

    refs: set[Path] = set()
    if not root_path.is_dir():
        return refs

    for dirpath, dirnames, filenames in os.walk(root_path, topdown=True):
        dirnames[:] = [
            d for d in dirnames
            if d not in skip_dirs
            and d != ".git"
            and d != "__pycache__"
        ]
        for fname in filenames:
            if not any(fname.endswith(ext) for ext in (".md", ".txt", ".markdown")):
                continue
            note_path = Path(dirpath) / fname
            try:
                text = note_path.read_text(encoding="utf-8")
            except Exception:
                continue

            text = html_img_to_markdown(text)

            for m in RE_MD_IMAGE.finditer(text):
                raw = normalize_local_image_ref(m.group(2))
                if not raw or raw.startswith(("http://", "https://")):
                    continue
                abs_img = Path(raw) if os.path.isabs(raw) else (
                    note_path.parent / raw
                ).resolve()
                refs.add(abs_img)

            for raw in RE_WIKI_IMAGE.findall(text):
                img = normalize_local_image_ref(raw)
                if not img or img.startswith(("http://", "https://")):
                    continue
                abs_img = Path(img) if os.path.isabs(img) else (
                    note_path.parent / img
                ).resolve()
                refs.add(abs_img)

    return refs


# ---------------------------------------------------------------------------
# Export-side image processing
# ---------------------------------------------------------------------------

def process_export_images(
    text: str,
    filepath: Path,
    conn: sqlite3.Connection,
    note_pk: int,
    bear_image_path: Path,
    assets_path: Path,
    export_path: Path,
) -> str:
    """
    Rewrite Bear image references in *text* to point at *assets_path* and
    copy the image files there (incrementally).

    Handles:
    • Bear 1.x: ``[image:UUID/filename]`` → stored at ``bear_image_path/UUID/filename``
    • Bear 2.x: ``![alt](filename)``      → linked via ZSFNOTEFILE UUID lookup
    """
    # Build filename → UUID map for Bear 2.x images attached to this note
    image_file_map: dict[str, str] = {}
    for row in conn.execute(
        "SELECT ZFILENAME, ZUNIQUEIDENTIFIER FROM ZSFNOTEFILE WHERE ZNOTE = ?",
        (note_pk,),
    ):
        image_file_map[row["ZFILENAME"]] = row["ZUNIQUEIDENTIFIER"]

    rel_assets = os.path.relpath(assets_path, export_path)

    # ── Bear 1.x: [image:UUID/filename] ──────────────────────────────────

    def _rewrite_bear1(m: "re.Match") -> str:
        ref = m.group(1)
        parts = ref.split("/", 1)
        if len(parts) != 2:
            return m.group(0)
        img_uuid, img_filename = parts
        source = bear_image_path / img_uuid / img_filename
        dest = assets_path / img_uuid / img_filename
        copy_incremental(source, dest)
        rel = f"{rel_assets}/{img_uuid}/{img_filename}"
        return f"![]({urllib.parse.quote(rel)})"

    text = RE_BEAR_IMAGE.sub(_rewrite_bear1, text)

    # ── Bear 2.x: ![alt](filename) ───────────────────────────────────────

    def _rewrite_md(m: "re.Match") -> str:
        img_url = m.group(2)
        if img_url.startswith("http"):
            return m.group(0)

        img_filename = urllib.parse.unquote(img_url)
        if img_filename.startswith(rel_assets + "/"):
            return m.group(0)  # already exported

        file_uuid = image_file_map.get(os.path.basename(img_filename))
        if file_uuid is None:
            return m.group(0)

        basename = os.path.basename(img_filename)
        source = bear_image_path / file_uuid / basename
        dest = assets_path / file_uuid / basename
        copy_incremental(source, dest)
        rel = f"{rel_assets}/{file_uuid}/{basename}"
        return f"![{m.group(1)}]({urllib.parse.quote(rel)})"

    return RE_MD_IMAGE.sub(_rewrite_md, text)


def process_export_images_textbundle(
    text: str,
    bundle_assets: Path,
    conn: sqlite3.Connection,
    note_pk: int,
    bear_image_path: Path,
) -> str:
    """
    Like ``process_export_images`` but for TextBundle format.

    Images are copied into *bundle_assets* (inside the ``.textbundle``).
    """
    # Copy Bear 1.x [image:UUID/filename] into assets/
    for match in RE_BEAR_IMAGE.findall(text):
        image_name = match
        new_name = image_name.replace("/", "_")
        source = bear_image_path / image_name
        target = bundle_assets / new_name
        if source.exists():
            shutil.copy2(source, target)
    text = RE_BEAR_IMG_SUB.sub(r"![](assets/\1_\2)", text)

    # Build UUID→filename map for Bear 2.x
    image_map: dict[str, str] = {}
    for row in conn.execute(
        "SELECT ZFILENAME, ZUNIQUEIDENTIFIER FROM ZSFNOTEFILE WHERE ZNOTE = ?",
        (note_pk,),
    ):
        image_map[row["ZFILENAME"]] = row["ZUNIQUEIDENTIFIER"]

    def _replace_md(m: "re.Match") -> str:
        alt_text = m.group(1)
        image_url = m.group(2)
        if image_url.startswith("http") or image_url.startswith("assets/"):
            return m.group(0)
        image_filename = urllib.parse.unquote(image_url)
        file_uuid = image_map.get(os.path.basename(image_filename))
        if not file_uuid:
            return m.group(0)
        basename = os.path.basename(image_filename)
        source = bear_image_path / file_uuid / basename
        new_name = f"{file_uuid}_{basename}"
        target = bundle_assets / new_name
        if source.exists():
            shutil.copy2(source, target)
        return f"![{alt_text}]({urllib.parse.quote(f'assets/{new_name}')})"

    return RE_MD_IMAGE.sub(_replace_md, text)


# ---------------------------------------------------------------------------
# Import-side image processing
# ---------------------------------------------------------------------------

def process_import_images(
    text: str,
    md_file: Path,
    export_path: Path,
    assets_path: Path,
    note_uuid: Optional[str] = None,
    note_title: Optional[str] = None,
    vault_index: Optional[dict[str, Path]] = None,
) -> str:
    """
    For each local image reference in *text*:
    - Skip remote URLs unchanged.
    - Identify Bear-exported images (no re-upload needed).
    - Upload new images to Bear via x-callback-url and rewrite the link.
    """
    from b2ou.platform_macos import bear_add_file

    text = html_img_to_markdown(text)
    md_dir = md_file.parent

    def _resolve(img_path_unquoted: str) -> Optional[Path]:
        # 1. Relative to the md file's directory
        candidate = (md_dir / img_path_unquoted).resolve()
        if candidate.exists():
            return candidate
        # 2. In the BearImages folder
        candidate = assets_path / os.path.basename(img_path_unquoted)
        if candidate.exists():
            return candidate
        # 3. Vault-wide lookup
        target_name = os.path.basename(img_path_unquoted)
        if vault_index and target_name in vault_index:
            return vault_index[target_name]
        # 4. Fresh walk fallback
        for root, dirs, files in os.walk(export_path):
            if ".obsidian" in dirs:
                dirs.remove(".obsidian")
            if target_name in files:
                return Path(root) / target_name
        return None

    def _upload_and_format(alt_text: str, img_path: str) -> str:
        img_unquoted = normalize_local_image_ref(img_path)
        if img_unquoted.startswith(("http://", "https://")):
            return f"![{alt_text}]({img_unquoted})"
        if not img_unquoted:
            return f"![{alt_text}]({urllib.parse.quote(str(img_path))})"

        abs_path = _resolve(img_unquoted)
        if abs_path is None:
            return f"![{alt_text}]({urllib.parse.quote(img_unquoted)})"

        img_filename = abs_path.name

        # Determine whether this is already a Bear-exported image
        normalised = "/" + img_unquoted.replace("\\", "/")
        is_bear_image = bool(
            RE_UUID_DIR.search(normalised) or RE_UUID_ASSET.search(normalised)
        )

        if not is_bear_image and (note_uuid or note_title):
            try:
                encoded = base64.b64encode(abs_path.read_bytes()).decode("utf-8")
                if note_uuid:
                    bear_add_file(note_uuid, img_filename, encoded)
                else:
                    # Upload by title — use add-file with title parameter
                    from b2ou.platform_macos import open_bear_url
                    safe_filename = urllib.parse.quote(img_filename)
                    safe_file = urllib.parse.quote(encoded, safe="")
                    safe_title = urllib.parse.quote(note_title or "")
                    url = (
                        f"bear://x-callback-url/add-file?show_window=no&open_note=no"
                        f"&title={safe_title}&filename={safe_filename}"
                        f"&mode=append&file={safe_file}"
                    )
                    open_bear_url(url)
                    import time
                    time.sleep(0.5)
            except Exception as exc:
                log.warning("Image upload failed for %s: %s", img_filename, exc)

        return f"![{alt_text}]({urllib.parse.quote(img_filename)})"

    text = RE_MD_IMAGE.sub(lambda m: _upload_and_format(m.group(1), m.group(2)), text)
    text = RE_WIKI_IMAGE.sub(lambda m: _upload_and_format("image", m.group(1)), text)
    return text
