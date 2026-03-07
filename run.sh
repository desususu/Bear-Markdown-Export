#!/usr/bin/env bash

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
PY_BIN="${VENV_DIR}/bin/python"
PIP_BIN="${VENV_DIR}/bin/pip"
CONFIG_FILE="${SCRIPT_DIR}/b2ou_config.json"

LAUNCHD_LABEL="com.b2ou.syncdaemon"
LAUNCHD_PLIST="${HOME}/Library/LaunchAgents/${LAUNCHD_LABEL}.plist"
LAUNCHD_OUT_LOG="${SCRIPT_DIR}/b2ou_launchd.out.log"
LAUNCHD_ERR_LOG="${SCRIPT_DIR}/b2ou_launchd.err.log"

LANG_CODE="zh"

say() {
  local zh="$1"
  local en="$2"
  if [[ "${LANG_CODE}" == "zh" ]]; then
    printf '%s\n' "${zh}"
  else
    printf '%s\n' "${en}"
  fi
}

ask() {
  local zh="$1"
  local en="$2"
  if [[ "${LANG_CODE}" == "zh" ]]; then
    read -r -p "${zh}" REPLY
  else
    read -r -p "${en}" REPLY
  fi
}

pause_wait() {
  if [[ "${LANG_CODE}" == "zh" ]]; then
    read -r -p "按回车键继续..."
  else
    read -r -p "Press Enter to continue..."
  fi
}

choose_language() {
  printf '\n'
  printf '========================================\n'
  printf '  B2OU Setup Launcher / 启动器\n'
  printf '========================================\n'
  printf '1) 中文\n'
  printf '2) English\n'
  read -r -p "请选择 / Select: " choice
  case "${choice}" in
    2|en|EN|english|English) LANG_CODE="en" ;;
    *) LANG_CODE="zh" ;;
  esac
}

ensure_macos() {
  if [[ "$(uname -s)" != "Darwin" ]]; then
    say "当前系统不是 macOS，无法运行此工具。" "This tool only supports macOS."
    return 1
  fi
  return 0
}

ensure_python3() {
  if ! command -v python3 >/dev/null 2>&1; then
    say "未检测到 python3。请先安装 Python 3，再重新运行。" \
        "python3 is not available. Install Python 3 first, then run again."
    return 1
  fi
  return 0
}

ensure_venv() {
  if [[ -x "${PY_BIN}" ]]; then
    return 0
  fi
  say "正在创建虚拟环境..." "Creating virtual environment..."
  if ! python3 -m venv "${VENV_DIR}"; then
    say "虚拟环境创建失败。" "Failed to create virtual environment."
    return 1
  fi
  return 0
}

install_dependencies() {
  if ! ensure_python3; then
    return 1
  fi
  if ! ensure_venv; then
    return 1
  fi
  say "正在安装/更新依赖（首次可能较慢）..." \
      "Installing/updating dependencies (first run may take a while)..."
  if ! "${PIP_BIN}" install -e ".[all]"; then
    say "依赖安装失败。请检查网络后重试。" \
        "Dependency installation failed. Check network and retry."
    return 1
  fi
  say "依赖安装完成。" "Dependencies installed."
  return 0
}

ensure_runtime() {
  if ! ensure_macos; then
    return 1
  fi
  if ! ensure_python3; then
    return 1
  fi
  if [[ ! -x "${PY_BIN}" ]]; then
    say "未检测到虚拟环境，请先执行“1. 一键初始化”。" \
        "Virtual environment not found. Run '1. One-click setup' first."
    return 1
  fi
  if ! "${PY_BIN}" -c "import b2ou" >/dev/null 2>&1; then
    say "未检测到 b2ou 依赖，请先执行“1. 一键初始化”。" \
        "b2ou package not available. Run '1. One-click setup' first."
    return 1
  fi
  return 0
}

ensure_config_exists() {
  if [[ -f "${CONFIG_FILE}" ]]; then
    return 0
  fi
  say "正在创建默认配置文件..." "Creating default config file..."
  "${PY_BIN}" -m b2ou sync --config "${CONFIG_FILE}" >/dev/null 2>&1 || true
  if [[ ! -f "${CONFIG_FILE}" ]]; then
    say "配置文件创建失败：${CONFIG_FILE}" "Failed to create config: ${CONFIG_FILE}"
    return 1
  fi
  say "已创建配置文件：${CONFIG_FILE}" "Config file created: ${CONFIG_FILE}"
  return 0
}

json_get() {
  local key="$1"
  "${PY_BIN}" - "${CONFIG_FILE}" "${key}" <<'PY'
import json
import sys
from pathlib import Path

cfg = Path(sys.argv[1])
key = sys.argv[2]
if not cfg.exists():
    print("")
    raise SystemExit(0)
try:
    data = json.loads(cfg.read_text(encoding="utf-8"))
except Exception:
    print("")
    raise SystemExit(0)
val = data.get(key, "")
print(val if isinstance(val, str) else str(val))
PY
}

prompt_default() {
  local prompt_zh="$1"
  local prompt_en="$2"
  local current="$3"
  local input=""
  if [[ "${LANG_CODE}" == "zh" ]]; then
    read -r -p "${prompt_zh} [${current}]: " input
  else
    read -r -p "${prompt_en} [${current}]: " input
  fi
  if [[ -z "${input}" ]]; then
    printf '%s' "${current}"
  else
    printf '%s' "${input}"
  fi
}

validate_paths() {
  "${PY_BIN}" - "$1" "$2" "$3" "$4" <<'PY'
import sys
from pathlib import Path

md = Path(sys.argv[1]).expanduser().resolve()
tb = Path(sys.argv[2]).expanduser().resolve()
bmd = Path(sys.argv[3]).expanduser().resolve()
btb = Path(sys.argv[4]).expanduser().resolve()

def inside(a: Path, b: Path) -> bool:
    try:
        a.relative_to(b)
        return True
    except Exception:
        return False

bad = False
if bmd == md or inside(bmd, md):
    print("backup_md")
    bad = True
if btb == tb or inside(btb, tb):
    print("backup_tb")
    bad = True
if bad:
    raise SystemExit(1)
PY
}

is_int() {
  [[ "$1" =~ ^[0-9]+$ ]]
}

is_float() {
  [[ "$1" =~ ^[0-9]+([.][0-9]+)?$ ]]
}

configure_paths() {
  if ! ensure_runtime; then
    return 1
  fi
  if ! ensure_config_exists; then
    return 1
  fi

  local folder_md folder_tb backup_md backup_tb
  local sync_interval write_quiet cooldown settle debounce retry conflict clean
  local clean_bool

  folder_md="$(json_get folder_md)"
  folder_tb="$(json_get folder_tb)"
  backup_md="$(json_get backup_md)"
  backup_tb="$(json_get backup_tb)"
  sync_interval="$(json_get sync_interval_seconds)"
  write_quiet="$(json_get write_quiet_seconds)"
  cooldown="$(json_get editor_cooldown_seconds)"
  settle="$(json_get bear_settle_seconds)"
  debounce="$(json_get daemon_debounce_seconds)"
  retry="$(json_get daemon_retry_seconds)"
  conflict="$(json_get conflict_backup_dir)"
  clean="$(json_get clean_export)"

  if [[ -z "${folder_md}" ]]; then folder_md="${HOME}/Documents/B2OU/MD_Export"; fi
  if [[ -z "${folder_tb}" ]]; then folder_tb="${HOME}/Documents/B2OU/TB_Export"; fi
  if [[ -z "${backup_md}" ]]; then backup_md="${HOME}/Documents/B2OU/MD_Backup"; fi
  if [[ -z "${backup_tb}" ]]; then backup_tb="${HOME}/Documents/B2OU/TB_Backup"; fi
  if [[ -z "${sync_interval}" ]]; then sync_interval="30"; fi
  if [[ -z "${write_quiet}" ]]; then write_quiet="30"; fi
  if [[ -z "${cooldown}" ]]; then cooldown="5"; fi
  if [[ -z "${settle}" ]]; then settle="3"; fi
  if [[ -z "${debounce}" ]]; then debounce="3.0"; fi
  if [[ -z "${retry}" ]]; then retry="5.0"; fi
  if [[ -z "${clean}" ]]; then clean="False"; fi

  say "请根据提示设置路径，直接回车可保留当前值。" \
      "Set values below. Press Enter to keep current values."

  folder_md="$(prompt_default "Markdown 导出目录" "Markdown export folder" "${folder_md}")"
  folder_tb="$(prompt_default "TextBundle 导出目录" "TextBundle export folder" "${folder_tb}")"
  backup_md="$(prompt_default "Markdown 备份目录（不可在导出目录内）" "Markdown backup folder (must be outside export)" "${backup_md}")"
  backup_tb="$(prompt_default "TextBundle 备份目录（不可在导出目录内）" "TextBundle backup folder (must be outside export)" "${backup_tb}")"
  sync_interval="$(prompt_default "同步间隔秒数（建议 30）" "Sync interval seconds (recommended 30)" "${sync_interval}")"
  write_quiet="$(prompt_default "写入静默秒数（建议 30）" "Write quiet seconds (recommended 30)" "${write_quiet}")"
  cooldown="$(prompt_default "编辑器冷却秒数（建议 5）" "Editor cooldown seconds (recommended 5)" "${cooldown}")"
  settle="$(prompt_default "Bear 数据库稳定等待秒数（建议 3）" "Bear settle seconds (recommended 3)" "${settle}")"
  debounce="$(prompt_default "守护进程防抖秒数（建议 3.0）" "Daemon debounce seconds (recommended 3.0)" "${debounce}")"
  retry="$(prompt_default "守护进程重试秒数（建议 5.0）" "Daemon retry seconds (recommended 5.0)" "${retry}")"
  conflict="$(prompt_default "冲突副本目录（可空）" "Conflict backup folder (optional)" "${conflict}")"
  clean="$(prompt_default "clean_export (True/False)" "clean_export (True/False)" "${clean}")"

  clean_bool="False"
  case "${clean}" in
    true|True|TRUE|1|yes|Yes|YES|y|Y) clean_bool="True" ;;
    *) clean_bool="False" ;;
  esac

  if ! validate_paths "${folder_md}" "${folder_tb}" "${backup_md}" "${backup_tb}" >/dev/null 2>&1; then
    say "备份目录不能与导出目录相同，也不能位于导出目录内部。配置未保存。" \
        "Backup folders must be outside export folders. Config not saved."
    return 1
  fi

  if ! is_int "${sync_interval}"; then sync_interval=30; fi
  if ! is_int "${write_quiet}"; then write_quiet=30; fi
  if ! is_int "${cooldown}"; then cooldown=5; fi
  if ! is_int "${settle}"; then settle=3; fi
  if ! is_float "${debounce}"; then debounce=3.0; fi
  if ! is_float "${retry}"; then retry=5.0; fi

  if [[ "${sync_interval}" -lt 30 ]]; then sync_interval=30; fi
  if [[ "${write_quiet}" -lt 5 ]]; then write_quiet=5; fi
  if [[ "${cooldown}" -lt 0 ]]; then cooldown=0; fi
  if [[ "${settle}" -lt 1 ]]; then settle=1; fi

  "${PY_BIN}" - "${CONFIG_FILE}" \
    "${folder_md}" "${folder_tb}" "${backup_md}" "${backup_tb}" \
    "${sync_interval}" "${write_quiet}" "${cooldown}" "${settle}" \
    "${debounce}" "${retry}" "${conflict}" "${clean_bool}" "${PY_BIN}" <<'PY'
import json
import sys
from pathlib import Path

cfg_path = Path(sys.argv[1])
folder_md, folder_tb, backup_md, backup_tb = sys.argv[2:6]
sync_interval, write_quiet, cooldown, settle = sys.argv[6:10]
debounce, retry, conflict, clean_bool, py_bin = sys.argv[10:15]

defaults = {
    "script_path": "b2ou",
    "python_path": "",
    "folder_md": "./Export/MD_Export",
    "folder_tb": "./Export/TB_Export",
    "backup_md": "./Backup/MD_Backup",
    "backup_tb": "./Backup/TB_Backup",
    "sync_interval_seconds": 30,
    "write_quiet_seconds": 30,
    "editor_cooldown_seconds": 5,
    "bear_settle_seconds": 3,
    "conflict_backup_dir": "",
    "daemon_debounce_seconds": 3.0,
    "daemon_retry_seconds": 5.0,
    "clean_export": False,
}

if cfg_path.exists():
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
else:
    data = {}

for k, v in defaults.items():
    data.setdefault(k, v)

data["folder_md"] = folder_md
data["folder_tb"] = folder_tb
data["backup_md"] = backup_md
data["backup_tb"] = backup_tb
data["sync_interval_seconds"] = int(sync_interval)
data["write_quiet_seconds"] = int(write_quiet)
data["editor_cooldown_seconds"] = int(cooldown)
data["bear_settle_seconds"] = int(settle)
data["daemon_debounce_seconds"] = float(debounce)
data["daemon_retry_seconds"] = float(retry)
data["conflict_backup_dir"] = conflict
data["clean_export"] = (clean_bool.lower() == "true")
data["python_path"] = py_bin

cfg_path.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")
print(cfg_path)
PY

  say "配置已保存：${CONFIG_FILE}" "Config saved: ${CONFIG_FILE}"
  return 0
}

run_sync_once() {
  local mode="$1"
  if ! ensure_runtime || ! ensure_config_exists; then
    return 1
  fi

  case "${mode}" in
    normal)
      say "开始执行单次智能同步..." "Running one-time smart sync..."
      "${PY_BIN}" -m b2ou sync --config "${CONFIG_FILE}" -v
      ;;
    force)
      say "开始执行强制同步..." "Running forced sync..."
      "${PY_BIN}" -m b2ou sync --config "${CONFIG_FILE}" --force -v
      ;;
    dry)
      say "开始预演（不写入）..." "Running dry-run (no writes)..."
      "${PY_BIN}" -m b2ou sync --config "${CONFIG_FILE}" --dry-run -v
      ;;
  esac
}

run_daemon_foreground() {
  if ! ensure_runtime || ! ensure_config_exists; then
    return 1
  fi
  say "即将以前台模式运行守护进程，按 Ctrl+C 可停止。" \
      "Starting daemon in foreground. Press Ctrl+C to stop."
  "${PY_BIN}" -m b2ou daemon --config "${CONFIG_FILE}" -v
}

run_guard_test() {
  if ! ensure_runtime || ! ensure_config_exists; then
    return 1
  fi
  "${PY_BIN}" -m b2ou guard-test --config "${CONFIG_FILE}" -v
}

open_config() {
  if ! ensure_runtime || ! ensure_config_exists; then
    return 1
  fi
  if command -v open >/dev/null 2>&1; then
    open "${CONFIG_FILE}"
  else
    say "无法打开编辑器，请手动编辑：${CONFIG_FILE}" \
        "Cannot open editor automatically. Edit file manually: ${CONFIG_FILE}"
  fi
}

tail_logs() {
  say "==== 同步日志（b2ou_sync.log）====" "==== Sync log (b2ou_sync.log) ===="
  tail -n 40 "${SCRIPT_DIR}/b2ou_sync.log" 2>/dev/null || true
  printf '\n'
  say "==== 守护日志（b2ou_daemon.log）====" "==== Daemon log (b2ou_daemon.log) ===="
  tail -n 40 "${SCRIPT_DIR}/b2ou_daemon.log" 2>/dev/null || true
  printf '\n'
  say "==== 开机启动日志（launchd）====" "==== Startup log (launchd) ===="
  tail -n 20 "${LAUNCHD_OUT_LOG}" 2>/dev/null || true
  tail -n 20 "${LAUNCHD_ERR_LOG}" 2>/dev/null || true
}

write_launchd_plist() {
  mkdir -p "${HOME}/Library/LaunchAgents"
  cat >"${LAUNCHD_PLIST}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PY_BIN}</string>
    <string>-m</string>
    <string>b2ou</string>
    <string>daemon</string>
    <string>--config</string>
    <string>${CONFIG_FILE}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>WorkingDirectory</key>
  <string>${SCRIPT_DIR}</string>
  <key>StandardOutPath</key>
  <string>${LAUNCHD_OUT_LOG}</string>
  <key>StandardErrorPath</key>
  <string>${LAUNCHD_ERR_LOG}</string>
</dict>
</plist>
EOF
}

launchd_install() {
  if ! ensure_runtime || ! ensure_config_exists; then
    return 1
  fi
  write_launchd_plist

  launchctl bootout "gui/${UID}" "${LAUNCHD_PLIST}" >/dev/null 2>&1 || true
  if launchctl bootstrap "gui/${UID}" "${LAUNCHD_PLIST}" >/dev/null 2>&1; then
    launchctl enable "gui/${UID}/${LAUNCHD_LABEL}" >/dev/null 2>&1 || true
    launchctl kickstart -k "gui/${UID}/${LAUNCHD_LABEL}" >/dev/null 2>&1 || true
    say "开机启动已安装并启动。" "Startup task installed and started."
    say "plist 路径：${LAUNCHD_PLIST}" "plist path: ${LAUNCHD_PLIST}"
    return 0
  fi

  if launchctl load -w "${LAUNCHD_PLIST}" >/dev/null 2>&1; then
    say "开机启动已安装（兼容模式）。" "Startup task installed (compat mode)."
    say "plist 路径：${LAUNCHD_PLIST}" "plist path: ${LAUNCHD_PLIST}"
    return 0
  fi

  say "开机启动安装失败，请检查 launchctl 权限或日志。" \
      "Failed to install startup task. Check launchctl permissions/logs."
  return 1
}

launchd_uninstall() {
  launchctl bootout "gui/${UID}" "${LAUNCHD_PLIST}" >/dev/null 2>&1 || true
  launchctl unload -w "${LAUNCHD_PLIST}" >/dev/null 2>&1 || true
  rm -f "${LAUNCHD_PLIST}"
  say "开机启动已卸载。" "Startup task uninstalled."
}

launchd_start_now() {
  if [[ ! -f "${LAUNCHD_PLIST}" ]]; then
    say "未检测到开机启动配置，请先安装。" \
        "Startup plist not found. Install it first."
    return 1
  fi
  launchctl kickstart -k "gui/${UID}/${LAUNCHD_LABEL}" >/dev/null 2>&1 || true
  say "已尝试启动后台任务。" "Tried to start background task."
}

launchd_stop_now() {
  launchctl kill SIGTERM "gui/${UID}/${LAUNCHD_LABEL}" >/dev/null 2>&1 || true
  say "已尝试停止后台任务。" "Tried to stop background task."
}

launchd_status() {
  if launchctl print "gui/${UID}/${LAUNCHD_LABEL}" >/dev/null 2>&1; then
    say "后台任务状态：已加载" "Startup task status: loaded"
    launchctl print "gui/${UID}/${LAUNCHD_LABEL}" | sed -n '1,40p'
  else
    say "后台任务状态：未加载" "Startup task status: not loaded"
  fi
}

one_click_setup() {
  if ! ensure_macos; then
    return 1
  fi
  if ! install_dependencies; then
    return 1
  fi
  if ! ensure_config_exists; then
    return 1
  fi
  configure_paths || true
  say "初始化完成。你现在可以执行单次同步或安装开机启动。" \
      "Setup complete. You can now run one-time sync or install startup."
}

menu() {
  while true; do
    printf '\n'
    say "================ B2OU 新手菜单 ================" \
        "================ B2OU Beginner Menu ================"
    say "1) 一键初始化（推荐）" "1) One-click setup (recommended)"
    say "2) 配置向导（修改导出/备份路径）" "2) Config wizard (edit export/backup paths)"
    say "3) 执行单次智能同步" "3) Run one-time smart sync"
    say "4) 执行强制同步（忽略守卫）" "4) Run forced sync (bypass guards)"
    say "5) 执行预演（不写入）" "5) Run dry-run (no writes)"
    say "6) 前台运行守护进程" "6) Run daemon in foreground"
    say "7) 守卫检测（guard-test）" "7) Guard diagnostic (guard-test)"
    say "8) 安装开机启动（launchd）" "8) Install startup task (launchd)"
    say "9) 卸载开机启动（launchd）" "9) Uninstall startup task (launchd)"
    say "10) 立即启动后台任务" "10) Start startup task now"
    say "11) 立即停止后台任务" "11) Stop startup task now"
    say "12) 查看后台任务状态" "12) Check startup task status"
    say "13) 查看日志" "13) View logs"
    say "14) 打开配置文件" "14) Open config file"
    say "q) 退出" "q) Quit"
    ask "请输入选项: " "Choose an option: "
    local choice="${REPLY}"

    case "${choice}" in
      1) one_click_setup; pause_wait ;;
      2) configure_paths; pause_wait ;;
      3) run_sync_once normal; pause_wait ;;
      4) run_sync_once force; pause_wait ;;
      5) run_sync_once dry; pause_wait ;;
      6) run_daemon_foreground; pause_wait ;;
      7) run_guard_test; pause_wait ;;
      8) launchd_install; pause_wait ;;
      9) launchd_uninstall; pause_wait ;;
      10) launchd_start_now; pause_wait ;;
      11) launchd_stop_now; pause_wait ;;
      12) launchd_status; pause_wait ;;
      13) tail_logs; pause_wait ;;
      14) open_config; pause_wait ;;
      q|Q|quit|exit) break ;;
      *) say "无效选项，请重试。" "Invalid option. Try again."; pause_wait ;;
    esac
  done
}

choose_language
menu
