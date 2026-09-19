#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
扫码登录脚本 —— 用手机 Telegram 扫二维码，无需手机验证码
=========================================================
适用场景：国内 +86 手机号收不到 Telegram 登录验证码。
前提：手机上已经登录着你的 TG 账号（你本来就有）。

用法（服务器上）:
    python qr_login.py
→ 终端显示二维码
→ 手机 Telegram: 设置 → 设备 → 链接桌面设备 → 扫码 → 确认登录
→ 提示"登录成功"后，直接运行 python tg2gotify.py 即可（共用同一 session）

若开了两步验证密码，脚本会提示输入一次。
"""
from __future__ import annotations

import asyncio

import qrcode
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from tg2gotify import CONFIG, chmod_private, load_config, proxy_dict, session_path


def render_qr(url: str) -> None:
    print("\n" + "=" * 56)
    print("手机 Telegram → 设置 → 设备 → 链接桌面设备 → 扫下面的码")
    print("=" * 56)
    code = qrcode.QRCode(border=1)
    code.add_data(url)
    code.print_ascii(invert=True)
    print("（二维码约 30 秒失效，会自动刷新；终端里扫不出就换光线/远近试试）")
    print(f"（备用：用 TG 内置扫码器直接扫链接也可: {url}）\n")


async def main() -> None:
    try:
        CONFIG.update(load_config())  # 填充 CONFIG（load_config 只返回，不自动写全局）
    except ValueError as e:
        print(f"[!!] {e}")
        print("     新部署请先: cp config.example.json config.json 并填好 API_ID / API_HASH")
        return
    if not CONFIG["API_ID"] or not CONFIG["API_HASH"]:
        print("[!!] 先在 config.json 里填 API_ID / API_HASH")
        print("     （在这台电脑的浏览器上打开 my.telegram.org 申请，服务器不需要浏览器）")
        return

    client = TelegramClient(session_path(), CONFIG["API_ID"],
                            CONFIG["API_HASH"], proxy=proxy_dict())
    await client.connect()
    chmod_private(session_path())                      # session 含账号凭据 → 600
    chmod_private(session_path() + "-journal")

    if await client.is_user_authorized():
        print("✅ 该 session 已登录过，无需扫码。直接运行: python tg2gotify.py")
        await client.disconnect()
        return

    print("正在申请登录二维码 …")
    qr = await client.qr_login()
    while True:
        render_qr(qr.url)
        try:
            await qr.wait()
            break
        except asyncio.TimeoutError:
            print("二维码已过期刷新，请扫屏幕上的新码 …")
            qr = await qr.recreate()
        except SessionPasswordNeededError:
            pwd = input("检测到两步验证密码，请输入: ")
            await client.sign_in(password=pwd)
            break

    me = await client.get_me()
    name = getattr(me, "first_name", None) or getattr(me, "username", None) or str(getattr(me, "id", "?"))
    print(f"\n✅ 登录成功：{name}")
    print("session 已保存。现在直接运行: python tg2gotify.py")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
