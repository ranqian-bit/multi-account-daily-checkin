# WorkBuddy 多账号自动签到 —— Linux 服务器版

> 在无图形界面的 Linux 服务器上，**每天自动为多个 WorkBuddy 账号领取签到积分**。
> 部署于 Linux 云服务器（Alibaba Cloud Linux 3），已实测跑通。

> 🔴 **合规声明（务必先读）**：本项目的「自动领取积分」行为**与 WorkBuddy 官方用户协议
> 明确冲突**——协议第 6.1(2) 条直接禁止"自动化脚本……滥用积分"，第 8.2 条禁止反向工程与
> 外挂登录。本项目**仅供个人学习与技术研究**，请勿用于多账号批量刷取、代领或任何商业用途。
> 使用风险（包括账号封禁、积分与订阅权益清零）由使用者自行承担。
> 完整条款比对见 [`协议合规核查.md`](协议合规核查.md)。

---

## 一、这个项目解决什么问题

WorkBuddy 官方客户端和社区脚本都假定「**定时任务跑在本机**」——因为鉴权令牌只存在于登录过客户端的机器上。服务器没有图形界面，所以要做三件事：

| 难点 | 本项目的做法 |
|------|-------------|
| 服务器上拿不到登录态 | 在服务器装 **CodeBuddy CLI**，用 tmux 顶替伪终端完成一次登录，之后凭据明文落盘，脚本自动探测 |
| 有**两个账号**，且只能手机号/微信登录 | 每个账号用**独立的 HOME**（`WORKBUDDY_HOME`），登录态完全隔离；配套独立的凭据文件、锁、日志、systemd 定时器 |
| 多账号容易互相踩 | 锁文件按账号分开（**不分开会互相阻塞**）；定时**自动错峰 30 分钟**（避免同一 IP 同时请求） |

> ### ⚠️ 隔离为什么必须用「独立 HOME」而不是 `CODEBUDDY_CONFIG_DIR`
>
> 这是本项目踩过的**最大的坑**，一定要看：
>
> CodeBuddy CLI 2.164.0 把登录凭据写在
> **`<HOME>/.local/share/CodeBuddyExtension/Data/Public/auth/Tencent-Cloud.coding-copilot.info`**，
> 这个路径**写死在 HOME 下、不读 `XDG_DATA_HOME`**；而 `CODEBUDDY_CONFIG_DIR` 只隔离
> **会话 / 插件 / 项目**数据，**完全不隔离凭据**。
>
> 后果：两个账号都用 `CODEBUDDY_CONFIG_DIR` 隔离时，**第二次登录会覆盖第一次**，
> 两个账号最终共用同一份令牌 —— 表现为「两个账号的 `doctor` 显示同一个令牌、
> 第二个账号提示`今日已签`」。实测已复现。
>
> 修复：给每个账号一个独立 HOME（`/root/.wb-acct-<账号>`），并对 CLI 与签到脚本
> 都生效。同时脚本增加了**硬防线**：只要设置了 `WORKBUDDY_ACCOUNT`，
> 就**只看该账号自己的落点，绝不回落到全局桌面端凭据**（否则就是串号 bug）。

---

## 二、三条硬约束（先看，决定方案的边界）

1. **接口**（已实测确认）
   ```
   POST https://copilot.tencent.com/v2/billing/meter/checkin-activity-status   # 只读查状态
   POST https://copilot.tencent.com/v2/billing/meter/daily-checkin             # 领取（幂等）
   头：Authorization: Bearer <accessToken> + X-User-Id: <uid>
   ```
   用无效令牌实测返回 **401**（不是 404），证明路径与请求头契约正确。

2. **新版客户端的令牌是加密信封** ⚠️
   `accessToken` 存的是 `{"$wbEncrypted":1,"envelope":"<两千多字密文>"}`，解密要调客户端运行时。
   **所以把桌面端的凭据文件拷到服务器是没用的**，服务器上必须用**明文**凭据。
   → 这就是为什么要装 CodeBuddy CLI（它写出的是明文）。

3. **令牌有两条寿命：accessToken 28 天、refreshToken 30 天** ✅ 已解决自动续期
   `expiresIn=2419200`(28 天)、`refreshExpiresIn=2592000`(30 天)。
   好消息：CLI 内置 `refreshTokenTimer`，会用 refreshToken 换回**全新的一对**令牌
   （两个有效期都从刷新时刻重新计算），**refreshToken 本身也会轮转** —— 这是滚动续期。
   本项目已把这件事自动化：`refresh_token.py` 在每次签到前检查，refreshToken 剩余不足
   20 天就驱动 CLI 刷新一次（约每 10 天一次）。
   → 用 CodeBuddy CLI 登录的账号**登录一次即可长期免扫码**；用明文令牌的账号没有
   refreshToken，仍需约 28 天重注入一次。

---

## 三、隔离设计（多账号的关键）

每个账号一套独立资源，互不干扰：

| 资源 | 路径（root 安装） |
|------|------------------|
| 凭据文件 | `/etc/workbuddy-signin/accounts/<账号>.env`（600） |
| **独立 HOME** | `/root/.wb-acct-<账号>`（CLI 的登录态就落在这里的 `.local/share/...`） |
| 锁文件 | `/run/workbuddy-signin/<账号>.lock` |
| 日志 | `/var/log/workbuddy-signin/<账号>.log` |
| systemd 定时器 | `workbuddy-signin@<账号>.timer`（实例独立） |

> 装到非 root 用户时，上述路径会相应落到 `$HOME/.local/share/workbuddy-signin/` 下，定时降级为 crontab。

---

## 四、快速开始

### 第 1 步：安装共享部分（只需一次）

```bash
sudo ./install.sh
```

装上：`/opt/workbuddy-signin/{signin_linux.py, run.sh, set-token.sh, login-cli.sh}`，
以及 systemd 模板单元 `workbuddy-signin@{.service,.timer}`。

### 第 2 步：添加账号（有多少加多少）

```bash
sudo ./install.sh --add-account acct1      # 第 1 个账号 → 自动定时 00:05
sudo ./install.sh --add-account acct2      # 第 2 个账号 → 自动错峰到 00:35
sudo ./install.sh --list                   # 查看账号、定时状态、上次日志
```

账号名只是标签，可用 `--time HH:MM` 指定时间。

### 第 3 步：给每个账号拿凭据（★ 需要你本人配合）

**方式 A：CodeBuddy CLI 登录（推荐，之后自动续期）**

```bash
sudo /opt/workbuddy-signin/login-cli.sh acct1
```

脚本会自动拉起 CLI、跳过「信任目录」提示，并把**一次性登录链接**打印出来：

```
https://copilot.tencent.com/login?platform=CLI&state=<uuid>&version=2.164.0
```

在你自己的浏览器里打开它，用**手机号/微信**完成登录即可。
**不需要 SSH 隧道** —— 这个 URL 没有 `redirect_uri`，是**服务端轮询**模式，任何设备打开都行。

然后对 `acct2` 重复一次（两个链接不通用，各自登录各自的账号）。

> ⚠️ **多账号最容易踩的坑**：第二个账号务必用**无痕窗口**打开链接，并输入**另一个手机号**。
> 普通窗口里已有第一个账号的登录态，点链接会被直接拿旧会话授权掉，
> 结果两个「账号」其实是同一个人（uid 相同），天天领的还是同一份积分。

验证：

```bash
sudo /opt/workbuddy-signin/audit-accounts.sh
# 期望两个账号的「账号身份」不同，并出现"全部正常"：
#   acct1   user-a / uid 1a2b3c4d…      READY   ...
#   acct2   user-b / uid 5e6f7a8b…      READY   ...
# 如果两边 uid 一样，它会直接报「串号」并以退出码 1 结束。
```

> 注意：**令牌指纹不同 ≠ 账号不同**。同一个账号登录两次也会签发出两个不同的令牌，
> 只有比对 `uid`（体检脚本里的「账号身份」）才能确认真的换人了。

确认续期机制就绪（可选，顺手做一次）：

```bash
sudo /opt/workbuddy-signin/refresh.sh acct1 --check    # 看还剩多久
sudo /opt/workbuddy-signin/refresh.sh acct1 --force    # 立刻续期一次，验证链路通
```

**方式 B：注入明文令牌（立刻可用，28 天后要再来一次）**

```bash
sudo /opt/workbuddy-signin/set-token.sh acct1
```
回车后粘贴（**不回显、不进 shell 历史**），会自动写入并跑一次自检。

### 第 4 步：试跑

```bash
sudo /opt/workbuddy-signin/run.sh acct1 claim
sudo /opt/workbuddy-signin/run.sh acct2 claim
```

看到这样就成功：

```
成功领取 100 积分（连续 5 天，累计 500 积分）。（令牌有效期至 2026-11-08 12:00（剩约 27 天））
```

### 第 5 步：确认定时器

```bash
systemctl list-timers 'workbuddy-signin@*'
journalctl -u 'workbuddy-signin@acct1' -n 30 --no-pager
```

---

## 五、命令速查

| 目的 | 命令 |
|------|------|
| 安装共享部分 | `sudo ./install.sh` |
| 添加账号 | `sudo ./install.sh --add-account <名> [--time HH:MM]` |
| 移除账号 | `sudo ./install.sh --remove-account <名>` |
| 查看所有账号 | `sudo ./install.sh --list` |
| 卸载（保留账号配置） | `sudo ./install.sh --uninstall` |
| 卸载并清除凭据 | `sudo ./install.sh --uninstall --purge` |
| CLI 登录 | `sudo /opt/workbuddy-signin/login-cli.sh <名> [--wait\|--show\|--kill]` |
| 注入令牌 | `sudo /opt/workbuddy-signin/set-token.sh <名>` |
| 自检（离线） | `sudo /opt/workbuddy-signin/run.sh <名> doctor` |
| 仅查状态 | `sudo /opt/workbuddy-signin/run.sh <名> status` |
| 领取 | `sudo /opt/workbuddy-signin/run.sh <名> claim` |
| 令牌还剩多久 | `sudo /opt/workbuddy-signin/refresh.sh <名> --check` |
| 立刻续期一次 | `sudo /opt/workbuddy-signin/refresh.sh <名> --force` |
| 多账号体检（查串号） | `sudo /opt/workbuddy-signin/audit-accounts.sh` |
| 手动触发定时器 | `sudo systemctl start workbuddy-signin@<名>.service` |
| 看日志 | `tail -f /var/log/workbuddy-signin/<名>.log` |
| 看续期日志 | `tail -f /var/log/workbuddy-signin/<名>.refresh.log` |

**签到脚本退出码**：`0` 成功/已签/正在运行 · `2` 无凭据或令牌失效 · `3` 网络失败 · `4` 接口业务失败 · `5` 端点被拒（非白名单） · `6` 配置错误

**续期脚本退出码**：`0` 已续期或无需续期 · `2` 无凭据 · `3` 续期失败（已自动回滚） · `4` 环境不支持（缺 CLI/tmux，或该账号用的是明文令牌）

---

## 六、排错

| 现象 | 原因与处理 |
|------|-----------|
| `NO_AUTH` | 该账号的 `.env` 里令牌为空且该账号 HOME 里没凭据。先跑 `login-cli.sh <名>` 或 `set-token.sh <名>` |
| **文件里明明有令牌却一直 NO_AUTH** | `run.sh` 用 `. <账号>.env` 加载变量时漏了 **`set -a`** —— 变量只存在 shell 内，不会传给 `exec` 出去的子进程。跑 `sh selftest-server.sh` 可复现/回归此问题 |
| **两个账号的 `doctor` 显示同一个令牌 / 第二个账号提示「今日已签」** | **串号**：隔离用的是 `CODEBUDDY_CONFIG_DIR`（不隔离凭据），两个账号共用了 `~/.local/share/...` 下的同一份凭据。必须改用**独立 HOME**（`WORKBUDDY_HOME=/root/.wb-acct-<名>`），见本文第一节的警告 |
| **体检显示两个账号 uid 相同** | 令牌是各自独立的，但登录时浏览器复用了同一个账号的会话。用**无痕窗口 + 另一个手机号**重新登录第二个账号（`login-cli.sh <名>` 重新取链接） |
| 续期后仍提示令牌即将过期 | 看 `/var/log/workbuddy-signin/<名>.refresh.log`：`SKIPPED` 表示未到阈值；`REFRESH_FAILED` 表示已回滚（原令牌仍可用），连续失败就要重新扫码 |
| 续期一直 `UNSUPPORTED` | 该账号凭据里没有 `refreshToken`（走的是 `set-token.sh` 明文令牌方式），或服务器上没装 CLI / tmux |
| `UNSUPPORTED_ENVELOPE` | 粘的是桌面端加密信封。改用 CLI 登录，或取明文令牌 |
| `TOKEN_EXPIRED` | 本地预检发现已过 28 天，重新注入（`--ignore-expiry` 可跳过预检） |
| `AUTH_REJECTED`(401) | 服务端拒绝：令牌真失效，或 uid 不匹配 |
| 退出码 3 / `ALREADY_RUNNING` | 上一次还在跑（文件锁），本次跳过，不算失败 |
| `login-cli.sh` 报缺少 tmux | `dnf install -y tmux`（Debian 系：`apt install -y tmux`） |
| CLI 登录后仍读不到凭据 | `login-cli.sh <名> --show` 看界面、账号 HOME 与凭据落点；CLI 升级可能改了落点，脚本有递归兜底扫描 |
| 两个账号互相阻塞 | 检查各自 `.env` 里的 `WORKBUDDY_LOCK_FILE` **是否不同** |

---

## 七、安全说明

| 保护层 | 实现 |
|--------|------|
| 令牌存储 | `.env` 权限 **600**、目录 **700**，仅 root 可读 |
| 令牌脱敏 | 任何输出只显示 `前4...后4(len=N)`，绝不打印完整令牌 |
| 注入方式 | `set-token.sh` 用 `stty -echo`，不回显、不进 shell 历史 |
| 端点白名单 | 只允许把凭据发往 `copilot.tencent.com`，防止环境变量被劫持后泄露令牌 |
| 传输 | 全程 HTTPS |
| 登录链接 | 一次性、服务端轮询；会话用完可 `login-cli.sh <名> --kill` 关掉 |

> ⚠️ 这类脚本走的是**逆向接口**，建议只用于自己的账号，**不要多账号批量刷**（会触发风控）。
> 腾讯改一版接口就可能失效，建议给上游 `88lin/workbuddy-auto-signin` 点个 Star 方便回头找修复。

---

## 八、文件清单

| 文件 | 说明 |
|------|------|
| `signin_linux.py` | 签到脚本本体（纯标准库，单文件，Python 3.7+） |
| `refresh_token.py` | **令牌自动续期**：refreshToken 快到期时驱动 CLI 滚动换新，免重新扫码（纯标准库） |
| `install.sh` | 多账号安装器：装共享部分 / 加账号 / 删账号 / 列表 / 卸载 |
| `login-cli.sh` | CodeBuddy CLI 登录驱动（tmux 伪终端 + 自动抓登录链接） |
| `audit-accounts.sh` | **多账号体检**：列出各账号身份（昵称 + uid），揪出「两个账号其实是同一个人」 |
| `set-token.sh` | 明文令牌安全注入（按账号） |
| `selftest.py` | 本地自测，19 组用例（凭据解析 / 信封 / 过期 / 白名单 / **独立 HOME 隔离与「不回落全局凭据」防线** / 锁） |
| `selftest-server.sh` | **服务器侧**自测，13 项：专测 shell 包装层（`set -a` 坑）、独立 HOME 凭据读取、账号隔离（不串号）与定时器 |
| `README.md` | 本文档 |
| `部署记录.md` | 本次在服务器上的实际部署与验证记录 |
| `协议合规核查.md` | **官方协议逐条比对**：本项目与 WorkBuddy 用户协议的冲突点（上传前必读） |
| `.gitignore` | 挡住凭据 / 备份 / 本地真实信息文件 |
| `请勿上传-真实信息备查.md` | 本地真实值备查（**已被 `.gitignore` 排除，勿提交**） |

---

## 九、定时任务细节

systemd 模板单元 + 每账号 drop-in：

```ini
# /etc/systemd/system/workbuddy-signin@.service
[Service]
Type=oneshot
# 签到前先尝试续期：refreshToken 快到 30 天上限就滚动换新，失败不影响签到
ExecStartPre=-/opt/workbuddy-signin/refresh.sh %i
ExecStart=/opt/workbuddy-signin/run.sh %i claim
TimeoutStartSec=900
```

> `ExecStartPre` 前面的 `-` 表示**忽略失败**：续期不成功也照常签到
> （原令牌只要没过期就还能用），不会因为续期出问题把签到一起拖垮。

```ini
# /etc/systemd/system/workbuddy-signin@acct2.timer.d/10-schedule.conf
[Timer]
OnCalendar=
OnCalendar=*-*-* 00:35:00
RandomizedDelaySec=300
Persistent=true
```

- `Persistent=true`：关机/宕机错过的执行，开机后补跑
- `RandomizedDelaySec=300`：±5 分钟随机抖动
- 无 systemd 时自动降级为 `/etc/cron.d/workbuddy-signin`

### 自动续期是怎么工作的

1. 每次签到前，`refresh.sh` 先看 `refreshExpiresAt` 还剩多少天；
2. 剩余 > 20 天 → 直接 `SKIPPED` 退出（几毫秒，不碰任何文件）；
3. 剩余 ≤ 20 天 → 把凭据里的 `expiresAt` **临时**改到 1 分钟后（只在本地生效，
   服务端并不知道），再用 tmux 拉起 CLI —— CLI 发现「快过期了」，
   立刻带 `X-Refresh-Token` 去服务端换回全新的一对令牌（两个有效期都重新计时）；
4. 校验新令牌指纹确实变了、有效期确实往后挪了，才算成功；任何一步不对，
   就把备份整体还原（`*.pre-refresh`），**绝不会留下半坏的凭据**。

按 30 天寿命、20 天阈值算，实际约**每 10 天**自动续一次，
而且每个账号的续期日志单独落在 `/var/log/workbuddy-signin/<名>.refresh.log`。
