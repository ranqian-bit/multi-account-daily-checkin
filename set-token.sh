#!/bin/sh
# =============================================================================
# 安全注入 / 更新某个账号的 WorkBuddy 访问令牌（明文令牌路线）
#
#   交互式：  ./set-token.sh <账号名>              # 粘贴时不回显、不进 shell 历史
#   管道式：  cat token.txt | ./set-token.sh <账号名>
#
#   只改该账号 env 文件里的两行，其余内容（含注释）原样保留。
#   写入后会自动经 run.sh 跑一次自检，即时暴露"文件里有令牌但读不到"这类问题。
#
#   注意：这条路线拿到的令牌约 28 天过期，且不会自动续期；
#         想一劳永逸请改用 ./login-cli.sh <账号名>（CodeBuddy CLI 登录）。
# =============================================================================
set -eu

ACCT="${1:-}"
[ -n "$ACCT" ] || { echo "用法: set-token.sh <账号名>   （账号名见 install.sh --list）" >&2; exit 64; }

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# ---- 定位该账号的凭据文件（与 install.sh 的 CRED_DIR 规则一致）
if [ -n "${WORKBUDDY_CRED_FILE:-}" ]; then
    CRED_FILE="$WORKBUDDY_CRED_FILE"
else
    CRED_FILE=""
    for c in "${WORKBUDDY_CRED_DIR:-}" /etc/workbuddy-signin/accounts "$DIR/accounts"; do
        [ -n "$c" ] || continue
        if [ -f "$c/$ACCT.env" ]; then CRED_FILE="$c/$ACCT.env"; break; fi
    done
fi
[ -n "$CRED_FILE" ] || {
    echo "找不到账号配置 $ACCT（先执行 install.sh --add-account $ACCT）" >&2
    exit 66
}

read_secret() {
    if [ -t 0 ]; then
        printf '%s' "$1"
        stty -echo 2>/dev/null || true
        IFS= read -r _val || _val=""
        stty echo 2>/dev/null || true
        printf '\n'
    else
        IFS= read -r _val || _val=""
    fi
    printf '%s' "$_val"
}

TOKEN="$(read_secret "粘贴 $ACCT 的 accessToken（输入不回显），回车确认：")"
TOKEN="$(printf '%s' "$TOKEN" | tr -d ' \t\r\n')"

if [ -z "$TOKEN" ]; then
    echo "令牌为空，已取消。" >&2
    exit 1
fi
case "$TOKEN" in
    *'$wbEncrypted'*|*envelope*)
        echo "这看起来是桌面端的加密信封，不是明文令牌。服务器无法解密，请改用明文 accessToken。" >&2
        exit 1 ;;
esac
if [ "${#TOKEN}" -lt 20 ]; then
    echo "令牌长度只有 ${#TOKEN} 位，疑似不完整，已取消。" >&2
    exit 1
fi

printf 'UID（account.uid，直接回车则保留原值）：'
IFS= read -r NEW_UID || NEW_UID=""
NEW_UID="$(printf '%s' "$NEW_UID" | tr -d ' \t\r\n')"

umask 077
TMP="$(mktemp "${CRED_FILE}.XXXXXX")"
trap 'rm -f "$TMP" "$TMP.2" 2>/dev/null || true' EXIT

if grep -q '^WORKBUDDY_ACCESS_TOKEN=' "$CRED_FILE"; then
    awk -v t="$TOKEN" '/^WORKBUDDY_ACCESS_TOKEN=/ { print "WORKBUDDY_ACCESS_TOKEN=" t; next } { print }' \
        "$CRED_FILE" > "$TMP"
else
    { cat "$CRED_FILE"; printf 'WORKBUDDY_ACCESS_TOKEN=%s\n' "$TOKEN"; } > "$TMP"
fi

if [ -n "$NEW_UID" ]; then
    if grep -q '^WORKBUDDY_UID=' "$TMP"; then
        awk -v u="$NEW_UID" '/^WORKBUDDY_UID=/ { print "WORKBUDDY_UID=" u; next } { print }' "$TMP" > "$TMP.2"
        mv "$TMP.2" "$TMP"
    else
        printf 'WORKBUDDY_UID=%s\n' "$NEW_UID" >> "$TMP"
    fi
fi

chmod 600 "$TMP"
mv "$TMP" "$CRED_FILE"
trap - EXIT

echo "已写入 $CRED_FILE（权限 600，令牌未回显）"
echo

# 自检必须走 run.sh：它会 source 账号 env 并导出变量。
# 直接调 signin_linux.py 的话令牌不在环境里，必然误报 NO_AUTH。
if [ -x "$DIR/run.sh" ]; then
    echo "--- 自检（经 run.sh 加载凭据）---"
    "$DIR/run.sh" "$ACCT" doctor || true
else
    echo "（未找到 $DIR/run.sh，跳过自检）"
fi
