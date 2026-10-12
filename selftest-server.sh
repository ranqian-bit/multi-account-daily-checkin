#!/bin/sh
# =============================================================================
# 服务器侧自测 —— 专测「shell 包装层」（run.sh）
#
#   Python 自测（selftest.py）覆盖不到这一层：它直接用进程环境变量传参，
#   而线上是 run.sh 先 source 账号 env 再 exec Python。若 run.sh 少了 set -a，
#   变量只存在于 shell 内、不会传给子进程，现象是「文件里明明有令牌却一直 NO_AUTH」。
#   本脚本就是为这个坑设的回归测试。
#
#   用法（在服务器上，root）：
#     sh selftest-server.sh [安装目录]        默认 /opt/workbuddy-signin
# =============================================================================
set -eu

DIR="${1:-/opt/workbuddy-signin}"
RUN="$DIR/run.sh"

if [ ! -x "$RUN" ]; then
    echo "[x] 找不到可执行的 $RUN（先跑 install.sh）" >&2
    exit 1
fi

TMP="$(mktemp -d /tmp/wbsignin-selftest.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/accounts" "$TMP/home-probe"
chmod 700 "$TMP/accounts"

cat > "$TMP/accounts/probe.env" <<EOF
WORKBUDDY_ACCOUNT=probe
WORKBUDDY_ACCESS_TOKEN=PROBE.TOKEN.FOR.SHELL.TEST.1234567890
WORKBUDDY_UID=999
WORKBUDDY_BUDGET_SECONDS=7
WORKBUDDY_HOME=$TMP/home-probe
WORKBUDDY_LOCK_FILE=$TMP/probe.lock
WORKBUDDY_SIGNIN_LOG=$TMP/probe.log
EOF
chmod 600 "$TMP/accounts/probe.env"

# 模拟 CLI 登录后的落盘：<HOME>/.local/share/CodeBuddyExtension/Data/Public/auth/*.info
mkdir -p "$TMP/home-probe/.local/share/CodeBuddyExtension/Data/Public/auth"
cat > "$TMP/home-probe/.local/share/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info" <<EOF
{"auth":{"accessToken":"HOME.CREDENTIAL.FOR.SHELL.TEST.0987654321"},"account":{"uid":"777"}}
EOF
chmod 600 "$TMP/home-probe/.local/share/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info"

# 只配 HOME、不配令牌的账号（用于验证「独立 HOME 自动读凭据」）
cat > "$TMP/accounts/homeonly.env" <<EOF
WORKBUDDY_ACCOUNT=homeonly
WORKBUDDY_HOME=$TMP/home-probe
WORKBUDDY_LOCK_FILE=$TMP/homeonly.lock
WORKBUDDY_SIGNIN_LOG=$TMP/homeonly.log
EOF
chmod 600 "$TMP/accounts/homeonly.env"

# 空 HOME 的账号（用于验证「账号模式不回落到全局凭据」）
mkdir -p "$TMP/home-empty"
cat > "$TMP/accounts/emptyhome.env" <<EOF
WORKBUDDY_ACCOUNT=emptyhome
WORKBUDDY_HOME=$TMP/home-empty
WORKBUDDY_LOCK_FILE=$TMP/emptyhome.lock
WORKBUDDY_SIGNIN_LOG=$TMP/emptyhome.log
EOF
chmod 600 "$TMP/accounts/emptyhome.env"

pass=0
fail=0
check() {  # $1=描述  $2=条件结果(0/1)
    if [ "$2" -eq 0 ]; then
        printf '  [OK]   %s\n' "$1"; pass=$((pass + 1))
    else
        printf '  [FAIL] %s\n' "$1"; fail=$((fail + 1))
    fi
}

echo "============================================================"
echo "1) run.sh 参数校验"
echo "============================================================"
rc=0; "$RUN" >/dev/null 2>&1 || rc=$?
check "无参数应退出码 64（用法错误）" "$([ "$rc" -eq 64 ] && echo 0 || echo 1)"
rc=0; "$RUN" no-such-account doctor >/dev/null 2>&1 || rc=$?
check "账号不存在应退出码 66（配置缺失）" "$([ "$rc" -eq 66 ] && echo 0 || echo 1)"

echo
echo "============================================================"
echo "2) 账号 env 是否正确传递给 Python（set -a / allexport）"
echo "============================================================"
out="$(WORKBUDDY_CRED_DIR="$TMP/accounts" "$RUN" probe doctor --json 2>&1 || true)"
printf '%s\n' "$out" | sed 's/^/    /'
case "$out" in
    *'"budget_seconds":7'*) check "env 里的 WORKBUDDY_BUDGET_SECONDS 已传入子进程" 0 ;;
    *)                      check "env 里的 WORKBUDDY_BUDGET_SECONDS 已传入子进程" 1 ;;
esac
case "$out" in
    *'"has_uid":true'*) check "env 里的 WORKBUDDY_UID 已传入子进程" 0 ;;
    *)                  check "env 里的 WORKBUDDY_UID 已传入子进程" 1 ;;
esac
case "$out" in
    *"$TMP/home-probe"*) check "env 里的 WORKBUDDY_HOME 已传入子进程（多账号隔离）" 0 ;;
    *)                   check "env 里的 WORKBUDDY_HOME 已传入子进程（多账号隔离）" 1 ;;
esac
case "$out" in
    *'"account":"probe"'*) check "账号名已随 env 传入" 0 ;;
    *)                     check "账号名已随 env 传入" 1 ;;
esac

echo
echo "============================================================"
echo "3) 反向验证：去掉 set -a 后变量确实丢失（证明该行不可省）"
echo "============================================================"
sed 's/^set -a$//' "$RUN" > "$TMP/run-noallexport.sh"
chmod 755 "$TMP/run-noallexport.sh"
out2="$(WORKBUDDY_CRED_DIR="$TMP/accounts" sh "$TMP/run-noallexport.sh" probe doctor --json 2>&1 || true)"
if printf '%s' "$out2" | grep -q '"budget_seconds":7'; then
    check "去掉 set -a 后仍能读到 env（与预期不符，请复核）" 1
else
    check "去掉 set -a 后 env 丢失（符合预期，说明 set -a 是必需的）" 0
fi

echo
echo "============================================================"
echo "4) 多账号锁隔离"
echo "============================================================"
echo "   （注：doctor 是离线自检，设计上不取文件锁；这里用 status 来验证锁行为）"
mkdir -p "$TMP/accounts"
cat > "$TMP/accounts/other.env" <<EOF
WORKBUDDY_ACCOUNT=other
WORKBUDDY_ACCESS_TOKEN=OTHER.TOKEN.FOR.SHELL.TEST.1234567890
WORKBUDDY_UID=888
WORKBUDDY_LOCK_FILE=$TMP/other.lock
WORKBUDDY_SIGNIN_LOG=$TMP/other.log
WORKBUDDY_API_BASE=http://evil.example.com
EOF
chmod 600 "$TMP/accounts/other.env"
# 手工占住 probe 的锁，probe 应被判"正在运行"，而 other 不受影响
: > "$TMP/probe.lock"
out3="$(WORKBUDDY_CRED_DIR="$TMP/accounts" "$RUN" probe status 2>&1 || true)"
case "$out3" in
    *ALREADY_RUNNING*) check "probe 的锁被占用 -> 正确判为运行中（幂等跳过）" 0 ;;
    *)                 check "probe 的锁被占用 -> 应判为运行中（实际输出：$out3）" 1 ;;
esac
# other 用的是另一把锁：应当顺利取得锁，随后才因端点非白名单而退出码 5
out4="$(WORKBUDDY_CRED_DIR="$TMP/accounts" "$RUN" other status 2>&1 || true)"
case "$out4" in
    *ALREADY_RUNNING*) check "other 账号不应受 probe 的锁影响（当前被误判为运行中）" 1 ;;
    *UNSAFE_ENDPOINT*) check "other 账号不受 probe 的锁影响（已成功取锁并走到端点检查）" 0 ;;
    *)                 check "other 账号应成功取锁（实际输出：$out4）" 1 ;;
esac

echo
echo "============================================================"
echo "4b) 独立 HOME 凭据读取 + 账号隔离（不串号）"
echo "============================================================"
out5="$(WORKBUDDY_CRED_DIR="$TMP/accounts" "$RUN" homeonly doctor --json 2>&1 || true)"
case "$out5" in
    *'"result":"READY"'*) check "只配 HOME 的账号能从独立 HOME 读到凭据" 0 ;;
    *)                    check "只配 HOME 的账号应能读到凭据（实际：$(printf '%s' "$out5" | cut -c1-90)）" 1 ;;
esac
case "$out5" in
    *"$TMP/home-probe/.local/share"*) check "凭据来源确认为该账号 HOME 下的 CLI 落点" 0 ;;
    *)                                check "凭据来源应为该账号 HOME 下的 CLI 落点" 1 ;;
esac

out6="$(WORKBUDDY_CRED_DIR="$TMP/accounts" "$RUN" emptyhome doctor --json 2>&1 || true)"
case "$out6" in
    *'"result":"NO_AUTH"'*) check "空 HOME 的账号 -> NO_AUTH（未静默回落到全局凭据）" 0 ;;
    *)                      check "空 HOME 的账号应判 NO_AUTH（实际：$(printf '%s' "$out6" | cut -c1-90)）" 1 ;;
esac

echo
echo "============================================================"
echo "5) 定时器（若已装 systemd 单元）"
echo "============================================================"
if command -v systemctl >/dev/null 2>&1; then
    timers="$(systemctl list-timers 'workbuddy-signin@*' --no-pager --no-legend 2>/dev/null || true)"
    if [ -n "$timers" ]; then
        printf '%s\n' "$timers" | sed 's/^/    /'
        n="$(printf '%s\n' "$timers" | grep -c 'workbuddy-signin@' || true)"
        check "已启用 $n 个账号定时器" "$([ "$n" -ge 1 ] && echo 0 || echo 1)"
    else
        printf '    （未发现 workbuddy-signin@*.timer，可能尚未添加账号）\n'
    fi
fi

echo
echo "============================================================"
printf '通过 %d 项，失败 %d 项\n' "$pass" "$fail"
echo "============================================================"
[ "$fail" -eq 0 ] || exit 1
