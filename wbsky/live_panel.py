# -*- coding: utf-8 -*-
"""live_panel.py — 私服实时面板（在线人数 / 房间 / 位置 / 聊天记录）。

数据来源：
  · 房间服 HTTP:  http://127.0.0.1:8125/stats （在线连接、每张图的房间实例、netId、
                  在线时长、坐标（房间服能解出来时才有））
  · MySQL skygame.chat_messages   （聊天正文、频道、关卡；失败时回退 logs/ws_outbox.jsonl）
  · MySQL wbsky.friends.nickname（给 UUID 配一个"好友给他起的备注"当显示名）

访问： http://<host>:2007/live   （HTTP Basic，账号 admin / 密码 admin123）
      http://<host>:2007/live/data  （同一份 JSON，面板每 3 秒拉一次）
"""
import base64
import hmac
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from collections import OrderedDict

from flask import Blueprint, Response, jsonify, request

import db as database

live_bp = Blueprint("live_bp", __name__)

# ── p94：把"每个客户端的网络延迟"喂给房间服 ─────────────────────────
# 客户端不回服务端主动发的 ping，sky-enet 也不暴露 ENet 的 roundTripTime，
# 但**内核**为每条 TCP 连接维护 rtt（ss -tinH 可读）。客户端会周期性请求主服务端口，
# 于是：Flask 侧按 IP 采样 TCP RTT + 记录 user→IP，房间服按 uuid 取用，
# 用于"房间权威选延迟最低的那个"。
_RTT_LOCK = threading.Lock()
_RTT_BY_IP = {}        # ip -> (tcp rtt ms, 采样时刻)：客户端是短连接，必须记住一段时间
_UUID_IP = {}          # uuid -> ip（客户端请求时记录）
_RTT_AT = {"ts": 0.0}
_SS_RE = re.compile(r"\brtt:([0-9.]+)/([0-9.]+)")
_ADDR_RE = re.compile(r"([0-9a-fA-F:.]+):(\d+)\s*$")
_RTT_KEEP_SEC = 120.0


def _sample_tcp_rtt():
    """跑一次 ss，取每条已建立 TCP 连接的 rtt（毫秒）→ 按 IP 记忆 120 秒。

    客户端 HTTP 是**短连接**（每请求即断），所以不能"每次清空重来"：
    即使连接只活了 100ms，内核也已经用 SYN/SYN-ACK 量出了 rtt，抓到就记住。
    """
    try:
        out = subprocess.run(
            ["ss", "-tinH", "state", "established", "( sport = :2999 )"],
            capture_output=True, text=True, timeout=4,
        ).stdout
    except Exception:
        return
    cur_ip = None
    now = time.time()
    fresh = {}
    for line in out.splitlines():
        if not line.startswith((" ", "\t")):
            parts = line.split()
            if len(parts) >= 5:
                m = _ADDR_RE.search(parts[4])     # peer address ip:port
                cur_ip = m.group(1) if m else None
            continue
        if cur_ip is None:
            continue
        m = _SS_RE.search(line)
        if m:
            rtt = float(m.group(1))
            if cur_ip not in fresh or rtt < fresh[cur_ip]:
                fresh[cur_ip] = rtt
    with _RTT_LOCK:
        for ip, rtt in fresh.items():
            old = _RTT_BY_IP.get(ip)
            # 取历史最小值附近的值（rtt 会因排队抖动，取小值更接近链路真实延迟）
            _RTT_BY_IP[ip] = (rtt if old is None else min(old[0] * 1.0, rtt), now)
        for ip in [k for k, (_, t) in _RTT_BY_IP.items() if now - t > _RTT_KEEP_SEC]:
            _RTT_BY_IP.pop(ip, None)
        _RTT_AT["ts"] = now


def _rtt_loop():
    while True:
        _sample_tcp_rtt()
        time.sleep(0.8)


threading.Thread(target=_rtt_loop, daemon=True).start()


def _peer_rtt_by_uuid():
    """{uuid: rtt_ms} —— 用最近一次请求的 IP 映射。"""
    with _RTT_LOCK:
        by_ip = dict(_RTT_BY_IP)
        u2i = dict(_UUID_IP)
        ts = _RTT_AT["ts"]
    out = {}
    for uuid, ip in u2i.items():
        if ip in by_ip:
            out[uuid] = int(round(by_ip[ip]))
    return out, ts


@live_bp.route("/internal/rtt")
def internal_rtt():
    """房间服来取：{uuid: rtt_ms}。仅允许本机访问。"""
    if request.remote_addr not in ("127.0.0.1", "::1", "localhost"):
        return jsonify({"error": "local only"}), 403
    data, ts = _peer_rtt_by_uuid()
    with _RTT_LOCK:
        dbg = {
            "n_uuid_ip": len(_UUID_IP),
            "n_ip_rtt": len(_RTT_BY_IP),
            "uuid_ip": dict(list(_UUID_IP.items())[:5]),
            "ip_rtt": {k: round(v[0], 1) for k, v in list(_RTT_BY_IP.items())[:5]},
        }
    out = {"ts": ts, "rtt": data}
    out.update(dbg)
    return jsonify(out)


def note_request():
    """index.py 的**应用级** before_request 会调它：记录 user→IP（任意接口都算）。

    必须挂在应用级：游戏客户端请求的是 /account/*，不会走 /live 蓝图。
    """
    try:
        ip = request.remote_addr or ""
        if ip in ("127.0.0.1", "::1", ""):
            return
        uid = request.headers.get("X-Sky-User")
        if not uid:
            body = request.get_data(cache=True) or b""
            if body[:1] == b"{":
                try:
                    uid = (json.loads(body.decode("utf-8", "replace")) or {}).get("user")
                except Exception:
                    uid = None
        if uid:
            with _RTT_LOCK:
                _UUID_IP[str(uid)] = ip
    except Exception:
        pass

USER = "admin"
PASSWORD = "admin123"
REALM = "sky-admin"

STATS_URL = "http://127.0.0.1:8125/stats"
WS_OUTBOX = "/wbsky/logs/ws_outbox.jsonl"
CHAT_LIMIT = 300

# /live/data 的 2 秒缓存（多人同时看面板时只查一次）
_CACHE = {"at": 0.0, "data": None}


# ─────────────────────────── 登录 ───────────────────────────
def _unauthorized():
    return Response(
        "需要登录\n", 401,
        {"WWW-Authenticate": 'Basic realm="%s", charset="UTF-8"' % REALM},
    )


def _check_auth():
    hdr = request.headers.get("Authorization") or ""
    if not hdr.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(hdr[6:]).decode("utf-8")
    except Exception:
        return False
    user, _, pwd = raw.partition(":")
    return hmac.compare_digest(user, USER) and hmac.compare_digest(pwd, PASSWORD)


@live_bp.before_request
def _guard():
    # /internal/rtt 是本机房间服来取延迟数据的，只允许本机、不需要 Basic
    if request.path == "/internal/rtt":
        if request.remote_addr not in ("127.0.0.1", "::1", "localhost"):
            return _unauthorized()
        return None
    if not _check_auth():
        return _unauthorized()
    return None


# ─────────────────────── 房间服 /stats ───────────────────────
def _fetch_stats():
    try:
        with urllib.request.urlopen(STATS_URL, timeout=3) as r:
            return json.loads(r.read().decode("utf-8")), None
    except Exception as e:  # 房间服重启中/未启动
        return None, "%s: %s" % (type(e).__name__, e)


def _fmt_dur(sec):
    if sec is None:
        return "-"
    sec = int(sec)
    if sec < 60:
        return "%ds" % sec
    if sec < 3600:
        return "%dm%02ds" % (sec // 60, sec % 60)
    return "%dh%02dm" % (sec // 3600, (sec % 3600) // 60)


def _short(uuid):
    return (uuid or "")[:8]


def _nicknames(uuids):
    """uuid -> 好友备注（谁给谁起的都算，取第一条非空）。失败返回 {}。"""
    out = {}
    uuids = [u for u in uuids if u]
    if not uuids:
        return out
    try:
        marks = ",".join(["%s"] * len(uuids))
        rows = database.query_all(
            "SELECT friend_id, nickname FROM friends WHERE friend_id IN (%s) AND nickname <> ''" % marks,
            tuple(uuids),
        ) or []
        for r in rows:
            uid = r.get("friend_id")
            nick = (r.get("nickname") or "").strip()
            if uid and nick and uid not in out:
                out[uid] = nick
    except Exception:
        pass
    return out


def _chat_from_db(limit=CHAT_LIMIT):
    rows = database.query_all(
        "SELECT from_user_id, message, channel, level_id, sent_at_ms "
        "FROM chat_messages ORDER BY id DESC LIMIT %d" % int(limit)
    ) or []
    return rows


def _chat_from_file(limit=CHAT_LIMIT):
    rows = []
    try:
        with open(WS_OUTBOX, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-limit:]
        for ln in lines:
            try:
                o = json.loads(ln)
            except Exception:
                continue
            if o.get("type") != "chat":
                continue
            rows.append({
                "from_user_id": o.get("sender_id"),
                "message": o.get("msg"),
                "channel": o.get("ch"),
                "level_id": o.get("level_id") or "",
                "sent_at_ms": o.get("timestamp") or 0,
            })
    except Exception:
        pass
    return list(reversed(rows))


# ─────────────────────────── 数据组装 ───────────────────────────
def build_payload():
    stats, stats_err = _fetch_stats()
    peers = (stats or {}).get("peers") or []

    # 地图/房间分组：levelName -> roomInst -> players
    levels = OrderedDict()
    uuid_by_level = {}
    for p in peers:
        if not p.get("inGame"):
            continue
        lname = p.get("levelName") or "未知"
        inst = p.get("roomInst") or 0
        lv = levels.setdefault(lname, {"name": lname, "levelId": p.get("levelId"),
                                       "rooms": OrderedDict()})
        room = lv["rooms"].setdefault(inst, {"inst": inst, "players": []})
        uuid = p.get("uuid") or ""
        room["players"].append({
            "playerId": p.get("playerId"),
            "uuid": uuid,
            "short": _short(uuid),
            "onlineSec": p.get("onlineSec"),
            "online": _fmt_dur(p.get("onlineSec")),
            "pos": p.get("pos"),
            "posErr": p.get("posErr"),
            "authority": bool(p.get("authority")),
            "stateLen": p.get("stateLen") or 0,
            "addr": p.get("addr"),
        })
        if uuid and lname:
            uuid_by_level.setdefault(lname, set()).add(uuid)

    # 聊天
    chat_rows = _chat_from_db() or _chat_from_file()
    if not chat_rows:
        chat_rows = _chat_from_file()

    uuids = set()
    for lv in levels.values():
        for room in lv["rooms"].values():
            for pl in room["players"]:
                uuids.add(pl["uuid"])
    for r in chat_rows:
        uuids.add(r.get("from_user_id"))
    nicks = _nicknames(sorted(uuids))

    # 关卡 id -> 名字（/stats 里同一张图会带上名字，用它给聊天记录补地图名）
    lv_names = {}
    for p in peers:
        lid = p.get("levelId")
        if lid is not None and p.get("levelName"):
            lv_names[int(lid)] = p["levelName"]
            lv_names["0x%x" % int(lid)] = p["levelName"]

    # 每个账号当前在哪张图（聊天记录里 level_id 常为空，用房间服的实时数据补）
    cur_level_by_uuid = {}
    for p in peers:
        if p.get("inGame") and p.get("uuid"):
            cur_level_by_uuid[p["uuid"]] = p.get("levelName") or ""

    chats = []
    for r in chat_rows:
        uid = r.get("from_user_id") or ""
        lid = r.get("level_id") or ""
        lname = ""
        try:
            lname = lv_names.get(int(lid, 16) if isinstance(lid, str) and lid.startswith("0x")
                                 else int(lid)) or ""
        except Exception:
            lname = ""
        if not lname:
            lname = cur_level_by_uuid.get(uid, "")
        chats.append({
            "t": r.get("sent_at_ms") or 0,
            "uuid": uid,
            "short": _short(uid),
            "name": nicks.get(uid) or "",
            "msg": r.get("message") or "",
            "ch": r.get("channel") or "local",
            "levelId": str(lid),
            "level": lname,
        })

    out_levels = []
    total_rooms = 0
    for lv in levels.values():
        rooms = []
        for room in lv["rooms"].values():
            players = sorted(room["players"], key=lambda x: (x["playerId"] or 0))
            total_rooms += 1
            rooms.append({"inst": room["inst"], "count": len(players), "players": players})
        rooms.sort(key=lambda r: r["inst"])
        out_levels.append({"name": lv["name"], "levelId": lv["levelId"],
                           "count": sum(r["count"] for r in rooms), "rooms": rooms})
    out_levels.sort(key=lambda x: (-x["count"], x["name"]))

    return {
        "ts": int(time.time() * 1000),
        "online": (stats or {}).get("online", 0),
        "connected": (stats or {}).get("connected", 0),
        "levelsCount": len(out_levels),
        "roomsCount": total_rooms,
        "recv": (stats or {}).get("recv", 0),
        "forwarded": (stats or {}).get("forwarded", 0),
        "statsErr": stats_err,
        "levels": out_levels,
        "chat": chats,
        "chatSource": "mysql" if chat_rows else "none",
    }


@live_bp.route("/live/data")
def live_data():
    # p86b: 2 秒缓存 —— 多个面板标签/多个人同时看时，不让 DB 与房间服被重复打
    now = time.time()
    if _CACHE["data"] is not None and now - _CACHE["at"] < 2.0:
        return jsonify(_CACHE["data"])
    data = build_payload()
    _CACHE["at"] = now
    _CACHE["data"] = data
    return jsonify(data)


# ─────────────────────────── 页面 ───────────────────────────
PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Sky 私服实时面板</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0f1116;--card:#171a21;--line:#262a34;--fg:#e6e8ee;--dim:#8b93a5;--acc:#5cc8ff;--ok:#59d18a;--warn:#ffb454}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,"Segoe UI",Roboto,"Microsoft YaHei",sans-serif}
header{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;gap:16px;align-items:center;background:#12151b;position:sticky;top:0;z-index:5}
h1{font-size:16px;margin:0 12px 0 0;font-weight:600}
.kpi{display:flex;gap:14px;flex-wrap:wrap}
.kpi div{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:4px 10px}
.kpi b{color:var(--acc);font-size:16px}
.spacer{flex:1}
button,input{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:5px 9px;font-size:13px}
main{display:flex;gap:14px;padding:14px;align-items:flex-start;flex-wrap:wrap}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;flex:1 1 460px;min-width:320px}
section h2{margin:0 0 10px;font-size:14px;color:var(--dim);font-weight:600;letter-spacing:.03em}
.map{margin-bottom:14px}
.map>.mh{display:flex;justify-content:space-between;font-weight:600;border-bottom:1px dashed var(--line);padding-bottom:4px;margin-bottom:8px}
.room{border:1px solid var(--line);border-radius:8px;padding:8px;margin-bottom:8px;background:#141821}
.room>.rh{display:flex;justify-content:space-between;color:var(--dim);font-size:12px;margin-bottom:6px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:3px 6px;border-bottom:1px solid #1e222b}
th{color:var(--dim);font-weight:500;font-size:12px}
td.id{color:var(--acc);font-variant-numeric:tabular-nums}
td.u{font-family:ui-monospace,Consolas,monospace;color:#c3c9d6}
.tag{font-size:11px;padding:1px 5px;border-radius:5px;background:#20303a;color:var(--acc)}
table.chat td.ts{color:var(--dim);white-space:nowrap;font-variant-numeric:tabular-nums}
table.chat td.who{white-space:nowrap}
table.chat td.msg{word-break:break-word;white-space:pre-wrap}
.chatbox{max-height:70vh;overflow:auto}
.err{color:var(--warn);font-size:13px}
footer{color:var(--dim);padding:0 18px 18px;font-size:12px}
</style></head><body>
<header>
  <h1>Sky 私服实时面板</h1>
  <div class="kpi">
    <div>在线 <b id="k-online">-</b></div>
    <div>地图 <b id="k-levels">-</b></div>
    <div>房间 <b id="k-rooms">-</b></div>
    <div>连接 <b id="k-conn">-</b></div>
  </div>
  <div class="spacer"></div>
  <input id="filter" placeholder="过滤账号/内容…" style="width:180px">
  <button id="pause">暂停刷新</button>
  <span id="ts" style="color:var(--dim)"></span>
</header>
<main>
  <section style="flex:1 1 520px">
    <h2>房间 / 位置</h2>
    <div id="rooms"><div style="color:var(--dim)">加载中…</div></div>
  </section>
  <section style="flex:1 1 420px">
    <h2>聊天记录（最近 <span id="chatn">0</span> 条）</h2>
    <div class="chatbox"><table class="chat"><tbody id="chat"></tbody></table></div>
  </section>
</main>
<footer id="foot"></footer>
<script>
let paused=false, timer=null, last=null;
const $=id=>document.getElementById(id);
$('pause').onclick=()=>{paused=!paused;$('pause').textContent=paused?'继续刷新':'暂停刷新';};
$('filter').oninput=()=>render(last);
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function pos(p){ if(!p.pos) return '<span style="color:var(--dim)">—</span>';
  const r=n=>Math.round(n*100)/100; return '('+r(p.pos.x)+', '+r(p.pos.y)+', '+r(p.pos.z)+')'; }
function render(d){
  if(!d) return;
  last=d;
  $('k-online').textContent=d.online; $('k-levels').textContent=d.levelsCount;
  $('k-rooms').textContent=d.roomsCount; $('k-conn').textContent=d.connected;
  $('ts').textContent=new Date(d.ts).toLocaleTimeString();
  const q=($('filter').value||'').trim().toLowerCase();
  let html='';
  for(const lv of d.levels){
    const rooms=lv.rooms.filter(r=>!q||r.players.some(p=>(p.short||'').includes(q)||(p.name||'').toLowerCase().includes(q)));
    if(q&&!rooms.length) continue;
    html+='<div class="map"><div class="mh"><span>'+esc(lv.name)+'</span><span>'+lv.count+' 人 / '+lv.rooms.length+' 间</span></div>';
    for(const r of rooms){
      html+='<div class="room"><div class="rh"><span>房间 '+(r.inst+1)+'（#'+r.inst+'）</span><span>'+r.count+' 人</span></div>';
      html+='<table><thead><tr><th>位置</th><th>槽位</th><th>账号</th><th>在线</th><th>坐标</th></tr></thead><tbody>';
      for(const p of r.players){
        html+='<tr><td>'+esc(lv.name)+' 房间'+(r.inst+1)+'</td><td class="id">'+esc(p.playerId)+'</td>'+
              '<td class="u">'+esc(p.name? p.name+' ('+p.short+')' : p.short)+'</td><td>'+esc(p.online)+'</td>'+
              '<td class="u">'+pos(p)+(p.authority?' <span class="tag">权威</span>':'')+'</td></tr>';
      }
      html+='</tbody></table></div>';
    }
    html+='</div>';
  }
  $('rooms').innerHTML=html||'<div style="color:var(--dim)">当前没有人在线</div>';
  let ch='', n=0;
  for(const c of d.chat){
    if(q&&!((c.short||'').includes(q)||(c.name||'').toLowerCase().includes(q)||(c.msg||'').toLowerCase().includes(q))) continue;
    n++;
    const t=c.t?new Date(c.t).toLocaleTimeString():'';
    const who=c.name? c.name+' ('+c.short+')' : c.short;
    ch+='<tr><td class="ts">'+esc(t)+'</td><td class="who">'+esc(who)+'</td>'+
        '<td style="color:var(--dim)">'+esc(c.level||'')+' '+esc(c.ch||'')+'</td>'+
        '<td class="msg">'+esc(c.msg)+'</td></tr>';
  }
  $('chat').innerHTML=ch||'<tr><td style="color:var(--dim)">暂无聊天</td></tr>';
  $('chatn').textContent=n;
  $('foot').innerHTML = (d.statsErr? '<span class="err">房间服 /stats 取不到: '+esc(d.statsErr)+'</span> ｜ ':'')+
    '房间服收包 '+d.recv+' / 转发 '+d.forwarded+' ｜ 每 3 秒自动刷新';
}
async function tick(){
  if(paused) return;
  try{
    const r=await fetch('/live/data',{cache:'no-store'});
    if(r.status===401){ location.reload(); return; }
    render(await r.json());
  }catch(e){ $('foot').innerHTML='<span class="err">拉取失败: '+esc(e)+'</span>'; }
}
timer=setInterval(tick,3000); tick();
</script></body></html>"""


@live_bp.route("/live")
def live_page():
    return Response(PAGE, mimetype="text/html; charset=utf-8")


@live_bp.route("/live/ping")
def live_ping():
    return jsonify({"ok": True, "user": USER, "t": int(time.time())})
