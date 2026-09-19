#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TG2Gotify v2 —— Telegram 频道监控 → Gotify 推送（Linux）
========================================================
以 Telegram 用户账号（Telethon session）监听指定频道/机器人，
按「共享关键词池 + 频道级过滤参数」匹配后推送到 Gotify。

v2 相比 v1 的改进：
  1. 共享关键词池 KEYWORD_POOL：改一次池子，所有走池子的频道同时生效
  2. 频道级参数：enabled / use_pool（全量转发）/ extra_keywords / exclude_keywords
  3. 热重载：后台每 5 秒检查 config.json 的 mtime，改配置 5 秒内生效，
     Telethon 连接不断线（无需重启服务）
  4. 配套 WebUI（webui.py，独立进程）：网页上改配置即保存即生效

首次运行需要登录（扫码见 qr_login.py），之后靠 session 文件免登录。
Telegram 流量走本地代理（默认 127.0.0.1:7890，中国网络环境必需）；
Gotify 在局域网内，强制直连、不走代理。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from collections import deque

import requests
from telethon import TelegramClient, events
from telethon.tl.types import PeerChannel

import filter as flt

# ============================ 配置加载 ============================

_DEFAULT_CONFIG = {
    "API_ID": 0,
    "API_HASH": "",
    "SESSION_NAME": "tg2gotify",
    "PROXY": {"enabled": True, "type": "socks5", "host": "127.0.0.1", "port": 7890},
    "GOTIFY_URL": "",
    "GOTIFY_TOKEN": "",
    "GOTIFY_PRIORITY": 8,
    "SEND_STARTUP_TEST": True,
    "KEYWORD_POOL": [],
    "SOURCES": {},
    "HEARTBEAT_MINUTES": 30,
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("TG2GOTIFY_CONFIG", os.path.join(BASE_DIR, "config.json"))
DEFAULT_SESSION = "tg2gotify"

CONFIG: dict = dict(_DEFAULT_CONFIG)


def load_config(path: str | None = None) -> dict:
    """读取 config.json；缺字段回退默认值。解析失败抛 ValueError（不直接退进程，
    便于热重载时只保留旧配置而不崩服务）。"""
    path = path or CONFIG_PATH
    if not os.path.exists(path):
        raise ValueError(f"配置文件不存在: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            user = json.load(f)
    except Exception as e:
        raise ValueError(f"配置文件 {path} 解析失败: {e}") from e
    cfg = dict(_DEFAULT_CONFIG)
    cfg.update({k: v for k, v in user.items() if k != "PROXY"})
    if isinstance(user.get("PROXY"), dict):
        proxy = dict(_DEFAULT_CONFIG["PROXY"]); proxy.update(user["PROXY"]); cfg["PROXY"] = proxy
    # 数值字段强制 int；转不成（手滑填了垃圾值）就回退默认值，防下游 int()/sleep 崩
    _INT_FIELDS = {"API_ID": 0, "GOTIFY_PRIORITY": 8, "HEARTBEAT_MINUTES": 30}
    for ik, _default in _INT_FIELDS.items():
        if cfg.get(ik) in (None, ""):
            cfg[ik] = _default
            continue
        try:
            cfg[ik] = int(cfg[ik])
        except (TypeError, ValueError):
            log.warning("配置字段 %s=%r 不是合法数字，回退默认值 %s",
                        ik, cfg[ik], _default)
            cfg[ik] = _default
    if not isinstance(cfg.get("KEYWORD_POOL"), list):
        cfg["KEYWORD_POOL"] = flt._as_keyword_list(cfg.get("KEYWORD_POOL"))
    if not isinstance(cfg.get("SOURCES"), dict):
        cfg["SOURCES"] = {}
    # 布尔字段规整（兼容手滑写成字符串 "true"/"false"）
    cfg["SEND_STARTUP_TEST"] = flt.normalize_enabled(cfg.get("SEND_STARTUP_TEST"), True)
    if isinstance(cfg.get("PROXY"), dict):
        cfg["PROXY"]["enabled"] = flt.normalize_enabled(cfg["PROXY"].get("enabled"), True)
    if "KEYWORD_POOL" not in user:
        flt.migrate_pool(cfg)   # v1 配置自动建池
    return cfg


def reload_config() -> bool:
    """重新读取配置并**原地**替换 CONFIG 内容（引用不变，事件回调无需重绑）。
    成功返回 True；失败（解析错误等）保留旧配置返回 False。"""
    global CONFIG
    try:
        new_cfg = load_config()
    except ValueError as e:
        log.warning("配置重载失败，保留旧配置: %s", e)
        return False
    CONFIG.clear()
    CONFIG.update(new_cfg)
    return True


def proxy_dict() -> dict | None:
    """把 PROXY 配置转成 Telethon(python-socks) 的代理字典；未启用返回 None。
    rdns=True = 域名解析也走代理，防 DNS 污染。"""
    if not CONFIG["PROXY"].get("enabled"):
        return None
    p = CONFIG["PROXY"]
    return {"proxy_type": p["type"], "addr": p["host"], "port": p["port"], "rdns": True}


def session_path(cfg: dict | None = None) -> str:
    """session 文件路径：**相对名落在脚本目录**、绝对名原样。
    与 webui.py / tglogin.py 用同一套规则 —— 否则手工在别的目录启动时，
    主程序和网页端认的会是两个不同的 session 文件。"""
    cfg = CONFIG if cfg is None else cfg
    name = str(cfg.get("SESSION_NAME") or DEFAULT_SESSION).strip() or DEFAULT_SESSION
    if not os.path.isabs(name):
        name = os.path.join(BASE_DIR, name)
    return name + ".session"


def chmod_private(path: str) -> None:
    """把含凭据的文件权限收紧到 600（session 文件等同于账号凭证）。"""
    try:
        if os.path.exists(path):
            os.chmod(path, 0o600)
    except OSError as e:
        log.debug("收紧 %s 权限失败（不影响运行）: %s", path, e)


PUSH_BODY_LIMIT = 3500          # 推送正文总长上限：**原文 + 链接一起算**
TRUNCATE_MARK = "\n…(已截断)"


def build_push_body(text: str, links: list[str] | None = None,
                    limit: int = PUSH_BODY_LIMIT) -> str:
    """拼推送正文，链接也算进 limit 预算里（总长不超 limit）。
    先给链接留位置，剩下的预算给原文；原文放不下就截断并加标记。
    极端情况（链接自己就快占满预算）优先保链接，原文整段丢掉。"""
    body = (text or "").strip()
    link_block = ""
    if links:
        link_block = "\n\n🔗 " + "\n🔗 ".join(links)
        if len(link_block) >= limit:
            return link_block[:limit]
    budget = limit - len(link_block)
    if len(body) > budget:
        body = body[:budget - len(TRUNCATE_MARK)] + TRUNCATE_MARK if budget > len(TRUNCATE_MARK) else ""
    return body + link_block


def warn_if_pool_empty() -> None:
    """池子为空却有来源勾着「走关键词过滤」→ 那些来源一条都不会推，启动/热重载时提醒一句。"""
    if not flt._as_keyword_list(CONFIG.get("KEYWORD_POOL")):
        n_use = sum(1 for s in CONFIG["SOURCES"].values()
                    if flt.normalize_source(s)["enabled"] and flt.normalize_source(s)["use_pool"])
        if n_use:
            log.warning("⚠️ 共享关键词池是空的，但有 %d 个来源勾着「走关键词过滤」——"
                        "它们一条消息都不会被推送；往池子里加词，或把这些来源改成全量转发。", n_use)


# ================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%m-%d %H:%M:%S",
)
log = logging.getLogger("tg2gotify")

# 消息去重（防重发，重启后 Telethon 本来就只投递新消息）
_seen_ids: deque = deque(maxlen=2000)

# ============================ 运行状态文件（给 WebUI 看） ============================
# WebUI 首页要回答「后端到底跑没跑、连上没有」。只看进程只能知道「有进程」，
# 看不出掉线/卡死；所以主程序自己写一份心跳状态文件，WebUI 读它。
STATUS_PATH = os.environ.get(
    "TG2GOTIFY_STATUS",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_status.json"),
)
APP_VERSION = "2.0"
HEARTBEAT_WRITE_SECONDS = 60     # 状态文件多久刷一次（热重载循环里偷跑）
_STARTED_AT = time.time()
ME_NAME = ""                    # 登录后填账号名
LAST_HIT_AT = 0.0                # 最近一次命中并成功推送的时间


def write_status() -> None:
    """原子写运行状态文件（WebUI 读；写失败不影响监听）。"""
    n_enabled = sum(1 for s in CONFIG["SOURCES"].values()
                    if flt.normalize_source(s)["enabled"])
    data = {
        "app": "tg2gotify",
        "version": APP_VERSION,
        "pid": os.getpid(),
        "started_at": _STARTED_AT,
        "updated_at": time.time(),
        "user": ME_NAME,
        "sources": len(CONFIG["SOURCES"]),
        "sources_enabled": n_enabled,
        "pool": len(CONFIG["KEYWORD_POOL"]),
        "last_hit_at": LAST_HIT_AT,
        "session": os.path.basename(session_path()),
        "proxy": ("%s://%s:%s" % (CONFIG["PROXY"]["type"], CONFIG["PROXY"]["host"],
                                   CONFIG["PROXY"]["port"]))
                 if CONFIG["PROXY"].get("enabled") else "直连",
        "config": os.path.basename(CONFIG_PATH),
    }
    try:
        tmp = STATUS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATUS_PATH)
    except OSError as e:
        log.debug("写运行状态文件失败（不影响监听）: %s", e)


def gotify_push(title: str, message: str, priority: int | None = None,
                click_url: str | None = None) -> bool:
    """推送到 Gotify（同步，含重试）。在线程里调用，避免阻塞事件循环。
    GOTIFY_URL 是局域网地址，强制不走代理（proxies=None），否则会绕进代理多一跳。
    click_url: 点击通知直接打开的链接（官方 client::notification.click，
    gotify/android ≥2.0.10 支持）—— 解决「推送里的 t.me 链接要点进去再找」的问题。
    最多 3 次，间隔 1s。"""
    url = f"{CONFIG['GOTIFY_URL'].rstrip('/')}/message"
    payload = {
        "title": title,
        "message": message,
        "priority": priority if priority is not None else CONFIG["GOTIFY_PRIORITY"],
    }
    if click_url:
        payload["extras"] = {"client::notification": {"click": {"url": click_url}}}
    # 显式禁用代理：为连 TG 设置的 HTTP_PROXY/ALL_PROXY 环境变量会被 requests
    # 继承，导致对局域网 Gotify 的推送被绕进代理。这里强制直连。
    _no_proxy = {"http": None, "https": None, "all": None}
    last_err = None
    for attempt in range(1, 4):
        try:
            r = requests.post(url, params={"token": CONFIG["GOTIFY_TOKEN"]}, json=payload,
                              timeout=5, proxies=_no_proxy)
            r.raise_for_status()
            log.info("Gotify 推送成功: %s (第%d次)", title, attempt)
            return True
        except Exception as e:
            last_err = e
            log.warning("Gotify 推送失败(第%d次): %s", attempt, e)
            if attempt < 3:
                time.sleep(1)
    log.error("Gotify 推送最终失败: %s | %s", title, last_err)
    return False


def match_source(chat) -> str | None:
    """把消息来源匹配到 CONFIG['SOURCES'] 的键上。"""
    uname = (getattr(chat, "username", None) or "").lower()
    if uname and uname in CONFIG["SOURCES"]:
        return uname
    # 兜底：来源没配 username（或改过名）时按标题模糊匹配（键统一小写比较）
    title = (getattr(chat, "title", None) or getattr(chat, "first_name", None) or "").lower()
    for key in CONFIG["SOURCES"]:
        if key.lower() in title:
            return key
    return None


# ============================ 热重载 ============================

RELOAD_INTERVAL = 5  # 秒


async def hot_reload_task():
    """后台任务：每 5 秒检查 config.json 的 mtime，变化即重载配置。
    Telethon 连接不受影响（连接参数只在启动时使用）。"""
    last_mtime = 0.0
    try:
        last_mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        pass
    ticks = 0
    while True:
        await asyncio.sleep(RELOAD_INTERVAL)
        ticks += 1
        if ticks * RELOAD_INTERVAL >= HEARTBEAT_WRITE_SECONDS:   # 心跳：刷状态文件
            ticks = 0
            write_status()
        try:
            m = os.path.getmtime(CONFIG_PATH)
        except OSError:
            continue
        if m == last_mtime:
            continue
        try:
            ok = reload_config()
        except Exception:
            log.exception("配置热重载出错（保留旧配置，任务继续）")
            ok = False
        if ok:
            last_mtime = m
            n_pool_on = sum(1 for s in CONFIG["SOURCES"].values()
                            if flt.normalize_source(s)["use_pool"])
            n_enabled = sum(1 for s in CONFIG["SOURCES"].values()
                            if flt.normalize_source(s)["enabled"])
            log.info("🔥 配置已热重载: 池子 %d 词 | 来源 %d 个（启用 %d / 走池过滤 %d）",
                     len(CONFIG["KEYWORD_POOL"]), len(CONFIG["SOURCES"]),
                     n_enabled, n_pool_on)
            warn_if_pool_empty()
        # 解析失败时保留旧配置，下轮 mtime 变了再试


# ============================ 主程序 ============================

async def build_tg_links(client, chat, message) -> list[str]:
    """收集消息的 t.me 跳转链接（去重，按优先级排）：
    ① 转发消息的**原始出处** —— v1 的隐形坑：聚合频道转发的消息，
       推送里只带聚合频道的链接，原文要进 TG 重新找；这里直接解析出原帖链接。
    ② 本条消息所在公开频道的链接。
    解析不出（私聊 bot、私有源频道、普通用户转发）就跳过该项，不影响推送。"""
    links: list[str] = []
    fwd = getattr(message, "fwd_from", None)
    if fwd is not None:
        try:
            if getattr(fwd, "channel_id", None) and getattr(fwd, "channel_post", None):
                orig = await client.get_entity(PeerChannel(fwd.channel_id))
                orig_uname = getattr(orig, "username", None)
                if orig_uname:
                    links.append(f"https://t.me/{orig_uname}/{fwd.channel_post}")
        except Exception as e:
            log.debug("解析转发出处失败（忽略，退回本条消息链接）: %s", e)
    uname = getattr(chat, "username", None)
    if uname:
        links.append(f"https://t.me/{uname}/{getattr(message, 'id', 0)}")
    return list(dict.fromkeys(links))  # 去重保序


async def _handle_message(event):
    try:
        chat = await event.get_chat()
        key = match_source(chat)
        if not key:
            return
        src_raw = CONFIG["SOURCES"].get(key)
        if src_raw is None:
            return

        text = (event.message.message or "").strip()
        if not text:
            # 纯图片/贴纸等无文字消息（「图+文字」的 caption 会出现在 message 里，不受影响）
            log.debug("[%s] 跳过无文字的纯媒体消息", key)
            return

        msg_key = (getattr(chat, "id", 0), event.message.id)
        if msg_key in _seen_ids:
            return

        hit, why = flt.match_message(text, src_raw, CONFIG["KEYWORD_POOL"])
        if not hit:
            if why in ("excluded", "disabled"):
                log.info("[%s] 消息被丢弃（%s）: %s", key, why,
                         text[:60].replace("\n", " "))
            else:
                log.info("[%s] 消息未命中关键词，跳过: %s", key,
                         text[:60].replace("\n", " "))
            return

        label = flt.normalize_source(src_raw)["label"] or key
        links = await build_tg_links(event.client, chat, event.message)
        body = build_push_body(text, links)          # 链接也占预算，总长不超 3500

        log.info("命中 [%s]（%s）→ 推送: %s", label, why, text[:80].replace("\n", " "))
        # 在线程里推送，不阻塞事件循环；成功后才标记去重（避免瞬时失败丢告警）
        # 点击通知直达 links[0]（转发消息 = 原帖，普通消息 = 本帖）
        ok = await asyncio.to_thread(gotify_push, f"🎯 {label}", body, None,
                                     links[0] if links else None)
        if ok:
            global LAST_HIT_AT
            LAST_HIT_AT = time.time()
            _seen_ids.append(msg_key)
        else:
            log.error("[%s] 推送失败，本条未标记去重（但 Telegram 不重投，需排查 Gotify）", label)
    except Exception as e:
        log.exception("处理消息时出错: %s", e)


def main():
    if not CONFIG["API_ID"] or not CONFIG["API_HASH"]:
        print("[!!] 请先在 config.json 里填 API_ID / API_HASH（my.telegram.org 申请），见 README。")
        sys.exit(1)
    if (not str(CONFIG.get("GOTIFY_URL") or "").strip()
            or not str(CONFIG.get("GOTIFY_TOKEN") or "").strip()):
        print("[!!] 请先在 config.json 里填 GOTIFY_URL / GOTIFY_TOKEN"
              "（Gotify 网页端 Apps → Create application 创建），见 README。")
        sys.exit(1)

    proxy = proxy_dict()
    if proxy:
        log.info("出站代理: %s://%s:%s (rdns)", CONFIG["PROXY"]["type"],
                 CONFIG["PROXY"]["host"], CONFIG["PROXY"]["port"])

    client = TelegramClient(session_path(), CONFIG["API_ID"], CONFIG["API_HASH"], proxy=proxy)

    @client.on(events.NewMessage(incoming=True))
    async def on_new_message(event):
        await _handle_message(event)

    async def heartbeat():
        while True:
            # 下限 1 分钟：HEARTBEAT_MINUTES 填 0/负数会让 sleep(0) 变成死循环空转烧 CPU；
            # 每轮重读 CONFIG，改心跳间隔热重载也能生效
            interval_min = max(1, int(CONFIG["HEARTBEAT_MINUTES"] or 1))
            await asyncio.sleep(interval_min * 60)
            enabled = sum(1 for s in CONFIG["SOURCES"].values()
                          if flt.normalize_source(s)["enabled"])
            log.info("心跳: 运行中，监听 %d 个来源（已启用），池子 %d 词",
                     enabled, len(CONFIG["KEYWORD_POOL"]))

    client.loop.create_task(heartbeat())
    client.loop.create_task(hot_reload_task())

    log.info("正在连接 Telegram …")
    try:
        # connect/is_user_authorized/disconnect 都是协程，必须真正 await（v2 初版漏了，
        # 导致连接从未建立、run_until_disconnected 报 "Cannot send requests while disconnected"）
        client.loop.run_until_complete(client.connect())
    except Exception as e:
        log.error("[!!] 连接 Telegram 失败: %s（查代理是否活着: %s）", e,
                  f"{CONFIG['PROXY']['host']}:{CONFIG['PROXY']['port']}" if proxy else "未配置代理")
        sys.exit(1)
    if not client.loop.run_until_complete(client.is_user_authorized()):
        # 故意不交互：systemd 下没有 tty，client.start() 会卡在「等输入验证码」永远挂着
        log.error("[!!] session 未登录。先运行: python qr_login.py 扫码登录，再启动服务")
        client.loop.run_until_complete(client.disconnect())
        sys.exit(1)
    log.info("已登录")
    chmod_private(session_path())                    # session 文件含账号凭据 → 600
    chmod_private(session_path() + "-journal")
    # 记账号名并写一份运行状态文件：WebUI 首页靠它显示「后端跑没跑、跑的是谁、心跳」
    global ME_NAME
    try:
        me = client.loop.run_until_complete(client.get_me())
        uname = getattr(me, "username", None)
        uid = getattr(me, "id", None)
        ME_NAME = ("@%s" % uname) if uname else (getattr(me, "first_name", None) or "")
        if uid:
            ME_NAME = "%s (id %s)" % (ME_NAME, uid) if ME_NAME else "id %s" % uid
    except Exception as e:
        log.warning("取账号信息失败（不影响监听）: %s", e)
    write_status()

    if CONFIG["SEND_STARTUP_TEST"]:
        src_list = "\n".join(
            f"  • @{k}（{flt.normalize_source(v)['label'] or k}）"
            f"{' [已禁用]' if not flt.normalize_source(v)['enabled'] else ''}"
            f"{' [全量转发]' if not flt.normalize_source(v)['use_pool'] else ''}"
            for k, v in CONFIG["SOURCES"].items()
        )
        gotify_push(
            "✅ TG 监控已启动 (v2)",
            f"监听来源:\n{src_list}\n\n关键词池 {len(CONFIG['KEYWORD_POOL'])} 词\n"
            f"出站代理: {CONFIG['PROXY']['host']}:{CONFIG['PROXY']['port']}\n"
            f"配置热重载: 已开启（{RELOAD_INTERVAL}s 检查一次）",
            priority=5,
        )
    log.info("开始监听 … (Ctrl+C 退出)")
    client.run_until_disconnected()


if __name__ == "__main__":
    try:
        CONFIG.update(load_config())
    except ValueError as e:
        print(f"[!!] {e}")
        print("     新部署请先: cp config.example.json config.json 并填好 API_ID / API_HASH")
        sys.exit(1)
    warn_if_pool_empty()
    main()
