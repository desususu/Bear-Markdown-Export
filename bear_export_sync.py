# encoding=utf-8
# python3.6
# bear_export_sync.py
# Developed with Visual Studio Code with MS Python Extension.

import shlex
import objc
import os
import base64
from AppKit import NSWorkspace, NSWorkspaceOpenConfiguration, NSURL

'''
# Markdown export from Bear sqlite database
Version 1.4-optimized
modified by: github/andymatuschak, andy_matuschak@twitter
original author: github/rovest, rorves@twitter
'''

make_tag_folders = False
multi_tag_folders = True
hide_tags_in_comment_block = False
only_export_these_tags = []

import sqlite3
import datetime
import re
import subprocess
import urllib.parse
import time
import shutil
import fnmatch
import json
import argparse

# ---------------------------------------------------------------------------
# Constants pre-computed once at module load
# ---------------------------------------------------------------------------
_CORE_DATA_EPOCH_OFFSET = 365.25 * 24 * 3600 * 31 + 3600 * 6  # ~31 years + 6h

# Pre-compiled regex patterns (major performance win on repeated calls)
RE_BEAR_ID_NEW   = re.compile(r'\[\/\/\]: # \(\{BearID:(.+?)\}\)\n?')
RE_BEAR_ID_OLD   = re.compile(r'\<\!-- ?\{BearID\:(.+?)\} ?--\>\n?')
RE_BEAR_ID_FIND_NEW = re.compile(r'\[\/\/\]: # \(\{BearID:(.+?)\}\)')
RE_BEAR_ID_FIND_OLD = re.compile(r'\<\!-- ?\{BearID\:(.+?)\} ?--\>')
RE_MD_IMAGE      = re.compile(r'!\[(.*?)\]\(([^)]+)\)')
RE_WIKI_IMAGE    = re.compile(r'!\[\[(.*?)\]\]')
RE_BEAR_IMAGE    = re.compile(r'\[image:(.+?)\]')
RE_BEAR_IMG_SUB  = re.compile(r'\[image:(.+?)/(.+?)\]')
RE_TAG_PATTERN1  = re.compile(r'(?<!\S)\#([.\w\/\-]+)[ \n]?(?!([\/ \w]+\w[#]))')
RE_TAG_PATTERN2  = re.compile(r'(?<![\S])\#([^ \d][.\w\/ ]+?)\#([ \n]|$)')
RE_REF_DEF       = re.compile(r'^\[(?!\/\/)([^\]]+)\]:\s*(\S+).*$', re.MULTILINE)
RE_REF_IMG       = re.compile(r'!\[([^\]]*)\]\[([^\]]+)\]')
RE_REF_IMP       = re.compile(r'!\[([^\[\]]+)\](?!\()')
RE_REF_CLEAN     = re.compile(r'^\[(?!\/\/)[^\]]+\]:\s*\S+.*$\n?', re.MULTILINE)
RE_HIDE_TAGS     = re.compile(r'(\n)[ \t]*(\#[^\s#].*)')
RE_HEADING       = re.compile(r'^#{1,6} ')
RE_MD_HEADING    = re.compile(r'^#+\s*')
RE_UUID_DIR      = re.compile(r'/[0-9A-F]{8}-([0-9A-F]{4}-){3}[0-9A-F]{12}/', re.IGNORECASE)
RE_UUID_ASSET    = re.compile(r'assets/[0-9A-F]{8}-([0-9A-F]{4}-){3}[0-9A-F]{12}_', re.IGNORECASE)
RE_UUID_FILENAME = re.compile(r'(?i)^[0-9A-F]{8}-([0-9A-F]{4}-){3}[0-9A-F]{12}_')
RE_TB_ASSET_IMG  = re.compile(r'!\[(.*?)\]\(assets/.+?_(.+?)( ".+?")?\) ?')
RE_CLEAN_TITLE   = re.compile(r'[\/\\:]')
RE_TRAILING_DASH = re.compile(r'-$')
RE_IMAGE_UUID_PREFIX = re.compile(r'[0-9A-F]{8}-([0-9A-F]{4}-){3}[0-9A-F]{12}', re.IGNORECASE)

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
HOME = os.getenv('HOME', '')
default_out_folder    = os.path.join(HOME, "Work", "BearNotes")
default_backup_folder = os.path.join(HOME, "Work", "BearSyncBackup")

parser = argparse.ArgumentParser(description="Sync Bear notes")
parser.add_argument("--out",       default=default_out_folder,    help="Path where Bear notes will be synced")
parser.add_argument("--backup",    default=default_backup_folder, help="Path where conflicts will be backed up (must be outside --out)")
parser.add_argument("--images",    default=None,                   help="Path where images will be stored")
parser.add_argument("--skipImport", action="store_const", const=True, default=False)
parser.add_argument("--skipExport", action="store_const", const=True, default=False,
                    help="Only run the import phase; skip export to disk entirely.")
parser.add_argument("--excludeTag", action="append", default=[],  help="Don't export notes with this tag. Repeatable.")
parser.add_argument("--hideTags",  action="store_const", const=True, default=False)
parser.add_argument("--format",    choices=['tb', 'md'], default='md')

parsed_args = vars(parser.parse_args())

if parsed_args.get("format") == 'tb':
    export_as_textbundles   = True
    export_as_hybrids       = True
    export_image_repository = False
else:
    export_as_textbundles   = False
    export_as_hybrids       = False
    export_image_repository = True

set_logging_on          = True
export_path             = parsed_args.get("out")
no_export_tags          = parsed_args.get("excludeTag")
hide_tags_in_comment_block = parsed_args.get("hideTags")
multi_export            = [(export_path, True)]

temp_path    = os.path.join(HOME, 'Temp', 'BearExportTemp')
bear_db      = os.path.join(HOME,
    'Library/Group Containers/9K33E3U3T4.net.shinyfrog.bear/Application Data/database.sqlite')
sync_backup  = parsed_args.get("backup")
log_file     = os.path.join(sync_backup, 'bear_export_sync_log.txt')

bear_image_path = os.path.join(HOME,
    'Library/Group Containers/9K33E3U3T4.net.shinyfrog.bear/Application Data/Local Files/Note Images')
assets_path  = parsed_args.get("images") if parsed_args.get("images") else os.path.join(export_path, 'BearImages')

sync_ts      = '.sync-time.log'
export_ts    = '.export-time.log'

sync_ts_file       = os.path.join(export_path, sync_ts)
sync_ts_file_temp  = os.path.join(temp_path,   sync_ts)
export_ts_file_exp = os.path.join(export_path, export_ts)
export_ts_file     = os.path.join(temp_path,   export_ts)

gettag_sh  = os.path.join(HOME, 'temp/gettag.sh')
gettag_txt = os.path.join(HOME, 'temp/gettag.txt')

# NSWorkspace configuration — created once
open_config = NSWorkspaceOpenConfiguration.alloc().init()
open_config.setActivates_(False)


# ===========================================================================
# Main entry
# ===========================================================================

def main():
    init_gettag_script()
    if not parsed_args.get("skipImport"):
        sync_md_updates()
    if parsed_args.get("skipExport"):
        # Import-only mode: no export, no timestamp update.
        exit(0)
    if check_db_modified():
        delete_old_temp_files()
        note_count = export_markdown()
        write_time_stamp()
        rsync_files_from_temp()
        # copy_bear_images() is removed: process_image_links() now copies
        # images directly and incrementally during the export loop itself,
        # so there is no longer a need for a separate rsync of the image store.
        write_log(str(note_count) + ' notes exported to: ' + export_path)
        exit(1)
    else:
        print('*** No notes needed exports')
        exit(0)


# ===========================================================================
# Logging
# ===========================================================================

def write_log(message):
    if not set_logging_on:
        return
    os.makedirs(sync_backup, exist_ok=True)
    time_stamp = datetime.datetime.now().strftime("%Y-%m-%d at %H:%M:%S")
    message = message.replace(export_path + '/', '')
    with open(log_file, 'a', encoding='utf-8') as f:
        f.write(time_stamp + ': ' + message + '\n')


# ===========================================================================
# Database / timestamp helpers
# ===========================================================================

def dt_conv(dtnum):
    """Convert Core Data timestamp (seconds since 2001-01-01) to Unix timestamp."""
    return dtnum + _CORE_DATA_EPOCH_OFFSET


def date_time_conv(dtnum):
    return datetime.datetime.fromtimestamp(dt_conv(dtnum)).strftime(' - %Y-%m-%d_%H%M')


def time_stamp_ts(ts):
    return datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d at %H:%M')


def date_conv(dtnum):
    return datetime.datetime.fromtimestamp(dtnum).strftime('%Y-%m-%d')


def get_file_date(filename):
    try:
        return os.path.getmtime(filename)
    except:
        return 0


def check_db_modified():
    if not os.path.exists(sync_ts_file):
        return True
    return get_file_date(bear_db) > get_file_date(export_ts_file_exp)


# ===========================================================================
# File I/O helpers
# ===========================================================================

def write_file(filename, file_content, modified, created):
    # Record whether the file is new *before* we (re-)create it.
    # SetFile sets the Finder creation date and costs ~50 ms per call
    # (a full subprocess spawn). For existing files the creation date
    # is already correct, so we only pay that cost once per new note.
    is_new_file = not os.path.exists(filename)

    with open(filename, "w", encoding='utf-8') as f:
        f.write(file_content)
    if modified > 0:
        os.utime(filename, (-1, modified))
    if created > 0 and is_new_file:
        newnum   = dt_conv(created)
        dtdate   = datetime.datetime.fromtimestamp(newnum)
        datestr  = dtdate.strftime("%m/%d/%Y %H:%M:%S")
        command  = 'SetFile -d "' + datestr + '" ' + shlex.quote(filename)
        subprocess.call(command, shell=True)


def read_file(file_name):
    with open(file_name, "r", encoding='utf-8') as f:
        return f.read()


def clean_title(title):
    title = title[:225].strip() or "Untitled"
    title = RE_CLEAN_TITLE.sub('-', title)
    title = RE_TRAILING_DASH.sub('', title)
    return title.strip()


# ===========================================================================
# Export phase helpers
# ===========================================================================

def delete_old_temp_files():
    if os.path.exists(temp_path) and "BearExportTemp" in temp_path:
        shutil.rmtree(temp_path)
    os.makedirs(temp_path)


def rsync_files_from_temp():
    for (dest_path, delete) in multi_export:
        os.makedirs(dest_path, exist_ok=True)
        if delete:
            subprocess.call(['rsync', '-r', '-t', '--crtimes', '-E', '--delete',
                             '--exclude', 'BearImages/',
                             '--exclude', '.obsidian/',
                             '--exclude', '.Ulysses*',
                             '--exclude', '*.Ulysses_Public_Filter',
                             temp_path + "/", dest_path])
        else:
            subprocess.call(['rsync', '-r', '-t', '-E',
                             temp_path + "/", dest_path])


# copy_bear_images() has been removed.
#
# The old approach synced the entire bear_image_path directory via rsync.
# It was broken by a timestamp race: write_time_stamp() + rsync updated
# export_ts_file_exp to "now" before the find-newer check ran, so every
# existing image appeared older than the reference file, and rsync was
# always skipped for Bear-added images.
#
# Image copying is now handled directly inside process_image_links() using
# an incremental mtime check per file, which is both correct and faster.


def write_time_stamp():
    msg = "Markdown from Bear written at: " + datetime.datetime.now().strftime("%Y-%m-%d at %H:%M:%S")
    write_file(export_ts_file, msg, 0, 0)
    write_file(sync_ts_file_temp, msg, 0, 0)


def hide_tags(md_text):
    if hide_tags_in_comment_block:
        md_text = RE_HIDE_TAGS.sub(r'\1', md_text)
    return md_text


def restore_tags(md_text):
    # hide_tags_in_comment_block strips the tag lines on export;
    # restoring them on import is not straightforward without storing them —
    # this function is intentionally a no-op (tags are not recoverable from
    # the stripped file).
    return md_text


# ===========================================================================
# Export: main export loop
# ===========================================================================

def export_markdown():
    temp_db_path = os.path.join(temp_path, 'database_copy.sqlite')
    try:
        shutil.copy2(bear_db, temp_db_path)
    except Exception as e:
        print(f"Warning: could not copy database, reading live DB. Error: {e}")
        temp_db_path = bear_db

    note_count = 0
    try:
        with sqlite3.connect(temp_db_path) as conn:
            conn.row_factory = sqlite3.Row
            # Select only the columns we actually use
            query = (
                "SELECT ZTITLE, ZTEXT, ZCREATIONDATE, ZMODIFICATIONDATE, "
                "       ZUNIQUEIDENTIFIER, Z_PK "
                "FROM ZSFNOTE "
                "WHERE ZTRASHED = 0 AND ZARCHIVED = 0"
            )
            rows = conn.execute(query).fetchall()

            for row in rows:
                title    = row['ZTITLE']
                md_text  = row['ZTEXT'].rstrip()
                creation = row['ZCREATIONDATE']
                modified = row['ZMODIFICATIONDATE']
                uuid     = row['ZUNIQUEIDENTIFIER']
                pk       = row['Z_PK']
                filename = clean_title(title)

                if make_tag_folders:
                    file_list = sub_path_from_tag(temp_path, filename, md_text)
                else:
                    is_excluded = any(("#" + tag) in md_text for tag in no_export_tags)
                    file_list = [] if is_excluded else [os.path.join(temp_path, filename)]

                if not file_list:
                    continue

                mod_dt  = dt_conv(modified)
                md_text = hide_tags(md_text)

                # Inject hidden BearID on the second line
                lines = md_text.split('\n', 1)
                if len(lines) > 1:
                    md_text = f"{lines[0]}\n[//]: # ({{BearID:{uuid}}})\n{lines[1]}"
                else:
                    md_text = f"{md_text}\n[//]: # ({{BearID:{uuid}}})"

                for filepath in file_list:
                    note_count += 1

                    # ── Incremental skip ─────────────────────────────────────
                    # If the exported file already exists and its mtime >= this
                    # note's Bear modification time, the note has not changed
                    # since the last export.  Copy the existing output to temp
                    # (so rsync keeps it) and skip the full regeneration
                    # (image queries, text processing, etc.).
                    if not export_as_textbundles:
                        existing_md = filepath.replace(temp_path, export_path) + '.md'
                        if (os.path.exists(existing_md)
                                and os.path.getmtime(existing_md) >= mod_dt):
                            shutil.copy2(existing_md, filepath + '.md')
                            continue
                    else:
                        dest_base   = filepath.replace(temp_path, export_path)
                        existing_tb = dest_base + '.textbundle'
                        existing_md = dest_base + '.md'
                        if (os.path.isdir(existing_tb)
                                and os.path.getmtime(existing_tb) >= mod_dt):
                            dest_tb = filepath + '.textbundle'
                            try:
                                shutil.copytree(existing_tb, dest_tb)
                                continue
                            except Exception:
                                # copytree can fail on duplicate cleaned
                                # titles (FileExistsError), macOS resource
                                # forks, etc.  Clean up any partial copy
                                # and fall through to a full rebuild.
                                if os.path.isdir(dest_tb):
                                    shutil.rmtree(dest_tb)
                        elif (os.path.exists(existing_md)
                                and os.path.getmtime(existing_md) >= mod_dt):
                            shutil.copy2(existing_md, filepath + '.md')
                            continue

                    if export_as_textbundles:
                        if check_image_hybrid(md_text, filepath):
                            make_text_bundle(md_text, filepath, mod_dt, conn, pk)
                        else:
                            write_file(filepath + '.md', md_text, mod_dt, creation)
                    elif export_image_repository:
                        md_proc = process_image_links(md_text, filepath, conn, pk)
                        write_file(filepath + '.md', md_proc, mod_dt, creation)
                    else:
                        write_file(filepath + '.md', md_text, mod_dt, creation)
    finally:
        if os.path.exists(temp_db_path) and temp_db_path != bear_db:
            os.remove(temp_db_path)

    return note_count


def check_image_hybrid(md_text, filepath):
    if not export_as_hybrids:
        return True
    dest_filepath = filepath.replace(temp_path, export_path)
    if os.path.exists(dest_filepath + '.textbundle'):
        return True
    return bool(RE_BEAR_IMAGE.search(md_text) or RE_MD_IMAGE.search(md_text))


def make_text_bundle(md_text, filepath, mod_dt, conn, pk):
    bundle_path  = filepath + '.textbundle'
    bundle_assets = os.path.join(bundle_path, 'assets')
    os.makedirs(bundle_assets, exist_ok=True)

    uuid_match = RE_BEAR_ID_FIND_NEW.search(md_text)
    uuid_str   = uuid_match.group(1) if uuid_match else ""

    info = f'''{{"transient":true,"type":"net.daringfireball.markdown","version":2,"creatorIdentifier":"net.shinyfrog.bear","bear_uuid":"{uuid_str}"}}'''

    if uuid_str:
        write_file(os.path.join(bundle_path, '.bearid'), uuid_str, mod_dt, 0)

    # Copy Bear-native [image:...] images
    for match in RE_BEAR_IMAGE.findall(md_text):
        image_name = match
        new_name   = image_name.replace('/', '_')
        source     = os.path.join(bear_image_path, image_name)
        target     = os.path.join(bundle_assets, new_name)
        if os.path.exists(source):
            shutil.copy2(source, target)
    md_text = RE_BEAR_IMG_SUB.sub(r'![](assets/\1_\2)', md_text)

    # Build UUID→filename map for this note's attached files
    image_map = {}
    for row in conn.execute("SELECT ZFILENAME, ZUNIQUEIDENTIFIER FROM ZSFNOTEFILE WHERE ZNOTE = ?", (pk,)):
        image_map[row["ZFILENAME"]] = row["ZUNIQUEIDENTIFIER"]

    def replace_markdown_image(m):
        alt_text  = m.group(1)
        image_url = m.group(2)
        if image_url.startswith("http"):
            return m.group(0)
        # Skip images already rewritten by the Bear 1.x [image:] loop above
        if image_url.startswith("assets/"):
            return m.group(0)
        image_filename = urllib.parse.unquote(image_url)

        # Use basename for the lookup — the image_map keys are bare
        # filenames (ZFILENAME), but image_url may carry a path prefix
        # (e.g. "assets/..." from a prior textbundle export, or "./"
        # from an external editor).  This mirrors process_image_links().
        file_uuid      = image_map.get(os.path.basename(image_filename))
        if file_uuid:
            basename = os.path.basename(image_filename)
            source   = os.path.join(bear_image_path, file_uuid, basename)
            new_name = f"{file_uuid}_{basename}"
            target   = os.path.join(bundle_assets, new_name)
            if os.path.exists(source):
                shutil.copy2(source, target)
            return f"![{alt_text}]({urllib.parse.quote(f'assets/{new_name}')})"
        return m.group(0)

    md_text = RE_MD_IMAGE.sub(replace_markdown_image, md_text)

    write_file(bundle_path + '/text.md',  md_text, mod_dt, 0)
    write_file(bundle_path + '/info.json', info,   mod_dt, 0)
    os.utime(bundle_path, (-1, mod_dt))


def sub_path_from_tag(temp_path, filename, md_text):
    tags = []
    if multi_tag_folders:
        tags.extend(m[0] for m in RE_TAG_PATTERN1.findall(md_text))
        tags.extend(m[0] for m in RE_TAG_PATTERN2.findall(md_text))
        if not tags:
            return [os.path.join(temp_path, filename)]
    else:
        m1 = RE_TAG_PATTERN1.search(md_text)
        m2 = RE_TAG_PATTERN2.search(md_text)
        if m1 and m2:
            tag = m1.group(1) if m1.start(1) < m2.start(1) else m2.group(1)
        elif m1:
            tag = m1.group(1)
        elif m2:
            tag = m2.group(1)
        else:
            return [os.path.join(temp_path, filename)]
        tags = [tag]

    paths = [os.path.join(temp_path, filename)]
    for tag in tags:
        if tag == '/':
            continue
        if only_export_these_tags:
            if not any(tag.lower().startswith(et.lower()) for et in only_export_these_tags):
                continue
        if any(tag.lower().startswith(nt.lower()) for nt in no_export_tags):
            return []
        sub_path = ('_' + tag[1:]) if tag.startswith('.') else tag
        tag_path = os.path.join(temp_path, sub_path)
        os.makedirs(tag_path, exist_ok=True)
        paths.append(os.path.join(tag_path, filename))
    return paths


def process_image_links(md_text, filepath, conn, pk):
    """
    Rewrite image links in the exported markdown to point at assets_path,
    AND directly copy the image files there (incrementally).

    Handles two Bear image formats:
      • Bear 1.x: [image:UUID/filename]  stored at bear_image_path/UUID/filename
      • Bear 2.x: ![alt](filename)       linked via ZSFNOTEFILE UUID lookup

    Files are copied only when the source is newer than the destination
    (mtime-based incremental), so repeated exports do not re-copy unchanged
    images.  This replaces the old copy_bear_images() rsync approach, which
    was unreliable because its find-newer gate used a timestamp that had
    already been updated to "now" before the check ran.
    """
    # Build filename → UUID map once (outside any closure) for Bear 2.x images
    image_file_map: dict = {}
    for row in conn.execute(
        "SELECT ZFILENAME, ZUNIQUEIDENTIFIER FROM ZSFNOTEFILE WHERE ZNOTE = ?", (pk,)
    ):
        image_file_map[row["ZFILENAME"]] = row["ZUNIQUEIDENTIFIER"]

    rel_assets = os.path.relpath(assets_path, export_path)

    def _copy_incremental(source: str, dest: str) -> None:
        """Copy source → dest only when source is newer; create dirs as needed."""
        if not os.path.exists(source):
            return
        if os.path.exists(dest) and os.path.getmtime(dest) >= os.path.getmtime(source):
            return
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(source, dest)

    # ── Bear 1.x: [image:UUID/filename] ─────────────────────────────────────
    def rewrite_bear1_image(m):
        ref = m.group(1)              # "UUID/filename"
        parts = ref.split('/', 1)
        if len(parts) != 2:
            return m.group(0)
        img_uuid, img_filename = parts
        source = os.path.join(bear_image_path, img_uuid, img_filename)
        dest   = os.path.join(assets_path, img_uuid, img_filename)
        _copy_incremental(source, dest)
        rel = f"{rel_assets}/{img_uuid}/{img_filename}"
        return f"![]({urllib.parse.quote(rel)})"

    md_text = RE_BEAR_IMAGE.sub(rewrite_bear1_image, md_text)

    # ── Bear 2.x: ![alt](filename) ──────────────────────────────────────────
    def rewrite_md_image(m):
        img_url = m.group(2)
        if img_url.startswith("http"):
            return m.group(0)

        img_filename = urllib.parse.unquote(img_url)

        # Skip if the link already points inside assets_path (already exported)
        if img_filename.startswith(rel_assets + '/'):
            return m.group(0)

        file_uuid = image_file_map.get(os.path.basename(img_filename))
        if file_uuid is None:
            # Not in DB — leave link unchanged
            return m.group(0)

        basename = os.path.basename(img_filename)
        source   = os.path.join(bear_image_path, file_uuid, basename)
        dest     = os.path.join(assets_path, file_uuid, basename)
        _copy_incremental(source, dest)
        rel = f"{rel_assets}/{file_uuid}/{basename}"
        return f"![{m.group(1)}]({urllib.parse.quote(rel)})"

    return RE_MD_IMAGE.sub(rewrite_md_image, md_text)


def restore_image_links(md_text):
    if export_as_textbundles:
        return RE_TB_ASSET_IMG.sub(r'![\1](\2)', md_text)
    elif export_image_repository:
        relative_asset_path = os.path.relpath(assets_path, export_path)
        pat = re.compile(
            r'!\[(.*?)\]\(' + re.escape(relative_asset_path) + r'/(.+?)/(.+?)\)'
        )
        return pat.sub(r'![\1](\3)', md_text)
    return md_text


# ===========================================================================
# Import phase helpers
# ===========================================================================

def sync_md_updates():
    if not os.path.exists(sync_ts_file) or not os.path.exists(export_ts_file_exp):
        return False

    ts_last_sync   = os.path.getmtime(sync_ts_file)
    ts_last_export = os.path.getmtime(export_ts_file_exp)

    current_sync_ts = time.time()
    update_sync_time_file(current_sync_ts)

    # Build a vault-wide filename→path index once so per-image searches are O(1)
    vault_index = _build_vault_index(export_path)

    updates_found = False
    file_types    = ('*.md', '*.txt', '*.markdown')

    for (root, dirnames, filenames) in os.walk(export_path):
        if '.obsidian' in dirnames:
            dirnames.remove('.obsidian')
        for pattern in file_types:
            for filename in fnmatch.filter(filenames, pattern):
                md_file = os.path.join(root, filename)
                ts      = os.path.getmtime(md_file)
                if ts > ts_last_sync:
                    if not updates_found:
                        time.sleep(1)
                    updates_found = True
                    md_text = read_file(md_file)
                    md_text = convert_ref_links_to_inline(md_text)
                    backup_ext_note(md_file)
                    if '.textbundle' in md_file:
                        textbundle_to_bear(md_text, md_file, ts)
                        write_log('Imported to Bear: ' + md_file)
                    else:
                        update_bear_note(md_text, md_file, ts, ts_last_export, vault_index)
                        write_log('Bear Note Updated: ' + md_file)

    return updates_found


def _build_vault_index(root_path):
    """
    Walk the vault once and return a dict mapping filename → absolute path.
    When multiple files share the same name, the first one wins (shallowest).
    """
    index = {}
    for dirpath, dirnames, filenames in os.walk(root_path):
        if '.obsidian' in dirnames:
            dirnames.remove('.obsidian')
        for fname in filenames:
            if fname not in index:
                index[fname] = os.path.join(dirpath, fname)
    return index


def convert_ref_links_to_inline(md_text):
    refs = dict(RE_REF_DEF.findall(md_text))
    if not refs:
        return md_text
    md_text = RE_REF_IMG.sub(lambda m: f"![{m.group(1)}]({refs.get(m.group(2), m.group(2))})", md_text)
    md_text = RE_REF_IMP.sub(lambda m: f"![{m.group(1)}]({refs.get(m.group(1), m.group(1))})", md_text)
    md_text = RE_REF_CLEAN.sub('', md_text)
    return md_text


def update_bear_note(md_text, md_file, ts, ts_last_export, vault_index=None):
    md_text = restore_tags(md_text)
    md_text = restore_image_links(md_text)

    match_new = RE_BEAR_ID_FIND_NEW.search(md_text)
    match_old = RE_BEAR_ID_FIND_OLD.search(md_text)

    if match_new or match_old:
        if match_new:
            uuid    = match_new.group(1)
            md_text = RE_BEAR_ID_NEW.sub('', md_text)
        else:
            uuid    = match_old.group(1)
            md_text = RE_BEAR_ID_OLD.sub('', md_text)

        md_text = md_text.lstrip() + '\n'

        # FIX: check conflict BEFORE uploading images (images update Bear's mod time)
        sync_conflict = check_sync_conflict(uuid, ts_last_export)
        md_text       = process_md_images(md_text, md_file, uuid=uuid, vault_index=vault_index)

        if sync_conflict:
            link_original = 'bear://x-callback-url/open-note?id=' + uuid
            message = ('::Sync conflict! External update: ' + time_stamp_ts(ts) + '::'
                       + '\n[Click here to see original Bear note](' + link_original + ')')
            x_create = 'bear://x-callback-url/create?show_window=no&open_note=no'
            bear_x_callback(x_create, md_text, message, '')
        else:
            orig_title = backup_bear_note(uuid)
            x_replace  = ('bear://x-callback-url/add-text?show_window=no&open_note=no'
                          '&mode=replace_all&id=' + uuid)
            bear_x_callback(x_replace, md_text, '', orig_title)
    else:
        first_line    = next((l.strip() for l in md_text.splitlines() if l.strip()), '')
        note_title    = RE_MD_HEADING.sub('', first_line).strip()
        recovered_uuid = lookup_uuid_by_title(note_title)

        if recovered_uuid:
            md_text    = process_md_images(md_text, md_file, uuid=recovered_uuid, vault_index=vault_index)
            orig_title = backup_bear_note(recovered_uuid)
            x_replace  = ('bear://x-callback-url/add-text?show_window=no&open_note=no'
                          '&mode=replace_all&id=' + recovered_uuid)
            bear_x_callback(x_replace, md_text, '', orig_title)
        else:
            # Entirely new note created outside Bear (e.g. in Obsidian)
            md_text_tags = get_tag_from_path(md_text, md_file, export_path)
            x_create     = 'bear://x-callback-url/create?show_window=no'
            bear_x_callback(x_create, md_text_tags, '', '')
            time.sleep(1.0)

            new_uuid       = lookup_uuid_by_title(note_title)
            final_md_text  = process_md_images(md_text_tags, md_file, uuid=new_uuid,
                                               note_title=note_title, vault_index=vault_index)

            if final_md_text != md_text_tags:
                if new_uuid:
                    x_replace = ('bear://x-callback-url/add-text?show_window=no&open_note=no'
                                 '&mode=replace_all&id=' + new_uuid)
                else:
                    safe_title = urllib.parse.quote(note_title)
                    x_replace  = ('bear://x-callback-url/add-text?show_window=no&open_note=no'
                                  '&mode=replace_all&title=' + safe_title)
                bear_x_callback(x_replace, final_md_text, '', '')


def process_md_images(md_text, md_file, uuid=None, note_title=None, vault_index=None):
    """
    For each image reference in md_text:
      - Skip remote URLs unchanged
      - Identify whether it is already a Bear-exported image (no re-upload needed)
      - Otherwise upload the image to Bear via x-callback-url and rewrite the link
    vault_index: pre-built {filename: abs_path} map to avoid repeated os.walk calls.
    """
    md_dir = os.path.dirname(md_file)

    def resolve_image_path(img_path_unquoted):
        """Return absolute path of the image, or None if not found."""
        # 1. Relative to the markdown file's directory
        candidate = os.path.normpath(os.path.join(md_dir, img_path_unquoted))
        if os.path.exists(candidate):
            return candidate
        # 2. In the BearImages folder
        candidate = os.path.join(export_path, 'BearImages', os.path.basename(img_path_unquoted))
        if os.path.exists(candidate):
            return candidate
        # 3. Vault-wide lookup via pre-built index
        target_name = os.path.basename(img_path_unquoted)
        if vault_index and target_name in vault_index:
            return vault_index[target_name]
        # 4. Fallback: fresh os.walk (only triggered if vault_index not supplied)
        if vault_index is None:
            for root, dirs, files in os.walk(export_path):
                if '.obsidian' in dirs:
                    dirs.remove('.obsidian')
                if target_name in files:
                    return os.path.join(root, target_name)
        return None

    def upload_and_format(alt_text, img_path):
        if img_path.startswith('http'):
            return f"![{alt_text}]({img_path})"

        img_path_unquoted = urllib.parse.unquote(img_path)
        abs_img_path      = resolve_image_path(img_path_unquoted)

        if abs_img_path is None:
            return f"![{alt_text}]({urllib.parse.quote(img_path_unquoted)})"

        img_filename = os.path.basename(abs_img_path)

        # Determine whether this is already a Bear-exported image (skip re-upload)
        normalised = '/' + img_path_unquoted.replace('\\', '/')
        is_bear_dir   = bool(RE_UUID_DIR.search(normalised))
        is_tb_asset   = bool(RE_UUID_ASSET.search(normalised))
        is_bear_image = is_bear_dir or is_tb_asset

        if not is_bear_image and (uuid or note_title):
            try:
                with open(abs_img_path, "rb") as fh:
                    encoded = base64.b64encode(fh.read()).decode("utf-8")

                safe_filename = urllib.parse.quote(img_filename)
                safe_file     = urllib.parse.quote(encoded, safe='')

                if uuid:
                    x_add_file = (f"bear://x-callback-url/add-file?show_window=no&open_note=no"
                                  f"&id={uuid}&filename={safe_filename}&mode=append&file={safe_file}")
                else:
                    safe_title = urllib.parse.quote(note_title)
                    x_add_file = (f"bear://x-callback-url/add-file?show_window=no&open_note=no"
                                  f"&title={safe_title}&filename={safe_filename}&mode=append&file={safe_file}")

                url = NSURL.URLWithString_(x_add_file)
                if url is not None:
                    NSWorkspace.sharedWorkspace().openURL_configuration_completionHandler_(url, open_config, None)
                    time.sleep(0.5)
            except Exception as e:
                print(f"Image upload failed for {img_filename}: {e}")

        return f"![{alt_text}]({urllib.parse.quote(img_filename)})"

    new_md = RE_MD_IMAGE.sub(lambda m: upload_and_format(m.group(1), m.group(2)), md_text)
    new_md = RE_WIKI_IMAGE.sub(lambda m: upload_and_format("image", m.group(1)), new_md)
    return new_md


def textbundle_to_bear(md_text, md_file, mod_dt):
    md_text = restore_tags(md_text)
    bundle  = os.path.split(md_file)[0]
    uuid    = None

    # Resolve UUID via multiple fallback strategies
    bearid_path = os.path.join(bundle, '.bearid')
    if os.path.exists(bearid_path):
        uuid = read_file(bearid_path).strip() or None

    if not uuid:
        m = RE_BEAR_ID_FIND_NEW.search(md_text) or RE_BEAR_ID_FIND_OLD.search(md_text)
        if m:
            uuid = m.group(1)

    if not uuid:
        info_path = os.path.join(bundle, 'info.json')
        if os.path.exists(info_path):
            try:
                with open(info_path, 'r', encoding='utf-8') as f:
                    uuid = json.load(f).get("bear_uuid") or None
            except Exception as e:
                print(f"Could not read info.json: {e}")

    if not uuid:
        first_line = next((l.strip() for l in md_text.splitlines() if l.strip()), '')
        note_title = RE_MD_HEADING.sub('', first_line).strip()
        uuid       = lookup_uuid_by_title(note_title)

    if uuid:
        id_tag = f"\n\n[//]: # ({{BearID:{uuid}}})\n"

        clean_md = RE_BEAR_ID_NEW.sub('', md_text)
        clean_md = RE_BEAR_ID_OLD.sub('', clean_md)
        clean_md = clean_md.rstrip() + '\n'

        # Normalise image paths to assets/ prefix for file writes
        def fix_image_path(m):
            filename = urllib.parse.unquote(m.group(2)).split('/')[-1]
            return f"![{m.group(1)}](assets/{urllib.parse.quote(filename)})"

        fixed_md = RE_MD_IMAGE.sub(fix_image_path, clean_md)

        write_file(md_file, fixed_md.rstrip() + id_tag, mod_dt, 0)
        write_file(os.path.join(bundle, '.bearid'), uuid, mod_dt, 0)

        # Keep info.json bear_uuid in sync
        info_path = os.path.join(bundle, 'info.json')
        if os.path.exists(info_path):
            try:
                with open(info_path, 'r', encoding='utf-8') as f:
                    info_data = json.load(f)
                info_data["bear_uuid"] = uuid
                write_file(info_path, json.dumps(info_data, indent=4), mod_dt, 0)
            except:
                pass

        assets_dir = os.path.join(bundle, 'assets')
        os.makedirs(assets_dir, exist_ok=True)

        # Move any loose image files into assets/ and collect new ones to upload
        new_images_to_upload = []
        for m in RE_MD_IMAGE.finditer(clean_md):
            img_filename = urllib.parse.unquote(m.group(2)).split('/')[-1]
            loose_path   = os.path.join(bundle, img_filename)
            asset_path   = os.path.join(assets_dir, img_filename)
            if os.path.exists(loose_path) and not os.path.exists(asset_path):
                shutil.copy2(loose_path, asset_path)
            if not RE_IMAGE_UUID_PREFIX.match(img_filename) and os.path.exists(asset_path):
                new_images_to_upload.append((img_filename, asset_path))

        for filename, filepath in new_images_to_upload:
            try:
                with open(filepath, "rb") as fh:
                    encoded = base64.b64encode(fh.read()).decode("utf-8")
                safe_filename = urllib.parse.quote(filename)
                safe_file     = urllib.parse.quote(encoded, safe='')
                x_add_file = (f"bear://x-callback-url/add-file?show_window=no&open_note=no"
                              f"&id={uuid}&filename={safe_filename}&mode=append&file={safe_file}")
                url = NSURL.URLWithString_(x_add_file)
                if url is not None:
                    NSWorkspace.sharedWorkspace().openURL_configuration_completionHandler_(url, open_config, None)
                    time.sleep(0.3)
            except Exception as e:
                print(f"Image upload failed for {filename}: {e}")

        # Build the Bear-formatted text (strip UUID prefix from filenames)
        def restore_img_format(m):
            filename   = urllib.parse.unquote(m.group(2)).split('/')[-1]
            clean_name = RE_UUID_FILENAME.sub('', filename)
            return f"![{m.group(1)}]({urllib.parse.quote(clean_name)})"

        bear_md = RE_MD_IMAGE.sub(restore_img_format, clean_md)
        x_replace = (f"bear://x-callback-url/add-text?show_window=no&open_note=no"
                     f"&mode=replace_all&id={uuid}"
                     f"&text={urllib.parse.quote(bear_md, safe='')}")
        url = NSURL.URLWithString_(x_replace)
        if url is not None:
            NSWorkspace.sharedWorkspace().openURL_configuration_completionHandler_(url, open_config, None)
            time.sleep(0.5)
    else:
        md_text = get_tag_from_path(md_text, bundle, export_path)
        write_file(md_file, md_text, mod_dt, 0)
        os.utime(bundle, (-1, mod_dt))
        subprocess.call(['open', '-a', 'Bear', bundle])
        time.sleep(0.5)


def backup_ext_note(md_file):
    if '.textbundle' in md_file:
        bundle_path = os.path.split(md_file)[0]
        bundle_name = os.path.split(bundle_path)[1]
        target      = os.path.join(sync_backup, bundle_name)
        bundle_raw  = os.path.splitext(target)[0]
        count = 2
        while os.path.exists(target):
            target = bundle_raw + " - " + str(count).zfill(2) + ".textbundle"
            count += 1
        shutil.copytree(bundle_path, target)
    else:
        shutil.copy2(md_file, sync_backup + '/')


def update_sync_time_file(ts):
    write_file(sync_ts_file,
               "Checked for Markdown updates to sync at: " +
               datetime.datetime.now().strftime("%Y-%m-%d at %H:%M:%S"),
               ts, 0)


# ===========================================================================
# Bear database helpers
# ===========================================================================

def check_sync_conflict(uuid, ts_last_export):
    """Return True if Bear has modified the note since the last export."""
    try:
        with sqlite3.connect(bear_db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT ZMODIFICATIONDATE FROM ZSFNOTE "
                "WHERE ZTRASHED = 0 AND ZUNIQUEIDENTIFIER = ?",
                (uuid,)
            ).fetchone()
        if row:
            return dt_conv(row['ZMODIFICATIONDATE']) > ts_last_export
    except Exception as e:
        print(f"check_sync_conflict error: {e}")
    return False


def backup_bear_note(uuid):
    """Back up the current Bear note to the sync_backup folder. Returns the note title."""
    title = ''
    try:
        with sqlite3.connect(bear_db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT ZTITLE, ZTEXT, ZMODIFICATIONDATE, ZCREATIONDATE "
                "FROM ZSFNOTE WHERE ZUNIQUEIDENTIFIER = ?",
                (uuid,)
            ).fetchone()

        if not row:
            return title

        title    = row['ZTITLE']
        md_text  = row['ZTEXT'].rstrip()
        mod_dt   = dt_conv(row['ZMODIFICATIONDATE'])
        cre_dt   = dt_conv(row['ZCREATIONDATE'])
        md_text  = insert_link_top_note(md_text, 'Link to updated note: ', uuid)
        dtdate   = datetime.datetime.fromtimestamp(cre_dt)
        filename = clean_title(title) + dtdate.strftime(' - %Y-%m-%d_%H%M')

        os.makedirs(sync_backup, exist_ok=True)
        file_part   = os.path.join(sync_backup, filename)
        backup_file = file_part + ".txt"
        count = 2
        while os.path.exists(backup_file):
            backup_file = file_part + " - " + str(count).zfill(2) + ".txt"
            count += 1
        write_file(backup_file, md_text, mod_dt, row['ZCREATIONDATE'])
        write_log('Original to sync_backup: ' + os.path.split(backup_file)[1])
    except Exception as e:
        print(f"backup_bear_note error: {e}")
    return title


def lookup_uuid_by_title(title):
    if not title:
        return None
    try:
        with sqlite3.connect(bear_db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT ZUNIQUEIDENTIFIER FROM ZSFNOTE "
                "WHERE ZTRASHED = 0 AND ZARCHIVED = 0 AND ZTITLE = ? "
                "ORDER BY ZMODIFICATIONDATE DESC LIMIT 1",
                (title,)
            ).fetchone()
        if row:
            found_uuid = row['ZUNIQUEIDENTIFIER']
            write_log(f'UUID recovered via title lookup: "{title}" -> {found_uuid}')
            return found_uuid
    except Exception as e:
        print(f"Title-based UUID lookup failed: {e}")
    return None


# ===========================================================================
# Text helpers
# ===========================================================================

def insert_link_top_note(md_text, message, uuid):
    lines = md_text.split('\n')
    title = RE_HEADING.sub('', lines[0])
    link  = '::' + message + '[' + title + '](bear://x-callback-url/open-note?id=' + uuid + ')::'
    lines.insert(1, link)
    return '\n'.join(lines)


def get_tag_from_path(md_text, md_file, root_path, inbox_for_root=False, extra_tag=''):
    path     = md_file.replace(root_path, '')[1:]
    sub_path = os.path.split(path)[0]
    tags     = []
    if '.textbundle' in sub_path:
        sub_path = os.path.split(sub_path)[0]
    if sub_path == '':
        tag = '#.inbox' if inbox_for_root else ''
    elif sub_path.startswith('_'):
        tag = '#.' + sub_path[1:].strip()
    else:
        tag = '#' + sub_path.strip()
    if ' ' in tag:
        tag += "#"
    if tag:
        tags.append(tag)
    if extra_tag:
        tags.append(extra_tag)
    for t in get_file_tags(md_file):
        t = '#' + t.strip()
        if ' ' in t:
            t += "#"
        tags.append(t)
    return md_text.strip() + '\n\n' + ' '.join(tags) + '\n'


def get_file_tags(md_file):
    try:
        subprocess.call([gettag_sh, md_file, gettag_txt])
        text     = re.sub(r'\\n\d{1,2}', '', read_file(gettag_txt))
        tag_list = json.loads(text)
        return tag_list
    except:
        return []


# ===========================================================================
# Bear x-callback-url
# ===========================================================================

def bear_x_callback(x_command, md_text, message, orig_title):
    if message:
        lines = md_text.splitlines()
        lines.insert(1, message)
        md_text = '\n'.join(lines)
    x_command_text = x_command + '&text=' + urllib.parse.quote(md_text, safe='')
    url = NSURL.URLWithString_(x_command_text)
    if url is not None:
        NSWorkspace.sharedWorkspace().openURL_configuration_completionHandler_(url, open_config, None)
    else:
        print("Warning: could not build NSURL for sync (unusual characters in text?).")
    time.sleep(.2)


# ===========================================================================
# System helpers
# ===========================================================================

def init_gettag_script():
    """Create the macOS-tag-reading shell script only if it doesn't already exist."""
    temp = os.path.join(HOME, 'temp')
    os.makedirs(temp, exist_ok=True)
    if os.path.exists(gettag_sh):
        return  # Already created — skip the write + chmod
    gettag_script = '''\
#!/bin/bash
if [[ ! -e "$1" ]] ; then
    echo 'file missing or not specified'
    exit 0
fi
JSON="$(xattr -p com.apple.metadata:_kMDItemUserTags "$1" 2>/dev/null | xxd -r -p | plutil -convert json - -o - 2>/dev/null)"
echo $JSON > "$2"
'''
    write_file(gettag_sh, gettag_script, 0, 0)
    subprocess.call(['chmod', '777', gettag_sh])


def check_if_image_added(md_text, md_file):
    if '.textbundle/' not in md_file:
        return False
    for image_filename in re.findall(r'!\[.*?\]\(assets/(.+?)\)', md_text):
        if not RE_IMAGE_UUID_PREFIX.match(image_filename):
            return True
    return False


def notify(message):
    title = "ul_sync_md.py"
    try:
        subprocess.call(['/Applications/terminal-notifier.app/Contents/MacOS/terminal-notifier',
                         '-message', message, "-title", title, '-sound', 'default'])
    except:
        write_log('"terminal-notifier.app" is missing!')


if __name__ == '__main__':
    main()