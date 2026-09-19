# TG2Gotify · TG 频道监控脚本

用 **你自己的 Telegram 账号**盯着指定的频道 / 机器人，消息按关键词过滤后推到 **Gotify** 手机通知。
典型用途：**VPS 补货 / 限量补货 / 商家上新**这类推送监控——盯一次，命中才响，不用一直刷频道。

专为 Linux 服务器（Debian / Ubuntu 等）设计，特别照顾 **中国网络环境**：Telegram 走本地代理
（连 DNS 解析也走代理，防污染），Gotify 在局域网所以强制直连、不绕代理。

GitHub 上 TG → Gotify 方向没有现成项目（搜到的全是反方向的 gotify → telegram），本项目填补这个空缺。

> **说清楚它是什么**：这是一个**监控/转发**工具——它只负责「盯频道 → 过滤 → 推通知」，
> 不做消息存储、不做数据分析、不碰 VPS 商家官网。所有消息和判断都留在你自己的机器上。
>
> **关于 v1 / v2**：本项目起步时有一个只在作者自己机器上跑的自用版本（文档里简称 v1），
> 公开的是重写后的 v2。**v1 从未对外发布，所以没有「老用户」**；代码里保留的兼容逻辑只为了
> 能读早期那种「每个频道各存一份关键词」的 config 格式，对外部使用者没有历史包袱。

---

## 目录

- [特性](#特性)
- [工作原理](#工作原理)
- [文件结构](#文件结构)
- [快速开始](#快速开始debian--ubuntu)
- [配置说明](#配置说明)
- [WebUI 使用](#webui-使用可选)
- [通知渠道（Gotify）](#通知渠道gotify)
- [功能清单（验收对照）](#功能清单预期行为验收对照)
- [明确不做 / 已知边界](#明确不做--已知边界)
- [路线图（以后可能会加）](#路线图以后可能会加)
- [中国网络环境说明](#中国网络环境说明本项目的核心卖点之一)
- [常见问题](#常见问题)
- [License](#license)

---

## 特性

- **共享关键词池**：改一次池子，所有走过滤的频道同时生效（不用每个频道各存一份）
- **频道级四参数**：每频道可独立 `启用` / `全量转发` / 私有额外关键词 / 排除黑名单
- **热重载**：改配置 **5 秒内生效**，Telegram 连接不断线（不用重启服务）
- **推送点击直达**：手机上点通知直接跳到 TG 原帖；聚合频道**转发**的消息也能解析出**原始出处**链接
  （v1 里转发消息只带聚合频道的链接，得进 TG 重新翻）
- **WebUI**：网页上改配置——频道开关、关键词池、出站代理、通知渠道，
  还能**启停后端**、**网页扫码 / 验证码登录 Telegram**、**发测试推送**
  （纯标准库 `http.server` 实现，零额外依赖）
- **中文文档 + 中国网络适配**：代理、DNS 防污染、局域网直连都替你想好了

### 本版本相比早期自用版的变化

（早期自用版从未对外发布，此表只为了解设计取舍；公开版本从这一版开始）

| | 早期自用版 | 本版本 |
|---|---|---|
| 关键词 | 每个频道各存一份数组 | 共享池 `KEYWORD_POOL` + 频道私有词 |
| 改配置 | 必须重启服务 | 热重载，5 秒生效不断线 |
| 界面 | 无 | WebUI（配置 / 状态 / 登录 / 测试推送） |
| 登录 | 只有终端扫码脚本 | 网页扫码 + 验证码（含两步验证） |
| 转发消息 | 只有聚合频道链接 | 优先解析原始出处链接 |
| 兼容 | — | 早期格式配置自动兼容，语义不变 |

---

## 工作原理

```
Telegram 频道 / 机器人
      │  (Telethon 以你的账号监听，流量走本地代理)
      ▼
tg2gotify.py ── 过滤：enabled → 关键词命中 → 排除黑名单
      │  (Gotify 在局域网 → 强制直连，不走代理)
      ▼
Gotify → 手机 / 电脑秒收推送（点通知直达原帖）
```

---

## 文件结构

```
tg2gotify.py            # 主程序：监听 + 过滤 + 推送 + 热重载 + 状态心跳
filter.py               # 匹配逻辑（独立小模块，便于单测）
webui.py                # 配置 WebUI（独立进程，可选运行）
tglogin.py              # WebUI 里的 Telegram 登录（扫码 / 验证码 / 两步验证）
qr_login.py             # 终端扫码登录（不想开 WebUI 时用这个）
config.example.json     # 配置模板（无机密，随仓库）
tg2gotify.service       # systemd 模板（主程序，含内存 / CPU 上限）
tg2gotify-webui.service # systemd 模板（WebUI，可选，含内存 / CPU 上限）
requirements.txt        # telethon、python-socks、requests、qrcode（后者只扫码登录用）
LICENSE                 # MIT
```

> 真实的 `config.json`、`*.session`、`run_status.json`、`login_state.json`、`*.log`
> 含机密或运行时状态，**永不入库**（`.gitignore` 已覆盖，包括带时间戳的 `config.json.bak-*`）。

---

## 快速开始（Debian / Ubuntu）

### 1. 准备

```bash
sudo apt install python3 python3-venv
git clone https://github.com/992939504/tg2gotify.git /opt/tg2gotify
cd /opt/tg2gotify
/usr/bin/python3 -m venv venv          # 本机裸 python3 -m venv 会报错时用绝对路径
./venv/bin/pip install -r requirements.txt
```

### 2. 配置

```bash
cp config.example.json config.json
vi config.json
```

必填：

- `API_ID` / `API_HASH`：在 [my.telegram.org](https://my.telegram.org) 申请（API development tools 页面）
- `GOTIFY_URL` / `GOTIFY_TOKEN`：你的 Gotify 地址 + 应用 token
  （Gotify 网页端 → **Apps** → **CREATE APPLICATION** → 复制那串 token）
- `PROXY`：中国网络环境默认 `socks5://127.0.0.1:7890`（Clash / Mihomo / ShellCrash 常用端口）；
  海外服务器把 `enabled` 改成 `false`
- `KEYWORD_POOL` / `SOURCES`：见[配置说明](#配置说明)

### 3. 首次登录（二选一）

**A. 网页登录（推荐，扫码或验证码都行）**——先起 WebUI：

```bash
./venv/bin/python webui.py     # 默认 8098；首次启动自动生成访问令牌并打印在终端
```

浏览器打开 `http://<服务器IP>:8098/` → 用令牌登录 → 点「📱 TG 登录 / 重新登录」→
扫码（手机 TG → 设置 → 设备 → 链接桌面设备）或填手机号收验证码（开了两步验证会再要密码）。

**B. 终端扫码**：

```bash
./venv/bin/python qr_login.py
```

终端显示二维码 → 手机扫码确认。登录成功后 session 文件已保存，以后免登录。

> 同一个 Telegram session **不能两处同时用**：网页登录前请先停掉后端监控
> （WebUI 的「运行状态」里有「⏹ 停止后端」按钮，或者 `systemctl stop tg2gotify`）。

### 4. 试运行

```bash
./venv/bin/python tg2gotify.py
```

看到「出站代理」「已登录」「开始监听」即成功；默认会发一条启动测试推送（`SEND_STARTUP_TEST` 可关）。

### 5. systemd 常驻

```bash
sudo cp tg2gotify.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tg2gotify
journalctl -u tg2gotify -f       # 看日志
```

模板里带了资源硬上限（`MemoryHigh=150M` / `MemoryMax=256M` / `CPUQuota=30%` / `TasksMax=64`），
常驻实际占用约 40–60 MB，可按机器情况调。

### 6. WebUI 常驻（可选）

仓库里带了一份单元文件 `tg2gotify-webui.service`（已设好内存 / CPU 上限）：
```bash
sudo cp tg2gotify-webui.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tg2gotify-webui
```

> WebUI 是**局域网工具**：绑 `0.0.0.0:8098`、token 认证、能启停后端进程。
> 只建议内网使用；非要暴露公网请务必换强令牌 + 反向代理 + HTTPS。
>
> 配置页的「🔌 运行状态」卡里也会实时告诉你是谁在管这个后端：
> **systemd 管理**（开机自启）还是**网页/命令行拉起的**（重启机器不会自己回来，同时给出上面的注册命令）。

---

## 配置说明

### 字段速查

| 字段 | 必填 | 说明 |
|---|---|---|
| `API_ID` / `API_HASH` | ✅ | my.telegram.org 申请 |
| `GOTIFY_URL` | ✅ | 如 `http://192.168.1.100:8080`（也可写内网域名，要带 `http://`） |
| `GOTIFY_TOKEN` | ✅ | Gotify 应用 token（WebUI 里也能改） |
| `GOTIFY_PRIORITY` | | 推送优先级，默认 8（0–10，见[通知渠道](#通知渠道gotify)） |
| `SEND_STARTUP_TEST` | | 默认 true，启动发一条测试推送 |
| `KEYWORD_POOL` | | 共享关键词池（数组） |
| `SOURCES` | ✅ | 监听来源，见下面的过滤规则 |
| `PROXY` | | `{enabled, type: socks5/socks4/http, host, port}`，默认 `socks5://127.0.0.1:7890` |
| `SESSION_NAME` | | session 文件名，默认 `tg2gotify` |
| `HEARTBEAT_MINUTES` | | 日志心跳间隔，默认 30（热重载生效，下限钳到 1） |
| `WEBUI_TOKEN` | | WebUI 访问令牌（留空则 `webui.py` 首启自动生成并打印） |
| `WEBUI_PORT` | | WebUI 端口，默认 8098 |

页面（WebUI）只会改 `KEYWORD_POOL` / `SOURCES` / `PROXY` / `GOTIFY_*` 这几个字段，
**其余字段（`API_ID`、`API_HASH`、`SESSION_NAME`、`WEBUI_TOKEN`、`SEND_STARTUP_TEST`、
`HEARTBEAT_MINUTES`）保存时原样保留**；手写进去的额外键（比如代理的用户名、频道里的自定义字段）
也不会被页面保存抹掉（只有已被迁移的 v1 字段 `keywords` 会被清掉）。

> 代理目前只支持「类型 / 地址 / 端口」，**不支持用户名密码认证**（Telethon 支持，但本项目没接）。
> 配置文件里含 `API_HASH` 与 Gotify token，`webui.py` 保存时会按 `600` 权限落盘；
> session 文件在连接后也会自动 `chmod 600`。

### 过滤规则

```json
{
  "KEYWORD_POOL": ["9929", "TRI", "补货", "特价"],
  "SOURCES": {
    "vpsme1_bot": {
      "label": "VPSME补货",
      "enabled": true,
      "use_pool": true,
      "extra_keywords": [],
      "exclude_keywords": []
    },
    "vmiss_com": {
      "label": "VMISS官方",
      "enabled": true,
      "use_pool": false,
      "extra_keywords": [],
      "exclude_keywords": ["广告"]
    }
  }
}
```

| 字段 | 含义 |
|---|---|
| `enabled` | `0`/`false` = 这条频道**暂时静默**（一条都不推，配置留着）；`1`/`true` = 监听 |
| `use_pool` | `true` = 走关键词过滤（共享池 + 频道私有词）；`false` = **全量转发** |
| `extra_keywords` | 频道私有关键词，与共享池是 **或** 的关系 |
| `exclude_keywords` | 排除黑名单，**两种模式下都生效**（优先级最高） |

判定顺序：

1. `enabled=0` → 跳过
2. `use_pool=true` → 消息包含池子词 **或** 频道私有词任一（不区分大小写）即命中；
   `use_pool=false` → 无条件命中
3. 消息包含任一排除词 → 丢弃（全量转发模式下同样生效）

**词的写法**：一行一个（回车换行），也可以用逗号；**空格不算分隔符**——
`9929 TRI` 会被当成一个词，别用空格隔词。（WebUI 里输入时会实时回显「分词结果」，不怕分不清。）

> **早期自用版**的 config 里每个频道存一份 `keywords`（数组），这种写法会被自动兼容：
> 各频道相同的关键词提升为共享池 `KEYWORD_POOL`，独有词留在该频道的 `extra_keywords`；
> `keywords` 为空/缺省（= 全量转发）的频道自动映射为 `use_pool=false`。兼容是**尽力而为**：
> 规则冲突时**以本版本的规则为准**（例如那种配置里就算显式写了 `use_pool`，也一律按本版本的用法解释）。
> 新部署不用管这些——直接照上面的字段写就行。

---

## WebUI 使用（可选）

浏览器打开 `http://<服务器IP>:8098/`，用 `WEBUI_TOKEN` 登录
（也可以直接访问 `http://<ip>:8098/login?token=xxx`）。

页面从上到下：

| 区块 | 能干什么 |
|---|---|
| **⚙️ 运行与推送设置** | 一张卡里三行，点哪行展开哪行（收起时右边留一行摘要） |
| 　🔌 运行状态 | TG 登录态、后端进程是否在跑 / 心跳是否还活着；**▶ 启动后端 / ⏹ 停止后端** 按钮（起不来会把日志尾部原样显示出来）；跳转 TG 登录页 |
| 　🌐 出站代理 | 直连 / 走代理切换，代理类型（socks5 / socks4 / http）+ 地址 + 端口 |
| 　📣 通知渠道 | Gotify 地址 / 应用 Token（可切明文）+ 推送优先级 + **📤 发一条测试推送** |
| **📚 共享关键词池** | 编辑池子，实时回显分词结果 |
| **📺 监听频道** | 每个频道：显示名、`启用`、`走关键词过滤`、私有额外关键词、排除黑名单、删除（二次确认）；顶部有参数说明；切换成「静默 / 全量转发」时那一行会冒出红字提示现状 |
| **➕ 新增监控频道** | 列表下方一条淡绿虚线框，点开填 username（**直接粘 `https://t.me/xxx`、`@xxx`、`tg://resolve?domain=xxx` 都行，会自动只留用户名**）→ 加完自动收起，新加那行高亮，底部「保存全部」后才写盘 |
| **底部条** | 保存全部（原子写入 + 自动留 `config.json.bak`）、TG 登录、退出 |

几个要点：

- **保存即写 `config.json`**，主程序 **5 秒内热重载**，Telegram 连接不断线
- **网页上的后端按钮**启停的是「WebUI 同目录的那个后端」；已有实例在跑时会拒绝重复启动
  （同一个 TG session 不能两处同时用）。如果后端由 systemd 托管，停止走 `systemctl stop`
  （服务是 `Restart=always`，直接 kill 会被 systemd 拉回来）
- 浏览器打开页面看状态是**只读轮询**（5 秒一次），不会去动你的服务
- 登录页开着时，网页登录会用独立进程操作 session，所以要求后端先停
- **登录凭证**：令牌在 `/login` 用一次，换回来的是**随机会话 cookie**（cookie 里不是令牌本身）；
  「退出登录」只清掉本浏览器这张 cookie —— TG 登录态（存在 session 文件里）和 Gotify token（存在
  `config.json` 里）都不动。要换 WebUI 访问令牌就改 `WEBUI_TOKEN` 后重启 WebUI
- **保存不丢字段**：页面只管它认识的字段，其它顶层字段与手写额外键原样留在 `config.json`；
  写盘是原子的、权限 `600`，写前还会留一份 `config.json.bak`
- 池子空着、却还有频道勾着「走关键词过滤」时，页面直接亮红字提醒（那些频道实际一条都不会推）；
  主程序启动 / 热重载时也会在日志里打一条 warning
- 运行状态卡会用红字/绿字标出后端是谁在管：**systemd 管理**（开机自启、掉线自动拉起）
  还是**网页按钮 / 命令行拉起的**（重启机器不会自己回来，同时直接给出注册自启的命令）

---

## 通知渠道（Gotify）

### 怎么配

1. 打开你的 Gotify 网页（如 `http://192.168.1.100:8080`），用管理员登录
2. 上方菜单 **Apps** → 输入一个应用名 → 点 **CREATE APPLICATION**
3. 复制那串 token（形如 `A1b2c3.xxxxx`），粘到 WebUI「📣 通知渠道」里的「应用 Token」
4. 点 **📤 发一条测试推送** —— 手机上/Gotify 网页上立刻能收到就说明通了
5. 点底部「保存全部」写盘（主程序 5 秒内热重载生效，不用重启）

也可以直接在 `config.json` 里填 `GOTIFY_URL` / `GOTIFY_TOKEN` / `GOTIFY_PRIORITY`，
效果一样（网页只是把它变成可视化操作）。

### 推送优先级（0–10）

| 值 | 表现 |
|---|---|
| 1–3 | 低，普通通知 |
| 4–7 | 中 |
| 8–10 | 高，**会穿透手机免打扰，可能响铃**（抢补货建议 8） |

### 测试推送的报错对照

| 提示 | 说明 |
|---|---|
| `还没填应用 Token` | Gotify 网页 → Apps → CREATE APPLICATION 拿一个 |
| `连不上 Gotify：Connection refused` | 地址 / 端口写错，或者 Gotify 没在跑 |
| `Gotify 返回 HTTP 401` | token 不对（多半是没复制全，或者用了 client token 而非应用 token） |
| `Gotify 返回 HTTP 404` | 地址多半指到了别的服务（Gotify 推送接口是 `/message`） |
| `这个地址回的好像不是 Gotify` | 回了一堆 HTML，说明地址指到别的网站去了 |

### 别的渠道支持吗

**目前只支持 Gotify**（后端就是按 Gotify 的接口写的）。Server酱 / Bark / ntfy / 钉钉 /
企业微信机器人 / 通用 Webhook / Telegram Bot 等都在[路线图](#路线图以后可能会加)里，
有人需要就一个一个加，每个都配自己的「测试推送」按钮。

---

## 功能清单（预期行为，验收对照）

### 监听与推送

| # | 功能 | 预期行为 |
|---|---|---|
| 1 | 用户账号监听 | Telethon 以自己的 TG 账号收消息；session 文件免登录；只处理新消息 |
| 2 | 关键词过滤 | 判定顺序：enabled → 命中（池 OR 私有词，不区分大小写）→ 排除词兜底 |
| 3 | 全量转发 | 频道 `use_pool=false` 时无条件命中（排除词仍生效） |
| 4 | 推送格式 | 标题 `🎯 {label}`；正文 = 原文 + 全部 t.me 链接，**两者合计不超 3500 字**（超了先截断原文并标「…(已截断)」，链接完整保留） |
| 5 | 点击直达 | 通知带 `client::notification.click`，点一下直达 t.me 链接 |
| 6 | 转发原帖解析 | 聚合频道转发的消息，优先解析 `fwd_from` 原帖链接排第一；解析不出退回本帖链接 |
| 7 | 推送可靠性 | 失败重试 3 次（间隔 1s）；成功才标记去重；Gotify 强制直连不走代理 |
| 8 | 消息去重 | `(chat_id, msg_id)` 内存去重，上限 2000 条 |
| 9 | 日志心跳 | 默认 30 分钟一条（`HEARTBEAT_MINUTES` 可改，热重载生效，下限钳到 1） |
| 10 | 启动测试推送 | `SEND_STARTUP_TEST=true` 时启动发一条测试推送 |
| 11 | 状态文件 | 每 60 秒刷新 `run_status.json`（PID / 版本 / 来源数 / 池子词数 / 代理 / 心跳），供 WebUI 判断「在跑」还是「卡住」 |

### 配置与热重载

| # | 功能 | 预期行为 |
|---|---|---|
| 12 | 热重载 | 每 5 秒查 `config.json` mtime，改动生效且 Telethon 不断线（`GOTIFY_*` 也在热重载范围内） |
| 13 | 热重载健壮性 | 坏 JSON / 坏来源值 → 保留旧配置继续跑，5 秒后重试；任务永不崩 |
| 14 | 垃圾数值 | `HEARTBEAT_MINUTES` / `GOTIFY_PRIORITY` / `API_ID` 填垃圾 → 记日志回退默认值 |
| 15 | 启动校验 | 缺 `API_ID`/`API_HASH` 或缺 `GOTIFY_URL`/`GOTIFY_TOKEN` → 明确报错退出，不静默空跑 |
| 16 | 非交互启动 | systemd 下 session 未登录 → 明确报错退出（由 systemd 重试），绝不卡死等输入 |
| 17 | 早期格式配置自动兼容 | 每个频道各存一份 `keywords` 的旧写法 → 共享池 + `extra_keywords`；`keywords` 空 → `use_pool=false` |

### WebUI（可选，默认 8098）

| # | 功能 | 预期行为 |
|---|---|---|
| 18 | 登录认证 | token 登录（`WEBUI_TOKEN`，首启自动生成打印到终端）；登录后发**随机会话 cookie**（不是令牌本身），未登录一律 401/跳登录页 |
| 19 | 运行状态 | 显示 TG 登录态 + 后端进程/心跳（超 180 秒没心跳 = 卡住提示）；折叠卡的摘要行一眼可读 |
| 20 | 启停后端 | 「▶ 启动后端」拉起同目录后端并等它写心跳，成功报 PID / 来源数 / 池子词数；起不来把日志尾部原样返回；已有实例在跑则拒绝 |
| 21 | 出站代理编辑 | 直连 / 走代理 + 类型 / 地址 / 端口，摘要行实时回显；保存后网页登录立即生效 |
| 22 | 通知渠道编辑 | Gotify 地址 / Token（明文开关）/ 优先级；保存写 `GOTIFY_URL`/`GOTIFY_TOKEN`/`GOTIFY_PRIORITY` |
| 23 | 测试推送 | 用**表单当前值**试推（不用先保存），成功 / 401 / 连不上 / 地址指错 / 被重定向分别给人话提示；不泄露 token；不跟随重定向、不被环境变量代理劫持（Gotify 强制直连） |
| 24 | 频道编辑 | 开关频道、切换 `use_pool`、改显示名 / 私有词 / 排除词、删除（二次确认） |
| 25 | 新增频道 | 折叠式入口（淡绿虚线框）；username 自动瘦身（`@`、`t.me` 链接、`tg://resolve?domain=` 都能识别）；新加那行高亮，保存后取消高亮 |
| 26 | 参数说明 | 卡内说明每行参数含义；切到「静默 / 全量转发」时那行显示红字现状 |
| 27 | 老链接键自愈 | 配置里键是 `https://t.me/xxx` 这类写法时，打开页面自动整理成用户名并提示保存 |
| 28 | 保存 | 原子写回 `config.json`（先临时文件再 rename，权限 `600`）；网页不认识的字段（顶层字段、手写额外键）原样保留；保存前自动留 `config.json.bak` |
| 29 | TG 网页登录 | 扫码（二维码自动换新）或手机号验证码（支持两步验证）；**后端在跑时拒绝登录**并说明原因；后台正在探测 session 时也会排队等它结束 |
| 30 | 界面整洁 | 运行状态 / 出站代理 / 通知渠道三块合并成一张卡，各自可折叠，默认收起 |
| 31 | 开机自启提示 | 状态卡标明后端是 systemd 管理还是网页/命令行拉起的；后一种情况直接给出 `systemctl enable --now` 的注册命令 |
| 32 | 空池提醒 | 池子空却有频道走关键词过滤 → 主程序打 warning、配置页亮红字（避免「以为在监控、其实一条不推」） |
| 33 | 凭据文件权限 | 保存 `config.json` 固定 `600`；主程序 / 扫码登录 / 网页登录连接后自动把 `*.session` 也 `chmod 600` |

### 测试覆盖

内部单元测试（匹配逻辑 / 配置加载 / 推送正文预算 / 网页登录状态机 / 通知渠道与保存路径 / 脱敏自检，
共 106 项）与项目源码一起维护，**不随仓库发布**。

---

## 明确不做 / 已知边界

- **不做**抓 VPS 商家官网页面轮询库存：Cloudflare 盾对抗、JS 动态渲染、信息还比 TG 频道滞后，
  TG 频道监听链路更省更准
- **不做**自动列出 / 创建你的 Gotify 应用：那需要管理员 client token（权限更高），
  为省一次复制粘贴不值当
- **不做**消息内容存储 / 历史回溯 / 数据分析：只做转发
- **不做** Windows 适配（仅 Linux）
- 转发原帖是**私有频道**时，Telegram 协议不允许生成外链 → 只能退回聚合帖链接
- 纯图片 / 贴纸等**无文字**消息不推送（补货频道常见的「图 + 文字」没问题，文字部分会被匹配）
- 来源匹配兜底按标题模糊匹配（继承早期自用版），标题撞子串可能误匹配；正常用 username 配置无此问题
- 去重表在内存里，进程重启后不保留（重启期间的消息不会重复推送，因为 Telegram 不重投）
- 代理只在**启动时**读一次：网页改代理后，网页登录立即生效，但**已经在跑的后端要重启**才生效
- 代理只支持「类型 / 地址 / 端口」，**不支持用户名密码认证**（Telethon 支持，本项目没接）
- 网页按钮拉起的后端是脱离终端的进程：**机器重启不会自己回来**，要常驻就用 systemd
  （配置页会直接给出 `sudo cp tg2gotify.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now tg2gotify`）
- 「退出登录」只清浏览器里的登录 cookie：TG 登录态（session 文件）和 Gotify token（config.json）都不动；
  要换 WebUI 访问令牌就改 `WEBUI_TOKEN` 后重启 WebUI
- 池子为空、却有频道勾着「走关键词过滤」时，那些频道一条都不会推（页面和日志会提醒，但不会自动放行）

---

## 路线图（以后可能会加）

按「有人真需要再做」的顺序，每条都是独立小改动：

### 通知渠道（最可能先做）

- **通用 Webhook**：自定义 URL + JSON 模板，一次覆盖一堆自建场景
- **Server酱**（`sctapi.ftqq.com/<key>.send`，title/desp；注意免费版每日条数限制）
- **Bark**（iOS：`api.day.app/<key>/标题/正文`）
- **ntfy**（自建或公共，POST 到 topic）
- **钉钉 / 企业微信机器人**（webhook + JSON；钉钉有关键词或加签要求）
- **Telegram Bot**（Bot API；国内要挂代理）
- **多 token 同时推多渠道**、**每频道指定不同渠道 / 优先级**

> 设计上要注意：不同渠道的「优先级 / 穿透免打扰 / 点击跳转」能力不一样，
> Gotify 的 priority + click 是它的强项，换渠道要么丢要么得映射，文档里得写清。

### WebUI 增强

- 网页里填 `API_ID` / `API_HASH` / `SESSION_NAME`（现在只能手改 `config.json`）
- 网页里开关 `SEND_STARTUP_TEST`、改 `HEARTBEAT_MINUTES`
- 命中历史页（内存留最近 N 条推送记录，不落盘）
- 每频道静默时段（免打扰时间窗）
- 页面本身加 HTTPS / 给 nginx 反代示例

### 运维与部署

- Docker / docker-compose 一键起（主程序 + WebUI）
- 英文 README / 多语言
- 更细的健康检查端点（配合 Uptime Kuma 之类）

### 观察中（还没决定）

- 启动时补抓最近 N 条历史消息（怕重复轰炸，需要去重持久化配合）
- 去重表落盘（重启不丢）

---

## 中国网络环境说明（本项目的核心卖点之一）

| 流量 | 路径 | 原因 |
|---|---|---|
| Telegram（出站） | 本地代理（默认 `socks5://127.0.0.1:7890`） | 国内直连不通；`rdns=True` 让 DNS 解析也走代理，防污染 |
| Gotify（出站） | 强制直连（`proxies=None`） | Gotify 在局域网；若继承代理环境变量会被绕进代理多一跳甚至失败 |

代理是 HTTP 的话把 `PROXY.type` 改成 `"http"`（也支持 `socks4`）。

---

## 常见问题

**Q: 需要机器人 token 吗？**
A: 不需要。用 Telethon 以 **你自己的 TG 账号**监听，只要账号加入了目标频道
（或向目标机器人发过消息）就能收到消息。

**Q: 改了 `config.json` 要重启服务吗？**
A: 不用。热重载每 5 秒检查一次文件改动，改完自动生效（`GOTIFY_*` 也在内）；
只有改 `SESSION_NAME` / 代理 / API 凭据才需要重启（那些是启动时建立的连接参数）。

**Q: 网页上能干什么？会不会乱动我的服务？**
A: 页面轮询状态是只读的；能动的只有三处：保存配置（写 `config.json` 并留备份）、
「启动 / 停止后端」按钮、TG 登录。都要求先通过 token 认证。

**Q: 网页上点「启动后端」起的是哪个后端？**
A: 起的是 **WebUI 同目录那个 `tg2gotify.py`**（网页配置的就是它）。
已经有实例在跑时会拒绝重复启动；如果那实例是 systemd 托管的，停止按钮会走 `systemctl stop`
（服务是 `Restart=always`，直接 kill 会被 systemd 10 秒后拉回来）。

**Q: 推送能直接跳到 TG 消息吗？**
A: 能。每条推送带 `client::notification.click`（Gotify Android ≥ 2.0.10 支持），
点通知直接打开对应 t.me 链接；转发消息会优先解析**原始出处**的链接，
解析不出（私聊 bot、私有源频道）就退回本条消息的链接，正文里也附带全部链接。

**Q: 命中但没收到推送？**
A: ① 在 WebUI「📣 通知渠道」点「发一条测试推送」——报错会直接告诉你原因（token 错 / 连不上）；
② 看日志 `journalctl -u tg2gotify -f`；③ 确认那个频道不是被取消勾选（静默）或被排除词打掉了。

**Q: 为什么我粘了频道链接，界面上显示成一长串？**
A: 已经修好了：新增频道时不管粘 `@xxx`、`t.me/xxx`、`https://t.me/xxx` 还是
`tg://resolve?domain=xxx`，都会自动只留用户名。如果旧配置里存着链接写法，
打开页面会自动整理，点保存就落盘。

**Q: 服务器上跑会有风险吗（封号）？**
A: Telethon 用户账号监听属于正常客户端行为，多年使用普遍稳定；但**不要**把同一个 session
在多处同时运行（本项目的 WebUI 登录守卫就是为了防这个）。

**Q: 页面说「后端进程在，但心跳停了」是什么意思？**
A: 进程还活着，但超过 180 秒没刷新状态文件——多半是卡住或掉线了。用「停止后端 / 启动后端」
重启一次即可（也可以 `systemctl restart tg2gotify`）。

**Q: 网页上点「退出登录」，会把我的 TG 登录一起退掉吗？**
A: 不会。「退出登录」只做两件事：把服务端记住的那个会话丢掉、让浏览器删掉这张 cookie。
TG 登录态存在 session 文件里、Gotify token 存在 `config.json` 里，两者都不受影响。
想换 WebUI 访问令牌，改 `config.json` 的 `WEBUI_TOKEN` 后重启 WebUI 即可。

**Q: 重启服务器后监控怎么没自己起来？**
A: 页面上「启动后端」或命令行拉起的进程是脱离终端的，重启机器不会自己回来。
要开机自启就用 systemd（配置页的运行状态卡里会直接给出命令）：
```bash
sudo cp tg2gotify.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tg2gotify
```
装好之后，页面上的「启动 / 停止后端」就是在操作这个服务。

**Q: 页面提示「池子是空的，但有频道勾着走关键词过滤」怎么办？**
A: 那种频道会一条都不推（池子没词 = 没东西能命中）。要么往池子里加词，
要么把这些频道改成「全量转发」（取消勾选「走关键词过滤」）。

---

## License

MIT，见 [LICENSE](LICENSE)。
