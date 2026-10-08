# route/service.py
import json
import os
import sys
import time
import glob
from flask import Blueprint, request, jsonify, send_from_directory
from datetime import datetime

# 导入统一数据库模块
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import db as database

# 创建蓝图
service_bp = Blueprint('service', __name__, url_prefix='/service')

# ---------- 工具函数 ----------
def load_config():
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def _hidden_unlocks():
    """永远不要下发的解锁名（config.json 的 unlock_hide，默认 HubStatueForm）。

    ★★★ 2026-10-04 反汇编定论（CandleSpace/Objects.level.bin）：
      #348 OnUnlocked("HubStatueForm")
          notUnlockedFx      = #529 [#114 Timeline "hub statue"]
                                 └─ #184 Enable{objects:#233}   ← 唯一的 Enable
          onUnacknowledgedFx = #233 [雕像 SetRender + #455 MeditationArea(enabled=0)]
          onUnlockedFx       = 空
      ⇒ 只有把 HubStatueForm 报成**未解锁**，客户端才会走 notUnlockedFx 那条
        时间轴去 Enable #233（雕像 + 打坐点）；否则打坐点永远 enabled=0，
        表现就是"柱子看得见、坐下没反应、进不去 SkyHub2"。
      详见 route/account.py 的同名函数注释。
    """
    try:
        names = load_config().get("unlock_hide")
    except Exception:
        names = None
    if names is None:
        names = ["HubStatueForm"]
    if isinstance(names, str):
        names = [names]
    try:
        return {str(n) for n in names if str(n).strip()}
    except TypeError:
        return {"HubStatueForm"}


def _merge_reported_unlocks(user_id, payload, list_key):
    """兜底：把库里客户端上报过的解锁项并回 all_unlock_status.json 的返回。

    见 route/account.py 的同名函数说明。
    ★ 例外：`_hidden_unlocks()` 里的名字永远不并回，还要从列表里剔掉。
    """
    hidden = _hidden_unlocks()
    if hidden:
        base_items = payload.get(list_key)
        if isinstance(base_items, list):
            kept = [u for u in base_items
                    if not (isinstance(u, dict) and u.get("name") in hidden)]
            if len(kept) != len(base_items):
                payload[list_key] = kept
                for k in ("status_unlocks_total_count", "unlocks_total_count"):
                    if isinstance(payload.get(k), int):
                        payload[k] = len(kept)

    if not user_id:
        return payload
    try:
        raw = database.get_user_field(user_id, "unlocks")
    except Exception:
        return payload
    if not raw:
        return payload
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "ignore")
    try:
        reported = json.loads(raw) or []
    except (TypeError, ValueError):
        return payload

    items = payload.get(list_key) or []
    by_name = {}
    for u in items:
        if isinstance(u, dict) and u.get("name"):
            by_name[u["name"]] = u
    for it in reported:
        if not isinstance(it, dict):
            continue
        name = it.get("name")
        if not name:
            continue
        if name in hidden:
            # HubStatueForm 必须保持"未解锁"，客户端上报了也绝不并回。
            continue
        # 原样保留客户端上报的 ack（"已解锁但未回执"是真实状态）。
        rep_ack = bool(it.get("ack", False))
        if name in by_name:
            by_name[name]["ack"] = rep_ack
            if it.get("unlocked_at"):
                by_name[name]["unlocked_at"] = it["unlocked_at"]
            continue
        entry = {
            "name": name,
            "type": it.get("type", "level"),
            "ack": rep_ack,
            "unlocked_at": it.get("unlocked_at", 0),
        }
        if "cost" in it:
            entry["cost"] = it["cost"]
        items.append(entry)
        by_name[name] = entry
    payload[list_key] = items
    return payload

# ============================================================
# /service/message/ 路由
# ============================================================

@service_bp.route("/message/api/v1/get_all", methods=["POST"])
def get_all_messages():
    req = request.get_json(force=True, silent=True) or {}
    user = req.get("user") or ""

    msg_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mail_and_messages")
    files = glob.glob(os.path.join(msg_dir, "*.json"))

    messages = []
    for fp in files:
        with open(fp, encoding="utf-8") as f:
            msg = json.load(f)
            msg.setdefault("payload", {}) \
               .setdefault("headers", {})["to"] = user
            messages.append(msg)

    return jsonify({"status": "OK", "messages": messages})

@service_bp.route("/message/api/v2/get_all", methods=["POST"])
def get_all_messages_v2():
    req = request.get_json(force=True, silent=True) or {}
    user = req.get("user") or ""

    msg_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mail_and_messages")
    files = glob.glob(os.path.join(msg_dir, "*.json"))

    messages = []
    for fp in files:
        with open(fp, encoding="utf-8") as f:
            msg = json.load(f)
            msg.setdefault("payload", {}) \
               .setdefault("headers", {})["to"] = user
            messages.append(msg)

    return jsonify({"status": "OK", "messages": messages})

@service_bp.route("/message/api/v1/mark_received", methods=["POST"])
def mark_received():
    cfg = load_config()
    return send_from_directory(
        directory=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config'),
        path='mail_config.json',
        mimetype='application/json'
    )

@service_bp.route("/message/api/v1/mark_seen", methods=["POST"])
def mark_seen():
    req = request.get_json(force=True, silent=True) or {}
    ids = [m.get("id") for m in req.get("messages", [])]

    cfg_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "mail_config.json")
    if not os.path.isfile(cfg_path):
        return jsonify({"status": "OK", "messages": []}), 404

    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)

    matched = []
    for msg in cfg.get("messages", []):
        if msg.get("payload", {}).get("headers", {}).get("id") in ids:
            matched.append({
                "payload": {
                    "headers": {
                        "id": msg["payload"]["headers"]["id"],
                        "seen_at": int(time.time())
                    }
                }
            })

    return jsonify({"status": "OK", "messages": matched})

@service_bp.route("/message/api/v1/get", methods=["POST"])
def v1get():
    req = request.get_json(force=True, silent=True) or {}
    msg_id = req.get("message", {}).get("id")

    if not msg_id:
        return jsonify({"status": "OK", "messages": []}), 400

    cfg_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "mail_config.json")
    if not os.path.isfile(cfg_path):
        return jsonify({"status": "OK", "messages": []}), 404

    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)

    for msg in cfg.get("messages", []):
        if msg.get("payload", {}).get("headers", {}).get("id") == msg_id:
            return jsonify({
                "status": "OK",
                "messages": [
                    {
                        "payload": {
                            "headers": {
                                "id": msg_id,
                                "seen_at": int(time.time())
                            }
                        }
                    }
                ]
            })

    return jsonify({"status": "OK", "messages": []})

# ============================================================
# /service/status/ 路由
# ============================================================

@service_bp.route("/status/api/v1/get_unlocks", methods=["POST"])
def get_unlocks_status():
    cfg = load_config()

    if cfg.get("all_users_allunlock", False):
        # ★ 全解锁，但首次账号（未到过遇境）剔除"遇境到达"标记
        req = request.get_json(force=True, silent=True) or {}
        user_id = req.get("user")
        first_time = False
        if user_id:
            try:
                first_time = not int(database.get_user_field(user_id, "visited_home") or 0)
            except Exception:
                first_time = False
        base_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config")
        with open(os.path.join(base_dir, "all_unlock_status.json"), encoding="utf-8-sig") as _f:
            payload = json.load(_f)
        if first_time:
            forbid = {"first_loaded_CandleSpace", "first_loaded_SkyHub2", "SkyHubFirstArrival",
                      "first_loaded_HubReveal", "1stHub", "DayHub", "FinishedIntro"}
            payload["status_unlocks"] = [u for u in payload.get("status_unlocks", []) if u.get("name") not in forbid]
        # ★ 兜底：把客户端上报过的解锁项并回来（修「神殿坐下不传送」）
        payload = _merge_reported_unlocks(user_id, payload, "status_unlocks")
        if isinstance(payload.get("status_unlocks"), list) and "status_unlocks_total_count" in payload:
            payload["status_unlocks_total_count"] = len(payload["status_unlocks"])
        # ★ 2026-10-06 共享空间「物品放置栏」道具：CharSkyKid_Prop_*（type=level）
        #   原来一条都没有 ⇒ 放置栏打不开。数据源同一份 OutfitDefs.json。
        try:
            from outfit_unlock_helper import apply_prop_unlocks
            apply_prop_unlocks(payload, "status_unlocks")
        except Exception as _e:
            try:
                from route.account import _social_log
                _social_log("prop_unlock.err", repr(_e))
            except Exception:
                pass
        if isinstance(payload.get("status_unlocks"), list) and "status_unlocks_total_count" in payload:
            payload["status_unlocks_total_count"] = len(payload["status_unlocks"])
        # ★ 让「柱子一直能去」：离开遇境后主动让客户端忘掉 HubStatueForm
        #   （它与 account.get_unlocks 是同一个机制，见 account._hub_reset_delete_list）
        try:
            from route.account import _hub_reset_delete_list  # 延迟导入避免循环
            _del = _hub_reset_delete_list(user_id) if user_id else []
        except Exception:
            _del = []
        if _del:
            payload["delete_unlocks"] = _del
        return jsonify(payload)

    req = request.get_json(force=True, silent=True) or {}
    user = req.get("user")
    if not user:
        return jsonify({"error": "No user provided"}), 400

    if not database.user_exists(user):
        return jsonify({"error": "User not found"}), 404

    raw_data = database.get_user_field(user, "unlocks")
    data = json.loads(raw_data) if raw_data else []

    unlocks = [
        {
            "name": item.get("name", ""),
            "type": "level",
            "ack": item.get("ack", False),
            "unlocked_at": item.get("unlocked_at", 0)
        }
        for item in data
    ]

    payload = {
        "status_unlocks": unlocks,
        "status_unlocks_total_count": len(unlocks)
    }
    try:
        from outfit_unlock_helper import apply_prop_unlocks
        apply_prop_unlocks(payload, "status_unlocks")
        payload["status_unlocks_total_count"] = len(payload.get("status_unlocks") or [])
    except Exception as _e:
        try:
            from route.account import _social_log
            _social_log("prop_unlock.err", repr(_e))
        except Exception:
            pass
    return jsonify(payload)

@service_bp.route("/status/api/v1/delete_unlock", methods=["POST"])
def delete_unlock():
    return jsonify({})

@service_bp.route("/status/api/v1/add_unlock", methods=["POST"])
def add_unlock():
    """客户端上报「我刚解锁了 X」。

    ★ 2026-10-04：回包形状按参考服务器对齐
      （win2/Sky__Sky__service__status__add_unlock.py）：
        {"result":"ok","update_status_unlocks":[{name,type,ack,unlocked_at}]}
      已存在时多一个 "status":"already"。
      旧代码回 `{}` —— 客户端拿不到回执，解锁状态可能一直悬着不落地；
      而且每次上报都盲目 append，重复上报会把 users.unlocks 撑爆。
    """
    req = request.get_json(force=True, silent=True) or {}
    user = req.get("user")
    name = req.get("name")
    ack = req.get("ack")

    if not user or name is None or ack is None:
        return jsonify({"error": "missing parameters"}), 400

    if not database.user_exists(user):
        return jsonify({"error": "invalid session or user not found"}), 401

    # ★★★ 2026-10-04：`unlock_hide` 里的名字**永远不确认、也不落库**。
    #   缘由（用户实测「只去得了一次空巢」）：
    #   遇境雕像的时间轴 #114 "hub statue" 只存在于
    #   OnUnlocked(HubStatueForm).**notUnlockedFx** 里 —— 它才是唯一
    #   Enable(#233)（雕像 + 打坐点 #455）的地方。客户端一旦把 HubStatueForm
    #   记成"已解锁"，下次进遇境走的就是 onUnacknowledgedFx（没有 Enable）
    #   ⇒ 打坐点就没法用了。
    #   而客户端解锁是在时间轴末尾由 #186 Unlock 发起的，**它会不会"落地"
    #   取决于服务端这次回执**：我们原来回 update_status_unlocks=[HubStatueForm]
    #   ⇒ 客户端确认已解锁 ⇒ 只能去一次。
    #   这里对 hidden 名字直接回空回执 ⇒ 客户端始终停在"未解锁" ⇒ 每次进遇境
    #   时间轴都重播 ⇒ **柱子永远能坐、永远能去空巢**。
    if str(name) in _hidden_unlocks():
        return jsonify({"result": "ok", "update_status_unlocks": []})

    raw_data = database.get_user_field(user, "unlocks")
    unlocks = json.loads(raw_data) if raw_data else []

    response = {"result": "ok", "update_status_unlocks": []}
    for it in unlocks:
        if isinstance(it, dict) and it.get("name") == name:
            response["status"] = "already"
            response["update_status_unlocks"].append({
                "name": name,
                "type": it.get("type", "level"),
                "ack": bool(it.get("ack", False)),
                "unlocked_at": it.get("unlocked_at", 0),
            })
            break
    else:
        unlocked_at = int(datetime.utcnow().timestamp())
        entry = {"name": name, "type": "level", "ack": bool(ack),
                 "unlocked_at": unlocked_at}
        unlocks.append(entry)
        database.set_user_field(user, "unlocks", json.dumps(unlocks))
        response["update_status_unlocks"].append(entry)

    # ★ 客户端首次加载遇境会记录 first_loaded_CandleSpace / first_loaded_SkyHub2
    #   （SkyHub2 与 CandleSpace 同图别名）→ 标记"到过遇境"
    if name in ("first_loaded_CandleSpace", "first_loaded_SkyHub2", "SkyHubFirstArrival"):
        database.set_user_field(user, "visited_home", 1)

    return jsonify(response)

# ---------- Catch-All 兜底路由 ----------
# 未实现的 /service/ 接口返回空 JSON，避免客户端按钮消失

@service_bp.route("/", defaults={"path": ""}, methods=["POST", "GET"])
@service_bp.route("/<path:path>", methods=["POST", "GET"])
def service_catch_all(path):
    return jsonify({}), 200

# ===========================================================================
# 2026-10-04: service endpoints the client calls but we never implemented
# ===========================================================================
@service_bp.route("/auth/api/v1/token", methods=["POST"])
def auth_api_token():
    return jsonify({"result": "ok", "status": "ok"})


@service_bp.route("/relationship/api/v1/update_friend_constellation_pages", methods=["POST"])
def update_friend_constellation_pages():
    """The client decides which constellation slot holds a friend's star.

    We had no route for this at all (the client did call it once), so every
    layout it reported was dropped and the star position was lost on the next
    resync ("friend added but no candle in the constellation").
    """
    req = request.get_json(force=True, silent=True) or {}
    me = req.get("user") or req.get("user_id")
    pages = (req.get("pages") or req.get("constellation_friend_pages")
             or req.get("friend_pages"))
    if me and isinstance(pages, list) and pages:
        try:
            now = int(time.time())
            row = database.query_one(
                "SELECT id FROM friend_constellation_pages "
                "WHERE user_id = ? ORDER BY id ASC LIMIT 1", (me,))
            blob = json.dumps(pages, ensure_ascii=False)
            if row:
                database.execute(
                    "UPDATE friend_constellation_pages SET pages = ?, updated_at = ? WHERE id = ?",
                    (blob, now, row["id"]))
            else:
                database.execute(
                    "INSERT INTO friend_constellation_pages "
                    "(user_id, friend_id, page_id, pages, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?)", (me, "", "0", blob, now, now))
        except Exception:
            pass
    return jsonify({"result": "ok", "status": "ok", "ok": True})


# 2026-10-05: the version-matched reference implements these four
# /service/relationship endpoints. Without them every call fell through to the
# catch-all and returned {} -- yet "free_gifts" IS listed in resource_list, so
# the client had nothing usable to sync for that resource.
@service_bp.route("/relationship/api/v1/free_gifts/get_pending",
                  methods=["POST", "OPTIONS"])
def get_pending_free_gifts():
    return jsonify({"set_sent_free_gifts": [], "set_recvd_free_gifts": []})


@service_bp.route("/relationship/api/v1/free_gifts/send",
                  methods=["POST", "OPTIONS"])
def send_free_gift_options():
    # the reference implements this as a CORS-only stub too
    resp = jsonify({})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = (
        "Content-Type,Authorization,X-Requested-With")
    resp.headers["Access-Control-Allow-Methods"] = "POST,OPTIONS"
    resp.headers["Access-Control-Max-Age"] = "3600"
    return resp


@service_bp.route("/relationship/api/v1/get_all_friend_gist",
                  methods=["POST", "OPTIONS"])
def get_all_friend_gist():
    return jsonify({"get_all_friend_gist": []})


@service_bp.route("/relationship/api/v1/get_follower_count",
                  methods=["POST", "OPTIONS"])
def get_follower_count():
    return jsonify({"count": 0})
# ============ /service/* 补齐(必须挂在 service_bp 上! ) 2026-10-05 ============
# account_bp 带 /account 前缀, 挂在它下面的 /service/* 会变成 /account/service/*,
# 真实请求会落到 service_bp 的兜底 service_catch_all 回 {}。
# 端点契约(客户端 APK 反汇编 + 官方抓包):
#   stage/get  -> {level_id, props:[{name,type,pos,ori,scale,userDataBool,version}],
#                  sequence, stage_id, status:"OK", user_id}   ★无摆放也必须回 props:[] + OK
#   stage/set  -> {status:"OK"}
#   status/ack_unlock                            -> {result, update_status_unlocks:[...]}
#   status/add_unlocks_batch  (names + ack)      -> {result, update_status_unlocks:[...]}
#   status/delete_unlocks_batch (names)          -> {result, delete_status_unlocks:[...]}
#   inventory/unlocks/delete_many (names)        -> {result, delete_unlocks:[...]}
#   message/redeem (messages:[{id}])             -> {merge_messages:[...]}
import io as _sv_io
import json as _sv_json
import os as _sv_os
import threading as _sv_threading
import time as _sv_time

_SV_STATE = "/wbsky/config/gapfill_state.json"
_SV_LOG = "/wbsky/logs/social.log"
_SV_LOCK = _sv_threading.Lock()


def _sv_log(tag, msg):
    try:
        with _sv_io.open(_SV_LOG, "a", encoding="utf-8") as f:
            f.write("%s | %s | %s\n" % (_sv_time.strftime("%Y-%m-%dT%H:%M:%S"), tag, msg))
    except Exception:
        pass


def _sv_load():
    try:
        with _sv_io.open(_SV_STATE, encoding="utf-8") as f:
            d = _sv_json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _sv_save(d):
    try:
        tmp = _SV_STATE + ".tmp"
        with _SV_LOCK:
            with _sv_io.open(tmp, "w", encoding="utf-8") as f:
                _sv_json.dump(d, f, ensure_ascii=False)
            _sv_os.replace(tmp, _SV_STATE)
        return True
    except Exception as e:
        _sv_log("svc.save.err", repr(e))
        return False


def _sv_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _sv_user(req):
    return req.get("user") or req.get("user_id") or ""


def _sv_names(req):
    names = req.get("names")
    if not isinstance(names, list):
        names = [req.get("name")] if req.get("name") else []
    out = []
    for n in names:
        if isinstance(n, dict):
            n = n.get("name")
        if n:
            out.append(str(n))
    return out


def _sv_stage_key(owner, stage_id):
    return "%s|%s" % (owner or "self", stage_id or "")


# ---------------- 共享空间摆放 ----------------

@service_bp.route("/stage/api/v1/get", methods=["POST"])
def stage_get():
    req = request.get_json(force=True, silent=True) or {}
    uid = _sv_user(req)
    stage_id = str(req.get("stage_id") or "")
    level_id = str(req.get("level_id") or "")
    owner = str(req.get("user_id") or "")
    if not stage_id and level_id:
        stage_id = level_id + "_CNDL"
    if owner in ("", "00000000-0000-0000-0000-000000000000"):
        owner = uid
    st = _sv_load()
    stages = st.get("stages") if isinstance(st.get("stages"), dict) else {}
    rec = stages.get(_sv_stage_key(owner, stage_id)) or {}
    props = rec.get("props")
    _sv_log("stage.get", {"user": uid, "stage": stage_id,
                          "props": len(props or [])})
    return jsonify({
        "level_id": rec.get("level_id") or level_id,
        "props": props if isinstance(props, list) else [],
        "sequence": _sv_int(rec.get("sequence"), 0),
        "stage_id": stage_id,
        "status": "OK",
        "user_id": owner or "00000000-0000-0000-0000-000000000000",
    })


@service_bp.route("/stage/api/v1/set", methods=["POST"])
def stage_set():
    req = request.get_json(force=True, silent=True) or {}
    uid = _sv_user(req)
    stage_id = str(req.get("stage_id") or "")
    level_id = str(req.get("level_id") or "")
    owner = str(req.get("user_id") or "")
    if not stage_id and level_id:
        stage_id = level_id + "_CNDL"
    if owner in ("", "00000000-0000-0000-0000-000000000000"):
        owner = uid
    props = None
    for k in ("props", "prop", "items", "placements", "stage_props",
              "props_list", "objects", "props_data"):
        v = req.get(k)
        if isinstance(v, list):
            props = v
            break
    if props is None:
        # 认不出 props 就不动已有摆放(参考实现同样做法), 仍回 OK
        _sv_log("stage.set.unknown", {"user": uid, "stage": stage_id,
                                      "keys": sorted(req.keys())})
        return jsonify({"status": "OK"})
    clean = []
    for p in props:
        if isinstance(p, dict):
            clean.append({"name": str(p.get("name") or ""),
                          "type": str(p.get("type") or "unlock"),
                          "pos": str(p.get("pos") or "(0,0,0)"),
                          "ori": str(p.get("ori") or "(0,0,0,1)"),
                          "scale": str(p.get("scale") or "(1,1,1)"),
                          "userDataBool": bool(p.get("userDataBool") or False),
                          "version": _sv_int(p.get("version"), 0)})
    st = _sv_load()
    stages = st.setdefault("stages", {})
    k = _sv_stage_key(owner, stage_id)
    old = stages.get(k) if isinstance(stages.get(k), dict) else {}
    seq = _sv_int(req.get("sequence"), 0)
    if seq <= 0:
        seq = _sv_int(old.get("sequence"), 0) + 1
    stages[k] = {"props": clean, "sequence": seq, "level_id": level_id,
                 "stage_id": stage_id, "updated_at": _sv_int(_sv_time.time())}
    _sv_save(st)
    _sv_log("stage.set", {"user": uid, "stage": stage_id, "props": len(clean),
                          "sequence": seq})
    return jsonify({"status": "OK", "sequence": seq})


# ---------------- 批量解锁 ----------------

def _sv_unlock_apply(uid, names, ack=None, remove=False):
    st = _sv_load()
    users = st.get("users") if isinstance(st.get("users"), dict) else None
    if users is None:
        users = {}
        st["users"] = users
    u = users.get(uid)
    if not isinstance(u, dict):
        u = {}
        users[uid] = u
    cur = u.get("status_unlocks")
    if not isinstance(cur, list):
        cur = []
    by = {}
    for e in cur:
        if isinstance(e, dict) and e.get("name"):
            by[str(e["name"])] = e
    touched = []
    for n in names:
        if remove:
            by.pop(n, None)
            touched.append(n)
        else:
            e = by.get(n) or {"name": n, "type": "level",
                              "created_at": _sv_int(_sv_time.time())}
            if ack is not None:
                e["ack"] = bool(ack)
            e.setdefault("unlocked_at", 0)
            by[n] = e
            touched.append(e)
    u["status_unlocks"] = list(by.values())
    if remove:
        tomb = u.get("status_unlock_tombstone")
        if not isinstance(tomb, list):
            tomb = []
        u["status_unlock_tombstone"] = sorted(set(tomb + names))
    _sv_save(st)
    return touched


@service_bp.route("/status/api/v1/ack_unlock", methods=["POST"])
def status_ack_unlock():
    req = request.get_json(force=True, silent=True) or {}
    uid = _sv_user(req)
    names = _sv_names(req)
    touched = _sv_unlock_apply(uid, names, ack=True) if (uid and names) else []
    _sv_log("status.ack_unlock", {"user": uid, "n": len(names)})
    return jsonify({"result": "ok", "update_status_unlocks": touched})


@service_bp.route("/status/api/v1/add_unlocks_batch", methods=["POST"])
def status_add_unlocks_batch():
    req = request.get_json(force=True, silent=True) or {}
    uid = _sv_user(req)
    names = _sv_names(req)
    ack = bool(req.get("ack"))
    touched = _sv_unlock_apply(uid, names, ack=ack) if (uid and names) else []
    _sv_log("status.add_unlocks_batch", {"user": uid, "n": len(names), "ack": ack})
    return jsonify({"result": "ok", "update_status_unlocks": touched})


@service_bp.route("/status/api/v1/delete_unlocks_batch", methods=["POST"])
def status_delete_unlocks_batch():
    req = request.get_json(force=True, silent=True) or {}
    uid = _sv_user(req)
    names = _sv_names(req)
    if uid and names:
        _sv_unlock_apply(uid, names, remove=True)
    _sv_log("status.delete_unlocks_batch", {"user": uid, "n": len(names)})
    return jsonify({"result": "ok", "delete_status_unlocks": names})


@service_bp.route("/inventory/api/v1/unlocks/delete_many", methods=["POST"])
def inventory_delete_many():
    req = request.get_json(force=True, silent=True) or {}
    uid = _sv_user(req)
    names = _sv_names(req)
    if uid and names:
        st = _sv_load()
        users = st.setdefault("users", {})
        u = users.setdefault(uid, {})
        tomb = u.get("inv_unlock_tombstone")
        if not isinstance(tomb, list):
            tomb = []
        u["inv_unlock_tombstone"] = sorted(set(tomb + names))
        _sv_save(st)
    _sv_log("inventory.delete_many", {"user": uid, "n": len(names)})
    return jsonify({"result": "ok", "delete_unlocks": names})


@service_bp.route("/message/api/v1/redeem", methods=["POST"])
def message_redeem():
    """兑换消息附件。messages 是**对象数组** [{id}]; 响应键必须是 merge_messages。"""
    req = request.get_json(force=True, silent=True) or {}
    uid = _sv_user(req)
    msgs = req.get("messages")
    ids = []
    if isinstance(msgs, list):
        for m in msgs:
            if isinstance(m, dict):
                if m.get("id"):
                    ids.append(str(m["id"]))
            elif m:
                ids.append(str(m))
    now = _sv_int(_sv_time.time())
    out = [{"payload": {"headers": {"id": mid, "redeemed_at": now}}} for mid in ids]
    _sv_log("message.redeem", {"user": uid, "n": len(ids)})
    return jsonify({"merge_messages": out})
