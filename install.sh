#!/usr/bin/env bash
# =============================================================================
# WorkBuddy 每日自动签到 —— Linux 多账号安装器
#
#   共享部分（只需装一次）：
#     sudo ./install.sh
#
#   账号管理：
#     sudo ./install.sh --add-account 账号名 [--time HH:MM]
#     sudo ./install.sh --remove-account 账号名
#     sudo ./install.sh --list
#     sudo ./install.sh --uninstall [--purge]
#
#   安装完成后，每个账号的日常用法：
#     <安装目录>/run.sh <账号名> doctor        # 自检
#     <安装目录>/run.sh <账号名> claim         # 试跑
#     <安装目录>/login-cli.sh <账号名>          # 登录 CodeBuddy CLI（推荐）
#     <安装目录>/refresh.sh <账号名> --check    # 看令牌还剩多久
#     <安装目录>/refresh.sh <账号名> --force    # 立即续期一次（验证用）
#     <安装目录>/set-token.sh <账号名>          # 或注入明文令牌（约 28 天过期）
#     <安装目录>/audit-accounts.sh             # 多账号体检（查串号）
#
#   自动续期：登录方式拿到的凭据 = accessToken 28 天 + refreshToken 30 天。
#   每个账号的 systemd 服务在每次签到前会跑一次 refresh.sh，
#   refreshToken 剩余不足 20 天时就用它滚动换新（约每 10 天一次），
#   因此登录一次即可长期免扫码。
#
#   隔离设计：每个账号拥有各自的
#     凭据文件  /etc/workbuddy-signin/accounts/<名>.env
#     独立 HOME /root/.wb-acct-<名>              ← 多账号隔离的关键，见下方说明
#     锁文件    /run/workbuddy-signin/<名>.lock  （多账号必须分开，否则互相阻塞）
#     日志      /var/log/workbuddy-signin/<名>.log
#     systemd   workbuddy-signin@<名>.timer      （实例独立、可错峰）
#
#   为什么用「独立 HOME」而不是 CODEBUDDY_CONFIG_DIR：
#     CodeBuddy CLI 把登录凭据写在 <HOME>/.local/share/CodeBuddyExtension/Data/Public/auth/
#     （2.164.0 实测；该路径写死在 HOME 下，不读 XDG_DATA_HOME），
#     而 CODEBUDDY_CONFIG_DIR 只隔离会话/插件数据、**不隔离凭据**。
#     所以每个账号必须有自己的 HOME，否则第二次登录会覆盖第一次，
#     两个账号最终共用同一份令牌。
# =============================================================================
set -euo pipefail

SCRIPT_NAME="signin_linux.py"
REFRESH_NAME="refresh_token.py"
RUN_NAME="run.sh"
REFRESH_RUN_NAME="refresh.sh"
SERVICE_TMPL="workbuddy-signin@"
DEFAULT_TIME="00:05"
STAGGER_MINUTES=30

INSTALL_DIR=""
ACTION="install"
ACCOUNT=""
SCHEDULE_TIME=""
PURGE=0

log()  { printf '\033[32m[+]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dir)            INSTALL_DIR="${2:?--dir 需要路径}"; shift 2 ;;
    --add-account)    ACTION="add";    ACCOUNT="${2:?--add-account 需要账号名}"; shift 2 ;;
    --remove-account) ACTION="remove"; ACCOUNT="${2:?--remove-account 需要账号名}"; shift 2 ;;
    --list)           ACTION="list"; shift ;;
    --time)           SCHEDULE_TIME="${2:?--time 需要 HH:MM}"; shift 2 ;;
    --uninstall)      ACTION="uninstall"; shift ;;
    --purge)          PURGE=1; shift ;;
    -h|--help)        sed -n '2,26p' "$0"; exit 0 ;;
    *)                die "未知参数：$1（--help 查看用法）" ;;
  esac
done

# ---------------------------------------------------------------- 环境探测
IS_ROOT=0
[ "$(id -u)" -eq 0 ] && IS_ROOT=1

if [ -z "$INSTALL_DIR" ]; then
  if [ "$IS_ROOT" -eq 1 ]; then INSTALL_DIR="/opt/workbuddy-signin"
  else INSTALL_DIR="$HOME/.local/share/workbuddy-signin"; fi
fi

if [ "$IS_ROOT" -eq 1 ] && [ "${INSTALL_DIR#/opt}" != "$INSTALL_DIR" ]; then
  CRED_DIR="/etc/workbuddy-signin/accounts"
  LOCK_DIR="/run/workbuddy-signin"
  LOG_DIR="/var/log/workbuddy-signin"
  ACCT_HOME_BASE="/root/.wb-acct"
else
  CRED_DIR="$INSTALL_DIR/accounts"
  LOCK_DIR="$INSTALL_DIR/run"
  LOG_DIR="$INSTALL_DIR/logs"
  ACCT_HOME_BASE="$HOME/.wb-acct"
fi

pick_python() {
  if [ -n "${WORKBUDDY_PYTHON:-}" ]; then printf '%s\n' "$WORKBUDDY_PYTHON"; return; fi
  for p in /usr/bin/python3.13 /usr/bin/python3.12 /usr/bin/python3.11 \
           /usr/bin/python3.10 /usr/bin/python3.9 /usr/bin/python3.8 \
           /usr/bin/python3.7 /usr/local/bin/python3; do
    [ -x "$p" ] && { printf '%s\n' "$p"; return; }
  done
  for p in python3 python; do
    c="$(command -v "$p" 2>/dev/null)" || continue
    [ -n "$c" ] && { printf '%s\n' "$c"; return; }
  done
}
try_python() {
  [ -n "$1" ] && [ -x "$1" ] || return 1
  "$1" -c 'import sys; sys.exit(0 if (3, 7) <= sys.version_info[:2] < (4, 0) else 1)' 2>/dev/null
}
PYTHON="$(pick_python)"
if ! try_python "$PYTHON"; then
  PYTHON=""
  for cand in python3.12 python3.11 python3.10 python3.9 python3.8 python3.7 python3 python; do
    p="$(command -v "$cand" 2>/dev/null)" || continue
    if try_python "$p"; then PYTHON="$p"; break; fi
  done
fi

HAVE_SYSTEMD=0
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ] && [ "$IS_ROOT" -eq 1 ]; then
  HAVE_SYSTEMD=1
fi

SRC_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

# 把文件装到目标位置；如果源和目标本来就是同一个文件
# （直接在被安装的目录里运行 install.sh），跳过复制只改权限 ——
# 否则 cp 会报 "are the same file"，而 set -e 会让整个安装中断。
install_file() {
  _src="$1"; _dst="$2"; _mode="$3"
  if [ -e "$_dst" ] && \
     [ "$(CDPATH= cd -- "$(dirname -- "$_src")" && pwd)/$(basename -- "$_src")" = \
       "$(CDPATH= cd -- "$(dirname -- "$_dst")" && pwd)/$(basename -- "$_dst")" ]; then
    :
  else
    cp -f "$_src" "$_dst"
  fi
  chmod "$_mode" "$_dst"
}

valid_account() {
  case "$1" in
    ''|*[!A-Za-z0-9_-]*) return 1 ;;
    *) return 0 ;;
  esac
}

hhmm_from_minutes() {
  m=$(( $1 % 1440 )); [ "$m" -lt 0 ] && m=$(( m + 1440 ))
  printf '%02d:%02d\n' $(( m / 60 )) $(( m % 60 ))
}

existing_accounts() {
  [ -d "$CRED_DIR" ] || return 0
  for f in "$CRED_DIR"/*.env; do
    [ -f "$f" ] || continue
    b="$(basename -- "$f")"
    printf '%s\n' "${b%.env}"
  done
}

account_timer() { printf '%s%s.timer' "$SERVICE_TMPL" "$1"; }

# ================================================================ 卸载
if [ "$ACTION" = "uninstall" ]; then
  log "开始卸载（安装目录：$INSTALL_DIR）"
  if [ "$HAVE_SYSTEMD" -eq 1 ]; then
    for acct in $(existing_accounts); do
      systemctl disable --now "$(account_timer "$acct")" 2>/dev/null || true
      rm -rf "/etc/systemd/system/$(account_timer "$acct").d"
    done
    rm -f "/etc/systemd/system/${SERVICE_TMPL}.service" \
          "/etc/systemd/system/${SERVICE_TMPL}.timer"
    systemctl daemon-reload 2>/dev/null || true
  fi
  if command -v crontab >/dev/null 2>&1; then
    crontab -l 2>/dev/null | grep -v "$INSTALL_DIR/$RUN_NAME" | crontab - || true
  fi
  [ -f "/etc/cron.d/workbuddy-signin" ] && rm -f "/etc/cron.d/workbuddy-signin"
  rm -rf "$INSTALL_DIR"
  if [ "$PURGE" -eq 1 ]; then
    rm -rf "$CRED_DIR" "$LOG_DIR"
    warn "已连同账号配置与日志一并删除（--purge）"
  else
    warn "账号配置保留在 $CRED_DIR（含令牌），如需删除请加 --purge"
  fi
  log "卸载完成。"
  exit 0
fi

# ================================================================ 列出账号
if [ "$ACTION" = "list" ]; then
  accts="$(existing_accounts)"
  if [ -z "$accts" ]; then
    warn "还没有任何账号。先执行：$0 --add-account <账号名>"
    exit 0
  fi
  printf '%-14s | %-34s | %s\n' "账号" "定时状态（systemd）" "上次日志"
  printf -- '---------------|------------------------------------|----------------------\n'
  for acct in $accts; do
    if [ "$HAVE_SYSTEMD" -eq 1 ]; then
      timer="$(account_timer "$acct")"
      tstate="inactive"
      systemctl is-active "$timer" >/dev/null 2>&1 && tstate="active"
      tnext="$(systemctl list-timers "$timer" --no-pager --no-legend 2>/dev/null \
               | awk '{print $1" "$2" "$3}' | head -1)"
      [ -n "$tnext" ] && tstate="$tstate / $tnext"
    else
      tstate="(无 systemd，走 crontab)"
    fi
    logf="$LOG_DIR/$acct.log"
    last="(无日志)"
    [ -f "$logf" ] && last="$(tail -1 "$logf" 2>/dev/null | cut -c1-60)"
    printf '%-14s | %-34s | %s\n' "$acct" "$tstate" "$last"
  done
  exit 0
fi

# ================================================================ 移除账号
if [ "$ACTION" = "remove" ]; then
  valid_account "$ACCOUNT" || die "账号名只能包含字母/数字/下划线/连字符：$ACCOUNT"
  if [ "$HAVE_SYSTEMD" -eq 1 ]; then
    systemctl disable --now "$(account_timer "$ACCOUNT")" 2>/dev/null || true
    rm -rf "/etc/systemd/system/$(account_timer "$ACCOUNT").d"
    systemctl daemon-reload 2>/dev/null || true
  fi
  if [ -f "$CRED_DIR/$ACCOUNT.env" ]; then
    rm -f "$CRED_DIR/$ACCOUNT.env"; log "已删除凭据 $CRED_DIR/$ACCOUNT.env"
  fi
  if [ -d "$ACCT_HOME_BASE-$ACCOUNT" ]; then
    rm -rf "$ACCT_HOME_BASE-$ACCOUNT"; log "已删除账号 HOME $ACCT_HOME_BASE-$ACCOUNT"
  fi
  log "账号 $ACCOUNT 已移除（日志保留在 $LOG_DIR/$ACCOUNT.log）"
  exit 0
fi

[ -n "$PYTHON" ] || die "未找到 Python 3.7+ 解释器。可先安装：dnf install -y python3.12，或用 WORKBUDDY_PYTHON=/path/to/python3 指定"

# ================================================================ 共享部分
install_shared() {
  [ -f "$SRC_DIR/$SCRIPT_NAME" ] || die "同目录下找不到 $SCRIPT_NAME，请把它与本脚本放在一起。"

  log "Python：$PYTHON"
  log "安装目录：$INSTALL_DIR"
  mkdir -p "$INSTALL_DIR" "$CRED_DIR"
  chmod 700 "$CRED_DIR" 2>/dev/null || true

  install_file "$SRC_DIR/$SCRIPT_NAME" "$INSTALL_DIR/$SCRIPT_NAME" 0755

  if [ -f "$SRC_DIR/$REFRESH_NAME" ]; then
    install_file "$SRC_DIR/$REFRESH_NAME" "$INSTALL_DIR/$REFRESH_NAME" 0755
  else
    warn "未找到 $REFRESH_NAME，自动续期功能将不可用"
  fi

  for extra in set-token.sh login-cli.sh audit-accounts.sh; do
    if [ -f "$SRC_DIR/$extra" ]; then
      install_file "$SRC_DIR/$extra" "$INSTALL_DIR/$extra" 0700
    else
      warn "未找到 $extra，跳过"
    fi
  done

  # ---- 运行包装：按账号加载凭据后再执行（systemd / cron / 手动共用）
  # 注意 set -a：只有导出过的变量才会被子进程看到，少了它
  # . <账号>.env 设的令牌不会传给 exec 出去的 Python。
  cat > "$INSTALL_DIR/$RUN_NAME" <<'RUNSH'
#!/bin/sh
# 由 install.sh 自动生成 —— 按账号加载凭据后执行签到脚本。请勿手工修改。
set -eu
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ACCT="${1:-}"
[ -n "$ACCT" ] || { echo "用法: run.sh <账号名> [claim|status|doctor]" >&2; exit 64; }
shift
CRED_DIR="${WORKBUDDY_CRED_DIR:-__CRED_DIR__}"
CRED_FILE="$CRED_DIR/$ACCT.env"
[ -f "$CRED_FILE" ] || { echo "找不到账号配置：$CRED_FILE（先执行 install.sh --add-account $ACCT）" >&2; exit 66; }
set -a
. "$CRED_FILE"
set +a
export WORKBUDDY_ACCOUNT="${WORKBUDDY_ACCOUNT:-$ACCT}"
export WORKBUDDY_LOCK_FILE="${WORKBUDDY_LOCK_FILE:-__LOCK_DIR__/$ACCT.lock}"
export WORKBUDDY_SIGNIN_LOG="${WORKBUDDY_SIGNIN_LOG:-__LOG_DIR__/$ACCT.log}"
mkdir -p "$(dirname -- "$WORKBUDDY_LOCK_FILE")" "$(dirname -- "$WORKBUDDY_SIGNIN_LOG")" 2>/dev/null || true
exec __PYTHON__ "$DIR/__SCRIPT_NAME__" "${@:-claim}"
RUNSH
  sed -i.bak \
      -e "s|__PYTHON__|$PYTHON|g" \
      -e "s|__CRED_DIR__|$CRED_DIR|g" \
      -e "s|__LOCK_DIR__|$LOCK_DIR|g" \
      -e "s|__LOG_DIR__|$LOG_DIR|g" \
      -e "s|__SCRIPT_NAME__|$SCRIPT_NAME|g" \
      "$INSTALL_DIR/$RUN_NAME"
  rm -f "$INSTALL_DIR/$RUN_NAME.bak"
  chmod 0755 "$INSTALL_DIR/$RUN_NAME"

  # ---- 续期包装：同 run.sh 的加载方式，但调用 refresh_token.py。
  # 令牌到期前靠它滚动续期，服务器就永远不需要重新扫码。
  cat > "$INSTALL_DIR/$REFRESH_RUN_NAME" <<'REFSH'
#!/bin/sh
# 由 install.sh 自动生成 —— 按账号加载凭据后执行令牌自动续期。请勿手工修改。
set -eu
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ACCT="${1:-}"
[ -n "$ACCT" ] || { echo "用法: refresh.sh <账号名> [--force|--check|--json]" >&2; exit 64; }
shift
CRED_DIR="${WORKBUDDY_CRED_DIR:-__CRED_DIR__}"
CRED_FILE="$CRED_DIR/$ACCT.env"
[ -f "$CRED_FILE" ] || { echo "找不到账号配置：$CRED_FILE（先执行 install.sh --add-account $ACCT）" >&2; exit 66; }
set -a
. "$CRED_FILE"
set +a
export WORKBUDDY_ACCOUNT="${WORKBUDDY_ACCOUNT:-$ACCT}"
export WORKBUDDY_REFRESH_LOG="${WORKBUDDY_REFRESH_LOG:-__LOG_DIR__/$ACCT.refresh.log}"
mkdir -p "$(dirname -- "$WORKBUDDY_REFRESH_LOG")" 2>/dev/null || true
exec __PYTHON__ "$DIR/__REFRESH_NAME__" ${1+"$@"}
REFSH
  sed -i.bak \
      -e "s|__PYTHON__|$PYTHON|g" \
      -e "s|__CRED_DIR__|$CRED_DIR|g" \
      -e "s|__LOG_DIR__|$LOG_DIR|g" \
      -e "s|__REFRESH_NAME__|$REFRESH_NAME|g" \
      "$INSTALL_DIR/$REFRESH_RUN_NAME"
  rm -f "$INSTALL_DIR/$REFRESH_RUN_NAME.bak"
  chmod 0755 "$INSTALL_DIR/$REFRESH_RUN_NAME"

  if [ "$HAVE_SYSTEMD" -eq 1 ]; then
    cat > "/etc/systemd/system/${SERVICE_TMPL}.service" <<EOF
[Unit]
Description=WorkBuddy 每日自动签到领积分（账号 %i）
Documentation=file:$INSTALL_DIR/$SCRIPT_NAME
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
# 签到前先尝试续期：令牌快到 30 天上限时自动滚动刷新，失败不影响签到
ExecStartPre=-$INSTALL_DIR/$REFRESH_RUN_NAME %i
ExecStart=$INSTALL_DIR/$RUN_NAME %i claim
TimeoutStartSec=900
EOF
    cat > "/etc/systemd/system/${SERVICE_TMPL}.timer" <<EOF
[Unit]
Description=WorkBuddy 签到定时器（账号 %i）

[Timer]
OnCalendar=*-*-* $DEFAULT_TIME:00
RandomizedDelaySec=300
Persistent=true
Unit=${SERVICE_TMPL}%i.service

[Install]
WantedBy=timers.target
EOF
    systemctl daemon-reload
    log "已安装 systemd 模板单元：${SERVICE_TMPL}{service,timer}"
  else
    warn "未检测到可用 systemd（或当前非 root），账号定时将写入 crontab。"
  fi
}

setup_account_schedule() {
  acct="$1"; time="$2"
  hh="${time%%:*}"; mm="${time##*:}"
  hh=$((10#$hh)); mm=$((10#$mm))

  if [ "$HAVE_SYSTEMD" -eq 1 ]; then
    DROPIN="/etc/systemd/system/$(account_timer "$acct").d"
    mkdir -p "$DROPIN"
    cat > "$DROPIN/10-schedule.conf" <<EOF
# 由 install.sh --add-account 生成：账号 $acct 的签到时间（错峰，避免多账号同时请求）
[Timer]
OnCalendar=
OnCalendar=*-*-* $hh:$mm:00
RandomizedDelaySec=300
Persistent=true
EOF
    systemctl daemon-reload
    systemctl enable --now "$(account_timer "$acct")" >/dev/null
    log "定时器已启用：$(account_timer "$acct")（每天 $time ± 300s，Persistent=true）"
  else
    LINE="$mm $hh * * * $INSTALL_DIR/$RUN_NAME $acct claim >/dev/null 2>&1"
    if [ "$IS_ROOT" -eq 1 ]; then
      CRONFILE="/etc/cron.d/workbuddy-signin"
      touch "$CRONFILE"
      grep -v " $acct claim" "$CRONFILE" > "$CRONFILE.tmp" || true
      mv "$CRONFILE.tmp" "$CRONFILE"
      {
        printf '# WorkBuddy 签到（由 install.sh 生成）\n'
        printf 'SHELL=/bin/sh\n'
        printf 'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n'
        printf '%s\n' "$LINE"
      } >> "$CRONFILE"
      chmod 644 "$CRONFILE"
      log "已写入 $CRONFILE：$LINE"
    else
      EXIST="$(crontab -l 2>/dev/null | grep -v " $acct claim" || true)"
      printf '%s\n%s\n' "$EXIST" "$LINE" | crontab -
      log "已写入当前用户 crontab：$LINE"
    fi
  fi
}

create_account_env() {
  acct="$1"
  envf="$CRED_DIR/$acct.env"
  if [ -f "$envf" ]; then
    warn "账号配置已存在，保留不动：$envf"
    return 1
  fi
  umask 077
  cat > "$envf" <<EOF
# ============================================================
# WorkBuddy 签到 —— 账号 $acct
#   A. 推荐：用 CodeBuddy CLI 登录，脚本会自动读取凭据
#        $INSTALL_DIR/login-cli.sh $acct
#   B. 备选：手动注入明文令牌（约 28 天过期，需定期重注入）
#        $INSTALL_DIR/set-token.sh $acct
# ============================================================
WORKBUDDY_ACCOUNT=$acct

# 本账号专属 HOME（多账号隔离的关键：CLI 凭据写在 <HOME>/.local/share 下）
WORKBUDDY_HOME=$ACCT_HOME_BASE-$acct

# 本账号专属的锁文件与日志（多账号必须各不相同）
WORKBUDDY_LOCK_FILE=$LOCK_DIR/$acct.lock
WORKBUDDY_SIGNIN_LOG=$LOG_DIR/$acct.log

# ---- 方式 A：保持为空，由 CLI 凭据自动探测 ----
WORKBUDDY_ACCESS_TOKEN=
WORKBUDDY_UID=

# ---- 方式 B：直接指向明文凭据 JSON 文件（可选，优先于自动探测）----
# WORKBUDDY_AUTH_FILE=$ACCT_HOME_BASE-$acct/.local/share/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info

# ---- 高级：接口地址（默认官方，请勿修改）----
# WORKBUDDY_API_BASE=https://copilot.tencent.com
EOF
  chmod 600 "$envf"
  chown root:root "$envf" 2>/dev/null || true
  return 0
}

# ================================================================ 添加账号
if [ "$ACTION" = "add" ]; then
  valid_account "$ACCOUNT" || die "账号名只能包含字母/数字/下划线/连字符：$ACCOUNT"

  if [ ! -f "$INSTALL_DIR/$SCRIPT_NAME" ] || [ ! -f "$INSTALL_DIR/$RUN_NAME" ]; then
    log "未检测到已安装的共享部分，先执行安装…"
    install_shared
  fi

  if [ -z "$SCHEDULE_TIME" ]; then
    idx=0
    for _a in $(existing_accounts); do idx=$(( idx + 1 )); done
    SCHEDULE_TIME="$(hhmm_from_minutes $(( 5 + STAGGER_MINUTES * idx )))"
    log "未指定 --time，按第 $(( idx + 1 )) 个账号自动错峰为 $SCHEDULE_TIME"
  fi
  case "$SCHEDULE_TIME" in
    [0-9]:[0-9][0-9]|0[0-9]:[0-9][0-9]|1[0-9]:[0-9][0-9]|2[0-3]:[0-9][0-9]) ;;
    *) die "--time 需要 HH:MM（24 小时制），收到：$SCHEDULE_TIME" ;;
  esac

  if create_account_env "$ACCOUNT"; then
    mkdir -p "$ACCT_HOME_BASE-$ACCOUNT"
    chmod 700 "$ACCT_HOME_BASE-$ACCOUNT" 2>/dev/null || true
    log "已创建账号配置：$CRED_DIR/$ACCOUNT.env（600）"
    log "账号 HOME：$ACCT_HOME_BASE-$ACCOUNT（独立隔离，CLI 登录态写在这里）"
  fi

  setup_account_schedule "$ACCOUNT" "$SCHEDULE_TIME"

  echo
  log "账号 $ACCOUNT 添加完成。下一步（二选一拿凭据）："
  echo "  ① 推荐（自动续期）：$INSTALL_DIR/login-cli.sh $ACCOUNT"
  echo "  ② 备选（28 天过期）：$INSTALL_DIR/set-token.sh $ACCOUNT"
  echo "  ③ 自检：            $INSTALL_DIR/$RUN_NAME $ACCOUNT doctor"
  echo "  ④ 试跑：            $INSTALL_DIR/$RUN_NAME $ACCOUNT claim"
  echo "  ⑤ 令牌续期状态：    $INSTALL_DIR/$REFRESH_RUN_NAME $ACCOUNT --check"
  echo "  ⑥ 多账号体检：      $INSTALL_DIR/audit-accounts.sh"
  exit 0
fi

# ================================================================ 仅安装共享部分
install_shared

echo
log "共享部分安装完成。接下来为每个账号执行："
echo "  $0 --add-account <账号名>          # 可重复执行，自动错峰定时"
echo "  $0 --list                          # 查看所有账号与定时状态"
if [ -n "$(existing_accounts)" ]; then
  log "已存在的账号：$(existing_accounts | tr '\n' ' ')"
fi
warn "提醒：登录方式拿到的令牌由 refresh.sh 在每次签到前自动续期（refreshToken 30 天上限，"
warn "      剩余不足 20 天就滚动换新）→ 正常情况下无需再扫码；"
warn "      明文令牌方式没有 refreshToken，约 28 天过期，接口返回 401 时用 set-token.sh 重新注入。"
