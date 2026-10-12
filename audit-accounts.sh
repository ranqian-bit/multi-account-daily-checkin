#!/bin/sh
# WorkBuddy 多账号体检 —— 一次性核对所有账号是否真的互不串号。
#
#   ./audit-accounts.sh          # 表格化输出
#   ./audit-accounts.sh --json   # 机器可读
#
# 为什么需要它：多账号最容易踩的坑是「两个账号其实登成了同一个人」。
# 这种情况下两边的凭据都能用、签到也都成功，只有账号身份（昵称 + uid）不一样才看得出来。
#
# 退出码：0=全部正常；1=发现有账号其实是同一个人（串号）；2=有账号凭据不可用。
set -eu

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CRED_DIR="${WORKBUDDY_CRED_DIR:-/etc/workbuddy-signin/accounts}"
PY="${WORKBUDDY_PYTHON:-/usr/bin/python3.12}"
if [ ! -x "$PY" ]; then
  for c in /usr/bin/python3 /usr/local/bin/python3 python3; do
    if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
  done
fi

AS_JSON=0
[ "${1:-}" = "--json" ] && AS_JSON=1

[ -d "$CRED_DIR" ] || { echo "找不到账号目录：$CRED_DIR" >&2; exit 2; }
[ -x "$DIR/run.sh" ] || { echo "找不到 $DIR/run.sh" >&2; exit 2; }

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT INT TERM

found=0
for f in "$CRED_DIR"/*.env; do
  [ -f "$f" ] || continue
  found=1
  acct=$(basename "$f" .env)
  out=$("$DIR/run.sh" "$acct" doctor --json 2>&1) || true
  printf '%s\t%s\n' "$acct" "$(printf '%s' "$out" | tr -d '\r\n')" >> "$TMP"
done

[ "$found" -eq 1 ] || { echo "账号目录里没有 *.env：$CRED_DIR" >&2; exit 2; }

"$PY" - "$TMP" "$AS_JSON" <<'PYEOF'
import json
import sys
import unicodedata

rows = []
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    line = line.rstrip("\r\n")
    if not line:
        continue
    acct, _, raw = line.partition("\t")
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except Exception:
        data = {"result": "UNPARSABLE", "report": raw[:180], "ok": False}
    rows.append((acct, data))


def width(text):
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text, size):
    return text + " " * max(0, size - width(text))


by_identity = {}
for acct, data in rows:
    by_identity.setdefault(data.get("identity") or "(未知)", []).append(acct)

dupes = {k: v for k, v in by_identity.items() if len(v) > 1}
bad = [a for a, d in rows if d.get("result") != "READY"]

if sys.argv[2] == "1":
    print(json.dumps({
        "accounts": [{"account": a, "identity": d.get("identity"),
                      "result": d.get("result"), "expiry": d.get("expiry"),
                      "source": d.get("credential_source")} for a, d in rows],
        "duplicates": dupes,
        "unavailable": bad,
        "ok": not dupes and not bad,
    }, ensure_ascii=False))
else:
    print("WorkBuddy 账号体检（共 %d 个）" % len(rows))
    print(pad("账号", 14) + pad("账号身份", 36) + pad("状态", 9) + "凭据有效期")
    print("-" * 92)
    for acct, data in rows:
        print(pad(acct, 14) + pad(str(data.get("identity") or "(未知)"), 36)
              + pad(str(data.get("result") or "-"), 9) + str(data.get("expiry") or "-"))
    print()
    for identity, accts in dupes.items():
        print("串号：%s 其实是同一个账号 -> %s" % (identity, "、".join(accts)))
    if dupes:
        print("     这两个账号会领同一份积分，请用另一个账号重新登录后再看。")
    if bad:
        print("不可用：%s" % "、".join(bad))
    if not dupes and not bad:
        print("全部正常：各账号身份唯一、凭据均可用。")

sys.exit(1 if dupes else (2 if bad else 0))
PYEOF
