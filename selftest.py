# -*- coding: utf-8 -*-
"""本地自测：验证 signin_linux.py 的凭据解析、自检、过期预检与错误路径。"""
import json, os, subprocess, sys, tempfile, time
from pathlib import Path

PY = sys.executable
HERE = Path(__file__).parent
SCRIPT = HERE / "signin_linux.py"

CLEAN_KEYS = ("WORKBUDDY_ACCESS_TOKEN", "WORKBUDDY_UID", "WORKBUDDY_AUTH_FILE",
              "WORKBUDDY_API_BASE", "WORKBUDDY_SIGNIN_LOG", "WORKBUDDY_ENTERPRISE_ID",
              "WORKBUDDY_BUDGET_SECONDS", "XDG_DATA_HOME", "LOCALAPPDATA", "USERPROFILE",
              "HOME",
              # 多账号相关
              "WORKBUDDY_ACCOUNT", "WORKBUDDY_CONFIG_DIR", "CODEBUDDY_CONFIG_DIR",
              "WORKBUDDY_HOME", "CODEBUDDY_HOME", "WORKBUDDY_LOCK_FILE")

def run(args, env=None, label="", isolate=True):
    """isolate=True 时把家目录指到临时目录，避免读到本机真实凭据。"""
    e = dict(os.environ)
    for k in CLEAN_KEYS:
        e.pop(k, None)
    tmp = None
    if isolate:
        tmp = tempfile.mkdtemp(prefix="wbsignin-test-")
        e["HOME"] = tmp
        e["USERPROFILE"] = tmp
        e["LOCALAPPDATA"] = os.path.join(tmp, "AppData", "Local")
        e["XDG_DATA_HOME"] = os.path.join(tmp, ".local", "share")
    if env:
        e.update(env)
    p = subprocess.run([PY, str(SCRIPT)] + args, capture_output=True, text=True,
                       env=e, timeout=180)
    print("--- %s ---" % label)
    print("exit:", p.returncode)
    print("out :", (p.stdout or "").strip()[:700])
    if p.stderr.strip():
        print("err :", p.stderr.strip()[:300])
    print()
    return p.returncode, p.stdout

# 1) 无凭据
rc, out = run(["doctor"], label="1. doctor / 无任何凭据")
assert rc == 2, "无凭据应退出码 2，实际 %d" % rc

# 2) 环境变量令牌
rc, out = run(["doctor", "--json"], env={"WORKBUDDY_ACCESS_TOKEN": "FAKE.TOKEN.FOR.TEST",
                               "WORKBUDDY_UID": "12345"},
              label="2. doctor / 环境变量令牌")
assert rc == 0, "有令牌应退出码 0，实际 %d" % rc
assert "FAKE.TOKEN.FOR.TEST" not in out, "令牌必须脱敏"
assert '"has_uid":true' in out

# 3) JSON 凭据文件（嵌套结构 + 未过期）
with tempfile.TemporaryDirectory() as d:
    f = Path(d) / "workbuddy-desktop.info"
    f.write_text(json.dumps({
        "auth": {"accessToken": "NESTED.TOKEN.VALUE",
                 "domain": "www.workbuddy.cn",
                 "expiresAt": int((time.time() + 20 * 86400) * 1000)},
        "account": {"uid": "9988-uuid", "enterpriseId": "ent-1"}}), encoding="utf-8")
    rc, out = run(["doctor", "--json"], env={"WORKBUDDY_AUTH_FILE": str(f)},
                  label="3. doctor / 嵌套凭据（有效期 20 天）")
    assert rc == 0 and "ent-1" not in out
    assert "NESTED.TOKEN.VALUE" not in out, "令牌必须脱敏"
    assert '"has_enterprise":true' in out
    assert "有效期至" in out and '"expired":false' in out, "应解析出有效期"

# 4) JSON 凭据文件（扁平结构）
with tempfile.TemporaryDirectory() as d:
    f = Path(d) / "flat.json"
    f.write_text(json.dumps({"accessToken": "FLAT.TOKEN.VALUE", "uid": "777"}),
                 encoding="utf-8")
    rc, out = run(["doctor", "--json"], env={"WORKBUDDY_AUTH_FILE": str(f)},
                  label="4. doctor / 扁平凭据文件")
    assert rc == 0, "扁平结构应被兼容，实际 %d" % rc

# 5) 加密信封必须给出针对性错误（而不是含糊的"未找到"）
with tempfile.TemporaryDirectory() as d:
    f = Path(d) / "encrypted.info"
    f.write_text(json.dumps({
        "auth": {"accessToken": {"$wbEncrypted": 1, "envelope": "x" * 64}},
        "account": {"uid": "u1"}}), encoding="utf-8")
    rc, out = run(["doctor", "--json"], env={"WORKBUDDY_AUTH_FILE": str(f)},
                  label="5. doctor / 加密信封（本机新版客户端格式）")
    assert rc == 6, "加密信封应退出码 6，实际 %d" % rc
    assert "UNSUPPORTED_ENVELOPE" in out and "CodeBuddy CLI" in out, "应给出可操作指引"

# 6) 令牌已过期 -> 预检直接拦下，不发网络请求
with tempfile.TemporaryDirectory() as d:
    f = Path(d) / "expired.json"
    f.write_text(json.dumps({
        "auth": {"accessToken": "EXPIRED.TOKEN.VALUE",
                 "expiresAt": int((time.time() - 3600) * 1000)},
        "account": {"uid": "u2"}}), encoding="utf-8")
    rc, out = run(["claim"], env={"WORKBUDDY_AUTH_FILE": str(f)},
                  label="6. claim / 已过期令牌（应本地拦下，退出码 2）")
    assert rc == 2, "过期应退出码 2，实际 %d" % rc
    assert "TOKEN_EXPIRED" in out
    # --ignore-expiry 应放行到网络层（随后因令牌无效而失败，但不是退出码 2 的预检）
    rc2, out2 = run(["claim", "--ignore-expiry"],
                    env={"WORKBUDDY_AUTH_FILE": str(f), "WORKBUDDY_BUDGET_SECONDS": "40"},
                    label="6b. claim --ignore-expiry（应放行到网络层）")
    assert "TOKEN_EXPIRED" not in out2, "--ignore-expiry 应跳过预检"

# 7) 非白名单端点必须拒绝
rc, out = run(["claim"], env={"WORKBUDDY_ACCESS_TOKEN": "FAKE.TOKEN.FOR.TEST",
                              "WORKBUDDY_UID": "1",
                              "WORKBUDDY_API_BASE": "http://evil.example.com"},
              label="7. 非白名单端点必须拒绝")
assert rc == 5, "非白名单端点应退出码 5，实际 %d" % rc

# 8) status 无凭据
rc, out = run(["status"], label="8. status / 无凭据")
assert rc == 2

# 9) 真实端点 + 无效令牌（唯一一次外网请求，验证错误处理不泄露令牌）
rc, out = run(["claim"], env={"WORKBUDDY_ACCESS_TOKEN": "invalid.token.probe",
                              "WORKBUDDY_UID": "0",
                              "WORKBUDDY_BUDGET_SECONDS": "40"},
              label="9. 真实端点 / 无效令牌（观察行为，不断言具体结果）")
assert "invalid.token.probe" not in out, "令牌不得出现在输出中"
assert rc != 0, "无效令牌不应成功"

# ============================================================ 多账号相关用例
CLI_REL = os.path.join("CodeBuddyExtension", "Data", "Public", "auth",
                       "Tencent-Cloud.coding-copilot.info")

def write_cli_cred(cfg_dir, token, uid, rel=None):
    """在某个 CLI 配置目录里写一份凭据，模拟 CLI 登录后的落盘结果。"""
    p = Path(cfg_dir) / (rel or CLI_REL)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"auth": {"accessToken": token},
                             "account": {"uid": uid}}), encoding="utf-8")
    return p


# 11) CODEBUDDY_CONFIG_DIR 扫描：按 CLI 标准落点自动找到凭据
with tempfile.TemporaryDirectory() as d:
    cfg = Path(d) / "codebuddy-acct-alice"
    write_cli_cred(cfg, "A" * 30, "uid-alice")
    rc, out = run(["doctor", "--json"],
                  env={"CODEBUDDY_CONFIG_DIR": str(cfg), "WORKBUDDY_ACCOUNT": "alice"},
                  label="11. doctor / CODEBUDDY_CONFIG_DIR 自动探测 + 账号名展示")
    assert rc == 0, "应能从 CLI 配置目录找到凭据，实际 %d" % rc
    assert "[账号 alice]" in out, "应展示账号名"
    assert '"config_dir"' in out, "应回显配置目录"
    assert "A" * 30 not in out, "令牌必须脱敏"

# 12) 多账号隔离：两个配置目录各自独立，互不串号
with tempfile.TemporaryDirectory() as d:
    cfg1 = Path(d) / "codebuddy-acct-alice"
    cfg2 = Path(d) / "codebuddy-acct-bob"
    write_cli_cred(cfg1, "A" * 30, "uid-alice")
    write_cli_cred(cfg2, "B" * 50, "uid-bob")
    _, out1 = run(["doctor", "--json"],
                  env={"WORKBUDDY_CONFIG_DIR": str(cfg1), "WORKBUDDY_ACCOUNT": "alice"},
                  label="12a. doctor / 账号 alice（令牌 30 位）")
    _, out2 = run(["doctor", "--json"],
                  env={"WORKBUDDY_CONFIG_DIR": str(cfg2), "WORKBUDDY_ACCOUNT": "bob"},
                  label="12b. doctor / 账号 bob（令牌 50 位）")
    assert "[账号 alice]" in out1 and "len=30" in out1, "alice 应读到自己的 30 位令牌"
    assert "[账号 bob]" in out2 and "len=50" in out2, "bob 应读到自己的 50 位令牌"
    assert "len=50" not in out1 and "len=30" not in out2, "两个账号不得串号"

# 13) 递归兜底：CLI 版本把凭据挪到非标准目录时仍能找到
with tempfile.TemporaryDirectory() as d:
    cfg = Path(d) / "codebuddy-acct-carol"
    write_cli_cred(cfg, "C" * 40, "uid-carol",
                   rel=os.path.join("some", "deep", "weird", "auth.json"))
    rc, out = run(["doctor", "--json"],
                  env={"WORKBUDDY_CONFIG_DIR": str(cfg), "WORKBUDDY_ACCOUNT": "carol"},
                  label="13. doctor / 非标准落点（rglob 兜底）")
    assert rc == 0, "递归兜底应找到凭据，实际 %d" % rc
    assert "len=40" in out

# 14) 锁隔离：多账号必须各用各的锁，否则会互相阻塞
with tempfile.TemporaryDirectory() as d:
    lock_a = Path(d) / "alice.lock"
    lock_a.write_text("", encoding="utf-8")          # 新鲜锁 -> 应判定"正在运行"
    rc, out = run(["claim"],
                  env={"WORKBUDDY_ACCESS_TOKEN": "T" * 30, "WORKBUDDY_UID": "1",
                       "WORKBUDDY_LOCK_FILE": str(lock_a)},
                  label="14a. 同一把锁被占用 -> ALREADY_RUNNING（幂等，退出码 0）")
    assert rc == 0, "锁被占用应退出码 0，实际 %d" % rc
    assert "ALREADY_RUNNING" in out, "应报告 ALREADY_RUNNING"

    rc, out = run(["claim"],
                  env={"WORKBUDDY_ACCESS_TOKEN": "T" * 30, "WORKBUDDY_UID": "1",
                       "WORKBUDDY_LOCK_FILE": str(Path(d) / "bob.lock"),
                       "WORKBUDDY_API_BASE": "http://evil.example.com"},
                  label="14b. 换个锁 -> 不再被判为运行中（应走到端点白名单检查=退出码 5）")
    assert rc == 5, "换锁后不应被判 ALREADY_RUNNING，实际 %d" % rc
    assert "ALREADY_RUNNING" not in out

# 15) 目录剪枝：plugins/ 下的干扰文件不得被当成凭据（配置目录会长到几十 MB）
with tempfile.TemporaryDirectory() as d:
    cfg = Path(d) / "codebuddy-acct-dave"
    write_cli_cred(cfg, "R" * 30, "uid-dave")                      # 真实凭据（标准落点）
    decoy = cfg / "plugins" / "marketplaces" / "decoy" / "auth.json"
    decoy.parent.mkdir(parents=True, exist_ok=True)
    decoy.write_text(json.dumps({"accessToken": "D" * 77, "uid": "decoy"}),
                     encoding="utf-8")
    rc, out = run(["doctor", "--json"],
                  env={"WORKBUDDY_CONFIG_DIR": str(cfg), "WORKBUDDY_ACCOUNT": "dave"},
                  label="15. doctor / 剪枝：plugins 下的干扰文件必须被忽略")
    assert rc == 0, "应找到真实凭据，实际 %d" % rc
    assert "len=30" in out and "len=77" not in out, "必须优先标准落点、跳过 plugins 目录"

# 10) 幂等性：并发运行应被文件锁挡住（同一时刻只跑一个）

# 16) 每账号独立 HOME：CLI 真实落点（<HOME>/.local/share/...）必须能被找到
with tempfile.TemporaryDirectory() as d:
    home = Path(d) / "wb-acct-alice"
    f = home / ".local" / "share" / CLI_REL
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"auth": {"accessToken": "H" * 45},
                             "account": {"uid": "uid-alice-home"}}), encoding="utf-8")
    rc, out = run(["doctor", "--json"],
                  env={"WORKBUDDY_HOME": str(home), "WORKBUDDY_ACCOUNT": "alice"},
                  label="16. doctor / 独立 HOME 下的 CLI 真实落点")
    assert rc == 0, "应能在独立 HOME 里找到凭据，实际 %d" % rc
    assert "len=45" in out and '"account_home"' in out, "应回显账号 HOME"
    assert "H" * 45 not in out, "令牌必须脱敏"

# 17) 【关键安全用例】账号模式绝不回落到全局桌面端凭据
#     根因：CLI 的凭据落在 ~/.local/share/...，而 CODEBUDDY_CONFIG_DIR 不隔离凭据；
#     若回落到全局路径，两个账号会共用同一份令牌（同一身份重复签到）。
with tempfile.TemporaryDirectory() as d:
    home = Path(d) / "fake-home"
    glob_cred = home / ".local" / "share" / CLI_REL
    glob_cred.parent.mkdir(parents=True, exist_ok=True)
    glob_cred.write_text(json.dumps({"auth": {"accessToken": "G" * 60},
                                     "account": {"uid": "global-uid"}}), encoding="utf-8")
    rc, out = run(["doctor", "--json"],
                  env={"HOME": str(home), "USERPROFILE": str(home),
                       "XDG_DATA_HOME": str(home / ".local" / "share"),
                       "WORKBUDDY_ACCOUNT": "alice"},
                  label="17. 账号模式 + 只有全局凭据 -> 必须 NO_AUTH（不得串号）")
    assert rc == 2, "账号模式下只存在全局凭据时应判 NO_AUTH，实际 %d" % rc
    assert "NO_AUTH" in out, "应报告 NO_AUTH"
    assert "len=60" not in out, "绝不能回落到全局凭据（这是串号 bug 的防线）"
    assert "login-cli.sh alice" in out, "应给出可操作指引"

# 18) 对照：非账号模式（单账号/本机）仍应能自动探测全局落点（向后兼容）
with tempfile.TemporaryDirectory() as d:
    home = Path(d) / "solo-home"
    xdg = home / ".local" / "share"
    f = xdg / CLI_REL
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"auth": {"accessToken": "S" * 33},
                             "account": {"uid": "solo-uid"}}), encoding="utf-8")
    rc, out = run(["doctor", "--json"],
                  env={"HOME": str(home), "USERPROFILE": str(home),
                       "XDG_DATA_HOME": str(xdg)},
                  label="18. 非账号模式 / 全局落点自动探测（向后兼容）")
    assert rc == 0, "单账号模式应仍能自动探测全局落点，实际 %d" % rc
    assert "len=33" in out

print("=" * 62)
print("全部断言通过 ✅")
