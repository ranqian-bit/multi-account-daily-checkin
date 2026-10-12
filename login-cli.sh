#!/bin/sh
# =============================================================================
# CodeBuddy CLI 登录驱动（服务器无图形界面时用 tmux 顶替伪终端）
#
#   用法：
#     ./login-cli.sh <账号名>          # 拉起登录界面并打印一次性登录链接
#     ./login-cli.sh <账号名> --wait   # 打印链接后继续等待，直到登录完成
#     ./login-cli.sh <账号名> --show   # 只显示当前界面（排错用）
#     ./login-cli.sh <账号名> --kill   # 关掉该账号的登录会话
#
#   原理：CLI 没有 login 子命令，无头运行会直接报 Authentication required，
#         必须在交互式 TUI 里登录。服务器上用一个可捕获的 tmux 会话来驱动。
#         登录 URL 是「服务端轮询」模式（无 redirect_uri），
#         所以你在任何设备/浏览器的网络里打开它都行，**不需要 SSH 隧道**。
#
#   每个账号用独立的 **HOME**（不是 CODEBUDDY_CONFIG_DIR），登录态互不干扰。
#   —— 实测 CLI 2.164.0 把凭据写在 <HOME>/.local/share/CodeBuddyExtension/Data/Public/auth/，
#      该路径写死在 HOME 下、不读 XDG_DATA_HOME，而 CODEBUDDY_CONFIG_DIR 只隔离
#      会话/插件数据。用配置目录隔离会导致两个账号共用一个凭据文件。
# =============================================================================
set -eu

ACCT="${1:-}"
[ -n "$ACCT" ] || { echo "用法: login-cli.sh <账号名> [--wait|--show|--kill]" >&2; exit 64; }
shift

MODE="run"
for a in "$@"; do
  case "$a" in
    --wait) MODE="wait" ;;
    --show) MODE="show" ;;
    --kill) MODE="kill" ;;
    *) echo "未知参数：$a" >&2; exit 64 ;;
  esac
done

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SESS="cb-login-$ACCT"

# ---- 定位该账号的配置文件（与 install.sh 的 CRED_DIR 规则一致）
CRED_FILE=""
for c in "${WORKBUDDY_CRED_DIR:-}" /etc/workbuddy-signin/accounts "$DIR/accounts"; do
  [ -n "$c" ] || continue
  if [ -f "$c/$ACCT.env" ]; then CRED_FILE="$c/$ACCT.env"; break; fi
done
[ -n "$CRED_FILE" ] || {
  echo "找不到账号配置：$ACCT.env（先执行 install.sh --add-account $ACCT）" >&2
  exit 66
}

set -a
. "$CRED_FILE"
set +a

# 本账号专属 HOME（CLI 的登录态就落在这里）
WB_HOME="${WORKBUDDY_HOME:-${CODEBUDDY_HOME:-}}"
[ -n "$WB_HOME" ] || WB_HOME="$HOME/.wb-acct-$ACCT"

# 凭据文件落点（CLI 实测路径）
AUTH_FILE="$WB_HOME/.local/share/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info"
AUTH_DIR="$(dirname -- "$AUTH_FILE")"

# ---- 定位 codebuddy 可执行文件（用调用方自己的 HOME 去找安装位置）
CB=""
for c in "${CODEBUDDY_BIN:-}" /usr/local/bin/codebuddy "$HOME/.local/bin/codebuddy"; do
  [ -n "$c" ] && [ -x "$c" ] && { CB="$c"; break; }
done
if [ -z "$CB" ]; then
  c="$(command -v codebuddy 2>/dev/null || true)"
  [ -n "$c" ] && CB="$c"
fi

command -v tmux >/dev/null 2>&1 || {
  echo "缺少 tmux。安装（Alibaba Cloud Linux / RHEL 系）：dnf install -y tmux" >&2
  echo "                                        （Debian/Ubuntu：apt install -y tmux）" >&2
  exit 69
}

# ---------------------------------------------------------------- 子命令
if [ "$MODE" = "kill" ]; then
  tmux kill-session -t "$SESS" 2>/dev/null || true
  echo "已关闭会话 $SESS"
  exit 0
fi

if [ "$MODE" = "show" ]; then
  echo "--- $SESS 当前界面 ---"
  tmux capture-pane -p -t "$SESS" 2>/dev/null || echo "(会话不存在)"
  echo "----------------------"
  echo "账号 HOME：$WB_HOME"
  echo "凭据落点：$AUTH_FILE"
  found=""
  for f in "$AUTH_FILE" $(ls "$AUTH_DIR"/*.info 2>/dev/null || true); do
    [ -f "$f" ] || continue
    found="$found
  $f ($(wc -c < "$f" 2>/dev/null) 字节)"
  done
  if [ -n "$found" ]; then echo "已发现凭据文件：$found"; else echo "尚未发现凭据文件。"; fi
  exit 0
fi

# 找出该账号 HOME 下的凭据文件（标准落点优先，再兜底通配）
find_cred() {
  if [ -f "$AUTH_FILE" ]; then printf '%s\n' "$AUTH_FILE"; return 0; fi
  for f in $(ls "$AUTH_DIR"/*.info "$AUTH_DIR"/*.json 2>/dev/null || true); do
    [ -f "$f" ] && { printf '%s\n' "$f"; return 0; }
  done
  return 1
}

[ -n "$CB" ] || {
  echo "找不到 codebuddy 可执行文件。先安装：" >&2
  echo "  curl -fsSL https://copilot.tencent.com/cli/install.sh | bash" >&2
  echo "或用 CODEBUDDY_BIN=/path/to/codebuddy 指定。" >&2
  exit 69
}

# ---------------------------------------------------------------- 拉起并驱动
mkdir -p "$WB_HOME" "$AUTH_DIR"
chmod 700 "$WB_HOME" 2>/dev/null || true

tmux kill-session -t "$SESS" 2>/dev/null || true
tmux new-session -d -s "$SESS" -x 220 -y 50 \
    "HOME='$WB_HOME' exec '$CB'"

printf '正在启动 CodeBuddy CLI（账号 %s）…\n' "$ACCT"
printf '账号 HOME：%s（与其它账号完全隔离）\n\n' "$WB_HOME"

sleep 3                       # 等 Node 应用接管终端，太早发按键会被吞掉

URL=""
STAGE=""
tries=0
limit=60                      # 60 × 2s = 2 分钟拿到链接（之后若 --wait 继续等）
n_trust=0; n_method=0; n_auth=0

# 交互提示可能因为「按键发早了」被吞掉，所以同一个提示要**反复重试**，
# 并交替使用 Enter / C-m 两种等价写法（这是本脚本最容易踩的坑）。
nudge() {                       # $1=会话 $2=重试计数
    if [ $(( $2 % 2 )) -eq 1 ]; then
        tmux send-keys -t "$1" Enter
    else
        tmux send-keys -t "$1" C-m
    fi
}

while [ "$tries" -lt "$limit" ]; do
  tries=$(( tries + 1 ))
  sleep 2
  pane="$(tmux capture-pane -p -t "$SESS" 2>/dev/null || true)"
  [ -n "$pane" ] || continue

  # 1) 首次启动的信任目录提示
  case "$pane" in
    *"trust the files"*|*"Do you trust"*)
      n_trust=$(( n_trust + 1 ))
      [ "$n_trust" -le 6 ] && nudge "$SESS" "$n_trust"
      ;;
    *) n_trust=0 ;;
  esac
  # 2) 登录方式选择（默认 Chinese Site，直接回车）
  case "$pane" in
    *"Select login method"*)
      n_method=$(( n_method + 1 ))
      [ "$n_method" -le 6 ] && nudge "$SESS" "$n_method"
      ;;
  esac
  # 3) 未登录提示（兜底：显式发 /login）
  case "$pane" in
    *"Authentication required"*)
      n_auth=$(( n_auth + 1 ))
      if [ "$n_auth" -le 4 ]; then tmux send-keys -t "$SESS" "/login" Enter; fi
      ;;
  esac

  # 4) 抓取一次性登录链接
  if [ -z "$URL" ]; then
    url="$(printf '%s\n' "$pane" \
           | grep -oE 'https://[^[:space:]]*/login\?[^[:space:]]*' | head -1 || true)"
    if [ -n "$url" ]; then
      URL="$url"
      printf '============================================================\n'
      printf '  请用手机或电脑浏览器打开下面这个链接并完成登录（手机号/微信）：\n\n'
      printf '  %s\n\n' "$URL"
      printf '  · 一次性链接，请尽快打开；过期了就重跑本脚本。\n'
      printf '  · 无需 SSH 隧道：这是服务端轮询模式，任何设备打开都行。\n'
      printf '============================================================\n'
      if [ "$MODE" != "wait" ]; then
        printf '登录完成后，回来执行自检：\n  %s/run.sh %s doctor\n' "$DIR" "$ACCT"
        exit 0
      fi
      printf '\n已进入等待模式（最多 10 分钟），登录成功会自动提示…\n'
      limit=$(( tries + 300 ))     # 再等 300 × 2s = 10 分钟
    fi
  fi

  # 5) 判断登录是否完成：登录链接消失且不再提示未认证
  if [ -n "$URL" ]; then
    case "$pane" in
      *"Waiting for auth"*) : ;;
      *"Authentication required"*) : ;;
      *"$URL"*) : ;;
      *"Select login method"*) : ;;
      *)
        printf '\n[√] 登录流程已结束（界面已切换）。\n'
        f="$(find_cred || true)"
        if [ -n "$f" ]; then
          chmod 600 "$f" 2>/dev/null || true
          printf '已写入凭据文件：\n  %s\n' "$f"
          printf '\n接着自检（应显示令牌与来源）：\n  %s/run.sh %s doctor\n' "$DIR" "$ACCT"
          exit 0
        fi
        printf '未在账号 HOME 发现凭据文件，请用 --show 查看界面确认。\n'
        exit 1
        ;;
    esac
  fi
done

printf '\n超时：未捕获到登录链接。\n' >&2
printf '当前界面：\n' >&2
tmux capture-pane -p -t "$SESS" 2>/dev/null | sed 's/^/    /' >&2 || true
printf '\n请用 --show 查看当前界面，或确认 CLI 版本：%s --version\n' "$CB" >&2
exit 1
