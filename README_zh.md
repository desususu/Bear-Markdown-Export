# Bear Markdown 导出与同步

> **本项目是对原版 [rovest/Bear-Markdown-Export](https://github.com/rovest/Bear-Markdown-Export) 的完整重构，已适配 Bear 2.0，并带来了显著的性能优化、实时同步守护进程以及改进的图片处理机制。**

**English documentation: [README.md](README.md)**

---

## ⚠️ 重要警告 — 请先备份

**在首次运行脚本之前，请务必备份你的 Bear 笔记。**

打开 Bear，依次点击 **文件 → 备份笔记…**，将备份文件保存到安全位置。
同时建议使用 Time Machine 或其他工具对 Mac 进行整体备份。

脚本内部使用了 `rsync` 和 `shutil.rmtree` 等强力命令，如果路径配置有误，可能导致文件被覆盖或删除。**首次运行前请仔细核对配置。**

---

## 项目概览

本项目包含两个协同工作的脚本：

| 脚本 | 作用 |
|---|---|
| `bear_export_sync.py` | 核心引擎 — 将 Bear 笔记导出为 Markdown / Textbundle，并将外部编辑同步回 Bear |
| `dual_sync.py` | 守护进程 — 基于文件系统事件实时调用核心引擎 |

### 功能简介

- 将所有 Bear 笔记导出为 `.md` 纯文本或包含图片的 `.textbundle` 包
- 监测外部编辑器（Obsidian、Typora、Ulysses 等）的修改，并自动同步回 Bear
- 以后台守护进程方式运行，对 Bear 笔记变更的响应延迟约为 1–2 秒
- 支持 Markdown 和 Textbundle 双格式并行导出

---

## 兼容性

- **仅支持 macOS** — 依赖 macOS 原生框架（`AppKit`、`NSWorkspace`、`FSEvents`）
- **Bear 2.0** — 读取 Bear 当前的 SQLite 数据库结构和 Group Container 路径
- **Python 3.9+**（最低 3.6+）

---

## 本次重构的主要改进

### 性能优化

- **预编译正则表达式** — 所有正则在模块加载时编译一次，处理大型笔记库时性能大幅提升
- **增量图片复制** — 图片在导出循环中直接按需复制，不再需要单独对整个图片目录执行 rsync，显著减少 I/O 开销
- **基于时间戳的变更检测** — 在做任何实际操作前，先检查 Bear `database.sqlite` 的修改时间；若无变化，立即退出，不写入任何文件
- **无变化时快速退出** — MD 和 TB 导出阶段在无需同步时均返回退出码 `0`，守护进程可跳过不必要的处理

### 实时同步守护进程（`dual_sync.py`）

- **FSEvents 触发导出** — 监控 Bear 的 SQLite WAL 文件；Bear 保存笔记后，导出周期在 1–2 秒内触发，无需等待下一个轮询间隔
- **文件写入静默守卫** — 通过 `watchdog` 监控导出目录；若外部编辑器正在写入文件，同步计时器暂停，待目录静默指定秒数后（默认 5 秒）再恢复，防止将未写完的文件导回 Bear
- **同步时间窗口** — 可配置活跃时段（例如 06:00–23:20），守护进程在此范围外休眠
- **编辑器活跃检测** — 检测 Bear、Obsidian、Typora 或 Ulysses 是否为前台应用，若是则推迟同步以避免冲突
- **手动触发** — 通过 `SIGUSR1` 信号（或运行 `python3 dual_sync.py --trigger`）绕过所有守卫，立即执行同步周期
- **热加载配置** — 每次循环都重新读取 `sync_config.json`，修改配置无需重启守护进程

### 改进的图片处理

- 图片从 Bear 内部图片库解析并在导出时复制到输出目录
- Textbundle 导出将图片作为 `assets/` 内嵌到 `.textbundle` 包中
- Markdown 导出将图片链接到共享的 `BearImages/` 目录（或自定义的 `--images` 路径）
- 图片文件名中的 UUID 前缀被自动剥离，输出更整洁
- 同步回 Bear 时，`![alt](url)` 和 `![[wikilink]]` 两种图片语法均可正确还原

### Bear 2.0 适配

- 读取当前 Group Container 路径：`~/Library/Group Containers/9K33E3U3T4.net.shinyfrog.bear/Application Data/database.sqlite`
- 支持最新版 Bear 的笔记图片存储结构
- Bear ID 采用新格式 `[//]: # ({BearID:...})`（同时兼容旧的 HTML 注释格式）

---

## 环境要求

### 系统

- macOS 12 Monterey 或更新版本（推荐）
- 已安装并登录 Bear 应用

### Python 依赖包

一键安装所有依赖：

```bash
pip install pyobjc-framework-Cocoa watchdog
```

| 依赖包 | 用途 | 是否必须 |
|---|---|---|
| `pyobjc-framework-Cocoa` | `AppKit` / `NSWorkspace` — 通过 URL Scheme 打开 Bear | **必须** |
| `watchdog` | FSEvents 观察者，用于实时监控数据库和文件夹变化 | 强烈建议安装 |

> 若未安装 `watchdog`，守护进程将退化为纯轮询模式，且文件写入静默守卫功能将被禁用。启动时会输出警告信息。

### 标准库（无需安装）

`sqlite3`、`re`、`subprocess`、`shutil`、`argparse`、`json`、`threading`、`signal`、`logging`

---

## 安装步骤

```bash
# 1. 克隆仓库
git clone https://github.com/desususu/Bear-Markdown-Export.git
cd Bear-Markdown-Export

# 2. （推荐）创建虚拟环境
python3 -m venv venv
source venv/bin/activate

# 3. 安装依赖
pip install pyobjc-framework-Cocoa watchdog

# 4. 首次运行前，务必备份 Bear 笔记！
#    Bear → 文件 → 备份笔记…
```

---

## 配置说明

### `sync_config.json`

首次运行 `dual_sync.py` 时，它会在同目录下生成默认的 `sync_config.json` 并退出，提示你检查路径配置后再重新启动。

```json
{
    "script_path":            "./bear_export_sync.py",
    "folder_md":              "./Export/MD_Export",
    "folder_tb":              "./Export/TB_Export",
    "backup_md":              "./Backup/MD_Backup",
    "backup_tb":              "./Backup/TB_Backup",
    "sync_interval_seconds":  180,
    "sync_on_startup":        false,
    "write_quiet_seconds":    5,
    "fast_trigger_on_db_change": true,
    "sync_window": {
        "start_hour":  6,  "start_minute":  0,
        "end_hour":   23,  "end_minute":   20
    }
}
```

| 配置项 | 说明 |
|---|---|
| `script_path` | `bear_export_sync.py` 的路径（相对或绝对路径均可） |
| `folder_md` | Markdown 导出目录 |
| `folder_tb` | Textbundle 导出目录 |
| `backup_md` / `backup_tb` | 冲突备份目录（必须在 `folder_md` / `folder_tb` 之外） |
| `sync_interval_seconds` | 兜底轮询间隔，单位为秒（最小 30） |
| `sync_on_startup` | 守护进程启动时立即执行一次完整同步 |
| `write_quiet_seconds` | 允许同步前目录需保持静默的秒数（防止导入未完成的文件） |
| `fast_trigger_on_db_change` | 启用基于 FSEvents 的即时导出（Bear 保存笔记时触发） |
| `sync_window` | 活跃时段；守护进程在此范围外休眠 |

---

## 使用方法

### 启动守护进程（推荐）

```bash
python3 dual_sync.py
```

守护进程在前台运行。可使用 `tmux`、`screen` 或 launchd plist 让其在后台持续运行。

### 运行一次后退出（适合 cron / launchd）

```bash
python3 dual_sync.py --once
```

### 向正在运行的守护进程发送立即同步请求

```bash
python3 dual_sync.py --trigger
```

### 查看守护进程状态

```bash
python3 dual_sync.py --status
```

### 直接运行核心脚本

```bash
# 仅导出（Markdown 格式）
python3 bear_export_sync.py --out ~/Notes/Bear --backup ~/Notes/Backup --format md

# 仅导出（Textbundle 格式）
python3 bear_export_sync.py --out ~/Notes/Bear --backup ~/Notes/Backup --format tb

# 仅导入（跳过导出）
python3 bear_export_sync.py --out ~/Notes/Bear --backup ~/Notes/Backup --skipExport

# 仅导出（跳过导入）
python3 bear_export_sync.py --out ~/Notes/Bear --backup ~/Notes/Backup --skipImport

# 排除带有特定标签的笔记
python3 bear_export_sync.py --out ~/Notes/Bear --backup ~/Notes/Backup --excludeTag private

# 使用自定义图片目录
python3 bear_export_sync.py --out ~/Notes/Bear --backup ~/Notes/Backup --images ~/Notes/Images
```

---

## 同步机制说明

### 导出（Bear → 磁盘）

1. 检查 `database.sqlite` 的修改时间；若无变化，立即退出
2. 从 Bear 的 SQLite 数据库查询所有笔记
3. 将每篇笔记以 `.md` 或 `.textbundle` 格式写入临时目录
4. 剥离 Bear 专有语法（图片引用、标签格式）以兼容外部编辑器
5. 在文件末尾追加 `BearID` 标记，供重新导入时匹配原笔记
6. 使用 `rsync` 将临时目录中的变更文件同步到导出目录（Dropbox、Obsidian 库等）

### 导入（磁盘 → Bear）

1. 扫描导出目录，找出自上次同步后被修改的 `.md` / `.textbundle` 文件
2. 通过文件中嵌入的 `BearID` 匹配原始 Bear 笔记
3. 使用 `bear://x-callback-url/add-text?mode=replace` URL Scheme 更新笔记内容，保留原始创建时间和笔记 ID
4. 发生同步冲突时，两个版本均保留在 Bear 中，并附有冲突提示
5. 没有 `BearID` 的新文件将作为新笔记创建到 Bear 中

---

## 注意事项

### Obsidian 用户 — 必须使用 `#` 一级标题

如果你将导出目录作为 Obsidian 库使用，**每篇笔记的第一行必须是 `#` 一级标题**。

```markdown
# 我的笔记标题

笔记正文……
```

若笔记首行不是 `# ` 格式的标题，Obsidian 将无法从标题推导文件名，导致文件链接失效、笔记无法被正确识别的 bug。Bear 默认以第一行作为笔记标题，请确保将其写为 Markdown 标题格式。

### 标签处理

- 导出时标签会被重新格式化，以防止在其他编辑器中被渲染为 H1 标题
- 如果在脚本中设置 `hide_tags_in_comment_block = True`，标签将被包裹在 HTML 注释中（`<!-- #tag -->`），导入时透明还原

### 冲突处理

- 若同一篇笔记在两次同步之间同时在 Bear 和外部编辑器中被修改，两个版本均会保留在 Bear 中
- 较新的版本会附有同步冲突提示和指向原笔记的链接

### 与 Ulysses 配合使用

- 将 Ulysses 外部文件夹格式设置为 **Textbundle** 和 **内联链接**
- 你在 Ulysses 中手动排列的笔记顺序在同步后保持不变，除非笔记标题发生变化

### 大型笔记库

- 首次导出大型 Bear 库可能需要一两分钟
- 后续同步速度很快，因为只处理发生变化的笔记

### `sync_config.json` 不会提交到 Git

该配置文件包含本地路径，已在 `.gitignore` 中排除，请勿提交到版本控制系统。

---

## 项目结构

```
Bear-Markdown-Export/
├── bear_export_sync.py   # 核心导出/导入引擎
├── dual_sync.py          # 实时同步守护进程
├── sync_config.json      # 本地配置（不提交）
├── LICENSE
├── README.md             # 英文文档
└── README_zh.md          # 中文文档（本文件）
```

---

## 致谢

- 原始作者：[rovest](https://github.com/rovest)（[@rorves](https://twitter.com/rorves)）
- 修改者：[andymatuschak](https://github.com/andymatuschak)（[@andy_matuschak](https://twitter.com/andy_matuschak)）
- 进一步重构与维护：[desususu](https://github.com/desususu)

---

## 许可证

MIT 许可证 — 详见 [LICENSE](LICENSE)。
