#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 令牌自动续期（Linux 服务器版，纯标准库）。

为什么需要它
------------
扫码登录拿到的凭据有两条寿命：accessToken 28 天、refreshToken 30 天。
服务器上没有常驻客户端，所以不会自己续期 —— 到点就得重新扫码。

它是怎么做到的
--------------
CodeBuddy CLI 内置一个 refreshTokenTimer：启动时若发现 accessToken 即将过期，
它会带着 X-Refresh-Token 去服务端换回**全新的一对**令牌
（accessToken 重新计 28 天、refreshToken 重新计 30 天）。实测确认这是滚动续期。

CLI 调的接口是 `POST /v2<prefix>/auth/token/refresh`，prefix 在构建期注入、
二进制里没有明文，所以本脚本**不去猜路径**，而是复用 CLI 本身完成刷新：

  1. 把凭据里的 expiresAt 临时改到 1 分钟后（模拟「快过期了」），lastRefreshTime 归零
     —— 这一步只影响本地文件，服务端并不知道；
  2. 在该账号的独立 HOME 里用 tmux 拉起 CLI，等它自己把新令牌写回凭据文件；
  3. 校验新令牌确实变了、有效期确实往后挪了，就把临时改动还原；
     任何一步失败都整体回滚到备份，绝不让凭据文件处于半坏状态。

于是只要在 refreshToken 过期前跑一次本脚本，就能一直滚下去，永远不用重新扫码。

用法
----
    python3 refresh_token.py                 # 按阈值判断，需要时才刷新
    python3 refresh_token.py --force         # 无条件刷新一次（验证用）
    python3 refresh_token.py --check         # 只看还剩多少天，不动任何文件
    python3 refresh_token.py --json          # 输出单行 JSON，便于监控采集

退出码：0=成功或无需刷新；2=凭据不可用；3=刷新失败（已回滚）；4=环境不支持（缺 CLI/tmux）
"""

import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

AUTH_REL = (".local/share/CodeBuddyExtension/Data/Public/auth/"
            "Tencent-Cloud.coding-copilot.info")

DEFAULT_THRESHOLD_DAYS = 20.0     # refreshToken 剩余不足这个天数就续期（30 天寿命 → 约每 10 天一次）
DEFAULT_WAIT_SECONDS = 150        # 等 CLI 写完新令牌的最长时间
POLL_SECONDS = 3
SETTLE_SECONDS = 3                # 启动后先等 CLI 起来

EXIT_OK = 0
EXIT_NO_AUTH = 2
EXIT_FAILED = 3
EXIT_UNSUPPORTED = 4


# ---------------------------------------------------------------- 小工具

def now_ms() -> int:
    return int(time.time() * 1000)


def fp(text: Optional[str]) -> str:
    """令牌指纹，任何输出都不打印令牌本体。"""
    if not text or not isinstance(text, str):
        return "(无)"
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def fmt_ms(value: Any) -> str:
    try:
        return datetime.datetime.fromtimestamp(int(value) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "(未知)"


def remaining_days(value: Any) -> Optional[float]:
    try:
        return (int(value) - now_ms()) / 86400000.0
    except (TypeError, ValueError):
        return None


def account_home() -> Path:
    home = (os.environ.get("WORKBUDDY_HOME")
            or os.environ.get("CODEBUDDY_HOME")
            or "").strip()
    if home:
        return Path(home).expanduser()
    acct = (os.environ.get("WORKBUDDY_ACCOUNT") or "").strip()
    if acct:
        return Path.home() / (".wb-acct-%s" % acct)
    return Path.home()


def auth_path() -> Path:
    return account_home() / AUTH_REL


# ---------------------------------------------------------------- 日志

class Log:
    def __init__(self, as_json: bool, log_path: Optional[Path]):
        self.as_json = as_json
        self.log_path = log_path
        self.payload: Dict[str, Any] = {}

    def emit(self, payload: Dict[str, Any]) -> None:
        self.payload = payload
        if self.log_path is not None:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                record = dict(payload)
                record["ts"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False,
                                        separators=(",", ":")) + "\n")
            except OSError:
                pass
        if self.as_json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(payload.get("report") or json.dumps(payload, ensure_ascii=False))


# ---------------------------------------------------------------- 凭据读写

def load_auth() -> Tuple[Path, Dict[str, Any], Dict[str, Any]]:
    path = auth_path()
    if not path.exists():
        raise FileNotFoundError(str(path))
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    auth = doc.get("auth") or {}
    if not isinstance(auth, dict):
        raise ValueError("凭据文件里没有 auth 段")
    return path, doc, auth


def save_doc(path: Path, doc: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 外部依赖

def find_cli() -> Optional[Path]:
    explicit = (os.environ.get("WORKBUDDY_CLI_BIN") or "").strip()
    candidates: List[str] = [explicit] if explicit else []
    candidates += ["~/.local/bin/codebuddy", "/usr/local/bin/codebuddy"]
    for cand in candidates:
        if not cand:
            continue
        p = Path(cand).expanduser()
        if p.exists() and os.access(str(p), os.X_OK):
            return p
    which = shutil.which("codebuddy")
    return Path(which) if which else None


def have_tmux() -> bool:
    return shutil.which("tmux") is not None


def tmux_run(args: List[str]) -> int:
    try:
        proc = subprocess.run(["tmux"] + args, capture_output=True, text=True, timeout=30)
        return proc.returncode
    except (OSError, subprocess.SubprocessError):
        return -1


def tmux_pane(session: str) -> str:
    try:
        proc = subprocess.run(["tmux", "capture-pane", "-p", "-t", session, "-S", "-40"],
                              capture_output=True, text=True, timeout=20)
        return proc.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


# ---------------------------------------------------------------- 主流程

def do_check(as_json: bool, log: Log) -> int:
    try:
        path, _doc, auth = load_auth()
    except (OSError, ValueError) as exc:
        log.emit({"result": "NO_AUTH", "ok": False,
                  "report": "读不到凭据：%s。请先登录（login-cli.sh）。" % exc})
        return EXIT_NO_AUTH

    at_left = remaining_days(auth.get("expiresAt"))
    rt_left = remaining_days(auth.get("refreshExpiresAt"))
    report = ("凭据 %s\n  accessToken  剩余 %s 天（到期 %s）\n  refreshToken 剩余 %s 天（到期 %s）"
              % (path,
                 "%.1f" % at_left if at_left is not None else "?",
                 fmt_ms(auth.get("expiresAt")),
                 "%.1f" % rt_left if rt_left is not None else "?",
                 fmt_ms(auth.get("refreshExpiresAt"))))
    log.emit({"result": "CHECK", "ok": True,
              "access_token_days": at_left, "refresh_token_days": rt_left,
              "expires_at": fmt_ms(auth.get("expiresAt")),
              "refresh_expires_at": fmt_ms(auth.get("refreshExpiresAt")),
              "access_fp": fp(auth.get("accessToken")),
              "refresh_fp": fp(auth.get("refreshToken")),
              "report": report})
    return EXIT_OK


def do_refresh(as_json: bool, log: Log, force: bool, threshold: float,
               wait_seconds: int) -> int:
    acct = (os.environ.get("WORKBUDDY_ACCOUNT") or "").strip() or "default"
    try:
        path, doc, auth = load_auth()
    except (OSError, ValueError) as exc:
        log.emit({"result": "NO_AUTH", "ok": False,
                  "report": "读不到凭据：%s。请先登录（login-cli.sh %s）。" % (exc, acct)})
        return EXIT_NO_AUTH

    old_at = auth.get("accessToken") or ""
    old_rt = auth.get("refreshToken") or ""
    rt_left = remaining_days(auth.get("refreshExpiresAt"))
    at_left = remaining_days(auth.get("expiresAt"))

    if not old_rt:
        log.emit({"result": "UNSUPPORTED", "ok": True,
                  "report": "凭据里没有 refreshToken（可能走的是明文令牌方式），跳过自动续期。"})
        return EXIT_UNSUPPORTED

    if not force and rt_left is not None and rt_left > threshold:
        log.emit({"result": "SKIPPED", "ok": True,
                  "refresh_token_days": rt_left,
                  "report": ("无需续期：refreshToken 还剩 %.1f 天（阈值 %.0f 天，约每 %.0f 天续一次）。"
                             % (rt_left, threshold, max(1.0, 30 - threshold)))})
        return EXIT_OK

    cli = find_cli()
    if cli is None:
        log.emit({"result": "UNSUPPORTED", "ok": True,
                  "report": "找不到 codebuddy 可执行文件，无法自动续期；"
                            "请安装 CLI 或用 set-token.sh 重新注入令牌。"})
        return EXIT_UNSUPPORTED
    if not have_tmux():
        log.emit({"result": "UNSUPPORTED", "ok": True,
                  "report": "缺少 tmux，无法无人值守地驱动 CLI 刷新。"
                            "安装：dnf install -y tmux / apt install -y tmux"})
        return EXIT_UNSUPPORTED

    session = "cb-refresh-%s-%d" % (acct, os.getpid())
    backup = path.with_suffix(path.suffix + ".pre-refresh")
    tmux_run(["kill-session", "-t", session])
    shutil.copy2(path, backup)

    def rollback(reason: str) -> None:
        try:
            shutil.copy2(backup, path)
            os.chmod(path, 0o600)
        except OSError:
            pass

    # 把过期时间提前到 1 分钟后，并清掉刷新冷却时间，逼 CLI 立刻刷新
    auth["expiresAt"] = now_ms() + 60_000
    auth["lastRefreshTime"] = 0
    save_doc(path, doc)

    ok = False
    new_auth: Dict[str, Any] = {}
    note = ""
    try:
        rc = tmux_run(["new-session", "-d", "-s", session, "-x", "200", "-y", "50",
                       "HOME='%s' exec '%s'" % (account_home(), cli)])
        if rc != 0:
            note = "无法启动 tmux 会话（rc=%d）" % rc
        else:
            time.sleep(SETTLE_SECONDS)
            deadline = time.time() + wait_seconds
            n_enter = 0
            while time.time() < deadline:
                pane = tmux_pane(session)
                if n_enter < 8 and ("trust" in pane.lower() or "Do you trust" in pane):
                    tmux_run(["send-keys", "-t", session, "Enter"])
                    n_enter += 1
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        cur = (json.load(fh) or {}).get("auth") or {}
                except (OSError, ValueError):
                    cur = {}
                cur_at = cur.get("accessToken") or ""
                if cur_at and cur_at != old_at:
                    # 指纹变了，还要确认有效期真的往后挪了 ——
                    # 我们伪造的是「1 分钟后过期」，成功刷新后应该重新计 28 天。
                    left = remaining_days(cur.get("expiresAt"))
                    if left is not None and left > 1.0:
                        new_auth = cur
                        ok = True
                        break
                # 会话都没了说明 CLI 崩了或退出了，没必要再等
                if tmux_run(["has-session", "-t", session]) != 0:
                    note = "CLI 会话提前退出（可能启动失败或需要交互）"
                    break
                time.sleep(POLL_SECONDS)
            if not ok:
                note = "等了 %d 秒仍未见新令牌" % wait_seconds
    finally:
        tmux_run(["kill-session", "-t", session])

    if not ok:
        rollback(note)
        try:
            os.unlink(backup)
        except OSError:
            pass
        log.emit({"result": "REFRESH_FAILED", "ok": False, "account": acct, "report":
                  "自动续期失败（%s），已回滚到原凭据；原令牌仍可用。"
                  "若到期临近，请重新扫码登录：login-cli.sh %s" % (note, acct)})
        return EXIT_FAILED

    new_at = new_auth.get("accessToken") or ""
    new_rt = new_auth.get("refreshToken") or old_rt
    new_at_left = remaining_days(new_auth.get("expiresAt"))
    new_rt_left = remaining_days(new_auth.get("refreshExpiresAt"))
    try:
        os.unlink(backup)
    except OSError:
        pass

    log.emit({
        "result": "REFRESHED", "ok": True, "account": acct,
        "old_access_fp": fp(old_at), "new_access_fp": fp(new_at),
        "old_refresh_fp": fp(old_rt), "new_refresh_fp": fp(new_rt),
        "refresh_token_rotated": fp(new_rt) != fp(old_rt),
        "access_token_days": new_at_left, "refresh_token_days": new_rt_left,
        "expires_at": fmt_ms(new_auth.get("expiresAt")),
        "refresh_expires_at": fmt_ms(new_auth.get("refreshExpiresAt")),
        "report": ("续期成功：accessToken 重新计 %s 天（至 %s），refreshToken 重新计 %s 天（至 %s）%s"
                   % ("%.0f" % new_at_left if new_at_left is not None else "?",
                      fmt_ms(new_auth.get("expiresAt")),
                      "%.0f" % new_rt_left if new_rt_left is not None else "?",
                      fmt_ms(new_auth.get("refreshExpiresAt")),
                      "，refreshToken 已轮转" if fp(new_rt) != fp(old_rt) else "")),
    })
    return EXIT_OK


# ---------------------------------------------------------------- 入口

def _log_path() -> Optional[Path]:
    raw = (os.environ.get("WORKBUDDY_REFRESH_LOG") or "").strip()
    if raw.lower() in ("0", "off", "none"):
        return None
    if raw:
        return Path(raw).expanduser()
    signin = (os.environ.get("WORKBUDDY_SIGNIN_LOG") or "").strip()
    if signin:
        p = Path(signin).expanduser()
        return p.with_name(p.stem + ".refresh" + (p.suffix or ".log"))
    return Path.home() / ".workbuddy" / "refresh.log"


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="refresh_token.py",
        description="WorkBuddy 令牌自动续期：让 CLI 用 refreshToken 滚动换新，免重新扫码")
    parser.add_argument("--force", action="store_true", help="无视阈值，强制续期一次")
    parser.add_argument("--check", action="store_true", help="只报告剩余天数，不改动任何文件")
    parser.add_argument("--json", action="store_true", help="输出单行 JSON")
    parser.add_argument("--threshold-days", type=float,
                        default=float(os.environ.get("WORKBUDDY_REFRESH_THRESHOLD_DAYS")
                                      or DEFAULT_THRESHOLD_DAYS),
                        help="refreshToken 剩余不足该天数就续期（默认 %g）" % DEFAULT_THRESHOLD_DAYS)
    parser.add_argument("--wait-seconds", type=int,
                        default=int(os.environ.get("WORKBUDDY_REFRESH_WAIT_SECONDS")
                                    or DEFAULT_WAIT_SECONDS),
                        help="等待 CLI 写完新令牌的最长秒数（默认 %d）" % DEFAULT_WAIT_SECONDS)
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    log = Log(args.json, _log_path())
    if args.check:
        return do_check(args.json, log)
    return do_refresh(args.json, log, args.force, args.threshold_days, args.wait_seconds)


if __name__ == "__main__":
    sys.exit(main())
