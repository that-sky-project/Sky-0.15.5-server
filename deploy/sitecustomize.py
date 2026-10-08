# -*- coding: utf-8 -*-
"""
wbsky 生产运行时适配层（Docker / 1Panel 部署用）
=====================================================================
本文件通过 Python 的 sitecustomize 机制**在解释器启动阶段自动加载**
（前提是 PYTHONPATH 里包含本文件所在目录，见 deploy/Dockerfile）。

它一个字节都不改 wbsky/ 里的业务代码，全部用"运行时打补丁"的方式完成：

  1) 关掉 Werkzeug 调试器与重载器
       index.py 末尾写的是 app.run(..., debug=True)。公网跑会暴露
       /console 交互式调试器（可执行任意代码）；重载器还会 fork 出
       第二个进程，两个进程抢同一个监听端口。
       → WB_SKY_DEBUG=0（默认）时强制 debug=False、use_reloader=False。

  2) 端口 / 监听地址可配
       index.py 里写死 host='0.0.0.0'、port=2999（本包统一覆盖成 2007）。
       → WB_SKY_HTTP_PORT / WB_SKY_BIND 覆盖；不设则与老行为完全一致。

  3) 房间服地址（下发给客户端的联机地址）
       wbsky/route/account.py 的 _udp_uri() 读 config.json 的
       udp_server_host / udp_server_port。
       → WB_SKY_UDP_HOST / WB_SKY_UDP_PORT 优先于 config.json。
         必须用环境变量覆盖的原因：容器里 config.json 是 bind mount 的宿主机
         文件，"改 .env 一处"做不到；而且运行期改 JSON 会和后台面板的写操作打架。

  4) 聊天 WebSocket 地址
       account.py 里下发 websocket_url 时把端口丢掉了（只取 request.host
       的域名部分），于是客户端会去连 443，而 443 上通常没有 WS 服务。
       → WB_SKY_WS_URL 显式指定（例如 wss://beta.admin.xyz/account/ws）；
         不设则退回"按实际访问地址拼"，比原代码多保留端口。

  5) 实时面板 live_panel.py 的适配
       原来写死：STATS_URL=http://127.0.0.1:8125/stats、
                 WS_OUTBOX=/wbsky/logs/ws_outbox.jsonl、
                 USER/PASSWORD=admin/admin123、
                 RTT 采样固定查 ( sport = :2999 )（本包按实际端口覆盖）。
       容器部署下这四个全都对不上：
         · 房间服是**另一个容器**（服务名 udp），不是 127.0.0.1；
         · 项目路径是 /app，不是 /wbsky；
         · 面板口令硬编码在源码里，等于把后台口令写进了部署包；
         · 主服务端口改成 2007 后，RTT 采样还查 2999 就永远查不到连接。
       → WB_SKY_UDP_STATS_URL / WB_SKY_WS_OUTBOX /
         WB_SKY_LIVE_USER / WB_SKY_LIVE_PASSWORD / WB_SKY_HTTP_PORT 覆盖。

  6) /internal/rtt 的来访限制
       live_panel._guard() 只允许 127.0.0.1 访问 /internal/rtt。
       房间服在另一个容器时源 IP 是 compose 网段，会被 401 掉。
       → WB_SKY_TRUST_RFC1918=1（默认开）时放行 10/172.16-31/192.168 私网来源。

  7) 建库容错（schema resilience）
       index.py 第 60 行是**模块级**的 database.init_db()，没有任何
       try/except：MySQL 没起来 / 密码不对 / 主机名解析不了，异常直接冒到
       模块顶层 → 进程退出 → 被 restart 策略无限重启。
       结果是连 UDP 联机服都一起起不来，而联机本来跟数据库无关。
       → 读表失败时重试 WB_SKY_DB_RETRY 次（默认 3），仍然失败就只打日志、
         让服务继续跑；UDP 联机不受影响，数据库好了重启一次即可。
         自动建表是幂等的（CREATE TABLE IF NOT EXISTS），随时可恢复。
         设 WB_SKY_SCHEMA_RESILIENT=0 可回到"连不上就崩"的原始行为。

开关汇总（都在 deploy/.env 里，全部有安全默认值，不设也能起）
  WB_SKY_HTTP_PORT / WB_SKY_BIND / WB_SKY_DEBUG
  WB_SKY_UDP_HOST / WB_SKY_UDP_PORT / WB_SKY_UDP_STATS_URL
  WB_SKY_WS_URL / WB_SKY_WS_OUTBOX
  WB_SKY_LIVE_USER / WB_SKY_LIVE_PASSWORD / WB_SKY_TRUST_RFC1918
  WB_SKY_SCHEMA_RESILIENT / WB_SKY_DB_RETRY
  WB_SKY_EXPECT_UDP_PORT（只用于启动自检，见 _check_udp_port_consistency）
"""
import os
import sys
import time

_WRAPPED = "_wbsky_wrapped"

# 主服务默认端口。
# index.py 源码里写死 2999；本包统一用 2007（与 .env 的 HTTPS_PORT 一致），
# 由 _patch_flask_run 强制覆盖。不设 WB_SKY_HTTP_PORT 时也走这个值。
DEFAULT_HTTP_PORT = 2007


# ===================================================================== #
# 基础工具
# ===================================================================== #
def _flag(name, default):
    """布尔开关一律 fail-safe：没设 / 空串 → 用默认值。"""
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return str(v).strip().lower() not in ("0", "false", "no", "off")


def _env_int(name, default=None):
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _log(msg):
    sys.stderr.write("[wbsky-prod] %s\n" % msg)


def _bootstrap_sys_path():
    """把应用目录插到 sys.path 最前面。

    ★ 这一步**必须**在打补丁之前做，否则会静默失效。
    原因：sitecustomize 是解释器启动阶段加载的，那时
      · 用 `python index.py` 启动时，脚本目录（= 应用目录 /app）还没进 sys.path
        （CPython 是在 sitecustomize 之后才把脚本目录插进去的）；
      · 用 `python -c` / 交互式启动时，连 cwd 都不在里面。
    于是这里 `import route.account` 会 ModuleNotFoundError。
    更糟的是：那个异常被本文件的兜底 try/except 吞掉，只留一行日志，
    表现为「服务能起来，但下发给客户端的联机地址还是 config.json 里的旧值」
    —— 也就是"能登录、能进图、看不到人"，而且日志看着一切正常。
    """
    cands = []
    app_dir = os.environ.get("APP_DIR")
    if app_dir:
        cands.append(app_dir)
    # 本文件在 <项目根>/deploy/ 下，所以 <项目根> 是上一级
    cands.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        cands.append(os.getcwd())
    except Exception:
        pass

    added = []
    for d in cands:
        if d and os.path.isdir(d) and d not in sys.path:
            sys.path.insert(0, d)
            added.append(d)
    return added


def _entries():
    """当前在跑的是哪个脚本（index.py / ws_server.py / live_panel ...）。"""
    names = []
    try:
        if sys.argv and sys.argv[0]:
            names.append(os.path.basename(sys.argv[0]).lower())
    except Exception:
        pass
    try:
        main_mod = sys.modules.get("__main__")
        f = getattr(main_mod, "__file__", None)
        if f:
            names.append(os.path.basename(f).lower())
    except Exception:
        pass
    return names


def _is_main_service():
    """只有 index.py 需要打「Flask 端口 / 建库容错」这类补丁。

    ws_server.py 是纯 stdlib 的 socket 服务，自己去 bind 端口、也不连数据库；
    live_panel 是 index.py 里的蓝图。对它们打 Flask 补丁没意义，还会在日志里
    刷出误导性的"已装载"。
    """
    names = _entries()
    for n in names:
        if n in ("index.py", "index"):
            return True
        if n.startswith("ws_server") or n.startswith("ws_relay"):
            return False
    # 判断不出来时按主服务处理（宁可多打补丁，也不要漏）
    return not any(n.startswith("ws_") for n in names)


# ===================================================================== #
# 1) Flask 运行参数：调试器 / 监听地址 / 端口
# ===================================================================== #
def _patch_flask_run():
    """包装 flask.Flask.run，强制覆盖 debug / use_reloader / host / port。

    注意：**绝对不能包装 flask.Flask 这个类本身**。早期踩过：
    包装类之后 `app = Flask(__name__)` 会变成
    `TypeError: Flask.__init__() got an unexpected keyword argument 'debug'`，
    index.py 直接起不来。只能动它的方法。
    """
    if not _is_main_service():
        return None

    try:
        import flask
    except Exception:
        return None

    force_debug = _flag("WB_SKY_DEBUG", False)
    bind = (os.environ.get("WB_SKY_BIND") or "").strip() or "0.0.0.0"
    port = _env_int("WB_SKY_HTTP_PORT", DEFAULT_HTTP_PORT) or DEFAULT_HTTP_PORT

    orig_run = flask.Flask.run
    if getattr(orig_run, _WRAPPED, False):
        return None

    def run_wrapper(self, *args, **kwargs):
        if not force_debug:
            kwargs["debug"] = False
            kwargs["use_reloader"] = False
        # 端口与地址是**强制覆盖**，不是"缺省才填"：
        # index.py 里是 app.run(..., host='0.0.0.0', port=2999)，本包覆盖成 2007，
        # 只在 key 不存在时才填的话，这两个环境变量永远不会生效。
        kwargs["host"] = bind
        kwargs["port"] = port
        return orig_run(self, *args, **kwargs)

    setattr(run_wrapper, _WRAPPED, True)
    run_wrapper.__wrapped__ = orig_run
    flask.Flask.run = run_wrapper

    return ("Flask.run 已接管: bind=%s port=%d debug=%s reloader=%s"
            % (bind, port, "ON(排障)" if force_debug else "off",
               "ON" if force_debug else "off"))


# ===================================================================== #
# 2) 房间服地址 + 聊天 WS 地址覆盖（route/account.py）
# ===================================================================== #
def _patch_account_endpoints():
    """覆盖 _udp_uri()（下发给客户端的联机地址）与 logini（websocket_url）。

    为什么直接改**模块属性**在这里有效，而在另一个包里无效：
      route/account.py 里 `/find_previous_or_empty`、`/hb` 这些视图函数内部
      调用的是**模块全局名** `_udp_uri()`，运行时再去 globals 里查，
      所以事后替换模块属性就会生效。
      （对照：logini 是被 `from ... import logini` 抓走注册进 app 的，
        换模块属性没用，必须换 app.view_functions —— 见 _patch_login_ws_url。）

    ★ 时机很关键：**必须在 route.account 已经完整 import 之后**再调用。
      这就是为什么它不直接挂在 _install() 里，而是由 register_blueprint 钩子
      在"路由注册完"那一刻调用（见 _install_login_hook）。
      早期版本在解释器启动阶段就 import route.account，结果因为
        · sys.path 里还没有应用目录
        · 或者 route.account 自己的模块级 init_db() 抛异常
      导致整个补丁被静默跳过，症状是"服务能起，但下发地址还是旧值"。
      现在改成：import 失败**不报错、不吞掉**，只是延迟到下一次调用再试。
    """
    if not _is_main_service():
        return None
    if os.environ.get("WB_SKY_PATCH_UDP", "1").strip().lower() in ("0", "false", "no", "off"):
        return None

    try:
        from route import account as acc
    except Exception as exc:
        # 只记一行，不当作错误：下一次（路由注册完/下一个请求）还会再试
        _log("route.account 暂不可用，联机地址覆盖稍后重试: %s" % exc)
        return None

    done = []

    # ---- (a) 联机地址 ----
    env_host = (os.environ.get("WB_SKY_UDP_HOST") or "").strip()
    env_port = _env_int("WB_SKY_UDP_PORT", None)

    orig_udp_uri = getattr(acc, "_udp_uri", None)
    if callable(orig_udp_uri) and not getattr(orig_udp_uri, _WRAPPED, False):

        def _udp_uri_wrapper():
            uri = orig_udp_uri()
            host, _, port = str(uri).rpartition(":")
            if env_host:
                host = env_host
            if env_port:
                port = str(env_port)
            if not host:
                host = "127.0.0.1"
            if not port:
                port = str(env_port or 8125)
            return "%s:%s" % (host, port)

        setattr(_udp_uri_wrapper, _WRAPPED, True)
        _udp_uri_wrapper.__wrapped__ = orig_udp_uri
        acc._udp_uri = _udp_uri_wrapper
        done.append("_udp_uri(host=%s port=%s)" % (env_host or "config.json",
                                                   env_port or "config.json"))

    return ("route.account 已接管: " + "; ".join(done)) if done else None


def _public_ws_url():
    """算出应该下发给客户端的 WebSocket 地址。

    取值优先级：
      1. WB_SKY_WS_URL 显式指定（最稳，反代场景就该用这个）
      2. 请求 Host 头里带的端口（客户端直连 2007 时就是这个）
      3. WB_SKY_WS_PORT 指定的端口
      4. 退回"只有域名"（源码原行为）
    """
    import flask

    forced = (os.environ.get("WB_SKY_WS_URL") or "").strip()
    if forced:
        return forced

    scheme = "wss" if flask.request.is_secure else "ws"
    host = flask.request.host or ""

    def _join(name, port):
        if port and str(port) not in ("443", "80", ""):
            return "%s://%s:%s/account/ws" % (scheme, name, port)
        return "%s://%s/account/ws" % (scheme, name)

    if ":" in host:
        name, _, port = host.rpartition(":")
        if port.isdigit():
            return _join(name, port)
        return _join(name, "")

    return _join(host, (os.environ.get("WB_SKY_WS_PORT") or "").strip().lstrip(":"))


def _patch_login_ws_url(app=None):
    """把 logini 下发的 websocket_url 改成按实际访问地址拼。

    【踩坑记录】只替换模块属性 `account.auth_login.auth_login.logini` 是没用的：
    route/account.py 里用的是 `from ... import logini`，路由注册时抓走的是
    **原始函数对象**，事后再改模块属性纹丝不动。
    正确做法：拿到 app，直接替换 app.view_functions 里的那个视图函数。
    """
    if not _is_main_service():
        return None

    targets = []
    if app is not None:
        fn = app.view_functions.get("logini")
        if fn is not None and not getattr(fn, _WRAPPED, False):
            targets.append(("view_functions", app, fn))
    if not targets:
        try:
            import importlib
            mod = importlib.import_module("route.account")
            cur = getattr(mod, "logini", None)
            if cur is not None and not getattr(cur, _WRAPPED, False):
                targets.append(("module", mod, cur))
        except Exception:
            pass
    if not targets:
        return None

    patched = []
    for kind, holder, fn in targets:

        def make_wrapper(orig_fn):
            def logini_wrapper(*args, **kwargs):
                resp = orig_fn(*args, **kwargs)
                try:
                    import flask
                    from werkzeug.wrappers import Response as WResponse
                    r = resp[0] if isinstance(resp, tuple) else resp
                    if isinstance(r, WResponse) and getattr(r, "is_json", False):
                        data = r.get_json(silent=True)
                        if isinstance(data, dict) and "websocket_url" in data:
                            fixed = _public_ws_url()
                            if fixed and fixed != data["websocket_url"]:
                                data["websocket_url"] = fixed
                                r.set_data(flask.json.dumps(data))
                                r.headers["Content-Type"] = "application/json"
                except Exception:
                    pass
                return resp
            return logini_wrapper

        w = make_wrapper(fn)
        setattr(w, _WRAPPED, True)
        w.__wrapped__ = fn
        if kind == "view_functions":
            holder.view_functions["logini"] = w
        else:
            holder.logini = w
        patched.append(kind)

    return ("websocket_url 修正已装载 (%s)" % ",".join(patched)) if patched else None


def _install_login_hook():
    """等 Flask app 注册完路由再替换 logini。

    做法：包装 flask.Flask.register_blueprint —— 它就是"路由都注册好了"的时刻。
    （不用 after_request 之类的钩子，那些要等到第一个请求才触发。）
    """
    if not _is_main_service():
        return None
    try:
        import flask
    except Exception:
        return None

    orig = flask.Flask.register_blueprint
    if getattr(orig, _WRAPPED, False):
        return None

    def register_wrapper(self, *args, **kwargs):
        result = orig(self, *args, **kwargs)
        try:
            _patch_login_ws_url(self)
        except Exception as exc:
            _log("websocket_url 修正失败: %s" % exc)
        # ★ 这里正好是"route.account 已完整 import、路由已注册"的时刻，
        #   所以联机地址覆盖也挪到这里做（详见 _patch_account_endpoints 注释）。
        try:
            note = _patch_account_endpoints()
            if note:
                _log(note)
        except Exception as exc:
            _log("联机地址覆盖失败: %s" % exc)
        # 蓝图注册完 = 各模块都已 import，此时才改得到 live_panel 的 RTT 端口
        try:
            note = _patch_live_panel()
            if note:
                _log(note)
        except Exception:
            pass
        return result

    setattr(register_wrapper, _WRAPPED, True)
    register_wrapper.__wrapped__ = orig
    flask.Flask.register_blueprint = register_wrapper
    return "register_blueprint 钩子已装载（用于修正 websocket_url）"


# ===================================================================== #
# 3) live_panel.py 适配
# ===================================================================== #
def _retarget_sample(fn, port):
    """把 live_panel._sample_tcp_rtt 里写死的端口换成实际端口。

    源码里是这一行：
        ["ss", "-tinH", "state", "established", "( sport = :2999 )"]
    本包主服务跑 2007，不改的话采样永远空（而且不报错）。
    做法：读函数源码 → 按缩进反推 → 整体重打一遍缩进 → exec 成一个新函数。
    任何一步失败都退回原函数（RTT 空着，不影响别的）。
    """
    try:
        import inspect
        import textwrap

        src = inspect.getsource(fn)
        lines = src.split("\n")
        # 去掉 def 行之前的装饰器（保持简单：只处理 def）
        start = 0
        for i, ln in enumerate(lines):
            if ln.lstrip().startswith("def "):
                start = i
                break
        lines = lines[start:]
        # 原文里的缩进量（函数内代码比 def 多缩进一级）
        base = len(lines[0]) - len(lines[0].lstrip())
        body_indent = None
        for ln in lines[1:]:
            if ln.strip():
                body_indent = len(ln) - len(ln.lstrip())
                break
        if body_indent is None:
            return fn
        extra = body_indent - base
        body = "\n".join(ln[extra:] if ln.strip() else ln for ln in lines[1:])
        # 关键替换：把查询里的端口换成目标端口
        import re
        body = re.sub(r"sport\s*=\s*:\d+", "sport = :%d" % int(port), body)
        body = textwrap.indent(textwrap.dedent(body), "    ")
        wrapper_src = "def _wbsky_sample():\n" + body
        ns = {}
        exec(compile(wrapper_src, "<wbsky:live_panel._sample_tcp_rtt>", "exec"), fn.__globals__, ns)
        return ns["_wbsky_sample"]
    except Exception as exc:
        try:
            _log("RTT 采样端口改写失败（RTT 会空着，不影响其它功能）: %s" % exc)
        except Exception:
            pass
        return fn


def _patch_live_panel():
    """改写实时面板里写死的路径 / 端口 / 口令 / 来访限制。"""
    try:
        import live_panel as lp
    except Exception:
        return None

    if getattr(lp, "_wbsky_adapted", False):
        return None

    notes = []

    stats_url = (os.environ.get("WB_SKY_UDP_STATS_URL") or "").strip()
    if stats_url:
        lp.STATS_URL = stats_url
        notes.append("STATS_URL=%s" % stats_url)

    outbox = (os.environ.get("WB_SKY_WS_OUTBOX") or "").strip()
    if outbox:
        lp.WS_OUTBOX = outbox
        notes.append("WS_OUTBOX=%s" % outbox)

    live_user = (os.environ.get("WB_SKY_LIVE_USER") or "").strip()
    if live_user:
        lp.USER = live_user
        notes.append("USER=%s" % live_user)

    live_pass = os.environ.get("WB_SKY_LIVE_PASSWORD")
    if live_pass:
        # 只报"已设置"，不回显口令本身
        lp.PASSWORD = live_pass
        notes.append("PASSWORD=(已按 .env 设置)")

    # ---- RTT 采样：端口可配 + 没有 ss 命令时不刷错误日志 ----
    http_port = _env_int("WB_SKY_HTTP_PORT", None)
    if http_port:
        real_sample = getattr(lp, "_sample_tcp_rtt", None)
        if callable(real_sample) and not getattr(real_sample, _WRAPPED, False):
            import shutil

            def sample_wrapper():
                # `ss` 来自 iproute2。python:3.10-slim 镜像里没有，
                # 这时原来每次采样都会抛 FileNotFoundError 被吞掉 —— 不报错，
                # 但面板上永远没有 RTT。这里显式判一下，省掉无意义的异常。
                if shutil.which("ss") is None:
                    return None
                # ★ 端口改写：live_panel 源码里写死了 `( sport = :2999 )`，
                #   而本包主服务跑在 2007。不改的话采样永远匹配不到连接，
                #   面板上 RTT 一栏永远是空的，而且**不会报任何错**。
                #   做法：把真实函数交给 _retarget_sample() 去改端口。
                try:
                    return _retarget_sample(real_sample, http_port)()
                except Exception:
                    return real_sample()

            setattr(sample_wrapper, _WRAPPED, True)
            sample_wrapper.__wrapped__ = real_sample     # 便于排障/测试回看原函数
            lp._sample_tcp_rtt = sample_wrapper
            notes.append("RTT 采样端口=%d（无 ss 时跳过）" % http_port)

    # ---- /internal/rtt 的来访限制：容器间调用源 IP 是私网 ----
    if _flag("WB_SKY_TRUST_RFC1918", True):
        orig_guard = getattr(lp, "_guard", None)
        if callable(orig_guard) and not getattr(orig_guard, _WRAPPED, False):

            def _is_rfc1918(ip):
                try:
                    p = [int(x) for x in str(ip).split(".")]
                except Exception:
                    return False
                if len(p) != 4 or any(not (0 <= x <= 255) for x in p):
                    return False
                if p[0] == 10:
                    return True
                if p[0] == 172 and 16 <= p[1] <= 31:
                    return True
                if p[0] == 192 and p[1] == 168:
                    return True
                return False

            def guard_wrapper():
                try:
                    import flask
                    if flask.request.path == "/internal/rtt" and _is_rfc1918(flask.request.remote_addr):
                        # 走原始 _check_auth 之外的短路：直接放行
                        # （原来的实现在 localhost 之外一律 401，容器里就废了）
                        return None
                except Exception:
                    pass
                return orig_guard()

            setattr(guard_wrapper, _WRAPPED, True)
            guard_wrapper.__wrapped__ = orig_guard
            lp._guard = guard_wrapper
            notes.append("/internal/rtt 放行私网来源")

    lp._wbsky_adapted = True
    return ("live_panel 已适配: " + "; ".join(notes)) if notes else None


# ===================================================================== #
# 4) 建库容错
# ===================================================================== #
def _patch_db_resilience():
    """让 init_db() 连不上库时**只记日志**，不要把整个进程带走。

    index.py:60 是模块级的 database.init_db()，异常会直接冒到顶层。
    """
    if not _is_main_service():
        return None
    if not _flag("WB_SKY_SCHEMA_RESILIENT", True):
        return None

    try:
        import db as database
    except Exception:
        return None

    orig_init = getattr(database, "init_db", None)
    if not callable(orig_init) or getattr(orig_init, _WRAPPED, False):
        return None

    attempts = _env_int("WB_SKY_DB_RETRY", 3) or 1
    attempts = max(1, min(20, attempts))
    delay = 2.0

    def init_wrapper(*args, **kwargs):
        last = None
        for i in range(1, attempts + 1):
            try:
                return orig_init(*args, **kwargs)
            except Exception as exc:
                last = exc
                _log("建表/迁移 第 %d/%d 次失败: %s"
                     % (i, attempts, str(exc).split("\n")[0][:200]))
                if i < attempts:
                    time.sleep(delay)
        _log("=" * 68)
        _log("[!]  数据库初始化最终失败，但**服务继续启动**。")
        _log("    原因: %s" % str(last).split("\n")[0][:300])
        _log("    影响: 注册/登录/好友等依赖数据库的功能暂不可用。")
        _log("    不受影响: UDP 联机、聊天 WS —— 这就是不让进程退出的原因。")
        _log("    怎么修: 自查 deploy/.env 的 MYSQL_* 与 1Panel 里的库/账号，")
        _log("            确认容器内能解析 MYSQL_HOST（宿主机 MySQL 用")
        _log("            host.docker.internal）；修好后 docker compose restart wbsky。")
        _log("            （建表是 CREATE TABLE IF NOT EXISTS，幂等，重启即可补上）")
        _log("    想恢复「连不上就退出」的老行为: .env 里 WB_SKY_SCHEMA_RESILIENT=0")
        _log("=" * 68)
        return None

    setattr(init_wrapper, _WRAPPED, True)
    init_wrapper.__wrapped__ = orig_init
    database.init_db = init_wrapper
    return "建库容错已装载（重试 %d 次后只记日志）" % attempts


# ===================================================================== #
# 5) 启动自检：房间服端口一致性
# ===================================================================== #
def _check_udp_port_consistency():
    """config.json 与 .env 的房间服端口不一致时，明确告警。

    这是本套部署最容易踩的坑：客户端拿到的联机端口来自
    **下发值**（.env），而房间服真正 listen 的端口来自
    **sky0155-udp/config.json**（或 WB_SKY_UDP_PORT）。
    两边不一致的表现是：能登录、能进图，但**看不到其他玩家**，
    而且日志里一句错都没有 —— 极难定位。
    """
    expect = _env_int("WB_SKY_EXPECT_UDP_PORT", None)
    if not expect:
        return
    try:
        import json
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "sky0155-udp", "config.json")
        cfg_path = os.path.normpath(cfg_path)
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        actual = int(cfg.get("udp_server_port") or 0)
    except Exception:
        return
    if actual and actual != expect:
        _log("=" * 68)
        _log("[!]  房间服端口不一致：.env 下发的是 %d，sky0155-udp/config.json 里是 %d。"
             % (expect, actual))
        _log("    症状 = 能进图但看不到其他玩家（而且不会有任何报错）。")
        _log("    修法（二选一）：")
        _log("      A) 把 sky0155-udp/config.json 的 udp_server_port 改成 %d" % expect)
        _log("      B) 在 .env 里设 WB_SKY_UDP_PORT=%d（房间服进程会用它覆盖）" % actual)
        _log("=" * 68)


# ===================================================================== #
# 装载
# ===================================================================== #
def _install():
    # ★ 先补 sys.path，再打补丁：顺序反了的话 _patch_account_endpoints 会因为
    #   `No module named 'route'` 静默失效（详见 _bootstrap_sys_path 的注释）。
    try:
        added = _bootstrap_sys_path()
        if added:
            _log("sys.path 已补: %s" % ", ".join(added))
    except Exception as exc:
        _log("补 sys.path 失败（已忽略）: %s" % exc)

    # 注意：这里**不**调用 _patch_account_endpoints()。
    #   它依赖 route.account 已完整 import，而启动阶段（解释器加载 sitecustomize 时）
    #   这一点还不成立 —— 早期版本在这里调用，结果补丁被静默跳过，
    #   服务能起来但下发给客户端的联机地址还是 config.json 里的旧值
    #   （= "能登录、能进图、看不到人"，而且日志看着一切正常）。
    #   现在它由 _install_login_hook 里的 register_blueprint 钩子触发。
    done = []
    for fn in (_patch_flask_run,
               _install_login_hook,
               _patch_live_panel,
               _patch_db_resilience):
        try:
            note = fn()
        except Exception as exc:      # 任何一个补丁失败都不能拦住启动
            note = None
            _log("补丁 %s 失败（已忽略）: %s" % (fn.__name__, exc))
        if note:
            done.append(note)
    if done:
        _log(" | ".join(done))
    try:
        _check_udp_port_consistency()
    except Exception:
        pass


_install()
