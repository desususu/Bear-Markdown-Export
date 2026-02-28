# encoding=utf-8
# python3.6
# bear_export_sync.py
# Developed with Visual Studio Code with MS Python Extension.

import shlex
import objc
from AppKit import NSWorkspace, NSWorkspaceOpenConfiguration, NSURL

'''
# Markdown export from Bear sqlite database 
Version 1.4, 2020-01-11
modified by: github/andymatuschak, andy_matuschak@twitter
original author: github/rovest, rorves@twitter

See also: bear_import.py for auto import to bear script.

## Sync external updates:
First checks for changes in external Markdown files (previously exported from Bear)
* Replacing text in original note with callback-url replace command   
  (Keeping original creation date)
  If changes in title it will be added just below original title
* New notes are added to Bear (with x-callback-url command)
* New notes get tags from sub folder names, or `#.inbox` if root
* Backing up original note as file to BearSyncBackup folder  
  (unless a sync conflict, then both notes will be there)

## Export:
Then exporting Markdown from Bear sqlite db.
* check_if_modified() on database.sqlite to see if export is needed
* Uses rsync for copying, so only markdown files of changed sheets will be updated  
  and synced by Dropbox (or other sync services)
* "Hides" tags with `period+space` on beginning of line: `. #tag` not appear as H1 in other apps.   
  (This is removed if sync-back above)
* Or instead hide tags in HTML comment blocks like: `<!-- #mytag -->` if `hide_tags_in_comment_block = True`
* Makes subfolders named with first tag in note if `make_tag_folders = True`
* Files can now be copied to multiple tag-folders if `multi_tags = True`
* Export can now be restricted to a list of spesific tags: `limit_export_to_tags = ['bear/github', 'writings']`  
or leave list empty for all notes: `limit_export_to_tags = []`
* Can export and link to images in common image repository
* Or export as textbundles with images included 
'''

make_tag_folders = False  # Exports to folders using first tag only, if `multi_tag_folders = False`
multi_tag_folders = True  # Copies notes to all 'tag-paths' found in note!
                          # Only active if `make_tag_folders = True`
hide_tags_in_comment_block = False  # Hide tags in HTML comments: `<!-- #mytag -->`

# The following two lists are more or less mutually exclusive, so use only one of them.
# (You can use both if you have some nested tags where that makes sense)
# Also, they only work if `make_tag_folders = True`.
only_export_these_tags = []  # Leave this list empty for all notes! See below for sample
# only_export_these_tags = ['bear/github', 'writings'] 

# export_as_textbundles = True  # Exports as Textbundles with images included
# export_as_hybrids = True  # Exports as .textbundle only if images included, otherwise as .md
                          # Only used if `export_as_textbundles = True`
# export_image_repository = False  # Export all notes as md but link images to 
                                 # a common repository exported to: `assets_path` 
                                 # Only used if `export_as_textbundles = False`

import os
HOME = os.getenv('HOME', '')
default_out_folder = os.path.join(HOME, "Work", "BearNotes")
default_backup_folder = os.path.join(HOME, "Work", "BearSyncBackup")

# NOTE! Your user 'HOME' path and '/BearNotes' is added below!
# NOTE! So do not change anything below here!!!

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

parser = argparse.ArgumentParser(description="Sync Bear notes")
parser.add_argument("--out", default=default_out_folder, help="Path where Bear notes will be synced")
parser.add_argument("--backup", default=default_backup_folder, help="Path where conflicts will be backed up (must be outside of --out)")
parser.add_argument("--images", default=None, help="Path where images will be stored")
parser.add_argument("--skipImport", action="store_const", const=True, default=False, help="When present, the script only exports from Bear to Markdown; it skips the import step.")
parser.add_argument("--excludeTag", action="append", default=[], help="Don't export notes with this tag. Can be used multiple times.")
parser.add_argument("--hideTags", action="store_const", const=True, default=False, help="Wrap tags in ")
# 【新增】接收格式参数，默认值为 md
parser.add_argument("--format", choices=['tb', 'md'], default='md', help="Export format: 'tb' for Textbundles/Hybrids, 'md' for standard Markdown with images.")

parsed_args = vars(parser.parse_args())

# 【新增】根据传入的格式参数，动态设置导出模式
if parsed_args.get("format") == 'tb':
    export_as_textbundles = True
    export_as_hybrids = True
    export_image_repository = False
else:
    export_as_textbundles = False
    export_as_hybrids = False
    export_image_repository = True

set_logging_on = True

# NOTE! if 'BearNotes' is left blank, all other files in my_sync_service will be deleted!! 
export_path = parsed_args.get("out")
no_export_tags = parsed_args.get("excludeTag")  # If a tag in note matches one in this list, it will not be exported.
hide_tags_in_comment_block = parsed_args.get("hideTags");

# NOTE! "export_path" is used for sync-back to Bear, so don't change this variable name!
multi_export = [(export_path, True)]  # only one folder output here. 
# Use if you want export to severa places like: Dropbox and OneDrive, etc. See below
# Sample for multi folder export:
# export_path_aux1 = os.path.join(HOME, 'OneDrive', 'BearNotes')
# export_path_aux2 = os.path.join(HOME, 'Box', 'BearNotes')

# NOTE! All files in export path not in Bear will be deleted if delete flag is "True"!
# Set this flag fo False only for folders to keep old deleted versions of notes
# multi_export = [(export_path, True), (export_path_aux1, False), (export_path_aux2, True)]

temp_path = os.path.join(HOME, 'Temp', 'BearExportTemp')  # NOTE! Do not change the "BearExportTemp" folder name!!!
bear_db = os.path.join(HOME, 'Library/Group Containers/9K33E3U3T4.net.shinyfrog.bear/Application Data/database.sqlite')
sync_backup = parsed_args.get("backup") # Backup of original note before sync to Bear.
log_file = os.path.join(sync_backup, 'bear_export_sync_log.txt')

# Paths used in image exports:
bear_image_path = os.path.join(HOME,
    'Library/Group Containers/9K33E3U3T4.net.shinyfrog.bear/Application Data/Local Files/Note Images')
assets_path = parsed_args.get("images") if parsed_args.get("images") else os.path.join(export_path, 'BearImages')

sync_ts = '.sync-time.log'
export_ts = '.export-time.log'

sync_ts_file = os.path.join(export_path, sync_ts)
sync_ts_file_temp = os.path.join(temp_path, sync_ts)
export_ts_file_exp = os.path.join(export_path, export_ts)
export_ts_file = os.path.join(temp_path, export_ts)

gettag_sh = os.path.join(HOME, 'temp/gettag.sh')
gettag_txt = os.path.join(HOME, 'temp/gettag.txt')


def main():
    init_gettag_script()
    if not parsed_args.get("skipImport"):
        sync_md_updates()
    if check_db_modified():
        delete_old_temp_files()
        note_count = export_markdown()
        write_time_stamp()
        rsync_files_from_temp()
        if export_image_repository and not export_as_textbundles:
            copy_bear_images()
        # notify('Export completed')
        write_log(str(note_count) + ' notes exported to: ' + export_path)
        exit(1)
    else:
        print('*** No notes needed exports')
        exit(0)


def write_log(message):
    if set_logging_on == True:
        if not os.path.exists(sync_backup):
            os.makedirs(sync_backup)
        time_stamp = datetime.datetime.now().strftime("%Y-%m-%d at %H:%M:%S")
        message = message.replace(export_path + '/', '')
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(time_stamp + ': ' + message + '\n')


def check_db_modified():
    if not os.path.exists(sync_ts_file):
        return True
    db_ts = get_file_date(bear_db)
    last_export_ts = get_file_date(export_ts_file_exp)
    return db_ts > last_export_ts


def export_markdown():
    with sqlite3.connect(bear_db) as conn:
        conn.row_factory = sqlite3.Row
        query = "SELECT * FROM `ZSFNOTE` WHERE `ZTRASHED` LIKE '0' AND `ZARCHIVED` LIKE '0'"
        c = conn.execute(query)
        note_count = 0
        for row in c:
            title = row['ZTITLE']
            md_text = row['ZTEXT'].rstrip()
            creation_date = row['ZCREATIONDATE']
            modified = row['ZMODIFICATIONDATE']
            uuid = row['ZUNIQUEIDENTIFIER']
            pk = row['Z_PK']
            filename = clean_title(title)
            file_list = []
            if make_tag_folders:
                file_list = sub_path_from_tag(temp_path, filename, md_text)
            else:
                is_excluded = False
                for no_tag in no_export_tags:
                    if ("#" + no_tag) in md_text:
                        is_excluded = True
                        break
                if not is_excluded:
                    file_list.append(os.path.join(temp_path, filename))
            if file_list:
                mod_dt = dt_conv(modified)
                md_text = hide_tags(md_text)
                md_text += '\n\n<!-- {BearID:' + uuid + '} -->\n'
                for filepath in file_list:
                    note_count += 1
                    # print(filepath)
                    if export_as_textbundles:
                        # 确保这里传入了 (md_text, filepath)
                        if check_image_hybrid(md_text, filepath):
                            # 注意：如果你之前根据 Bear 2.0 的修复加了 conn, pk 参数，这里请保持原样
                            make_text_bundle(md_text, filepath, mod_dt, conn, pk)                        
                        else:
                            write_file(filepath + '.md', md_text, mod_dt, creation_date)
                    elif export_image_repository:
                        md_proc_text = process_image_links(md_text, filepath, conn, pk)
                        write_file(filepath + '.md', md_proc_text, mod_dt, creation_date)
                    else:
                        write_file(filepath + '.md', md_text, mod_dt, creation_date)
    return note_count


def check_image_hybrid(md_text, filepath):
    if export_as_hybrids:
        # 【关键修复】：将 filepath 中的 temp_path 替换为真实的 export_path
        # 这样脚本就会去你的实际外部文件夹里检查文件是否存在了
        dest_filepath = filepath.replace(temp_path, export_path)
        
        # 检查真实的外部文件夹中是否已经存在 .textbundle
        if os.path.exists(dest_filepath + '.textbundle'):
            return True
            
        # 否则，检查文本中是否含有图片 (同时兼容旧版和新版图片格式)
        if re.search(r'\[image:(.+?)\]', md_text) or re.search(r'!\[(.*?)\]\((.+?)\)', md_text):
            return True
        else:
            return False
    else:
        return True


def make_text_bundle(md_text, filepath, mod_dt, conn, pk):
    '''
    Exports as Textbundles with images included 
    '''
    bundle_path = filepath + '.textbundle'
    assets_path = os.path.join(bundle_path, 'assets')    
    if not os.path.exists(bundle_path):
        os.makedirs(bundle_path)
        os.makedirs(assets_path)
        
    info = '''{
    "transient" : true,
    "type" : "net.daringfireball.markdown",
    "creatorIdentifier" : "net.shinyfrog.bear",
    "version" : 2
    }'''
    
    # 1. 兼容旧版本 Bear 1 的图片格式
    matches = re.findall(r'\[image:(.+?)\]', md_text)
    for match in matches:
        image_name = match
        new_name = image_name.replace('/', '_')
        source = os.path.join(bear_image_path, image_name)
        target = os.path.join(assets_path, new_name)
        if os.path.exists(source):
            shutil.copy2(source, target)
    md_text = re.sub(r'\[image:(.+?)/(.+?)\]', r'![](assets/\1_\2)', md_text)

    # 2. 兼容新版本 Bear 2 的图片格式
    image_map = {}
    files = conn.execute("SELECT * FROM `ZSFNOTEFILE` WHERE ZNOTE = ?", (pk,))
    for row in files:
        filename = row["ZFILENAME"]
        uuid = row["ZUNIQUEIDENTIFIER"]
        image_map[filename] = uuid
    
    def replace_markdown_image(match):
        alt_text = match.group(1)
        image_url = match.group(2)
        if image_url.startswith("http"):
            return match.group(0)
        
        image_filename = urllib.parse.unquote(image_url)
        file_uuid = image_map.get(image_filename)
        
        if file_uuid:
            source = os.path.join(bear_image_path, file_uuid, image_filename)
            new_name = f"{file_uuid}_{image_filename}"
            target = os.path.join(assets_path, new_name)
            if os.path.exists(source):
                shutil.copy2(source, target)
            encoded_target = urllib.parse.quote(f"assets/{new_name}")
            return f"![{alt_text}]({encoded_target})"
        return match.group(0)

    md_text = re.sub(r'!\[(.*?)\]\((.+?)\)', replace_markdown_image, md_text)
    
    write_file(bundle_path + '/text.md', md_text, mod_dt, 0)
    write_file(bundle_path + '/info.json', info, mod_dt, 0)
    os.utime(bundle_path, (-1, mod_dt))


def sub_path_from_tag(temp_path, filename, md_text):
    # Get tags in note:
    pattern1 = r'(?<!\S)\#([.\w\/\-]+)[ \n]?(?!([\/ \w]+\w[#]))'
    pattern2 = r'(?<![\S])\#([^ \d][.\w\/ ]+?)\#([ \n]|$)'
    if multi_tag_folders:
        # Files copied to all tag-folders found in note
        tags = []
        for matches in re.findall(pattern1, md_text):
            tag = matches[0]
            tags.append(tag)
        for matches2 in re.findall(pattern2, md_text):
            tag2 = matches2[0]
            tags.append(tag2)
        if len(tags) == 0:
            # No tags found, copy to root level only
            return [os.path.join(temp_path, filename)]
    else:
        # Only folder for first tag
        match1 =  re.search(pattern1, md_text)
        match2 =  re.search(pattern2, md_text)
        if match1 and match2:
            if match1.start(1) < match2.start(1):
                tag = match1.group(1)
            else:
                tag = match2.group(1)
        elif match1:
            tag = match1.group(1)
        elif match2:
            tag = match2.group(1)
        else:
            # No tags found, copy to root level only
            return [os.path.join(temp_path, filename)]
        tags = [tag]
    paths = [os.path.join(temp_path, filename)]
    for tag in tags:
        if tag == '/':
            continue
        if only_export_these_tags:
            export = False
            for export_tag in only_export_these_tags:
                if tag.lower().startswith(export_tag.lower()):
                    export = True
                    break
            if not export:
                continue
        for no_tag in no_export_tags:
            if tag.lower().startswith(no_tag.lower()):
                return []
        if tag.startswith('.'):
            # Avoid hidden path if it starts with a '.'
            sub_path = '_' + tag[1:]     
        else:
            sub_path = tag    
        tag_path = os.path.join(temp_path, sub_path)
        if not os.path.exists(tag_path):
            os.makedirs(tag_path)
        paths.append(os.path.join(tag_path, filename))      
    return paths


def process_image_links(md_text, filepath, conn, pk):
    image_map = None
    remaining_images = set()
    def replace_image_link(match):
        # We're only processing local assets.
        if match.group(2).startswith("http"):
            return match.group(0)

        nonlocal image_map
        if image_map is None:
            image_map = {}
            files = conn.execute("SELECT * FROM `ZSFNOTEFILE` WHERE ZNOTE = ?", (pk,))
            for row in files:
                filename = row["ZFILENAME"]
                uuid = row["ZUNIQUEIDENTIFIER"]
                out_file_path = os.path.relpath(assets_path, export_path) + f"/{uuid}/{filename}"
                image_map[filename] = out_file_path
                remaining_images.add(filename)

        # Markdown image URLs are percent-encoded, but the Bear database is not.
        image_filename = urllib.parse.unquote(match.group(2))
        out_file_path = image_map.get(image_filename)
        if out_file_path is None:
            print(f"WARNING: Note {filepath} has image {image_filename} which was not found in database. Skipping.")
            return match.group(0)
        remaining_images.remove(image_filename)
        encoded_out_file_path = urllib.parse.quote(out_file_path)
        return f"![{match.group(1)}]({encoded_out_file_path})"

    if remaining_images:
        print(f"WARNING: Note {filepath} has images in the database which weren't matched in the note: {remaining_images}")
    out_text = re.sub(r'!\[(.*?)\]\((.+?)\)', replace_image_link, md_text)
    return out_text


def restore_image_links(md_text):
    # 修复：移除旧版 Bear 1 [image:...] 格式转换，直接剥离 UUID 前缀适配 Bear 2
    if export_as_textbundles:
        return re.sub(r'!\[(.*?)\]\(assets/.+?_(.+?)( ".+?")?\) ?', r'![\1](\2)', md_text)
    elif export_image_repository:
        relative_asset_path = os.path.relpath(assets_path, export_path)
        return re.sub(r'!\[(.*?)\]\(' + re.escape(relative_asset_path) + r'/(.+?)/(.+?)\)', r'![\1](\3)', md_text)
    return md_text
        


def copy_bear_images():
    # Image files copied to a common image repository
    subprocess.call(['rsync', '-r', '-t', '--delete', 
                    bear_image_path + "/", assets_path])


def write_time_stamp():
    # write to time-stamp.txt file (used during sync)
    write_file(export_ts_file, "Markdown from Bear written at: " +
               datetime.datetime.now().strftime("%Y-%m-%d at %H:%M:%S"), 0, 0)
    write_file(sync_ts_file_temp, "Markdown from Bear written at: " +
               datetime.datetime.now().strftime("%Y-%m-%d at %H:%M:%S"), 0, 0)


def hide_tags(md_text):
    # Hide tags from being seen as H1, by placing `period+space` at start of line:
    if hide_tags_in_comment_block:
        md_text =  re.sub(r'(\n)[ \t]*(\#[^\s#].*)', r'\1<!-- \2 -->', md_text)
    return md_text


def restore_tags(md_text):
    # Tags back to normal Bear tags, stripping the `period+space` at start of line:
    if hide_tags_in_comment_block:
        md_text =  re.sub(r'(\n)<!--[ \t]*(\#[^\s#].*?) -->', r'\1\2', md_text)
    return md_text


def clean_title(title):
    title = title[:225].strip()
    if title == "":
        title = "Untitled"
    title = re.sub(r'[\/\\:]', r'-', title)
    title = re.sub(r'-$', r'', title)    
    return title.strip()


def write_file(filename, file_content, modified, created):
    with open(filename, "w", encoding='utf-8') as f:
        f.write(file_content)
    if modified > 0:
        os.utime(filename, (-1, modified))
    if created > 0:
        newnum = dt_conv(created)
        dtdate = datetime.datetime.fromtimestamp(newnum)
        datestring = dtdate.strftime("%m/%d/%Y %H:%M:%S")
        command = 'SetFile -d "' + datestring + '" ' + shlex.quote(filename)
        subprocess.call(command, shell=True)


def read_file(file_name):
    with open(file_name, "r", encoding='utf-8') as f:
        file_content = f.read()
    return file_content


def get_file_date(filename):
    try:
        t = os.path.getmtime(filename)
        return t
    except:
        return 0


def dt_conv(dtnum):
    # Formula for date offset based on trial and error:
    hour = 3600 # seconds
    year = 365.25 * 24 * hour
    offset = year * 31 + hour * 6
    return dtnum + offset


def date_time_conv(dtnum):
    newnum = dt_conv(dtnum) 
    dtdate = datetime.datetime.fromtimestamp(newnum)
    #print(newnum, dtdate)
    return dtdate.strftime(' - %Y-%m-%d_%H%M')


def time_stamp_ts(ts):
    dtdate = datetime.datetime.fromtimestamp(ts)
    return dtdate.strftime('%Y-%m-%d at %H:%M') 


def date_conv(dtnum):
    dtdate = datetime.datetime.fromtimestamp(dtnum)
    return dtdate.strftime('%Y-%m-%d')


def delete_old_temp_files():
    # Deletes all files in temp folder before new export using "shutil.rmtree()":
    # NOTE! CAUTION! Do not change this function unless you really know shutil.rmtree() well!
    if os.path.exists(temp_path) and "BearExportTemp" in temp_path:
        # *** NOTE! Double checking that temp_path folder actually contains "BearExportTemp"
        # *** Because if temp_path is accidentally empty or root,
        # *** shutil.rmtree() will delete all files on your complete Hard Drive ;(
        shutil.rmtree(temp_path)
        # *** NOTE: USE rmtree() WITH EXTREME CAUTION!
    os.makedirs(temp_path)


def rsync_files_from_temp():
    # Moves markdown files to new folder using rsync:
    # This is a very important step! 
    # By first exporting all Bear notes to an emptied temp folder,
    # rsync will only update destination if modified or size have changed.
    # So only changed notes will be synced by Dropbox or OneDrive destinations.
    # Rsync will also delete notes on destination if deleted in Bear.
    # So doing it this way saves a lot of otherwise very complex programing.
    # Thank you very much, Rsync! ;)
    for (dest_path, delete) in multi_export:
        if not os.path.exists(dest_path):
            os.makedirs(dest_path)
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

def convert_ref_links_to_inline(md_text):
    # 将外部编辑器产生的引用式图片链接转换为标准的内联式链接
    refs = dict(re.findall(r'^\[([^\]]+)\]:\s*(\S+).*$', md_text, flags=re.MULTILINE))
    if not refs:
        return md_text
    def repl(m):
        return f"![{m.group(1)}]({refs.get(m.group(2), m.group(2))})"
    md_text = re.sub(r'!\[([^\]]*)\]\[([^\]]+)\]', repl, md_text)
    def repl_imp(m):
        return f"![{m.group(1)}]({refs.get(m.group(1), m.group(1))})"
    md_text = re.sub(r'!\[([^\]]+)\](?!\()', repl_imp, md_text)
    return md_text

def sync_md_updates():
    updates_found = False
    if not os.path.exists(sync_ts_file) or not os.path.exists(export_ts_file):
        return False
    ts_last_sync = os.path.getmtime(sync_ts_file)
    ts_last_export = os.path.getmtime(export_ts_file)
    # Update synced timestamp file:
    update_sync_time_file(0)
    file_types = ('*.md', '*.txt', '*.markdown')
    for (root, dirnames, filenames) in os.walk(export_path):
        if '.obsidian' in dirnames:
            dirnames.remove('.obsidian')
        '''
        This step walks down into all sub folders, if any.
        '''
        for pattern in file_types:
            for filename in fnmatch.filter(filenames, pattern):
                md_file = os.path.join(root, filename)
                ts = os.path.getmtime(md_file)
                if ts > ts_last_sync:
                    if not updates_found:  # Yet
                        # Wait 5 sec at first for external files to finish downloading from dropbox.
                        # Otherwise images in textbundles might be missing in import:
                        time.sleep(5)
                    updates_found = True
                    md_text = read_file(md_file)
                    # 【新增下面这一行】：在读取文件后立即修复图片格式
                    md_text = convert_ref_links_to_inline(md_text) 
                    backup_ext_note(md_file)
                    # 【修改为下面这行】：只要是 textbundle，就必须走专门的安全通道
                    if '.textbundle' in md_file:
                        textbundle_to_bear(md_text, md_file, ts)
                        write_log('Imported to Bear: ' + md_file)
                    else:
                        update_bear_note(md_text, md_file, ts, ts_last_export)
                        write_log('Bear Note Updated: ' + md_file)
    if updates_found:
        # Give Bear time to process updates:
        time.sleep(3)
        # Check again, just in case new updates synced from remote (OneDrive/Dropbox) 
        # during this process!
        # The logic is not 100% fool proof, but should be close to 99.99%
        sync_md_updates() # Recursive call
    return updates_found


def check_if_image_added(md_text, md_file):
    if not '.textbundle/' in md_file:
        return False
    # 修复：只要 assets/ 里的图片不是以 Bear 的长串 UUID 开头，就认定为新添加的外部图片
    matches = re.findall(r'!\[.*?\]\(assets/(.+?)\)', md_text)
    for image_filename in matches:
        if not re.match(r'[0-9A-F]{8}-([0-9A-F]{4}-){3}[0-9A-F]{12}', image_filename):
            return True
    return False       
def textbundle_to_bear(md_text, md_file, mod_dt):
    md_text = restore_tags(md_text)
    bundle = os.path.split(md_file)[0]
    match = re.search(r'\{BearID:(.+?)\}', md_text)
    
    if match:
        uuid = match.group(1)
        # 1. 移除外部专用的 BearID 标记
        clean_md_text = re.sub(r'\<\!-- ?\{BearID\:' + uuid + r'\} ?--\>', '', md_text).rstrip() + '\n'
        
        # 【新增防御】：自动将底部“引用式”链接翻译成“内联式”标准链接
        # 抓取文档底部所有的 [image-1]: assets/... 格式（兼容空格和制表符）
        refs = dict(re.findall(r'^\[([^\]]+)\]:[ \t]*(\S+).*$', clean_md_text, flags=re.MULTILINE))
        if refs:
            # 替换 ![alt][ref] 格式
            clean_md_text = re.sub(r'!\[([^\]]*)\]\[([^\]]+)\]', lambda m: f"![{m.group(1)}]({refs.get(m.group(2), m.group(2))})", clean_md_text)
            # 替换 ![ref] 格式
            clean_md_text = re.sub(r'!\[([^\]]+)\](?!\()', lambda m: f"![{m.group(1)}]({refs.get(m.group(1), m.group(1))})", clean_md_text)
            # 清除底部的引用定义，保持文档干净
            clean_md_text = re.sub(r'^\[[^\]]+\]:[ \t]*\S+.*$\n?', '', clean_md_text, flags=re.MULTILINE)
        
        # 将干净的纯标准文本写回外部文件
        write_file(md_file, clean_md_text, mod_dt, 0)
        
        # 2. 扫描并上传外部刚加进来的新图片 (现在的 clean_md_text 已经全部是能识别的标准格式了)
        matches = re.findall(r'!\[(.*?)\]\(([^)]+)\)', clean_md_text)
        for alt_text, img_path in matches:
            img_path_unq = urllib.parse.unquote(img_path)
            img_filename = img_path_unq.split('/')[-1]
            
            # 如果文件名不带有 Bear 的长串 UUID，说明是你自己刚刚拖进去的新图片
            if not re.match(r'[0-9A-F]{8}-([0-9A-F]{4}-){3}[0-9A-F]{12}_', img_filename):
                # 地毯式搜索图片源文件位置
                possible_paths = [
                    os.path.join(bundle, img_path_unq),       
                    os.path.join(bundle, 'assets', img_filename), 
                    os.path.join(bundle, img_filename)        
                ]
                
                img_full_path = next((p for p in possible_paths if os.path.exists(p)), None)
                        
                if img_full_path:
                    safe_path = urllib.parse.quote(img_full_path, safe='')
                    # 使用 mode=append 静默将图片塞入熊掌记数据库
                    x_file = f"bear://x-callback-url/add-file?id={uuid}&mode=append&filepath={safe_path}"
                    url = NSURL.URLWithString_(x_file)
                    if url is not None:
                        NSWorkspace.sharedWorkspace().openURL_configuration_completionHandler_(url, open_config, None)
                        time.sleep(0.5) 
                        
        # 3. 核心大招：把所有 Markdown 图片路径转换回熊掌记的原生内部格式！
        def restore_img_format(m):
            alt = m.group(1)
            filename = urllib.parse.unquote(m.group(2)).split('/')[-1]
            clean_name = re.sub(r'^[0-9A-F]{8}-([0-9A-F]{4}-){3}[0-9A-F]{12}_', '', filename)
            return f"![{alt}]({clean_name})"
            
        bear_md_text = re.sub(r'!\[(.*?)\]\(([^)]+)\)', restore_img_format, clean_md_text)
        
        # 4. 用原生排版的文本安全地覆盖熊掌记笔记
        safe_text = urllib.parse.quote(bear_md_text, safe='')
        x_replace = f"bear://x-callback-url/add-text?show_window=no&open_note=no&mode=replace_all&id={uuid}&text={safe_text}"
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
        target = os.path.join(sync_backup, bundle_name)
        bundle_raw = os.path.splitext(target)[0]
        count = 2
        while os.path.exists(target):
            # Adding sequence number to identical filenames, preventing overwrite:
            target = bundle_raw + " - " + str(count).zfill(2) + ".textbundle"
            count += 1
        shutil.copytree(bundle_path, target)
    else:
        # Overwrite former bacups of incoming changes, only keeps last one:
        shutil.copy2(md_file, sync_backup + '/')


def update_sync_time_file(ts):
    write_file(sync_ts_file,
        "Checked for Markdown updates to sync at: " +
        datetime.datetime.now().strftime("%Y-%m-%d at %H:%M:%S"), ts, 0)


def update_bear_note(md_text, md_file, ts, ts_last_export):
    md_text = restore_tags(md_text)
    md_text = restore_image_links(md_text)
    uuid = ''
    match = re.search(r'\{BearID:(.+?)\}', md_text)
    sync_conflict = False
    if match:
        uuid = match.group(1)
        # Remove old BearID: from new note
        md_text = re.sub(r'\<\!-- ?\{BearID\:' + uuid + r'\} ?--\>', '', md_text).rstrip() + '\n'

        sync_conflict = check_sync_conflict(uuid, ts_last_export)
        if sync_conflict:
            link_original = 'bear://x-callback-url/open-note?id=' + uuid
            message = '::Sync conflict! External update: ' + time_stamp_ts(ts) + '::'
            message += '\n[Click here to see original Bear note](' + link_original + ')'
            x_create = 'bear://x-callback-url/create?show_window=no&open_note=no' 
            bear_x_callback(x_create, md_text, message, '')   
        else:
            # Regular external update
            orig_title = backup_bear_note(uuid)
            # message = '::External update: ' + time_stamp_ts(ts) + '::'   
            x_replace = 'bear://x-callback-url/add-text?show_window=no&open_note=no&mode=replace_all&id=' + uuid
            bear_x_callback(x_replace, md_text, '', orig_title)
            # # Trash old original note:
            # x_trash = 'bear://x-callback-url/trash?show_window=no&id=' + uuid
            # subprocess.call(["open", x_trash])
            # time.sleep(.2)
    else:
        # New external md Note, since no Bear uuid found in text: 
        # message = '::New external Note - ' + time_stamp_ts(ts) + '::' 
        md_text = get_tag_from_path(md_text, md_file, export_path)
        x_create = 'bear://x-callback-url/create?show_window=no' 
        bear_x_callback(x_create, md_text, '', '')
    return


def get_tag_from_path(md_text, md_file, root_path, inbox_for_root=False, extra_tag=''):
    # extra_tag should be passed as '#tag' or '#space tag#'
    path = md_file.replace(root_path, '')[1:]
    sub_path = os.path.split(path)[0]
    tags = []
    if '.textbundle' in sub_path:
        sub_path = os.path.split(sub_path)[0]
    if sub_path == '': 
        if inbox_for_root:
            tag = '#.inbox'
        else:
            tag = ''
    elif sub_path.startswith('_'):
        tag = '#.' + sub_path[1:].strip()
    else:
        tag = '#' + sub_path.strip()
    if ' ' in tag: 
        tag += "#"               
    if tag != '': 
        tags.append(tag)
    if extra_tag != '':
        tags.append(extra_tag)
    for tag in get_file_tags(md_file):
        tag = '#' + tag.strip()
        if ' ' in tag: tag += "#"                   
        tags.append(tag)
    return md_text.strip() + '\n\n' + ' '.join(tags) + '\n'


def get_file_tags(md_file):
    try:
        subprocess.call([gettag_sh, md_file, gettag_txt])
        text = re.sub(r'\\n\d{1,2}', r'', read_file(gettag_txt))
        tag_list = json.loads(text)
        return tag_list
    except:
        return []


open_config = NSWorkspaceOpenConfiguration.alloc().init()
open_config.setActivates_(False)

def bear_x_callback(x_command, md_text, message, orig_title):
    # 1. 如果有提示信息，将其插入到第二行
    if message != '':
        lines = md_text.splitlines()
        lines.insert(1, message)
        md_text = '\n'.join(lines)
        
    # 【已删除】旧代码中 "if orig_title != '':" 的逻辑被整体移除。
    # 这样可以确保文本的第一行（标题）永远不会再被错误地吞掉！
    
    # 2. 对整个 Markdown 文本进行严格的 URL 编码
    # 增加 safe='' 强制转义所有特殊字符，防止 NSURL 解析失败导致新建 md 无法同步
    x_command_text = x_command + '&text=' + urllib.parse.quote(md_text, safe='')
    url = NSURL.URLWithString_(x_command_text)
    
    # 3. 触发系统 URL 方案，并增加安全检查
    if url is not None:
        NSWorkspace.sharedWorkspace().openURL_configuration_completionHandler_(url, open_config, None)
    else:
        # 当文本包含极其特殊的异常字符导致 URL 生成失败时，能在终端给出提示，避免静默失败
        print("警告: 无法构建 NSURL 进行同步。这通常是因为文本中包含异常字符。")
        
    time.sleep(.2)

def check_sync_conflict(uuid, ts_last_export):
    conflict = False
    # Check modified date of original note in Bear sqlite db!
    with sqlite3.connect(bear_db) as conn:
        conn.row_factory = sqlite3.Row
        query = "SELECT * FROM `ZSFNOTE` WHERE `ZTRASHED` LIKE '0' AND `ZUNIQUEIDENTIFIER` LIKE '" + uuid + "'"
        c = conn.execute(query)
    for row in c:
        modified = row['ZMODIFICATIONDATE']
        uuid = row['ZUNIQUEIDENTIFIER']
        mod_dt = dt_conv(modified)
        conflict = mod_dt > ts_last_export
    return conflict


def backup_bear_note(uuid):
    # Get single note from Bear sqlite db!
    with sqlite3.connect(bear_db) as conn:
        conn.row_factory = sqlite3.Row
        query = "SELECT * FROM `ZSFNOTE` WHERE `ZUNIQUEIDENTIFIER` LIKE '" + uuid + "'"
        c = conn.execute(query)
    title = ''
    for row in c:  # Will only get one row if uuid is found!
        title = row['ZTITLE']
        md_text = row['ZTEXT'].rstrip()
        modified = row['ZMODIFICATIONDATE']
        mod_dt = dt_conv(modified)
        created = row['ZCREATIONDATE']
        cre_dt = dt_conv(created)
        md_text = insert_link_top_note(md_text, 'Link to updated note: ', uuid)
        dtdate = datetime.datetime.fromtimestamp(cre_dt)
        filename = clean_title(title) + dtdate.strftime(' - %Y-%m-%d_%H%M')
        if not os.path.exists(sync_backup):
            os.makedirs(sync_backup)
        file_part = os.path.join(sync_backup, filename) 
        # This is a Bear text file, not exactly markdown.
        backup_file = file_part + ".txt"
        count = 2
        while os.path.exists(backup_file):
            # Adding sequence number to identical filenames, preventing overwrite:
            backup_file = file_part + " - " + str(count).zfill(2) + ".txt"
            count += 1
        write_file(backup_file, md_text, mod_dt, created)
        filename2 = os.path.split(backup_file)[1]
        write_log('Original to sync_backup: ' + filename2)
    return title


def insert_link_top_note(md_text, message, uuid):
    lines = md_text.split('\n')
    title = re.sub(r'^#{1,6} ', r'', lines[0])
    link = '::' + message + '[' + title + '](bear://x-callback-url/open-note?id=' + uuid + ')::'        
    lines.insert(1, link) 
    return '\n'.join(lines)


def init_gettag_script():
    gettag_script = \
    '''#!/bin/bash
    if [[ ! -e "$1" ]] ; then
    echo 'file missing or not specified'
    exit 0
    fi
    JSON="$(xattr -p com.apple.metadata:_kMDItemUserTags "$1" 2>/dev/null | xxd -r -p | plutil -convert json - -o - 2>/dev/null)"
    echo $JSON > "$2"
    '''
    temp = os.path.join(HOME, 'temp')
    if not os.path.exists(temp):
        os.makedirs(temp)
    write_file(gettag_sh, gettag_script, 0, 0)
    subprocess.call(['chmod', '777', gettag_sh])
    

def notify(message):
    title = "ul_sync_md.py"
    try:
        # Uses "terminal-notifier", download at:
        # https://github.com/julienXX/terminal-notifier/releases/download/2.0.0/terminal-notifier-2.0.0.zip
        # Only works with MacOS 10.11+
        subprocess.call(['/Applications/terminal-notifier.app/Contents/MacOS/terminal-notifier',
                         '-message', message, "-title", title, '-sound', 'default'])
    except:
        write_log('"terminal-notifier.app" is missing!')        
    return


if __name__ == '__main__':
    main()
