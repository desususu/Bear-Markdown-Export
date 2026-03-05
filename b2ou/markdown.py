"""
Pure Markdown transformation functions — no I/O, no config dependencies.

All functions take a string and return a string (or ancillary data).
They rely only on the pre-compiled patterns in ``b2ou.constants``.
"""

from __future__ import annotations

import urllib.parse
from typing import Optional

from b2ou.constants import (
    RE_BEAR_ID_FIND_NEW,
    RE_BEAR_ID_FIND_OLD,
    RE_BEAR_ID_NEW,
    RE_BEAR_ID_OLD,
    RE_CLEAN_TITLE,
    RE_HEADING,
    RE_HIDE_TAGS,
    RE_HTML_IMG_ALT,
    RE_HTML_IMG_SRC,
    RE_HTML_IMG_TAG,
    RE_MD_HEADING,
    RE_MD_IMAGE,
    RE_REF_CLEAN,
    RE_REF_DEF,
    RE_REF_IMP,
    RE_REF_IMG,
    RE_REF_LINK,
    RE_REF_LINK_IMP,
    RE_TAG_PATTERN1,
    RE_TAG_PATTERN2,
    RE_TRAILING_DASH,
    RE_WIKI_IMAGE,
)


# ---------------------------------------------------------------------------
# Title sanitisation
# ---------------------------------------------------------------------------

def clean_title(title: str) -> str:
    """Return a filesystem-safe version of *title* (max 225 chars)."""
    title = title[:225].strip() or "Untitled"
    title = RE_CLEAN_TITLE.sub("-", title)
    title = RE_TRAILING_DASH.sub("", title)
    return title.strip()


# ---------------------------------------------------------------------------
# BearID injection / extraction / stripping
# ---------------------------------------------------------------------------

def inject_bear_id(text: str, uuid: str) -> str:
    """Insert a hidden BearID comment on the second line of *text*."""
    lines = text.split("\n", 1)
    id_marker = f"[//]: # ({{BearID:{uuid}}})"
    if len(lines) > 1:
        return f"{lines[0]}\n{id_marker}\n{lines[1]}"
    return f"{text}\n{id_marker}"


def extract_bear_id(text: str) -> Optional[str]:
    """Return the BearID UUID embedded in *text*, or ``None``."""
    m = RE_BEAR_ID_FIND_NEW.search(text) or RE_BEAR_ID_FIND_OLD.search(text)
    return m.group(1) if m else None


def strip_bear_id(text: str) -> str:
    """Remove all BearID markers from *text*."""
    text = RE_BEAR_ID_NEW.sub("", text)
    text = RE_BEAR_ID_OLD.sub("", text)
    return text


# ---------------------------------------------------------------------------
# Tag handling
# ---------------------------------------------------------------------------

def hide_tags(text: str) -> str:
    """Strip Bear tag lines (lines beginning with #tag) from *text*."""
    return RE_HIDE_TAGS.sub(r"\1", text)


def extract_tags(text: str) -> list[str]:
    """Return all Bear tag strings found in *text* (without the leading #)."""
    tags: list[str] = []
    tags.extend(m[0] for m in RE_TAG_PATTERN1.findall(text))
    tags.extend(m[0] for m in RE_TAG_PATTERN2.findall(text))
    return tags


def tag_from_path(
    text: str,
    md_file: str,
    root_path: str,
    inbox_for_root: bool = False,
    extra_tag: str = "",
    file_tags: Optional[list[str]] = None,
) -> str:
    """
    Append a Bear tag derived from the note's folder path (and optionally
    macOS file tags) to *text*.

    The folder path relative to *root_path* becomes the tag:
      root/                 → #.inbox  (when inbox_for_root)
      root/some_folder/     → #some_folder
      root/_private/        → #._private  (underscore prefix → dot prefix)
    """
    import os

    path = md_file.replace(root_path, "")[1:]
    sub_path = os.path.split(path)[0]
    if ".textbundle" in sub_path:
        sub_path = os.path.split(sub_path)[0]

    if sub_path == "":
        tag = "#.inbox" if inbox_for_root else ""
    elif sub_path.startswith("_"):
        tag = "#." + sub_path[1:].strip()
    else:
        tag = "#" + sub_path.strip()

    if " " in tag:
        tag += "#"

    all_tags = [t for t in [tag] if t]
    if extra_tag:
        all_tags.append(extra_tag)
    for ft in (file_tags or []):
        t = "#" + ft.strip()
        if " " in t:
            t += "#"
        all_tags.append(t)

    return text.strip() + "\n\n" + " ".join(all_tags) + "\n"


def sub_path_from_tag(
    base_path: str,
    filename: str,
    text: str,
    make_tag_folders: bool,
    multi_tag_folders: bool,
    only_export_tags: list[str],
    exclude_tags: list[str],
) -> list[str]:
    """
    Return the list of output file paths for a note, based on its tags.

    With *make_tag_folders=False* (default) returns a single path under
    *base_path*.  With *make_tag_folders=True* the note may be written to
    multiple tag-based subdirectories (when *multi_tag_folders=True*).
    """
    import os

    if not make_tag_folders:
        is_excluded = any(("#" + tag) in text for tag in exclude_tags)
        return [] if is_excluded else [os.path.join(base_path, filename)]

    if multi_tag_folders:
        tags = extract_tags(text)
        if not tags:
            return [os.path.join(base_path, filename)]
    else:
        t1 = RE_TAG_PATTERN1.search(text)
        t2 = RE_TAG_PATTERN2.search(text)
        if t1 and t2:
            tag = t1.group(1) if t1.start(1) < t2.start(1) else t2.group(1)
        elif t1:
            tag = t1.group(1)
        elif t2:
            tag = t2.group(1)
        else:
            return [os.path.join(base_path, filename)]
        tags = [tag]

    paths = [os.path.join(base_path, filename)]
    for tag in tags:
        if tag == "/":
            continue
        if only_export_tags:
            if not any(tag.lower().startswith(et.lower())
                       for et in only_export_tags):
                continue
        if any(tag.lower().startswith(nt.lower()) for nt in exclude_tags):
            return []
        sub = ("_" + tag[1:]) if tag.startswith(".") else tag
        tag_path = os.path.join(base_path, sub)
        os.makedirs(tag_path, exist_ok=True)
        paths.append(os.path.join(tag_path, filename))
    return paths


# ---------------------------------------------------------------------------
# HTML → Markdown image conversion
# ---------------------------------------------------------------------------

def html_img_to_markdown(text: str) -> str:
    """Replace ``<img src=... alt=...>`` tags with ``![alt](src)`` syntax."""

    def _replace(m: "re.Match") -> str:
        tag = m.group(0)
        src_m = RE_HTML_IMG_SRC.search(tag)
        if not src_m:
            return tag
        src = (src_m.group(2) or "").strip()
        if not src:
            return tag
        alt_m = RE_HTML_IMG_ALT.search(tag)
        alt = (alt_m.group(2) if alt_m else "image").strip() or "image"
        alt = alt.replace("]", r"\]")
        return f"![{alt}]({src})"

    return RE_HTML_IMG_TAG.sub(_replace, text)


# ---------------------------------------------------------------------------
# Reference-style link resolution
# ---------------------------------------------------------------------------

def ref_links_to_inline(text: str) -> str:
    """
    Expand reference-style links and images to inline syntax.

    ``[text][ref]`` → ``[text](url)``
    ``![alt][ref]`` → ``![alt](url)``
    """
    refs = dict(RE_REF_DEF.findall(text))
    if not refs:
        return text

    # Reference images
    text = RE_REF_IMG.sub(
        lambda m: f"![{m.group(1)}]({refs.get(m.group(2), m.group(2))})",
        text,
    )
    text = RE_REF_IMP.sub(
        lambda m: f"![{m.group(1)}]({refs.get(m.group(1), m.group(1))})",
        text,
    )
    # Reference links
    text = RE_REF_LINK.sub(
        lambda m: f"[{m.group(1)}]({refs.get(m.group(2), m.group(2))})",
        text,
    )
    text = RE_REF_LINK_IMP.sub(
        lambda m: (
            f"[{m.group(1)}]({refs[m.group(1)]})"
            if m.group(1) in refs
            else m.group(0)
        ),
        text,
    )
    # Remove reference definitions
    text = RE_REF_CLEAN.sub("", text)
    return text


# ---------------------------------------------------------------------------
# Image link normalisation
# ---------------------------------------------------------------------------

def normalize_local_image_ref(raw_url: Optional[str]) -> str:
    """
    Convert an image URL / path from Markdown or HTML src to a local path token.

    Strips URL encoding, optional ``file://`` prefix, and inline title strings.
    Returns an empty string for ``None`` or empty input.
    """
    if not raw_url:
        return ""
    url = urllib.parse.unquote(str(raw_url)).strip()
    if not url:
        return ""

    # Strip optional inline title: path "title" / path 'title'
    for q in ('"', "'"):
        if q in url:
            head = url[: url.find(q)].rstrip()
            if head:
                url = head
                break

    if url.startswith("<") and url.endswith(">"):
        url = url[1:-1].strip()

    if url.lower().startswith("file://"):
        parsed = urllib.parse.urlparse(url)
        fp = urllib.parse.unquote(parsed.path or "")
        if fp:
            return fp

    return url


# ---------------------------------------------------------------------------
# TextBundle image-link restoration (import side)
# ---------------------------------------------------------------------------

def restore_image_links_md(text: str, assets_path: str, export_path: str) -> str:
    """
    Rewrite ``![alt](BearImages/UUID/file)`` → ``![alt](file)`` so that
    Bear receives clean filenames on import.
    """
    import os
    import re as _re

    relative_asset_path = os.path.relpath(assets_path, export_path)
    pat = _re.compile(
        r"!\[(.*?)\]\(" + _re.escape(relative_asset_path) + r"/(.+?)/(.+?)\)"
    )
    return pat.sub(r"![\1](\3)", text)


def restore_image_links_tb(text: str) -> str:
    """
    Rewrite TextBundle asset links ``![alt](assets/UUID_file)`` → ``![alt](file)``.
    """
    from b2ou.constants import RE_TB_ASSET_IMG
    return RE_TB_ASSET_IMG.sub(r"![\1](\2)", text)


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

def insert_link_top_note(text: str, message: str, uuid: str) -> str:
    """Insert a Bear back-link on the second line of *text*."""
    lines = text.split("\n")
    title = RE_HEADING.sub("", lines[0])
    link = (
        f"::{message}"
        f"[{title}](bear://x-callback-url/open-note?id={uuid})::"
    )
    lines.insert(1, link)
    return "\n".join(lines)


def first_heading(text: str) -> str:
    """Return the first non-empty line stripped of any Markdown heading prefix."""
    for line in text.splitlines():
        line = line.strip()
        if line:
            return RE_MD_HEADING.sub("", line).strip()
    return ""
