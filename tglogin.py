#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tglogin.py —— TG2Gotify 网页端 Telegram 登录（扫码 / 验证码 两种方式）
======================================================================
给 WebUI（webui.py）用的小模块：把 Telethon 的登录搬进浏览器，
以后 session 失效、被踢或新部署时，不用 SSH 进终端跑 qr_login.py，
网页上扫码或填验证码即可（这两点在中国网络环境下很实用）。

设计要点：
  • 单文件独立模块；webui.py 只加几条路由，主程序 tg2gotify.py 一行不动。
  • Telethon 是异步的：本模块开一个**后台线程跑自己的 asyncio 事件循环**，
    HTTP 侧通过线程安全队列投递指令、通过锁保护的快照读状态（状态机）。
  • 两种方式同时只跑一个流程；发起新流程会自动清掉旧的。
  • 登录成功 / 取消后**立刻断开连接释放 session 文件**；另外 10 分钟无结果
    自动取消（防止点了登录又关页面，把 session 文件一直占着让主程序起不来）。
  • 主程序（tg2gotify.py）在跑时**一律拒绝**发起登录：同一个 session 不能两处
    同时用（Telegram 会互相踢 + SQLite 文件锁冲突），页面会明说原因。

依赖：telethon（主程序必需）+ qrcode（仅扫码需要，requirements.txt 已含；
      没装也能用验证码方式，页面会给出提示）。
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("TG2GOTIFY_CONFIG", os.path.join(BASE_DIR, "config.json"))

IDLE_TIMEOUT = 600          # 单个登录流程最长存活秒数，到点自动取消释放 session
DEFAULT_SESSION = "tg2gotify"
DEFAULT_PROXY = {"enabled": True, "type": "socks5", "host": "127.0.0.1", "port": 7890}

ACTIVE_STATES = ("connecting", "qr_pending", "code_sent", "password_needed")

log = logging.getLogger("tglogin")


def mask_phone(phone: str) -> str:
    """日志里不出现完整手机号。"""
    p = str(phone or "")
    return (p[:4] + "****" + p[-2:]) if len(p) > 8 else "****"


def chmod_private(path: str) -> None:
    """session 文件等同于账号凭据 → 权限收紧到 600。"""
    try:
        if os.path.exists(path):
            os.chmod(path, 0o600)
    except OSError as e:
        log.debug("[登录] 收紧 %s 权限失败（不影响功能）：%s", path, e)


# ============================ 配置 / 环境 ============================

def read_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def session_path(cfg: dict) -> str:
    """与主程序一致：SESSION_NAME 是相对名时落在本目录（主程序用 CWD）。"""
    name = str(cfg.get("SESSION_NAME") or DEFAULT_SESSION).strip() or DEFAULT_SESSION
    if not os.path.isabs(name):
        name = os.path.join(BASE_DIR, name)
    return name + ".session"


def proxy_arg(cfg: dict) -> dict | None:
    p = cfg.get("PROXY")
    p = dict(DEFAULT_PROXY, **(p if isinstance(p, dict) else {}))
    if not p.get("enabled"):
        return None
    return {"proxy_type": p.get("type", "socks5"), "addr": p.get("host", "127.0.0.1"),
            "port": int(p.get("port", 7890)), "rdns": True}


def main_program_pids() -> list[int]:
    """tg2gotify.py 主程序（或别的实例）的 PID 列表 —— 扫 /proc 判断，
    不依赖 ps/pgrep（轻量环境里不一定有）。"""
    me = os.getpid()
    found: list[int] = []
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return []
    for pid in pids:
        if int(pid) == me:
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                raw = f.read().decode("utf-8", "ignore")
        except OSError:
            continue
        parts = [p for p in raw.split("\0") if p]
        for i, part in enumerate(parts):
            # 只认「python … tg2gotify.py」这种真跑起来的进程：光看进程名会误伤
            # 命令行里恰好提到 tg2gotify.py 的其它进程（比如排查用的 pgrep/shell）
            if not part.endswith("tg2gotify.py"):
                continue
            if any(os.path.basename(p).startswith("python") for p in parts[:i]):
                found.append(int(pid))
                break
    return found


def main_program_running() -> bool:
    return bool(main_program_pids())


def qr_svg(url: str) -> str:
    """把 tg://login?token=… 画成内联 SVG（qrcode 纯 Python，不需要 PIL）。"""
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage,
                      box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode("utf-8")
    svg = svg.replace("<?xml version='1.0' encoding='UTF-8'?>", "").strip()
    # 固定宽高会盖住响应式样式，改成 data-* 交给 CSS 控制
    svg = svg.replace('width="', 'data-w="').replace('height="', 'data-h="')
    return svg


# ============================ 登录状态机 ============================

class TGLogin:
    """后台线程 + asyncio 事件循环里的登录流程；对外只暴露线程安全快照与指令投递。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        # 状态字段（全部走 self._lock 读写）
        self._state = "idle"      # idle|connecting|qr_pending|code_sent|password_needed|success|error|cancelled
        self._message = ""
        self._detail = ""
        self._qr = ""
        self._qr_url = ""
        self._qr_expires = 0.0
        self._phone = ""
        self._user = ""
        self._session_note = ""     # session 状态提示（如「已被 Telegram 注销」）
        self._active_since = 0.0
        self._busy = False
        # 仅后台线程内使用
        self._client = None
        self._task = None
        self._phone_code_hash = None

    # ---------- 线程安全读写 ----------
    def _set(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, "_" + k, v)

    def _snapshot(self) -> dict:
        with self._lock:
            st = {
                "state": self._state, "message": self._message, "detail": self._detail,
                "qr": self._qr, "qr_url": self._qr_url,
                "qr_expires_in": max(0, int(self._qr_expires - time.time())) if self._qr else 0,
                "phone": self._phone, "user": self._user, "busy": self._busy,
                "session_note": self._session_note,
            }
        st["main_running"] = main_program_running()
        try:
            cfg = read_config()
            sp = session_path(cfg)
            p = proxy_arg(cfg)
            st.update({
                "config_ok": True,
                "session_name": os.path.basename(sp),
                "session_exists": os.path.exists(sp),
                "proxy": ("%s://%s:%s" % (p["proxy_type"], p["addr"], p["port"])) if p else "未启用（直连）",
            })
        except Exception as e:      # 配置坏了页面也得能打开看提示
            st.update({"config_ok": False, "session_name": "?", "session_exists": False,
                       "proxy": "config.json 读取失败: %s" % e})
        return st

    def status(self) -> dict:
        return self._snapshot()

    def state_name(self) -> str:
        with self._lock:
            return self._state

    # ---------- 指令投递 ----------
    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._thread_main, name="tg-login", daemon=True)
            self._thread.start()

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._async_main())
        except Exception as e:                      # 后台线程整体挂掉的兜底
            log.error("[登录] 后台线程异常退出：%s: %s", type(e).__name__, e)
            self._set(state="error", message="登录后台线程异常退出",
                      detail="%s: %s" % (type(e).__name__, e), busy=False)

    def _err(self, text: str) -> dict:
        self._set(busy=False, detail=text)
        return self._snapshot()

    def _submit(self, action: str, **kw) -> dict:
        blocked = self._guard()
        if blocked:
            return blocked
        what = {"start_qr": "扫码", "start_phone": "验证码", "submit_code": "提交验证码",
                "submit_password": "提交两步验证密码", "cancel": "取消"}.get(action, action)
        log.info("[登录] 收到指令：%s%s", what, "（强制重登）" if kw.get("force") else "")
        self._set(busy=True, state="connecting", message="处理中 …", detail="")
        self._ensure_thread()
        self._q.put((action, kw))
        return self._snapshot()

    # ---------- 对外动作 ----------
    def start_qr(self, force: bool = False) -> dict:
        return self._submit("start_qr", force=bool(force))

    def start_phone(self, phone: str, force: bool = False) -> dict:
        phone = (phone or "").strip().replace(" ", "").replace("-", "")
        if not phone:
            return self._err("请填写手机号（国际格式，如 +8613800138000）")
        if not phone.startswith("+"):
            return self._err("手机号要带国家码，例如 +8613800138000")
        if not phone[1:].isdigit() or len(phone) < 8:
            return self._err("手机号格式看着不对，例如 +8613800138000")
        return self._submit("start_phone", phone=phone, force=bool(force))

    def submit_code(self, code: str) -> dict:
        code = (code or "").strip().replace(" ", "").replace("-", "")
        if not code:
            return self._err("请填写收到的验证码")
        return self._submit("submit_code", code=code)

    def submit_password(self, password: str) -> dict:
        if not (password or "").strip():
            return self._err("请填写两步验证密码")
        return self._submit("submit_password", password=password)

    def cancel(self) -> dict:
        """取消登录：**不走 _guard** —— 它只负责释放资源（断连接、放掉 session 文件），
        绝不碰 Telegram；主程序在跑时更要保证随时能取消。"""
        if not (self._thread and self._thread.is_alive()):
            self._set(state="cancelled", message="已取消登录",
                      detail="没有进行中的登录流程，没有占用 session 文件。", busy=False,
                      qr="", qr_url="", phone="")
            return self._snapshot()
        self._set(busy=True)
        self._q.put(("cancel", {}))
        return self._snapshot()

    # ---------- 拦截 ----------
    def _guard(self) -> dict | None:
        """主程序在跑 / 配置不可用时拒绝发起登录。"""
        if main_program_running():
            log.warning("[登录] 拒绝：主程序 tg2gotify.py 正在运行（session 不能两处同时用）")
            self._set(state="error", message="主程序正在运行，已拒绝登录",
                      detail="同一个 Telegram session 不能两处同时使用（会互相踢下线）。"
                             "请先停止主程序：systemctl stop tg2gotify（手工跑的按 Ctrl+C），"
                             "再回来登录；登录成功后再把主程序启动起来。", busy=False)
            return self._snapshot()
        with _state_lock:
            probing = bool(_state_cache.get("probing"))
        if probing:
            log.info("[登录] 稍等：后台正在探测 session 状态（同一个 session 文件不两处用）")
            self._set(state="error", message="正在检查 session 状态，请等几秒再试",
                      detail="配置页每隔几分钟会真连一次 Telegram 看 session 还有效没；"
                             "这会儿发起登录会跟它抢同一个 session 文件。几秒后重试即可。",
                      busy=False)
            return self._snapshot()
        try:
            cfg = read_config()
        except Exception as e:
            self._set(state="error", message="读不到 config.json",
                      detail="%s（先修好配置再登录）" % e, busy=False)
            return self._snapshot()
        api_id = str(cfg.get("API_ID") or "").strip()
        api_hash = str(cfg.get("API_HASH") or "").strip()
        if api_id in ("", "0") or not api_hash or "在此" in api_hash:
            self._set(state="error", message="config.json 里缺 API_ID / API_HASH",
                      detail="先去 my.telegram.org 申请 API development tools，填好再登录。",
                      busy=False)
            return self._snapshot()
        return None

    # ---------- 后台事件循环 ----------
    async def _async_main(self) -> None:
        asyncio.create_task(self._watchdog_loop())
        while True:
            action, kw = await asyncio.to_thread(self._q.get)
            if action == "stop":
                break
            try:
                await self._dispatch(action, kw)
            except Exception as e:
                log.error("[登录] 流程出错：%s: %s", type(e).__name__, e)
                await self._teardown()
                self._set(state="error", message="登录流程出错",
                          detail="%s: %s" % (type(e).__name__, e), busy=False)

    async def _watchdog_loop(self) -> None:
        """空闲超时兜底：流程开着没人管时自动取消，释放 session 文件。"""
        while True:
            await asyncio.sleep(10)
            with self._lock:
                active = self._state in ACTIVE_STATES
                since = self._active_since
            if active and since and (time.time() - since) > IDLE_TIMEOUT:
                log.info("[登录] 超时 %d 分钟未完成，自动取消并释放 session", IDLE_TIMEOUT // 60)
                await self._teardown()
                self._set(state="cancelled", message="登录超时，已自动取消",
                          detail="%d 分钟没完成登录，session 文件已释放；需要就重新发起。"
                                 % (IDLE_TIMEOUT // 60), busy=False,
                          qr="", qr_url="", phone="")

    async def _dispatch(self, action: str, kw: dict) -> None:
        if action in ("start_qr", "start_phone"):
            blocked = self._guard()
            if blocked:
                return
            await self._teardown()          # 先清干净上一次流程（含连接与 session 占用）
            await self._connect(read_config())
            with self._lock:
                self._active_since = time.time()
            # 先看这个 session 到底什么状态：
            #   已登录  → 什么都不用做（除非显式 force）
            #   被注销  → 提示一下，直接走重新登录（Telegram 允许在这种 key 上重新登录）
            #   其它错  → 当未登录处理，真错误交给后面的流程报
            from telethon import functions
            from telethon.errors import AuthKeyUnregisteredError
            authorized, dead = False, False
            try:
                await self._client(functions.updates.GetStateRequest())
                authorized = True
            except AuthKeyUnregisteredError:
                dead = True
            except Exception:
                pass
            log.info("[登录] session 状态：%s",
                     "已登录（无需重登）" if authorized else
                     ("已被 Telegram 注销，需重新登录" if dead else "未登录"))
            self._set(session_note=(
                "当前 session 已被 Telegram 注销（AUTH_KEY_UNREGISTERED），重新登录一次即可。"
                if dead else ""))
            if authorized and not kw.get("force"):
                me = await self._client.get_me()
                await self._finish_success(me, already=True)
                return

        if action == "start_qr":
            self._set(state="connecting", message="正在申请二维码 …", detail="", busy=False)
            self._task = asyncio.create_task(self._qr_loop())

        elif action == "start_phone":
            from telethon.errors import (FloodWaitError, PhoneNumberInvalidError,
                                         PhoneNumberBannedError)
            phone = kw.get("phone", "")
            try:
                sent = await self._client.send_code_request(phone)
            except FloodWaitError as e:
                await self._teardown()
                self._set(state="error", message="Telegram 限流中",
                          detail="重试太频繁会被风控，等 %d 秒再试。" % e.seconds, busy=False)
                return
            except PhoneNumberInvalidError:
                await self._teardown()
                self._set(state="error", message="手机号无效",
                          detail="确认带上国家码（例如 +8613800138000）。", busy=False)
                return
            except PhoneNumberBannedError:
                await self._teardown()
                self._set(state="error", message="该号码已被 Telegram 封禁",
                          detail="换一个 Telegram 账号，或改用扫码登录。", busy=False)
                return
            self._phone_code_hash = getattr(sent, "phone_code_hash", None)
            log.info("[登录] 验证码已发送到 %s（等接验证码；type=%s）",
                     mask_phone(phone), getattr(getattr(sent, "type", None), "__class__", type(None)).__name__)
            self._set(state="code_sent", phone=phone, busy=False, message="验证码已发送",
                      detail="Telegram 会把验证码发到你手机的 TG（已登录设备，通常比短信快）；"
                             "收到后填到下面。开了两步验证的话，下一步会再要一次密码。")

        elif action == "submit_code":
            await self._do_sign_in_code(kw.get("code", ""))

        elif action == "submit_password":
            await self._do_sign_in_password(kw.get("password", ""))

        elif action == "cancel":
            log.info("[登录] 已按取消，断开连接、释放 session 文件")
            await self._teardown()
            self._set(state="cancelled", message="已取消登录",
                      detail="session 文件已释放，可以启动主程序了。", busy=False,
                      qr="", qr_url="", phone="")

        else:
            self._set(busy=False, detail="未知指令: %s" % action)

    # ---------- 连接 / 断开 ----------
    async def _connect(self, cfg: dict) -> None:
        from telethon import TelegramClient
        self._client = TelegramClient(session_path(cfg), int(cfg["API_ID"]),
                                      cfg["API_HASH"], proxy=proxy_arg(cfg))
        await self._client.connect()
        chmod_private(session_path(cfg))
        chmod_private(session_path(cfg) + "-journal")
        p = proxy_arg(cfg)
        log.info("[登录] 已连上 Telegram（%s，session=%s）",
                 "%s://%s:%s" % (p["proxy_type"], p["addr"], p["port"]) if p else "直连",
                 os.path.basename(session_path(cfg)))

    async def _ensure_connected(self):
        """Telethon 在遇到 Telegram 内部错误（如 AuthRestartError）时会自行断开重连，
        中间窗口里发请求会报 “Cannot send requests while disconnected”——
        这里兜一下：没连着就重连，连不上就抛 ConnectionError 交给调用方提示。"""
        client = self._client
        if client is None:
            raise ConnectionError("登录流程已失效（请重新发起登录）")
        if not client.is_connected():
            await client.connect()
            if not client.is_connected():
                raise ConnectionError("连不上 Telegram（检查代理是否活着）")
        return client

    @staticmethod
    def _transient(e: Exception) -> bool:
        """瞬时错误（重试可能就好）vs 真错误（重试也没用）。"""
        try:
            from telethon.errors import AuthRestartError
        except Exception:
            AuthRestartError = ()          # type: ignore
        return isinstance(e, (ConnectionError, OSError, asyncio.TimeoutError, AuthRestartError))

    async def _teardown(self) -> None:
        """停掉在跑的登录任务、断开连接（释放 session 文件）。"""
        task = self._task
        self._task = None
        # 扫码成功那条路是在 _qr_loop 任务**自己内部**调到这里来的：不能 cancel 自己，跳过即可
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        with self._lock:
            self._active_since = 0.0

    async def _finish_success(self, me, already: bool = False) -> None:
        """登录成功：取账号名 → 断开（释放 session）→ 报成功。
        already=True 表示本来就已经登录着（什么都没做）。"""
        name = ""
        try:
            uname = getattr(me, "username", None)
            uid = getattr(me, "id", None)
            name = ("@%s" % uname) if uname else (getattr(me, "first_name", None) or "")
            if uid:
                name = "%s (id %s)" % (name, uid) if name else "id %s" % uid
        except Exception:
            pass
        await self._teardown()
        if already:
            log.info("[登录] 结束：session 本来就可登录，什么都没改")
        else:
            log.info("[登录] ✅ 登录成功：%s", name or "(未取名)")
        self._set(state="success", user=name or "已登录", busy=False,
                  message="session 已经是登录状态（无需重新登录）" if already else "登录成功 🎉",
                  detail=("当前 session 可用，什么都没动。真的要重新登录，"
                          "勾上「强制重新登录」再点按钮。" if already else
                          "session 文件已写入磁盘并释放占用。下一步启动主程序："
                          "systemctl start tg2gotify（手工跑的 ./venv/bin/python tg2gotify.py）。"),
                  qr="", qr_url="", phone="")

    # ---------- 扫码 ----------
    async def _qr_loop(self) -> None:
        from telethon.errors import SessionPasswordNeededError
        tries = 0
        while True:
            try:
                client = await self._ensure_connected()
                qr = await client.qr_login()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Telegram 偶尔自己抽风（AuthRestartError 等），重试几次通常就好；
                # 一上来就报错让用户干瞪眼是最差的体验
                if self._transient(e) and tries < 3:
                    tries += 1
                    self._set(state="connecting", busy=False,
                              message="Telegram 临时抽风，正在重试 …（第 %d 次）" % tries,
                              detail="%s: %s" % (type(e).__name__, e))
                    await asyncio.sleep(2)
                    continue
                await self._teardown()
                self._set(state="error", message="申请二维码失败",
                          detail="%s: %s（查一下代理是否活着）" % (type(e).__name__, e), busy=False)
                return
            break

        while True:
            try:
                svg = qr_svg(qr.url)
            except ImportError:
                await self._teardown()
                self._set(state="error", message="缺少 qrcode 库，画不了二维码",
                          detail="部署目录执行 ./venv/bin/pip install qrcode 即可；"
                                 "或改用「验证码登录」标签页（不需要这个库）。", busy=False)
                return
            except Exception as e:
                await self._teardown()
                self._set(state="error", message="生成二维码失败", detail=str(e), busy=False)
                return
            self._set(state="qr_pending", qr=svg, qr_url=qr.url, busy=False,
                      qr_expires=time.time() + 30, message="用手机 Telegram 扫码",
                      detail="手机 TG → 设置 → 设备 → 链接桌面设备 → 扫描本页二维码；"
                             "二维码约 30 秒自动换新，不用手动刷新。")
            log.info("[登录] 二维码已生成，等手机扫码（约 30 秒自动换新）")
            try:
                await qr.wait(timeout=25)
                me = await client.get_me()
                await self._finish_success(me)
                return
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                try:
                    await qr.recreate()          # 过期自动换新码
                except Exception as e:
                    if self._transient(e) and tries < 3:
                        tries += 1                       # 重新申请一张码
                        break
                    await self._teardown()
                    self._set(state="error", message="二维码刷新失败", detail=str(e), busy=False)
                    return
            except SessionPasswordNeededError:
                self._set(state="password_needed", busy=False, message="需要两步验证密码",
                          detail="这个账号开了两步验证：填一次密码即可完成登录。",
                          qr="", qr_url="")
                return
            except Exception as e:
                if self._transient(e) and tries < 3:
                    tries += 1                           # 重新申请一张码（Telethon 可能已自断重连）
                    await asyncio.sleep(2)
                    break
                await self._teardown()
                self._set(state="error", message="扫码登录失败",
                          detail="%s: %s" % (type(e).__name__, e), busy=False)
                return

    # ---------- 验证码 ----------
    async def _do_sign_in_code(self, code: str) -> None:
        from telethon.errors import (FloodWaitError, PhoneCodeExpiredError,
                                     PhoneCodeInvalidError, SessionPasswordNeededError)
        try:
            client = await self._ensure_connected()
        except ConnectionError as e:
            self._set(state="code_sent", busy=False, message="网络断了，没能提交", detail=str(e))
            return
        try:
            await client.sign_in(phone=self._phone, code=code,
                                 phone_code_hash=self._phone_code_hash)
            await self._finish_success(await client.get_me())
        except SessionPasswordNeededError:
            log.info("[登录] 验证码已通过，账号开了两步验证，等填密码")
            self._set(state="password_needed", busy=False, message="需要两步验证密码",
                      detail="验证码对了，再填一次两步验证密码就完成登录。")
        except PhoneCodeInvalidError:
            log.warning("[登录] 验证码不对（用户重新输入）")
            self._set(state="code_sent", busy=False, message="验证码不对",
                      detail="再试一次（不要带空格）；实在收不到就点「发送验证码」重发。")
        except PhoneCodeExpiredError:
            log.warning("[登录] 验证码已过期")
            self._set(state="error", message="验证码过期了",
                      detail="点「发送验证码」重发一次再填。", busy=False)
        except FloodWaitError as e:
            log.warning("[登录] Telegram 限流，需等 %s 秒", e.seconds)
            self._set(state="error", message="Telegram 限流中",
                      detail="等 %d 秒再试（连续重试会触发风控）。" % e.seconds, busy=False)
        except Exception as e:
            log.warning("[登录] 提交验证码失败（不记内容）：%s: %s", type(e).__name__, e)
            self._set(state="code_sent", busy=False, message="验证失败",
                      detail="%s: %s" % (type(e).__name__, e))

    async def _do_sign_in_password(self, password: str) -> None:
        from telethon.errors import PasswordHashInvalidError
        try:
            client = await self._ensure_connected()
        except ConnectionError as e:
            self._set(state="password_needed", busy=False,
                      message="网络断了，没能提交", detail=str(e))
            return
        try:
            await client.sign_in(password=password)
            await self._finish_success(await client.get_me())
        except PasswordHashInvalidError:
            log.warning("[登录] 两步验证密码不对（用户重新输入）")
            self._set(state="password_needed", busy=False, message="两步验证密码不对",
                      detail="再填一次试试。")
        except Exception as e:
            log.warning("[登录] 两步验证失败（不记内容）：%s: %s", type(e).__name__, e)
            self._set(state="password_needed", busy=False, message="两步验证失败",
                      detail="%s: %s" % (type(e).__name__, e))


# 单例，webui.py 直接用
LOGIN = TGLogin()


def action(payload: dict) -> dict:
    """HTTP 层统一入口：{'action': ..., 其余为参数} → 最新状态快照。"""
    act = str((payload or {}).get("action") or "").strip()
    force = bool((payload or {}).get("force"))
    if act == "start_qr":
        return LOGIN.start_qr(force=force)
    if act == "start_phone":
        return LOGIN.start_phone(payload.get("phone", ""), force=force)
    if act == "submit_code":
        return LOGIN.submit_code(payload.get("code", ""))
    if act == "submit_password":
        return LOGIN.submit_password(payload.get("password", ""))
    if act == "cancel":
        return LOGIN.cancel()
    return LOGIN._err("未知操作: %s" % (act or "(空)"))


# ============================ session 登录态探测（配置页用） ============================
# 配置页要回答一个问题：「现在到底登录了没有？」判断顺序（快，且绝不跟主程序抢 session）：
#   1. 有登录流程在进行 → 报「进行中」，不插一脚
#   2. 主程序 tg2gotify.py 在跑 → 一定是已登录（未授权它会直接退出，见 tg2gotify.py）
#   3. 其它情况真连一次 Telegram 看 session 还有效没 → 结果缓存 60 秒
# 探测结果同时落一份 login_state.json：页面重启后先显示「上次确认」，不至于是空白。

STATE_PATH = os.environ.get("TG2GOTIFY_LOGIN_STATE",
                            os.path.join(BASE_DIR, "login_state.json"))
PROBE_TTL = 300         # 秒；探测结果缓存这么久（页面开着时不会每秒去连 TG）

# 主程序自己写的心跳状态文件（tg2gotify.py 的 STATUS_PATH，两边约定同一个名字）
STATUS_PATH = os.environ.get("TG2GOTIFY_STATUS", os.path.join(BASE_DIR, "run_status.json"))
HEARTBEAT_STALE = 180   # 秒；超过这么久没刷心跳 = 进程在但卡住/掉线

_state_lock = threading.Lock()
_state_cache: dict = {"checked_at": 0.0, "probing": False}


def _proc_cmdline(pid: int) -> str:
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            return f.read().decode("utf-8", "ignore").replace("\0", " ").strip()
    except OSError:
        return ""


def _proc_unit(pid: int) -> str:
    """进程属于哪个 systemd 单元（读 /proc/<pid>/cgroup，比每次调 systemctl 便宜）。
    返回 '' 表示不是 systemd 拉起来的（网页按钮 / 手工启动）——那份重启服务器不会自己回来。"""
    try:
        with open("/proc/%d/cgroup" % pid, "r", encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return ""
    for line in txt.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        seg = parts[2].strip().rstrip("/").rsplit("/", 1)[-1]
        if seg.endswith(".service"):
            return seg
    return ""


def backend_state() -> dict:
    """后端（tg2gotify.py 主程序）运行态。
    优先读它写的心跳状态文件（能看出掉线/卡死），再用 PID 存活核对防文件残留；
    没有状态文件（旧版 v1）时退回扫进程，至少告诉用户「有进程在跑」。"""
    now = time.time()
    data: dict = {}
    try:
        with open(STATUS_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        data = d if isinstance(d, dict) else {}
    except Exception:
        data = {}
    try:
        pid = int(data.get("pid") or 0)
    except Exception:
        pid = 0
    # PID 还活着、且确实是 tg2gotify.py 才认（防 pid 被别的进程复用）
    cmd = _proc_cmdline(pid) if pid else ""
    alive = bool(pid) and "tg2gotify.py" in cmd and "python" in cmd

    if not alive:
        pids = main_program_pids()
        if pids:      # 旧版 v1 或没写状态文件：只能靠进程判断
            return {"running": True, "known": False, "pid": pids[0], "stale": False,
                    "heartbeat_age": None, "started_at": 0, "uptime": 0, "user": "",
                    "sources": None, "sources_enabled": None, "pool": None,
                    "version": "", "proxy": "", "session": "", "last_hit_at": 0,
                    "unit": _proc_unit(pids[0]),
                    "note": "进程在跑，但没有心跳状态文件（旧版本？）"}

    updated = float(data.get("updated_at") or 0)
    started = float(data.get("started_at") or 0)
    hb_age = (now - updated) if updated else None
    return {
        "running": alive, "known": bool(data), "pid": pid,
        "user": str(data.get("user") or ""),
        "started_at": started,
        "uptime": int(now - started) if (alive and started) else 0,
        "sources": data.get("sources"), "sources_enabled": data.get("sources_enabled"),
        "pool": data.get("pool"), "version": str(data.get("version") or ""),
        "proxy": str(data.get("proxy") or ""), "session": str(data.get("session") or ""),
        "last_hit_at": float(data.get("last_hit_at") or 0),
        "heartbeat_age": (int(hb_age) if hb_age is not None else None),
        "stale": bool(alive and hb_age is not None and hb_age > HEARTBEAT_STALE),
        "unit": _proc_unit(pid) if alive else "",
        "note": "",
    }


def _state_saved() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _state_save(d: dict) -> None:
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        log.debug("[状态] 写 %s 失败（不影响功能）：%s", STATE_PATH, e)


async def _probe_async() -> dict:
    """真连一次 Telegram，看 session 是否还有效。只读，不改任何东西。"""
    from telethon import TelegramClient, functions
    from telethon.errors import AuthKeyUnregisteredError
    cfg = read_config()
    client = TelegramClient(session_path(cfg), int(cfg["API_ID"]), cfg["API_HASH"],
                            proxy=proxy_arg(cfg))
    try:
        try:
            await client.connect()
        except Exception as e:
            return {"logged_in": None, "user": "",
                    "error": "连不上 Telegram（检查代理 / 网络）：%s" % e}
        try:
            await client(functions.updates.GetStateRequest())
        except AuthKeyUnregisteredError:
            return {"logged_in": False, "user": "",
                    "error": "session 已被 Telegram 注销（换了设备或主动退出），需要重新登录"}
        user = ""
        try:
            me = await client.get_me()
            uname = getattr(me, "username", None)
            uid = getattr(me, "id", None)
            user = ("@%s" % uname) if uname else (getattr(me, "first_name", None) or "")
            if uid:
                user = "%s (id %s)" % (user, uid) if user else "id %s" % uid
        except Exception:
            pass
        return {"logged_in": True, "user": user, "error": ""}
    except Exception as e:
        return {"logged_in": None, "user": "", "error": "%s: %s" % (type(e).__name__, e)}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def _probe_worker() -> None:
    try:
        res = asyncio.run(_probe_async())
    except Exception as e:
        res = {"logged_in": None, "user": "", "error": "%s: %s" % (type(e).__name__, e)}
    res["checked_at"] = time.time()
    with _state_lock:
        _state_cache.update(res, probing=False)
    log.info("[状态] 探测结果：%s %s", res.get("logged_in"), res.get("error") or res.get("user"))
    _state_save({"logged_in": res.get("logged_in"), "user": res.get("user", ""),
                 "checked_at": res["checked_at"]})


BACKEND_LOG = os.path.join(BASE_DIR, "tg2gotify-run.log")
BACKEND_WAIT = 12      # 拉起后最多等这么久，看它有没有真起来（起不来通常几秒就退）


def _log_tail(offset: int = 0, limit: int = 1200) -> str:
    """读后端日志的尾部（从 offset 之后开始更好，只看本次启动输出）。"""
    try:
        size = os.path.getsize(BACKEND_LOG)
        with open(BACKEND_LOG, "rb") as f:
            start = offset if (offset and size > offset) else max(0, size - limit)
            f.seek(start)
            data = f.read(limit)
        return data.decode("utf-8", "ignore").strip()
    except OSError:
        return ""


def _wait_gone(pid: int, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if not _proc_cmdline(pid):
            return True
        time.sleep(0.3)
    return False


def _wait_probe_idle(timeout: float = 10.0) -> bool:
    """等后台的 session 探测跑完 —— 它也用同一个 session 文件，不能跟后端启动撞一起。
    没在探测、或等到了就返回 True；超时返回 False。"""
    end = time.time() + timeout
    while time.time() < end:
        with _state_lock:
            if not _state_cache.get("probing"):
                return True
        time.sleep(0.3)
    return False


def backend_start() -> dict:
    """从网页上拉起后端主程序（就是本目录的 tg2gotify.py —— 网页配置的那个）。
    已经有实例在跑就拒绝：同一个 TG session 不能两处同时用。
    返回 {ok, message, detail, pid}；起不来时 detail 直接给日志尾部，不藏着。"""
    cur = backend_state()
    if cur.get("running"):
        return {"ok": False, "pid": int(cur.get("pid") or 0),
                "message": "已经有后端在跑了（PID %s），不重复启动" % cur.get("pid"),
                "detail": "同一个 Telegram session 不能两处同时用；要换就用下面的「停止后端」先停掉。"}
    if not _wait_probe_idle():
        return {"ok": False, "pid": 0, "pending": False,
                "message": "正在检查 session 状态，稍等几秒再点启动",
                "detail": "配置页每隔几分钟会真连一次 Telegram 看 session 还有效没；"
                          "同一个 session 文件不能两处同时用，等它结束再启动。"}
    main = os.path.join(BASE_DIR, "tg2gotify.py")
    if not os.path.exists(main):
        return {"ok": False, "pid": 0, "message": "本目录找不到 tg2gotify.py", "detail": main}
    try:
        offset = os.path.getsize(BACKEND_LOG)
    except OSError:
        offset = 0
    try:
        logf = open(BACKEND_LOG, "ab", buffering=0)
    except OSError as e:
        return {"ok": False, "pid": 0, "message": "打不开日志文件", "detail": "%s: %s" % (BACKEND_LOG, e)}
    try:
        proc = subprocess.Popen([sys.executable, main], cwd=BASE_DIR,
                                stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
                                start_new_session=True)   # 脱离 WebUI：WebUI 重启不影响它
    except OSError as e:
        return {"ok": False, "pid": 0, "message": "启动失败",
                "detail": "%s: %s" % (type(e).__name__, e)}
    finally:
        logf.close()
    log.info("[后端] 已拉起 %s（PID %d），日志 %s", main, proc.pid, BACKEND_LOG)

    deadline = time.time() + BACKEND_WAIT
    while time.time() < deadline:
        if proc.poll() is not None:            # 起不来（session 未登录 / 代理不通 / 配置缺字段）
            return {"ok": False, "pid": 0,
                    "message": "后端起来就退了（退出码 %s），下面是它的日志：" % proc.returncode,
                    "detail": _log_tail(offset)}
        st = backend_state()
        # 只看「它自己写了心跳状态文件」这个硬信号：光看 /proc 会误判 ——
        # 配置缺字段 / session 过期时进程能活几秒，但马上就要退出
        if (st.get("running") and st.get("known")
                and int(st.get("pid") or 0) == proc.pid):
            return {"ok": True, "pid": proc.pid,
                    "message": "后端已启动（PID %d）" % proc.pid,
                    "detail": "监听 %s 个来源（启用 %s）｜ 池子 %s 词" % (
                        st.get("sources"), st.get("sources_enabled"), st.get("pool"))}
        time.sleep(0.4)
    # 进程活着，但还没写心跳：中性提示（别打绿勾 —— 它可能正在首次连 TG，也可能马上就要退）
    return {"ok": True, "pending": True, "pid": proc.pid,
            "message": "后端进程已拉起（PID %d），但 %d 秒内还没写心跳文件"
                       % (proc.pid, BACKEND_WAIT),
            "detail": "可能是首次连 Telegram 比较慢；看一下下面的日志。\n" + _log_tail(offset)}


def backend_stop() -> dict:
    """停掉后端。systemd 管着的（Restart=always）必须用 systemctl stop ——
    直接 kill 会被 systemd 10 秒后自己拉回来。有多个实例时一并全停。"""
    st = backend_state()
    if not st.get("running"):
        return {"ok": False, "pid": 0, "message": "后端没在跑，不用停", "detail": ""}
    pids = [p for p in dict.fromkeys(main_program_pids() or [int(st.get("pid") or 0)]) if p]
    first = pids[0] if pids else 0

    unit_active = False
    try:
        r = subprocess.run(["systemctl", "is-active", "tg2gotify"],
                           capture_output=True, text=True, timeout=6)
        unit_active = (r.stdout or "").strip() == "active"
    except Exception:
        unit_active = False

    if unit_active:
        try:
            subprocess.run(["systemctl", "stop", "tg2gotify"],
                           capture_output=True, text=True, timeout=25)
        except Exception as e:
            return {"ok": False, "pid": first, "message": "systemctl stop 调用失败",
                    "detail": "%s: %s" % (type(e).__name__, e)}
        left = [p for p in pids if not _wait_gone(p, 8)]
        return {"ok": not left, "pid": first,
                "message": ("已通过 systemd 停止（%s）" % ", ".join(str(p) for p in pids)) if not left
                           else "已发停止指令，但进程 %s 还在" % ", ".join(str(p) for p in left),
                "detail": "" if not left else "等会儿刷新看看；还不掉就 SSH 上 systemctl status tg2gotify。"}

    failed = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as e:
            failed.append("%s（%s）" % (pid, type(e).__name__))
    if failed and len(failed) == len(pids):
        return {"ok": False, "pid": first, "message": "发停止信号失败", "detail": "、".join(failed)}
    left = [p for p in pids if not _wait_gone(p, 8)]
    msg = ("已停止（%s）" % ", ".join(str(p) for p in pids)) if not left \
        else "已发停止信号，但进程 %s 还在" % ", ".join(str(p) for p in left)
    detail = "" if not left else "它会自己断连接退出，过几秒刷新看状态。"
    if failed:
        detail = ("部分进程发信号失败：" + "、".join(failed) + "。" + detail).strip()
    return {"ok": not left, "pid": first, "message": msg, "detail": detail}


def get_state(force: bool = False) -> dict:
    """给配置页用的状态快照（TG 登录态 + 后端运行态）。永远不阻塞：真探测放后台线程。"""
    now = time.time()
    with _state_lock:
        cache = dict(_state_cache)
    backend = backend_state()
    main_run = bool(backend.get("running"))
    out = {"logged_in": None, "user": "", "main_running": main_run,
           "backend": backend,
           "in_progress": LOGIN.state_name() in ACTIVE_STATES, "checking": False,
           "checked_at": float(cache.get("checked_at") or 0), "note": ""}

    if out["in_progress"]:
        out["note"] = "正在走登录流程（登录页开着），这里先不打扰。"
        return out

    if main_run:
        saved = _state_saved()
        out.update(logged_in=True, user=saved.get("user") or cache.get("user", ""),
                   note="主程序 tg2gotify.py 正在运行 —— 它起得来就说明 session 是登录状态。")
        return out

    fresh = (now - float(cache.get("checked_at") or 0)) < PROBE_TTL
    if not force and fresh and cache.get("logged_in") in (True, False):
        out.update(logged_in=cache["logged_in"], user=cache.get("user", ""),
                   note=cache.get("error", "") or "")
        return out

    if cache.get("probing"):
        out.update(logged_in=cache.get("logged_in"), user=cache.get("user", ""),
                   checking=True, note="正在检查 session …")
        return out

    with _state_lock:                      # 起后台探测，页面下次轮询就能拿到结果
        if not _state_cache.get("probing"):
            _state_cache["probing"] = True
            threading.Thread(target=_probe_worker, name="tg-state-probe",
                             daemon=True).start()
    saved = _state_saved()
    if saved and saved.get("logged_in") is not None:
        out.update(logged_in=saved.get("logged_in"), user=saved.get("user", ""),
                   checked_at=float(saved.get("checked_at") or 0),
                   note="正在检查 session …（以下为上次确认的结果）")
    else:
        out.update(checking=True, note="正在检查 session …")
    return out


# ============================ 页面 ============================

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TG2Gotify 监控脚本 · Telegram 登录</title>
<style>
  :root { --bg:#f7f6f3; --card:#fff; --line:#e3e0da; --text:#2d2a26; --dim:#8a857c;
          --acc:#2f6f4f; --acc2:#eef5f0; --warn:#b4531f; --warnbg:#fdf3ec; }
  * { box-sizing:border-box; }
  body { font-family:system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
         background:var(--bg); color:var(--text); margin:0; padding:24px 12px 60px; }
  .wrap { max-width:620px; margin:0 auto; }
  h1 { font-size:19px; margin:0 0 4px; }
  .sub { color:var(--dim); font-size:13px; margin-bottom:16px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:16px 18px; margin-bottom:14px; }
  .tabs { display:flex; gap:8px; margin-bottom:14px; }
  .tab { flex:1; text-align:center; padding:10px; border:1px solid var(--line);
         border-radius:8px; cursor:pointer; font-size:14px; background:var(--card); }
  .tab.on { border-color:var(--acc); background:var(--acc2); color:var(--acc); font-weight:600; }
  input { width:100%; padding:10px; font-size:14px; border:1px solid var(--line);
          border-radius:6px; font-family:inherit; margin-top:4px; }
  button { background:var(--acc); color:#fff; border:0; border-radius:8px;
           padding:10px 20px; font-size:14px; cursor:pointer; }
  button.ghost { background:var(--acc2); color:var(--acc); border:1px solid var(--acc); }
  button:disabled { background:#9db8a9; cursor:not-allowed; }
  .row { display:flex; gap:10px; align-items:center; margin-top:12px; flex-wrap:wrap; }
  .hint { font-size:12px; color:var(--dim); margin-top:8px; line-height:1.6; }
  .banner { border-radius:8px; padding:10px 12px; font-size:13px; line-height:1.6;
            margin-bottom:14px; border:1px solid #e6c9b4; background:var(--warnbg); color:var(--warn); }
  .qrbox { display:flex; justify-content:center; padding:10px 0; }
  .qrbox svg { width:240px; height:240px; }
  .stat { font-size:14px; font-weight:600; }
  .ok { color:var(--acc); }
  .bad { color:var(--warn); }
  a { color:var(--acc); }
</style>
</head>
<body>
<div class="wrap">
  <h1>📱 Telegram 登录</h1>
  <div class="sub">这是给「TG 频道监控脚本」用的账号 —— 它靠这个账号盯着频道/机器人的推送消息
    （主要用途：VPS 补货、商家上新这类提醒）。<br>
    session 失效 / 换设备 / 新部署时在这里登录 —— 不用 SSH 进终端跑二维码脚本。<br>
    <b>登录前要先停掉后端监控</b>（同一个 session 不能两处同时用）。</div>

  <div id="banner" class="banner" hidden></div>

  <div class="card">
    <div class="tabs">
      <div class="tab on" id="tab-qr" onclick="setTab('qr')">扫码登录</div>
      <div class="tab" id="tab-phone" onclick="setTab('phone')">验证码登录</div>
    </div>

    <div id="pane-qr">
      <div class="qrbox" id="qrbox" hidden></div>
      <div class="row"><button id="btn-qr" onclick="act({action:'start_qr'})">开始扫码 / 换一张码</button></div>
      <div class="hint">手机 TG → 设置 → 设备 → 链接桌面设备 → 扫描本页二维码。<br>
        二维码约 30 秒自动换新；扫不上就等它换一张再扫。</div>
    </div>

    <div id="pane-phone" hidden>
      <label class="hint">手机号（带国家码）</label>
      <input id="phone" placeholder="+8613800138000" autocomplete="tel">
      <div class="row"><button id="btn-send"
        onclick="act({action:'start_phone', phone:document.getElementById('phone').value})">发送验证码</button></div>

      <div id="codeblk" hidden>
        <label class="hint">收到的验证码</label>
        <input id="code" placeholder="12345" autocomplete="one-time-code" inputmode="numeric">
        <div class="row"><button id="btn-code"
          onclick="act({action:'submit_code', code:document.getElementById('code').value})">提交验证码</button></div>
      </div>

      <div id="pwblk" hidden>
        <label class="hint">两步验证密码（开了两步验证才需要）</label>
        <input id="pw" type="password" autocomplete="current-password">
        <div class="row"><button
          onclick="act({action:'submit_password', password:document.getElementById('pw').value})">提交密码</button></div>
      </div>

      <div class="hint">验证码由 Telegram 发到你手机的 TG（已登录设备直接弹出），比短信快。</div>
    </div>
  </div>

  <div class="card">
    <div class="stat" id="statline">状态：加载中 …</div>
    <div class="hint" id="detail"></div>
    <div class="hint" id="note"></div>
    <div class="hint" id="meta"></div>
    <label class="hint" style="display:block;margin-top:10px">
      <input type="checkbox" id="force" style="width:auto;margin:0 6px 0 0">
      强制重新登录（当前 session 还能用时才需要勾）
    </label>
    <div class="row">
      <button class="ghost" onclick="act({action:'cancel'})">取消 / 释放 session</button>
      <a href="/">← 回到配置页</a>
    </div>
  </div>
</div>

<script>
let TAB = 'qr';
function setTab(t) {
  TAB = t;
  document.getElementById('tab-qr').classList.toggle('on', t === 'qr');
  document.getElementById('tab-phone').classList.toggle('on', t === 'phone');
  document.getElementById('pane-qr').hidden = (t !== 'qr');
  document.getElementById('pane-phone').hidden = (t !== 'phone');
}
async function act(payload) {
  if (String(payload.action).indexOf('start_') === 0) {
    payload.force = document.getElementById('force').checked;
    if (payload.force && !confirm('当前 session 要是还能用，强制重新登录会多出一条登录记录。继续？')) return;
  }
  await fetch('/api/tg-login/action', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  setTimeout(poll, 300);
}
function render(s) {
  const banner = document.getElementById('banner');
  if (s.main_running) {
    banner.hidden = false;
    banner.textContent = '⚠️ 主程序正在运行：同一个 TG session 不能两处同时用（会互相踢）。'
      + '请先停掉主程序（systemctl stop tg2gotify），再回来登录；登录成功后再启动。';
  } else if (!s.config_ok) {
    banner.hidden = false;
    banner.textContent = '⚠️ config.json 读不了，先把配置修好再来登录。';
  } else {
    banner.hidden = true;
  }
  const block = s.main_running || !s.config_ok;
  document.querySelectorAll('button').forEach(b => { b.disabled = block; });
  const st = s.state;
  const qrbox = document.getElementById('qrbox');
  qrbox.hidden = (st !== 'qr_pending');
  if (st === 'qr_pending' && s.qr) qrbox.innerHTML = s.qr;
  if (st === 'qr_pending' && TAB !== 'qr') setTab('qr');

  document.getElementById('codeblk').hidden = (st !== 'code_sent');
  document.getElementById('pwblk').hidden = (st !== 'password_needed');
  if ((st === 'code_sent' || st === 'password_needed') && TAB !== 'phone') setTab('phone');
  if (st === 'password_needed') document.getElementById('pw').focus();

  const line = document.getElementById('statline');
  let text = '状态：' + (s.message || st) + (s.busy ? '（处理中 …）' : '');
  if (st === 'success' && s.user) text += '　账号：' + s.user;
  line.textContent = text;
  line.className = 'stat ' + (st === 'success' ? 'ok' : (st === 'error' ? 'bad' : ''));
  document.getElementById('detail').textContent = s.detail || '';
  document.getElementById('note').textContent = s.session_note || '';
  document.getElementById('meta').textContent =
    'session 文件：' + s.session_name + (s.session_exists ? '（已存在）' : '（登录后创建）')
    + '　｜　出站代理：' + s.proxy;
}
async function poll() {
  try {
    const r = await fetch('/api/tg-login/status');
    if (r.status === 401) { location.href = '/login'; return; }
    render(await r.json());
  } catch (e) { /* 网络抖动忽略，下次再拉 */ }
}
poll();
setInterval(poll, 2000);
</script>
</body>
</html>"""
