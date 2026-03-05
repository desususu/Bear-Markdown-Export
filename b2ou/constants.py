"""
Compiled regex patterns, file-extension sets, and other module-level constants.

All patterns are compiled once at import time for performance.
"""

import re

# ---------------------------------------------------------------------------
# Core Data / epoch
# ---------------------------------------------------------------------------

# Bear stores timestamps as Core Data "seconds since 2001-01-01 UTC".
# Adding this offset converts them to Unix timestamps.
CORE_DATA_EPOCH: float = 978_307_200.0  # 2001-01-01 00:00:00 UTC

# ---------------------------------------------------------------------------
# BearID markers
# ---------------------------------------------------------------------------

# Current format:  [//]: # ({BearID:UUID})
RE_BEAR_ID_NEW = re.compile(r'\[\/\/\]: # \(\{BearID:(.+?)\}\)\n?')
RE_BEAR_ID_OLD = re.compile(r'\<\!-- ?\{BearID\:(.+?)\} ?--\>\n?')

# Find-only variants (no trailing newline consumption)
RE_BEAR_ID_FIND_NEW = re.compile(r'\[\/\/\]: # \(\{BearID:(.+?)\}\)')
RE_BEAR_ID_FIND_OLD = re.compile(r'\<\!-- ?\{BearID\:(.+?)\} ?--\>')

# ---------------------------------------------------------------------------
# Image references
# ---------------------------------------------------------------------------

RE_MD_IMAGE     = re.compile(r'!\[(.*?)\]\(([^)]+)\)')
RE_WIKI_IMAGE   = re.compile(r'!\[\[(.*?)\]\]')
RE_HTML_IMG_TAG = re.compile(r'<img\b[^>]*>', re.IGNORECASE)
RE_HTML_IMG_SRC = re.compile(r'\bsrc=(["\'])(.*?)\1', re.IGNORECASE)
RE_HTML_IMG_ALT = re.compile(r'\balt=(["\'])(.*?)\1', re.IGNORECASE)

# Bear 1.x inline image syntax: [image:UUID/filename]
RE_BEAR_IMAGE     = re.compile(r'\[image:(.+?)\]')
RE_BEAR_IMG_SUB   = re.compile(r'\[image:(.+?)/(.+?)\]')

# TextBundle exported image: ![alt](assets/UUID_filename "title"?)
RE_TB_ASSET_IMG = re.compile(r'!\[(.*?)\]\(assets/.+?_(.+?)( ".+?")?\) ?')

# ---------------------------------------------------------------------------
# UUID patterns (for detecting already-exported Bear images)
# ---------------------------------------------------------------------------

RE_UUID_DIR      = re.compile(
    r'/[0-9A-F]{8}-([0-9A-F]{4}-){3}[0-9A-F]{12}/', re.IGNORECASE)
RE_UUID_ASSET    = re.compile(
    r'assets/[0-9A-F]{8}-([0-9A-F]{4}-){3}[0-9A-F]{12}_', re.IGNORECASE)
RE_UUID_FILENAME = re.compile(
    r'(?i)^[0-9A-F]{8}-([0-9A-F]{4}-){3}[0-9A-F]{12}_')
RE_IMAGE_UUID_PREFIX = re.compile(
    r'[0-9A-F]{8}-([0-9A-F]{4}-){3}[0-9A-F]{12}', re.IGNORECASE)

# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

# Pattern 1: #tag or #nested/tag followed by space/newline, not part of a URL
RE_TAG_PATTERN1 = re.compile(
    r'(?<!\S)\#([.\w\/\-]+)[ \n]?(?!([\/ \w]+\w[#]))')
# Pattern 2: multi-word tags enclosed in double hashes: #multi word tag#
RE_TAG_PATTERN2 = re.compile(
    r'(?<![\S])\#([^ \d][.\w\/ ]+?)\#([ \n]|$)')

# Hide tags: strip tag lines from exported markdown
RE_HIDE_TAGS = re.compile(r'(\n)[ \t]*(\#[^\s#].*)')

# ---------------------------------------------------------------------------
# Markdown structure
# ---------------------------------------------------------------------------

RE_HEADING    = re.compile(r'^#{1,6} ')
RE_MD_HEADING = re.compile(r'^#+\s*')
RE_CLEAN_TITLE    = re.compile(r'[\/\\:]')
RE_TRAILING_DASH  = re.compile(r'-$')

# Reference-style links
RE_REF_DEF      = re.compile(r'^\[(?!\/\/)([^\]]+)\]:\s*(\S+).*$', re.MULTILINE)
RE_REF_IMG      = re.compile(r'!\[([^\]]*)\]\[([^\]]+)\]')
RE_REF_IMP      = re.compile(r'!\[([^\[\]]+)\](?!\()')
RE_REF_LINK     = re.compile(r'(?<!!)\[([^\]]+)\]\[([^\]]+)\]')
RE_REF_LINK_IMP = re.compile(r'(?<!!)\[([^\[\]]+)\](?!\(|\[|:)')
RE_REF_CLEAN    = re.compile(r'^\[(?!\/\/)[^\]]+\]:\s*\S+.*$\n?', re.MULTILINE)

# ---------------------------------------------------------------------------
# File-system constants
# ---------------------------------------------------------------------------

IMAGE_EXTENSIONS: frozenset[str] = frozenset({
    '.png', '.jpg', '.jpeg', '.gif', '.webp',
    '.heic', '.bmp', '.tif', '.tiff',
})

NOTE_EXTENSIONS: frozenset[str] = frozenset({'.md', '.txt', '.markdown'})

SENTINEL_FILES: frozenset[str] = frozenset({
    '.sync-time.log', '.export-time.log',
})

# Directories always skipped during export-folder walks
EXPORT_SKIP_DIRS: frozenset[str] = frozenset({'BearImages', '.obsidian'})
EXPORT_SKIP_DIR_PREFIXES: tuple[str, ...] = ('.Ulysses',)

# Directories always skipped during import-folder walks
IMPORT_SKIP_DIRS: frozenset[str] = frozenset({
    '.obsidian', 'BearImages', '.git', '__pycache__',
})

# Cloud-sync junk directories (removed before sync)
CLOUD_JUNK_DIRS: frozenset[str] = frozenset({
    "@eaDir", "#recycle", ".SynologyDrive", "@SynoResource",
    ".sync", ".stfolder", ".stversions", "__MACOSX",
    ".dropbox.cache", ".dropbox",
})

# Pre-compiled pattern for cloud-junk files
CLOUD_JUNK_RE = re.compile(
    r"^("
    r"\.DS_Store|\.\_\.DS_Store|\._.*|\.syncloud_.*"
    r"|Thumbs\.db|desktop\.ini|.*\.tmp|~\$.*"
    r"|\.~lock\..*|.*\.swp|.*\.crdownload|\.fuse_hidden.*"
    r"|.*\.partial|.*\.part|\.dropbox|Icon\r"
    r")$"
)

# lsof: system processes that open note files (not editors)
SYSTEM_PROCESS_PREFIXES: tuple[str, ...] = (
    "mds", "mdworker",
    "Finder", "fseventsd", "kernel",
    "SynologyDr", "CloudDrive",
    "Dropbox", "dbfsevent",
    "bird", "cloudd", "nsurlsessi",
    "python", "Python", "rsync",
    "launchd", "loginwindow",
    "com.apple",
    "revisiond", "quicklookd", "iconservi",
    "Spotlight",
)

# Known editor bundle IDs (Layer 3 frontmost-app check)
EDITOR_BUNDLE_IDS: frozenset[str] = frozenset({
    "net.shinyfrog.bear",
    "com.ulyssesapp.mac", "com.soulmen.ulysses3", "com.soulmen.ulysses",
    "md.obsidian",
    "abnerworks.typora", "io.typora.typora",
    "com.microsoft.VSCode", "com.microsoft.VSCodeInsiders",
    "com.sublimetext.4", "com.sublimetext.3",
    "com.apple.TextEdit", "com.apple.dt.Xcode",
    "com.coteditor.CotEditor", "com.macromates.TextMate",
    "com.barebones.bbedit", "com.panic.Nova",
    "org.vim.MacVim", "com.qvacua.VimR",
    "com.todesktop.230313mzl4w4u92",  # Cursor
    "dev.zed.Zed",
    "pro.writer.mac", "co.writer.mac",
    "com.xelaton.marktext-github",
    "com.joplinapp.desktop",
    "com.omz-software.Drafts",
    "io.github.nicehash.zettlr",
    "com.logseq.logseq",
    "com.lukilabs.lukiedit",
})

EDITOR_KEYWORDS: tuple[str, ...] = (
    "ulysses", "obsidian", "typora", "bear", "vscode", "sublime",
    "textedit", "textmate", "bbedit", "nova", "vim", "emacs", "cursor",
    "zed", "ia.writer", "iawriter", "writer.pro", "marktext",
    "joplin", "drafts", "zettlr", "logseq", "craft", "notepad",
)

# Lower-cased set for O(1) lookup
EDITOR_BUNDLE_IDS_LOWER: frozenset[str] = frozenset(
    b.lower() for b in EDITOR_BUNDLE_IDS
)
