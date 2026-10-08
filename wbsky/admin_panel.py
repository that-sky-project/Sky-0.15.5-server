# -*- coding: utf-8 -*-
"""wbsky 管理后台（/admin）—— 蓝图形式挂在主服务上，与 /live 并列。

## 和 skrJ2（包2 / v4）那套 admin 的关系

功能与页面照它的思路做（总览 / 用户 / 好友 / 动态 / 工具），
但**数据层全部重写**成 wbsky 自己的裸 SQL（见 admin_store.py 顶部说明）——
两边的表结构完全不同，代码没法直接搬。

## 和 /live 的分工

    /live   实时面板：在线玩家、房间、位置、聊天（只读，看"现在"）
    /admin  管理后台：账号、货币、解锁、好友、动态（可写，改"数据"）

两个各自独立鉴权，互不影响（`/live` 的守卫只拦 `/live*`，本面板只拦 `/admin*`）。

## 鉴权

会话 Cookie（HttpOnly + SameSite=Lax），口令来自（优先级从高到低）：

    wbsky/config.json   "admin_user" / "admin_password"
    环境变量            WB_SKY_ADMIN_USER / WB_SKY_ADMIN_PASSWORD
    内置默认            admin / admin123

★ 上线前请改掉默认口令（见 .env 与文档）。
★ 面板**没有 CSRF token**：它设计上只给 SSH 隧道 / 内网访问（见文档），
  不要直接暴露到公网。真要暴露，请在 nginx 上再套一层 Basic 认证。

## 开关

    config.json   "admin_enabled": false     整个面板下线（路由仍然存在，直接 404）
    config.json   "admin_session_minutes": 720   免登录时长
"""
import functools
import json
import logging
import os
import secrets
import time

from flask import Blueprint, Response, jsonify, request

import admin_store
import db as database

logger = logging.getLogger(__name__)

admin_bp = Blueprint("wbsky_admin", __name__, url_prefix="/admin")

SESSION_COOKIE = "wbsky_admin"
_sessions = {}          # token -> {"user":..., "exp": ts}
SECRET = secrets.token_hex(16)


# ---------------------------------------------------------------- 配置
def _cfg():
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        with open(p, encoding="utf-8-sig") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _conf(key, env_name, default):
    v = os.environ.get(env_name)
    if v is not None and str(v).strip() != "":
        return str(v).strip()
    cfg = _cfg()
    v = cfg.get(key)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip()


def admin_user():
    return _conf("admin_user", "WB_SKY_ADMIN_USER", "admin")


def admin_password():
    return _conf("admin_password", "WB_SKY_ADMIN_PASSWORD", "admin123")


def session_minutes():
    try:
        return int(_conf("admin_session_minutes", "WB_SKY_ADMIN_SESSION_MINUTES", "720"))
    except (TypeError, ValueError):
        return 720


def enabled():
    v = _conf("admin_enabled", "WB_SKY_ADMIN_ENABLED", "1")
    return v.lower() not in ("0", "false", "no", "off")


# ---------------------------------------------------------------- 会话
def _new_session(user):
    tok = secrets.token_urlsafe(24)
    _sessions[tok] = {"user": user, "exp": time.time() + session_minutes() * 60}
    # 顺手清过期
    now = time.time()
    for k in [k for k, v in _sessions.items() if v.get("exp", 0) < now]:
        _sessions.pop(k, None)
    return tok


def _session_user():
    tok = request.cookies.get(SESSION_COOKIE) or ""
    s = _sessions.get(tok)
    if not s:
        return None
    if s.get("exp", 0) < time.time():
        _sessions.pop(tok, None)
        return None
    return s.get("user")


def _set_cookie(resp, token, max_age):
    resp.set_cookie(SESSION_COOKIE, token, max_age=max_age,
                    httponly=True, samesite="Lax", path="/")


def _audit(action, detail=None, uid=None, result="ok"):
    """写一条操作日志（JSONL，一个操作一行）。"""
    try:
        logdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        os.makedirs(logdir, exist_ok=True)
        rec = {
            "t": int(time.time()),
            "user": _session_user() or "-",
            "ip": request.remote_addr,
            "action": action,
            "target": uid,
            "detail": detail,
            "result": result,
        }
        with open(os.path.join(logdir, "admin_audit.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_audit(limit=300):
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "admin_audit.jsonl")
        if not os.path.isfile(p):
            return []
        lines = open(p, encoding="utf-8").read().splitlines()[-int(limit):]
        out = []
        for ln in reversed(lines):
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
        return out
    except Exception:
        return []


# ---------------------------------------------------------------- 页面：登录
LOGIN_HTML = u"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>skywb 管理后台 · 登录</title>
<style>
:root{--bg:#0b1020;--card:#151c33;--fg:#e8ecff;--dim:#8b96c0;--acc:#6ea8fe;--err:#ff6b81}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
 background:radial-gradient(1200px 600px at 20% -10%,#1b2a5e 0%,var(--bg) 60%);color:var(--fg);
 font:15px/1.6 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.card{width:360px;padding:28px;border-radius:18px;background:var(--card);
 box-shadow:0 20px 60px rgba(0,0,0,.45);border:1px solid #26304f}
h1{margin:0 0 4px;font-size:20px}
p.sub{margin:0 0 20px;color:var(--dim);font-size:13px}
label{display:block;margin:14px 0 6px;color:var(--dim);font-size:13px}
input{width:100%;padding:11px 12px;border-radius:10px;border:1px solid #2c3757;
 background:#0e1428;color:var(--fg);font-size:15px}
input:focus{outline:none;border-color:var(--acc)}
button{width:100%;margin-top:20px;padding:12px;border:0;border-radius:10px;cursor:pointer;
 background:linear-gradient(135deg,#4f8cff,#7b5cff);color:#fff;font-size:15px;font-weight:600}
button:disabled{opacity:.6;cursor:default}
.err{margin-top:14px;color:var(--err);font-size:13px;min-height:18px}
.hint{margin-top:18px;color:var(--dim);font-size:12px;line-height:1.7;
 border-top:1px dashed #2c3757;padding-top:12px}
code{background:#0e1428;padding:1px 5px;border-radius:5px}
</style></head><body>
<form class="card" id="f">
  <h1>skywb 管理后台</h1>
  <p class="sub">账号 · 货币 · 解锁 · 好友 · 动态</p>
  <label>用户名</label>
  <input id="u" autocomplete="username" autofocus>
  <label>密码</label>
  <input id="p" type="password" autocomplete="current-password">
  <button id="b">登录</button>
  <div class="err" id="e"></div>
  <div class="hint">
    口令来自 <code>config.json</code> 的 <code>admin_user/admin_password</code>，
    或环境变量 <code>WB_SKY_ADMIN_USER/WB_SKY_ADMIN_PASSWORD</code>。<br>
    这个面板设计上只给内网 / SSH 隧道用，<b>不要直接暴露到公网</b>。
  </div>
</form>
<script>
const f=document.getElementById('f'),b=document.getElementById('b'),e=document.getElementById('e');
f.addEventListener('submit',async ev=>{
  ev.preventDefault();e.textContent='';b.disabled=true;
  try{
    const r=await fetch('/admin/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({user:document.getElementById('u').value,password:document.getElementById('p').value})});
    const j=await r.json();
    if(j.ok){location.href='/admin/';return}
    e.textContent=j.msg||'登录失败';
  }catch(err){e.textContent='请求失败: '+err}
  b.disabled=false;
});
</script></body></html>
"""


def _login_page():
    return Response(LOGIN_HTML, mimetype="text/html; charset=utf-8")


def _need_login_resp():
    if _wants_json():
        return jsonify({"ok": False, "need_login": True, "msg": "未登录"}), 401
    return _login_page(), 401


def _wants_json():
    p = request.path or ""
    return p.startswith("/admin/api/") or "application/json" in (request.headers.get("Accept") or "")


def _guard(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not enabled():
            return jsonify({"ok": False, "msg": "管理后台已在 config.json 里关闭（admin_enabled=false）"}), 404
        if not _session_user():
            return _need_login_resp()
        return fn(*a, **kw)
    return wrapper


# ---------------------------------------------------------------- 页面：主界面
@admin_bp.route("/")
@admin_bp.route("")
def admin_index():
    if not enabled():
        return Response(u"管理后台已关闭（config.json 的 admin_enabled=false）",
                        mimetype="text/plain; charset=utf-8"), 404
    if not _session_user():
        return _login_page()
    import admin_ui
    return Response(admin_ui.PAGE, mimetype="text/html; charset=utf-8")


# ---------------------------------------------------------------- 登录 / 登出
@admin_bp.route("/api/login", methods=["POST"])
def api_login():
    if not enabled():
        return jsonify({"ok": False, "msg": "管理后台已关闭"}), 404
    body = request.get_json(force=True, silent=True) or {}
    u = str(body.get("user") or "").strip()
    p = str(body.get("password") or "")
    # 常量时间比较，避免时序侧信道
    ok = (secrets.compare_digest(u, admin_user())
          and secrets.compare_digest(p, admin_password()))
    if not ok:
        logger.warning("[admin] 登录失败 user=%r ip=%s", u, request.remote_addr)
        return jsonify({"ok": False, "msg": "用户名或密码不对"}), 401
    tok = _new_session(u)
    resp = jsonify({"ok": True, "user": u, "minutes": session_minutes()})
    _set_cookie(resp, tok, session_minutes() * 60)
    _audit("login")
    return resp


@admin_bp.route("/api/logout", methods=["POST", "GET"])
def api_logout():
    tok = request.cookies.get(SESSION_COOKIE) or ""
    _sessions.pop(tok, None)
    resp = jsonify({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@admin_bp.route("/api/whoami")
def api_whoami():
    u = _session_user()
    if not u:
        return jsonify({"ok": False, "need_login": True}), 401
    return jsonify({"ok": True, "user": u, "minutes": session_minutes()})


# ---------------------------------------------------------------- 总览
@admin_bp.route("/api/overview")
@_guard
def api_overview():
    return jsonify({"ok": True, "data": admin_store.overview()})


# ---------------------------------------------------------------- 用户
@admin_bp.route("/api/users")
@_guard
def api_users():
    a = request.args
    return jsonify({"ok": True, "data": admin_store.list_users(
        keyword=a.get("q", ""),
        limit=a.get("limit", 50),
        offset=a.get("offset", 0),
        order=a.get("order", "candles_desc"))})


@admin_bp.route("/api/user/<path:uid>")
@_guard
def api_user_detail(uid):
    u = admin_store.get_user(uid)
    if not u:
        return jsonify({"ok": False, "msg": "账号不存在"}), 404
    return jsonify({"ok": True, "data": u})


@admin_bp.route("/api/user/<path:uid>/set_candles", methods=["POST"])
@_guard
def api_set_candles(uid):
    b = request.get_json(force=True, silent=True) or {}
    try:
        v = admin_store.set_candles(uid, b.get("value"))
        _audit("set_candles", {"value": b.get("value"), "now": v}, uid)
        return jsonify({"ok": True, "candles": v})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)[:200]}), 400


@admin_bp.route("/api/user/<path:uid>/set_currency", methods=["POST"])
@_guard
def api_set_currency(uid):
    b = request.get_json(force=True, silent=True) or {}
    try:
        v = admin_store.set_currency(uid, b.get("field"), b.get("value"))
        _audit("set_currency", {"field": b.get("field"), "value": b.get("value"), "now": v}, uid)
        return jsonify({"ok": True, "value": v})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)[:200]}), 400


@admin_bp.route("/api/user/<path:uid>/add_currency", methods=["POST"])
@_guard
def api_add_currency(uid):
    b = request.get_json(force=True, silent=True) or {}
    try:
        v = admin_store.add_currency(uid, b.get("field"), b.get("delta"))
        _audit("add_currency", {"field": b.get("field"), "delta": b.get("delta"), "now": v}, uid)
        return jsonify({"ok": True, "value": v})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)[:200]}), 400


@admin_bp.route("/api/user/<path:uid>/unlocks")
@_guard
def api_unlocks(uid):
    return jsonify({"ok": True, "data": admin_store.list_unlocks(uid, request.args.get("q", ""))})


@admin_bp.route("/api/user/<path:uid>/unlocks/add", methods=["POST"])
@_guard
def api_unlocks_add(uid):
    b = request.get_json(force=True, silent=True) or {}
    names = b.get("names") or []
    if isinstance(names, str):
        names = [x for x in names.replace(",", "\n").splitlines()]
    r = admin_store.add_unlocks(uid, names)
    _audit("unlocks_add", {"n": r.get("added")}, uid)
    return jsonify({"ok": True, "data": r})


@admin_bp.route("/api/user/<path:uid>/unlocks/remove", methods=["POST"])
@_guard
def api_unlocks_remove(uid):
    b = request.get_json(force=True, silent=True) or {}
    names = b.get("names") or []
    if isinstance(names, str):
        names = [x for x in names.replace(",", "\n").splitlines()]
    r = admin_store.remove_unlocks(uid, names)
    _audit("unlocks_remove", {"n": r.get("removed")}, uid)
    return jsonify({"ok": True, "data": r})


@admin_bp.route("/api/user/<path:uid>/collects")
@_guard
def api_collects(uid):
    return jsonify({"ok": True, "data": admin_store.list_collects(uid)})


@admin_bp.route("/api/user/<path:uid>/collects/add", methods=["POST"])
@_guard
def api_collects_add(uid):
    b = request.get_json(force=True, silent=True) or {}
    ids = b.get("ids") or []
    if isinstance(ids, str):
        ids = [x for x in ids.replace(",", "\n").splitlines()]
    r = admin_store.add_collects(uid, ids)
    _audit("collects_add", {"n": r.get("added")}, uid)
    return jsonify({"ok": True, "data": r})


@admin_bp.route("/api/user/<path:uid>/wing_buffs")
@_guard
def api_wing_buffs(uid):
    return jsonify({"ok": True, "data": admin_store.list_wing_buffs(uid)})


@admin_bp.route("/api/user/<path:uid>/wing_buffs/add", methods=["POST"])
@_guard
def api_wing_buffs_add(uid):
    b = request.get_json(force=True, silent=True) or {}
    names = b.get("names") or []
    if isinstance(names, str):
        names = [x for x in names.replace(",", "\n").splitlines()]
    r = admin_store.add_wing_buffs(uid, names)
    _audit("wing_buffs_add", {"n": r.get("added")}, uid)
    return jsonify({"ok": True, "data": r})


@admin_bp.route("/api/user/<path:uid>/clear", methods=["POST"])
@_guard
def api_clear(uid):
    b = request.get_json(force=True, silent=True) or {}
    field = str(b.get("field") or "")
    if b.get("confirm") != "CLEAR":
        return jsonify({"ok": False, "msg": "危险操作：请在 confirm 里填 CLEAR"}), 400
    try:
        admin_store.clear_collect(uid, field)
        _audit("clear_field", {"field": field}, uid)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)[:200]}), 400


@admin_bp.route("/api/user/<path:uid>/delete", methods=["POST"])
@_guard
def api_user_delete(uid):
    b = request.get_json(force=True, silent=True) or {}
    if b.get("confirm") != "DELETE":
        return jsonify({"ok": False, "msg": "危险操作：请在 confirm 里填 DELETE"}), 400
    r = admin_store.delete_user(uid)
    _audit("delete_user", r, uid, result="deleted")
    return jsonify({"ok": True, "data": r})


# ---------------------------------------------------------------- 好友
@admin_bp.route("/api/friends")
@_guard
def api_friends():
    return jsonify({"ok": True, "data": admin_store.list_friends(request.args.get("uid", ""))})


@admin_bp.route("/api/friend/add", methods=["POST"])
@_guard
def api_friend_add():
    b = request.get_json(force=True, silent=True) or {}
    try:
        r = admin_store.add_friend(b.get("a"), b.get("b"), b.get("nickname"))
        _audit("friend_add", {"a": b.get("a"), "b": b.get("b")}, b.get("a"))
        return jsonify({"ok": True, "data": r})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)[:200]}), 400


@admin_bp.route("/api/friend/remove", methods=["POST"])
@_guard
def api_friend_remove():
    b = request.get_json(force=True, silent=True) or {}
    r = admin_store.remove_friend(b.get("a"), b.get("b"))
    _audit("friend_remove", {"a": b.get("a"), "b": b.get("b")}, b.get("a"))
    return jsonify({"ok": True, "data": r})


@admin_bp.route("/api/friend/nickname", methods=["POST"])
@_guard
def api_friend_nickname():
    b = request.get_json(force=True, silent=True) or {}
    r = admin_store.set_friend_nickname(b.get("uid"), b.get("friend_id"), b.get("nickname"))
    _audit("friend_nickname", r, b.get("uid"))
    return jsonify({"ok": True, "data": r})


@admin_bp.route("/api/friend/reroll", methods=["POST"])
@_guard
def api_friend_reroll():
    b = request.get_json(force=True, silent=True) or {}
    r = admin_store.reroll_nickname(b.get("uid"), b.get("friend_id"))
    _audit("friend_reroll", r, b.get("uid"))
    return jsonify({"ok": True, "data": r})


# ---------------------------------------------------------------- 动态 / 邀请
@admin_bp.route("/api/feed")
@_guard
def api_feed():
    return jsonify({"ok": True, "data": admin_store.list_feed(request.args.get("q", ""),
                                                              request.args.get("limit", 100))})


@admin_bp.route("/api/feed/delete", methods=["POST"])
@_guard
def api_feed_delete():
    b = request.get_json(force=True, silent=True) or {}
    try:
        n = admin_store.delete_feed(b.get("id"))
        _audit("feed_delete", {"id": b.get("id"), "n": n})
        return jsonify({"ok": True, "deleted": n})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)[:200]}), 400


@admin_bp.route("/api/invites")
@_guard
def api_invites():
    return jsonify({"ok": True, "data": admin_store.list_pending_invites()})


@admin_bp.route("/api/invites/delete", methods=["POST"])
@_guard
def api_invite_delete():
    b = request.get_json(force=True, silent=True) or {}
    n = admin_store.delete_pending_invite(b.get("token_id"))
    _audit("invite_delete", {"token_id": b.get("token_id"), "n": n})
    return jsonify({"ok": True, "deleted": n})


# ---------------------------------------------------------------- 工具
@admin_bp.route("/api/health")
@_guard
def api_health():
    return jsonify({"ok": True, "data": admin_store.db_health()})


@admin_bp.route("/api/audit")
@_guard
def api_audit():
    return jsonify({"ok": True, "data": read_audit()})


@admin_bp.route("/api/name_pool")
@_guard
def api_name_pool():
    try:
        import friend_name
        return jsonify({"ok": True, "data": friend_name.pool_info()})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)[:200]}), 500


@admin_bp.route("/api/name_pool/random")
@_guard
def api_name_pool_random():
    try:
        import friend_name
        n = request.args.get("n", 20)
        n = max(1, min(int(n), 200))
        return jsonify({"ok": True, "data": [friend_name.random_name() for _ in range(n)]})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)[:200]}), 500
