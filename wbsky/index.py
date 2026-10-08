# index.py
import json
import os
import uuid
import requests
import glob
import secrets
from flask import Flask, request, jsonify, g, send_from_directory
import time
from datetime import datetime
import importlib
import logging
import command

from route import account_bp, service_bp, chat_bp, client_bp  # 导入所有蓝图
from live_panel import live_bp  # p86: 实时面板 /live（Basic 登录）

app = Flask(__name__)
app.json.ensure_ascii = False

# ---------- 注册蓝图 ----------
app.register_blueprint(account_bp)
app.register_blueprint(service_bp)
app.register_blueprint(chat_bp)
app.register_blueprint(client_bp)    # 客户端检查更新：GET /client/version
app.register_blueprint(live_bp)      # p86: 实时面板 /live（在线/房间/位置/聊天）

# ★ 2026-10 新增：管理后台 /admin（账号/货币/解锁/好友/动态）
#   与 /live 的分工：/live 只看"现在"（在线、房间、位置、聊天），
#   /admin 改数据。两者各自独立鉴权。
#   面板数据层走 wbsky 自己的裸 SQL（见 admin_store.py），
#   登录口令在 config.json 的 admin_user/admin_password
#   （或环境变量 WB_SKY_ADMIN_USER / WB_SKY_ADMIN_PASSWORD）。
#   关掉整个面板：config.json 里 "admin_enabled": false
try:
    from admin_panel import admin_bp
    app.register_blueprint(admin_bp)
except Exception as _admin_err:      # 面板坏了也不能拦住游戏服务
    logging.getLogger(__name__).warning("[admin] 管理后台装载失败（不影响游戏）: %r", _admin_err)
# 卡密系统（route/license.py）已按需求整块移除


# ---------- 卡密残留接口兜底 ----------
# 卡密系统删掉后，若客户端包体里仍写死了调用 /key/verify，
# 落到 catch-all 会拿到空 JSON，可能卡在启动校验界面。
# 这里显式回一个「校验已关闭」的结构，保证客户端直接放行。
@app.route("/key/verify", methods=["POST", "GET"])
def license_removed_verify():
    return jsonify({
        "ok": True,
        "permanent": True,
        "expire_at": 0,
        "remain": 0,
        "msg": "校验已关闭",
        "bypass": True,
    })


@app.route("/key/unbind", methods=["POST", "GET"])
def license_removed_unbind():
    return jsonify({"ok": True, "msg": "卡密系统已移除"})


# ---------- /admin 占位路由已删除 ----------
# 这里原本是"卡密系统的管理端已下线"的兜底（对 /admin 与 /admin/api/* 一律回 404）。
# 现在 /admin 由 admin_panel.py 的蓝图接管（真面板），
# 所以这一整块必须删掉 —— 留着的话 Flask 会用它去接 /admin/api/* 的请求，
# 面板的接口全都会被兜成 404。

# ---------- 数据库初始化 ----------
# 使用统一的 db 模块（支持 MySQL/SQLite）
import db as database
database.init_db()

# ---------- 工具 ----------
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)

# 确保 logs 目录存在
LOG_DIR = os.path.join(os.path.dirname(__file__), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

# ============================================================
# 日志中间件（保留在 index.py）
# ============================================================
@app.before_request
def log_account_create_body():
    if request.endpoint == 'account.request_geonotes_for_n_friends':
        body = request.get_json(silent=True) or request.form.to_dict() or request.data.decode()
        headers = dict(request.headers)
        log_file = os.path.join(LOG_DIR, 'purchase_unlock.log')
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(f"{datetime.utcnow().isoformat()}Z\n")
            f.write("Headers:\n")
            f.write(json.dumps(headers, ensure_ascii=False, indent=2) + "\n")
            f.write("Body:\n")
            f.write(json.dumps(body, ensure_ascii=False, indent=2) + "\n")
            f.write("-" * 40 + "\n")

@app.before_request
def get_friendslog():
    if request.endpoint == 'account.get_friends':
        body = request.get_json(silent=True) or request.form.to_dict() or request.data.decode()
        headers = dict(request.headers)
        log_file = os.path.join(LOG_DIR, 'purchase_unlock.log')
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(f"{datetime.utcnow().isoformat()}Z\n")
            f.write("Headers:\n")
            f.write(json.dumps(headers, ensure_ascii=False, indent=2) + "\n")
            f.write("Body:\n")
            f.write(json.dumps(body, ensure_ascii=False, indent=2) + "\n")
            f.write("-" * 40 + "\n")

# ============================================================
# ★ 请求记录（排障开关，默认关）
#   config.json 里设 "log_requests": true 才生效；关掉就设 false。
#   为什么要有它：社交类问题（好友请求 / 给蜡烛的"接受"提示 / 对方装扮）
#   客户端走的不是 UDP，而是这些 HTTP 接口 —— 只有看到"客户端到底调了什么、
#   我们回了什么"，才能把请求/响应的结构对上。
#   日志：logs/requests.log（会增长，排障完记得关掉或清空）
# ============================================================
LOG_REQ_FILE = os.path.join(LOG_DIR, 'requests.log')
_SKIP_PREFIX = ('/dl/', '/admin', '/static', '/favicon')


@app.before_request
def wbsky_log_request_in():
    try:
        if not load_config().get("log_requests"):
            return
    except Exception:
        return
    p = request.path or ''
    if p.startswith(_SKIP_PREFIX):
        return
    try:
        body = request.get_data(cache=True) or b''
        txt = body.decode('utf-8', 'replace')
    except Exception:
        txt = ''
    if len(txt) > 1200:
        txt = txt[:1200] + '...<截断>'
    g.wbsky_req = "%s %s  body=%s" % (request.method, p, txt)


@app.after_request
def wbsky_log_request_out(resp):
    # 2026-10-04b: the reference server marks every account/service response
    # no-store ("important: the client caches this otherwise"). Without it the
    # client keeps serving a cached empty friend list, which is exactly the
    # "the DB has the friend but the game shows none until a relog" symptom.
    # Placed before the early return so it applies even when logging is off.
    try:
        _p = request.path or ''
        if _p.startswith('/account/') or _p.startswith('/service/'):
            resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            resp.headers['Pragma'] = 'no-cache'
            resp.headers['Expires'] = '0'
    except Exception:
        pass
    info = getattr(g, 'wbsky_req', None)
    if not info:
        return resp
    try:
        if getattr(resp, 'direct_passthrough', False):
            out = ' resp=<文件/流>'
        else:
            data = resp.get_data()
            if len(data) < 4096:
                out = ' resp=' + data.decode('utf-8', 'replace').replace('\n', ' ')
            else:
                out = ' resp=<%d bytes>' % len(data)
        with open(LOG_REQ_FILE, 'a', encoding='utf-8') as f:
            f.write("%s | %s | %s%s\n" % (datetime.utcnow().isoformat() + 'Z',
                                          info, resp.status_code, out))
    except Exception:
        pass
    return resp


# ============================================================
# 其他路由（非 /account/ 和 /service/）
# ============================================================

@app.route("/starwatch/auth/api/v1/auth", methods=["POST"])
def starwatch_auth_api_v1_auth():
    # 官方 that-sky-server：starwatch 认证成功返回空对象 {}
    # 之前返回 starwatch.json 里的"验证错误"结构 -> 客户端认证失败 ->
    # 不请求 find_previous_or_empty（中继地址）-> 不连 UDP -> 看不到人
    return jsonify({})

# ---------- 404 错误处理 ----------
# 返回空 JSON 而非 HTML，防止客户端解析失败导致按钮消失

@app.errorhandler(404)
def not_found_error(e):
    return jsonify({}), 200

@app.errorhandler(500)
def internal_error(e):
    return jsonify({}), 200

# ---------- 健康检查（容器 / 1Panel / 负载均衡用） ----------
# ★ 2026-10：容器化部署新增。
#   为什么不直接用现成的路由：本服务是**全部 POST** 的游戏 API，
#   curl 探活默认发 HEAD/GET，会被 Flask 判成 405，容器就一直显示
#   unhealthy（老包踩过：拿 "/" 探活，而 Flask 没注册根路由，返回 404）。
#   这里注册一个明确的 GET 探针，放在 catch-all 之前，不干扰任何业务路由。
@app.route("/healthz", methods=["GET", "HEAD"])
def wbsky_healthz():
    # ★ 恒返回 200：这是**进程存活探针**，不是"数据库健康探针"。
    #   数据库状态放在 JSON 的 db 字段里（status 会变成 degraded）。
    #   为什么不返回 503：Docker 的 healthcheck 一旦失败，1Panel 之类的面板
    #   会把容器标红甚至按策略重启/重建，而数据库暂时不可用时连 UDP 联机
    #   都还在正常跑 —— 用 503 会把一个可恢复的小故障放大成"服务一直重启"。
    #   要看数据库有没有通：curl -s http://127.0.0.1:2007/healthz | grep '"db"'
    info = {"status": "ok", "service": "wbsky"}
    try:
        db = database.db_status()
        info["db"] = db
        if not db.get("ok"):
            info["status"] = "degraded"
    except Exception as e:
        info["db"] = {"ok": False, "error": str(e)[:200]}
        info["status"] = "degraded"
    return jsonify(info), 200

# ---------- 根级 Catch-All 兜底路由 ----------
# 任何未匹配的 POST 请求返回空 JSON

@app.route("/", defaults={"path": ""}, methods=["POST", "GET"])
@app.route("/<path:path>", methods=["POST", "GET"])
def root_catch_all(path):
    return jsonify({}), 200


# ---------- p89: 请求级 SQL 去重缓存的开关 ----------
@app.before_request
def wbsky_begin_sql_cache():
    try:
        database.begin_request_cache()
    except Exception:
        pass


@app.teardown_request
def wbsky_end_sql_cache(exc=None):
    try:
        database.end_request_cache()
    except Exception:
        pass


# ---------- p94: 记录客户端 IP（房间服据此把内核 TCP RTT 对到玩家） ----------
@app.before_request
def wbsky_note_client_ip():
    try:
        import live_panel
        live_panel.note_request()
    except Exception:
        pass

# ---------- 启动 ----------
if __name__ == '__main__':
    with app.app_context():
        # 检查 SSL 证书是否存在，存在则启用 HTTPS，否则用 HTTP
        cert_file = 'fullchain.pem'
        key_file = 'privkey.key'
        if os.path.isfile(cert_file) and os.path.isfile(key_file):
            app.run(ssl_context=(cert_file, key_file), debug=True, threaded=True,
                host='0.0.0.0', port=2999)
        else:
            app.run(debug=True, threaded=True, host='0.0.0.0', port=2999)
