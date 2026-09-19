#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
webui.py —— TG2Gotify v2 配置界面（纯标准库 http.server，单文件，无新依赖）
==========================================================================
功能：
  • 频道列表：每频道 开关 enabled / 切换 use_pool / 编辑 label、
    extra_keywords、exclude_keywords
  • 共享关键词池 KEYWORD_POOL 增删改
  • 保存 → 写回 config.json（人可读缩进，原子写入）→ 主程序 5 秒内热重载生效

安全：
  • 访问令牌（config.json 的 WEBUI_TOKEN；首次启动自动生成并打印到终端）
  • 登录后发一个**随机会话 cookie**（cookie 里不是长期令牌本身）；
    「退出登录」只清掉本浏览器这个 cookie
  • 也支持 URL 带 ?token=xxx 直接登录（方便第一次用 / 手机书签；别把带 token 的链接外发）
  • 默认绑定 0.0.0.0:8098（局域网工具，别直接暴露到公网）

运行： python3 webui.py   （独立进程，可选；主程序 tg2gotify.py 不依赖它）
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import tglogin   # 网页端 TG 登录（扫码/验证码），独立模块，主程序不依赖它
from http import cookies as http_cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("TG2GOTIFY_CONFIG",
                             os.path.join(BASE_DIR, "config.json"))
DEFAULT_PORT = 8098
COOKIE_NAME = "t2g_token"
_save_lock = threading.Lock()  # 防并发保存交叉写坏 config.json

log = logging.getLogger("tg2gotify.webui")

# ---- 登录会话 ----
# cookie 里放的是**随机值**，不是长期令牌本身：浏览器里泄漏的只是个可随时废弃的会话。
SESSION_TTL = 30 * 86400        # 会话有效期（秒）
SESSION_MAX = 200               # 最多记住多少个会话（超了丢最旧的）
_sessions: dict = {}
_session_lock = threading.Lock()


def _issue_session() -> str:
    """发一个新的随机会话 id（登录成功后写进 cookie）。"""
    sid = secrets.token_urlsafe(24)
    now = time.time()
    with _session_lock:
        _sessions[sid] = now
        for k, t in [(k, t) for k, t in _sessions.items() if now - t > SESSION_TTL]:
            _sessions.pop(k, None)
        while len(_sessions) > SESSION_MAX:
            _sessions.pop(min(_sessions, key=_sessions.get), None)
    return sid


def _drop_session(sid: str) -> None:
    with _session_lock:
        _sessions.pop(sid, None)


def _session_ok(sid: str) -> bool:
    if not sid:
        return False
    with _session_lock:
        ts = _sessions.get(sid)
        if ts is None:
            return False
        if time.time() - ts > SESSION_TTL:
            _sessions.pop(sid, None)
            return False
    return True

_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TG2Gotify · TG 频道监控脚本</title>
<style>
  :root { --bg:#f7f6f3; --card:#fff; --line:#e3e0da; --text:#2d2a26; --dim:#8a857c;
          --acc:#2f6f4f; --acc2:#eef5f0; --warn:#b4531f; }
  * { box-sizing:border-box; }
  body { font-family:system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
         background:var(--bg); color:var(--text); margin:0; padding:24px 12px 60px; }
  .wrap { max-width:920px; margin:0 auto; }
  h1 { font-size:20px; margin:0 0 4px; }
  .sub { color:var(--dim); font-size:13px; margin-bottom:18px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:16px 18px; margin-bottom:16px; }
  .card h2 { font-size:15px; margin:0 0 10px; }
  /* 一张卡里的小折叠块（运行状态 / 出站代理 / 通知渠道），点标题一行展/收 */
  .sub { border:1px solid var(--line); border-radius:8px; margin-bottom:10px; overflow:hidden; }
  .sub:last-child { margin-bottom:0; }
  .subhead { display:flex; align-items:center; gap:10px; padding:10px 13px; cursor:pointer;
             background:#fbfaf8; user-select:none; }
  .subhead:hover { background:#f4f2ee; }
  .subhead .subtitle { font-size:14px; font-weight:600; flex:1; }
  .subhead .sum { font-size:12px; color:var(--dim); text-align:right; }
  .subhead .sum.ok { color:var(--acc); }
  .subhead .sum.bad { color:var(--warn); }
  .subhead .caret { color:var(--dim); font-size:13px; transition:transform .15s; }
  .sub.collapsed .subhead .caret { transform:rotate(-90deg); }
  .sub.collapsed .subbody { display:none; }
  .subbody { padding:12px 13px; border-top:1px solid var(--line); }
  .src { border:1px solid var(--line); border-radius:8px; padding:12px 14px; margin-bottom:10px; }
  /* 刚加的频道高亮一下（还没保存），保存后自动恢复普通样式 */
  .src.new { border-color:var(--acc); background:#f5fbf7;
             box-shadow:0 0 0 3px rgba(47,111,79,.10); animation:pop .4s ease; }
  @keyframes pop { from { background:#dff0e6; } to { background:#f5fbf7; } }
  .src-head { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  .src-head .key { font-weight:600; font-size:14px; }
  .src-head input[type=text] { flex:1; min-width:160px; }
  .row { display:flex; align-items:center; gap:16px; flex-wrap:wrap; margin-top:8px; }
  label.ck { display:flex; align-items:center; gap:6px; font-size:13px; color:var(--text); cursor:pointer; }
  textarea { width:100%; min-height:52px; margin-top:6px; padding:8px 10px; font-size:13px;
             border:1px solid var(--line); border-radius:6px; font-family:inherit; resize:vertical; }
  input[type=text], input[type=password] { padding:7px 10px; font-size:13px; border:1px solid var(--line);
                     border-radius:6px; font-family:inherit; }
  .hint { font-size:12px; color:var(--dim); }
  .addrow { display:flex; gap:10px; margin-top:12px; flex-wrap:wrap;
            border-top:1px dashed var(--line); padding-top:12px; }
  .addrow input[type=text] { flex:1; min-width:150px; }
  /* 新增频道区：常态就是一整行淡绿虚线框，一眼能找到「在哪里加」 */
  .addzone { margin-top:14px; border:1px dashed var(--acc); border-radius:8px;
             background:#f5fbf7; overflow:hidden; }
  .addzone .addhead { display:flex; align-items:center; gap:8px; cursor:pointer;
                      padding:11px 14px; font-size:14px; font-weight:600; color:var(--acc);
                      user-select:none; }
  .addzone .addhead:hover { background:#eaf6ef; }
  .addzone .addhead .caret { margin-left:4px; transition:transform .15s; }
  .addzone.open .addhead .caret { transform:rotate(180deg); }
  .addzone .addbody { padding:2px 14px 14px; }
  .addzone .addbody input[type=text] { background:#fff; }
  .addzone .addbody input[type=text] { flex:1; min-width:150px; }
  /* 参数说明块 */
  .legend { font-size:12px; color:var(--dim); line-height:1.8; background:#fbfaf8;
            border:1px solid var(--line); border-radius:6px; padding:8px 11px; margin-bottom:12px; }
  .legend b { color:var(--text); }
  .legend .lb { display:inline-block; min-width:118px; }
  button.del { background:transparent; border:1px solid transparent; border-radius:6px;
               cursor:pointer; font-size:13px; opacity:.5; padding:3px 8px; }
  button.del:hover { opacity:1; border-color:var(--warn); color:var(--warn); background:#fdf3ec; }
  .bar { position:fixed; left:0; right:0; bottom:0; background:var(--card);
         border-top:1px solid var(--line); padding:10px 16px; display:flex;
         gap:12px; align-items:center; justify-content:center; box-shadow:0 -2px 8px rgba(0,0,0,.04); }
  button.primary { background:var(--acc); color:#fff; border:0; border-radius:8px;
                   padding:9px 28px; font-size:14px; cursor:pointer; }
  button.primary:disabled { background:#9db8a9; }
  button.ghost { background:var(--acc2); color:var(--acc); border:1px solid var(--acc);
                 border-radius:8px; padding:8px 18px; font-size:13px; cursor:pointer; }
  #msg { font-size:13px; }
  #msg.ok { color:var(--acc); } #msg.err { color:var(--warn); }
  .pool textarea { min-height:110px; }
  .logout { color:var(--dim); font-size:12px; margin-left:auto; }
  .stat { font-size:14px; font-weight:600; }
  .ok { color:var(--acc); } .bad { color:var(--warn); }
  .cnt { margin-top:4px; }
  #backendlog { max-height:150px; overflow:auto; background:#fbfaf8; border:1px solid var(--line);
                border-radius:6px; padding:6px 8px; margin-top:6px; }
  #backendlog:empty { display:none; border:0; padding:0; }
</style>
</head>
<body>
<div class="wrap">
  <h1>📡 TG2Gotify — TG 频道监控脚本</h1>
  <div class="sub">
    用你的 Telegram 账号盯着指定的频道 / 机器人（主要用途：VPS 补货、限量补货、商家上新这类推送消息），
    命中关键词就推到 Gotify 手机通知。<br>
    下面改完点「保存全部」写入 config.json，主程序 5 秒内自动热重载（Telethon 连接不断线）。
  </div>

  <div class="card">
    <h2>⚙️ 运行与推送设置</h2>

    <div class="sub collapsed" id="card-state">
      <div class="subhead" onclick="toggleCard('card-state')">
        <span class="subtitle">🔌 运行状态（监控在不在跑）</span>
        <span class="sum" id="state-sum">正在检查 …</span>
        <span class="caret">▾</span>
      </div>
      <div class="subbody">
        <div class="stat" id="tgline">正在检查 …</div>
        <div class="stat" id="beeline" style="margin-top:4px"></div>
        <div class="hint" id="tgmeta"></div>
        <div class="row" style="margin-top:8px">
          <button class="ghost" id="btn-start" onclick="backendAct('start')">▶ 启动后端</button>
          <button class="ghost" id="btn-stop" onclick="backendAct('stop')">⏹ 停止后端</button>
          <span class="hint">网页上的按钮启的就是本目录这个后端（systemd 托管的也一并管）</span>
        </div>
        <div class="hint" id="backendmsg"></div>
        <div class="hint" id="backendlog" style="white-space:pre-wrap"></div>
        <div class="hint" id="autostart"></div>
        <div class="row" style="margin-top:6px">
          <a class="hint" href="/tg-login">📱 TG 登录 / 重新登录</a>
          <span class="hint" id="tgwhen"></span>
        </div>
        <div class="hint">
          「退出登录」只清掉本浏览器里的登录 cookie：TG 登录态存在 session 文件里、Gotify token 存在
          config.json 里，都不受影响；要换 WebUI 访问令牌就改 config.json 的 <b>WEBUI_TOKEN</b> 后重启 WebUI。
        </div>
      </div>
    </div>

    <div class="sub collapsed" id="card-proxy">
      <div class="subhead" onclick="toggleCard('card-proxy')">
        <span class="subtitle">🌐 出站代理（连 Telegram 用）</span>
        <span class="sum" id="proxy-sum"></span>
        <span class="caret">▾</span>
      </div>
      <div class="subbody">
        <div class="row" style="margin-top:0">
          <label class="ck"><input type="radio" name="proxymode" id="proxy-off" value="off" onchange="proxyGate()"> 直连（不走代理）</label>
          <label class="ck"><input type="radio" name="proxymode" id="proxy-on" value="on" onchange="proxyGate()"> 走代理</label>
        </div>
        <div class="row">
          <label class="ck" style="gap:6px">类型
            <select id="proxy-type" style="padding:7px 10px;font-size:13px;border:1px solid var(--line);border-radius:6px">
              <option value="socks5">socks5</option>
              <option value="socks4">socks4</option>
              <option value="http">http</option>
            </select>
          </label>
          <label class="ck" style="gap:6px">地址
            <input type="text" id="proxy-host" placeholder="127.0.0.1" style="width:180px">
          </label>
          <label class="ck" style="gap:6px">端口
            <input type="text" id="proxy-port" placeholder="7890" style="width:90px">
          </label>
        </div>
        <div class="hint">
          给连 Telegram 用（中国网络环境常填本机：socks5 + 127.0.0.1 + 7890）；
          Gotify 在局域网，永远直连、不走这里。<br>
          保存后：网页端 TG 登录立即用新代理；<b>已经在跑的主程序要重启才生效</b>（代理只在启动时读一次）。
        </div>
      </div>
    </div>

    <div class="sub collapsed" id="card-notify">
      <div class="subhead" onclick="toggleCard('card-notify')">
        <span class="subtitle">📣 通知渠道（推送去哪）</span>
        <span class="sum" id="notify-sum"></span>
        <span class="caret">▾</span>
      </div>
      <div class="subbody">
        <div class="hint" style="margin-bottom:10px">
          目前支持 <b>Gotify</b>（本项目就是为它写的推送通道）。<br>
          <b>Token 去哪拿：</b>① 打开你的 Gotify 网页，用管理员登录 →
          ② 上方菜单 <b>Apps</b> → 输入一个应用名 → 点 <b>CREATE APPLICATION</b> →
          ③ 复制那串 token（形如 <code>A1b2c3.xxxxx</code>）粘到下面。<br>
          一个应用＝一路推送渠道；想分用途（手机 / 电脑 / 群）就在 Gotify 里建多个应用。
        </div>
        <div class="row" style="margin-top:0">
          <label class="ck" style="gap:6px;flex:1">Gotify 地址
            <input type="text" id="gotify-url" placeholder="http://192.168.1.100:8080" style="flex:1;min-width:220px">
          </label>
        </div>
        <div class="row">
          <label class="ck" style="gap:6px;flex:1">应用 Token
            <input type="password" id="gotify-token" placeholder="A1b2c3.xxxxx" autocomplete="off" style="flex:1;min-width:200px">
          </label>
          <label class="ck" style="gap:6px"><input type="checkbox" id="gotify-show"> 显示明文</label>
        </div>
        <div class="row">
          <label class="ck" style="gap:6px">推送优先级
            <input type="text" id="gotify-priority" placeholder="8" style="width:70px">
          </label>
          <span class="hint">0-10：1-3 低（普通通知）、4-7 中、<b>8-10 高（会穿透手机免打扰，可能响铃）</b></span>
        </div>
        <div class="row">
          <button class="ghost" id="btn-gotify-test" onclick="gotifyTest()">📤 发一条测试推送</button>
          <span class="hint">用上面<b>当前填的值</b>试推（不用先保存）——地址错 / Token 错当场就能看出来</span>
        </div>
        <div class="hint" id="gotify-msg"></div>
        <div class="hint" id="gotify-detail" style="white-space:pre-wrap"></div>
        <div class="hint" style="margin-top:8px">
          地址也可以填内网域名（要带 http://，如 <code>http://gotify.local</code>）。
          保存后主程序 5 秒内热重载生效，不用重启。<br>
          还没装 Gotify？本项目只管往它推，它自己用 docker 一行命令就起来了。
        </div>
      </div>
    </div>
  </div>

  <div class="card pool">
    <h2>📚 共享关键词池（KEYWORD_POOL）</h2>
    <textarea id="pool" placeholder="每行一个关键词，回车换行（也可用逗号分隔）"></textarea>
    <div class="hint cnt" id="poolcount"></div>
    <div class="hint bad" id="poolwarn" hidden></div>
    <div class="hint" style="margin-top:6px">
      <b>怎么分隔词：回车换行＝一个新词</b>；逗号也算（英文 <code>,</code> 或中文 <code>，</code>）；
      <b>空格不算分隔符</b>——「9929 TRI」会被当成一个词，别用空格隔词。<br>
      命中规则：消息包含池中任一词（不区分大小写）即推送。<br>
      哪个频道不想走这个池子（要全量转发），去下面「📺 监听频道」里把它的「走关键词过滤」取消勾选。
    </div>
  </div>

  <div class="card">
    <h2>📺 监听频道（SOURCES）</h2>
    <div class="legend">
      <b>参数怎么用：</b>
      <span class="lb">✓ <b>启用</b></span>不勾＝这条频道<b>暂时静默</b>（一条都不推，配置留着，随时勾回来）<br>
      <span class="lb">✓ <b>走关键词过滤</b></span>不勾＝<b>全量转发</b>（这条频道的所有消息都推，不筛关键词）<br>
      <span class="lb"><b>私有额外关键词</b></span>跟共享池是「<b>或</b>」：命中池子里的词、或这里的词，任一就推<br>
      <span class="lb"><b>排除黑名单</b></span>不管上面勾没勾，命中这里就拦下不推（优先级最高）<br>
      <span class="lb"><b>词的写法</b></span>一行一个（回车换行），也可用逗号；<b>空格不算分隔符</b>
    </div>
    <div id="sources"></div>

    <div class="addzone" id="addzone">
      <div class="addhead" onclick="toggleAdd()">
        <span>➕ 新增监控频道</span>
        <span class="hint" id="add-hint" style="margin-left:auto;font-weight:400">点这里展开</span>
        <span class="caret">▾</span>
      </div>
      <div class="addbody" id="addbody" hidden>
        <div class="row" style="margin-top:0">
          <input type="text" id="new-key" placeholder="频道 username（直接粘 t.me 链接也行，如 vpsme2_bot）">
          <input type="text" id="new-label" placeholder="显示名称（可选，方便自己认，如 VPSME补货）">
          <button class="ghost" onclick="addSrc()">添加</button>
        </div>
        <div class="hint" style="margin-top:8px">
          频道 username 不带 @；直接粘 <code>https://t.me/xxx</code> / <code>@xxx</code> / <code>tg://resolve?domain=xxx</code>
          都行，会自动只留用户名。<br>
          新增/删除都要点底部「保存全部」才写入生效。
        </div>
      </div>
    </div>
  </div>
</div>

<div class="bar">
  <button class="primary" id="save" onclick="save()">保存全部</button>
  <span id="msg"></span>
  <a class="logout" href="/tg-login">📱 TG 登录</a>
  <a class="logout" href="/logout" style="margin-left:14px"
     title="只清掉本浏览器里的登录 cookie：TG 登录态（session 文件）和 Gotify token 都不动">退出登录</a>
</div>

<script>
let CFG = null;
// 运行状态 / 出站代理两张卡默认收起，点标题一行展开、再点收起
function toggleCard(id) { document.getElementById(id).classList.toggle('collapsed'); }
const esc = s => String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;')
                             .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const kwJoin = a => (a||[]).join("\\n");
const kwSplit = s => s.split(/\\n|,|，/).map(x=>x.trim()).filter(Boolean);

// 分词结果现场回显：直接告诉用户「这几个字符被当成了几个词」，
// 免得再纠结是回车还是空格（分隔规则：回车换行 / 逗号，空格不分割）
function cntText(v) {
  const a = kwSplit(v);
  if (!a.length) return '分词结果：0 个（空）';
  return '分词结果：' + a.length + ' 个 → ' + a.slice(0, 8).join(' / ') + (a.length > 8 ? ' …' : '');
}
document.addEventListener('input', e => {
  const t = e.target;
  if (!t || t.tagName !== 'TEXTAREA') return;
  const box = t.parentElement.querySelector('.cnt');
  if (box) box.textContent = cntText(t.value);
  if (t.id === 'pool') poolWarn();
});
function refreshCnts() {
  document.querySelectorAll('textarea').forEach(t => {
    const box = t.parentElement.querySelector('.cnt');
    if (box) box.textContent = cntText(t.value);
  });
  poolWarn();
}

// 池子空着、却有频道勾了「走关键词过滤」→ 那些频道实际上一条都不会推，当场提醒
function poolWarn() {
  const el = document.getElementById('poolwarn');
  if (!el) return;
  const n = kwSplit(document.getElementById('pool').value).length;
  const using = [...document.querySelectorAll('#sources .f-usepool')].filter(c => c.checked).length;
  if (n === 0 && using > 0) {
    el.hidden = false;
    el.textContent = '⚠️ 池子是空的，但有 ' + using + ' 个频道勾着「走关键词过滤」—— 它们一条消息都不会推。'
                   + '要么往池子里加词，要么把那些频道改成全量转发。';
  } else {
    el.hidden = true;
  }
}

// 频道名瘦身：不管粘的是 @xxx / t.me/xxx / https://t.me/xxx / tg://resolve?domain=xxx，
// 都只留下真正的 username（主程序就是拿 username 对消息来源的，带链接永远匹配不上）
function cleanKey(raw) {
  let k = String(raw ?? '').trim();
  k = k.replace(/^(?:https?|tg):[/]{2}/i, '');                    // 去掉 https:// 、 tg://
  k = k.replace(/^(?:www[.])?(?:t[.]me|telegram[.]me)[/]/i, '');  // 去掉 t.me/
  k = k.replace(/^resolve[?]domain=/i, '');                       // 去掉 resolve?domain=
  k = k.replace(/^@+/, '');
  k = k.split(/[/?#]/)[0];                                        // 只留第一段（丢掉路径 / ?si=xxx）
  return [...k].filter(ch => ' \t\u00a0'.indexOf(ch) < 0).join('').toLowerCase();
}

// 老配置里如果存着链接写法，展开页面时也顺手整理成用户名（不保存不落盘）
function normalizeSources(srcs) {
  const out = {}; let changed = false;
  for (const [k, v] of Object.entries(srcs || {})) {
    const nk = cleanKey(k);
    if (!nk) { out[k] = v; continue; }        // 认不出来的原样留着，不擅自丢
    if (out[nk]) { changed = true; continue; } // 瘦身后撞名的，保留先出现的那个
    if (nk !== k) changed = true;
    out[nk] = v;
  }
  normalizeSources.changed = changed;
  return out;
}

function srcCard(key, s) {
  const el = document.createElement('div');
  el.className = 'src';
  el.dataset.key = key;
  el.innerHTML = `
    <div class="src-head">
      <span class="key">@${esc(key)}</span>
      <input type="text" class="f-label" value="${esc(s.label)}" placeholder="显示名称">
      <label class="ck" title="不勾＝这条频道暂时静默：一条都不推，配置留着"><input type="checkbox" class="f-enabled" ${s.enabled?'checked':''}> 启用</label>
      <span class="hint f-s1"></span>
      <label class="ck" title="不勾＝全量转发：这条频道所有消息都推，不筛关键词"><input type="checkbox" class="f-usepool" ${s.use_pool?'checked':''}> 走关键词过滤</label>
      <span class="hint f-s2"></span>
      <button class="del" title="删除该频道（保存后生效）" onclick="delSrc(this)">🗑 删除</button>
    </div>
    <div class="row">
      <div style="flex:1">
        <span class="hint">私有额外关键词（与池子是「或」的关系，每行一个）</span>
        <textarea class="f-extra">${esc(kwJoin(s.extra_keywords))}</textarea>
        <div class="hint cnt"></div>
      </div>
      <div style="flex:1">
        <span class="hint">排除黑名单（两种模式下都生效，每行一个）</span>
        <textarea class="f-exclude">${esc(kwJoin(s.exclude_keywords))}</textarea>
        <div class="hint cnt"></div>
      </div>
    </div>`;

  // 参数现状回显：只有「不常用」的状态才显字，平时不啰唆
  const s1 = el.querySelector('.f-s1'), s2 = el.querySelector('.f-s2');
  const sync = () => {
    const on = el.querySelector('.f-enabled').checked,
          pool = el.querySelector('.f-usepool').checked;
    s1.textContent = on ? '' : '（当前：静默中，一条都不推）';
    s1.className = 'hint f-s1' + (on ? '' : ' bad');
    s2.textContent = pool ? '' : '（当前：全量转发，所有消息都推）';
    s2.className = 'hint f-s2' + (pool ? '' : ' bad');
  };
  el.querySelector('.f-enabled').addEventListener('change', sync);
  el.querySelector('.f-usepool').addEventListener('change', sync);
  sync();
  return el;
}

let deleteConfirmed = false;   // 本页内只弹一次确认，后续删除直接执行

function delSrc(btn) {
  if (!deleteConfirmed) {
    if (!confirm('确定删除这个频道？点「保存全部」后才会真正写盘生效（本次会话内后续删除不再弹窗）')) return;
    deleteConfirmed = true;
  }
  btn.closest('.src').remove();
  refreshCnts();             // 删掉一条后重算「池子空 / 有频道走过滤」的提醒
  msgShow('ok', '已从列表移除（未保存）。改动要点「保存全部」才写入并生效');
}

// 新增频道区：常态收起（只留一整行绿框），点标题一行展开；刚加完自动收起
function toggleAdd(forceOpen) {
  const z = document.getElementById('addzone'), b = document.getElementById('addbody');
  const open = (forceOpen === undefined) ? b.hidden : !!forceOpen;
  b.hidden = !open;
  z.classList.toggle('open', open);
  document.getElementById('add-hint').textContent = open ? '点这里收起' : '点这里展开';
  if (open) document.getElementById('new-key').focus();
}

function addSrc() {
  const kEl = document.getElementById('new-key'), lEl = document.getElementById('new-label');
  const raw = kEl.value.trim();
  const key = cleanKey(raw);
  if (!key) { alert('请填频道 username（如 vpsme2_bot），也可以直接粘 t.me 链接'); return; }
  if ([...document.querySelectorAll('#sources .src')].some(el => el.dataset.key === key)) {
    alert('该频道已存在：@' + key); return;
  }
  const s = { label: lEl.value.trim(), enabled: true, use_pool: true,
              extra_keywords: [], exclude_keywords: [] };
  CFG.SOURCES[key] = s;
  const card = srcCard(key, s);
  card.classList.add('new');            // 高亮：这一行是刚加的，还没保存
  document.getElementById('sources').appendChild(card);
  card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  kEl.value = ''; lEl.value = '';
  refreshCnts();
  msgShow('ok', '已添加 @' + key + (raw && raw !== key ? '（已从链接里取出用户名）' : '')
                + '，点「保存全部」后写入并生效');
  toggleAdd(false);        // 加完把新增区收回去，页面回到干净状态
}

function msgShow(cls, text) {
  const msg = document.getElementById('msg');
  msg.textContent = text; msg.className = cls;
}

// ---------- 出站代理 ----------
function proxySummary() {
  const el = document.getElementById('proxy-sum');
  if (!document.getElementById('proxy-on').checked) {
    el.textContent = '直连（不走代理）'; el.className = 'sum';
    return;
  }
  const t = document.getElementById('proxy-type').value,
        h = document.getElementById('proxy-host').value.trim() || '（地址没填）',
        p = document.getElementById('proxy-port').value.trim();
  el.textContent = '走代理 ' + t + ' ' + h + (p ? ':' + p : '');
  el.className = 'sum ok';
}
['proxy-host', 'proxy-port', 'proxy-type'].forEach(id => {
  const el = document.getElementById(id);
  el.addEventListener('input', proxySummary);
  el.addEventListener('change', proxySummary);
});
function proxyGate() {
  const on = document.getElementById('proxy-on').checked;
  ['proxy-type', 'proxy-host', 'proxy-port'].forEach(
    id => { document.getElementById(id).disabled = !on; });
  proxySummary();
}
function proxyPayload() {
  return {
    enabled: document.getElementById('proxy-on').checked,
    type: document.getElementById('proxy-type').value,
    host: document.getElementById('proxy-host').value.trim(),
    port: document.getElementById('proxy-port').value.trim(),
  };
}
function loadProxy(px) {
  px = px || {};
  document.getElementById(px.enabled ? 'proxy-on' : 'proxy-off').checked = true;
  document.getElementById('proxy-type').value = px.type || 'socks5';
  document.getElementById('proxy-host').value = px.host || '';
  document.getElementById('proxy-port').value = px.port || '';
  proxyGate();
}

// ---------- 通知渠道（Gotify）----------
function gotifySummary() {
  const el = document.getElementById('notify-sum');
  const url = document.getElementById('gotify-url').value.trim();
  const token = document.getElementById('gotify-token').value.trim();
  const prio = document.getElementById('gotify-priority').value.trim() || '8';
  if (!url || !token) {
    el.textContent = '⚠️ 还没配全（缺地址或 Token）'; el.className = 'sum bad';
    return;
  }
  el.textContent = 'Gotify ' + url.replace(/^https?:[/]{2}/, '') + ' · 优先级 ' + prio;
  el.className = 'sum ok';
}
function gotifyPayload() {
  return {
    url: document.getElementById('gotify-url').value.trim(),
    token: document.getElementById('gotify-token').value.trim(),
    priority: document.getElementById('gotify-priority').value.trim() || '8',
  };
}
function loadGotify(g) {
  g = g || {};
  document.getElementById('gotify-url').value = g.url || '';
  document.getElementById('gotify-token').value = g.token || '';
  document.getElementById('gotify-priority').value =
    (g.priority === undefined || g.priority === null) ? 8 : g.priority;
  gotifySummary();
}
['gotify-url', 'gotify-token', 'gotify-priority'].forEach(id => {
  const el = document.getElementById(id);
  el.addEventListener('input', gotifySummary);
  el.addEventListener('change', gotifySummary);
});
document.getElementById('gotify-show').addEventListener('change', e => {
  document.getElementById('gotify-token').type = e.target.checked ? 'text' : 'password';
});

// 测试推送：用表单里当前的值直接推一条（不用先保存），成败当场回显
async function gotifyTest() {
  const msg = document.getElementById('gotify-msg'), det = document.getElementById('gotify-detail');
  const btn = document.getElementById('btn-gotify-test');
  msg.className = 'hint'; msg.textContent = '正在发送 …'; det.textContent = '';
  btn.disabled = true;
  try {
    const r = await fetch('/api/gotify-test', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(gotifyPayload())
    });
    if (r.status === 401) { location.href = '/login'; return; }
    const d = await r.json();
    if (d.error) { msg.textContent = '❌ ' + d.error; msg.className = 'hint bad'; }
    else {
      msg.textContent = (d.ok ? '✅ ' : '❌ ') + (d.message || '');
      msg.className = 'hint ' + (d.ok ? 'ok' : 'bad');
      det.textContent = d.detail || '';
    }
  } catch (e) {
    msg.textContent = '❌ 请求失败：' + e.message; msg.className = 'hint bad';
  }
  btn.disabled = false;
}

async function load() {
  const r = await fetch('/api/config');
  if (r.status === 401) { location.href = '/login'; return; }
  CFG = await r.json();
  CFG.SOURCES = normalizeSources(CFG.SOURCES);
  document.getElementById('pool').value = kwJoin(CFG.KEYWORD_POOL);
  loadProxy(CFG.PROXY);
  loadGotify(CFG.GOTIFY);
  const box = document.getElementById('sources');
  box.innerHTML = '';
  for (const [key, s] of Object.entries(CFG.SOURCES)) box.appendChild(srcCard(key, s));
  refreshCnts();
  if (normalizeSources.changed)
    msgShow('ok', '有频道的写法被整理成了用户名（比如 t.me 链接）—— 点「保存全部」落盘生效');
}

async function save() {
  const btn = document.getElementById('save'), msg = document.getElementById('msg');
  btn.disabled = true; msg.textContent = '保存中…'; msg.className = '';
  const SOURCES = {};
  document.querySelectorAll('#sources .src').forEach(el => {
    SOURCES[el.dataset.key] = {
      label: el.querySelector('.f-label').value.trim(),
      enabled: el.querySelector('.f-enabled').checked,
      use_pool: el.querySelector('.f-usepool').checked,
      extra_keywords: kwSplit(el.querySelector('.f-extra').value),
      exclude_keywords: kwSplit(el.querySelector('.f-exclude').value),
    };
  });
  try {
    const r = await fetch('/api/save', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ KEYWORD_POOL: kwSplit(document.getElementById('pool').value), SOURCES,
                             PROXY: proxyPayload(), GOTIFY: gotifyPayload() })
    });
    if (r.status === 401) { location.href = '/login'; return; }
    if (!r.ok) throw new Error((await r.json()).error || ('HTTP ' + r.status));
    msg.textContent = '✅ 已保存，主程序将在 5 秒内热重载'; msg.className = 'ok';
    document.querySelectorAll('#sources .src.new').forEach(el => el.classList.remove('new'));
    gotifySummary();
  } catch (e) {
    msg.textContent = '❌ 保存失败: ' + e.message; msg.className = 'err';
  }
  btn.disabled = false;
}

// ---------- 运行状态（TG 登录态 + 后端主程序）----------
function fmtDur(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600),
        m = Math.floor((sec % 3600) / 60);
  if (d) return d + ' 天 ' + h + ' 小时';
  if (h) return h + ' 小时 ' + m + ' 分';
  if (m) return m + ' 分钟';
  return sec + ' 秒';
}
function fmtAge(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  if (sec < 60) return sec + ' 秒前';
  if (sec < 3600) return Math.floor(sec / 60) + ' 分钟前';
  return Math.floor(sec / 3600) + ' 小时前';
}
function renderState(s) {
  const b = s.backend || {};
  const running = (b.running === undefined) ? !!s.main_running : !!b.running;

  // ① TG 登录态
  const line = document.getElementById('tgline');
  let text, cls = '';
  if (s.logged_in === true) { text = '✅ TG 登录：已登录'; cls = 'ok'; }
  else if (s.logged_in === false) { text = '⚠️ TG 登录：未登录（主程序起不来，先去登录）'; cls = 'bad'; }
  else { text = s.checking ? '⏳ TG 登录：检查中 …' : '❓ TG 登录：查不到（代理 / 网络不通？）'; cls = 'bad'; }
  if (s.user) text += '　账号：' + s.user;
  line.textContent = text;
  line.className = 'stat ' + cls;

  // ② 后端主程序运行态（看心跳，能分辨「进程在」和「真的在干活」）
  const bl = document.getElementById('beeline');
  let bt, bc = '';
  if (running && b.stale) {
    bt = '⚠️ 后端程序：进程在（PID ' + b.pid + '）但心跳停了 ' + fmtAge(b.heartbeat_age)
       + '，可能卡住 / 掉线，建议重启';
    bc = 'bad';
  } else if (running) {
    bt = '✅ 后端程序：运行中（PID ' + b.pid + (b.uptime ? '，已跑 ' + fmtDur(b.uptime) : '') + '）';
    if (b.sources != null) bt += '　监听 ' + (b.sources_enabled || 0) + '/' + b.sources + ' 个来源';
    if (b.heartbeat_age != null) bt += '　心跳 ' + fmtAge(b.heartbeat_age);
    bc = 'ok';
  } else {
    bt = '⚠️ 后端程序：未运行（补货监控现在是停的）';
    bc = 'bad';
  }
  bl.textContent = bt;
  bl.className = 'stat ' + bc;

  const parts = [];
  if (s.note) parts.push(s.note);
  if (b.note) parts.push(b.note);
  if (b.version) parts.push('后端版本 ' + b.version);
  if (running && b.proxy) parts.push('走代理：' + b.proxy);
  document.getElementById('tgmeta').textContent = parts.join('　｜　');
  document.getElementById('tgwhen').textContent =
    s.checked_at ? ('TG 登录态上次检查：' + new Date(s.checked_at * 1000).toLocaleTimeString()) : '';

  // 收起时也能一眼看到状态（想看详情点标题一行）
  const sum = document.getElementById('state-sum');
  if (!running) sum.textContent = '⚠️ 监控没在跑';
  else if (b.stale) sum.textContent = '⚠️ 进程在（PID ' + b.pid + '）但心跳停了';
  else sum.textContent = '✅ 监控运行中（PID ' + b.pid + '）';
  const tag = (s.logged_in === true) ? '　TG 已登录'
            : (s.logged_in === false ? '　TG 没登录' : '');
  sum.textContent += tag;
  sum.className = 'sum ' + ((running && !b.stale) ? 'ok' : 'bad');

  // 开机自启提醒：网页按钮 / 命令行拉起的后端，重启机器后不会自己回来
  const auto = document.getElementById('autostart');
  const START_CMDS = 'sudo cp tg2gotify.service /etc/systemd/system/ && sudo systemctl daemon-reload'
                   + ' && sudo systemctl enable --now tg2gotify';
  if (running && b.unit) {
    auto.className = 'hint';
    auto.textContent = '✅ 由 systemd 管理（' + b.unit + '）—— 开机自启、掉线自动拉起；不想自启就：sudo systemctl disable --now '
                     + String(b.unit).replace(/[.]service$/, '');
  } else if (running) {
    auto.className = 'hint bad';
    auto.textContent = '⚠️ 这个后端是网页按钮 / 命令行拉起来的，重启服务器后不会自己回来。'
                     + '要开机自启就在服务器上执行：' + START_CMDS + '（之后页面的启停按钮就是在操作 systemd 服务）。';
  } else {
    auto.className = 'hint';
    auto.textContent = '提示：页面上「启动后端」拉起的进程重启服务器后不会自己回来；'
                     + '要开机自启就用 systemd：' + START_CMDS;
  }

  // 按钮跟着状态走：没跑才能启，在跑才能停
  document.getElementById('btn-start').hidden = running;
  document.getElementById('btn-stop').hidden = !running;
}

async function backendAct(action) {
  if (action === 'stop' && !confirm('停掉后端？补货监控会中断（需要时再点「启动后端」拉起来）。')) return;
  const msg = document.getElementById('backendmsg'), log = document.getElementById('backendlog');
  msg.className = 'hint'; log.textContent = '';
  msg.textContent = (action === 'start' ? '正在启动，等它自己报状态 …' : '正在停止 …');
  document.getElementById('btn-start').disabled = true;
  document.getElementById('btn-stop').disabled = true;
  try {
    const r = await fetch('/api/backend', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ action: action })
    });
    if (r.status === 401) { location.href = '/login'; return; }
    const d = await r.json();
    if (d.error) { msg.textContent = '❌ ' + d.error; msg.className = 'hint bad'; }
    else {
      // pending = 进程拉起来了但还没写心跳（不一定是失败，别打绿勾也别打红叉）
      msg.textContent = (d.pending ? '⏳ ' : (d.ok ? '✅ ' : '❌ ')) + (d.message || '');
      msg.className = 'hint ' + (d.pending ? '' : (d.ok ? 'ok' : 'bad'));
      log.textContent = d.detail || '';
    }
  } catch (e) {
    msg.textContent = '❌ 请求失败：' + e.message; msg.className = 'hint bad';
  }
  document.getElementById('btn-start').disabled = false;
  document.getElementById('btn-stop').disabled = false;
  pollState();
}
async function pollState() {
  try {
    const r = await fetch('/api/tg-state');
    if (r.status === 401) { location.href = '/login'; return; }
    renderState(await r.json());
  } catch (e) { /* 网络抖动，下次再拉 */ }
}

load();
pollState();
setInterval(pollState, 5000);
</script>
</body>
</html>"""

_LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>TG2Gotify 监控脚本 · 配置登录</title>
<style>
  body { font-family:system-ui,"PingFang SC","Microsoft YaHei",sans-serif; background:#f7f6f3;
         display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }
  .box { background:#fff; border:1px solid #e3e0da; border-radius:10px; padding:28px 32px; width:320px; }
  h1 { font-size:16px; margin:0 0 14px; }
  input { width:100%; padding:9px 10px; font-size:14px; border:1px solid #e3e0da;
          border-radius:6px; box-sizing:border-box; }
  button { width:100%; margin-top:12px; background:#2f6f4f; color:#fff; border:0;
           border-radius:8px; padding:10px; font-size:14px; cursor:pointer; }
</style></head>
<body>
<div class="box">
  <h1>📡 TG2Gotify 监控脚本 · 配置登录</h1>
  <form method="get" action="/login">
    <input name="token" placeholder="访问令牌（见服务器终端输出）" autofocus>
    <button>登录</button>
  </form>
</div>
</body></html>"""


def _read_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_config(cfg: dict) -> None:
    """原子写回 config.json（先写临时文件再 rename，防写一半损坏）。
    写之前把上一版留一份 config.json.bak：网页上误存 / 测试写坏了还能捡回来。
    临时文件按 600 建：配置里有 API_HASH 和 Gotify token，不给别的本机用户读
    （os.replace 会把权限带过去，所以每次保存后不会变回 644）。"""
    tmp = CONFIG_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    try:
        os.chmod(tmp, 0o600)          # umask 再宽松也按 600 落盘
    except OSError:
        pass
    try:
        if os.path.exists(CONFIG_PATH):
            shutil.copy2(CONFIG_PATH, CONFIG_PATH + ".bak")
    except OSError:
        pass                      # 备份失败不该拦着保存
    os.replace(tmp, CONFIG_PATH)


def _ensure_webui_token() -> str:
    """读 WEBUI_TOKEN；没有就生成一个写回配置并打印（首次启动）。"""
    cfg = _read_config()
    token = str(cfg.get("WEBUI_TOKEN") or "").strip()
    if token:
        return token
    token = secrets.token_urlsafe(16)
    cfg["WEBUI_TOKEN"] = token
    _write_config(cfg)
    print("=" * 56)
    print(f"[webui] 已自动生成访问令牌并写入 config.json:")
    print(f"        {token}")
    print("        （WebUI 登录用；泄露可在 config.json 里改掉重生成）")
    print("=" * 56)
    return token


def _valid_sources(payload: dict, existing: dict | None = None) -> dict:
    """校验并规整前端提交的 SOURCES —— 直接复用主程序的 normalize_source，
    保证「保存落盘的字段」与「主程序实际解释的字段」是同一套规则。
    来源键统一去 @、去首尾空白、转小写（主程序匹配来源时就是拿小写 username
    对键名做精确匹配，小写键才走得上这条路，大写键只能靠标题模糊兑底）。
    existing = 盘上现有的 SOURCES：网页不认识的字段（手写在频道里的额外键）原样保留，
    不因为保存一次就被抹掉；老的 v1 字段 keywords 除外（它已被迁移到 extra_keywords）。"""
    from filter import normalize_source
    if not isinstance(payload, dict):
        raise ValueError("SOURCES 必须是对象")
    existing = existing if isinstance(existing, dict) else {}
    out = {}
    for key, raw in payload.items():
        if not isinstance(key, str):
            raise ValueError("SOURCES 的键必须是字符串")
        k = key.strip().lstrip("@").lower()
        if not k:
            raise ValueError("SOURCES 存在空的来源键")
        if any(c.isspace() for c in k):
            raise ValueError(f"来源键 {k!r} 不能包含空白字符")
        if len(k) > 64:
            raise ValueError(f"来源键 {k!r} 过长（>64 字符）")
        if not isinstance(raw, dict):
            raise ValueError(f"来源 {k} 配置必须是对象")
        item = normalize_source(raw)
        old = existing.get(k)
        if isinstance(old, dict):
            for kk, vv in old.items():
                if kk not in item and kk != "keywords":
                    item[kk] = vv
        out[k] = item
    return out


def _proxy_view(cfg: dict) -> dict:
    """把 config 里的 PROXY 规整成页面好用的样子（缺失/写坏时给默认值）。"""
    p = cfg.get("PROXY")
    p = p if isinstance(p, dict) else {}
    try:
        port = int(p.get("port") or 7890)
    except (TypeError, ValueError):
        port = 7890
    return {"enabled": bool(p.get("enabled", True)),
            "type": str(p.get("type") or "socks5"),
            "host": str(p.get("host") or "127.0.0.1"),
            "port": port}


def _valid_proxy(payload, existing: dict | None = None) -> dict:
    """校验页面提交的代理设置。选「直连」时不校验地址/端口（但照旧存着，方便来回切）。
    Telethon（python-socks）支持的代理类型：socks5 / socks4 / http。
    existing = 盘上现有的 PROXY：手写进去的额外键（如代理用户名）原样保留。"""
    if not isinstance(payload, dict):
        raise ValueError("PROXY 必须是对象")
    enabled = bool(payload.get("enabled"))
    ptype = str(payload.get("type") or "socks5").strip().lower()
    if ptype not in ("socks5", "socks4", "http"):
        raise ValueError("代理类型只能是 socks5 / socks4 / http")
    host = str(payload.get("host") or "").strip()
    port_raw = str(payload.get("port") or "").strip()
    try:
        port = int(port_raw) if port_raw else 0
    except ValueError:
        raise ValueError("代理端口要填数字（如 7890）")
    if enabled:
        if not host:
            raise ValueError("选了「走代理」就得填代理地址（如 127.0.0.1）")
        if any(c.isspace() for c in host):
            raise ValueError("代理地址不能带空格")
        if not (1 <= port <= 65535):
            raise ValueError("代理端口要在 1-65535 之间")
    out = {"enabled": enabled, "type": ptype,
           "host": host or "127.0.0.1", "port": port or 7890}
    if isinstance(existing, dict):
        for k, v in existing.items():
            if k not in out:
                out[k] = v            # 手写在 config.json 里的键（如 username）不因保存丢失
    return out


def _gotify_view(cfg: dict) -> dict:
    """把 config 里的 Gotify 设置规整成页面好用的样子。"""
    try:
        prio = int(cfg.get("GOTIFY_PRIORITY") or 8)
    except (TypeError, ValueError):
        prio = 8
    return {"url": str(cfg.get("GOTIFY_URL") or "").strip().rstrip("/"),
            "token": str(cfg.get("GOTIFY_TOKEN") or "").strip(),
            "priority": prio}


def _valid_gotify(payload) -> dict:
    """校验页面提交的 Gotify 设置。允许留空（还没部署 Gotify 的人能先存别的配置），
    但填了就得是像样的 http(s) 地址、Token 不带空白、优先级 0-10。"""
    if not isinstance(payload, dict):
        raise ValueError("GOTIFY 必须是对象")
    url = str(payload.get("url") or "").strip().rstrip("/")
    token = str(payload.get("token") or "").strip()
    raw = str(payload.get("priority") or "").strip()
    try:
        prio = int(raw) if raw else 8
    except ValueError:
        raise ValueError("推送优先级要填数字（0-10）")
    if not (0 <= prio <= 10):
        raise ValueError("推送优先级要在 0-10 之间（8-10 会穿透手机免打扰）")
    if url:
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("Gotify 地址要以 http:// 或 https:// 开头（如 http://192.168.1.100:8080）")
        if any(c.isspace() for c in url):
            raise ValueError("Gotify 地址不能带空格")
    if token and any(c.isspace() for c in token):
        raise ValueError("Gotify Token 不能带空白字符（多半是复制时把换行/空格也带上了）")
    return {"url": url, "token": token, "priority": prio}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """测试推送不跟随重定向。
    跟随跳转有两个坑：① 请求被带到另一个地址，对方的响应还会被当成「推送成功」；
    ② 等于开了个「借 WebUI 打内网任意 http 地址」的口子。宁可报错让用户改地址。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# 显式禁用环境变量代理（Gotify 在局域网，强制直连，跟主程序一致）+ 关掉自动重定向
_push_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def gotify_test_push(url: str, token: str, priority: int = 8) -> dict:
    """「发一条测试推送」按钮背后：拿给定参数直接往 Gotify 推一条。
    返回 {ok, message, detail}；错误分得清（没填 / 连不上 / 401 token 错 / 地址指到了别的服务）。
    Gotify 在局域网 → 本机直连、不走代理（也不跟随重定向）；纯标准库，不引新依赖。"""
    url = str(url or "").strip().rstrip("/")
    token = str(token or "").strip()
    if not url:
        return {"ok": False, "message": "还没填 Gotify 地址",
                "detail": "例：http://192.168.1.100:8080"}
    if not token:
        return {"ok": False, "message": "还没填应用 Token",
                "detail": "Gotify 网页 → Apps → CREATE APPLICATION → 复制那串 token"}
    if not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "message": "地址要以 http:// 或 https:// 开头", "detail": url}
    try:
        prio = int(priority)
    except (TypeError, ValueError):
        prio = 8
    target = url + "/message?token=" + urllib.parse.quote(token)
    body = json.dumps({"title": "TG2Gotify 测试推送",
                       "message": "看到这条就说明推送渠道通了 ✅",
                       "priority": prio}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(target, data=body, method="POST", headers={
        "Content-Type": "application/json; charset=utf-8"})
    try:
        with _push_opener.open(req, timeout=8) as r:
            raw = r.read(4000).decode("utf-8", "ignore")
            code = r.status
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read(2000).decode("utf-8", "ignore").strip()
        except Exception:
            pass
        hint = ""
        if e.code in (301, 302, 303, 307, 308):
            hint = "（地址被重定向了：多半是 http/https 写错、或端口不对；测试推送不跟随跳转）"
        elif e.code == 401:
            hint = "（token 不对：Gotify 里那个应用 token 是不是没复制全？）"
        elif e.code == 404:
            hint = "（地址多半指到了别的服务，Gotify 的推送接口是 /message）"
        return {"ok": False, "message": "Gotify 返回 HTTP %d%s" % (e.code, hint),
                "detail": detail[:400]}
    except urllib.error.URLError as e:
        return {"ok": False, "message": "连不上 Gotify：%s" % (getattr(e, "reason", e),),
                "detail": "地址/端口写对了吗？Gotify 服务在跑吗？"}
    except Exception as e:
        return {"ok": False, "message": "发送失败：%s: %s" % (type(e).__name__, e), "detail": ""}
    if raw.lstrip()[:1] == "{":          # 正常 Gotify 会回一个带 id 的 JSON
        return {"ok": True, "message": "已发出，Gotify 上应该收到了",
                "detail": "HTTP %d ← %s" % (code, url)}
    return {"ok": False, "message": "这个地址回的好像不是 Gotify（返回的是网页/文本）",
            "detail": raw[:200]}


class Handler(BaseHTTPRequestHandler):
    server_version = "TG2GotifyWebUI/2.0"
    _token: str = ""  # 类属性，serve() 里赋值

    # ---------- 工具 ----------
    def _send(self, code: int, body: bytes, ctype: str, extra_headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code: int, text: str, extra_headers=None):
        self._send(code, text.encode("utf-8"), "text/html; charset=utf-8", extra_headers)

    def _json(self, code: int, obj: dict, extra_headers=None):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", extra_headers)

    def _authed(self) -> bool:
        """cookie 里带的是登录时发的随机会话 id（不是长期令牌本身）。"""
        c = http_cookies.SimpleCookie(self.headers.get("Cookie", ""))
        sid = c[COOKIE_NAME].value if COOKIE_NAME in c else ""
        if _session_ok(sid):
            return True
        who = self.client_address[0] if self.client_address else "?"
        log.warning("未认证的请求已拒绝：%s %s（来自 %s）", self.command, self.path, who)
        return False

    def _redirect_auth(self, sid: str) -> None:
        """登录成功：发一个随机会话 cookie 并跳回首页。"""
        self._html(303, "", extra_headers=[
            ("Location", "/"),
            ("Set-Cookie", f"{COOKIE_NAME}={sid}; Path=/; HttpOnly; SameSite=Lax"),
        ])

    def log_message(self, fmt, *args):  # 静默默认访问日志，出错仍打
        pass

    # ---------- 路由 ----------
    def do_GET(self):
        url = urlparse(self.path)
        path, qs = url.path, parse_qs(url.query)

        if path == "/logout":
            c = http_cookies.SimpleCookie(self.headers.get("Cookie", ""))
            if COOKIE_NAME in c:
                _drop_session(c[COOKIE_NAME].value)     # 服务端也把这个会话丢棃
            self._html(303, "", extra_headers=[
                ("Location", "/login"),
                ("Set-Cookie", f"{COOKIE_NAME}=; Path=/; Max-Age=0"),
            ])
            return

        if path == "/login":
            token_param = (qs.get("token") or [""])[0].strip()
            if token_param and secrets.compare_digest(token_param, self._token):
                self._redirect_auth(_issue_session())       # 发随机会话，不把令牌本身当 cookie
            else:
                self._html(200, _LOGIN_PAGE)
            return

        if path in ("/", "/index.html"):
            if not self._authed():
                self._html(303, "", extra_headers=[("Location", "/login")])
                return
            self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return

        if path == "/tg-login":           # 网页端 TG 登录页（扫码 / 验证码）
            if not self._authed():
                self._html(303, "", extra_headers=[("Location", "/login")])
                return
            self._send(200, tglogin.PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return

        if path == "/api/tg-login/status":
            if not self._authed():
                self._json(401, {"error": "未登录"})
                return
            try:
                self._json(200, tglogin.LOGIN.status())
            except Exception as e:
                self._json(500, {"error": "读取登录状态失败: %s" % e})
            return

        if path == "/api/tg-state":          # 首页要看的：TG 到底登录了没 + 主程序在不在跑
            if not self._authed():
                self._json(401, {"error": "未登录"})
                return
            try:
                self._json(200, tglogin.get_state())
            except Exception as e:
                self._json(500, {"error": "读取运行状态失败: %s" % e})
            return

        if path == "/api/config":
            if not self._authed():
                self._json(401, {"error": "未登录"})
                return
            try:
                cfg = _read_config()
            except Exception as e:
                self._json(500, {"error": f"config.json 读取/解析失败: {e}"
                                           "（配置可能被手工改坏，修好再刷新）"})
                return
            from filter import normalize_source
            # 老 v1 配置（没写 KEYWORD_POOL）先做迁移视图，否则页面看不到老
            # keywords、一保存就会把它们丢掉；再逐源规整成标准四字段，
            # 保证返回给页面的每条都带齐 enabled/use_pool，保存不会丢参数
            if "KEYWORD_POOL" not in cfg:
                from filter import migrate_pool
                migrate_pool(cfg)
            cfg["SOURCES"] = {k: normalize_source(v) for k, v in cfg.get("SOURCES", {}).items()}
            self._json(200, {"KEYWORD_POOL": cfg.get("KEYWORD_POOL", []),
                             "SOURCES": cfg.get("SOURCES", {}),
                             "PROXY": _proxy_view(cfg),
                             "GOTIFY": _gotify_view(cfg)})
            return

        self._html(404, "404 Not Found")

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/api/tg-login/action":   # 登录指令：起流程 / 提交验证码 / 取消
            if not self._authed():
                self._json(401, {"error": "未登录"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 64 * 1024:
                    raise ValueError("请求体过大")
                raw = self.rfile.read(length).decode("utf-8") if length else "{}"
                payload = json.loads(raw or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("请求体必须是 JSON 对象")
                self._json(200, tglogin.action(payload))
            except Exception as e:
                self._json(400, {"error": str(e)})
            return
        if url.path == "/api/backend":           # 网页上拉起 / 停止后端主程序
            if not self._authed():
                self._json(401, {"error": "未登录"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 8 * 1024:
                    raise ValueError("请求体过大")
                raw = self.rfile.read(length).decode("utf-8") if length else "{}"
                payload = json.loads(raw or "{}")
                act = str((payload or {}).get("action") or "").strip()
                if act == "start":
                    self._json(200, tglogin.backend_start())
                elif act == "stop":
                    self._json(200, tglogin.backend_stop())
                else:
                    raise ValueError("action 只能是 start / stop")
            except Exception as e:
                self._json(400, {"error": str(e)})
            return
        if url.path == "/api/gotify-test":    # 拿表单里的当前值试推一条（改完先测、测通再存）
            if not self._authed():
                self._json(401, {"error": "未登录"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 32 * 1024:
                    raise ValueError("请求体过大")
                raw = self.rfile.read(length).decode("utf-8") if length else "{}"
                payload = json.loads(raw or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("请求体必须是对象")
                try:          # 读不到已存配置也不该拦着测试（用户可能正在修配置）
                    saved = _gotify_view(_read_config())
                except Exception:
                    saved = {"url": "", "token": "", "priority": 8}
                prio = payload.get("priority")
                if prio in (None, ""):
                    prio = saved["priority"]
                g = _valid_gotify({"url": payload.get("url") or saved["url"],
                                   "token": payload.get("token") or saved["token"],
                                   "priority": prio})
                self._json(200, gotify_test_push(g["url"], g["token"], g["priority"]))
            except Exception as e:
                self._json(400, {"error": str(e)})
            return
        if url.path != "/api/save":
            self._html(404, "404 Not Found")
            return
        if not self._authed():
            self._json(401, {"error": "未登录"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 512 * 1024:
                raise ValueError("请求体过大")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            pool = payload.get("KEYWORD_POOL")
            if not isinstance(pool, list):
                raise ValueError("KEYWORD_POOL 必须是数组")
            pool = [str(x).strip() for x in pool if str(x).strip()]

            # 读盘上的最新配置，只替换这几个字段，其余字段原样保留
            with _save_lock:
                cfg = _read_config()
                cfg["KEYWORD_POOL"] = pool
                # 传 existing：网页不认识的手写字段（频道里的额外键、代理里的额外键）跟着保留
                cfg["SOURCES"] = _valid_sources(payload.get("SOURCES"), cfg.get("SOURCES"))
                if "PROXY" in payload:
                    cfg["PROXY"] = _valid_proxy(payload["PROXY"], cfg.get("PROXY"))
                if "GOTIFY" in payload:
                    gotify = _valid_gotify(payload["GOTIFY"])
                    cfg["GOTIFY_URL"] = gotify["url"]
                    cfg["GOTIFY_TOKEN"] = gotify["token"]
                    cfg["GOTIFY_PRIORITY"] = gotify["priority"]
                _write_config(cfg)
            self._json(200, {"ok": True,
                             "pool": len(cfg["KEYWORD_POOL"]), "sources": len(cfg["SOURCES"]),
                             "proxy": _proxy_view(cfg),
                             "gotify": _gotify_view(cfg)})
        except Exception as e:
            self._json(400, {"error": str(e)})


def serve():
    if not logging.getLogger().handlers:      # 让 tglogin 的 INFO 日志能落到 stderr（journald/nohup 日志）
        logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                            format="%(asctime)s %(levelname)s %(message)s",
                            datefmt="%m-%d %H:%M:%S")
    logging.getLogger("telethon").setLevel(logging.WARNING)   # Telethon 的 INFO 太吵，只看异常
    port = DEFAULT_PORT
    if not os.path.exists(CONFIG_PATH):
        print(f"[!!] 未找到 {CONFIG_PATH}，请先部署好 config.json（可从 config.example.json 复制）")
        sys.exit(1)
    try:  # 端口也可在 config.json 里配 WEBUI_PORT
        port = int(_read_config().get("WEBUI_PORT") or DEFAULT_PORT)
    except Exception as e:
        print(f"[!!] 读不了 {CONFIG_PATH}: {e}")
        print("     config.json 被改坏了？修好它（或从 config.example.json 重新拷一份）再启动。")
        sys.exit(1)
    try:
        Handler._token = _ensure_webui_token()
    except Exception as e:
        print(f"[!!] 生成/读取访问令牌失败: {e}")
        print(f"     检查 {CONFIG_PATH} 是不是合法 JSON（可从 config.example.json 重新拷一份）。")
        sys.exit(1)
    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    except OSError as e:
        print(f"[!!] WebUI 端口 {port} 启动失败: {e}")
        print("     常见原因：端口被占用（另一个 webui 还在跑？）或权限不足；")
        print("     可在 config.json 里改 WEBUI_PORT 换端口。")
        sys.exit(1)
    print(f"[webui] TG2Gotify 配置界面已启动: http://<本机IP>:{port}/  （token 认证已开启）")
    print("[webui] 登录用访问令牌：首次启动会自动生成并打印；已存在时见 config.json 的 WEBUI_TOKEN")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    serve()
