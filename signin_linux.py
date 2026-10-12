#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 每日签到自动领积分 —— Linux 服务器版（多账号）
=======================================================

纯标准库实现（Python 3.7+，无需 pip 安装任何依赖），面向无图形界面的
Linux 服务器 / 容器 / 云主机运行。

与上游「读本机桌面端登录态」的方案不同，本脚本支持三种凭据来源，
优先使用环境变量，便于在服务器上以 systemd / cron 静默运行：

  1. 环境变量 WORKBUDDY_ACCESS_TOKEN（可选 WORKBUDDY_UID /
     WORKBUDDY_ENTERPRISE_ID / WORKBUDDY_DOMAIN）
  2. JSON 凭据文件（WORKBUDDY_AUTH_FILE 指定，或自动探测）
  3. 本机 CodeBuddy CLI 登录后写出的凭据文件（自动探测，零配置）

【多账号】每个账号用独立的环境变量组隔离，互不干扰：
  WORKBUDDY_ACCOUNT       账号名（只用于日志/自检展示）
  WORKBUDDY_HOME          该账号专属 HOME（多账号隔离的**关键**，见下）
  WORKBUDDY_CONFIG_DIR    该账号的 CLI 配置目录；也兼容读取 CODEBUDDY_CONFIG_DIR
  WORKBUDDY_LOCK_FILE     该账号专属锁文件（多账号必须各不相同，否则互相阻塞）
  WORKBUDDY_SIGNIN_LOG    该账号专属日志文件
配合 install.sh 生成的 `run.sh <账号名>` 与 systemd 模板单元
`workbuddy-signin@<账号名>.timer` 使用。

⚠️ 多账号隔离为什么必须用独立 HOME 而不是 CODEBUDDY_CONFIG_DIR：
   CodeBuddy CLI 把凭据写在 `<HOME>/.local/share/CodeBuddyExtension/Data/Public/auth/`
   （2.164.0 实测；该路径写死在 HOME 下，**不读 XDG_DATA_HOME**），
   而 CODEBUDDY_CONFIG_DIR 只隔离会话/插件数据、**不隔离凭据**。
   所以只要设置了 WORKBUDDY_ACCOUNT，本脚本就**只看该账号自己的落点**，
   绝不回落到全局桌面端凭据 —— 否则两个账号会共用同一份令牌（同一身份重复签到）。

接口来源：从 WorkBuddy 桌面端app.asar 逆向得到的官方计费接口，
与上游项目 github.com/88lin/workbuddy-auto-signin 一致：
  POST {endpoint}/v2/billing/meter/checkin-activity-status   查询签到状态（只读）
  POST {endpoint}/v2/billing/meter/daily-checkin             领取今日积分（幂等）

用法：
  python3 signin_linux.py                # 查询 + 领取（默认，幂等）
  python3 signin_linux.py status         # 仅查询今日状态
  python3 signin_linux.py doctor         # 离线自检：凭据来源 / 运行时能力，不联网
  python3 signin_linux.py claim --json   # 领取并输出单行 JSON（给定时任务/监控用）

退出码：
  0  成功（已领取 / 今日已签）
  2  认证失败（无凭据、令牌失效）
  3  网络失败
  4  接口业务失败
  5  端点不安全（非白名单 HTTPS 域名，已拒绝发送凭据）
  6  配置错误
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------- 常量配置

DEFAULT_ENDPOINT = "https://copilot.tencent.com"
# 只允许把登录凭据发往这些域名，防止被环境变量劫持后泄露令牌
ALLOWED_ENDPOINT_HOSTS = {"copilot.tencent.com"}

STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
CLAIM_PATH = "/v2/billing/meter/daily-checkin"

REQUEST_TIMEOUT = 20
# 仅用于「只读查询」和「幂等领取」的重试退避；写操作超时后不重试
RETRY_DELAYS = (5, 15, 30, 60, 90)
DEFAULT_BUDGET_SECONDS = 180
MAX_BUDGET_SECONDS = 540

USER_AGENT = "WorkBuddy"

# 新版客户端把 accessToken 存成 {"$wbEncrypted": 1, "envelope": "..."} 加密信封，
# 解密要调用客户端自带运行时，服务器上不具备该能力，必须改用明文令牌。
ENVELOPE_HINT = (
    "凭据里的 accessToken 是加密信封（$wbEncrypted），服务器上没有客户端运行时，"
    "无法解密。请改用明文令牌：① 在服务器上安装并登录 CodeBuddy CLI（它写出的是明文凭据，"
    "本脚本会自动探测）；② 或从 Web 控制台/抓包取出明文 Bearer 令牌，"
    "通过 WORKBUDDY_ACCESS_TOKEN 注入。"
)

# 自动探测的凭据文件候选路径（与上游保持一致的目录约定）
AUTH_BASENAME = os.path.join("CodeBuddyExtension", "Data", "Public", "auth",
                             "workbuddy-desktop.info")
CLI_AUTH_BASENAME = os.path.join("CodeBuddyExtension", "Data", "Public", "auth",
                                 "Tencent-Cloud.coding-copilot.info")


class Fail(Exception):
    """带退出码与结果码的失败。"""

    def __init__(self, result: str, message: str, exit_code: int = 1):
        super().__init__(message)
        self.result = result
        self.exit_code = exit_code


# ---------------------------------------------------------------- 输出与日志

def mask_token(token: Optional[str]) -> str:
    """令牌脱敏，任何输出都不打印完整令牌。"""
    if not token:
        return "(空)"
    if len(token) <= 12:
        return "*" * len(token)
    return "%s...%s(len=%d)" % (token[:4], token[-4:], len(token))


def describe_identity(account: Dict[str, Any]) -> str:
    """把凭据里的账号身份拼成一行，用于肉眼核对多账号是否串号。

    多账号场景下最容易踩的坑是「两个账号其实登成了同一个人」——
    此时两边的凭据都能用、签到也都成功，只有 uid 不一样才看得出来。
    只显示昵称 + uid 前 8 位：足以区分账号，又不整体外露。
    """
    parts: List[str] = []
    nickname = account.get("nickname")
    if nickname:
        parts.append(str(nickname))
    uid = str(account.get("uid") or "")
    if uid:
        parts.append("uid %s" % (uid[:8] + "…" if len(uid) > 8 else uid))
    return " / ".join(parts)


def log_line(payload: Dict[str, Any], log_path: Optional[Path]) -> None:
    if log_path is None:
        return
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = dict(payload)
        record["ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError as exc:  # 日志失败不阻断签到
        print("警告：写入日志失败：%s" % exc, file=sys.stderr)


def emit(payload: Dict[str, Any], as_json: bool, log_path: Optional[Path] = None) -> None:
    log_line(payload, log_path)
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return
    report = payload.get("report")
    if report is None:
        print(json.dumps(payload, ensure_ascii=False))
    elif payload.get("ok") is False and payload.get("result"):
        # 失败时带上结果码，方便 grep 日志与监控告警
        print("%s：%s" % (payload["result"], report))
    else:
        print(report)


# ---------------------------------------------------------------- 凭据解析

def flat_credentials() -> Dict[str, Any]:
    """从环境变量组装凭据（优先级最高，适合容器 / systemd）。"""
    token = os.environ.get("WORKBUDDY_ACCESS_TOKEN")
    if not token:
        return {}
    auth: Dict[str, Any] = {"accessToken": token.strip()}
    account: Dict[str, Any] = {}
    if os.environ.get("WORKBUDDY_DOMAIN"):
        auth["domain"] = os.environ["WORKBUDDY_DOMAIN"].strip()
    if os.environ.get("WORKBUDDY_UID"):
        account["uid"] = os.environ["WORKBUDDY_UID"].strip()
    if os.environ.get("WORKBUDDY_ENTERPRISE_ID"):
        account["enterpriseId"] = os.environ["WORKBUDDY_ENTERPRISE_ID"].strip()
    return {"auth": auth, "account": account, "_source": "环境变量 WORKBUDDY_ACCESS_TOKEN"}


def _scan_auth_files(root: Path, max_files: int = 400) -> List[Path]:
    """在配置目录里找可能的凭据文件。

    配置目录会长到几十 MB、上千个文件（plugins / sessions / projects / logs 等），
    所以这里做**目录剪枝**，避免每天的定时任务白扫一遍。
    """
    skip_dirs = {"plugins", "node_modules", "sessions", "projects", "logs",
                 "shell-snapshots", "cache", "Cache", "__pycache__", ".git"}
    found: List[Path] = []
    try:
        for dirpath, dirnames, filenames in os.walk(str(root)):
            dirnames[:] = [d for d in dirnames
                           if d not in skip_dirs and not d.startswith(".")]
            for name in filenames:
                if name.startswith("."):
                    continue
                if not (name.endswith(".info") or name.endswith(".json")):
                    continue
                path = Path(dirpath) / name
                try:
                    if path.stat().st_size > 262144:      # 凭据文件很小，大文件直接跳过
                        continue
                except OSError:
                    continue
                found.append(path)
                if len(found) >= max_files:
                    return sorted(found)
    except OSError:
        pass
    return sorted(found)


def _dedupe(paths: List[Path]) -> List[Path]:
    ordered: List[Path] = []
    seen = set()
    for path in paths:
        key = str(path)
        if key not in seen:
            seen.add(key)
            ordered.append(path)
    return ordered


def _files_in_dir(directory: Path) -> List[Path]:
    out: List[Path] = []
    try:
        if not directory.is_dir():
            return out
        for pattern in ("*.info", "*auth*.json", "*.auth.json"):
            out.extend(sorted(directory.glob(pattern)))
    except OSError:
        pass
    return out


def _home_root_auth_files(root: Path) -> List[Path]:
    """在一个「HOME 根目录」里找 CLI 凭据。

    CodeBuddy CLI 把凭据写到 <HOME>/.local/share/CodeBuddyExtension/Data/Public/auth/
    这个路径**写死在 HOME 下、不走 XDG_DATA_HOME**（实测 2.164.0）。
    所以多账号隔离只能靠给每个账号一个独立 HOME。
    """
    root = Path(root).expanduser()
    xdg = root / ".local" / "share"
    files: List[Path] = []
    files.extend(_files_in_dir(xdg / "CodeBuddyExtension" / "Data" / "Public" / "auth"))
    files.append(xdg / CLI_AUTH_BASENAME)
    files.extend(_scan_auth_files(xdg))
    files.extend(_files_in_dir(root / ".codebuddy"))
    return files


def account_scope_home() -> Optional[Path]:
    raw = (os.environ.get("WORKBUDDY_HOME") or os.environ.get("CODEBUDDY_HOME") or "").strip()
    return Path(raw).expanduser() if raw else None


def candidate_auth_files() -> List[Path]:
    """按当前模式给出凭据文件候选列表。

    【账号隔离模式】设置了 WORKBUDDY_ACCOUNT 时，**只认该账号自己的落点**
    （WORKBUDDY_HOME / WORKBUDDY_CONFIG_DIR），绝不回落到全局桌面端路径。
    原因：实测 CLI 的凭据落在 ~/.local/share/CodeBuddyExtension/...，
    而 CODEBUDDY_CONFIG_DIR 只隔离会话/插件数据、**不隔离凭据**；
    若回落到全局路径，多个账号会共用同一份令牌（同一身份重复签到）。
    """
    files: List[Path] = []

    if os.environ.get("WORKBUDDY_ACCOUNT", "").strip():
        home_root = account_scope_home()
        if home_root is not None:
            files.extend(_home_root_auth_files(home_root))
        config_dir = (os.environ.get("WORKBUDDY_CONFIG_DIR")
                      or os.environ.get("CODEBUDDY_CONFIG_DIR"))
        if config_dir:
            base = Path(config_dir).expanduser()
            files.extend(_files_in_dir(base / "CodeBuddyExtension" / "Data" / "Public" / "auth"))
            files.extend(_files_in_dir(base / "local_storage"))
            files.extend(_scan_auth_files(base))
        # 标出"只看过哪些位置"，方便排错
        return _dedupe(files)

    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
    xdg_data = Path(os.environ.get("XDG_DATA_HOME") or (home / ".local" / "share"))

    files = [
        local / AUTH_BASENAME,                                        # Windows 桌面端
        home / "Library" / "Application Support" / AUTH_BASENAME,      # macOS 桌面端
        xdg_data / CLI_AUTH_BASENAME,                                  # Linux CodeBuddy CLI
        home / ".config" / AUTH_BASENAME,
        home / ".workbuddy" / "auth" / "workbuddy-desktop.info",
    ]

    # CodeBuddy CLI 的落点随版本变化（~/.codebuddy 或 XDG 数据目录），
    # 直接扫目录取里面的 .info / 认证类 json，避免写死文件名导致漏检。
    scan_dirs = [
        xdg_data / "CodeBuddyExtension" / "Data" / "Public" / "auth",
        home / ".codebuddy",
        home / ".config" / "codebuddy",
    ]
    for directory in scan_dirs:
        files.extend(_files_in_dir(directory))

    return _dedupe(files)


def _normalize(raw: Dict[str, Any], source: str) -> Optional[Dict[str, Any]]:
    """兼容两种结构：嵌套 auth/account，以及扁平 accessToken/uid。

    注意：加密信封（accessToken 为字典）也视为「找到了凭据」，
    由 build_headers 给出明确的不可用原因，而不是静默当成「没找到」。
    """
    if not isinstance(raw, dict):
        return None
    auth = raw.get("auth")
    if isinstance(auth, dict) and auth.get("accessToken"):
        result = dict(raw)
        result["_source"] = source
        return result
    token = raw.get("accessToken") or raw.get("access_token")
    if token:
        return {
            "auth": {"accessToken": token, "domain": raw.get("domain"),
                     "expiresAt": raw.get("expiresAt")},
            "account": {"uid": raw.get("uid"), "enterpriseId": raw.get("enterpriseId")},
            "_source": source,
        }
    return None


def token_expiry(auth: Dict[str, Any]) -> Optional[float]:
    """解析 expiresAt（兼容秒 / 毫秒），返回 Unix 秒时间戳。"""
    raw = auth.get("expiresAt")
    if isinstance(raw, str):
        try:
            raw = float(raw)
        except ValueError:
            return None
    if not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    if value > 1e11:          # 毫秒时间戳
        value /= 1000.0
    if value < 1e9:           # 明显不是绝对时间戳（可能是 expiresIn 秒数）
        return None
    return value


def describe_expiry(auth: Dict[str, Any]) -> Tuple[Optional[str], bool]:
    """返回 (人话描述, 是否已过期)。拿不到有效期时返回 (None, False)。"""
    expiry = token_expiry(auth)
    if expiry is None:
        return None, False
    left = expiry - time.time()
    stamp = datetime.fromtimestamp(expiry).strftime("%Y-%m-%d %H:%M")
    if left <= 0:
        return "令牌已于 %s 过期" % stamp, True
    if left < 3 * 86400:
        return "令牌将于 %s 过期（仅剩 %d 小时，请尽快重新注入）" % (stamp, int(left / 3600)), False
    return "令牌有效期至 %s（剩约 %d 天）" % (stamp, int(left / 86400)), False



def is_envelope(creds: Dict[str, Any]) -> bool:
    """判断凭据是否为无法在服务器端解密的加密信封。"""
    token = (creds.get("auth") or {}).get("accessToken")
    if isinstance(token, dict):
        return True
    return isinstance(token, str) and "$wbEncrypted" in token


def load_credentials() -> Tuple[Dict[str, Any], List[str]]:
    """按优先级解析凭据，返回 (凭据, 探测过的位置列表)。

    优先返回可用的明文凭据；若只找到加密信封，则抛出具针对性的错误，
    避免「明明有文件却报没找到」的误导。
    """
    looked: List[str] = []

    env_creds = flat_credentials()
    if env_creds:
        looked.append("环境变量 WORKBUDDY_ACCESS_TOKEN：命中")
        if is_envelope(env_creds):
            raise Fail("UNSUPPORTED_ENVELOPE", ENVELOPE_HINT, 6)
        return env_creds, looked
    looked.append("环境变量 WORKBUDDY_ACCESS_TOKEN：未设置")

    files: List[Path] = []
    override = os.environ.get("WORKBUDDY_AUTH_FILE")
    if override:
        override_path = Path(override).expanduser()
        files.append(override_path)
        if not override_path.exists():
            looked.append("WORKBUDDY_AUTH_FILE=%s：文件不存在" % override_path)
    files.extend(candidate_auth_files())

    seen = set()
    envelope_seen: Optional[str] = None
    for path in files:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        looked.append(str(path))
        try:
            if not path.is_file():
                continue
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            looked[-1] += "：读取失败(%s)" % type(exc).__name__
            continue
        creds = _normalize(raw, "凭据文件 %s" % path)
        if not creds:
            continue
        if is_envelope(creds):
            looked[-1] += "：加密信封，不可用"
            if envelope_seen is None:
                envelope_seen = key
            continue
        return creds, looked

    if envelope_seen:
        raise Fail("UNSUPPORTED_ENVELOPE", ENVELOPE_HINT, 6)

    account_name = os.environ.get("WORKBUDDY_ACCOUNT", "").strip()
    if account_name:
        raise Fail("NO_AUTH",
                   "账号 %s 没有可用凭据。请先登录：%s（或在 .env 里填 "
                   "WORKBUDDY_ACCESS_TOKEN）。注意账号模式下**不会**回落到全局桌面端凭据，"
                   "这是为了避免多个账号共用同一份令牌。"
                   % (account_name, "login-cli.sh " + account_name), 2)
    raise Fail("NO_AUTH",
               "未找到可用的 WorkBuddy 登录凭据。请设置 WORKBUDDY_ACCESS_TOKEN，"
               "或用 WORKBUDDY_AUTH_FILE 指向明文凭据文件；"
               "也可以在服务器上安装并登录一次 CodeBuddy CLI。",
               2)


def build_headers(creds: Dict[str, Any]) -> Dict[str, str]:
    auth = creds.get("auth") or {}
    account = creds.get("account") or {}
    token = auth.get("accessToken")
    if isinstance(token, dict) or (isinstance(token, str) and "$wbEncrypted" in token):
        raise Fail("UNSUPPORTED_ENVELOPE", ENVELOPE_HINT, 6)
    if not isinstance(token, str) or not token.strip() or any(c.isspace() for c in token):
        raise Fail("INVALID_TOKEN",
                   "凭据里的 accessToken 为空或格式非法（含空白字符）。", 6)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % token.strip(),
        "User-Agent": USER_AGENT,
    }
    uid = account.get("uid")
    if uid:
        headers["X-User-Id"] = str(uid)
    enterprise_id = account.get("enterpriseId")
    if enterprise_id:
        headers["X-Enterprise-Id"] = str(enterprise_id)
        headers["X-Tenant-Id"] = str(enterprise_id)
    if auth.get("domain"):
        headers["X-Domain"] = str(auth["domain"])
    return headers


# ---------------------------------------------------------------- 网络层

def resolve_endpoint() -> str:
    endpoint = os.environ.get("WORKBUDDY_API_BASE", DEFAULT_ENDPOINT).rstrip("/")
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_ENDPOINT_HOSTS:
        raise Fail("UNSAFE_ENDPOINT",
                   "拒绝向非白名单 HTTPS 域名发送登录凭据：%s" % endpoint, 5)
    return endpoint


class Budget:
    def __init__(self, seconds: int):
        self.deadline = time.monotonic() + seconds

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def expired(self) -> bool:
        return self.remaining() <= 0


def post_json(endpoint: str, path: str, headers: Dict[str, str],
              budget: Budget, retry: bool = True) -> Tuple[int, Dict[str, Any]]:
    """POST 空 JSON 体。只对网络错误 / 5xx 做事先限定的退避重试。"""
    data = b"{}"
    ctx = ssl.create_default_context()
    attempts = len(RETRY_DELAYS) + 1 if retry else 1

    last_error: Optional[Fail] = None
    for attempt in range(attempts):
        if attempt and budget.expired():
            raise Fail("TIMEOUT", "已达本次运行的时间预算，剩余步骤留待下次。", 3)
        req = urllib.request.Request(endpoint + path, data=data,
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=ctx) as resp:
                raw = resp.read().decode("utf-8", "replace")
                try:
                    return resp.status, json.loads(raw)
                except ValueError:
                    raise Fail("INVALID_RESPONSE", "接口返回了无法解析的数据。", 3)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code in (401, 403):
                raise Fail("AUTH_REJECTED",
                           "服务端拒绝认证（HTTP %d），令牌可能已过期或权限不足。" % exc.code, 2)
            if 500 <= exc.code < 600 and attempt < attempts - 1:
                last_error = Fail("NETWORK", "服务端返回 HTTP %d。" % exc.code, 3)
            else:
                detail = ""
                try:
                    parsed = json.loads(body)
                    detail = str(parsed.get("msg") or parsed.get("message") or "")[:200]
                except ValueError:
                    detail = body[:200]
                raise Fail("HTTP_ERROR",
                           "接口返回 HTTP %d %s" % (exc.code, detail), 3)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            last_error = Fail("NETWORK", "网络请求失败：%s" % reason, 3)
        if attempt < attempts - 1:
            delay = RETRY_DELAYS[attempt]
            if delay > budget.remaining():
                break
            time.sleep(delay)
    raise last_error or Fail("NETWORK", "网络请求失败。", 3)


def extract_data(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise Fail("INVALID_RESPONSE", "接口返回格式异常。", 3)
    if payload.get("code") != 0:
        raise Fail("API_ERROR", str(payload.get("msg") or "接口请求失败"), 4)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise Fail("INVALID_RESPONSE", "接口缺少 data 字段。", 3)
    return data


STATUS_KEYS = ("active", "today_checked_in", "today_credit", "daily_credit",
               "streak_days", "total_credits", "end_time")
CLAIM_KEYS = ("credit", "credits", "today_credit", "streak_days",
              "total_credits", "message")


def _pick(data: Dict[str, Any], keys: Tuple[str, ...]) -> Dict[str, Any]:
    return {k: data[k] for k in keys if k in data}


# ---------------------------------------------------------------- 业务逻辑

def do_status(endpoint: str, headers: Dict[str, str], budget: Budget) -> Dict[str, Any]:
    _, payload = post_json(endpoint, STATUS_PATH, headers, budget)
    return extract_data(payload)


def do_claim(endpoint: str, headers: Dict[str, str], budget: Budget) -> Dict[str, Any]:
    status = do_status(endpoint, headers, budget)

    if not status.get("active", False):
        return {"result": "INACTIVE", **_pick(status, STATUS_KEYS),
                "report": "当前没有进行中的签到活动（非签到季属正常）。"}

    if status.get("today_checked_in", False):
        streak = status.get("streak_days")
        total = status.get("total_credits")
        return {"result": "ALREADY", **_pick(status, STATUS_KEYS),
                "report": "今日已签过（连续 %s 天，累计 %s 积分）。" % (streak, total)}

    try:
        _, payload = post_json(endpoint, CLAIM_PATH, headers, budget)
        claim = extract_data(payload)
    except Fail:
        # 领取请求异常时复核一次状态：可能服务端已处理成功
        try:
            refreshed = do_status(endpoint, headers, budget)
        except Fail:
            raise
        if refreshed.get("today_checked_in", False):
            return {"result": "ALREADY", **_pick(refreshed, STATUS_KEYS),
                    "report": "今日已签过（领取被判重，视为已成功）。"}
        raise

    verified = do_status(endpoint, headers, budget)
    if not verified.get("today_checked_in", False):
        raise Fail("VERIFY_FAILED", "领取接口返回成功，但状态复核未通过。", 4)

    credit = claim.get("credit", claim.get("credits", claim.get("today_credit")))
    streak = verified.get("streak_days")
    total = verified.get("total_credits")
    return {"result": "CLAIMED", **_pick(claim, CLAIM_KEYS), **_pick(verified, STATUS_KEYS),
            "report": "成功领取 %s 积分（连续 %s 天，累计 %s 积分）。" % (credit, streak, total)}


# ---------------------------------------------------------------- 进程锁

class Lock:
    def __init__(self, path: Path, stale_seconds: int = 900):
        self.path = path
        self.stale_seconds = stale_seconds
        self.fd: Optional[int] = None

    def __enter__(self) -> "Lock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError:
                age = 0
            if age > self.stale_seconds:
                try:
                    self.path.unlink()
                except OSError:
                    pass
        try:
            self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise Fail("ALREADY_RUNNING", "另一个签到进程正在运行，本次跳过。", 0)
        return self

    def __exit__(self, *exc_info) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        try:
            self.path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- 自检

def cmd_doctor(as_json: bool) -> int:
    info: Dict[str, Any] = {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "endpoint": os.environ.get("WORKBUDDY_API_BASE", DEFAULT_ENDPOINT),
        "budget_seconds": _budget_seconds(),
        "looked_in": [],
    }
    account_name = os.environ.get("WORKBUDDY_ACCOUNT", "").strip()
    if account_name:
        info["account"] = account_name
        home_root = account_scope_home()
        if home_root is not None:
            info["account_home"] = str(home_root)
    config_dir = (os.environ.get("WORKBUDDY_CONFIG_DIR")
                  or os.environ.get("CODEBUDDY_CONFIG_DIR"))
    if config_dir:
        info["config_dir"] = config_dir
    try:
        creds, looked = load_credentials()
        headers = build_headers(creds)
        auth = creds.get("auth") or {}
        info["looked_in"] = looked
        info["credential_source"] = creds.get("_source")
        info["token"] = mask_token(auth.get("accessToken"))
        info["has_uid"] = "X-User-Id" in headers
        info["has_enterprise"] = "X-Enterprise-Id" in headers
        expiry_text, expired = describe_expiry(auth)
        info["expiry"] = expiry_text
        info["expired"] = expired
        info["result"] = "EXPIRED" if expired else "READY"
        prefix = "[账号 %s] " % account_name if account_name else ""
        identity = describe_identity(creds.get("account") or {})
        if identity:
            info["identity"] = identity
        info["report"] = "%s%s凭据可用（未联网校验）。令牌 %s，来源：%s" % (
            prefix, ("身份：%s。" % identity) if identity else "",
            info["token"], info["credential_source"])
        if expiry_text:
            info["report"] += "；%s" % expiry_text
        if not info["has_uid"]:
            info["report"] += "；警告：缺少 uid，若接口报错请补充 WORKBUDDY_UID。"
    except Fail as exc:
        info["result"] = exc.result
        info["report"] = str(exc)
        info["ok"] = False
    emit(info, as_json)
    if info.get("result") == "READY":
        return 0
    return 2 if info.get("result") in ("NO_AUTH", "EXPIRED") else 6


# ---------------------------------------------------------------- 入口

def _budget_seconds() -> int:
    raw = os.environ.get("WORKBUDDY_BUDGET_SECONDS")
    if not raw:
        return DEFAULT_BUDGET_SECONDS
    try:
        value = int(float(raw))
    except ValueError:
        return DEFAULT_BUDGET_SECONDS
    if value <= 0:
        return DEFAULT_BUDGET_SECONDS
    return min(value, MAX_BUDGET_SECONDS)


def _log_path() -> Optional[Path]:
    raw = os.environ.get("WORKBUDDY_SIGNIN_LOG", "").strip()
    if raw.lower() in ("0", "off", "none"):
        return None
    return Path(raw).expanduser() if raw else Path.home() / ".workbuddy" / "signin.log"


def _lock_path() -> Path:
    """锁文件。多账号场景下每个账号必须用各自的锁，否则会互相阻塞。"""
    raw = os.environ.get("WORKBUDDY_LOCK_FILE", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".workbuddy" / "signin.lock"


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="signin_linux.py",
        description="WorkBuddy 每日签到自动领积分（Linux 服务器版，纯标准库）")
    parser.add_argument("action", nargs="?", default="claim",
                        choices=["claim", "status", "doctor"],
                        help="claim=查询并领取（默认）；status=仅查询；doctor=离线自检")
    parser.add_argument("--json", action="store_true", help="输出单行 JSON，便于监控采集")
    parser.add_argument("--ignore-expiry", action="store_true",
                        help="忽略凭据里的 expiresAt 过期预检（该字段不可信时用）")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    log_path = _log_path()

    if args.action == "doctor":
        return cmd_doctor(args.json)

    try:
        with Lock(_lock_path()):
            creds, _looked = load_credentials()
            headers = build_headers(creds)
            endpoint = resolve_endpoint()
            budget = Budget(_budget_seconds())

            # 过期预检：本地就能判定，省掉一次必然失败的请求
            expiry_text, expired = describe_expiry(creds.get("auth") or {})
            if expired and not args.ignore_expiry:
                raise Fail("TOKEN_EXPIRED",
                           "%s。请重新注入明文令牌（加 --ignore-expiry 可跳过本预检）。"
                           % expiry_text, 2)

            if args.action == "status":
                status = do_status(endpoint, headers, budget)
                payload = {"result": "STATUS", **_pick(status, STATUS_KEYS)}
                checked = status.get("today_checked_in")
                payload["report"] = ("今日已签（连续 %s 天，累计 %s 积分）。"
                                     % (status.get("streak_days"), status.get("total_credits"))
                                     if checked else "今日尚未签到。")
            else:
                payload = do_claim(endpoint, headers, budget)

            if expiry_text:
                payload["token_expiry"] = expiry_text
                payload["report"] = "%s %s" % (payload.get("report", ""), "（%s）" % expiry_text)
            payload["ok"] = True
            emit(payload, args.json, log_path)
            return 0

    except Fail as exc:
        emit({"ok": False, "result": exc.result, "report": str(exc),
              "needs_attention": exc.exit_code not in (0,)}, args.json, log_path)
        return exc.exit_code
    except KeyboardInterrupt:
        emit({"ok": False, "result": "ABORTED", "report": "被用户中断。"}, args.json)
        return 130
    except Exception as exc:  # noqa: BLE001 - 兜底，异常不静默丢失
        emit({"ok": False, "result": "ERROR",
              "report": "脚本运行异常（%s: %s）" % (type(exc).__name__, exc)},
             args.json, log_path)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
