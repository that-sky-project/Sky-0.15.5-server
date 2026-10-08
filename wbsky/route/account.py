# route/account.py
import json
import os
import sys
import uuid
import secrets
import hashlib
import tempfile
import threading
import time
import glob
from datetime import datetime
from flask import Blueprint, request, jsonify, send_from_directory, current_app

# 导入统一数据库模块（支持 MySQL 和 SQLite）
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import db as database

account_bp = Blueprint('account', __name__, url_prefix='/account')

# ---------- 工具函数（必须放在所有路由函数之前） ----------
def load_config():
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


# ---------- 联机地址 + 签名（2026-10-03 按参考服务器实测对齐）----------
# 参考服务器 thatskygame.de5.net 的 /account/hb、/find_previous_or_empty、
# /find_prev_or_empty 三个接口返回**同一个结构**：
#
#   {conn_queued, delay, level, level_hash, move_ts, new_cutoff,
#    other_players[], private_uri, sig, signature, uri}
#
# 关键实测结论：
#   * sig 与 signature 是**同一个值**（参考服务器上逐字节相同）
#   * 长度 64 = SHA256 十六进制。我们原来是 40 位（SHA1），格式就不对
#   * uri 与 private_uri 同值，形如 "IP:PORT"（参考是 103.91.208.146:40053）
#
# 客户端拿这个 uri 去连游戏服务器、并用 signature 做校验，所以这里必须
# 动态生成且两者一致。

def _record_client_ver(req):
    """记住这个客户端**自己上报**的版本号。

    ★ 2026-10-06 用户实测：好友传送报「需要最新客户端」。
      原因是 /account/join_friend_game 的 move_to_game 里 client_version 固定回
      5023（抄自官方 0.23+ 抓包），而本服 0.15.5 客户端自报 4852 ——
      客户端拿目标房间的版本和自己的比，判定"房间需要更新的客户端"就拒绝传送。
      所以这里把客户端在 find_previous_or_empty 里自报的 server_version /
      client_version（以及 UA 里的 build 号，当 client_changelist）存进 prefs，
      传送回包原样回显。
    """
    out = {}
    try:
        user = (req or {}).get("user") or (req or {}).get("user_id")
    except Exception:
        user = None
    try:
        sv = _int_or_none(_pick_any(req, "server_version", "server_ver"))
        cv = _int_or_none(_pick_any(req, "client_version", "client_ver"))
        if sv:
            out["server_version"] = sv
        if cv:
            out["client_version"] = cv
        try:
            ua = request.headers.get("User-Agent", "") or ""
            m = re.search(r"/(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?", ua)
            if m:
                out["client_changelist"] = m.group(4) or m.group(3)
                out["client_ua"] = ua[:120]
        except Exception:
            pass
        if user and out:
            _set_prefs(user, out)
    except Exception as e:
        try:
            _social_log("client_ver.err", repr(e))
        except Exception:
            pass
    return out


def _verify_teleport(user, friend, want_level, before_level=0, delay=8.0):
    """传送后自动验收（后台线程 delay 秒后比对）—— 见 join_previous_game 的调用点。

    ★ 2026-10-06 收紧判据：原来只比"和好友是否同图"，两人本来就同图时恒为真，
      证明不了传送生效。现在同时记录传送前后**我所在的图**：
        moved         = 传送后我换到了别的图
        same_as_friend= 传送后我和好友同图
        ok            = moved && same_as_friend   ← 这才是"跨图传送成功"
      只写日志，不改任何状态。
    """
    def _run():
        try:
            import udp_rooms
            after = udp_rooms.level_of_user(user, -1)
            fr_lv = udp_rooms.level_of_user(friend, -2)
            before = int(before_level or 0)
            moved = bool(after and before and after != before)
            same = bool(after and fr_lv and after == fr_lv)
            want = int(want_level or 0)
            ok = bool(moved and same)
            if not before:
                note = "传送前我不在房间里（无法判定是否移动）"
            elif ok:
                note = "跨图传送成功：已到好友那张图"
            elif same and not moved:
                note = "与好友同图但没移动过（本来就同图，不能据此判定传送）"
            elif moved and not same:
                note = "移动了但没到好友那张图"
            else:
                note = "没移动（好友不在房间 / 客户端未执行移动）"
            _social_log("teleport.verify", {
                "user": user, "friend": friend, "ok": ok,
                "before_level": before, "after_level": after,
                "moved": moved, "same_as_friend": same,
                "friend_level": fr_lv, "want_level": want, "note": note,
            })
        except Exception as e:
            try:
                _social_log("teleport.verify.err", repr(e))
            except Exception:
                pass
    try:
        import threading
        t = threading.Timer(float(delay), _run)
        t.daemon = True
        t.start()
    except Exception:
        pass


def _client_ver_of(user, default_cv=4852, default_sv=294, default_cl="179644"):
    """取该账号上报过的版本；没有就用本服 0.15.5 客户端的实测值兜底。"""
    p = {}
    try:
        p = _get_prefs(user) or {}
    except Exception:
        p = {}
    cv = _int_or_none(p.get("client_version")) or default_cv
    sv = _int_or_none(p.get("server_version")) or default_sv
    cl = str(p.get("client_changelist") or "").strip() or default_cl
    return {"server_version": sv, "client_version": cv, "client_changelist": cl}


def _sign_session(user, uri, move_ts):
    """生成 64 位十六进制签名（sha256）。sig 与 signature 用同一个值。"""
    raw = "%s|%s|%d" % (user or "", uri or "", int(move_ts or 0))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _udp_uri():
    """算出下发给客户端的联机地址 "host:port"。"""
    cfg = load_config()
    udp_host = str(cfg.get("udp_server_host", "auto") or "auto").strip()
    _env = os.environ.get("WB_SKY_UDP_HOST", "").strip()
    if _env:
        udp_host = _env
    if udp_host in ("", "auto", "0.0.0.0", "127.0.0.1", "localhost"):
        try:
            udp_host = request.host.split(":")[0]
        except Exception:
            udp_host = "127.0.0.1"
    try:
        udp_port = int(cfg.get("udp_server_port", 8125))
    except (TypeError, ValueError):
        udp_port = 8125
    return "%s:%d" % (udp_host, udp_port)


def _join_payload(body, extra=None):
    """构造 /hb 与 /find_previous_or_empty 共用的回包（字段对齐参考服务器）。"""
    cfg = load_config()
    user = (body or {}).get("user") or (body or {}).get("user_id") or ""
    uri = _udp_uri()
    move_ts = int(time.time())
    sig = _sign_session(user, uri, move_ts)
    out = {
        "conn_queued": False,
        "delay": int(cfg.get("hb_poll_delay", 30) or 30),
        "level": 0,
        "level_hash": 0,
        "move_ts": move_ts,
        "new_cutoff": 0,
        "other_players": [],
        "private_uri": uri,
        "sig": sig,
        "signature": sig,
        "uri": uri,
    }
    if extra:
        out.update(extra)
    return out


def _hidden_unlocks():
    """永远不要下发的解锁名（config.json 的 unlock_hide，默认 HubStatueForm）。

    ★★★ 2026-10-04 反汇编定论（CandleSpace/Objects.level.bin，564 节点全图走了一遍）：

      #348 OnUnlocked(name="HubStatueForm")
          ├─ notUnlockedFx      = #529 = [#114 Timeline "hub statue"]
          │      └─ timeNodes[4] = #184 Enable{enable:1, objects:#233}   ← **唯一的 Enable**
          ├─ onUnacknowledgedFx = #233 = [#37 IsLatestCheckpoint("SkyHub2"),
          │                               #350 SetRender(雕像mesh #61),
          │                               #183 DialogHint("intro_skyhub_00"),
          │                               #89  LevelMesh CandleSpaceStatue_01 (enabled=0),
          │                               #455 MeditationArea(type=2, needSit=1,
          │                                     onComplete→#561→#8 ChangeLevelWithFade("SkyHub2"))]
          └─ onUnlockedFx       = 空

      也就是说：**把 #233 整个打开的只有 notUnlockedFx 里那条时间轴上的 #184**。
      一旦服务端把 HubStatueForm 报成"已解锁"（无论 ack 真假），客户端就走
      onUnacknowledgedFx 分支 —— 雕像被 #350 SetRender 渲染出来（看着"柱子出来了"），
      但 #455 打坐点文件里写的 `enabled: 0` 永远没人去 Enable 它
      ⇒ 坐下没反应、进不去 SkyHub2（用户 2026-10-04 实测症状）。

      之前我在 _merge_reported_unlocks 里写的因果链是反的：以为
      onUnacknowledgedFx 才是"打坐点可用"，所以特意把 ack 保留成 false。
      真正的开关是**这一项根本不能出现在下发列表里**。

      另外 #70 HasReachedLevel("SkyHub2") 与 #215 OnUnlocked("QuestStoneForm")
      .onUnacknowledgedFx 是这条链的上游，所以：
        · checkpoints 里必须出现过 SkyHub2（force_spawn_level 已保证）
        · QuestStoneForm 必须保持"已解锁但未回执"（ack=false），不能删也不能 ack=true
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
    """把该账号在库里已上报过的解锁项合并进 all_unlock*.json 的返回结果。

    ★ 重要修复（2026-10-01）：客户端推进剧情靠的是解锁名（unlock name），
      只要有一项在服务端返回里**不存在**，客户端上报后永远收不到确认，
      就会卡死在那个动作上，而且**重启游戏也没用**（配置里根本没有这一项）。

      真实案例：客户端在晨岛/遇境枢纽的神殿冥想点「坐下」时会走
      `HubStatueForm`（7 大区雕像形态之一），但 all_unlock.json 里当时只有
      Day/Rain/Sunset/Dusk/Night/Storm 六个 StatueForm，**唯独漏了
      HubStatueForm** → 玩家坐下后不传送、一直卡在加载/过场。
      客户端其实已经把 HubStatueForm 上报进库了（见 users.unlocks），
      只是回包时被这张静态配置表盖掉了。

      这里做兜底：凡是客户端自己上报过的解锁名，一律并回返回列表
      —— 以后配置再漏项也能自愈，不用再逐个补 JSON。

      ★ 例外：`_hidden_unlocks()` 里的名字**永远不并回、还要从列表里剔掉**
        （HubStatueForm 必须让客户端走 notUnlockedFx，见 _hidden_unlocks 说明）。
    """
    # 先做隐藏项过滤 —— 必须放在所有 early return 之前，否则没上报过的账号
    # 会直接把静态表里的名字透出去。
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
            # ★★★ 2026-10-04：HubStatueForm 这类"必须保持未解锁"的项，
            #   客户端上报了也绝不并回（详见 _hidden_unlocks 的反汇编结论）。
            continue
        # ★★★ 2026-10-03：原样保留客户端上报的 ack（"已解锁但未回执"是真实状态）。
        #   注意：真正的因果链见 _hidden_unlocks() —— 那才是 SkyHub2 打坐点的开关。
        rep_ack = bool(it.get("ack", False))
        if name in by_name:
            # 客户端上报的状态优先（它才是真正的进度来源）
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


def _login_user_ok(user, device):
    """登录放行判定。

    ★ 重要修复（2026-10-01）：
      客户端本地会持久化自己的账号 id（files/AccountAuthInfo.bin）。
      一旦服务端换机 / 换库 / 清库（本项目就发生过：账号建在旧库上），
      登录就会 404 user not found → 客户端弹「服务器错误 / 账号验证失败」，
      并且**后续所有登录资源同步全部跳过**（日志里看不到 Syncing login resource）。
      这里改成：账号不存在就按客户端给的 id 直接补建一条，自愈。
      想恢复旧行为，在 config.json 里设 "auto_create_user_on_login": false。
    """
    if not user:
        return False
    if database.user_exists(user):
        return True

    try:
        auto = load_config().get("auto_create_user_on_login", True)
    except Exception:
        auto = True
    if not auto:
        return False

    try:
        database.create_user(user, device or str(uuid.uuid4()), secrets.token_hex(32))
        print("[login] auto-created missing user %s (device=%s)" % (user, device), flush=True)
        return True
    except Exception as e:
        print("[login] auto-create failed for %s: %r" % (user, e), flush=True)
        try:
            return database.user_exists(user)
        except Exception:
            return False

# ---------- 数据库初始化 ----------
# 使用统一的 db 模块，支持 MySQL/SQLite 自动切换
database.init_db()

# ============================================================
# 账户相关路由
# ============================================================

@account_bp.route("/get_latest_build_version", methods=["POST"])
def get_latest_build_version():
    cfg = load_config()
    return jsonify(
        {
            "latest_build_version": cfg["latest_build_version"],
            "min_gpu_rating": 270,
            "maintenance_mode": False,
            "maintenance_msg": "",
            "app_store_uri": cfg["app_store_uri"],
            "tos": {
                "tos_url": cfg["tos_url"],
                "pp_url": cfg["pp_url"],
                "version": "4"
            },
            "cs_enabled": True,
            "cs_db_enabled": False,
            "country_code": "CN"
        }
    )

@account_bp.route("/get_vars", methods=["POST"])
def get_vars():
    """客户端 Vars（服务端权威）。

    ★ 2026-10-04：**不要再随机生成 `welcome`**，也不要塞顶层 `websocket_enabled`。
      参考服务器（win3/Sky/account/get_vars/get_vars.py）的响应就是 `{"vars": {...}}`，
      `welcome` 是固定 GUID。随机 welcome 会让客户端每次开机都认为"配置变了"。
      vars.json 已整体替换为参考服务器的 94 个键（关键修复：
      ab_mainstreet_enabled=1 主街商店、beta_mode=1、c_max_friend_pages=10、
      enable_constellation_revisions=1 好友星座、level_storm=1 …）。
    """
    vars_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "vars.json")
    with open(vars_path, encoding="utf-8-sig") as f:
        data = json.load(f)
    if "vars" not in data or not isinstance(data["vars"], dict):
        data = {"vars": {}}
    data["vars"].setdefault("welcome", "7b4f7a67-0b62-4868-bdec-5ac00fcac09d")
    return jsonify({"vars": data["vars"]})

@account_bp.route("/get_motd", methods=["POST"])
def get_motd():
    cfg = load_config()
    current_timestamp = int(time.time())
    # ★ 公告从 config/motd.json 读取，改公告直接编辑该文件，无需改代码
    motd_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "motd.json")
    try:
        with open(motd_path, encoding="utf-8-sig") as _f:
            motd_data = json.load(_f)
    except Exception:
        motd_data = {}
    return jsonify({
        "motd_title": motd_data.get("motd_title", "wbSky"),
        "motd": motd_data.get("motd", "欢迎来到 wbSky 私服。"),
        "motd_timestamp": current_timestamp,
        "motd_version": motd_data.get("motd_version", 137688),
        "motd_buttons": motd_data.get("motd_buttons", []),
        "motd_season_end": motd_data.get("motd_season_end", 3408191999)
    })

@account_bp.route("/auth/login", methods=["POST"])
def new_login():
    req = request.get_json(force=True, silent=True) or {}
    user = req.get("user")
    device = req.get("device")

    if not user or not device:
        return jsonify({"error": "missing user or device"}), 400

    if not _login_user_ok(user, device):
        return jsonify({"error": "user not found"}), 404

    return jsonify({
        "authinfo":{
        "user": user,
        "device": device,
        "recovery_token": "",
        "key": "0000000000000000000000000000000000000000000000000000430079000000"
        },
        "session": secrets.token_hex(16),
        "resource_list": [
            "device_capabilities", "userdata", "set_friends", "set_friend_statues",
            "online_friends", "set_rank", "currency", "forge_rates", "motd",
            "outfit_defs", "achievements", "achievement_stats", "relationship_defs",
            "questionnaires", "event_schedule", "set_world_quests", "iap_list",
            "invite_list", "unlocks", "get_shop", "spirit_shops", "collectibles",
            "wing_buffs", "gift_messages", "set_app_badge_number",
            "get_consumable_defs", "get_consumables", "get_buff_defs", "get_buffs",
            "get_lootboxes", "checkpoints", "generic_shop_defs",
            "external_account_friends", "serendipity_matches", "get_star_tag_defs",
            "status_unlocks", "free_gifts", "get_map_defs", "achievement_defs",
            "achievement_stats_tracking", "event_currency_defs", "infractions",
            "messages", "chat", "external_links"
        ],
        "external_links": [],
        "player_badge_type": -1
    })

@account_bp.route("/auth/create", methods=["POST"])
def new_create():
    user_id = str(uuid.uuid4())
    device_id = str(uuid.uuid4())
    session = secrets.token_hex(16)
    recovery = secrets.token_hex(32)
    database.create_user(user_id, device_id, recovery)

    return jsonify({
        "authinfo":{
        "user": user_id,
        "device": device_id,
        "key": "no_bcrypt",
        "recovery_token": recovery
        },
        "session": session,
        "resource_list": [
            "device_capabilities", "userdata", "set_friends", "set_friend_statues",
            "online_friends", "set_rank", "currency", "forge_rates", "motd",
            "outfit_defs", "achievements", "achievement_stats", "relationship_defs",
            "questionnaires", "event_schedule", "set_world_quests", "iap_list",
            "invite_list", "unlocks", "get_shop", "spirit_shops", "collectibles",
            "wing_buffs", "gift_messages", "set_app_badge_number",
            "get_consumable_defs", "get_consumables", "get_buff_defs", "get_buffs",
            "get_lootboxes", "checkpoints", "generic_shop_defs",
            "external_account_friends", "serendipity_matches", "get_star_tag_defs",
            "status_unlocks", "free_gifts", "get_map_defs", "achievement_defs",
            "achievement_stats_tracking", "event_currency_defs", "infractions",
            "messages", "chat", "external_links"
        ]
    })

@account_bp.route("/login", methods=["POST"])
def auth_login():
    req = request.get_json(force=True, silent=True) or {}
    user = req.get("user")
    device = req.get("device")
    current_timestamp = int(time.time())

    if not user or not device:
        return jsonify({"error": "missing user or device"}), 400

    if not _login_user_ok(user, device):
        return jsonify({"error": "user not found"}), 404

    return jsonify({
        "user": user,
        "device": device,
        "key": "0000000000000000000000000000000000000000000000000000430079000000",
        "session": secrets.token_hex(16),
        "resource_list": [
            "device_capabilities", "userdata", "set_friends", "set_friend_statues",
            "online_friends", "set_rank", "currency", "forge_rates", "motd",
            "outfit_defs", "achievements", "achievement_stats", "relationship_defs",
            "questionnaires", "event_schedule", "set_world_quests", "iap_list",
            "invite_list", "unlocks", "get_shop", "spirit_shops", "collectibles",
            "wing_buffs", "gift_messages", "set_app_badge_number",
            "get_consumable_defs", "get_consumables", "get_buff_defs", "get_buffs",
            "get_lootboxes", "checkpoints", "generic_shop_defs",
            "external_account_friends", "serendipity_matches", "get_star_tag_defs",
            "status_unlocks", "free_gifts", "get_map_defs", "achievement_defs",
            "achievement_stats_tracking", "event_currency_defs", "infractions",
            "messages", "chat", "external_links"
        ],
        "external_links": [],
        "player_badge_type": -1,
        "motd_title": "你知道吗？",
        "motd": "在遇境的天上，有许多星星。这些星星来自先灵或你的友人，点亮他们吧！",
        "motd_timestamp": current_timestamp,
        "motd_version": 137688,
        "motd_buttons": [],
        "motd_season_end": 3408191999
    })

@account_bp.route("/create", methods=["POST"])
def auth_create():
    user_id = str(uuid.uuid4())
    device_id = str(uuid.uuid4())
    session = secrets.token_hex(16)
    recovery = secrets.token_hex(32)
    current_timestamp = int(time.time())

    database.create_user(user_id, device_id, recovery)

    return jsonify({
        "user": user_id,
        "device": device_id,
        "key": "no_bcrypt",
        "recovery_token": recovery,
        "session": session,
        "resource_list": [
            "device_capabilities", "userdata", "set_friends", "set_friend_statues",
            "online_friends", "set_rank", "currency", "forge_rates", "motd",
            "outfit_defs", "achievements", "achievement_stats", "relationship_defs",
            "questionnaires", "event_schedule", "set_world_quests", "iap_list",
            "invite_list", "unlocks", "get_shop", "spirit_shops", "collectibles",
            "wing_buffs", "gift_messages", "set_app_badge_number",
            "get_consumable_defs", "get_consumables", "get_buff_defs", "get_buffs",
            "get_lootboxes", "checkpoints", "generic_shop_defs",
            "external_account_friends", "serendipity_matches", "get_star_tag_defs",
            "status_unlocks", "free_gifts", "get_map_defs", "achievement_defs",
            "achievement_stats_tracking", "event_currency_defs", "infractions",
            "messages", "chat", "external_links"
        ],
        "motd_title": "你知道吗？",
        "motd": "在遇境的天上，有许多星星。这些星星来自先灵或你的友人，点亮他们吧！",
        "motd_timestamp": current_timestamp,
        "motd_version": 137688,
        "motd_buttons": [],
        "motd_season_end": 3408191999
    })

@account_bp.route("/support/has_reply", methods=["POST"])
def has_reply():
    cfg = load_config()
    return jsonify({
        "result": "ok",
        "has_reply": cfg["has_reply"]
    })

@account_bp.route("/device_capabilities/lookup", methods=["POST"])
def device_capabilities_lookup():
    cfg = load_config()
    return jsonify({
        "device_capabilities": {
            "gpu_rating": cfg["gpu_rating"],
            "hash_id": cfg["hash_id"]
        }
    })

@account_bp.route("/sync_user_data", methods=["POST"])
def syncserdata():
    req = request.get_json(force=True, silent=True) or {}
    user = req.get("user")
    if not user:
        return jsonify({"error": "Missing 'user' field"}), 400

    if not database.user_exists(user):
        return jsonify({"error": "Can't find user"}), 400

    # 读取已保存的 user_data
    stored_data = database.get_user_field(user, "user_data")

    # 如果客户端上传了数据，则保存
    if req.get("data"):
        data_str = json.dumps({
            "time": req.get("time"),
            "version": req.get("version"),
            "data": req.get("data")
        }, ensure_ascii=False)
        database.set_user_field(user, "user_data", data_str)

    # 返回已保存的数据（如果有），否则回传客户端上传的数据
    if stored_data:
        try:
            parsed = json.loads(stored_data)
            return jsonify({"userdata": parsed})
        except (json.JSONDecodeError, TypeError):
            pass

    return jsonify({
        "userdata": {
            "time": req.get("time", 0),
            "version": req.get("version", 0),
            "data": req.get("data", {})
        }
    })

@account_bp.route("/userdata", methods=["POST"])
def userdata():
    req = request.get_json(force=True, silent=True) or {}
    user = req.get("user")
    if not user:
        return jsonify({"error": "Missing 'user' field"}), 400

    if not database.user_exists(user):
        return jsonify({"error": "Can't find user"}), 400

    stored_data = database.get_user_field(user, "user_data")

    # 如果客户端上传了数据，则保存
    if req.get("data"):
        data_str = json.dumps({
            "time": req.get("time"),
            "version": req.get("version"),
            "data": req.get("data")
        }, ensure_ascii=False)
        database.set_user_field(user, "user_data", data_str)

    # 返回已保存的数据（如果有）
    if stored_data:
        try:
            parsed = json.loads(stored_data)
            return jsonify({"userdata": parsed})
        except (json.JSONDecodeError, TypeError):
            pass

    return jsonify({
        "userdata": {
            "time": req.get("time", 0),
            "version": req.get("version", 0),
            "data": req.get("data", {})
        }
    })

@account_bp.route("/get_friends", methods=["POST"])
def get_friends():
    """好友列表。

    ★ 2026-10-03：原来返回的是**写死的假好友**（固定那个 41f54dba-… 的 UUID），
      既和真实账号无关，也不带昵称/关系等级。现在改成读 friends 表
      （接受邀请时写入），返回结构保持原来的 `set_friends: [{update_friends, AccountFriend}]`
      形状不变（形状不对会让客户端解析失败），只是内容换成真好友。
    """
    req = request.get_json(force=True, silent=True) or {}
    me = _req_user(req)
    # ★ 2026-10-04：客户端会带 `players`（它关心/刚加的那几个人）。
    #   参考服务器（win3/account/get_friends）是按它过滤的：
    #     query.filter(Friendship.friend_id.in_(players))
    #   我们原来忽略这个参数、永远回全部好友 —— 客户端拿它做"这个刚加的人到底
    #   在我好友列表里没有"的判定，回的集合不对会让它一直重试（实测新客户端
    #   在日志里刷了 8000+ 次 get_friends）。
    want = req.get("players")
    if isinstance(want, str):
        want = [want]
    want = [str(x) for x in want] if isinstance(want, list) and want else []
    rows = []
    if me:
        try:
            if want:
                marks = ",".join(["?"] * len(want))
                rows = database.query_all(
                    "SELECT friend_id, nickname, level, created_at FROM friends "
                    "WHERE user_id = ? AND friend_id IN (%s) ORDER BY created_at ASC LIMIT 200"
                    % marks, tuple([me] + want))
            else:
                rows = database.query_all(
                    "SELECT friend_id, nickname, level, created_at FROM friends WHERE user_id = ? "
                    "ORDER BY created_at ASC LIMIT 200", (me,))
        except Exception as e:
            _social_log("get_friends.err", repr(e))
    # 2026-10-04b: the client polls get_friends with players=[<target>] right
    # after the in-world candle handshake and then waits for that target to
    # appear in set_friends. It does NOT POST give_candle for this flow, so
    # unless we materialise the relation here the client retries forever
    # (~250 ms observed, 4854 calls per log window) and the friend tree never
    # opens. Take the poll itself as the client's assertion of the friendship.
    if me and want:
        _have = set()
        for _r in (rows or []):
            if _r.get("friend_id"):
                _have.add(str(_r.get("friend_id")))
        _created = []
        for _pid in want:
            if not _pid or _pid == me or _pid in _have:
                continue
            # 2026-10-04d: `players` is a FILTER, not an assertion -- the client
            # puts every id it cares about in there, including vars["welcome"]
            # (7b4f7a67-..., a hardcoded uuid present in the official capture).
            # Only ids that exist in `users` may ever become "friends".
            try:
                _exists = database.query_one(
                    "SELECT id FROM users WHERE id = ?", (_pid,))
            except Exception:
                _exists = None
            if not _exists:
                _social_log("get_friends.autofriend.skip", {"pid": _pid})
                continue
            try:
                _ensure_friendship(me, _pid, ability_id=1, cost=0)
                _created.append(_pid)
            except Exception as e:
                _social_log("get_friends.autofriend.err", repr(e))
        if _created:
            _social_log("get_friends.autofriend",
                        {"me": me, "created": _created})
            try:
                _marks = ",".join(["?"] * len(want))
                rows = database.query_all(
                    "SELECT friend_id, nickname, level, created_at FROM friends "
                    "WHERE user_id = ? AND friend_id IN (%s) "
                    "ORDER BY created_at ASC LIMIT 200" % _marks,
                    tuple([me] + want))
            except Exception as e:
                _social_log("get_friends.reload.err", repr(e))
    # ── p90：一次性预取"每个好友都要查一次"的两张表，消掉 N+1 ──
    _fids = [str(r.get("friend_id")) for r in (rows or []) if r.get("friend_id")]
    _fr_batch = database.get_friendships_batch(me, _fids) if _fids else {}
    _nodes_batch = database.constellation_ensure_batch(me, _fids) if _fids else {}
    _outfit_rows = _load_outfit_rows(_fids)   # p91: 装扮行也一次取回
    items = []
    by_id = {}
    for idx, r in enumerate(rows or []):
        fid = r.get("friend_id")
        if not fid:
            continue
        ts = int(r.get("created_at") or 0) or (idx + 1)
        # ★ 2026-10-03：把 friendships 表的字段一并带上。
        #   客户端 AccountFriend 的解析字段（libBootloader.so 的 NetAccountTypes.cpp
        #   字符串簇）是：given_wax / recvd_wax / local_warp_blocked / local_blocked /
        #   when_created / unlinked_star_sku / hints / last_gift_time / nickname /
        #   created / friend_id / constellation_node_index。
        #   旧版只回了 friend_id/nickname/level，abilities、hints、constellation_node_index
        #   全缺 —— 客户端认为这条关系"没生效"，表现出来就是"给了蜡烛但加不上"。
        fr = {}
        try:
            fr = _fr_batch.get(str(fid)) or {}   # p90: 批量预取，不再逐个查库
        except Exception:
            fr = {}
        try:
            abilities = json.loads(fr.get("abilities") or "[]")
        except Exception:
            abilities = []
        try:
            hints = json.loads(fr.get("hints") or "[]")
        except Exception:
            hints = []
        node_index = _nodes_batch.get(str(fid), -1)   # p90: 批量预取
        created = int(fr.get("created_at") or ts or 0)
        # 2026-10-05: the in-game friend menu showed no outfit for any friend
        # because the barn entry carried no `outfit` key at all. Reuse exactly
        # the payload /account/get_remote_outfit serves (that path is known good).
        try:
            _fo = _outfit_payload(fid, _outfit_rows.get(str(fid)))   # p91: 用预取的行
        except Exception as e:
            _social_log("friend_entry.outfit.err", repr(e))
            _fo = None
        # ★ 2026-10-06：好友在线 = 它是否在 UDP 房间里。原来这里写死 False，
        #   客户端把所有人都画成离线（好友树上的传送/加入按钮状态也就不对）。
        try:
            import udp_rooms
            _online = bool(udp_rooms.is_in_game(fid))
            _online_level = int(udp_rooms.level_of_user(fid, 0) or 0)
        except Exception:
            _online, _online_level = False, 0
        item = {
            "outfit": _fo,
            "last_seen_outfit": _fo,
            "update_friends": True,
            "AccountFriend": fid,
            "friend_id": fid,
            "friend": fid,
            "user_id": fid,
            # ★ 2026-10：没名字时不再回退成 uuid 前 8 位，改为从名字池随机取一个
            #   （并落库，见 db.get_or_assign_friend_nickname 的说明）。
            #   老行为注释保留在下面，方便对照 / 出问题时切回去。
            # (win3 accept_invite.py:162 -> default_friend_nickname = user.id[:8])
            "nickname": database.get_or_assign_friend_nickname(
                user, fid,
                (fr.get("custom_name") or r.get("nickname") or "").strip()),
            "platform": "android",
            "device": "",
            "is_online": _online,
            "level_id": _online_level,
            "last_seen": None,
            "relationship": "friend",
            "status": "active",
            "level": fr.get("relationship_level", 0) or 0,
            "friend_level": fr.get("relationship_level", 0) or 0,
            "relationship_level": fr.get("relationship_level", 0) or 0,
            "abilities": abilities,
            "hints": hints,
            # ★ 参考服务器（win3/account/get_friends）用的字段名是 given/recvd，
            #   客户端 .so 串表里是 given_wax/recvd_wax —— 两个都发，各取所需。
            "given": int(fr.get("given") or 0),
            "recvd": int(fr.get("recvd") or 0),
            "given_wax": int(fr.get("given") or 0),
            "recvd_wax": int(fr.get("recvd") or 0),
            # 客户端 .so 里 AccountFriend 的字段名（0x103CBxx 串池），缺了会掉好友
            "unlinked_star_sku": "",
            "last_gift_time": int(fr.get("last_gift_time") or 0),
            "when_created": created,
            "created": created,
            "created_at": created,
            "player_badge_type": 0,
            "attitude": "0",
            "constellation_node_index": node_index,
            "local_blocked": 0,
            "local_warp_blocked": 0,
            "soft_deleted": 0,
            "friendUpdateTime": created or ts,
            "updateTime": created or ts,
        }
        items.append(item)
        by_id[str(fid)] = item
    # ★ 2026-10-06：官方用 update_online_friends:[{friend_id, level_id}]
    #   告诉客户端"好友现在在哪张图"（传送时要和 move_to_game.level 对上）。
    _online_upd = _dedup_by_fid(
        [{"friend_id": str(it.get("friend_id")),
          "level_id": int(it.get("level_id") or 0)}
         for it in items if it.get("is_online")])
    return jsonify({
        # ★★★ 2026-10-04 关键修复：`set_friends` 必须是**按 friend_id 索引的对象**！
        #   参考服务器 win3/account/get_friends 返回的是 `set_friends = {}` 字典，
        #   真机抓包也是 `"set_friends": {}`。我们原来发的是**数组** ——
        #   数组没法反序列化进客户端的 AccountFriendBarn，
        #   于是整份好友列表被丢弃 ⇒ 表现就是「好友树一点关注就退回未加好友状态」。
        # 2026-10-04g: the version-matched reference (-For-0.13.4-) returns
        #   "set_friends": [{"update_friends": true, "AccountFriend": "<uuid>"}]
        # i.e. a LIST. Unity JsonUtility cannot deserialise into a Dictionary,
        # so AccountFriendBarn cannot be fed a keyed object -- with one the
        # client believes it has no friends and re-polls forever.
        "set_friends": items,
        "result": "success",
        "user_id": me or "",
        "total_friends": len(items),
        "page_max": int(req.get("page_max") or 50) or 50,
        "page_offset": int(req.get("page_offset") or 0),
        "timestamp": int(time.time() * 1000),
        # ★ 客户端 .so 的 get_friends 响应字段是
        #   `set_friend_count` + `set_friends` + `update_friend_commerce`（0xFF210F 区）。
        #   set_friend_count 必须给（客户端拿它跟 set_friends 对账，
        #   不对账就 assert "m_friendCount == kAccount_FriendMaxCount" 并丢弃好友）。
        "set_friend_count": len(items),
        # ★★★ 2026-10-04：客户端 .so 里 AccountFriendBarn 的响应字段就是
        #   `set_friend_count` + `set_friends` + **`update_friend_commerce`**
        #   （0xFF210F 串池；反汇编 fn 0x3db780）。
        #   我们上次发的是**数组** `[]`，真机立刻报
        #     W Account: Failed to parse resource sync response of AccountFriendBarn
        #                for syncAction update_friend_commerce
        #   ⇒ 它不是列表，而是**按好友 id 索引的对象/映射**（和 set_friends 同形）。
        #   所以这里发空对象 `{}`：键在、类型对、没有内容 → 不会再触发解析失败，
        #   也不会像"缺键"那样让客户端反复 resync。
        "update_online_friends": _online_upd,
        "update_friend_commerce": {},
    })

def _friend_entry(user_id, fid):
    """构造一条客户端认的 AccountFriend 条目（含 friendUpdateTime）。"""
    nick = ""
    ts = 0
    try:
        row = database.query_one(
            "SELECT nickname, created_at FROM friends WHERE user_id = ? AND friend_id = ?",
            (user_id, fid))
        if row:
            nick = row.get("nickname") or ""
            ts = int(row.get("created_at") or 0)
    except Exception as e:
        _social_log("friend_entry.err", repr(e))
    try:
        _fo = _outfit_payload(fid)
    except Exception:
        _fo = None
    return {
        "update_friends": True,
        "AccountFriend": fid, "friend_id": fid, "friend": fid,
        "nickname": database.get_or_assign_friend_nickname(user_id, fid, nick),
        "level": 1, "friend_level": 1,
        "outfit": _fo, "last_seen_outfit": _fo,
        "friendUpdateTime": ts or 1, "updateTime": ts or 1,
    }


def _friend_update_entry(user_id, fid):
    """官方 /account/set_friend_name 里 update_friends 的那条（逐字段照抓包）。

    官方 account__set_friend_name.json:
      {"result": true, "update_friends": [{"abilities": [...], "created_at": "...",
        "friend_id": "...", "hints": [...], "last_seen_outfit": null,
        "nickname": "UWAAA", "soft_deleted": null, "they_soft_deleted": null}]}
    ★ p76：以前回的是 set_friends:[...]，而客户端 set_friends 是**字典**、改名走的是
      update_friends —— 类型对不上会让整轮好友同步失败、好友树被重置。
    """
    nick = ""
    created = None
    try:
        row = database.query_one(
            "SELECT nickname, created_at FROM friends WHERE user_id = ? AND friend_id = ?",
            (user_id, fid))
        if row:
            nick = (row.get("nickname") or "").strip()
            created = row.get("created_at")
    except Exception as e:
        _social_log("friend_update_entry.err", repr(e))
    abilities, hints = [], []
    try:
        fr = database.get_friendship(user_id, fid) or {}
        abilities = json.loads(fr.get("abilities") or "[]")
        if not isinstance(abilities, list):
            abilities = []
        hints = json.loads(fr.get("hints") or "[]")
        if not isinstance(hints, list) or not hints:
            hints = abilities
    except Exception:
        pass
    if hasattr(created, "isoformat"):
        try:
            created = created.isoformat() + ("" if getattr(created, "tzinfo", None) else "Z")
        except Exception:
            created = str(created)
    return {
        "abilities": abilities,
        "created_at": created,
        "friend_id": fid,
        "hints": hints,
        "last_seen_outfit": None,
        "nickname": database.get_or_assign_friend_nickname(user_id, fid, nick),
        "soft_deleted": None,
        "they_soft_deleted": None,
    }


def _friend_pair(req):
    """从请求里取 (我, 对方)。字段名在客户端串表里是 /friend /target /recv_user。"""
    me = _req_user(req)
    fid = _pick_any(req, "friend", "target", "recv_user", "friend_id", "user_id")
    return me, (str(fid).strip() if fid else "")


@account_bp.route("/set_friend_name", methods=["POST"])
def set_friend_name():
    """给好友改昵称 —— 游戏里对方头顶那个「铅笔」按钮点的就是它。

    ★ 客户端字段（libBootloader.so 字符串簇）：`/friend` + `/name`。
      URL 串紧跟在 `AccountMessageType` 那一片里：
        /name | /account/drop_unlock | /friend | /account/set_friend_name |
        /account/set_friend_favorite | /blocked | /account/set_friend_block |
        /mute | /account/set_friend_mute | /warp_blocked | /account/set_friend_warp_blocked |
        /hint | /account/remove_relationship_hint
      之前这些路由**全都没实现** → 被 catch-all 兜成 `{}` → 铅笔点了没有任何反应。
    """
    req = request.get_json(force=True, silent=True) or {}
    _social_log("set_friend_name.req", req)
    me, fid = _friend_pair(req)
    name = _pick_any(req, "name", "nickname", "friend_name")
    name = ("" if name is None else str(name))[:190]
    if not me or not fid:
        return jsonify({"error": "missing user or friend"}), 400
    try:
        if not database.query_one(
                "SELECT user_id FROM friends WHERE user_id = ? AND friend_id = ?", (me, fid)):
            database.execute(
                "INSERT INTO friends (user_id, friend_id, nickname, level, created_at) "
                "VALUES (?,?,?,?,?)", (me, fid, name, 0, _now()))
        else:
            database.execute(
                "UPDATE friends SET nickname = ? WHERE user_id = ? AND friend_id = ?",
                (name, me, fid))
    except Exception as e:
        _social_log("set_friend_name.err", repr(e))
    entry = _friend_update_entry(me, fid)
    # ★ p76：官方只回 result + update_friends；多给 set_friends（且是列表）会让
    #   客户端的 set_friends 解析（字典）失败 ⇒ 好友树重置。
    resp = {"result": True, "update_friends": ([entry] if entry else [])}
    _social_log("set_friend_name.resp", {"user": me, "friend": fid, "name": name})
    return jsonify(resp)


def _friend_flag_route(flag):
    """set_friend_favorite / block / mute / warp_blocked。

    ★ p76：以前回 {"set_friends": [条目]}（列表）——客户端 set_friends 是字典，
      解析失败会把整棵好友树重置（用户实测：点"特别关注"或改备注后好友树重置）。
      现在统一按官方 /account/set_friend_name 的形状回 result + update_friends。

    ★ 特别关注（favorite）：**不做任何状态改动**（等于把这个功能停用），
      只回一份好友更新，客户端 UI 不会因此回退/重置。
    """
    req = request.get_json(force=True, silent=True) or {}
    me, fid = _friend_pair(req)
    value = _pick_any(req, flag, "value", "enabled", "on")
    _social_log("set_friend_%s" % flag, {"user": me, "friend": fid, "value": value,
                                         "noop": flag == "favorite"})
    if not me or not fid:
        return jsonify({"error": "missing user or friend"}), 400
    entry = _friend_update_entry(me, fid)
    return jsonify({"result": True, "update_friends": ([entry] if entry else [])})


@account_bp.route("/set_friend_favorite", methods=["POST"])
def set_friend_favorite():
    return _friend_flag_route("favorite")


@account_bp.route("/set_friend_block", methods=["POST"])
def set_friend_block():
    return _friend_flag_route("blocked")


@account_bp.route("/set_friend_mute", methods=["POST"])
def set_friend_mute():
    return _friend_flag_route("mute")


@account_bp.route("/set_friend_warp_blocked", methods=["POST"])
def set_friend_warp_blocked():
    return _friend_flag_route("warp_blocked")


@account_bp.route("/remove_relationship_hint", methods=["POST"])
def remove_relationship_hint():
    """清掉头顶那个「关系提示」图标（铅笔/蜡烛气泡）。

    客户端字段：`/hint`。之前没实现 → 提示消不掉，一直挂在对方头顶。
    """
    req = request.get_json(force=True, silent=True) or {}
    _social_log("remove_relationship_hint", req)
    me, fid = _friend_pair(req)
    hint = _pick_any(req, "hint", "hint_name", "name") or ""
    return jsonify({"result": "ok", "status": "ok", "ok": True,
                    "friend": fid, "hint": str(hint)})


@account_bp.route("/get_rank", methods=["POST"])
def get_rank():
    """玩家社交排名（resource_list 里的 `set_rank` 资源）。

    ★★★ 2026-10-04 关键补齐：我们**根本没有这个路由** —— 客户端的
    resource_list 里有 `set_rank`，客户端会来拉这个资源；落到 catch-all 只有
    `{}`，响应里**没有 `set_rank` 成员**，客户端解析这个资源失败。
    参考服务器（win3/account/get_rank）与真机抓包都是：
        {"set_rank": {"social": 0.0, "is_shepherd": false, "is_beginner": false}}
    缺这个成员会让整轮 AccountResource 同步中断 —— 表现就是
    「商店一直维护中 / 好友树点关注就回退 / 货币看着被重置」这一类
    「资源没同步上」的症状。
    """
    return jsonify({"set_rank": {
        "social": 0.0,
        "is_shepherd": False,
        "is_beginner": False,
    }})


@account_bp.route("/commerce/friend_info", methods=["POST"])
def commerce_friend_info():
    """好友商店/交易信息（客户端 AccountFriendCommerceInfoRequest → /account/commerce/friend_info）。

    ★ 2026-10-04：**不要再返回 `update_friend_commerce: []`**。
      客户端的 AccountFriendBarn 解析这个资源时，多出来的
      `update_friend_commerce` 空数组会触发
      "Failed to parse resource sync response of AccountFriendBarn"
      → 整个好友树同步失败、已加的好友被丢掉。
      参考服务器的这个接口只回 result/friend 这类短字段。
    """
    req = request.get_json(force=True, silent=True) or {}
    me, fid = _friend_pair(req)
    return jsonify({"result": "success", "status": "success",
                    "friend": fid, "user_id": me,
                    "friend_info": {}, "commerce": [],
                    "set_friend_commerce": []})


@account_bp.route("/get_friend_statues", methods=["POST"])
def get_friend_statues():
    """好友星座（星星）列表 + 星座页。

    客户端字段（.so 字符串簇）：set_friend_statues / update_friend_statues /
    sort_ver / constellation_friend_pages。

    ★ 2026-10-04 对齐参考服务器（win3/account/get_friend_statues + 真机抓包）：
      外层信封必须有 result / user_id / total_friends，
      并且 `set_friend_statues` 要**真的把已加的好友列出来**（原来永远回空数组，
      客户端认为一个好友都没有 ⇒ 遇境星座上不长蜡烛、好友树点关注就被回退）。
      每个星座页同时给 `name` 和 `page_index` 两个键（两代客户端各取所需）。
    """
    req = request.get_json(force=True, silent=True) or {}
    me = _req_user(req)

    # ---- 星座页（10 页，每页若干好友）----
    pages = []
    if me:
        try:
            row = database.query_one(
                "SELECT pages FROM friend_constellation_pages "
                "WHERE user_id = ? ORDER BY id ASC LIMIT 1", (me,))
            if row and row.get("pages"):
                pages = json.loads(row["pages"])
        except Exception as e:
            _social_log("get_friend_statues.err", repr(e))
            pages = []
    if not isinstance(pages, list):
        pages = []
    for i, pg in enumerate(pages):
        if not isinstance(pg, dict):
            continue
        if "page_index" not in pg:
            pg["page_index"] = i
        # authority page object is exactly {page_index, friends}
        pg.pop("name", None)
    # 2026-10-04h: the authority (win3 get_friend_statues.py:243-271) builds the
    # page list from pages_dict, i.e. it emits ONLY pages that actually hold a
    # friend. Emitting ten mostly-empty pages made the Home constellation chart
    # render wrong.
    pages = [pg for pg in pages if isinstance(pg, dict) and pg.get("friends")]

    # ---- 好友星星（set_friend_statues）----
    statues = []
    if me:
        try:
            rows = database.query_all(
                "SELECT friend_id, nickname FROM friends WHERE user_id = ? "
                "ORDER BY created_at ASC LIMIT 200", (me,))
        except Exception as e:
            _social_log("get_friend_statues.rows.err", repr(e))
            rows = []
        # ── p90：一次性预取，消掉"每个好友一次查询" ──
        _fids = [str(r.get("friend_id")) for r in (rows or []) if r.get("friend_id")]
        _nodes_batch = database.constellation_ensure_batch(me, _fids) if _fids else {}
        _fr_batch = database.get_friendships_batch(me, _fids) if _fids else {}
        for r in (rows or []):
            fid = r.get("friend_id")
            if not fid:
                continue
            node = _nodes_batch.get(str(fid), -1)
            # full AccountFriendStatue entry (see the reference server)
            fr = _fr_batch.get(str(fid)) or {}
            try:
                _ab = [int(a) for a in json.loads(fr.get("abilities") or "[]")]
            except Exception:
                _ab = []
            try:
                _lvl = int(fr.get("relationship_level") or 1)
            except (TypeError, ValueError):
                _lvl = 1
            try:
                _wc = int(fr.get("created_at") or 0)
            except (TypeError, ValueError):
                _wc = 0
            try:
                _st_outfit = _outfit_payload(fid)
            except Exception as e:
                _social_log("statue.outfit.err", repr(e))
                _st_outfit = None
            # ★ 2026-10-06：星盘条目也要带"好友当前在哪张图"——
            #   好友树/星盘的传送入口就是从这里读的。
            try:
                import udp_rooms as _urst
                _st_on = bool(_urst.is_in_game(fid))
                _st_lv = int(_urst.level_of_user(fid, 0) or 0)
            except Exception:
                _st_on, _st_lv = False, 0
            statues.append({
                "friend_id": fid,
                "user_id": fid,
                "is_online": _st_on,
                "level_id": _st_lv,
                "last_seen_outfit": _st_outfit,
                "recvd": int(fr.get("recvd") or 0),
                "given": int(fr.get("given") or 0),
                "nickname": database.get_or_assign_friend_nickname(
                    me, fid,
                    (str(fr.get("custom_name") or "").strip()
                     or str(r.get("nickname") or "").strip())),
                "when_created": _wc,
                "level": _lvl,
                "abilities": _ab,
                "outfit": _st_outfit,
                "player_badge_type": 0,
                "constellation_node_index": node,
                "page_index": 0 if node is None or node < 0 else (node // 8),
            })

    statues = _dedup_by_fid(statues)
    return jsonify({
        "set_friend_statues": statues,
        # the -For-0.13.4- reference spells this key set_friends_statues;
        # emitting both costs nothing and covers whichever the client reads.
        "set_friends_statues": statues,
        "update_friend_statues": [],
        "friend_statues": statues,
        "constellation_friend_pages": pages,
        "friend_pages": pages,
        "result": "success",
        "user_id": me or "",
        "total_friends": len(statues),
        "total_pages": len(pages),
        "timestamp": int(time.time() * 1000),
    })

@account_bp.route("/get_online_friends", methods=["POST"])
def get_online_friends():
    """2026-10-05: we used to return four empty lists. The reference
    (win3 account/get_online_friends/get_online_friends.py:195-207) returns a
    populated `online_friends` list, and each entry carries
    `constellation_position` {page_index, node_index} -- that is what puts a
    friend's star on the Home constellation -- plus nickname and outfit.
    An empty list meant the friend star chart had nothing to draw."""
    req = request.get_json(force=True, silent=True) or {}
    me = req.get("user") or req.get("user_id")
    now = int(time.time())
    out = []
    _online_n = 0
    rows = []
    try:
        rows = database.query_all(
            "SELECT friend_id, nickname FROM friends WHERE user_id = ? "
            "ORDER BY created_at ASC LIMIT 200", (me,)) or []
    except Exception as e:
        _social_log("get_online_friends.rows.err", repr(e))
    _fids = [str(r.get("friend_id")) for r in (rows or []) if r.get("friend_id")]
    _fr_batch = database.get_friendships_batch(me, _fids) if _fids else {}
    for r in rows:
        fid = r.get("friend_id")
        if not fid:
            continue
        fr = _fr_batch.get(str(fid)) or {}   # p90: 批量预取
        nick = database.get_or_assign_friend_nickname(
            me, fid,
            (str(fr.get("custom_name") or "").strip()
             or str(r.get("nickname") or "").strip()))
        node = None
        try:
            node = database.add_to_constellation(me, fid)
        except Exception:
            node = None
        pos = None
        if node is not None and int(node) >= 0:
            pos = {"page_index": int(node) // 8, "node_index": int(node)}
        outfit = None
        try:
            outfit = _outfit_payload(fid)
        except Exception:
            outfit = None
        # ★ 2026-10-06：原来给每个好友写死 is_online=True。真值来自 UDP 房间状态。
        try:
            import udp_rooms
            _on = bool(udp_rooms.is_in_game(fid))
            _lv = int(udp_rooms.level_of_user(fid, 0) or 0)
        except Exception:
            _on, _lv = False, 0
        if _on:
            _online_n += 1
        out.append({
            "friend_id": fid,
            "nickname": nick,
            "platform": "android",
            "device": "",
            "device_name": "",
            "is_online": _on,
            "level_id": _lv,
            "last_activity": now,
            "constellation_position": pos,
            "outfit_available": outfit is not None,
            "outfit": outfit,
        })
    out = _dedup_by_fid(out)
    body = {
        "online_friends": out,
        "refresh_online_friends": out,
        "set_online_friends": out,
        "update_online_friends": _dedup_by_fid(
            [{"friend_id": f.get("friend_id"),
              "level_id": int(f.get("level_id") or 0)}
             for f in out if f.get("is_online")]),
        "total_online": _online_n,
        "total_friends": len(out),
        "timestamp": now,
        "user_id": me or "",
    }
    _social_log("get_online_friends.resp", {"user": me, "n": len(out)})
    return jsonify(body)


@account_bp.route("/get_blocked_friends", methods=["POST"])
def get_blocked_friends():
    """拉黑列表。客户端字段：set_blocked_friends / page_next / page_prev。"""
    return jsonify({"set_blocked_friends": [], "blocked_friends": [],
                    "page_next": 0, "page_prev": 0})

# ---------------------------------------------------------------------------
# 货币快照
# ---------------------------------------------------------------------------
# 客户端 currency 快照的**固定字段集**：
#   来源 ① 参考服务器 win3/Sky/currency_helper.py 的 _BASE_FIELDS
#   来源 ② 真机抓包 skyqw服/static_responses/account%2Fget_currency.json（23 个键）
# 为什么必须固定：客户端把**每一个**返回 currency 的接口都当成"货币最新快照"，
# 并按字段做差值滚动动画。不同接口字段集合不一致时，缺的字段会被动画到 0
# 再涨回来 —— 表现就是"送 3 颗心，心先掉光再涨回原值"、"买完看不到扣费"。
CURRENCY_KEYS = (
    "candles", "wax", "vip", "heart", "rainbow_candle", "rainbow_heart",
    "day_temple", "storm_key", "dawn_heart", "day_heart", "rain_heart",
    "dusk_heart", "sunset_heart", "night_heart", "storm_heart",
    "season_candle", "season_wax", "prestige", "prestige_vip", "prestige_wax",
    "season_pass_token", "heart_wax", "season_heart",
)

# currency 表列名 -> 客户端字段名（注意 DB 里叫 hearts，客户端要 heart）
_CURRENCY_COLUMN_MAP = {
    "hearts": "heart",
    "heart_wax": "heart_wax",
    "season_candle": "season_candle",
    "season_heart": "season_heart",
    "season_pass_token": "season_pass_token",
    "season_wax": "season_wax",
    "season_wax_14": "season_wax_14",
    "wax": "wax",
    "prestige": "prestige",
    "prestige_vip": "prestige_vip",
    "prestige_wax": "prestige_wax",
    "vip": "vip",
    "rainbow_candle": "rainbow_candle",
    "rainbow_heart": "rainbow_heart",
    "day_temple": "day_temple",
    "storm_key": "storm_key",
    "dawn_heart": "dawn_heart",
    "day_heart": "day_heart",
    "rain_heart": "rain_heart",
    "dusk_heart": "dusk_heart",
    "sunset_heart": "sunset_heart",
    "night_heart": "night_heart",
    "storm_heart": "storm_heart",
}


def build_currency(user, candles=None):
    """构造客户端认的**完整** currency 字典（23 个键一个都不缺）。

    所有会回 currency 的接口（get_currency / purchase_unlock / commerce/fulfill …）
    都必须走这里，保证字段集合和真实值完全一致。
    """
    cur = {k: 0 for k in CURRENCY_KEYS}
    try:
        row = database.currency_get(user) or {}
    except Exception as _e:
        _social_log("build_currency.err", repr(_e))
        row = {}
    for _col, _key in _CURRENCY_COLUMN_MAP.items():
        v = _int_or_none(row.get(_col))
        if v:
            cur[_key] = v
    if candles is None:
        candles = _candle_balance(user)
    cur["candles"] = int(candles or 0)
    return cur


@account_bp.route("/get_currency", methods=["POST"])
def get_currency():
    req = request.get_json(force=True, silent=True) or {}
    user = req.get("user")

    if not user:
        return jsonify({"error": "Cannot find Data"}), 400

    if not database.user_exists(user):
        return jsonify({"error": "Cannot find Data"}), 404

    cfg = load_config()

    # ★ 修复「蜡烛数量永远不变」：
    #   原实现只在账号第一次出现时写入 3 根，之后**全服没有任何**增加蜡烛的写入点
    #   （私服没有实现「收集烛光 -> 换算蜡烛」的逻辑），所以蜡烛永远停在 3。
    #   现在支持 config.json 的 `all_user_candles`：每次拉取时把蜡烛**补齐**到该值。
    #   只补不降 —— 用聊天命令 `/cmd candles N` 加到更多时不会被覆盖回去。
    candles = database.get_user_field(user, "candles")
    try:
        candles = int(candles) if candles is not None else 0
    except (TypeError, ValueError):
        candles = 0

    # ★ 面板「设置」里按玩家指定的蜡烛数：直接覆盖（可升可降，滑条用）
    # ★ 2026-10-05：面板写死的 candles 以前**每次 get_currency 都重新覆盖**
    #   （实测 15 个账号 prefs.candles=9999）-> 玩家花掉的蜡烛下一帧就被重置回去,
    #   表现就是"蜡烛扣不掉 / 加减不正常"。现在只在面板改了值的那一次应用,
    #   之后走 users.candles 正常加减（再改一次面板值仍会立即生效）。
    _pfx = _get_prefs(user) or {}
    pc = _int_or_none(_pfx.get("candles"))
    if pc is not None and _int_or_none(_pfx.get("candles_set")) != pc:
        candles = max(0, pc)
        database.set_user_field(user, "candles", candles)
        _set_prefs(user, {"candles_set": pc})
        _social_log("get_currency.candles_override", {"user": user, "candles": candles})
        return jsonify({"currency": build_currency(user, candles)})

    floor = cfg.get("all_user_candles")
    if floor is not None:
        try:
            floor = int(floor)
        except (TypeError, ValueError):
            floor = None

    # ★ 2026-10-03：以前每次拉取都把蜡烛"补齐"到 all_user_candles，
    #   于是刚扣掉的蜡烛下一帧就被补回来 —— 用户看到的就是"不扣蜡烛"。
    #   "candle_refill" 控制这个行为：
    #     "always" = 老行为（每次补齐，适合送无限蜡烛的服）
    #     "init"   = 只在账号第一次拉取时发一次 all_user_candles（默认，扣得掉）
    mode = str(cfg.get("candle_refill", "init")).lower()
    if mode == "always":
        if floor is not None:
            if candles < floor:
                candles = floor
                database.set_user_field(user, "candles", candles)
        elif candles == 0:
            candles = 3
            database.set_user_field(user, "candles", candles)
    else:
        granted = _int_or_none(_get_prefs(user).get("candles_granted"))
        if not granted and floor is not None:
            candles = floor
            database.set_user_field(user, "candles", candles)
            _set_prefs(user, {"candles_granted": 1})

    # ★ 2026-10-04：把 currency 表里的其他货币一起回给客户端（内购买到的
    #   季节蜡烛 / 季节凭证 / 心 / 蜡 都要在这里体现，否则商店买完看不到）。
    #   candles 仍以 users.candles 为准（面板滑条 + /cmd candles 走那条线）。
    #   字段集由 build_currency() 固定为 23 个键，任何接口都不许少发。
    return jsonify({"currency": build_currency(user, candles)})

@account_bp.route("/get_forge_rates", methods=["POST"])
def get_forge_rates():
    return send_from_directory(
        directory=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config'),
        path='forge_rates.json',
        mimetype='application/json'
    )

@account_bp.route("/get_achievements", methods=["POST"])
def get_achievements():
    req = request.get_json(force=True, silent=True) or {}
    user = req.get("user")

    if not user:
        return jsonify({"achievements": []}), 200

    achievements = database.get_user_field(user, "achievements")
    if achievements:
        return jsonify({"achievements": json.loads(achievements)})
    else:
        return jsonify({"achievements": []}), 200

@account_bp.route("/get_achievement_stats", methods=["POST"])
def get_achievement_stats():
    req = request.get_json(force=True, silent=True) or {}
    user = req.get("user")

    if not user:
        return jsonify({"achievement_stats": []}), 200

    raw_data = database.get_user_field(user, "achievements")
    raw = json.loads(raw_data) if raw_data else []
    stats = [
        {
            "type": item.get("type", ""),
            "value": item.get("value", 0),
            "update": item.get("update", 0)
        }
        for item in raw
    ]
    return jsonify({"achievement_stats": stats})

@account_bp.route("/get_surveys", methods=["POST"])
def get_surveys():
    cfg = load_config()
    return send_from_directory(
        directory=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config'),
        path='surveys.json',
        mimetype='application/json'
    )

@account_bp.route("/get_event_schedule", methods=["POST"])
def get_event_schedule():
    cfg_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "event_schedule.json")
    if not os.path.isfile(cfg_path):
        return jsonify({"error": "config not found"}), 404

    with open(cfg_path, encoding="utf-8") as f:
        data = json.load(f)

    if "event_schedule" in data:
        data["event_schedule"]["server_time"] = int(time.time())
    else:
        data["event_schedule"] = {"server_time": int(time.time())}

    return jsonify(data)

@account_bp.route("/get_account_world_quests", methods=["POST"])
def get_account_world_quests():
    cfg = load_config()
    # ★ 2026-10-04：键名是 `set_world_quests`（原来拼成了 set_wrld_quests，
    #   客户端按名字找不到成员，会当成"这个世界任务资源没同步"）。
    return jsonify({"set_world_quests": []})

def _hub_reset_delete_list(user_id):
    """需要让客户端「忘掉」的解锁名（配合 get_unlocks 的 delete_unlocks）。

    ★★★ 2026-10-04 用户要求「柱子要一直能去，不是只能去一次」。
      机制：遇境雕像的时间轴 #114 "hub statue" 只存在于
      OnUnlocked(HubStatueForm).**notUnlockedFx**，而它是唯一
      Enable(#233)（雕像 + 打坐点 #455）的地方。客户端在时间轴末尾自己把它
      解锁掉以后，这一分支就消失了 ⇒ 只能去一次（直到重登）。
      客户端反汇编（/account/get_unlocks 描述符 0xFF4869）里有
      `update_unlocks` / **`delete_unlocks`** 两个字段 —— 服务端可以主动让
      客户端忘掉某个解锁。我们在这里把 unlock_hide 的名字列进 delete_unlocks，
      客户端就会回到「未解锁」状态 ⇒ 下次进遇境时间轴重播 ⇒ 又能坐、又能去。

    ★ 只在玩家的当前存档点**不是遇境**时下发：
      玩家还在遇境里时删掉它会把正在播的雕像 FX 打断，得不偿失。
      （实测：客户端会话中途会反复拉 get_unlocks，所以这一招能立刻生效。）

    config.json 的 `hub_reset_unlock` 设 false 可整体关掉。
    """
    try:
        if not load_config().get("hub_reset_unlock", True):
            return []
    except Exception:
        pass
    hidden = sorted(_hidden_unlocks())
    if not hidden:
        return []
    try:
        cp = int(database.get_user_field(user_id, "checkpoint") or 0)
    except (TypeError, ValueError):
        cp = 0
    return hidden   # ALWAYS: 'not unlocked' is the only state where the door teleports
    return hidden


def _norm_unlock_items(items):
    """统一解锁条目字段：同时给 unlocked_at 与 created_at（两代客户端都要）。"""
    out = []
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        e = dict(it)
        ts = e.get("created_at", e.get("unlocked_at", 0))
        e["created_at"] = ts
        e["unlocked_at"] = e.get("unlocked_at", ts)
        out.append(e)
    return out


@account_bp.route("/get_unlocks", methods=["POST"])
def get_unlocks():
    cfg = load_config()

    if cfg.get("all_users_allunlock", False):
        # ★ 全解锁，但首次账号（未到过遇境）剔除"遇境到达"标记，
        #   否则客户端永远认为已到过遇境，无视 checkpoint 直接出生遇境
        req = request.get_json(force=True, silent=True) or {}
        user_id = req.get("user")
        first_time = False
        if user_id:
            try:
                first_time = not int(database.get_user_field(user_id, "visited_home") or 0)
            except Exception:
                first_time = False
        base_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config")
        with open(os.path.join(base_dir, "all_unlock.json"), encoding="utf-8-sig") as _f:
            payload = json.load(_f)
        if first_time:
            forbid = {"first_loaded_CandleSpace", "first_loaded_SkyHub2", "SkyHubFirstArrival",
                      "first_loaded_HubReveal", "1stHub", "DayHub", "FinishedIntro"}
            payload["unlocks"] = [u for u in payload.get("unlocks", []) if u.get("name") not in forbid]
        # ★ 兜底：客户端自己上报过的解锁项一律并回（修「神殿坐下不传送」的根因）
        payload = _merge_reported_unlocks(user_id, payload, "unlocks")
        # ★ 2026-10-04：参考服务器的键是 created_at（我们原来只有 unlocked_at），
        #   两个都给；并补 unlocks_total_count。
        payload["unlocks"] = _norm_unlock_items(payload.get("unlocks"))
        payload["unlocks_total_count"] = len(payload["unlocks"])
        # ★ 2026-10-06 服饰解锁：客户端版本自带 OutfitDefs.json 里的全部服饰名
        #   （config/outfit_defs_0.15.5.json，用户提供）⇒ 衣柜有全部装扮。
        #   type=spiritshop；体积闸门 86016 字节（见 outfit_unlock_helper.py）。
        try:
            from outfit_unlock_helper import apply_outfit_unlocks
            apply_outfit_unlocks(payload, "unlocks")
            payload["unlocks_total_count"] = len(payload.get("unlocks") or [])
        except Exception as _e:
            try:
                _social_log("outfit_unlock.err", repr(_e))
            except Exception:
                pass
        # ★ 让「柱子一直能去」：离开遇境后主动让客户端忘掉 HubStatueForm
        #   （见 _hub_reset_delete_list 的说明）。
        _del = _hub_reset_delete_list(user_id)
        if _del:
            payload["delete_unlocks"] = _del
        return jsonify(payload)

    req = request.get_json(force=True, silent=True) or {}
    user_id = req.get("user")
    if not user_id:
        return jsonify({"error": "No user_id provided"}), 400

    if not database.user_exists(user_id):
        return jsonify({"error": "Cannot find latest checkpoint"}), 404

    raw_data = database.get_user_field(user_id, "unlocks")
    data = json.loads(raw_data) if raw_data else []

    unlocks = [
        {
            "name": item.get("name", ""),
            "type": "level",
            "ack": item.get("ack", False),
            "unlocked_at": item.get("unlocked_at", 0),
            "created_at": item.get("created_at", item.get("unlocked_at", 0)),
        }
        for item in data
    ]
    payload = {"unlocks": unlocks, "unlocks_total_count": len(unlocks)}
    try:
        from outfit_unlock_helper import apply_outfit_unlocks
        apply_outfit_unlocks(payload, "unlocks")
        payload["unlocks_total_count"] = len(payload.get("unlocks") or [])
    except Exception as _e:
        try:
            _social_log("outfit_unlock.err", repr(_e))
        except Exception:
            pass
    return jsonify(payload)

@account_bp.route("/get_shop", methods=["POST"])
def get_shop():
    cfg = load_config()
    return send_from_directory(
        directory=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config'),
        path='get_shop.json',
        mimetype='application/json'
    )

def _truncate_spirit_shops(items, max_nodes):
    """按**整棵祖先子树**截断先灵/季节向导树，绝不按广度截断。

    参考服务器（win3/Sky/account/get_spirit_shops/get_spirit_shops.py:57-67）按客户端
    版本分档：>=0.26.0 不限 / >=0.20.0 500 / **>=0.10.0 只有 250** / <0.10.0 120。
    节点结构：dep = 祖先 id（0 = 根祖先），ta/typ 决定渲染。
    按广度截断会出现"根节点多于一个"⇒ 客户端断言 More than one root node detected。
    """
    if max_nodes <= 0 or len(items) <= max_nodes:
        return items
    children = {}
    roots = []
    for it in items:
        if not isinstance(it, dict):
            continue
        dep = it.get("dep") or 0
        if dep:
            children.setdefault(dep, []).append(it)
        else:
            roots.append(it)
    if not roots:
        return items[:max_nodes]
    out, seen = [], set()

    def _add(node):
        nid = node.get("id")
        if nid in seen:
            return
        seen.add(nid)
        out.append(node)
        for ch in children.get(nid, []):
            _add(ch)

    for r in roots:
        if len(out) >= max_nodes:
            break
        _add(r)
    return out or items[:max_nodes]


@account_bp.route("/get_spirit_shops", methods=["POST"])
def get_spirit_shops():
    req = request.get_json(force=True, silent=True) or {}
    try:
        offset = int(req.get("o", 0) or 0)
        limit = int(req.get("l", 0) or 0)
    except (TypeError, ValueError):
        offset, limit = 0, 0
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config', 'spirit_shops.json')
    try:
        with open(path, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)
    except Exception:
        data = {"spirit_shops": [], "spirit_shops_version": 0, "spirit_shops_total_count": 0}
    items = data.get("spirit_shops", [])
    if not isinstance(items, list):
        items = []

    # ★★★ 2026-10-04 季节向导兑换树「点开是空的」两个原因都在这：
    #   ① 原来 `l` 参数缺省（客户端不带分页参数时）**直接返回空列表** items=[]。
    #   ② config/spirit_shops.json 是 0.13.4 的老表（52 条、没有 questap 季节向导节点）
    #      —— 已整体换成参考服务器的 451 条（144 个 questap 向导节点、34 棵祖先树）。
    #   另外按参考服务器的渲染规则做一次归一化：
    #     · 季节向导祖先（spirit 以 questap 开头）：cst 强制 0、ta 改写成 zero_trust_unlock
    #       （不改就点不动向导）
    #     · 其它 cst==0 的节点抬到 1（客户端会把 cst==0 当成"已领取"⇒ 整棵树空白）
    for it in items:
        if not isinstance(it, dict):
            continue
        if str(it.get("spirit") or "").startswith("questap"):
            it["cst"] = 0
            if str(it.get("ta") or "").startswith("spirit_unlock"):
                it["ta"] = "zero_trust_unlock"
        elif it.get("cst") == 0:
            it["cst"] = 1

    # ★★★ 2026-10-05: 客户端固定发 l=1000, 而老版本客户端一次收 1000
    #   个节点会直接闪退(实测 0.15.5: 登录批最后一条 get_spirit_shops 回了
    #   251860 字节之后客户端就没了)。参考服务器按版本封顶:
    #   >=0.26 不限 / >=0.20 500 / >=0.10 250 / <0.10 120。
    #   原来这个闸门只加在 limit<=0 的分支上, 客户端一发 l=1000 就绕过了。
    try:
        _cap = int(load_config().get("spirit_shops_max_nodes", 250) or 0)
    except (TypeError, ValueError):
        _cap = 250
    if _cap > 0 and len(items) > _cap:
        items = _truncate_spirit_shops(items, _cap)
    total = len(items)
    if limit > 0:
        page = items[offset:offset + limit]
    else:
        page = items[offset:] if offset else list(items)
        # 老客户端尺寸闸门（默认 250，可用 config.json 的 spirit_shops_max_nodes 调）
        try:
            _mx = int(load_config().get("spirit_shops_max_nodes", 250) or 0)
        except (TypeError, ValueError):
            _mx = 250
        page = _truncate_spirit_shops(page, _mx)

    return jsonify({
        "spirit_shops": page,
        "spirit_shops_total_count": total,
        # 参考服务器给的是 388242；我们数据文件里的版本号优先
        "spirit_shops_version": data.get("spirit_shops_version", 388242) or 388242,
    })

@account_bp.route("/get_collectibles", methods=["POST"])
def get_collectibles():
    cfg = load_config()
    if cfg.get("all_user_allcollects", False):
        return send_from_directory(
            directory=os.path.join(os.path.dirname(os.path.dirname(__file__)), "config"),
            path='all_collect.json',
            mimetype='application/json'
        )

    req = request.get_json(force=True, silent=True) or {}
    user = req.get("user")
    if not user:
        return jsonify({"error": "No The User"}), 400

    if not database.user_exists(user):
        return jsonify({"error": "No The User"}), 404

    raw_data = database.get_user_field(user, "collects")
    data = json.loads(raw_data) if raw_data else []

    if not data:
        default = ["skykid", "sit", "normal", "flame", "home", "cape"]
        database.set_user_field(user, "collects", json.dumps(default))
        data = default

    achievements = [
        {
            "id": cid,
            "used": True,
            "candle_space": False,
            "carrying": False,
            "level": 2,
            "permanent_level": 1
        }
        for cid in data
    ]
    return jsonify({"collectibles": achievements})

@account_bp.route("/wing_buffs/get", methods=["POST"])
def get_wing_buffs():
    cfg = load_config()
    req = request.get_json(force=True, silent=True) or {}
    user = req.get("user")

    # ★ 面板「设置」里指定的光翼数量：从全量列表里取前 N 个下发（滑条用）
    n = _int_or_none(_get_prefs(user).get("wingbuffs")) if user else None

    cfg_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config")
    if cfg.get("all_user_allhaswingbuff", False):
        if n is None:
            return send_from_directory(directory=cfg_dir,
                                       path='all_wing_buffs.json',
                                       mimetype='application/json')
        try:
            with open(os.path.join(cfg_dir, "all_wing_buffs.json"),
                      encoding="utf-8") as f:
                d = json.load(f)
            wbs = d.get("wing_buffs") or []
            d["wing_buffs"] = wbs[:max(0, n)]
            return jsonify(d)
        except Exception:
            return send_from_directory(directory=cfg_dir,
                                       path='all_wing_buffs.json',
                                       mimetype='application/json')

    if not user:
        return jsonify({"error": "No The User"}), 400

    if not database.user_exists(user):
        return jsonify({"error": "No The User"}), 404

    raw_data = database.get_user_field(user, "wing_buffs")
    data = json.loads(raw_data) if raw_data else []

    return jsonify({
        "result": "ok",
        "wing_buffs": [
            {
                "name": wb,
                "collected": True,
                "deposited": False,
                "last_conversion": 0,
                "deposit_id": 0
            }
            for wb in data
        ]
    })

@account_bp.route("/send", methods=["POST"])
def send():
    """客户端发送聊天消息的真正端点。

    ★ 2026-10-03 修聊天「发出去、谁(包括自己)都收不到」。

    机制来自参考实现（SkyMoon / Windows端私服 websocket_handler.py + send.py）：
      1. 客户端 POST /account/send {user, session, msg, ch}
      2. 服务端把消息投进**每个用户**的待发队列
      3. 所有客户端轮询 /account/get_pending_messages，从 set_recvd_messages 里取走

    实测依据：我们服务器的 nginx 日志里客户端调过 /account/get_pending_messages
    25 次，说明客户端确实在用这条轮询链路；而参考服务器同名端点返回
    {"set_recvd_messages":[],"set_sent_messages":[]}，键名一致。

    参考实现里那个函数名是 broadcast_message_to_websocket_clients，但注释明确写着
    「使用HTTP轮询队列方式（绕过WebSocket）」—— 所以投递跟 WebSocket 无关。

    我们之前只有 /account/chat/send（处理 /cmd 指令用），客户端发的 /account/send
    落到了 catch-all，消息被静默丢弃 —— 这就是聊天不通的服务端根因。
    """
    req = request.get_json(force=True, silent=True) or {}
    user = _pick_any(req, "user", "user_id", "account", "id")
    msg = req.get("msg") or req.get("message") or ""
    ch = (req.get("ch") or req.get("channel") or "local")
    if isinstance(msg, str):
        msg = msg.strip()
    if not user or not msg:
        # 参数不全：参考实现回 200 + result=error，不回 4xx（客户端只认 200）
        missing = []
        if not user:
            missing.append("user")
        if not msg:
            missing.append("msg")
        return jsonify({"result": "error",
                        "message": "Missing parameters: " + ", ".join(missing)})

    msg_id = str(uuid.uuid4())
    sent_at_ms = int(time.time() * 1000)

    # 1) 落库（chat_messages：聊天记录，供事后查询/审计）
    try:
        database.save_chat_message(
            message_id=msg_id, from_user=user, message=msg,
            channel=str(ch)[:50], level_id="", to_user="0",
            sent_at=sent_at_ms // 1000, sent_at_ms=sent_at_ms,
            signature=None, invalid=0, recordable=0)
    except Exception as e:
        _social_log("send.chat_messages.err", repr(e))

    # 2) 投递：写进所有用户的收件箱（含发送者自己）
    #    参考实现就是 User.query.all() 全量投递；客户端自己渲染自己的消息
    #    也依赖这条回环（"谁(包括发送者自己)都收不到" 是参考实现里明确的症状）。
    recipients = []
    try:
        rows = database.query_all("SELECT id FROM users") or []
        recipients = [r.get("id") for r in rows if r.get("id")]
    except Exception as e:
        _social_log("send.users.err", repr(e))
    if user not in recipients:
        recipients.append(user)

    queued = database.enqueue_chat_message(
        recipients, sender_id=user, msg_id=msg_id, msg=msg,
        ch=str(ch)[:50], msg_type="chat", sent_at_ms=sent_at_ms)

    app_logger = current_app.logger if current_app else None
    if app_logger:
        app_logger.info("[聊天投递] from=%s ch=%s 收件人=%d :: %s"
                        % (user[:8], ch, queued, msg[:60]))

    return jsonify({
        "result": "success",
        "msg_id": msg_id,
        "sender_id": user,
        "msg": msg,
        "ch": ch,
        "timestamp": sent_at_ms,
    })


@account_bp.route("/get_pending_messages", methods=["POST"])
def get_pending_messages():
    """待收 / 已发礼物消息。

    ★★ 2026-10-03 修「给蜡烛之后对方没有接受按钮」：
       以前这里直接发静态的 config/pending.json（`{"set_sent_messages": [],
       "set_recvd_messages": []}` 两个空数组），所以**对方永远看不到任何待处理消息**，
       自然也就没有"接受"入口。
       现在改成读 gift_messages 表（`/account/give_candle`、`account/send_message`
       写入的那张表），回包键名保持原样 `set_recvd_messages` / `set_sent_messages`
       （已由 pending.json 与 libBootloader.so 两处交叉确认）。
    """
    req = request.get_json(force=True, silent=True) or {}
    me = _pick_any(req, "user", "user_id", "account", "id")
    recvd, sent = [], []
    # ★ 2026-10-03 新增：聊天消息。这条链路才是聊天真正的投递方式 ——
    #   /account/send 把消息写进 chat_inbox，客户端轮询这里取走。
    #   字段名对齐参考实现（SkyMoon websocket_handler.broadcast_message_to_all_clients
    #   构造的 game_format）：type / sender_id / msg_id / msg / ch / timestamp / result
    chat_recvd = []
    # p74: 聊天消息默认**不**再混进 set_recvd_messages。
    #   那个数组是 AccountGiftMessage 数组（客户端按整数 msg_id + 官方 type 解析），
    #   塞进 type="chat"、msg_id=uuid 的聊天条目会让客户端解析失败 -> 闪退。
    #   聊天走 WS(type='chat') 与 /service/message 接口。
    #   临时恢复旧行为：config.json 里 pending_messages_include_chat=true。
    try:
        _include_chat = bool(load_config().get("pending_messages_include_chat", False))
    except Exception:
        _include_chat = False
    if me and _include_chat:
        try:
            for m in database.take_chat_inbox(me):
                chat_recvd.append({
                    "type": m.get("msg_type") or "chat",
                    "sender_id": m.get("sender_id") or "unknown",
                    "msg_id": m.get("msg_id") or "",
                    "msg": m.get("msg") or "",
                    "ch": m.get("ch") or "local",
                    "timestamp": int(m.get("sent_at_ms") or 0),
                    "result": "success",
                })
        except Exception as e:
            _social_log("get_pending_messages.chat.err", repr(e))
    if me:
        try:
            rows = database.query_all(
                "SELECT msg_id, from_user, to_user, gift_type, currency_type, "
                "currency_count, raw_message, sent_at, claimed FROM gift_messages "
                "WHERE to_user = ? AND claimed = 0 ORDER BY sent_at DESC LIMIT 100", (me,))
            for r in rows or []:
                recvd.append(_gift_entry(r, True))
            rows = database.query_all(
                "SELECT msg_id, from_user, to_user, gift_type, currency_type, "
                "currency_count, raw_message, sent_at, claimed FROM gift_messages "
                "WHERE from_user = ? ORDER BY sent_at DESC LIMIT 100", (me,))
            for r in rows or []:
                sent.append(_gift_entry(r, False))
        except Exception as e:
            _social_log("get_pending_messages.err", repr(e))
    # ★ `sent_message` 是**单数**：客户端的 syncAction 就叫 `sent_message`，
    #   客户端拿一个"最新已发消息对象"（不是数组）去解析；以前这里给数组 →
    #   "Failed to parse resource sync response ... for syncAction sent_message"。
    # ★ 聊天消息与礼物消息合并进 set_recvd_messages（客户端的统一收件箱视图）。
    all_recvd = chat_recvd + recvd

    # ★ 2026-10-03 对照实验：参考实现（SkyMoon / Windows端私服
    #   get_pending_messages.py，修复前后都一致）只返回**两个键**：
    #       {"set_sent_messages": [], "set_recvd_messages": [...]}
    #   而我们的响应带了一堆额外键，其中 sent_message 是个礼物对象
    #   （claimed/gift_type/types/raw_message...）。若客户端解析该字段失败，
    #   有可能把整个响应丢掉 → 聊天消息也跟着不显示。
    #   配置 config.json 的 "pending_messages_strict": true 即切到严格两键结构，
    #   用来判定「是不是多余字段导致客户端不渲染」。改完实时生效，无需重启。
    try:
        strict = bool(load_config().get("pending_messages_strict", False))
    except Exception:
        strict = False
    if strict:
        return jsonify({
            "set_sent_messages": [],
            "set_recvd_messages": all_recvd,
        })

    return jsonify({
        "result": "ok", "status": "ok", "ok": True,
        "set_recvd_messages": all_recvd,
        "set_sent_messages": sent,
        "recvd_messages": all_recvd,
        "sent_message": (sent[0] if sent else None),
        "recv_count": len(all_recvd),
        "sent_count": len(sent),
    })

@account_bp.route("/get_app_badge_number", methods=["POST"])
def get_app_badge_number():
    cfg = load_config()
    return jsonify({"set_app_badge_number": 0})


# ---------------------------------------------------------------------------
# 魔法 / 消耗品 (consumable)   —— 2026-10-05
#
# 参考 win3/Sky/magic_helper.py 的实测结论:
#   * 客户端背包条目形状 = {"consumable_id": int, "cooldown_until": int, "quantity": int}
#   * 物品 id = FNV-1a-32(名字)，**发定义表里不存在的 id -> 客户端闪退**
#   * 数量 0 的条目不能发；但 UGC 一族（纸船/留言/留影/空间蜡烛）例外，
#     它们一旦查不到，创建留言就闪退
#   * 定义表本身照发（官方 507 条约 86KB，客户端吃得下）
# 我们原来: /account/get_consumables 无 handler(回 {})、
#           /consumable/get_consumables1 恒回 []、
#           /consumable/get_consumable_defs 恒回 [] —— 魔法面板整个是空的。
# 存储: users 表没有魔法列，放进 config/user_prefs.json 的 "consumables" 字典。
# ---------------------------------------------------------------------------
_CONSUMABLE_CACHE = {"mtime": 0, "defs": []}


def _consumable_defs():
    """读 config/consumable_defs.json（与 /account/get_consumable_defs 同一份表）。"""
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "config", "consumable_defs.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    if _CONSUMABLE_CACHE["mtime"] == mtime and _CONSUMABLE_CACHE["defs"]:
        return _CONSUMABLE_CACHE["defs"]
    items = []
    try:
        with open(path, encoding="utf-8-sig") as f:
            d = json.load(f)
        if isinstance(d, dict):
            items = d.get("get_consumable_defs") or []
        elif isinstance(d, list):
            items = d
    except Exception as e:
        _social_log("consumable_defs.read.err", repr(e))
        items = []
    if not isinstance(items, list):
        items = []
    _CONSUMABLE_CACHE["mtime"] = mtime
    _CONSUMABLE_CACHE["defs"] = items
    return items


def _consumable_ids():
    out = []
    for c in _consumable_defs():
        try:
            cid = int(c.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if cid:
            out.append(cid)
    return sorted(set(out))


def _consumable_uuid_names():
    """UGC 一族（纸船/留言/留影/空间蜡烛）—— 永远要有条目，否则创建留言闪退。"""
    ids = set()
    for c in _consumable_defs():
        nm = str(c.get("name") or "")
        ev = str(c.get("event") or "")
        if (nm in ("memory_candle", "sharedspace_candle")
                or nm.startswith("message_")
                or nm.startswith("personality_message_")
                or ev in ("SocialMessageBoats", "SocialMessageStones",
                          "RecordingCandleEvent", "SharedSpaceCandleEvent")):
            try:
                ids.add(int(c.get("id") or 0))
            except (TypeError, ValueError):
                pass
    ids.discard(0)
    return ids


def _grant_all_consumables(user_id, qty=999):
    """把定义表里的每一件魔法都发给该用户（幂等，只增不减）。返回发放件数。"""
    if not user_id:
        return 0
    ids = _consumable_ids()
    if not ids:
        return 0
    try:
        prefs = _get_prefs(user_id) or {}
        inv = prefs.get("consumables")
        if not isinstance(inv, dict):
            inv = {}
        q = int(qty or 999)
        for cid in ids:
            k = str(cid)
            cur = 0
            try:
                cur = int(inv.get(k) or 0)
            except (TypeError, ValueError):
                cur = 0
            if cur < q:
                inv[k] = q
        _set_prefs(user_id, {"consumables": inv})
        return len(ids)
    except Exception as e:
        _social_log("consumables.grant.err", repr(e))
        return 0


def _consumables_inventory(user_id):
    """该用户背包里的魔法（客户端条目形状）。只发定义表里真实存在的 id。"""
    known = set(_consumable_ids())
    ugc = _consumable_uuid_names()
    try:
        prefs = _get_prefs(user_id) or {}
    except Exception:
        prefs = {}
    inv = prefs.get("consumables")
    if not isinstance(inv, dict):
        inv = {}
    out, seen = [], set()
    for k, v in inv.items():
        try:
            cid = int(k)
            q = int(v or 0)
        except (TypeError, ValueError):
            continue
        if not cid or (known and cid not in known):
            continue
        if q <= 0 and cid not in ugc:
            continue
        out.append({"consumable_id": cid, "cooldown_until": 0,
                    "quantity": q if q > 0 else 99})
        seen.add(cid)
    # UGC 一族即使没发过也要下发，否则留言/纸船流程会闪退
    for cid in sorted(ugc):
        if cid not in seen:
            out.append({"consumable_id": cid, "cooldown_until": 0, "quantity": 99})
    out.sort(key=lambda e: e["consumable_id"])
    return out

@account_bp.route("/get_consumables", methods=["POST"])
@account_bp.route("/consumable/get_consumables", methods=["POST"])
@account_bp.route("/consumable/get_consumables1", methods=["POST"])
def get_consumables1():
    """魔法背包。参考 win3 account/get_consumables{,1}: {"get_consumables": [...]}。
    配置 config.json 里 all_user_allconsumables=true 时，每次查询都确保
    "所有魔法 x N" 已发放（新账号自动、老账号补齐）。"""
    req = request.get_json(force=True, silent=True) or {}
    me = req.get("user") or req.get("user_id")
    try:
        if bool(load_config().get("all_user_allconsumables", False)):
            n = int(load_config().get("all_user_consumables_count", 999) or 999)
            _grant_all_consumables(me, n)
    except Exception as e:
        _social_log("get_consumables.grant.err", repr(e))
    inv = _consumables_inventory(me)
    _social_log("get_consumables.resp", {"user": me, "n": len(inv)})
    return jsonify({"get_consumables": inv, "consumables": inv})




# ---------------------------------------------------------------------------
# 魔法使用链路: consume_item1 / get_buffs1 / get_buff_defs   —— 2026-10-05
#
# 参考 win3/Sky/account/consume_item1/consume_item1.py 的官方抓包合约:
#   请求 {"user","session","consumable_id","consumable_count"?}
#   响应 {"consume_item":"success","get_buffs":[{active_until,buff_id,
#         cooldown_until,giver_user_id}],"get_buffs_sign":"...",
#         "get_consumables":[...],"shared_results":[],"new_height":1.0}
# 三条实测坑(参考文件头逐条列出):
#   1. 必须有 new_height, 否则体型类魔法用完后状态错乱;
#   2. buff_id 要从**真实定义表**取, 表为空则 buff 永远挂不上、冷却永远 600s;
#   3. get_consumables 必须回**整包**背包, 而且**必须回显刚消耗的那一件**
#      (扣到 0 也要回显) —— 否则客户端"刚用过的那件"凭空消失并闪退。
# 我们原来 /account/consumable/consume_item1 根本没有 handler(回 {}),
# buff defs 表是空的 21 字节 -> 点魔法必闪退。
# 存储: config/user_prefs.json 的 "buffs" = {buff_id: {active_until, cooldown_until, giver}}
# ---------------------------------------------------------------------------
_BUFF_CACHE = {"mtime": 0, "defs": []}


def _buff_defs():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "config", "buff_defs.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    if _BUFF_CACHE["mtime"] == mtime and _BUFF_CACHE["defs"]:
        return _BUFF_CACHE["defs"]
    items = []
    try:
        with open(path, encoding="utf-8-sig") as f:
            d = json.load(f)
        if isinstance(d, dict):
            items = d.get("get_buff_defs") or []
        elif isinstance(d, list):
            items = d
    except Exception as e:
        _social_log("buff_defs.read.err", repr(e))
        items = []
    if not isinstance(items, list):
        items = []
    _BUFF_CACHE["mtime"] = mtime
    _BUFF_CACHE["defs"] = items
    return items


def _buff_def(buff_id):
    try:
        bid = int(buff_id or 0)
    except (TypeError, ValueError):
        return None
    if not bid:
        return None
    for b in _buff_defs():
        try:
            if int(b.get("id") or 0) == bid:
                return b
        except (TypeError, ValueError):
            continue
    return None


def _consumable_def(cid):
    try:
        want = int(cid or 0)
    except (TypeError, ValueError):
        return None
    if not want:
        return None
    for c in _consumable_defs():
        try:
            if int(c.get("id") or 0) == want:
                return c
        except (TypeError, ValueError):
            continue
    return None


def _resolve_consumable_id(*cands):
    """客户端不同接口传的键名不一样, 按数字 id / 名字依次落到定义表上。"""
    for c in cands:
        if c is None or c == "":
            continue
        d = _consumable_def(c)
        if d is not None:
            return int(d.get("id"))
    for c in cands:
        if isinstance(c, str) and c:
            for d in _consumable_defs():
                if d.get("name") == c:
                    return int(d.get("id") or 0)
    return 0


def _buffs_store(user_id):
    try:
        prefs = _get_prefs(user_id) or {}
    except Exception:
        prefs = {}
    b = prefs.get("buffs")
    return dict(b) if isinstance(b, dict) else {}


def _buff_add(user_id, buff_id, giver=None, duration_seconds=None):
    if not user_id or not buff_id:
        return
    now = int(time.time())
    d = _buff_def(buff_id) or {}
    if duration_seconds is None:
        try:
            minutes = float(d.get("duration") or 0)
        except (TypeError, ValueError):
            minutes = 0.0
        duration_seconds = int(minutes * 60) if minutes > 0 else 600
    duration_seconds = max(int(duration_seconds), 1)
    try:
        cooldown_min = float(d.get("cooldown") or 0)
    except (TypeError, ValueError):
        cooldown_min = 0.0
    cur = _buffs_store(user_id)
    cur[str(buff_id)] = {
        "active_until": now + duration_seconds,
        "cooldown_until": now + int(cooldown_min * 60) if cooldown_min > 0 else now,
        "giver": str(giver or user_id),
    }
    try:
        _set_prefs(user_id, {"buffs": cur})
    except Exception as e:
        _social_log("buff.add.err", repr(e))


def _buffs_payload(user_id):
    """官方 get_buffs 条目形状 + 签名: sha256(规范JSON + user_id)。"""
    import hashlib
    now = int(time.time())
    out = []
    for k, v in _buffs_store(user_id).items():
        try:
            bid = int(k)
        except (TypeError, ValueError):
            continue
        if not bid:
            continue
        try:
            au = int((v or {}).get("active_until") or 0)
        except (TypeError, ValueError):
            au = 0
        if au and au < now:
            continue
        out.append({
            "active_until": au,
            "buff_id": bid,
            "cooldown_until": int((v or {}).get("cooldown_until") or 0),
            "giver_user_id": str((v or {}).get("giver") or user_id or ""),
        })
    out.sort(key=lambda x: x["buff_id"])
    payload = json.dumps(out, sort_keys=True, separators=(",", ":"))
    sign = hashlib.sha256((payload + (user_id or "")).encode()).hexdigest()
    return out, sign


def _current_height(user_id):
    try:
        return float((_outfit_payload(user_id) or {}).get("height") or 1.0)
    except Exception:
        return 1.0


def _reroll_height(user_id):
    """resize_potion（体型重置药水）专用：重掷身高并返回新值。

    参考 win3 magic_helper._reroll_height 用 random.uniform(0.85, 1.15)。
    客户端二进制里 new_height 紧挨着 ResizeConsumableEvent 与
    consumable_resize_taller/shorter 这几个字符串 —— 它是这个事件专用的字段，
    客户端拿到它去同步体型。**必须与当前值不同**（否则增量为 0），
    而且必须落在 kOutfitAvatarHeight 的合法区间 -3.0..3.0 内。
    """
    import random
    try:
        p = _get_prefs(user_id) or {}
        cur = _float_or_none(p.get("height"))
        new = round(random.uniform(0.85, 1.15), 6)
        for _ in range(8):
            if cur is None or abs(new - cur) > 0.05:
                break
            new = round(random.uniform(0.85, 1.15), 6)
        new = _clamp_h(new)
        _set_prefs(user_id, {"height": new})
        _social_log("resize_potion.reroll", {"user": user_id, "from": cur, "to": new})
        return new
    except Exception as e:
        _social_log("resize_potion.reroll.err", repr(e))
        return None


@account_bp.route("/consumable/consume_item1", methods=["POST"])
@account_bp.route("/consume_item1", methods=["POST"])
def consume_item1():
    """使用一件魔法。响应形状见文件头说明(必须回显被消耗的那一件 + new_height)。"""
    req = request.get_json(force=True, silent=True) or {}
    me = req.get("user") or req.get("user_id")
    cid = _resolve_consumable_id(req.get("consumable_id"), req.get("consumable_name"),
                                req.get("item_name"), req.get("name"))
    cdef = _consumable_def(cid) or {}
    try:
        count = max(int(req.get("consumable_count") or 1), 1)
    except (TypeError, ValueError):
        count = 1
    now = int(time.time())
    try:
        cooldown_sec = int(float(cdef.get("cooldown") or 0) * 60) or 600
    except (TypeError, ValueError):
        cooldown_sec = 600

    left = 0
    if me and cid:
        try:
            prefs = _get_prefs(me) or {}
            inv = prefs.get("consumables")
            if not isinstance(inv, dict):
                inv = {}
            k = str(cid)
            try:
                cur = int(inv.get(k) or 0)
            except (TypeError, ValueError):
                cur = 0
            left = max(cur - count, 0)
            # ★ p76：all_user_allconsumables 打开时当作"魔法无限"——扣完立刻补回配置数量。
            #   以前扣到 0 之后 _consumables_inventory 会跳过 q<=0 的条目，
            #   客户端背包里那件直接消失（用户实测"看视是999，用一个就消失"）。
            try:
                _cfg76 = load_config()
                if bool(_cfg76.get("all_user_allconsumables", False)):
                    left = int(_cfg76.get("all_user_consumables_count", 999) or 999)
            except Exception:
                pass
            inv[k] = left
            _set_prefs(me, {"consumables": inv})
        except Exception as e:
            _social_log("consume_item1.deduct.err", repr(e))

    bid = 0
    try:
        bid = int(cdef.get("buff_id") or 0)
    except (TypeError, ValueError):
        bid = 0
    if me and bid:
        _buff_add(me, bid, giver=me)
    elif me and str(cdef.get("name") or "") == "resize_potion":
        # 官方: 无 buff 的 resize_potion 重掷身高, 客户端用响应里的 new_height 同步。
        # 原来漏了这支分支 -> new_height 回的是**当前值**(增量 0) -> 客户端崩。
        _reroll_height(me)

    buffs, sign = _buffs_payload(me)
    bag = []
    try:
        bag = _consumables_inventory(me)
    except Exception as e:
        _social_log("consume_item1.bag.err", repr(e))
    if cid and not any(e.get("consumable_id") == cid for e in bag):
        bag.append({"consumable_id": cid, "cooldown_until": now + cooldown_sec,
                    "quantity": left})
        bag.sort(key=lambda e: e.get("consumable_id") or 0)

    _social_log("consume_item1", {"user": me, "consumable_id": cid, "name": cdef.get("name"),
                                  "buff_id": bid, "left": left})
    return jsonify({
        "consume_item": "success",
        "get_buffs": buffs,
        "get_buffs_sign": sign,
        "get_consumables": bag,
        "shared_results": [],
        "new_height": _current_height(me),
    })


@account_bp.route("/buff/get_buffs", methods=["POST"])
@account_bp.route("/lootboxes/get", methods=["POST"])
def get_lootboxes():
    cfg = load_config()
    return jsonify({"get_lootboxes": []})

# ================================================================
# 一次性传送：客户端状态面板的「去空巢」按钮 → 让下一次进图直接落到目标关卡
#   写一个带过期时间的小文件，get_checkpoints 命中即把该关卡当作出生点下发；
#   客户端重启游戏后重新走 get_checkpoints，于是直接进目标图。
#   2 分钟过期，避免用户在目标图里再切图时被反复拉回去。
# ================================================================
_GOTO_FILE = os.path.join(tempfile.gettempdir(), "wbsky_goto.json")
_GOTO_TTL = 300

# level_id 全部 = FNV-1a-32(关卡代号)，已用 9 个已知 ID 校验通过
_GOTO_TARGETS = {
    # 常驻地图
    "home": 3526133726,        # CandleSpace —— 遇境
    "candlespace": 3526133726,
    "skyhub": 2825107789,      # SkyHub2 —— 空巢 / 云巢
    "skyhub2": 2825107789,
    "dawn": 1649439303,        # Dawn —— 晨岛
    "prairie": 927037567,      # Prairie —— 云野
    "rain": 164626931,         # Rain —— 雨林
    "valley": 1638008359,      # Sunset —— 霞谷
    "sunset": 1638008359,
    "dusk": 1147491976,        # Dusk —— 暮土
    "night": 2358907137,       # Night —— 禁阁
    "storm": 1705189686,       # Storm —— 暴风眼
    # 子地图（细分出生点）
    "dawncave": 748712866,     # DawnCave —— 晨岛试炼洞窟
    "dayhubcave": 2394719185,  # DayHubCave —— 八人门洞穴
    "hubreveal": 2018906977,   # HubReveal —— 开场 reveal（无传送打坐点）
    "duskstart": 817373972,    # DuskStart
    "duskmid": 1597085778,     # DuskMid
    "duskend": 4158956653,     # DuskEnd
    "night2": 2307461961,      # Night2
    "nightend": 2267185542,    # NightEnd
    "stormstart": 3110721718,  # StormStart
    "stormend": 3479786579,    # StormEnd
}

# 面板"设置 - 出生点"里给客户端展示的清单（顺序 = 显示顺序）
SPAWN_CHOICES = [
    {"key": "home",     "name": "遇境",     "level_id": 3526133726},
    {"key": "skyhub",   "name": "空巢",     "level_id": 2825107789},
    {"key": "dawn",     "name": "晨岛",     "level_id": 1649439303},
    {"key": "prairie",  "name": "云野",     "level_id": 927037567},
    {"key": "rain",     "name": "雨林",     "level_id": 164626931},
    {"key": "valley",   "name": "霞谷",     "level_id": 1638008359},
    {"key": "dusk",     "name": "暮土",     "level_id": 1147491976},
    {"key": "night",    "name": "禁阁",     "level_id": 2358907137},
    {"key": "storm",    "name": "暴风眼",   "level_id": 1705189686},
]


# ================================================================
# 玩家偏好（出生点 / 身高 / 大小 / 蜡烛 / 光翼数量）
#   存 config/user_prefs.json，不改表结构；字段缺省 = 用全局默认
#   面板「设置」页拖滑条 → POST /account/prefs {user, prefs:{...}}
# ================================================================
_PREFS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                           "config", "user_prefs.json")
_prefs_lock = threading.Lock()

_PREFS_KEYS = (
    "spawn",
    "height",
    "scale",
    "candles",
    "wingbuffs",
    "candles_granted",
    "consumables",
    "buffs",
    "candles_set",
)


def _load_prefs_all():
    try:
        with open(_PREFS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_prefs_all(d):
    tmp = _PREFS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _PREFS_FILE)


def _get_prefs(user_id):
    if not user_id:
        return {}
    with _prefs_lock:
        all_d = _load_prefs_all()
    p = all_d.get(str(user_id))
    if not isinstance(p, dict):
        return {}
    return {k: v for k, v in p.items() if k in _PREFS_KEYS}


def _set_prefs(user_id, patch):
    with _prefs_lock:
        all_d = _load_prefs_all()
        cur = all_d.get(str(user_id))
        if not isinstance(cur, dict):
            cur = {}
        for k, v in (patch or {}).items():
            if k not in _PREFS_KEYS:
                continue
            if v is None:
                cur.pop(k, None)
            else:
                cur[k] = v
        all_d[str(user_id)] = cur
        _save_prefs_all(all_d)
        return dict(cur)


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float_or_none(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ================================================================
# 当前玩家识别
#   客户端是 IL2CPP，Java 层拿不到游戏的 user id。两条路：
#   1) 客户端扫自己沙箱里的文本文件，把候选 UUID 发来 → /account/whoami 校验
#      （users 表里存在的那个才是真号）
#   2) 兜底：服务端记住"最近活跃的 user"，面板不带 user 时用它
# ================================================================
_recent_users = {}
_recent_lock = threading.Lock()
_RECENT_TTL = 900


def _note_recent(user, ip):
    try:
        with _recent_lock:
            _recent_users[str(user)] = (ip or "", time.time())
            if len(_recent_users) > 800:
                now = time.time()
                for k in [k for k, v in list(_recent_users.items())
                          if now - v[1] > _RECENT_TTL]:
                    _recent_users.pop(k, None)
    except Exception:
        pass


def _guess_user(ip):
    now = time.time()
    with _recent_lock:
        items = list(_recent_users.items())
    best = None
    for u, (uip, ts) in items:
        if now - ts > _RECENT_TTL:
            continue
        if ip and uip == ip and (best is None or ts > best[1]):
            best = (u, ts)
    if best:
        return best[0]
    alive = [(u, v[1]) for u, v in items if now - v[1] <= 180]
    return alive[0][0] if len(alive) == 1 else None


@account_bp.before_request
def _account_note_user():
    """顺手记录每个带 user 的请求 → 给「最近活跃」兜底用。"""
    if request.method != "POST":
        return None
    try:
        d = request.get_json(force=True, silent=True) or {}
        u = d.get("user")
        if u:
            _note_recent(u, request.remote_addr)
    except Exception:
        pass
    return None


@account_bp.route("/whoami", methods=["POST"])
def whoami():
    """客户端把本地扫到的候选 UUID 发来，返回 users 表里真实存在的那个。"""
    req = request.get_json(force=True, silent=True) or {}
    for c in (req.get("candidates") or []):
        if isinstance(c, str) and database.user_exists(c):
            return jsonify({"ok": True, "user": c, "from": "candidates"})
    u = _guess_user(request.remote_addr)
    return jsonify({"ok": bool(u), "user": u or "", "from": "recent"})


@account_bp.route("/prefs", methods=["POST"])
def user_prefs():
    """读/写玩家偏好。带 prefs 字段 = 写入，否则 = 读取。user 可省略。"""
    req = request.get_json(force=True, silent=True) or {}
    user_id = req.get("user") or _guess_user(request.remote_addr)
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400
    patch = req.get("prefs")
    if isinstance(patch, dict):
        patch = dict(patch)
        if "spawn" in patch:
            sp = patch["spawn"]
            if isinstance(sp, str):
                sp = _GOTO_TARGETS.get(sp.strip().lower(), 0)
            patch["spawn"] = _int_or_none(sp) or 0
        for k in ("candles", "wingbuffs"):
            if k in patch:
                patch[k] = _int_or_none(patch[k])
        for k in ("height", "scale"):
            if k in patch:
                patch[k] = _float_or_none(patch[k])
        cur = _set_prefs(user_id, patch)
        return jsonify({"result": "ok", "prefs": cur})
    return jsonify({"result": "ok", "prefs": _get_prefs(user_id),
                    "spawn_choices": SPAWN_CHOICES})


def _set_forced_level(level_id):
    with open(_GOTO_FILE, "w") as f:
        json.dump({"level": int(level_id), "expire": time.time() + _GOTO_TTL}, f)


def _take_forced_level():
    """读取传送标记；过期则丢弃。未过期时**不消费**（TTL 内持续生效）。"""
    try:
        with open(_GOTO_FILE) as f:
            d = json.load(f)
    except Exception:
        return None
    if float(d.get("expire", 0)) < time.time():
        try:
            os.remove(_GOTO_FILE)
        except OSError:
            pass
        return None
    return d.get("level")


@account_bp.route("/goto_level", methods=["POST"])
def goto_level():
    """面板按钮用：设置一次性传送目标 {target: "skyhub" | level_id: 2825107789}"""
    req = request.get_json(force=True, silent=True) or {}
    raw = req.get("target")
    if raw is None:
        raw = req.get("level_id")
    if isinstance(raw, str):
        lv = _GOTO_TARGETS.get(raw.strip().lower())
    else:
        try:
            lv = int(raw)
        except (TypeError, ValueError):
            lv = None
    if not lv:
        return jsonify({"ok": False, "msg": "未知的传送目标"}), 400
    try:
        _set_forced_level(lv)
    except Exception as e:
        return jsonify({"ok": False, "msg": "写入失败: %s" % e}), 500
    return jsonify({"ok": True, "level_id": int(lv),
                    "msg": "已设置目标关卡，重进游戏即传送"})


# ============================================================
# ★ 空巢（SkyHub2）门禁 —— 一个函数全开
# ============================================================
HUB_GATES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "hub_gates.json")


def _fnv1a32(text):
    """Sky 关卡代号 -> level_id（FNV-1a-32）。验证过 Dawn/CandleSpace/SkyHub2/HubReveal。"""
    h = 2166136261
    for b in str(text).encode("utf-8"):
        h ^= b
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def _hub_gate_levels():
    """返回 [(关卡名, level_id)] —— 空巢里所有门需要的关卡。

    ★★★ 2026-10-03：用一个函数统一解决「空巢去其他地图的门全被风墙挡住」。

    依据（用 .bin 解析器解开 assets/Data/Levels/SkyHub2/Objects.level.bin，
    2943 个 BST 节点，126 个类）：

      · 「门」= ChangeLevelWithFade，文件里共 9 处，目标关卡名：
            Dawn  Rain  Sunset  Dusk  Night×2  Storm×2  DayHubMid
      · 「门禁」= HasReachedLevel(levelName=…)，共 8 处：
            Sunset  Night  CandleSpaceEnd  Dusk  Dawn  NightEnd  HubReveal  Rain
      · HasReachedLevel 判定的是「客户端有没有到过这张图」，
        而"到过"的唯一来源就是 /account/get_checkpoints 下发的 level_id 列表。

    ⇒ 把这些关卡名对应的 level_id 全部下发一次，空巢 8 个门同时打开。
      清单放 config/hub_gates.json（由关卡文件解析得到），改 json 即可增删，
      不需要改代码。原来散落在 get_checkpoints 里的那几段硬编码补发
      （云野/雨林/霞谷/暮土/禁阁/暴风眼各分图）现在由这个函数统一覆盖。
    """
    out = []
    try:
        with open(HUB_GATES_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        _social_log("hub_gates.err", repr(exc))
        data = {}
    for item in (data.get("hub_gates") or []):
        if isinstance(item, dict) and item.get("name"):
            name = str(item["name"])
            try:
                lid = int(item.get("id") or _fnv1a32(name))
            except (TypeError, ValueError):
                lid = _fnv1a32(name)
            out.append((name, lid))
        elif isinstance(item, str):
            out.append((item, _fnv1a32(item)))
    return out


CHECKPOINT_DEFS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "checkpoint_defs.json")

# 关卡 -> level_hash。客户端在 /account/set_checkpoint 里会主动上报真实值，
# 这里是真机抓包（skyqw服 get_checkpoints.json）与参考服务器里已知的几个，
# 查不到的用 42128（参考服务器 0.13.4 的默认值）。
_LEVEL_HASHES = {
    1649439303: 23270,   # Dawn
    3526133726: 32427,   # CandleSpace 遇境
    164626931: 21787,    # Rain
    864432821: 7139,     # DuskGraveyard
    2825107789: 42128,   # SkyHub2 空巢
}
_DEFAULT_LEVEL_HASH = 42128

# 必须出现在 checkpoint 列表里的关卡（HasReachedLevel 用它判定"到过没到过"）
_ESSENTIAL_LEVELS = (
    ("Dawn", 1649439303),
    ("CandleSpace", 3526133726),
    ("SkyHub2", 2825107789),
    ("HubReveal", 2018906977),
)


def _arc_order_map():
    """config/checkpoint_defs.json -> {level_id: arc_order}（参考服务器同一张表）。"""
    try:
        with open(CHECKPOINT_DEFS_PATH, encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as e:
        _social_log("checkpoint_defs.err", repr(e))
        return {}
    out = {}
    if isinstance(data, list):
        for it in data:
            if not isinstance(it, dict):
                continue
            try:
                out[int(it.get("level_id"))] = int(it.get("arc_order"))
            except (TypeError, ValueError):
                continue
    return out


def _checkpoint_levels():
    """(level_id, name) 列表：空巢门禁全部关卡 + 4 个必需关卡。"""
    out, seen = [], set()
    for name, lid in _hub_gate_levels():
        if lid in seen:
            continue
        seen.add(lid)
        out.append((lid, name))
    for name, lid in _ESSENTIAL_LEVELS:
        if lid in seen:
            continue
        seen.add(lid)
        out.append((lid, name))
    return out


def _checkpoint_spawn_level(user_id):
    """出生点（= 唯一 is_furthest 的关卡）。

    config.json 的 force_spawn_level 优先（面板/运维钉死）；否则用该账号
    在 set_checkpoint 里记录的真实关卡；都没有 → 0（全新账号）。

    ★ 全新账号（还没到过遇境、也没有任何存档）一律返回 0 —— 这样
    checkpoint 列表为空，客户端走 TutorialStartLevel="Dawn"，
    即用户要的「新号出生在晨岛」；老号才用 force_spawn_level（遇境）。
    """
    try:
        fresh = not int(database.get_user_field(user_id, "visited_home") or 0)
    except Exception:
        fresh = False
    try:
        db_cp = int(database.get_user_field(user_id, "checkpoint") or 0)
    except (TypeError, ValueError):
        db_cp = 0
    if fresh and not db_cp:
        return 0
    try:
        forced = int(load_config().get("force_spawn_level", 0) or 0)
    except (TypeError, ValueError):
        forced = 0
    return forced or db_cp


def build_checkpoint_list(user_id):
    """构造整份 checkpoint 列表 —— 这是"遇境雕像能不能开门"的开关。

    ★★★ 2026-10-04 关键结论（三个来源互相印证）：

      ① 反汇编 CandleSpace/Objects.level.bin，遇境雕像那条链是：
           #168 FeatureFlag("system_achievements").onEnabled
             → #316 HasReachedLevel("Dawn")
               → #215 OnUnlocked("QuestStoneForm").onUnacknowledgedFx
                 → #70 HasReachedLevel("SkyHub2")
                   → #348 OnUnlocked("HubStatueForm")
                        notUnlockedFx → #114 Timeline "hub statue"
                                          → #184 Enable{#233}
                        #233 = [#37 IsLatestCheckpoint("SkyHub2"), #89 雕像,
                                #455 MeditationArea(onComplete→#8 ChangeLevelWithFade("SkyHub2"))]
      ② `HasReachedLevel(关卡名)` **只由 /account/get_checkpoints 里出现过该 level_id 来满足**
         （没有对应的 unlock 标志位）。我们原来 checkpoints_mode="single" 只回
         SkyHub2 **一条** ⇒ `HasReachedLevel("Dawn")` 直接为假 ⇒ 整条链的入口就断了
         ⇒ 坐下没反应、空巢的 8 道地图门也不开。
      ③ 之前试过"整表下发"却失败，原因是**所有条目的 is_furthest 都是 false** —
         客户端认为"没有任何最远存档"。参考服务器（win3/set_checkpoint.py）的做法是：
         列表里**恰好一条** is_furthest=true（arc_order 创新高的那条）。

      ⇒ 本函数：整表下发（Hub 门禁 28 关 + Dawn/CandleSpace/SkyHub2/HubReveal），
        并且**恰好一条** is_furthest=true —— 就是出生点那条，同时把它排到数组第一位。
        全新账号（没有任何存档）返回空列表 ⇒ 客户端走 TutorialStartLevel="Dawn"，
        即用户要的"新号出生在晨岛"。
    """
    spawn = _checkpoint_spawn_level(user_id)
    if not spawn:
        return []
    arcs = _arc_order_map()
    levels = _checkpoint_levels()
    if spawn not in {lid for lid, _ in levels}:
        levels.append((spawn, str(spawn)))
    rows = []
    for lid, _name in levels:
        rows.append({
            "user": user_id,
            "level_id": lid,
            "level_hash": _LEVEL_HASHES.get(lid, _DEFAULT_LEVEL_HASH),
            "bst_id": 7,
            "states": [True] * 16,
            "is_furthest": (lid == spawn),
            "arc_order": arcs.get(lid, 0),
        })
    # 出生点排第一（客户端按第一条 + is_furthest 取"最远/最新存档"）
    rows.sort(key=lambda r: 0 if r["is_furthest"] else 1)
    return rows


@account_bp.route("/get_checkpoints", methods=["POST"])
def get_checkpoints():
    req = request.get_json(force=True, silent=True) or {}
    user_id = req.get("user")

    if not user_id:
        return jsonify({"error": "Cannot find latest checkpoint"}), 400
    if not database.user_exists(user_id):
        return jsonify({"error": "Cannot find latest checkpoint"}), 404

    try:
        return jsonify({"checkpoints": build_checkpoint_list(user_id)})
    except Exception as e:
        _social_log("get_checkpoints.build.err", repr(e))

    # ★★★ 2026-10-03：默认改成**和 0.13.4 原版一致**的行为 —— 只回一条 checkpoint。
    #
    #   权威依据：ThatLAGroup/ThatSkyPrivateServer-For-0.13.4- 的 app/routes/account.py
    #     level_id = user.get('checkpoint', 1649439303)
    #     return {"checkpoints": [{"user","level_id","level_hash":42128,"bst_id":7,
    #                              "states":[True]*16,"is_furthest":True,"arc_order":7}]}
    #   ⇒ 原版**只回一条**，而且 `is_furthest: True`（"这是我最近/最远的存档"）。
    #
    #   我们原来回 30~36 条、且全部 `is_furthest: False`
    #   ⇒ 客户端认为**没有任何"最远存档"**
    #   ⇒ 遇境雕像那条 IsLatestCheckpoint 链不成立、关卡切换也不成立
    #   ⇒ 症状就是「能坐下，但坐完没反应，一直在原地」（2026-10-03 用户实测）。
    #
    #   想回到旧的"整表下发"行为：config.json 里把 checkpoints_mode 改成 "list"。
    try:
        if str(load_config().get("checkpoints_mode", "list")).lower() != "list":
            cp = None
            # ★★★ 2026-10-04：force_spawn_level 可以把出生点**钉死**。
            #   背景：checkpoints_mode="single" 是"跟随客户端上报"的 —— 客户端每到
            #   一张图都会 POST /account/set_checkpoint，于是玩家一回到遇境，
            #   出生点就跟着变回遇境；而遇境去 SkyHub2 的那个打坐点
            #   （MeditationArea #455）在客户端侧 onComplete 不触发（详见 README），
            #   结果就是"能进一次空巢，之后就再也进不去"。
            #   config.json 里设 "force_spawn_level": 2825107789 即可永远出生在空巢。
            #   设 0 / 不设 = 老行为（跟随 users.checkpoint）。
            try:
                forced = int(load_config().get("force_spawn_level", 0) or 0)
            except (TypeError, ValueError):
                forced = 0
            if forced:
                cp = forced
            else:
                try:
                    cp = database.get_user_field(user_id, "checkpoint")
                except Exception:
                    cp = None
                try:
                    cp = int(cp) if cp is not None else None
                except (TypeError, ValueError):
                    cp = None
            if not cp:
                # 没到过遇境的按晨岛出生（原版默认值就是 1649439303 = FNV-1a("Dawn")）
                try:
                    visited = int(database.get_user_field(user_id, "visited_home") or 0)
                except Exception:
                    visited = 0
                cp = 3526133726 if visited else 1649439303
                try:
                    database.set_user_field(user_id, "checkpoint", cp)
                except Exception:
                    pass
            return jsonify({"checkpoints": [{
                "user": user_id,
                "level_id": cp,
                "level_hash": 42128,
                "bst_id": 7,
                "states": [True] * 16,
                "is_furthest": True,
                "arc_order": 7,
            }]})
    except Exception as _e:
        _social_log("get_checkpoints.single.err", repr(_e))

    # ============================================================
    # Sky 0.15.5 版本 checkpoint 适配
    # build_version: 179644 (Android) / 179535 (iOS) / 179482 (Switch)
    # level_id = FNV-1a-32(map_codename)
    # 已验证: Dawn -> 1649439303, CandleSpace -> 3526133726
    # ============================================================

    # 晨岛（Dawn / Isle）
    DAWN_LEVEL_ID = 1649439303       # FNV-1a("Dawn")
    DAWN_LEVEL_ID_OLD = 1649439403   # MAPID.txt 记录的旧版 ID

    # 试炼洞窟（DawnCave / Trials Cave）
    DAWN_CAVE_LEVEL_ID = 748712866   # FNV-1a("DawnCave")

    # 八人门洞穴（DayHubCave）
    DAY_HUB_CAVE_LEVEL_ID = 2394719185  # FNV-1a("DayHubCave")

    # 遇境枢纽（HubReveal）- 出生点
    HUB_REVEAL_LEVEL_ID = 2018906977   # FNV-1a("HubReveal")

    # 遇境（Home / CandleSpace）
    HOME_LEVEL_ID = 3526133726       # FNV-1a("CandleSpace")

    # 遇境别名关卡（SkyHub2）- 客户端里与 CandleSpace 共用同一份地图数据
    # （BstBaked.meshes / Objects.level 哈希完全一致，Resources.lua 引用 Levels/CandleSpace/*）
    # 客户端进入 SkyHub2 / 联机都需要服务端把它当作已知关卡下发，否则闪退 / 落到 CandleSpace
    SKY_HUB2_LEVEL_ID = 2825107789   # FNV-1a("SkyHub2")

    # 云野（Prairie / Day）
    PRAIRIE_LEVEL_ID = 3265870096
    PRAIRIE_LEVEL_ID_FNVA = 927037567  # FNV-1a("Prairie")

    # 雨林（Forest / Rain）
    FOREST_LEVEL_ID = 2889552629
    FOREST_LEVEL_ID_FNVA = 164626931  # FNV-1a("Rain")

    # 霞谷（Valley / Sunset）
    VALLEY_LEVEL_ID = 1007551460
    VALLEY_LEVEL_ID_FNVA = 1638008359  # FNV-1a("Sunset")

    # ★ 云野/雨林/霞谷门真实目标代号（2026-10-02 反汇编补）——
    #   空巢门挂的目标关卡代号不是"地图代号"，而是 Day/Rain/Sunset 这套：
    #   云野门 → DayHubCave, DayEnd, Day；雨林门 → Rain, RainEnd；
    #   霞谷门 → SunsetRace, SunsetEnd, Sunset, SunsetEnd2。
    DAY_LEVEL_ID = 1230250653            # FNV-1a("Day")
    DAY_END_LEVEL_ID = 1190972738        # FNV-1a("DayEnd")
    RAIN_END_LEVEL_ID = 128844448        # FNV-1a("RainEnd")
    SUNSET_RACE_LEVEL_ID = 571720490     # FNV-1a("SunsetRace")
    SUNSET_END_LEVEL_ID = 2360310676     # FNV-1a("SunsetEnd")
    SUNSET_END2_LEVEL_ID = 507487826     # FNV-1a("SunsetEnd2")

    # ★ 暮土 / 禁阁 / 暴风眼（2026-10-02 补）——
    #   之前**从来没下发过**（见 MAPID.txt：这三张图标着"level_id 未知"）。
    #   空巢/遇境里通往各图的门是按「我有没有去过目标关卡」判定的：客户端里每个门
    #   （LevelGatesDusk_01 / LevelGatesNight_01 / LevelGatesStorm_01 …）都挂着一份
    #   目标关卡列表，关卡不在 checkpoint 里 → 客户端认为"还没去过" → 门口生成风墙
    #   （星座门阻挡体 constellation_gate_blocked_passage），只能从门顶飞过去。
    #   level_id = FNV-1a-32(关卡代号)，已用 9 个已知 ID 校验全部一致。
    DUSK_START_LEVEL_ID = 817373972       # FNV-1a("DuskStart")
    DUSK_LEVEL_ID = 1147491976            # FNV-1a("Dusk")
    DUSK_GRAVEYARD_LEVEL_ID = 864432821   # FNV-1a("DuskGraveyard")
    DUSK_MID_LEVEL_ID = 1597085778        # FNV-1a("DuskMid")
    DUSK_END_LEVEL_ID = 4158956653        # FNV-1a("DuskEnd")
    NIGHT_LEVEL_ID = 2358907137           # FNV-1a("Night")
    NIGHT2_LEVEL_ID = 2307461961          # FNV-1a("Night2")
    NIGHT_END_LEVEL_ID = 2267185542       # FNV-1a("NightEnd")
    STORM_START_LEVEL_ID = 3110721718     # FNV-1a("StormStart")
    STORM_LEVEL_ID = 1705189686           # FNV-1a("Storm")
    STORM_END_LEVEL_ID = 3479786579       # FNV-1a("StormEnd")

    # 0.15.x 版本需要 16 个 True 状态
    # 确保打坐点/冥想点已激活、神庙大门开启、无空气墙
    FULL_STATES = [True] * 16

    # ★ 出生点规则（2026-09-15）：
    #   新号（未到过遇境）→ 出生在遇境枢纽 HubReveal（新手引导）
    #   到过遇境 CandleSpace / SkyHub2 的账号 → 之后每次登录都出生在遇境
    visited_home = int(database.get_user_field(user_id, "visited_home") or 0)
    if not visited_home:
        # 兼容老账号：最近一次 checkpoint 就是遇境（CandleSpace / SkyHub2 同图），
        # 或解锁记录里已有 first_loaded_CandleSpace / first_loaded_SkyHub2
        try:
            cp = database.get_user_field(user_id, "checkpoint")
            if cp is not None and int(cp) in (HOME_LEVEL_ID, SKY_HUB2_LEVEL_ID):
                visited_home = 1
        except (TypeError, ValueError):
            pass
    if not visited_home:
        try:
            raw = database.get_user_field(user_id, "unlocks")
            if raw:
                unlocks = json.loads(raw)
                if any(u.get("name") in ("first_loaded_CandleSpace", "first_loaded_SkyHub2", "SkyHubFirstArrival") for u in unlocks):
                    visited_home = 1
        except Exception:
            pass
    if visited_home:
        database.set_user_field(user_id, "checkpoint", HOME_LEVEL_ID)
        dawn_furthest = False
        home_furthest = True
    else:
        # ★ 出生点规则（2026-09-18）：首次进入出生在晨岛 Dawn（初始地图），
        #   并立即标记 visited_home=1 → 第二次及以后每次登录统一出生在遇境 CandleSpace
        database.set_user_field(user_id, "checkpoint", DAWN_LEVEL_ID)
        database.set_user_field(user_id, "visited_home", 1)
        dawn_furthest = True
        home_furthest = False

    # ★ 一次性传送标记（面板「去空巢」按钮）：命中则无视常规出生点，直接落到目标关卡
    forced = _take_forced_level()
    if forced:
        print("[GOTO] user=%s -> level_id=%s (一次性传送)" % (user_id, forced), flush=True)

    # ★ 面板「设置 - 出生点」里选定的常驻出生点（一次性标记优先）
    if not forced:
        spawn = _int_or_none(_get_prefs(user_id).get("spawn"))
        if spawn:
            forced = spawn
            print("[SPAWN] user=%s -> level_id=%s (自定义出生点)"
                  % (user_id, spawn), flush=True)

    if forced:
        return jsonify({"checkpoints": [{
            "user": user_id,
            "level_id": int(forced),
            "level_hash": 42128,
            "bst_id": 7,
            "states": FULL_STATES,
            "is_furthest": True,
            "arc_order": 7
        }]})

    checkpoints = []

    # 1. 晨岛（Dawn）- 出生点（首次进入），同时下发两个 ID
    checkpoints.append({
        "user": user_id,
        "level_id": DAWN_LEVEL_ID,
        "level_hash": 42128,
        "bst_id": 7,
        "states": FULL_STATES,
        "is_furthest": dawn_furthest,
        "arc_order": 7
    })
    checkpoints.append({
        "user": user_id,
        "level_id": DAWN_LEVEL_ID_OLD,
        "level_hash": 42128,
        "bst_id": 7,
        "states": FULL_STATES,
        "is_furthest": False,
        "arc_order": 7
    })

    # 2. 八人门洞穴（DayHubCave）- 已通关
    checkpoints.append({
        "user": user_id,
        "level_id": DAY_HUB_CAVE_LEVEL_ID,
        "level_hash": 42128,
        "bst_id": 7,
        "states": FULL_STATES,
        "is_furthest": False,
        "arc_order": 7
    })

    # 3. 试炼洞窟（DawnCave）- 已通关
    checkpoints.append({
        "user": user_id,
        "level_id": DAWN_CAVE_LEVEL_ID,
        "level_hash": 42128,
        "bst_id": 7,
        "states": FULL_STATES,
        "is_furthest": False,
        "arc_order": 7
    })

    # 遇境枢纽（HubReveal）- 已知关卡（非出生点，放在列表后部）
    checkpoints.append({
        "user": user_id,
        "level_id": HUB_REVEAL_LEVEL_ID,
        "level_hash": 42128,
        "bst_id": 7,
        "states": FULL_STATES,
        "is_furthest": False,
        "arc_order": 7
    })

    # 5. 遇境（Home / CandleSpace）- 到过遇境的账号以此为出生点
    checkpoints.append({
        "user": user_id,
        "level_id": HOME_LEVEL_ID,
        "level_hash": 42128,
        "bst_id": 7,
        "states": FULL_STATES,
        "is_furthest": home_furthest,
        "arc_order": 19
    })

    # 5b. 遇境别名关卡（SkyHub2）- 与 CandleSpace 同地图，必须作为已知关卡下发，
    #     否则客户端进入 SkyHub2 会闪退、或导航被拉回 CandleSpace
    checkpoints.append({
        "user": user_id,
        "level_id": SKY_HUB2_LEVEL_ID,
        "level_hash": 42128,
        "bst_id": 7,
        "states": FULL_STATES,
        "is_furthest": False,
        "arc_order": 19
    })

    # 6. 云野（Prairie / Day）- 同时下发两个 ID 确保兼容
    checkpoints.append({
        "user": user_id,
        "level_id": PRAIRIE_LEVEL_ID,
        "level_hash": 42128,
        "bst_id": 7,
        "states": FULL_STATES,
        "is_furthest": False,
        "arc_order": 3
    })
    checkpoints.append({
        "user": user_id,
        "level_id": PRAIRIE_LEVEL_ID_FNVA,
        "level_hash": 42128,
        "bst_id": 7,
        "states": FULL_STATES,
        "is_furthest": False,
        "arc_order": 3
    })

    # 7. 雨林（Forest / Rain）- 同时下发两个 ID
    checkpoints.append({
        "user": user_id,
        "level_id": FOREST_LEVEL_ID,
        "level_hash": 42128,
        "bst_id": 7,
        "states": FULL_STATES,
        "is_furthest": False,
        "arc_order": 5
    })
    checkpoints.append({
        "user": user_id,
        "level_id": FOREST_LEVEL_ID_FNVA,
        "level_hash": 42128,
        "bst_id": 7,
        "states": FULL_STATES,
        "is_furthest": False,
        "arc_order": 5
    })

    # 8. 霞谷（Valley / Sunset）- 同时下发两个 ID
    checkpoints.append({
        "user": user_id,
        "level_id": VALLEY_LEVEL_ID,
        "level_hash": 42128,
        "bst_id": 7,
        "states": FULL_STATES,
        "is_furthest": False,
        "arc_order": 7
    })
    checkpoints.append({
        "user": user_id,
        "level_id": VALLEY_LEVEL_ID_FNVA,
        "level_hash": 42128,
        "bst_id": 7,
        "states": FULL_STATES,
        "is_furthest": False,
        "arc_order": 7
    })

    # 8b. ★ 云野/雨林/霞谷门的「真实目标关卡代号」补发 —— 2026-10-02 反汇编定论
    #     客户端空巢里的门（hub_doorN / LevelGates）判定"能不能进"看的是
    #     「目标关卡在不在 get_checkpoints 列表里」，不在 = "还没去过" = 风墙。
    #     反汇编 SkyHub2/Objects.level.bin 发现门挂的目标代号是：
    #       云野门 → DayHubCave, DayEnd, Day
    #       雨林门 → Rain, RainEnd
    #       霞谷门 → SunsetRace, SunsetEnd, Sunset, SunsetEnd2
    #     之前只按"地图代号"（Prairie/Forest/Valley）下发，跟门挂的代号
    #     （Day/Rain/Sunset 这套）对不上，所以风墙一直没消除。
    for _lid, _arc in (
        (DAY_LEVEL_ID, 3), (DAY_END_LEVEL_ID, 3),        # 云野门
        (RAIN_END_LEVEL_ID, 5),                            # 雨林门
        (SUNSET_RACE_LEVEL_ID, 7), (SUNSET_END_LEVEL_ID, 7),
        (SUNSET_END2_LEVEL_ID, 7),                         # 霞谷门
    ):
        checkpoints.append({
            "user": user_id,
            "level_id": _lid,
            "level_hash": 42128,
            "bst_id": 7,
            "states": FULL_STATES,
            "is_furthest": False,
            "arc_order": _arc
        })

    # 9. ★ 暮土（Dusk）/ 禁阁（Night）/ 暴风眼（Storm）——
    #    主关卡 + 各分图全部下发，否则空巢里这三个门会有风墙（"还没去过"）。
    for _lid, _arc in (
        (DUSK_START_LEVEL_ID, 9), (DUSK_LEVEL_ID, 9), (DUSK_GRAVEYARD_LEVEL_ID, 9),
        (DUSK_MID_LEVEL_ID, 9), (DUSK_END_LEVEL_ID, 9),
        (NIGHT_LEVEL_ID, 11), (NIGHT2_LEVEL_ID, 11), (NIGHT_END_LEVEL_ID, 11),
        (STORM_START_LEVEL_ID, 13), (STORM_LEVEL_ID, 13), (STORM_END_LEVEL_ID, 13),
    ):
        checkpoints.append({
            "user": user_id,
            "level_id": _lid,
            "level_hash": 42128,
            "bst_id": 7,
            "states": FULL_STATES,
            "is_furthest": False,
            "arc_order": _arc
        })

    # ★ 出生点优先级（客户端按 checkpoint 列表第一个进图）：
    #   首次进入（未到过遇境）→ Dawn 置顶；二次及以后（到过遇境）→ CandleSpace 置顶
    if home_furthest:
        checkpoints.sort(key=lambda x: 0 if x["level_id"] == HOME_LEVEL_ID else 1)
    else:
        checkpoints.sort(key=lambda x: 0 if x["level_id"] in (DAWN_LEVEL_ID, DAWN_LEVEL_ID_OLD) else 1)

    # ⚠️ 曾经这里有一段排障用的 TEMP-DEBUG，会把指定账号的出生点强制改成
    #   HubReveal（遇境枢纽 = 开场 reveal 用的"intro home"）。已删除，原因：
    #   ① 那个关卡没有"传送打坐点"，被强制进去的账号**出不来**（不能传送）；
    #   ② 它和其它账号的 CandleSpace 是**两个不同关卡** → 联机分组不同 → 同一张图
    #      里永远看不到对方。教训：排障开关用完必须删，且不要写死具体 user_id。

    # ★★★ 空巢（SkyHub2）门禁关卡 —— 见 _hub_gate_levels()。
    #
    #   ⚠️⚠️ 2026-10-03 默认**关闭**：用户实测「先前没改门的时候都能进 SkyHub2，
    #   改了之后进不去」。所以这一块先关掉，恢复改动前的 checkpoint 列表。
    #   想再启用：config.json 里 hub_gates_enable = true。
    #
    #   曾经验证的插入位置（若启用必须遵守）：插在**出生点之后**（index 1），
    #   绝不能 append 到末尾 —— 客户端把列表**最后一个**当作"最近一次 checkpoint"，
    #   而遇境雕像那支链里有 `IsLatestCheckpoint(levelName="SkyHub2")`
    #   （反汇编 CandleSpace/Objects.level.bin：#37 位于展开
    #     HubStatueForm.onUnacknowledgedFx 的 clump233 里，与去 SkyHub2 的
    #     打坐点 #455 同一个 clump）。追加到末尾 ⇒ SkyHub2 不再是"最近"
    #   ⇒ 这条链不跑 ⇒ 打坐点行为异常。
    try:
        if str(load_config().get("hub_gates_enable", False)).lower() in ("1", "true", "yes", "on"):
            have_ids = {c.get("level_id") for c in checkpoints}
            gate_entries = []
            for _name, _lid in _hub_gate_levels():
                if _lid in have_ids:
                    continue
                have_ids.add(_lid)
                gate_entries.append({
                    "user": user_id,
                    "level_id": _lid,
                    "level_hash": 42128,
                    "bst_id": 7,
                    "states": FULL_STATES,
                    "is_furthest": False,
                    "arc_order": 3,
                })
            if gate_entries:
                at = 1 if checkpoints else 0
                checkpoints[at:at] = gate_entries
                _log = current_app.logger if current_app else None
                if _log:
                    _log.info("[空巢门禁] 插入 %d 个关卡 @%d (共 %d)"
                              % (len(gate_entries), at, len(checkpoints)))
    except Exception:
        pass

    return jsonify({
        "checkpoints": checkpoints
    })

@account_bp.route("/get_external_friends", methods=["POST"])
def get_external_friendsmethods():
    cfg = load_config()
    return jsonify({"external_account_friends": []})

@account_bp.route("/serendipity/get", methods=["POST"])
def get_serendipity():
    cfg = load_config()
    return jsonify({"serendipity_matches": []})

@account_bp.route("/serendipity/join", methods=["POST"])
def serendipity_join():
    req = request.get_json(force=True, silent=True) or {}
    user = req.get("user", "")
    # serendipity（随机邂逅）加入匹配：当前实现返回空匹配池
    return jsonify({
        "serendipity_update": {
            "type": "serendipity_match_join",
            "matches": [],
            "join_confirm": True,
            "user": user
        }
    })


@account_bp.route("/star/get", methods=["POST"])
def get_star_nfc():
    cfg = load_config()
    return send_from_directory(
        directory=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config'),
        path='stars_config.json',
        mimetype='application/json'
    )

@account_bp.route("/get_outfit", methods=["POST"])
def get_outfit():
    req = request.get_json(force=True, silent=True) or {}
    user_id = req.get("user")
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400

    if not database.user_exists(user_id):
        return jsonify({"error": "User not found"}), 404

    fields = (
        "outfit_wing", "outfit_prop", "outfit_face", "outfit_mask",
        "outfit_hair", "outfit_body", "outfit_arms", "outfit_feet",
        "outfit_hat", "outfit_horn", "outfit_neck", "outfit_height"
    )
    row = database.query_one(
        f"SELECT {','.join(fields)} FROM users WHERE id = ?", (user_id,)
    )
    # ★ 修复「衣服不保存 / 外观变默认」：清库后新建的账号 outfit_* 全是建表默认 0，
    #   客户端拿到全 0 就渲染成裸装；这里补成默认装扮（缺哪项补哪项）。
    if not row or not any(row.get(f) for f in fields):
        defaults = {
            "outfit_wing": 2245279351,
            "outfit_prop": 2035109393,
            "outfit_face": 0,
            "outfit_mask": 3663390882,
            "outfit_hair": 1229053584,
            "outfit_body": 3691343236,
            "outfit_arms": 0,
            "outfit_feet": 0,
            "outfit_hat": 0,
            "outfit_horn": 0,
            "outfit_neck": 0,
            "outfit_height": -0.34827823582230427
        }
        database.execute(
            "UPDATE users SET " +
            ", ".join([f"{k}=?" for k in defaults]) +
            " WHERE id=?",
            (*defaults.values(), user_id)
        )
        if not row:
            row = defaults
        else:
            row = dict(row)
            row.update(defaults)

    # ★ 面板「设置」里的身高 / 大小（拖滑条），有值就覆盖。**必须夹紧到客户端合法区间**
    #   （客户端 `kOutfitAvatarHeight | -3.0 3.0`；越界会被丢弃 → 身高失效、高个子漂浮）
    _p = _get_prefs(user_id)
    _ph = _clamp_h(_float_or_none(_p.get("height")))
    _ps = _clamp_h(_float_or_none(_p.get("scale")), -3.0, 3.0)

    # row 是 dict，按字段名访问
    # ★ _clean_outfit：把 id==0 的槽位去掉（否则客户端每帧刷
    #   "Outfit slot is falling back to default since ID 0 is not found!"）
    return jsonify({
        "result": "ok",
        "set_outfit": _clean_outfit({
            "wing": {"tex": 0, "pat": 0, "mask": 0, "id": row["outfit_wing"]},
            "voice": 0,
            "seed": 39450,
            "scale": _ps if _ps is not None else -0.0810321030110921,
            "prop": {"tex": 0, "pat": 0, "mask": 0, "id": row["outfit_prop"]},
            "neck": {"tex": 0, "pat": 0, "mask": 0, "id": row["outfit_neck"]},
            "mask": {"tex": 0, "pat": 0, "mask": 0, "id": row["outfit_mask"]},
            "horn": {"tex": 0, "pat": 0, "mask": 0, "id": row["outfit_horn"]},
            "height": _clamp_h(_ph if _ph is not None else row["outfit_height"]),
            "hat": {"id": row["outfit_hat"]},
            "hair": {"tex": 0, "pat": 0, "mask": 0, "id": row["outfit_hair"]},
            "feet": {"tex": 0, "pat": 0, "mask": 0, "id": row["outfit_feet"]},
            "face": {"id": row["outfit_face"]},
            "body": {"tex": 0, "pat": 0, "mask": 0, "id": row["outfit_body"]},
            "attitude": "0",
            "arms": {"tex": 0, "pat": 0, "mask": 0, "id": row["outfit_arms"]}
        })
    })

# ══════════════════════════════════════════════════════════════════
# 装扮：别人的外观（远端玩家）
#
# ★★ 2026-10-03 用户实测：能看到人、能开好友树，但「看不到装扮、也看不到身高，
#    高的人直接漂浮」。根因：客户端会为每个看到的玩家请求
#        POST /account/get_remote_outfit  {"user":我, "session":…, "target":对方}
#    而我们没实现这个路由 → 被 catch-all 兜成 `{}` → 客户端
#    （libBootloader.so 里的 AccountRemoteOutfitRequest）解析失败、退回默认模型：
#      卤蛋头 + 默认身高（身高/大小来自这里：日志 "Request update - height or scale:
#      Client: (height …) Cache: (height …)"）。
#    客户端还会校验回包 target 是否与请求一致（"Response target desynced %s != %s"），
#    所以 target 必须原样回显。另外 /account/set_outfit 也一直是 `{}` → 自己的
#    装扮改动根本没落库。
# ══════════════════════════════════════════════════════════════════
_OUTFIT_DB_FIELDS = (
    "outfit_wing", "outfit_prop", "outfit_face", "outfit_mask",
    "outfit_hair", "outfit_body", "outfit_arms", "outfit_feet",
    "outfit_hat", "outfit_horn", "outfit_neck", "outfit_height"
)

_DEFAULT_OUTFIT = {
    "outfit_wing": 2245279351,
    "outfit_prop": 2035109393,
    "outfit_face": 0,
    "outfit_mask": 3663390882,
    "outfit_hair": 1229053584,
    "outfit_body": 3691343236,
    "outfit_arms": 0,
    "outfit_feet": 0,
    "outfit_hat": 0,
    "outfit_horn": 0,
    "outfit_neck": 0,
    "outfit_height": -0.34827823582230427,
}

# 客户端 kOutfitSlot_* 在 libBootloader.so 字符串表里就是这个顺序
_OUTFIT_SLOT_ENUM = {"body": 0, "wing": 1, "hair": 2, "mask": 3, "neck": 4,
                     "feet": 5, "horn": 6, "arms": 7, "prop": 8}

# ★★ 每个槽位"空"项的真实 id = FNV-1a-32("CharSkyKid_<Slot>_None")
#    （在 config/outfit_defs.json 里按 name 前缀找到、网格为 Outfit_None 的条目；
#      FNV-1a-32 公式已用 11 个已知 id 全量校验命中，见 _clean_outfit 注释。）
#    ⚠️ `arms` 不是客户端的槽位，故无此表项。
_NONE_OUTFIT_IDS = {
    "body": 1502671700,   # CharSkyKid_Body_Museum   (mesh Outfit_None)
    "face": 3599986885,   # CharSkyKid_Face_None
    "feet": 1532093990,   # CharSkyKid_Feet_None
    "hair": 1740771042,   # CharSkyKid_Hair_None
    "hat": 1470462579,    # CharSkyKid_Hat_None
    "horn": 3680499229,   # CharSkyKid_Horn_None
    "mask": 4267756678,   # CharSkyKid_Mask_None
    "neck": 3800884691,   # CharSkyKid_Neck_None
    "prop": 2035109393,   # CharSkyKid_Prop_None
    "wing": 1324678683,   # CharSkyKid_Wing_None
}

_REMOTE_OUTFIT_SEQ = {"n": 0}


def _load_outfit_rows(user_ids):
    """p91：一次取回多个玩家的装扮行 -> {user_id: row}，消掉好友列表的 N+1。

    全 0 的行（清库后的新账号）沿用单条路径 `_load_outfit_row`，保持"补默认值并写回"
    的原行为；其余直接返回，不再逐个查库。
    """
    out = {}
    ids = []
    for u in (user_ids or []):
        u = str(u or "")
        if u and u not in ids:
            ids.append(u)
    if not ids:
        return out
    marks = ",".join(["?"] * len(ids))
    try:
        rows = database.query_all(
            "SELECT id," + ",".join(_OUTFIT_DB_FIELDS) + " FROM users WHERE id IN (%s)" % marks,
            tuple(ids)) or []
    except Exception:
        return out
    for r in rows:
        rid = str(r.get("id") or "")
        if not rid:
            continue
        if not any(r.get(f) for f in _OUTFIT_DB_FIELDS):
            out[rid] = _load_outfit_row(rid)
        else:
            out[rid] = dict(r)
    return out


def _load_outfit_row(user_id):
    """读装扮行；全是 0（清库后新账号）就补默认值并写回。"""
    row = database.query_one(
        f"SELECT {','.join(_OUTFIT_DB_FIELDS)} FROM users WHERE id = ?", (user_id,)
    )
    if not row or not any(row.get(f) for f in _OUTFIT_DB_FIELDS):
        if database.user_exists(user_id):
            database.execute(
                "UPDATE users SET " + ", ".join(f"{k}=?" for k in _DEFAULT_OUTFIT) + " WHERE id=?",
                (*_DEFAULT_OUTFIT.values(), user_id)
            )
        row = dict(row) if row else {}
        row.update(_DEFAULT_OUTFIT)
    return dict(row)


_OUTFIT_WHITELIST_CACHE = {"data": None, "mtime": 0}


def _outfit_whitelist():
    """读客户端**真实**装扮表白名单 config/outfit_id_whitelist.json。

    ★★★ 2026-10-03 终极定位（这一条推翻了之前所有关于 id 的猜测）：
      客户端的装扮表**不是**服务端下发的那份 `config/outfit_defs.json`，
      而是它自带的 APK 资源 `assets/Data/Resources/OutfitDefs.json`
      （432 条；`.so` 里既没有 `outfit_defs` 也没有 `get_outfit_defs` 字符串
      ⇒ 客户端**从不调用** `/account/get_outfit_defs`）。
      实证：`libBootloader.so` 0x1042900~0x1042a84 整片连续字段名
      （`color_hsv / pattern_hsv / tint_hsv / skipMotionBlur / hornOffset /
        putBackAnimSeq / disableBody …`）**只存在于 APK 那份 schema**；
      而我们 `outfit_defs.json` 的键（`diffuseTex / inCloset / base_hsv /
      maskTex / icon_hsv / dyeable_primary …`）在 .so 里**一个都没有**。
      ⇒ 两套 schema 完全不同；客户端那份只有 **7 个槽位**
        （body / hair / horn / mask / neck / prop / wing），
        **没有 face / hat / arms**，也**没有 feet** 的任何条目。
    """
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "config", "outfit_id_whitelist.json")
    try:
        mt = os.path.getmtime(path)
        if _OUTFIT_WHITELIST_CACHE["data"] is None or _OUTFIT_WHITELIST_CACHE["mtime"] != mt:
            with open(path, encoding="utf-8") as f:
                _OUTFIT_WHITELIST_CACHE["data"] = json.load(f)
            _OUTFIT_WHITELIST_CACHE["mtime"] = mt
    except Exception as e:
        _OUTFIT_WHITELIST_CACHE["data"] = None
        try:
            _social_log("outfit_whitelist.err", repr(e))
        except Exception:
            pass
    return _OUTFIT_WHITELIST_CACHE["data"]


def _clean_outfit(o):
    """只下发客户端**真的认得**的槽位和 id；认不出的一律不发。

    ★★★ 为什么必须这样（真机闭环证据，2026-10-03）：
      上一版我们给 face / feet / hat 三个槽位"补"了 id（其中
      `_NONE_OUTFIT_IDS['body']` 还是 `CharSkyKid_Body_Museum` 这种猜出来的值）。
      但客户端：
        ① 根本没有 `face` / `hat` / `arms` 这三个槽位（`kOutfitSlot_*` 枚举
           只有 Body/Wing/Hair/Mask/Neck/Feet/Horn/Arms/Prop）；
        ② 它的表里也没有 `Face_None` / `Feet_None` / `Hat_None` 这些条目。
      ⇒ **只要回包里出现一个客户端解析不出的槽位，整个 outfit 就回落到内置默认**
        ⇒ 现象「不管对方穿什么，看到的都是同一个默认模型（卤蛋无斗篷）」；
        而"默认装"看起来是好的，**只是因为回落的默认恰好等于默认装**。

    规则：
      · 槽位必须在客户端表里存在（body/wing/hair/mask/neck/horn/prop）；
      · id 必须在白名单里（= FNV-1a-32(name)，name 来自客户端那份 432 条表）；
      · id 不在白名单 → 换成该槽位客户端表里真实的 isDefault 值，绝不发明 id；
      · 连默认都没有 → **整个槽位不发**，让客户端用它自己的默认。
    """
    wl = _outfit_whitelist() or {}
    allowed = wl.get("ids") or {}
    defaults = wl.get("default_by_slot") or {}
    none_by_slot = wl.get("none_by_slot") or {}
    slots = set(wl.get("slots") or ())

    if not allowed:
        # 白名单文件缺失时的保守兜底：只发默认装（值都取自客户端 isDefault）
        slots = {"body", "wing", "hair", "mask", "neck", "horn", "prop"}
        defaults = {k: v for k, v in _DEFAULT_OUTFIT.items()}
        allowed = {}
        for k, v in _DEFAULT_OUTFIT.items():
            if v:
                allowed[str(v)] = k

    out = {}
    for k, v in o.items():
        if not isinstance(v, dict):
            out[k] = v
            continue
        if k not in slots:
            continue                      # face / hat / arms / feet：客户端没有
        try:
            i = int(v.get("id") or 0)
        except (TypeError, ValueError):
            i = 0
        if str(i) not in allowed:
            i = int(defaults.get(k) or none_by_slot.get(k) or 0)
        if not i or (allowed and str(i) not in allowed):
            continue                      # 没有合法值 → 这个槽位干脆不发
        out[k] = dict(v, id=i)
    return out


def _clamp_h(v, lo=-3.0, hi=3.0):
    """身高/大小必须落在客户端的合法区间内。

    ★ 客户端字符串里有 `kOutfitAvatarHeight | -3.0 3.0`；真机请求记录里出现过
      `height = -4.0`（面板滑条越界）⇒ 客户端直接丢弃该值 ⇒ 渲染默认身高
      ⇒ **高个子玩家看起来在漂浮**。这里统一夹紧。
    """
    if v is None:
        return None
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return None


def _outfit_payload(user_id, row=None):
    """组装客户端认的 set_outfit 结构 —— 与 /account/get_outfit 保持完全一致
    （那条路径客户端日志是 "Successfully read outfit from server"，已验证可用）。"""
    if row is None:
        row = _load_outfit_row(user_id)
    p = _get_prefs(user_id)
    ph = _clamp_h(_float_or_none(p.get("height")))
    ps = _clamp_h(_float_or_none(p.get("scale")), -3.0, 3.0)
    return _clean_outfit({
        "wing": {"tex": 0, "pat": 0, "mask": 0, "id": row["outfit_wing"]},
        "voice": 0,
        "seed": 39450,
        "scale": ps if ps is not None else -0.0810321030110921,
        "prop": {"tex": 0, "pat": 0, "mask": 0, "id": row["outfit_prop"]},
        "neck": {"tex": 0, "pat": 0, "mask": 0, "id": row["outfit_neck"]},
        "mask": {"tex": 0, "pat": 0, "mask": 0, "id": row["outfit_mask"]},
        "horn": {"tex": 0, "pat": 0, "mask": 0, "id": row["outfit_horn"]},
        "height": _clamp_h(ph if ph is not None else row["outfit_height"]),
        "hat": {"id": row["outfit_hat"]},
        "hair": {"tex": 0, "pat": 0, "mask": 0, "id": row["outfit_hair"]},
        "feet": {"tex": 0, "pat": 0, "mask": 0, "id": row["outfit_feet"]},
        "face": {"id": row["outfit_face"]},
        "body": {"tex": 0, "pat": 0, "mask": 0, "id": row["outfit_body"]},
        "attitude": "0",
    })


@account_bp.route("/get_remote_outfit", methods=["POST"])
def get_remote_outfit():
    """别的玩家穿什么 + 身高/大小（客户端渲染远端光之子用）。"""
    req = request.get_json(force=True, silent=True) or {}
    target = str(req.get("target") or "").strip()
    if not target:
        return jsonify({"error": "Missing target"}), 400

    row = _load_outfit_row(target)
    outfit = _outfit_payload(target, row)
    unlocks = [
        {"type": _OUTFIT_SLOT_ENUM[k], "unlock": v["id"], "slot": k, "id": v["id"],
         "name": k, "option": v["id"]}
        for k, v in outfit.items()
        # 只给客户端真正有的槽位（kOutfitSlot_* 里没有 hat / face，别让它们进来）
        if isinstance(v, dict) and v.get("id") and k in _OUTFIT_SLOT_ENUM
    ]
    _REMOTE_OUTFIT_SEQ["n"] = (_REMOTE_OUTFIT_SEQ["n"] % 100000) + 1
    seq = _REMOTE_OUTFIT_SEQ["n"]

    resp = {
        "result": "ok", "status": "ok", "ok": True,
        # ★ 必须原样回显 target，否则客户端判 "Response target desynced"
        "target": target, "user": target, "id": target,
        "seq": seq, "remote_seq": seq, "remoteSeq": seq, "ack_seq": seq,
        "set_outfit": outfit, "outfit": outfit, "remote_outfit": outfit,
        "unlocks": unlocks, "unlock_count": len(unlocks), "unlocks_count": len(unlocks),
        "height": outfit["height"], "scale": outfit["scale"],
        "attitude": "0", "voice": 0, "seed": outfit["seed"],
        "serendipity_types": [], "voices": [],
    }
    # ★★ 2026-10-03 反汇编定位（libBootloader.so 0x504cc4）：
    #      mov x0,<响应对象>; mov w2,#0x11; add x1,="get_remote_outfit"; bl 查找成员
    #      cbz w0, → 找不到就走另一条路
    #    ⇒ 响应体里**必须存在一个叫 `get_remote_outfit` 的顶层成员**（长度正好 17），
    #      而且同一个函数紧接着还查 `serendipity_types`（也 17）。
    #      所以这里把整份 payload 原样再挂到同名成员下，并补一个 AccountResource
    #      常见的 `set_remote_outfit` 别名 —— 多给的键客户端会忽略，缺键才会掉进默认模型。
    # ★★★ 2026-10-03 关键修正：`get_remote_outfit` 这个顶层成员必须是**装扮本体**。
    #
    #   反汇编 libBootloader.so 0x504cc4：
    #       mov x0,<响应对象>; mov w2,#0x11; add x1,="get_remote_outfit"; bl 查成员; cbz w0 → 跳过
    #   客户端先按这个名字取成员，**取到之后直接拿它当装扮资源解析**
    #   （读 body / wing / hair / mask / neck / horn / prop / height / scale 这些槽位键）。
    #
    #   上一版我把整个 resp（set_outfit / unlocks / target / seq / height …）挂在
    #   这个名字下 —— 那个对象里**没有槽位键**，客户端解析不出任何槽位
    #   ⇒ 整体回落到内置默认模型 ⇒ 现象「身高能看到（height 在顶层被单独读过）、
    #   但装扮永远看不到（一直是卤蛋无斗篷）」。
    #   ⇒ 这里把它设成装扮本体（与 set_outfit 同一份），既安全又对症。
    resp["get_remote_outfit"] = dict(outfit)
    resp["set_remote_outfit"] = dict(outfit)
    _social_log("get_remote_outfit", {"user": _req_user(req), "target": target,
                                      "height": resp["height"], "scale": resp["scale"],
                                      "unlocks": len(unlocks)})
    return jsonify(resp)


@account_bp.route("/get_level_pickups", methods=["POST"])
def get_level_pickups():
    req = request.get_json(force=True, silent=True) or {}
    level = str(req.get("level", "")).strip()

    pickups_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "level_pickups_config")
    file_path = os.path.join(pickups_dir, f"{level}.json")

    if os.path.isfile(file_path):
        return send_from_directory(directory=pickups_dir, path=f"{level}.json",
                                   mimetype='application/json')

    log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
    log_file = os.path.join(log_dir, "not_found_level.txt")
    os.makedirs(log_dir, exist_ok=True)

    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"{datetime.utcnow().isoformat()}Z - {json.dumps(req, ensure_ascii=False)}\n")

    # ★ 关键修复：以前这里直接返回 {}。
    #   客户端解析地图物件（拾取物 / 发射器）配置时是按固定字段读的，
    #   拿到 {} 会因为字段缺失而卡在「地图初始化」阶段 —— 表现就是
    #   「进入该地图后无法移动」。这里返回结构完整的空配置。
    try:
        level_num = int(level)
    except (TypeError, ValueError):
        level_num = 0

    return jsonify({
        "level": level_num,
        "pickups": [],
        "global_pickups": [],
        "pickup_emitters": []
    })

@account_bp.route("/find_previous_or_empty", methods=['POST'])
def find_previous_or_empty():
    """查找可用的 UDP 房间服务器。

    ★ 2026-10-03 按参考服务器 thatskygame.de5.net 实测对齐：
      回包字段与 /account/hb 完全一致（conn_queued / delay / level /
      level_hash / move_ts / new_cutoff / other_players / private_uri /
      sig / signature / uri），其中 sig == signature 且都是 64 位 sha256，
      uri == private_uri == "host:port"。

      之前这里回的是自造结构（status/session/source/platform_filter/ps_only），
      且 sig 与 signature 是两个不同的硬编码值，与参考实现不一致。
    """
    body = request.get_json(force=True, silent=True) or {}
    # ★ 记住客户端自报的版本（好友传送回包要原样回显，见 _record_client_ver）
    try:
        _record_client_ver(body)
    except Exception:
        pass

    app_logger = current_app.logger if current_app else None
    if app_logger:
        app_logger.info("[UDP] 下发房间服务器 %s (from %s)" % (_udp_uri(), request.host))

    return jsonify(_join_payload(body))

@account_bp.route("/join_previous_game", methods=["POST"])
@account_bp.route("/join_friend_game", methods=["POST"])
def join_previous_game():
    """加入好友 / 上一局游戏（好友树上「传送 / 关注」走这里）。

    ★★★ 2026-10-06 按官方抓包重写 + 版本回显：
      move_to_game = {signature, sig, target_id, client_changelist, ps_only,
                      platform_filter, public_ip, private_ip, port, level,
                      level_hash, client_version, players, move_ts, event_id}
      · level/level_hash 用**好友当前所在关卡**（原来恒发 0，客户端不知道去哪张图）。
      · client_version / client_changelist 必须回**客户端自己上报的那套**
        （本服 0.15.5 = 4852 / build 179644）。原来硬编码 5023 / "282913"，
        客户端判定"目标房间需要更新的客户端" ⇒ 传送直接报失败。
      · sig 与 signature 官方是两个不同的 sha256。
      · 好友不在房间时退回请求里的 level，仍 200 + is_joinable=true
        （避免客户端因为失败把好友树清空）。
    """
    body = request.get_json(force=True, silent=True) or {}
    try:
        room = _join_payload(body) or {}
    except Exception as e:
        _social_log("join_previous_game.room.err", repr(e))
        room = {}
    target = _pick_any(body, "target_id", "friend_id", "target", "recv_user") or ""
    now_ms = int(time.time() * 1000)

    level, lhash, in_game = 0, 0, False
    try:
        import udp_rooms
        p = udp_rooms.peer_of_user(target) if target else None
        if p and p.get("inGame"):
            in_game = True
            level = int(p.get("levelId") or 0)
            lhash = udp_rooms.level_hash(level, 0)
    except Exception as e:
        _social_log("join_previous_game.rooms.err", repr(e))

    if not level:
        try:
            level = int(body.get("level", body.get("recent_level_hash") or 0) or 0)
        except (TypeError, ValueError):
            level = 0
    if not lhash:
        try:
            lhash = int(body.get("level_hash", body.get("recent_level_hash") or 0) or 0)
        except (TypeError, ValueError):
            lhash = 0

    # ★ 客户端版本：回它自己上报的那套（见 _record_client_ver 的说明）
    ver = _client_ver_of(_req_user(body))
    if body.get("client_version"):
        try:
            ver["client_version"] = int(body["client_version"])
        except (TypeError, ValueError):
            pass
    if body.get("server_version"):
        try:
            ver["server_version"] = int(body["server_version"])
        except (TypeError, ValueError):
            pass

    uri = room.get("uri") or _udp_uri()
    host = uri.split(":")[0] if ":" in uri else uri
    try:
        port = int(uri.rsplit(":", 1)[1])
    except (TypeError, ValueError, IndexError):
        port = 8125
    try:
        public_ip = str(load_config().get("udp_server_host") or host)
    except Exception:
        public_ip = host
    if public_ip in ("", "auto", "0.0.0.0", "127.0.0.1", "localhost"):
        public_ip = host

    sig = room.get("sig") or _sign_session(target or "", uri, now_ms)
    signature = _sign_session("sig|%s|%s|%d" % (target or "", uri, now_ms), uri, now_ms)

    move = {
        "signature": signature,
        "sig": sig,
        "uri": uri,
        "private_uri": room.get("private_uri") or uri,
        "target_id": target,
        "client_changelist": ver["client_changelist"],
        "ps_only": room.get("ps_only", False),
        "platform_filter": room.get("platform_filter", 0),
        "public_ip": body.get("public_ip") or public_ip,
        "private_ip": body.get("private_ip") or host,
        "port": body.get("port") or port,
        "level": level,
        "level_hash": lhash,
        "client_version": ver["client_version"],
        "server_version": ver["server_version"],
        "players": ([target] if target else []),
        "move_ts": now_ms,
        "event_id": body.get("event_id", 0),
        "in_game": in_game,
    }
    # ★ 2026-10-06：好友不在房间里（in_game=false）时，
    #   ① 不能再下发 level=0 —— 客户端会拿它去移动（FriendBarn.cpp 的
    #      "Failed to join friend game"）；改成"我自己当前那张图"，
    #      即使客户端硬走一次移动也不会跳到 0 号图。
    #   ② is_joinable=false，让 UI 给出正确的"好友不在游戏中"。
    #   注意：没有 target 的 /account/join_previous_game（回上一局）保持原样。
    is_joinable = True
    if target and not in_game:
        is_joinable = False
        try:
            import udp_rooms as _ur
            _my_lv = int(_ur.level_of_user(_req_user(body), 0) or 0)
        except Exception:
            _my_lv = 0
        if _my_lv:
            level = _my_lv
            move["level"] = level
            move["move_ts"] = int(time.time() * 1000)
    resp = {
        "move_to_game": move,
        "is_joinable": is_joinable,
        "is_dnd": False,
        "result": "ok",
        "status": "ok",
    }
    # ★ 传送后自动验收（8 秒后比对：我有没有换图、有没有到好友那张图），只写日志
    try:
        if target and level:
            _my_before = 0
            try:
                import udp_rooms as _ur0
                _my_before = int(_ur0.level_of_user(_req_user(body), 0) or 0)
            except Exception:
                _my_before = 0
            _verify_teleport(_req_user(body), target, level, _my_before, 8.0)
    except Exception:
        pass
    _social_log("join_previous_game.resp", {"user": _req_user(body), "target": target,
                                            "uri": uri, "level": level, "level_hash": lhash,
                                            "in_game": in_game, "client_version": ver["client_version"],
                                            "client_changelist": ver["client_changelist"]})
    return jsonify(resp)

@account_bp.route("/link_gamecenter", methods=["POST"])
def link_gamecenter():
    """Game Center 关联接口 - 不记录日志"""
    # 直接返回成功，不触发日志中间件
    # 注意：如果全局日志中间件没有过滤，这个请求还是会触发日志
    return jsonify({
        "result": "ok",
        "linked": True
    })


@account_bp.route("/set_checkpoint", methods=["POST"])
def set_checkpoint():
    req = request.get_json(force=True, silent=True) or {}
    user = req.get("user")
    level_id = req.get("level_id")
    level_hash = req.get("level_hash", 42128)
    bst_id = req.get("bst_id", 7)
    # 始终返回全部已激活状态(16个True)，与 get_checkpoints 保持一致
    # 确保：1) 打坐点不会因状态不一致而消失 2) 神庙大门保持开启
    states = [True] * 16
    arc_order = req.get("arc_order", 7)

    if not user or level_id is None:
        return jsonify({"error": "Missing user or level_id"}), 400

    if not database.user_exists(user):
        return jsonify({"error": "User not found"}), 404

    database.set_user_field(user, "checkpoint", level_id)
    # ★ 2026-10-06 记下 (level_id, level_hash)：好友传送要下发
    #   好友那张图的真实 level_hash（官方抓包 level_hash=32977）。
    try:
        import udp_rooms
        udp_rooms.record_level_hash(level_id, level_hash)
    except Exception:
        pass

    # ★ 记录"到过遇境(CandleSpace / SkyHub2)"：客户端进入遇境时上报 level_id
    #   3526133726（CandleSpace）或 2825107789（SkyHub2，同图别名）。
    #   一旦到过，之后登录一律出生在遇境（见 get_checkpoints）
    HOME_LEVEL_ID = 3526133726  # FNV-1a("CandleSpace")
    SKY_HUB2_LEVEL_ID = 2825107789  # FNV-1a("SkyHub2")，与 CandleSpace 同地图
    try:
        if int(level_id) in (HOME_LEVEL_ID, SKY_HUB2_LEVEL_ID):
            database.set_user_field(user, "visited_home", 1)
    except (TypeError, ValueError):
        pass

    # ★★★ 2026-10-04：set_checkpoint 的回包也要跟 force_spawn_level 对齐。
    #
    #   客户端把 set_checkpoint 的**响应**当成"我当前的最新存档点"：
    #   玩家一进遇境就上报 level_id=3526133726(CandleSpace)，我们原来原样回
    #   CandleSpace ⇒ 客户端的"最新存档点"变成遇境。
    #
    #   而遇境去空巢的打坐点 #455 和 #37 IsLatestCheckpoint("SkyHub2") 是
    #   **同一个 clump #233 里的兄弟节点**（见 hub-door 反汇编）：
    #     #233 = [ #37 IsLatestCheckpoint("SkyHub2"), #350 SetRender,
    #              #183 DialogHint("intro_skyhub_00"), #89 雕像, #455 打坐点 ]
    #   2026-10-03 那次"能坐下、动画放完站原地不动"就是 checkpoint 语义不对
    #   （当时是 is_furthest 全 false，改成单条 true 后才好了一半）。
    #   这里把回包钉死在 force_spawn_level 上，客户端任何时刻的"最新存档点"
    #   都是 SkyHub2，IsLatestCheckpoint 那条链才成立。
    #   DB 里仍然记客户端真实上报的关卡（退出重登的出生点照样是 force_spawn_level）。
    # ★★★ 2026-10-04：回包改成**和 get_checkpoints 完全一致的整表**。
    #
    #   原来这里只回"客户端刚进的那一张图"一条 —— 而那会让客户端的
    #   checkpoint 集合瞬间塌成 1 条：玩家一进遇境，`HasReachedLevel("Dawn")`
    #   就变假（整条雕像链的入口），空巢那 8 道地图门也随之关闭。
    #   参考服务器（win3/set_checkpoint.py）也是回整份列表。
    #   DB 里仍然记客户端真实上报的关卡（用于出生点回落）。
    try:
        return jsonify({"checkpoints": build_checkpoint_list(user)})
    except Exception as _e:
        _social_log("set_checkpoint.build.err", repr(_e))

    return jsonify({
        "checkpoints": [
            {
                "user": user,
                "level_id": level_id,
                "level_hash": level_hash,
                "bst_id": bst_id,
                "states": states,
                "is_furthest": True,
                "arc_order": arc_order
            }
        ]
    })

@account_bp.route("/set_outfit", methods=["POST"])
def set_outfit():
    req = request.get_json(force=True, silent=True) or {}
    user = req.get("user")
    outfit = req.get("outfit", {})

    if not user or not outfit:
        return jsonify({"error": "missing user or outfit"}), 400

    fields = {
        "outfit_body": outfit.get("body", {}).get("id", 0),
        "outfit_wing": outfit.get("wing", {}).get("id", 0),
        "outfit_hair": outfit.get("hair", {}).get("id", 0),
        "outfit_mask": outfit.get("mask", {}).get("id", 0),
        "outfit_neck": outfit.get("neck", {}).get("id", 0),
        "outfit_feet": outfit.get("feet", {}).get("id", 0),
        "outfit_horn": outfit.get("horn", {}).get("id", 0),
        "outfit_arms": outfit.get("arms", {}).get("id", 0),
        "outfit_prop": outfit.get("prop", {}).get("id", 0),
        # ★ 修复：以前 hat / face 被写死成 0，玩家换的帽子和面具永远存不住
        "outfit_hat": outfit.get("hat", {}).get("id", 0),
        "outfit_face": outfit.get("face", {}).get("id", 0),
        "outfit_height": outfit.get("height", 0)
    }

    if not database.user_exists(user):
        return jsonify({"error": "user not found"}), 404

    database.execute(
        "UPDATE users SET " +
        ", ".join([f"{k}=?" for k in fields]) +
        " WHERE id=?",
        (*fields.values(), user)
    )

    row = database.query_one(
        "SELECT outfit_wing, outfit_prop, outfit_face, outfit_mask, "
        "outfit_hair, outfit_body, outfit_arms, outfit_feet, "
        "outfit_hat, outfit_horn, outfit_neck, outfit_height "
        "FROM users WHERE id = ?", (user,)
    )

    # ★ 面板「设置」里的身高 / 大小优先于客户端本次传来的值（**都要夹紧到客户端合法区间**）
    _p = _get_prefs(user)
    _ph = _clamp_h(_float_or_none(_p.get("height")))
    _ps = _clamp_h(_float_or_none(_p.get("scale")), -3.0, 3.0)

    return jsonify({
        "result": "ok",
        "set_outfit": _clean_outfit({
            "wing": {"tex": 0, "pat": 0, "mask": 0, "id": row["outfit_wing"]},
            "voice": 0,
            "seed": 37061,
            "scale": _clamp_h(_ps if _ps is not None else outfit.get("scale", 0.009), -3.0, 3.0),
            "prop": {"tex": 0, "pat": 0, "mask": 0, "id": row["outfit_prop"]},
            "neck": {"tex": 0, "pat": 0, "mask": 0, "id": row["outfit_neck"]},
            "mask": {"tex": 0, "pat": 0, "mask": 0, "id": row["outfit_mask"]},
            "horn": {"tex": 0, "pat": 0, "mask": 0, "id": row["outfit_horn"]},
            "height": _clamp_h(_ph if _ph is not None else row["outfit_height"]),
            "hat": {"id": row["outfit_hat"]},
            "hair": {"tex": 0, "pat": 0, "mask": 0, "id": row["outfit_hair"]},
            "feet": {"tex": 0, "pat": 0, "mask": 0, "id": row["outfit_feet"]},
            "face": {"id": row["outfit_face"]},
            "body": {"tex": 0, "pat": 0, "mask": 0, "id": row["outfit_body"]},
            "attitude": str(outfit.get("attitude", 0)),
        })
    })

@account_bp.route("/get_relationship_abilities", methods=["POST"])
def get_relationship_abilities():
    cfg = load_config()
    return send_from_directory(
        directory=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config'),
        path='relationship_abilities.json',
        mimetype='application/json'
    )

@account_bp.route("/iaplist", methods=["POST"])
def iap_list():
    """内购商品列表 + 该账号的购买状态。

    ★ 2026-10-04：原来是直接发 config/iaplist.json（静态文件），
      于是 purchased_non_consumables / unfulfilled_purchases 永远是空数组。
      现在按账号动态填这两项（数据来自 commerce_orders 表），
      客户端才能正确显示"已购/待发放"。

    客户端字段（.so Commerce.cpp 字符串簇）：
      AccountIAPList | system_loading_iaplist | /account/iaplist |
      purchased_non_consumables | unfulfilled_purchases | commerce_gift_count
    """
    req = request.get_json(force=True, silent=True) or {}
    me = _req_user(req)
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config', 'iaplist.json')
    try:
        with open(path, encoding='utf-8-sig') as f:
            data = json.load(f)
    except Exception as e:
        _social_log("iaplist.err", repr(e))
        data = {"iaplist": [], "purchased_non_consumables": [],
                "unfulfilled_purchases": [], "commerce_gift_count": 0}
    products = {p.get("id"): p for p in (data.get("iaplist") or []) if isinstance(p, dict)}
    owned, unfulfilled = [], []
    if me:
        for row in database.commerce_orders_of(me):
            pid = row.get("product_id")
            prod = products.get(pid) or {"id": pid, "type": "Consumable"}
            item = dict(prod)
            item["transaction_id"] = row.get("transaction_id")
            item["product_id"] = pid
            item["status"] = row.get("status")
            # purchased_non_consumables 只放**非消耗品**（客户端用它做"恢复购买"）
            if str(prod.get("type", "")).lower().startswith("non"):
                if str(row.get("status")) == "fulfilled" or True:
                    owned.append(pid)
            if str(row.get("status")) != "fulfilled":
                unfulfilled.append(item)
    data["purchased_non_consumables"] = owned
    data["unfulfilled_purchases"] = unfulfilled
    data["commerce_gift_count"] = len([1 for _ in unfulfilled])
    # ★★★ 2026-10-04 内购商店「正在维护」的正主：
    #   客户端反汇编（libBootloader.so fn 0x5960b4，AccountIAPList 描述符）读的字段名是
    #     iap_list / purchased_non_consumables / unfulfilled_purchases / commerce_gift_count
    #   而 config/iaplist.json 里的顶层键叫 `iaplist`（老版本客户端的叫法）。
    #   键名对不上 ⇒ 客户端拿到的商品数永远是 0；商店状态机在 WaitForAccountResource
    #   (fn 0x594268, 0x59441C) 里比较"商品数量有没有变多"，数量不涨就**原地返回不推进**
    #   ⇒ 永远到不了 ReadyForPurchases ⇒ 弹 commerce_purchases_unavailable
    #   （「内购商店正在维护，请稍后再来。」）。
    #   两个键都发，新旧客户端各取所需，且保证非空。
    data["iap_list"] = data.get("iaplist") or []
    return jsonify(data)


# ---------------------------------------------------------------- 内购 / commerce
IAP_LIST_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "iaplist.json")


def _iap_products():
    """config/iaplist.json 的 {product_id: 商品} 表。"""
    try:
        with open(IAP_LIST_PATH, encoding="utf-8-sig") as f:
            data = json.load(f)
        return {p.get("id"): p for p in (data.get("iaplist") or []) if isinstance(p, dict)}
    except Exception as e:
        _social_log("iap_products.err", repr(e))
        return {}


def _iap_grant(user, product):
    """按商品定义把奖励发给账号（货币 + 解锁项）。返回发放明细。"""
    granted = []
    if not user or not isinstance(product, dict):
        return granted
    for tkey, ckey in (("currencyType", "currencyCount"), ("currencyType2", "currencyCount2")):
        ctype, cnt = product.get(tkey), product.get(ckey)
        if not ctype or not cnt:
            continue
        # ★ p77：candles（白蜡烛）必须以 users.candles 为准 —— 客户端的蜡烛数字
        #   来自 build_currency() 里的 _candle_balance(user)，它会覆盖 currency 表的
        #   candles 列。以前内购只写 currency 表 ⇒ 买了看不到（用户实测）。
        if str(ctype) == "candles":
            try:
                total = _candle_balance(user) + int(cnt)
                database.set_user_field(user, "candles", total)
                granted.append({"currency": "candles", "count": int(cnt), "total": total})
                continue
            except Exception as _e:
                _social_log("iap_grant.candles.err", repr(_e))
        ok, val = database.currency_add(user, ctype, cnt)
        if ok:
            granted.append({"currency": ctype, "count": int(cnt), "total": val})
    # 解锁项（形如 {{Seasons::iap_unlocks_SPASSR}}，本服不解析模板，原样记录）
    for unl in (product.get("unlocks") or []):
        try:
            _merge = unl if isinstance(unl, str) else json.dumps(unl, ensure_ascii=False)
            if _merge:
                granted.append({"unlock": _merge})
        except Exception:
            pass
    return granted


@account_bp.route("/commerce/receipt", methods=["POST"])
def commerce_receipt():
    """内购凭据校验 —— 私服不做 Google/Apple 校验，直接接受并发放。

    客户端字段（.so Commerce.cpp）：
      CommerceVerifyReceiptRequest -> /account/commerce/receipt
        请求: target_pid / target_uid / transactions
        回包: errorcode（0 = 成功）
      客户端日志串: "Verification receipt empty" / "Verification productID:… transactionId:…"
                    / "Fulfillment Verification succeeded, processing response" / duplicate

    ★ 前提：客户端必须走 kUseAppStoreIAP=false 这条"假购买"路径
      （assets/Data/Vars/Vars_Live.lua 里改；Vars_Test.lua 本来就是 false），
      否则它去问真实商店，根本不会把凭据发到这里。
    """
    req = request.get_json(force=True, silent=True) or {}
    _social_log("commerce.receipt.req", req)
    me = _req_user(req)
    target = _pick_any(req, "target_uid", "target_pid", "user_id") or me
    txs = req.get("transactions")
    if isinstance(txs, dict):
        txs = [txs]
    if not isinstance(txs, list):
        txs = []
    # ★★★ 2026-10-04 真机实证（新客户端 10:05:52 的**完整**请求体）：
    #   走 kUseAppStoreIAP=false 的"假购买"路径时**根本没有 transactions 数组**，
    #   商品 id 在 `target_pid`、凭据在 `receipt`（形如 "<包名>.<商品ID>"）：
    #     {"user":"…","session":"…","platform":"fake",
    #      "receipt":"com.hwb.sky.FTCDL2","target_pid":"FTCDL2","target_uid":"…"}
    #   我们原来只读 transactions ⇒ 每次都回 `{"n":0,"granted":[]}`：
    #   客户端显示"购买成功"、服务端一样东西都没发（用户报的"买了没到账"）。
    if not txs:
        _pid = _pick_any(req, "target_pid", "target_product_id", "product_id", "productId")
        if _pid:
            txs = [{
                "product_id": _pid,
                "transaction_id": (_pick_any(req, "receipt", "transaction_id", "transactionId")
                                   or _pid),
            }]
    products = _iap_products()
    out, granted_all = [], []
    for tx in txs:
        if not isinstance(tx, dict):
            continue
        pid = tx.get("product_id") or tx.get("productId") or tx.get("id")
        tid = tx.get("transaction_id") or tx.get("transactionId") or tx.get("tid")
        if not pid:
            continue
        if not tid:
            tid = str(uuid.uuid4())
        prod = products.get(pid)
        # ★ 消耗品（蜡烛包）**每次购买都要发**：假购买路径的 receipt 永远是
        #   "com.hwb.sky.<pid>"、没有随机 nonce，按它去重会把"再买一次"当成重复。
        #   非消耗品按 transaction_id/product 幂等，防止重复刷。
        is_consumable = bool(prod) and str(prod.get("type", "")).lower().startswith("cons")
        is_new = database.commerce_record(me or target, pid, tid, "verified", "")
        granted = []
        # ★ p76：假购买路径的 receipt 恒为 "com.hwb.sky.<pid>"，按它去重会把
        #   "再买一次"当成重复 ⇒ 非消耗品（SNC00 蜡烛包）第二次买就不发货
        #   （用户报的"商店买白蜡烛不到账"）。私服一律每次购买都发。
        if prod:
            granted = _iap_grant(me or target, prod)
            granted_all.extend(granted)
        item = dict(tx)
        item.update({"product_id": pid, "transaction_id": tid,
                     "result": "ok", "status": "verified",
                     "productId": pid, "transactionId": tid,
                     "duplicate": (not is_new) and (not is_consumable),
                     "granted": granted})
        out.append(item)
    resp = {
        "errorcode": 0, "result": "ok", "status": "ok",
        "transactions": out, "verified": out, "granted": granted_all,
    }
    # ★ 买完立刻回一份货币快照：客户端拿它刷新蜡烛/季节蜡烛数字。
    #   参考服务器（win3/currency_helper）所有涉及扣费/发货的接口都带 currency。
    try:
        if me or target:
            resp["currency"] = build_currency(me or target)
    except Exception as _e:
        _social_log("commerce.receipt.currency.err", repr(_e))
    _social_log("commerce.receipt.resp", {"n": len(out), "granted": granted_all})
    return jsonify(resp)


@account_bp.route("/commerce/fulfill", methods=["POST"])
def commerce_fulfill():
    """把某个购买订单标记为已发放。

    客户端字段：CommerceFulfillReceiptItemRequest -> /account/commerce/fulfill，请求 target_tid。
    客户端日志串: "Fake Fulfill productID: …"
    """
    req = request.get_json(force=True, silent=True) or {}
    _social_log("commerce.fulfill.req", req)
    me = _req_user(req)
    tid = _pick_any(req, "target_tid", "transaction_id", "transactionId", "tid")
    pid = _pick_any(req, "product_id", "productId")
    if tid:
        database.commerce_set_status(me, tid, "fulfilled")
    cur = {}
    try:
        row = database.currency_get(me) or {}
        for _col in ("candles", "hearts", "heart_wax", "season_candle", "season_heart",
                     "season_pass_token", "wax", "season_wax", "prestige", "prestige_wax"):
            v = _int_or_none(row.get(_col))
            if v:
                cur[_col] = v
    except Exception:
        cur = {}
    return jsonify({"errorcode": 0, "result": "ok", "status": "fulfilled",
                    "transaction_id": tid or "", "product_id": pid or "",
                    "fulfilled": True, "currency": cur})


@account_bp.route("/commerce/update", methods=["POST"])
def commerce_update():
    """受赠方信息更新（送礼场景）。客户端字段：gifter。"""
    req = request.get_json(force=True, silent=True) or {}
    _social_log("commerce.update.req", req)
    return jsonify({"errorcode": 0, "result": "ok", "status": "ok",
                    "gifter": _pick_any(req, "gifter", "user") or ""})


@account_bp.route("/get_invites", methods=["POST"])
def get_invites():
    """客户端周期性拉取"待接受的好友邀请"。

    ★ 2026-10-03：以前这里写死返回 {"invites": []} —— 真机请求记录里
      `POST /account/get_invites {"user":…,"session":…}` 一直在轮询，永远拿到空表，
      所以"接受好友"入口只能靠礼物消息兜。现在读 pending_invites 表
      （与 /check_invite 同一份数据），并多给几种键名拼写以防猜错。
    """
    req = request.get_json(force=True, silent=True) or {}
    me = _req_user(req)
    invites = []
    if me:
        try:
            for r in database.query_all(
                    # ★ 列名是 `nickname`（见 db.py 的建表语句），不是 invite_nickname ——
                    #   写错会导致 OperationalError(1054) 被吞掉 → 永远返回空邀请列表
                    #   → 真机表现就是「接受好友点了没反应」。
                    "SELECT token_id, from_user, nickname, level_id, created_at, status "
                    "FROM pending_invites WHERE to_user = ? AND status = 'pending' "
                    "ORDER BY created_at DESC LIMIT 50", (me,)):
                invites.append({
                    "token_id": r.get("token_id"), "token": r.get("token_id"),
                    "from_user": r.get("from_user"), "user": r.get("from_user"),
                    "invite_nickname": r.get("nickname") or "",
                    "nickname": r.get("nickname") or "",
                    "level_id": r.get("level_id") or 0,
                    "created_at": r.get("created_at") or 0,
                    "status": "pending",
                })
        except Exception as e:
            _social_log("get_invites.err", repr(e))
    if invites:
        _social_log("get_invites", {"user": me, "count": len(invites)})
    return jsonify({"result": "ok", "status": "ok", "ok": True,
                    "invites": invites, "count": len(invites),
                    "set_invites": invites})

@account_bp.route("/buff/get_buff_defs", methods=["POST"])
def get_buff_defs():
    cfg = load_config()
    return send_from_directory(
        directory=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config'),
        path='buff_defs.json',
        mimetype='application/json'
    )

@account_bp.route("/get_generic_shops", methods=["POST"])
def get_generic_shops():
    cfg = load_config()
    return send_from_directory(
        directory=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config'),
        path='generic_shops.json',
        mimetype='application/json'
    )

@account_bp.route("/set_achievement_stats", methods=["POST"])
def set_achievement_stats():
    req = request.get_json(force=True, silent=True) or {}
    user = req.get("user")
    stats = req.get("achievement_stats", [])

    if not user or not isinstance(stats, list):
        return jsonify({"error": "invalid payload"}), 400

    if not database.user_exists(user):
        return jsonify({"error": "user not found"}), 404

    raw_data = database.get_user_field(user, "achievements")
    old = json.loads(raw_data) if raw_data else []

    now = int(datetime.utcnow().timestamp())
    merged = {item["type"]: item for item in old}
    for st in stats:
        tp, val = st["type"], st["value"]
        if tp in merged:
            merged[tp]["value"] += val
            merged[tp]["update"] = now
        else:
            merged[tp] = {"type": tp, "value": val, "update": now}

    new_list = list(merged.values())
    database.set_user_field(user, "achievements", json.dumps(new_list))

    return jsonify({
        "achievement_stats": [
            {"type": i["type"], "value": i["value"], "update": i["update"]}
            for i in new_list
        ]
    })

# 空白返回区
@account_bp.route("/drop_unlock", methods=["POST"])
def drop_unlock():
    return jsonify({}), 200

@account_bp.route("/collect_pickup_batch", methods=["POST"])
def collect_pickup_batch():
    """烧花 / 收烛火。参考 win3 account/collect_pickup_batch：
      每个 pickup +4 烛火，写进**数据库** currency.wax。
      老实现写的是 user_prefs.json，currency.wax 永远是 0 —— 客户端烛火条不动、
      数据库里也查不到任何烛火。同时补上锻造：烛火攒够 candle_forge_cost
      （默认 150）就换 1 根蜡烛，写回 users.candles（蜡烛的权威存储）。
    """
    req = request.get_json(force=True, silent=True) or {}
    me = req.get("user") or req.get("user_id")
    level_id = req.get("level_id")
    pickup_ids = req.get("pickup_ids") or []
    global_ids = req.get("global_pickup_ids") or []
    season_ids = []
    wax_globals = []
    for g in global_ids:
        try:
            gi = int(g)
        except (TypeError, ValueError):
            gi = 0
        if gi == 4180017154:
            season_ids.append(g)
        else:
            wax_globals.append(g)
    wax_gain = 4 * (len(pickup_ids) + len(wax_globals))

    # ---- 1) 烛火进数据库 currency.wax ----
    wax_total = None
    try:
        if wax_gain:
            _ok, wax_total = database.currency_add(me, "wax", wax_gain)
        if season_ids:
            database.currency_add(me, "season_candle", len(season_ids))
    except Exception as ex:
        _social_log("collect_pickup_batch.add.err", repr(ex))
    if wax_total is None:
        try:
            wax_total = int((database.currency_get(me) or {}).get("wax") or 0)
        except Exception:
            wax_total = 0
    wax_total = int(wax_total or 0)

    # ---- 2) 锻造：烛火 -> 蜡烛 ----
    cost = 150
    try:
        _c = (load_config() or {}).get("candle_forge_cost")
        if not _c:
            _v = json.load(open("/wbsky/config/vars.json", encoding="utf-8-sig"))
            _c = (_v.get("vars") or {}).get("candle_forge_cost")
        if _c:
            cost = int(_c)
    except Exception:
        cost = 150
    if cost <= 0:
        cost = 150
    forged = 0
    try:
        if wax_total >= cost:
            forged = wax_total // cost
            database.currency_add(me, "wax", -(forged * cost))
            wax_total -= forged * cost
            cur_c = _int_or_none(database.get_user_field(me, "candles")) or 0
            cur_c += forged
            database.set_user_field(me, "candles", cur_c)
            database.currency_add(me, "candles", forged)
    except Exception as ex:
        _social_log("collect_pickup_batch.forge.err", repr(ex))

    # ---- 3) 响应（官方抓包形状）----
    candles = None
    try:
        candles = _int_or_none(database.get_user_field(me, "candles"))
    except Exception:
        candles = None
    snap = {}
    try:
        snap = build_currency(me, candles)
    except Exception as ex:
        _social_log("collect_pickup_batch.currency.err", repr(ex))
    batch = []
    for pid in pickup_ids:
        batch.append({"cc": 4, "cid": "", "ct": "wax", "id": pid, "r": "ok", "w": wax_total})
    for gid in wax_globals:
        batch.append({"cc": 4, "cid": "", "ct": "wax", "id": gid, "r": "ok", "w": wax_total})
    for gid in season_ids:
        batch.append({"cc": 1, "cid": "", "ct": "wax", "id": gid, "r": "ok", "w": wax_total})
    out_fr = []
    try:
        _fr = json.load(open("/wbsky/config/forge_rates.json", encoding="utf-8-sig"))
        _fr = _fr.get("forge_rates") or []
    except Exception:
        _fr = []
    for _i, _e in enumerate(_fr):
        _e = dict(_e) if isinstance(_e, dict) else {}
        if _i == 0:
            _e["reset_acknowledged"] = True
            _e["tier_completion"] = round(min(1.0, wax_total / float(cost)), 6)
        out_fr.append(_e)
    _social_log("collect_pickup_batch", {"user": me, "level_id": level_id,
                                        "pickups": len(pickup_ids),
                                        "global": len(global_ids),
                                        "wax_gain": wax_gain, "wax": wax_total,
                                        "forged": forged, "candles": candles})
    return jsonify({
        "batch_results": batch,
        "currency": snap,
        "level_id": level_id,
        "update_forge_rates": out_fr,
    })
@account_bp.route("/social_feed/get_user_feeds", methods=["POST"])
def get_user_feeds():
    return jsonify(_feed_response(request.get_json(force=True, silent=True) or {}, 20, "get_user_feeds"))
@account_bp.route("/social_feed/get_curated_feeds", methods=["POST"])
def get_curated_feeds():
    return jsonify(_feed_response(request.get_json(force=True, silent=True) or {}, 32, "get_curated_feeds"))
@account_bp.route("/get_season_quests", methods=["POST"])
def get_season_quests():
    cfg_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "season_quests.json")
    if not os.path.isfile(cfg_path):
        return jsonify({"error": "config not found"}), 404

    with open(cfg_path, encoding="utf-8") as f:
        data = json.load(f)

    today = datetime.utcnow().strftime("%Y-%m-%d")

    # ★ 2026-10-04：真机抓包（skyqw服 get_season_quests.json）与参考服务器的键名是
    #   `date`，不是 `data`。拼错会让季节向导的每日任务/兑换树判断失效。
    #   同时补齐参考实现里的字段：start_value=-1、activated/rewarded=false。
    if not isinstance(data.get("season_quests"), list) or not data.get("season_quests"):
        try:
            with open(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                   "config", "game_control.json"), encoding="utf-8-sig") as _f:
                gc = json.load(_f) or {}
        except Exception:
            gc = {}
        pool = (((gc.get("season_quests") or {}).get("daily_pool"))
                or (((gc.get("daily_quests") or {}).get("season") or {}).get("pool"))
                or [])
        sq, rewards = [], []
        for it in pool:
            if not isinstance(it, dict):
                continue
            qid = it.get("daily_quest_def_id")
            if not qid:
                continue
            sq.append({"date": today, "daily_quest_def_id": qid,
                       "stat_type": it.get("stat_type", ""),
                       "start_value": -1, "activated": False})
            rewards.append({"date": today, "daily_quest_def_id": qid,
                            "rewarded": False})
        data["season_quests"] = sq
        data["has_season_quests_rewards"] = rewards

    for key in ("season_quests", "has_season_quests_rewards"):
        for obj in data.get(key, []):
            if not isinstance(obj, dict):
                continue
            if "daily_quest_def_id" in obj:
                obj["date"] = today
                obj.pop("data", None)
                if key == "season_quests":
                    obj.setdefault("start_value", -1)
                    obj.setdefault("activated", False)
                else:
                    obj.setdefault("rewarded", False)

    return jsonify(data)

@account_bp.route("/activate_season_quest", methods=["POST"])
def activate_season_quests():
    """激活季节任务 —— 参考服务器回的就是 get_season_quests 的同一份负载。"""
    return get_season_quests()


@account_bp.route("/claim_season_quest_reward", methods=["POST"])
def claim_season_quest_reward():
    return jsonify({})

@account_bp.route("/social_feed/create", methods=["POST"])
def create_feed():
    return jsonify(_feed_create(request.get_json(force=True, silent=True) or {}))
@account_bp.route("/wing_buffs/collect", methods=["POST"])
def collect_wing_buffs():
    req = request.get_json(force=True, silent=True) or {}
    user = req.get("user")
    names = req.get("names", [])

    if not user or not isinstance(names, list):
        return jsonify({"error": "invalid payload"}), 400

    if not database.user_exists(user):
        return jsonify({"error": "user not found"}), 404

    raw_data = database.get_user_field(user, "wing_buffs")
    old = json.loads(raw_data) if raw_data else []

    new_buffs = list(set(old + names))
    database.set_user_field(user, "wing_buffs", json.dumps(new_buffs))

    update_list = [{"name": n, "collected": True} for n in names]
    return jsonify({
        "result": "ok",
        "update_wing_buffs": update_list
    })

@account_bp.route("/purchase_unlock", methods=["POST"])
def purchase_unlock():
    """购买/解锁一项（雕像形态 StatueForm、装扮、动作 …）。

    ★★★ 2026-10-04 重写：**原实现只是 `send_from_directory(all_purchase_unlock.json)`**
      —— 一个静态文件，永远不扣费、也永远不回 currency。
      客户端「买了东西但蜡烛没少」= 用户报的「蜡烛扣除机制失效」的根因之一。

    参考实现（win3/Sky/account/purchase_unlock/purchase_unlock.py）+ 真机抓包
    （skyqw服/static_responses/account%2Fpurchase_unlock.json）：
      请求：{user, session, name, type, ack, cost}   （cost 缺省 0）
      响应：{"currency": {完整 23 键},
             "result": "ok" | "already",
             "update_unlocks": [{"name","type","ack","created_at"}]}
      余额不足 → 400 {"result": "insufficient_currency"}
      参考服务器还带两条连带授予：SkyHubFirstArrival → HubStatueForm；
      披风类 → GainedFlame。
    """
    req = request.get_json(force=True, silent=True) or {}
    user = _req_user(req)
    name = req.get("name")
    typ = req.get("type") or "level"
    ack = bool(req.get("ack", False))
    cost = _int_or_none(req.get("cost")) or 0

    if not user or not name:
        return jsonify({"result": "invalid_request"}), 400
    if not database.user_exists(user):
        return jsonify({"result": "unauthorized"}), 401

    # ---- 已有上报过的解锁（users.unlocks 是客户端上报的 JSON 数组）----
    try:
        raw = database.get_user_field(user, "unlocks")
        owned = json.loads(raw) if raw else []
    except Exception:
        owned = []
    if not isinstance(owned, list):
        owned = []
    owned_names = {str(it.get("name")) for it in owned if isinstance(it, dict)}

    name = str(name)
    if name in owned_names:
        return jsonify({"currency": build_currency(user), "result": "already",
                        "update_unlocks": []})

    # ---- 扣费（这是"蜡烛扣不掉"的正主）----
    candles = _candle_balance(user)
    if cost > 0:
        if candles < cost:
            return jsonify({"currency": build_currency(user, candles),
                            "result": "insufficient_currency"}), 400
        candles -= cost
        _set_candles(user, candles)

    now = int(time.time())
    hidden = _hidden_unlocks()
    added = []

    def _grant(nm):
        if not nm or nm in owned_names or nm in hidden:
            return
        entry = {"name": nm, "type": typ, "ack": ack, "created_at": now}
        owned.append(entry)
        owned_names.add(nm)
        added.append(entry)

    _grant(name)
    # 参考服务器的连带授予
    if name == "SkyHubFirstArrival":
        _grant("HubStatueForm")
    if typ.lower() == "cape":
        _grant("GainedFlame")

    if added:
        try:
            database.set_user_field(user, "unlocks", json.dumps(owned))
        except Exception as _e:
            _social_log("purchase_unlock.save.err", repr(_e))

    return jsonify({"currency": build_currency(user, candles),
                    "result": "ok", "update_unlocks": added})

# ---------- 解锁确认接口 ----------
@account_bp.route("/ack_unlock", methods=["POST"])
def ack_unlock():
    """解锁确认接口 - 客户端调用此接口确认已收到解锁状态"""
    return jsonify({"result": "ok"})

# ---------- 缺失的 API 端点补充 ----------
# 以下端点在 resource_list 中声明但原代码未实现，补充空返回以避免 404

@account_bp.route("/get_map_defs", methods=["POST"])
def get_map_defs():
    return jsonify({"map_defs": []})


# ---------------------------------------------------------------------------
# ★ 2026-10-04：以下端点在 resource_list 里声明、客户端一定会来拉，但原来没有
#   实现 —— 落到 catch-all 只回 `{}`。**响应里缺少"同名成员"时客户端的资源解析
#   会当这次同步失败**（远端装扮 get_remote_outfit 那次就是同一类问题）。
#   这里按真机抓包（skyqw服/static_responses）的确切形状补上。
# ---------------------------------------------------------------------------
@account_bp.route("/get_questionnaires", methods=["POST"])
def get_questionnaires():
    return jsonify({"result": "ok"})


@account_bp.route("/get_questionnaire_groups", methods=["POST"])
def get_questionnaire_groups():
    return jsonify({"result": "ok"})


@account_bp.route("/ack_collectible", methods=["POST"])
def ack_collectible():
    return jsonify({"result": "ok"})


@account_bp.route("/redeem_reward", methods=["POST"])
def redeem_reward():
    return jsonify({"result": "ok"})


@account_bp.route("/remote_config/upload", methods=["POST"])
def remote_config_upload():
    return jsonify({"result": "ok"})


@account_bp.route("/purchase_spirit_shop_item", methods=["POST"])
def purchase_spirit_shop_item():
    """季节向导/先灵商店购买。

    ★ 2026-10-04：原来落到 catch-all 的 `{}` —— 客户端把"季节向导兑换树"
    点不动/不显示。参考服务器这个接口回 {result:"ok"} + 货币快照。
    """
    req = request.get_json(force=True, silent=True) or {}
    user = _req_user(req)
    if not user:
        return jsonify({"result": "ok"})
    return jsonify({"result": "ok", "currency": build_currency(user)})


@account_bp.route("/purchase_generic_shop_item", methods=["POST"])
def purchase_generic_shop_item():
    req = request.get_json(force=True, silent=True) or {}
    user = _req_user(req)
    if not user:
        return jsonify({"result": "ok"})
    return jsonify({"result": "ok", "currency": build_currency(user)})

@account_bp.route("/get_status_unlocks", methods=["POST"])
def get_status_unlocks():
    """获取状态解锁列表 - 包含所有内购和关卡解锁。

    ★ 2026-10-04：原来是 `send_from_directory` 直接发文件，没法按账号注入
      delete_unlocks（"柱子一直能去"需要它）。现在读文件 + 注入。
    """
    req = request.get_json(force=True, silent=True) or {}
    me = _req_user(req)
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        'config', 'all_unlock_status.json')
    try:
        with open(path, encoding='utf-8-sig') as f:
            data = json.load(f)
    except Exception as e:
        _social_log("get_status_unlocks.err", repr(e))
        data = {"status_unlocks": [], "status_unlocks_total_count": 0}
    hidden = _hidden_unlocks()
    if hidden and isinstance(data.get("status_unlocks"), list):
        data["status_unlocks"] = [u for u in data["status_unlocks"]
                                  if not (isinstance(u, dict) and u.get("name") in hidden)]
        data["status_unlocks_total_count"] = len(data["status_unlocks"])
    _del = _hub_reset_delete_list(me) if me else []
    if _del:
        data["delete_unlocks"] = _del
    return jsonify(data)

@account_bp.route("/get_free_gifts", methods=["POST"])
def get_free_gifts():
    return jsonify({"free_gifts": []})

@account_bp.route("/get_achievement_defs", methods=["POST"])
def get_achievement_defs():
    return jsonify({"achievement_defs": []})

@account_bp.route("/get_achievement_stats_tracking", methods=["POST"])
def get_achievement_stats_tracking():
    return jsonify({"achievement_stats_tracking": []})

@account_bp.route("/get_event_currency_defs", methods=["POST"])
def get_event_currency_defs():
    return jsonify({"event_currency_defs": []})

@account_bp.route("/get_consumable_defs", methods=["POST"])
def get_consumable_defs():
    return send_from_directory(
        directory=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config'),
        path='consumable_defs.json',
        mimetype='application/json'
    )

@account_bp.route("/get_infractions", methods=["POST"])
def get_infractions():
    return jsonify({"infractions": []})

# ---------- 聊天/消息相关端点补充 ----------
# 说明：光遇「同一张地图内」的实时聊天走的是 UDP（ENet）通道，
# 由 udp_relay_enet.py 负责转发，不经过这里。
# 下面这些 HTTP 端点只是客户端可能调用的兼容桩。
# 为了便于排查「聊天到底走的哪条路」，这里会把请求体写到 logs/chat.log。

def _log_chat(endpoint):
    try:
        log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
        os.makedirs(log_dir, exist_ok=True)
        body = request.get_json(force=True, silent=True)
        if body is None:
            body = request.get_data(as_text=True)
        with open(os.path.join(log_dir, "chat.log"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.utcnow().isoformat()}Z {endpoint} {request.host}\n")
            f.write(json.dumps(body, ensure_ascii=False)[:2000] + "\n")
            f.write("-" * 60 + "\n")
    except Exception:
        pass


@account_bp.route("/get_messages", methods=["POST"])
def get_messages():
    """获取消息列表 - 用于聊天历史同步"""
    _log_chat("get_messages")
    return jsonify({"messages": []})

@account_bp.route("/gift_messages/get", methods=["POST"])
def get_gift_messages():
    """获取礼物消息"""
    return jsonify({"gift_messages": []})

@account_bp.route("/message_chat/get", methods=["POST"])
def get_message_chat():
    """聊天消息获取端点"""
    _log_chat("message_chat/get")
    return jsonify({"message_chats": []})

@account_bp.route("/message_chat/send", methods=["POST"])
def send_message_chat():
    """聊天消息发送端点 - 实时聊天主要走 UDP，这里返回成功确认"""
    _log_chat("message_chat/send")
    return jsonify({"result": "ok"})

@account_bp.route("/chat/join", methods=["POST"])
def chat_join():
    """加入聊天室"""
    return jsonify({"result": "ok"})

@account_bp.route("/chat/leave", methods=["POST"])
def chat_leave():
    """离开聊天室"""
    return jsonify({"result": "ok"})

# ---------- 缺失接口补充（0.15.5 兼容） ----------

@account_bp.route("/get_outfit_defs", methods=["POST"])
def get_outfit_defs():
    """装扮定义目录（2.1MB）。

    ★★★ 2026-10-03 第二修：**服务端这两份表，客户端一份都不认**。
      客户端的装扮表是它**自带的 APK 资源** `assets/Data/Resources/OutfitDefs.json`
      （432 条 / `mesh` 是数组 / 键名 `color_hsv|pattern_hsv|tint_hsv|iconName|
       skipMotionBlur|putBackAnimSeq|disableBody|isSkyKid…`）；
      而 `config/outfit_defs.json` 是**另一套 schema**（`mesh` 是字符串、
      键名 `attribTex|diffuseTex|inCloset|base_hsv|icon_hsv|dyeable_primary…`）。
      判据：`.so` 里搜 `outfit_defs` / `get_outfit_defs` **都不存在**
      ⇒ 客户端**从不调用**本接口；`0x1042900~0x1042a84` 那一整片连续字段名
      **只对得上 APK 那份**，我们那 16 个独有键在 .so 里只命中 1 个。

      ⇒ 按用户建议，把**客户端自己那份 JSON 原样放到服务端**并在这里下发
        （`config/outfit_defs_client.json`，schema 与客户端完全一致）。
        这样即使将来某个版本真来读，也一定解析得动。
    """
    cfg_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config")
    client_path = os.path.join(cfg_dir, "outfit_defs_client.json")
    if os.path.isfile(client_path):
        try:
            with open(client_path, encoding="utf-8") as f:
                defs = json.load(f)
            return jsonify({"outfit_defs": defs})
        except Exception as e:
            _social_log("get_outfit_defs.client.err", repr(e))
    return send_from_directory(
        directory=cfg_dir,
        path='outfit_defs.json',
        mimetype='application/json'
    )

@account_bp.route("/get_relationship_defs", methods=["POST"])
def get_relationship_defs():
    """关系定义 - 客户端需要此接口加载社交关系树"""
    return send_from_directory(
        directory=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config'),
        path='relationship_abilities.json',
        mimetype='application/json'
    )

# 2026-10-05: the client also calls /account/get_star_tags (a DIFFERENT route
# from get_star_tag_defs). It had no handler, so it fell through to the
# catch-all and returned {} -- and the Home star chart is built from exactly
# this data: config/stars_config.json already has the two keys the reference
# returns (get_star_tag_defs + star_tag_links) and 141 defs, starting with the
# ancestor_* elder tags. Without it every ancestor on the star chart was dead.
@account_bp.route("/get_star_tags", methods=["POST"])
def get_star_tags():
    return send_from_directory(
        directory=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config'),
        path='stars_config.json',
        mimetype='application/json'
    )


@account_bp.route("/get_star_tag_defs", methods=["POST"])
def get_star_tag_defs():
    """星标定义"""
    return send_from_directory(
        directory=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config'),
        path='stars_config.json',
        mimetype='application/json'
    )

# ---------- 好友状态/在线相关端点 ----------

@account_bp.route("/set_friend_status", methods=["POST"])
def set_friend_status():
    """设置好友状态"""
    return jsonify({"result": "ok"})

@account_bp.route("/set_random", methods=["POST"])
def set_random():
    """随机匹配相关"""
    return jsonify({"set_random": []})

@account_bp.route("/hb", methods=["POST"])
def heartbeat():
    """心跳/轮询接口。

    ★ 2026-10-03 按参考服务器 thatskygame.de5.net 实测对齐。

      参考实现的 /account/hb 与 /account/find_previous_or_empty 返回
      **完全相同的结构**：

        {conn_queued, delay, level, level_hash, move_ts, new_cutoff,
         other_players[], private_uri, sig, signature, uri}

      实测要点：
        * sig == signature，均为 64 位 sha256 十六进制
        * uri == private_uri == "host:port"（客户端用它连游戏服务器）
        * conn_queued 是布尔 false；delay 是轮询秒数（参考为 30）
        * level / level_hash 为 0

      我上一版在这里塞了自己猜的字段（status/timestamp/server_time/
      current_game/pending_game/websocket_enabled）并且把 uri 留空 ——
      客户端拿不到联机地址，直接导致连不上。现在改为与参考一致。
    """
    body = request.get_json(force=True, silent=True) or {}
    return jsonify(_join_payload(body))

# ---------- 0.15.5 额外兼容端点 ----------

@account_bp.route("/get_outfit_wards", methods=["POST"])
def get_outfit_wards():
    return jsonify({"outfit_wards": []})

@account_bp.route("/get_traveling_spirits", methods=["POST"])
def get_traveling_spirits():
    return jsonify({"traveling_spirits": []})

@account_bp.route("/get_season_pass_defs", methods=["POST"])
def get_season_pass_defs():
    return jsonify({"season_pass_defs": []})

@account_bp.route("/get_quest_defs", methods=["POST"])
def get_quest_defs():
    return jsonify({"quest_defs": []})

@account_bp.route("/get_weekly_quests", methods=["POST"])
def get_weekly_quests():
    return jsonify({"weekly_quests": []})

@account_bp.route("/get_daily_quests", methods=["POST"])
def get_daily_quests():
    return jsonify({"daily_quests": []})

@account_bp.route("/get_currency_defs", methods=["POST"])
def get_currency_defs():
    return jsonify({"currency_defs": []})

@account_bp.route("/get_bundle_defs", methods=["POST"])
def get_bundle_defs():
    return jsonify({"bundle_defs": []})

@account_bp.route("/get_season_quests_v2", methods=["POST"])
def get_season_quests_v2():
    return jsonify({"season_quests": [], "has_season_quests_rewards": []})

@account_bp.route("/get_spirit_memory_defs", methods=["POST"])
def get_spirit_memory_defs():
    return jsonify({"spirit_memory_defs": []})

@account_bp.route("/get_event_currency", methods=["POST"])
def get_event_currency():
    return jsonify({"event_currency": []})

@account_bp.route("/get_social_card_defs", methods=["POST"])
def get_social_card_defs():
    return jsonify({"social_card_defs": []})

# ============================================================
# ★★ 2026-10-03 新增：社交链路（加好友邀请 / 给蜡烛 / 礼物消息）
#
# 为什么会有这一段：客户端 libBootloader.so 的字符串表里明确有这些接口
#   /account/give_candle      (keys: /recv_user /ability_id /ability_cost /pos /level_id)
#   /account/create_invite    (key : /invite_nickname)
#   /account/delete_invite    (key : /token_id)
#   /account/check_invite     ← 对方拉取"待接受邀请"，接受按钮就是它给的
#   /account/accept_invite
#   account/send_message      (keys: /target /gift_type)
#   account/claim_message_gift(key : /msg_id)
#   /account/get_pending_messages (回包: set_recvd_messages / set_sent_messages)
#   AccountGiftMessage 的字段: recvd / from_id / from_currency_type /
#                              from_currency_count / to_id / to_currency_type /
#                              to_currency_count / raw_message
# 这些接口我们**一个都没实现**，以前全被 catch-all 兜成 `{}`
# ⇒ 现象就是「给蜡烛之后，对方那一边永远不出现接受按钮」。
#
# 实现上做了两层保险：
#   ① 字段名兼容多种拼写（user/user_id/…、recv_user/target/…），万一我猜的键名和
#      客户端实际用的不完全一致也能吃下去；
#   ② config.json 打开 log_requests 后，logs/requests.log 会记下每个请求的原始 body，
#      真机跑一次就能把真实字段名核准、再收紧。
# ============================================================

_SOCIAL_LOG = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'logs', 'social.log')


def _social_log(tag, payload):
    """社交接口留痕（本身就是排障要用的，量很小，一直开着）。"""
    try:
        os.makedirs(os.path.dirname(_SOCIAL_LOG), exist_ok=True)
        with open(_SOCIAL_LOG, 'a', encoding='utf-8') as f:
            f.write("%s %s %s\n" % (datetime.utcnow().isoformat() + 'Z', tag,
                                    json.dumps(payload, ensure_ascii=False)[:2000]))
    except Exception:
        pass


def _pick_any(req, *names):
    """从请求体里按多个候选键名取第一个非空值。"""
    for n in names:
        if isinstance(req, dict) and n in req:
            v = req[n]
            if v not in (None, "", []):
                return v
    return None


def _req_user(req):
    return _pick_any(req, "user", "user_id", "account", "from_user", "sender", "id")


def _req_target(req):
    return _pick_any(req, "recv_user", "target", "to_user", "to_id", "friend",
                     "friend_id", "other_user")


def _now():
    return int(time.time())


def _load_cfg_safe():
    try:
        return load_config()
    except Exception:
        return {}


@account_bp.route("/create_invite", methods=["POST"])
def create_invite():
    """发起"加好友"邀请（给对方发一个待接受邀请 + 昵称）。"""
    req = request.get_json(force=True, silent=True) or {}
    _social_log("create_invite.req", req)
    me = _req_user(req)
    if not me:
        return jsonify({"result": "error", "error": "missing user",
                        "status": "error", "ok": False}), 400
    target = _req_target(req)
    nick = _pick_any(req, "invite_nickname", "nickname", "name", "friend_name") or ""
    # ★ 客户端没带名字时不要留空：邀请列表里会显示成空/uuid 前 8 位。
    #   随机取一个当"我给他起的名字"（接受之后就成为 friends.nickname）。
    if not str(nick).strip():
        try:
            import friend_name as _fn
            if _fn.enabled():
                nick = _fn.random_name()
        except Exception:
            nick = ""
    token = str(uuid.uuid4())
    try:
        database.execute(
            "INSERT INTO pending_invites (token_id, from_user, to_user, nickname, "
            "level_id, created_at, status) VALUES (?,?,?,?,?,?,?)",
            (token, me, target or "", str(nick)[:190],
             int(_pick_any(req, "level_id", "level") or 0), _now(), "pending"))
    except Exception as e:
        _social_log("create_invite.err", repr(e))
    resp = {"result": "ok", "status": "ok", "ok": True,
            "token_id": token, "token": token,
            "invite_nickname": str(nick)}
    _social_log("create_invite.resp", resp)
    return jsonify(resp)


@account_bp.route("/check_invite", methods=["POST"])
def check_invite():
    """查「这个邀请还在不在 / 有没有发给我的邀请」。

    ★★★ 2026-10-04 扫码加好友的**真正断点**在这里（实测数据库证据）：
      · 客户端 `create_invite` 只带 **`invite_nickname`**（一个昵称），
        根本没有目标 user id —— 所以我们建的 pending_invites 行
        `to_user` 全是空字符串（db 里 3 行都是 `to=`）。
      · 而本函数原来只按 `to_user = 我` 查 ⇒ **永远返回空**
        ⇒ 客户端永远不弹"接受"按钮 ⇒ 扫码加不上好友。
      · 客户端反汇编（/account/check_invite 描述符 0xFECC1B）：请求字段是
        **`token_id`**（扫到二维码后拿它来问），回包要
        **`invite_exists`** + **`invite_nickname`**（0xFECBD2）。
    现在两条路都支持：带 token_id 就精确查那一条；否则再按 to_user 兜。
    """
    req = request.get_json(force=True, silent=True) or {}
    me = _req_user(req)
    token = _pick_any(req, "token_id", "token", "invite_token_id", "code")
    _social_log("check_invite.req", {"user": me, "token": token})
    rows = []
    if token:
        try:
            row = database.query_one(
                "SELECT token_id, from_user, nickname, level_id, created_at, status "
                "FROM pending_invites WHERE token_id = ?", (token,))
            if row:
                rows = [row]
        except Exception as e:
            _social_log("check_invite.token.err", repr(e))
    if not rows and me:
        try:
            rows = database.query_all(
                "SELECT token_id, from_user, nickname, level_id, created_at FROM pending_invites "
                "WHERE to_user = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 50", (me,))
        except Exception as e:
            _social_log("check_invite.err", repr(e))

    invites = []
    for r in rows or []:
        invites.append({
            "token_id": r.get("token_id"),
            "token": r.get("token_id"),
            "invite_token_id": r.get("token_id"),
            "invite_exists": True,
            "from_user": r.get("from_user"),
            "user": r.get("from_user"),
            "invite_nickname": r.get("nickname", ""),
            "nickname": r.get("nickname", ""),
            "level_id": r.get("level_id", 0),
            "created_at": r.get("created_at", 0),
            "status": "pending",
        })
    resp = {"result": "ok", "status": "ok", "ok": True,
            "invite_exists": bool(invites),
            "invite_nickname": invites[0]["invite_nickname"] if invites else "",
            "invite_token_id": invites[0]["token_id"] if invites else "",
            "invite_count": len(invites),
            "invites": invites, "set_invites": invites, "pending_invites": invites}
    _social_log("check_invite.resp", {"count": len(invites), "exists": bool(invites)})
    return jsonify(resp)


@account_bp.route("/code/verify", methods=["POST"])
@account_bp.route("/account/code/verify", methods=["POST"])
def code_verify():
    """扫二维码后校验邀请码。

    ★ 2026-10-04 新增：客户端反汇编里这条是扫码链的必经一步
      （invite_qr_button / SocialInvitationScanner / ShowScanInviteDialog），
      我们原来**完全没有这个路由** ⇒ 落到 catch-all 的 `{}`
      ⇒ 扫码后客户端判定邀请无效 ⇒ 加不上好友。
    参考回包：{"result":"ok","type":"i","code":"<token_id>","invite_info":{…}}
    """
    req = request.get_json(force=True, silent=True) or {}
    code = _pick_any(req, "code", "token_id", "token", "invite_token_id")
    _social_log("code.verify.req", {"code": code})
    row = None
    if code:
        for cand in (code, str(code).replace("-", "").strip()):
            try:
                row = database.query_one(
                    "SELECT token_id, from_user, to_user, nickname, level_id, created_at "
                    "FROM pending_invites WHERE token_id = ?", (cand,))
            except Exception as e:
                _social_log("code.verify.err", repr(e))
                row = None
            if row:
                break
    if not row:
        return jsonify({"result": "error", "status": "error",
                        "error": "invite_not_found"}), 200
    return jsonify({
        "result": "ok", "status": "ok", "type": "i",
        "code": row.get("token_id"),
        "token_id": row.get("token_id"),
        "invite_token_id": row.get("token_id"),
        "invite_exists": True,
        "invite_nickname": row.get("nickname", ""),
        "invite_info": {
            "token_id": row.get("token_id"),
            "from_user": row.get("from_user"),
            "nickname": row.get("nickname", ""),
            "level_id": row.get("level_id", 0),
            "created_at": row.get("created_at", 0),
        },
    })


@account_bp.route("/accept_invite", methods=["POST"])
def accept_invite():
    """接受邀请 → 双方互为好友（写入 friends 表，get_friends 会读它）。"""
    req = request.get_json(force=True, silent=True) or {}
    _social_log("accept_invite.req", req)
    me = _req_user(req)
    token = _pick_any(req, "token_id", "token")
    inviter = _pick_any(req, "from_user", "sender")
    nick = _pick_any(req, "invite_nickname", "nickname") or ""

    mine_nick = nick
    if token and not inviter:
        try:
            row = database.query_one(
                "SELECT from_user, nickname FROM pending_invites WHERE token_id = ?", (token,))
            if row:
                inviter = row.get("from_user")
                if not mine_nick:
                    mine_nick = row.get("nickname", "")
        except Exception as e:
            _social_log("accept_invite.err", repr(e))

    if me and inviter:
        # ★★★ 2026-10-04：接受邀请必须写**四张关系**（friendships 双向 + friends 双向
        #   + 星座页），否则会出现「列表里有这个好友、但星座/好友树上没有」，
        #   表现为"加了但看不到"。直接复用 _ensure_friendship（它已经是双向落库）。
        try:
            _ensure_friendship(me, inviter)
        except Exception as e:
            _social_log("accept_invite.err2", repr(e))
    if token:
        try:
            database.execute("UPDATE pending_invites SET status = 'accepted' WHERE token_id = ?",
                             (token,))
        except Exception:
            pass
    if me and inviter:
        try:
            database.execute("UPDATE pending_invites SET status = 'accepted' "
                             "WHERE from_user = ? AND to_user = ?", (inviter, me))
        except Exception:
            pass
    # ★ 客户端反汇编（/account/accept_invite 描述符区 0xFECC31）：请求字段是 token_id；
    #   回包要带 invite_accepted / invite_result / invite_friend_id / update_friends，
    #   客户端据此立刻把好友挂上星座，不再等下一次 resync。
    resp = {
        "result": "ok", "status": "ok", "ok": True,
        "invite_accepted": True,
        "invite_result": "ok",
        "invite_friend_id": inviter or "",
        "friend": inviter or "",
        "update_friends": [_friend_entry(me, inviter)] if (me and inviter) else [],
    }
    _social_log("accept_invite.resp", resp)
    return jsonify(resp)


@account_bp.route("/delete_invite", methods=["POST"])
def delete_invite():
    req = request.get_json(force=True, silent=True) or {}
    _social_log("delete_invite.req", req)
    token = _pick_any(req, "token_id", "token")
    if token:
        try:
            database.execute("UPDATE pending_invites SET status = 'deleted' WHERE token_id = ?",
                             (token,))
        except Exception:
            pass
    return jsonify({"result": "ok", "status": "ok", "ok": True})


def _candle_balance(user):
    try:
        v = database.get_user_field(user, "candles")
        return int(v) if v is not None else 0
    except Exception:
        return 0


def _set_candles(user, value):
    try:
        database.set_user_field(user, "candles", max(0, int(value)))
    except Exception as e:
        _social_log("set_candles.err", repr(e))


def _deduct_candles(user, count):
    """真扣蜡烛。返回 (余额够不够, 扣后余额)。

    ★ 2026-10-03：用户反馈「给蜡烛不扣蜡烛」——以前这里**根本没有扣费**，
      再加上 /account/get_currency 每次都把蜡烛补回 all_user_candles，
      所以蜡烛数永远不变。现在给蜡烛按 ability_cost 实扣。
      config.json 的 "candles_enforce_cost" = true 时余额不够会拒绝，
      默认 false：照样扣、扣到 0 为止，不挡玩家加好友。
    """
    count = int(count or 0)
    cur = _candle_balance(user)
    if count <= 0 or not user:
        return True, cur
    enough = cur >= count
    try:
        enforce = bool(load_config().get("candles_enforce_cost", False))
    except Exception:
        enforce = False
    if enforce and not enough:
        return False, cur
    left = max(0, cur - count)
    _set_candles(user, left)
    _social_log("candles.deduct", {"user": user, "cost": count,
                                   "before": cur, "after": left, "enough": enough})
    return enough, left


# A brand-new friend gets ONLY the add-friend node (nickname /
# accept_addfriend / UiSocialAddFriend). Everything else in the 47-node tree
# (hug=26, hand_hold=2, chat=9, block=10, report=19, ...) must be unlocked with
# candles -- granting the whole set made the tree come up already finished.
STARTER_ABILITIES = [11, 1]


def _starter_set():
    """Starter ability ids for a new friendship. Overridable from config.json:
       "friend_starter_abilities": [11, 1]"""
    try:
        v = load_config().get("friend_starter_abilities")
        if isinstance(v, list) and v:
            return [int(x) for x in v]
    except Exception:
        pass
    return list(STARTER_ABILITIES)


def _ensure_friendship(a, b, ability_id=None, cost=0):
    """双向建立好友关系（幂等），并写上游那两张表。

    ★★★ 2026-10-03 重写：这是「递交蜡烛却加不上好友」的直接原因。
      真机实证（logs/social.log）：
        10:44:09  4739cc13 --give_candle/ability_id=1/cost=1--> e8086411
      只有**单向**一次，于是旧代码的 reciprocal 判定不成立 ⇒ friends 表一行都没写
      ⇒ 客户端 /account/get_friends 拿不到人 ⇒ 界面上就是"加不上"。
      正确做法（对齐上游 xysky 生产库）是一次给蜡烛就双向落库：
        · friendships            —— 关系本体（relationship_level / abilities / given / recvd）
        · friends                —— 兼容旧接口 /account/get_friends
        · friend_constellation_pages —— 星座页，决定好友在星座图上有没有星星
    """
    if not a or not b or a == b:
        return
    # 2026-10-04e: never invent a relation with an id that is not a real account.
    # vars["welcome"] (7b4f7a67-0b62-4868-bdec-5ac00fcac09d) and similar
    # non-player ids arrive as give_candle recipients / get_friends filters and
    # must not become "friends". Guarding here covers every call site
    # (get_friends, give_candle, accept_invite).
    try:
        if not database.query_one("SELECT id FROM users WHERE id = ?", (a,)):
            _social_log("ensure_friendship.skip", {"missing": a, "other": b})
            return
        if not database.query_one("SELECT id FROM users WHERE id = ?", (b,)):
            _social_log("ensure_friendship.skip", {"missing": b, "other": a})
            return
    except Exception as e:
        _social_log("ensure_friendship.guard.err", repr(e))
    now = _now()
    for u, f, g, r in ((a, b, 1, 0), (b, a, 0, 1)):
        # 1) 兼容表
        try:
            if not database.query_one(
                    "SELECT user_id FROM friends WHERE user_id = ? AND friend_id = ?",
                    (u, f)):
                nick = ""
                try:
                    nick = str(database.get_user_field(f, "nickname") or "")[:190]
                except Exception:
                    nick = ""
                # ★ 建关系时就给名字，别等到客户端来读（读的时候也会补，见
                #   db.get_or_assign_friend_nickname）。原来是空字符串 ——
                #   客户端第一次拉好友列表会看到 uuid 前 8 位。
                try:
                    if not nick:
                        nick = database.get_or_assign_friend_nickname(u, f, "")
                except Exception:
                    pass
                database.execute(
                    "INSERT INTO friends (user_id, friend_id, nickname, level, created_at) "
                    "VALUES (?,?,?,?,?)", (u, f, nick, 0, now))
        except Exception as e:
            _social_log("friendship.err", repr(e))
        # 2) 上游结构的 friendships。
        #    friend_full_unlock=true（默认）：两端直接满解锁（given=1/recvd=1 +
        #      abilities/hints 全表）。之前写成 (given=1,recvd=0)/(given=0,recvd=1)
        #      的不对称形态，客户端按 given 判"前置节点解锁没"，于是总有一侧显示
        #      未解锁（提示 relationship_unlock_required =「你必须先解锁前置节点」）。
        #    friend_full_unlock=false：正常玩法 —— 只解锁这次花蜡烛买的那个节点，
        #      关系等级 1，其余节点让玩家继续用蜡烛逐个解锁。
        try:
            _full = True
            try:
                _full = bool(load_config().get("friend_full_unlock", True))
            except Exception:
                _full = True
            if _full:
                database.upsert_friendship(u, f, ability_id=ability_id,
                                           relationship_level=database.FULL_RELATIONSHIP_LEVEL,
                                           given=1, recvd=1, full_unlock=True)
            else:
                database.upsert_friendship(u, f, ability_id=ability_id,
                                           relationship_level=1,
                                           given=1, recvd=1, full_unlock=False)
                # 2026-10-04c: a brand-new friend must start from the authority
                # starter set -- level 1, abilities [19,6,9,2,27,1,10], no hints.
                # While the tree came back fully unlocked the client treated the
                # pair as already-friends, so the "add friend" flow never began.
                try:
                    _lvl0 = 1
                    try:
                        _lvl0 = int(load_config().get("friend_starter_level", 1))
                    except Exception:
                        _lvl0 = 1
                    database.execute(
                        "UPDATE friendships SET relationship_level=?, "
                        "abilities=?, hints=? WHERE user_id=? AND friend_id=?",
                        (_lvl0, json.dumps(_starter_set()), json.dumps([]), u, f))
                except Exception as e:
                    _social_log("friendship.starter.err", repr(e))
        except Exception as e:
            _social_log("friendship.upsert.err", repr(e))
        # 3) 星座页
        try:
            database.add_to_constellation(u, f)
        except Exception as e:
            _social_log("friendship.constellation.err", repr(e))


@account_bp.route("/give_candle", methods=["POST"])
def give_candle():
    """给蜡烛 / 送心火（好友树上「递蜡烛」、点亮星星走这里）。

    ★★★ 2026-10-06 按官方抓包重写（static_responses/account__give_candle.json）：
      · 回包 = {res:"ok", result:true, currency, set_friend_count,
                set_sent_messages:[发出侧…], update_friends:[…],
                update_online_friends:[{friend_id, level_id}]}
      · **礼物必须保持 claimed=0**：老代码插完立刻
        `UPDATE gift_messages SET claimed=1 WHERE from_user=? AND to_user=?`，
        于是对方 get_pending_messages（只查 claimed=0）永远查不到，
        客户端也从不调用 claim_message_gift（线上实测 0 次）
        ⇒ 用户报的「好友送心收不到」就是这么来的。
      · msg_id 改**整数**（官方 13169326930 这种）。
      · 免费(cost<=0) => gift_heart_wax：收件人得 heart_wax_per_light 个心火；
        付费(cost>0)  => gift：收件人得 cost 根蜡烛。
      · no_gift=true（UDP 侧"靠近即建好友"用）只建关系、不发礼物。
    """
    req = request.get_json(force=True, silent=True) or {}
    _social_log("give_candle.req", req)
    cfg = load_config()
    me = _req_user(req)
    target = _req_target(req)
    cost = _int_or_none(_pick_any(req, "ability_cost", "cost", "count", "currency_count")) or 0
    ability = _int_or_none(_pick_any(req, "ability_id", "ability")) or 0
    no_gift = bool(req.get("no_gift"))
    msg_id = _new_gift_id()

    enough, left = (True, _candle_balance(me)) if not me else _deduct_candles(me, cost)

    is_wax = cost <= 0
    g_type = "gift_heart_wax" if is_wax else "gift"
    c_type = "heart_wax" if is_wax else "candles"
    try:
        c_count = int(cfg.get("heart_wax_per_light", 5) or 5) if is_wax else cost
    except (TypeError, ValueError):
        c_count = 5 if is_wax else cost

    if me and target and not no_gift:
        try:
            database.execute(
                "INSERT INTO gift_messages (msg_id, from_user, to_user, gift_type, "
                "currency_type, currency_count, raw_message, sent_at, claimed) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (str(msg_id), me, target, g_type, c_type, c_count, str(ability)[:250], _now(), 0))
        except Exception as e:
            _social_log("give_candle.err", repr(e))

    if me and target:
        _ensure_friendship(me, target, ability_id=ability, cost=cost)

    fr_row = {}
    if me and target:
        try:
            fr_row = database.get_friendship(me, target) or {}
        except Exception as e:
            _social_log("give_candle.readback.err", repr(e))
    try:
        fr_abilities = json.loads(fr_row.get("abilities") or "[]")
    except Exception:
        fr_abilities = []
    try:
        fr_level = int(fr_row.get("relationship_level") or 0)
    except (TypeError, ValueError):
        fr_level = 0

    def _i0(v):
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    upd_one = None
    if me and target:
        upd_one = {
            "friend_id": target, "user_id": target,
            "abilities": fr_abilities, "hints": fr_abilities,
            "given": _i0(fr_row.get("given")), "recvd": _i0(fr_row.get("recvd")),
            "level": fr_level, "last_seen_outfit": None,
            "soft_deleted": None, "they_soft_deleted": None, "unlinked_star_sku": "",
            "when_created": fr_row.get("when_created") or fr_row.get("created_at") or _now(),
        }

    sent = [_gift_entry(r, False) for r in _gift_sent_rows(me)] if me else []
    set_friends = {}
    friend_status = None
    if me and target:
        try:
            friend_status = _friend_entry(me, target)
            if friend_status:
                set_friends = {str(target): friend_status}
        except Exception:
            set_friends = {}
    try:
        set_friend_count = len(database.query_all(
            "SELECT 1 FROM friendships WHERE user_id = ?", (me,)) or [])
    except Exception:
        set_friend_count = len(set_friends)

    friend_level = 0
    try:
        from udp_rooms import level_of_user as _lvl_of_user
        friend_level = int(_lvl_of_user(target, 0) or 0)
    except Exception:
        friend_level = 0

    resp = {
        "res": "ok", "result": True, "status": "ok", "ok": True,
        "msg_id": msg_id, "gift_id": msg_id,
        "currency": (build_currency(me, left) if me else {}),
        "set_friend_count": set_friend_count,
        "set_sent_messages": sent,
        "update_friends": ([upd_one] if upd_one else []),
        "update_online_friends": ([{"friend_id": target, "level_id": friend_level}] if target else []),
        "ability_id": ability, "ability_cost": cost,
        "friend_id": target or "", "target": target or "",
        "candles": left, "enough_candles": bool(enough), "result_bool": True,
        "set_friends": set_friends,
        "update_friend_statues": ([friend_status] if friend_status else []),
        "friendship": {"user": me or "", "friend": target or "",
                       "relationship_level": fr_level, "abilities": fr_abilities,
                       "given": _i0(fr_row.get("given")), "recvd": _i0(fr_row.get("recvd"))},
        "relationship": {"user": target or "", "friend": target or "", "ability_id": ability,
                         "relationship_level": fr_level, "unlocked": True},
    }
    _social_log("give_candle.resp", {"result": True, "ability_cost": cost, "gift_type": g_type,
                                     "currency": c_type, "count": c_count, "candles": left,
                                     "no_gift": no_gift, "sent_n": len(sent)})
    return jsonify(resp)

@account_bp.route("/send_message", methods=["POST"])
def send_message():
    """送礼消息（客户端键 /target /gift_type）。

    ★ 2026-10-06 按官方抓包（account__send_message.json）重写：
      回包 = {currency, result:"ok", sent_message:{msg_id,gift_id,type,to_id,
              currency_type,currency_count,recvd:false}}
      原来只回 {result,status,ok,msg_id} —— 客户端拿不到 sent_message、
      礼物状态不落地（线上 449 次调用反复重发）。
    """
    req = request.get_json(force=True, silent=True) or {}
    _social_log("send_message.req", req)
    cfg = load_config()
    me = _req_user(req)
    target = _req_target(req)
    raw_gt = str(_pick_any(req, "gift_type", "gift") or "gift_heart_wax")
    count = _int_or_none(_pick_any(req, "currency_count", "count", "gift_count")) or 0
    is_wax = ("wax" in raw_gt.lower()) or raw_gt in ("0", "")
    if is_wax:
        g_type, c_type = "gift_heart_wax", "heart_wax"
        try:
            c_count = int(cfg.get("heart_wax_per_light", 5) or 5)
        except (TypeError, ValueError):
            c_count = 5
        cost = 0
    else:
        g_type, c_type = "gift", "candles"
        try:
            default_cost = int(cfg.get("heart_gift_candle_cost", 3) or 3)
        except (TypeError, ValueError):
            default_cost = 3
        c_count = count or default_cost
        cost = c_count
    msg_id = _new_gift_id()
    enough, left = (True, _candle_balance(me)) if not me else _deduct_candles(me, cost)
    row = {"msg_id": msg_id, "from_user": me, "to_user": target, "gift_type": g_type,
           "currency_type": c_type, "currency_count": c_count, "raw_message": "",
           "sent_at": _now(), "claimed": 0}
    if me and target:
        try:
            database.execute(
                "INSERT INTO gift_messages (msg_id, from_user, to_user, gift_type, "
                "currency_type, currency_count, raw_message, sent_at, claimed) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (str(msg_id), me, target, g_type, c_type, c_count,
                 str(_pick_any(req, "raw_message", "text", "message") or "")[:250], _now(), 0))
            _social_log("send_message.ok", {"msg_id": msg_id, "to": target, "raw": raw_gt,
                                            "type": g_type, "count": c_count, "cost": cost})
        except Exception as e:
            _social_log("send_message.err", repr(e))
    return jsonify({
        "currency": (build_currency(me, left) if me else {}),
        "result": "ok", "status": "ok", "ok": True,
        "msg_id": msg_id, "gift_id": msg_id,
        "sent_message": _gift_entry(row, False),
    })

def _new_gift_id():
    """礼物 id：官方是**大整数**（如 13169326930），不是 uuid 字符串。

    客户端按整数解析 msg_id/gift_id；给字符串会让 AccountGiftMessage 解析失败，
    表现就是"送出去了但对方收不到 / 接受按钮不出来"。
    """
    base = int(time.time() * 1000) * 1000
    try:
        return base + int(secrets.randbelow(1000))
    except Exception:
        try:
            return base + (int.from_bytes(secrets.token_bytes(2), "big") % 1000)
        except Exception:
            return base


def _dedup_by_fid(seq):
    """按 friend_id 去重（保留第一次出现）。friends 表里同一好友可能有重复行。"""
    out, seen = [], set()
    for it in seq or []:
        try:
            k = str((it or {}).get("friend_id") or "")
        except Exception:
            k = ""
        if k and k in seen:
            continue
        if k:
            seen.add(k)
        out.append(it)
    return out


# ---------------------------------------------------------------------------
# p74 (2026-10-06): 礼物消息「出站形状」归一化
#   事故：收礼的一方闪退，之后进不去游戏。
#   原因：gift_messages 里 13684 行老数据 msg_id 是 uuid 字符串、gift_type 是
#         能力 id；客户端 AccountGiftMessage 按 int64 解析 msg_id/gift_id、
#         按官方枚举解析 type，解析不了就挂，登录时又下发同一份 payload。
# ---------------------------------------------------------------------------
_GIFT_TYPES_OK = ("gift_heart_wax", "gift", "gift_wax", "gift_season_pass")


def _gift_int_id(v):
    """任意 msg_id -> 正 int64；uuid 用 md5 确定性折叠（同一 uuid 恒定同一 id）。"""
    if isinstance(v, bool):
        v = int(v)
    if isinstance(v, int) and v > 0:
        return v
    s = str(v or "").strip()
    if s.isdigit() and int(s) > 0:
        return int(s)
    try:
        h = int(hashlib.md5(s.encode("utf-8")).hexdigest()[:14], 16)
    except Exception:
        # 兜底：不依赖 hashlib，也必须是「一个字符串一个值」，
        # 否则所有 uuid 会折叠成同一个 id -> 领错礼物。
        h = 0
        for ch in s:
            h = (h * 131 + ord(ch)) & 0xFFFFFFFFFFFF
    return 1000000000000 + (h % 8000000000000)


def _gift_currency_out(t, ct, cc):
    """currency_type 为空的老数据按 type 补全（gift_heart_wax => heart_wax）。"""
    ct = (ct or "").strip()
    if ct:
        return ct
    if not cc:
        return ""
    return {"gift_heart_wax": "heart_wax", "gift_wax": "wax",
            "gift": "candles"}.get(t, ct)


def _gift_type_norm(r):
    """gift_messages.gift_type -> 官方 gift type 字符串（非法值按货币推断）。"""
    v = r.get("gift_type")
    t = v.strip() if isinstance(v, str) else ""
    if not t:
        try:
            t = "gift" if int(v or 0) else "gift_heart_wax"
        except (TypeError, ValueError):
            t = "gift_heart_wax"
    t = t.strip().lower()
    if t in _GIFT_TYPES_OK:
        return t
    ct = _gift_currency_str(r)
    return "gift_heart_wax" if ct == "heart_wax" else "gift"


def _gift_type_str(r):
    """gift_messages.gift_type -> 客户端字符串（兼容老数据的整数/空值）。"""
    v = r.get("gift_type")
    if isinstance(v, str) and v.strip():
        return v.strip()
    try:
        iv = int(v or 0)
    except (TypeError, ValueError):
        iv = 0
    return "gift" if iv else "gift_heart_wax"


def _gift_currency_str(r):
    """gift_messages.currency_type -> 客户端字符串（老数据里是整数 0/1）。"""
    v = r.get("currency_type")
    if isinstance(v, str):
        s = v.strip()
        if s.isdigit():
            return "" if s == "0" else ("heart_wax" if s == "1" else s)
        return s
    try:
        iv = int(v or 0)
    except (TypeError, ValueError):
        iv = 0
    return "" if iv == 0 else ("heart_wax" if iv == 1 else "")


def _gift_cols():
    return ("msg_id, from_user, to_user, gift_type, currency_type, "
            "currency_count, raw_message, sent_at, claimed")


def _gift_sent_rows(user_id, limit=40):
    try:
        return database.query_all(
            "SELECT " + _gift_cols() + " FROM gift_messages WHERE from_user = ? "
            "ORDER BY sent_at DESC LIMIT %d" % int(limit), (user_id,)) or []
    except Exception as e:
        _social_log("gift.sent_rows.err", repr(e))
        return []


def _gift_recvd_rows(user_id, limit=100):
    try:
        return database.query_all(
            "SELECT " + _gift_cols() + " FROM gift_messages "
            "WHERE to_user = ? AND claimed = 0 ORDER BY sent_at DESC LIMIT %d" % int(limit),
            (user_id,)) or []
    except Exception as e:
        _social_log("gift.recvd_rows.err", repr(e))
        return []


def _grant_gift_currency(user, ct, count):
    """按礼物的 currency_type 把货币发给领取人（heart_wax 进 heart_wax，candles 进蜡烛）。"""
    try:
        count = int(count or 0)
    except (TypeError, ValueError):
        count = 0
    if not user or count <= 0:
        return (ct or "", 0)
    c = (ct or "").strip()
    col = {"heart_wax": "heart_wax", "candles": "candles", "wax": "wax"}.get(c, "candles")
    try:
        database.insert_ignore("INSERT IGNORE INTO currency (user_id, updated_at) VALUES (?,?)",
                               (user, _now()))
        if col == "candles":
            database.set_user_field(user, "candles", _candle_balance(user) + count)
        else:
            database.execute("UPDATE currency SET %s = COALESCE(%s,0) + ?, updated_at = ? "
                             "WHERE user_id = ?" % (col, col), (count, _now(), user))
    except Exception as e:
        _social_log("gift.grant.err", {"user": user, "col": col, "count": count, "err": repr(e)})
        return (c, 0)
    _social_log("gift.grant", {"user": user, "col": col, "count": count})
    return (c, count)


def _gift_entry(r, recvd):
    """gift_messages 一行 -> 客户端 AccountGiftMessage（严格照官方抓包字段集）。

    收到侧: {msg_id, gift_id, type, from_id, currency_type, currency_count, raw_message}
    发出侧: {msg_id, gift_id, type, to_id, recvd:false, currency_type:"", currency_count:0}
            （发出侧那份货币是**收件人**的，官方一律给 ""/0）
    p74：msg_id/gift_id 一律 int64（uuid 折叠），type 一律官方字符串，
         currency_type 为空的老数据按 type 补全。
    """
    t = _gift_type_norm(r)
    ct = _gift_currency_str(r)
    try:
        cc = int(r.get("currency_count") or 0)
    except (TypeError, ValueError):
        cc = 0
    ct_out = _gift_currency_out(t, ct, cc)
    gid = _gift_int_id(r.get("msg_id"))
    out = {"msg_id": gid, "gift_id": gid, "type": t, "gift_type": t,
           "raw_message": r.get("raw_message") or None}
    if recvd:
        out["from_id"] = r.get("from_user")
        out["currency_type"] = ct_out
        out["currency_count"] = cc
    else:
        out["to_id"] = r.get("to_user")
        out["recvd"] = False
        out["currency_type"] = ""
        out["currency_count"] = 0
    out.update({
        "from_user": r.get("from_user"), "to_user": r.get("to_user"),
        "from_currency_type": ct_out, "from_currency_count": cc,
        "to_currency_type": ct_out, "to_currency_count": cc,
        "types": [{"type": t, "count": cc, "currency_type": ct_out, "currency_count": cc}],
        "type_count": 1,
        "sent_at": int(r.get("sent_at") or 0),
        "claimed": int(r.get("claimed") or 0),
    })
    return out

@account_bp.route("/claim_message_gift", methods=["POST"])
def claim_message_gift():
    """领取礼物（键 /msg_id）：标记已领 + 按 currency_type 把货币发给领取人。

    ★ 2026-10-06 按官方抓包（account__claim_message_gift.json）重写：
      回包 = {currency, get_buffs:[], get_buffs_sign:<64hex>, result:"ok",
              set_app_badge_number:N, set_recvd_messages:[剩余待领…]}
      ★ 老代码把**所有**礼物都加进 candles；官方 gift_heart_wax 的
        currency_type 是 "heart_wax"、count 5 ⇒ 必须进 heart_wax。
    """
    req = request.get_json(force=True, silent=True) or {}
    _social_log("claim_message_gift.req", req)
    me = _req_user(req)
    msg_id = _pick_any(req, "msg_id", "id", "gift_id")
    granted = ("", 0)
    row = None
    if msg_id not in (None, ""):
        # p74：客户端拿到的 msg_id 一定是 int（uuid 已被 _gift_int_id 折叠），
        # 而老库里存的还是 uuid 字符串 => 先按原样查、再按折叠后的整数查，
        # 都不中就把该用户未领取的礼物扫一遍按折叠值比对。
        want = _gift_int_id(msg_id)
        for key in (str(msg_id), str(want)):
            try:
                row = database.query_one(
                    "SELECT " + _gift_cols() + " FROM gift_messages WHERE msg_id = ?", (key,))
            except Exception as e:
                _social_log("claim_message_gift.err", repr(e))
                row = None
            if row:
                break
        if not row and me:
            for cand in _gift_recvd_rows(me, 200):
                if _gift_int_id(cand.get("msg_id")) == want:
                    row = cand
                    break
    if row and not (_int_or_none(row.get("claimed")) or 0):
        try:
            database.execute("UPDATE gift_messages SET claimed = 1 WHERE msg_id = ?",
                             (str(row.get("msg_id")),))
            if not me or str(row.get("to_user")) == str(me):
                # p74：老数据 currency_type 可能是空的（gift_heart_wax + ""），
                # 旧代码会当成 candles 发蜡烛 => 收心变成收蜡烛。
                _cc = _int_or_none(row.get("currency_count")) or 0
                granted = _grant_gift_currency(
                    me or row.get("to_user"),
                    _gift_currency_out(_gift_type_norm(row), _gift_currency_str(row), _cc),
                    _cc)
        except Exception as e:
            _social_log("claim_message_gift.err2", repr(e))
    recvd = [_gift_entry(r, True) for r in _gift_recvd_rows(me)] if me else []
    try:
        sign = hashlib.sha256(("gift|%s|%d" % (me or "", _now())).encode("utf-8")).hexdigest()
    except Exception:
        sign = ""
    resp = {
        "currency": (build_currency(me) if me else {}),
        "get_buffs": [], "get_buffs_sign": sign,
        "result": "ok", "status": "ok", "ok": True,
        "set_app_badge_number": len(recvd),
        "set_recvd_messages": recvd,
        "msg_id": msg_id, "claimed_currency": granted[0], "claimed_count": granted[1],
    }
    _social_log("claim_message_gift.resp", {"user": me, "msg_id": msg_id,
                                            "granted": granted, "left": len(recvd)})
    return jsonify(resp)

@account_bp.route("/", defaults={"path": ""}, methods=["POST", "GET"])
@account_bp.route("/<path:path>", methods=["POST", "GET"])
def account_catch_all(path):
    """兜底路由：未实现的 /account/ 接口返回空 JSON，避免客户端按钮消失"""
    return jsonify({}), 200

# ===========================================================================
# 2026-10-04: endpoints the client really calls but we never implemented
# (they used to fall into the catch-all and return `{}`).
# ===========================================================================
@account_bp.route("/ack_forge_rate_reset", methods=["POST"])
def ack_forge_rate_reset():
    """Forge-rate reset acknowledgement.

    Reference (win3/account/ack_forge_rate_reset) answers
    {"result":"ok","update_forge_rates":[{wax tier 26 / level 4 / count 5}]}.
    We used to fall into the catch-all, so the response had no
    `update_forge_rates` member at all (the client retried 27 times).
    """
    return jsonify({
        "result": "ok",
        "update_forge_rates": [{
            "source_type": "wax",
            "current_tier": 26,
            "current_level": 4,
            "tier_completion": 0.0,
            "level_completion": 0.0,
            "level_tier_index": 0,
            "level_tier_count": 5,
            "reset_acknowledged": True,
            "enabled": True,
        }],
    })


@account_bp.route("/consumable/get_consumable_defs", methods=["POST"])
def get_consumable_defs_sub():
    """The `consumable/`-prefixed variant. It used to answer
    {"get_consumable_defs": []} -- an empty definition table means the client
    cannot resolve any magic id (icon/name), which shows up as a broken/blank
    magic menu. Reference serves the real table (official: 507 entries, ~86KB)."""
    return send_from_directory(
        directory=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config'),
        path='consumable_defs.json',
        mimetype='application/json'
    )


@account_bp.route("/translate", methods=["POST"])
def translate_text():
    return jsonify({"result": "ok", "translated": "", "text": ""})


@account_bp.route("/get_buff_defs", methods=["POST"])
def get_buff_defs_root():
    """顶层 /account/get_buff_defs（与 /account/buff/get_buff_defs 同义）。
    原来没有 handler -> {} -> 客户端解析不到任何 buff 定义。"""
    return send_from_directory(
        directory=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config'),
        path='buff_defs.json',
        mimetype='application/json'
    )


# 2026-10-05: 之前用"从装饰器到下一个装饰器"的正则替换多装饰器路由块，两次都只吃掉了
# 第一个装饰器行、留下重名函数（服务两次 spawn error）。改为**追加到文件末尾**，
# Flask 是按 rule 匹配的，与注册顺序无关。
@account_bp.route("/buff/get_buffs", methods=["POST"])
@account_bp.route("/buff/get_buffs1", methods=["POST"])
def get_buffs1():
    """真实生效的 buff 列表 + 签名（原来恒回 []，签名还是写死的）。"""
    req = request.get_json(force=True, silent=True) or {}
    me = req.get("user") or req.get("user_id")
    buffs, sign = _buffs_payload(me)
    return jsonify({"get_buffs": buffs, "get_buffs_sign": sign})


@account_bp.route("/consumable/reset_timers", methods=["POST"])
def consumable_reset_timers():
    """客户端字符串表里有 /account/consumable/reset_timers。
    清掉所有魔法冷却并回整包背包（形状照 consume_item1 的 get_consumables）。"""
    req = request.get_json(force=True, silent=True) or {}
    me = req.get("user") or req.get("user_id")
    try:
        prefs = _get_prefs(me) or {}
        inv = prefs.get("consumables")
        if isinstance(inv, dict):
            for k in list(inv.keys()):
                inv[k] = int(inv[k] or 0)
        _set_prefs(me, {"consumables": inv if isinstance(inv, dict) else {}})
        _set_prefs(me, {"buffs": {}})
    except Exception as e:
        _social_log("consumable_reset_timers.err", repr(e))
    bag = []
    try:
        bag = _consumables_inventory(me)
    except Exception:
        bag = []
    return jsonify({"result": "ok", "get_consumables": bag})
# ============ social feed (留言小船/纸船/留言蜡烛/留言灯笼) MySQL 版 2026-10-05 ============
# 客户端 0.15.5 字符串表要求: 响应必须是 {"feeds":[...]}, 每条含
#   extra_data / reactions / create_at(毫秒) / social_feed_id / message / pool_* / user_id ...
# 存储改走 MySQL 表 social_feed_post(原来存 /wbsky/config/social_feeds.json) —— DB 不可用时
# 自动回退到那个 JSON 文件, 保证功能不会因为表建不出来而挂掉。
import hashlib
import io
import json
import os
import threading
import time
import uuid

_FEED_PATH = "/wbsky/config/social_feeds.json"
_FEED_LOG = "/wbsky/logs/social_feed.log"
_FEED_TABLE = "social_feed_post"
_FEED_MAX = 5000

_feed_ok = [False]
_feed_ddl_lock = threading.Lock()
_feed_file_lock = threading.Lock()

_FEED_DDL = (
    "CREATE TABLE IF NOT EXISTS social_feed_post ("
    "id BIGINT NOT NULL AUTO_INCREMENT,"
    "social_feed_id VARCHAR(96) NOT NULL DEFAULT '',"
    "user_id VARCHAR(64) NOT NULL DEFAULT '',"
    "pool_type VARCHAR(64) NOT NULL DEFAULT '',"
    "pool_name VARCHAR(96) NOT NULL DEFAULT '',"
    "message TEXT,"
    "extra_data TEXT,"
    "tags TEXT,"
    "comments_enabled TINYINT NOT NULL DEFAULT 1,"
    "is_private TINYINT NOT NULL DEFAULT 0,"
    "created_at BIGINT NOT NULL DEFAULT 0,"
    "expire_at BIGINT NOT NULL DEFAULT 0,"
    "likes_count INT NOT NULL DEFAULT 0,"
    "hide_count INT NOT NULL DEFAULT 0,"
    "report_count INT NOT NULL DEFAULT 0,"
    "is_poisoned TINYINT NOT NULL DEFAULT 0,"
    "PRIMARY KEY (id),"
    "UNIQUE KEY uniq_social_feed_id (social_feed_id),"
    "KEY idx_social_feed_pool (pool_type, pool_name, created_at)"
    ") DEFAULT CHARSET=utf8mb4"
)

_FEED_DEFAULT_EXTRA = {
    "chunk_count": 0,
    "ori": "(0,0,0,1)",
    "pos": "(-10,0,-10)",
    "prompt_used": "",
    "resource_id": "",
    "scale": "(1,1,1)",
    "sub_type": 0,
}

_FEED_ZERO_REACTIONS = {
    "is_poisoned": False,
    "total_comment_count": 0,
    "total_hide_count": 0,
    "total_like_count": 0,
    "total_like_reward_message_id": 0,
    "total_report_count": 0,
    "total_reported_comment_count": 0,
    "total_super_like_count": 0,
}


def _feed_log(tag, msg):
    try:
        with io.open(_FEED_LOG, "a", encoding="utf-8") as f:
            f.write("%s | %s | %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), tag, msg))
    except Exception:
        pass


def _feed_ensure():
    """建表(幂等)。失败则整块功能退回 JSON 文件。"""
    if _feed_ok[0]:
        return True
    with _feed_ddl_lock:
        if _feed_ok[0]:
            return True
        try:
            database.execute(_FEED_DDL, ())
            _feed_ok[0] = True
            _feed_log("ddl", "social_feed_post ready")
        except Exception as e:
            _feed_log("ddl.err", repr(e))
    return _feed_ok[0]


# ---------------- JSON 文件回退 ----------------

def _feed_file_load():
    try:
        with io.open(_FEED_PATH, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return []
    if isinstance(d, dict):
        d = d.get("feeds")
    return d if isinstance(d, list) else []


def _feed_file_save(items):
    tmp = _FEED_PATH + ".tmp"
    with _feed_file_lock:
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump({"feeds": items[-_FEED_MAX:]}, f, ensure_ascii=False)
        os.replace(tmp, _FEED_PATH)


# ---------------- 记录 <-> 行 ----------------

def _row_to_rec(r):
    ex = r.get("extra_data")
    if isinstance(ex, str) and ex:
        try:
            ex = json.loads(ex)
        except Exception:
            ex = {}
    if not isinstance(ex, dict):
        ex = {}
    tg = r.get("tags")
    if isinstance(tg, str) and tg:
        try:
            tg = json.loads(tg)
        except Exception:
            tg = None
    return {
        "social_feed_id": r.get("social_feed_id") or "",
        "user_id": r.get("user_id") or "",
        "pool_type": r.get("pool_type") or "",
        "pool_name": r.get("pool_name") or "",
        "message": r.get("message") or "",
        "create_at": int(r.get("created_at") or 0),
        "expire_at": int(r.get("expire_at") or 0),
        "extra_data": ex,
        "tags": tg,
        "comments_enabled": bool(r.get("comments_enabled", 1)),
        "reactions": {
            "is_poisoned": bool(r.get("is_poisoned") or 0),
            "total_like_count": int(r.get("likes_count") or 0),
            "total_hide_count": int(r.get("hide_count") or 0),
            "total_report_count": int(r.get("report_count") or 0),
        },
    }


def _feed_load():
    """全部未过期 feed(最新在前)。DB 优先, 失败退回文件。"""
    if _feed_ensure():
        try:
            rows = database.query_all(
                "SELECT * FROM social_feed_post ORDER BY created_at DESC LIMIT 2000") or []
            return [_row_to_rec(r) for r in rows]
        except Exception as e:
            _feed_log("db.load.err", repr(e))
    return _feed_file_load()


def _feed_insert(rec):
    if _feed_ensure():
        try:
            database.execute(
                "INSERT INTO social_feed_post (social_feed_id, user_id, pool_type, pool_name,"
                " message, extra_data, tags, comments_enabled, is_private, created_at, expire_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (rec["social_feed_id"], rec["user_id"], rec["pool_type"], rec["pool_name"],
                 rec["message"], json.dumps(rec.get("extra_data") or {}, ensure_ascii=False),
                 json.dumps(rec.get("tags"), ensure_ascii=False) if rec.get("tags") is not None else None,
                 1 if rec.get("comments_enabled", True) else 0,
                 1 if rec.get("private") else 0,
                 int(rec.get("create_at") or 0), int(rec.get("expire_at") or 0)))
            return True
        except Exception as e:
            _feed_log("db.insert.err", repr(e))
    items = _feed_file_load()
    items.append(rec)
    _feed_file_save(items)
    return False


def _feed_find(data):
    want = str(data.get("social_feed_id") or data.get("feed_id") or "")
    bare = _feed_bare(want)
    if want and _feed_ensure():
        try:
            rows = database.query_all(
                "SELECT * FROM social_feed_post WHERE social_feed_id = ? OR social_feed_id = ?"
                " OR social_feed_id LIKE ? LIMIT 1",
                (want, bare, (bare + ":%") if bare else "\x00")) or []
            if rows:
                return _row_to_rec(rows[0]), (rows[0].get("social_feed_id") or want)
        except Exception as e:
            _feed_log("db.find.err", repr(e))
    for rec in _feed_file_load():
        rid = str(rec.get("social_feed_id") or "")
        if rid == want or (bare and _feed_bare(rid) == bare):
            return rec, rid
    return None, want


def _feed_apply(kind, rid, data):
    """改库(或回退文件)。rid 是完整 "<uuid>:<hex>"。"""
    if _feed_ensure():
        try:
            if kind == "delete":
                database.execute("DELETE FROM social_feed_post WHERE social_feed_id = ?", (rid,))
            elif kind == "edit":
                database.execute("UPDATE social_feed_post SET message = ? WHERE social_feed_id = ?",
                                 (str(data.get("message") or ""), rid))
            else:
                col = {"like": "likes_count", "super_like": "likes_count",
                       "hide": "hide_count", "report": "report_count"}[kind]
                database.execute(
                    "UPDATE social_feed_post SET %s = %s + 1 WHERE social_feed_id = ?" % (col, col),
                    (rid,))
            return True
        except Exception as e:
            _feed_log("db." + kind + ".err", repr(e))
    items = _feed_file_load()
    out = []
    for r in items:
        if _feed_bare(r.get("social_feed_id")) != _feed_bare(rid):
            out.append(r)
            continue
        if kind == "delete":
            continue
        r = dict(r)
        if kind == "edit":
            if data.get("message") is not None:
                r["message"] = str(data.get("message"))
        else:
            rk = r.get("reactions")
            if not isinstance(rk, dict):
                rk = {}
            rk = dict(rk)
            key = {"like": "total_like_count", "super_like": "total_like_count",
                   "hide": "total_hide_count", "report": "total_report_count"}[kind]
            rk[key] = int(rk.get(key) or 0) + 1
            r["reactions"] = rk
        out.append(r)
    _feed_file_save(out)
    return False


# ---------------- 组装 ----------------

def _feed_public_id(fid, message):
    """account 前缀(老客户端, 本服是 0.15.5)的 create 返回**裸 uuid**
    (见参考 win2 account/create_social_feed.py: str(uuid.uuid4()));
    "<uuid>:<8hex>" 是 0.26.5+ 的 /service/social-feed/api/v1 才引入的公开 id 形态。
    老客户端把 social_feed_id 当 GUID 用, 带后缀 -> 本地 SocialFeedBarn 里查不到这条 feed
    -> 世界里的小船点不开(点击时客户端本地就 return, 连一个 HTTP 包都不发)。
    """
    return str(fid)


def _feed_bare(fid):
    return str(fid or "").split(":")[0]


def _feed_as_list(v):
    if isinstance(v, str):
        return [v] if v else []
    if isinstance(v, (list, tuple)):
        return [x for x in v if isinstance(x, str) and x]
    return []


def _feed_item(rec):
    extra = dict(_FEED_DEFAULT_EXTRA)
    up = rec.get("extra_data")
    if isinstance(up, dict):
        for k, v in up.items():
            if v is not None:
                extra[k] = v
    r = rec.get("reactions")
    if not isinstance(r, dict):
        r = {}
    reactions = {}
    for k, dv in _FEED_ZERO_REACTIONS.items():
        v = r.get(k, dv)
        reactions[k] = bool(v) if k == "is_poisoned" else int(v or 0)
    return {
        "comments_enabled": bool(rec.get("comments_enabled", True)),
        "create_at": int(rec.get("create_at") or 0) * 1000,
        "expire_at": int(rec.get("expire_at") or 0) * 1000,
        "extra_data": extra,
        "message": rec.get("message") or "",
        "pool_name": rec.get("pool_name") or "",
        "pool_type": rec.get("pool_type") or "",
        "reactions": reactions,
        "social_feed_id": rec.get("social_feed_id") or "",
        "state": "normal",
        "tags": rec.get("tags"),
        "user_id": rec.get("user_id") or "",
        "author_id": rec.get("user_id") or "",
        "total_report_count": reactions["total_report_count"],
        "total_like_count": reactions["total_like_count"],
        "total_hide_count": reactions["total_hide_count"],
        "user_reactions": [],
    }


def _feed_iso(ts, with_ms=True):
    """秒 -> 官方风格的 ISO 时间串（create_at 带毫秒、expire_at 不带）。"""
    try:
        ts = int(ts or 0)
    except (TypeError, ValueError):
        ts = 0
    if ts <= 0:
        return None
    try:
        base = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return None
    return (base + ".000Z") if with_ms else (base + "Z")


def _feed_item_v2(rec):
    """官方 social-feed v2 (get_curated_feeds) 条目形状。

    官方抓包 service__social-feed__api__v2__get_curated_feeds.json 里每条是：
      comments_enabled / create_at(ISO,带毫秒) / expire_at(ISO) /
      extra_data{chunk_count,ori,pos,prompt_used,resource_id,scale,sub_type} /
      followed / friend / local_creation / message / pool_name / pool_type /
      reactions{…} / social_feed_id / state / tags / user_id
    """
    extra = dict(_FEED_DEFAULT_EXTRA)
    up = rec.get("extra_data")
    if isinstance(up, dict):
        for k, v in up.items():
            if v is not None:
                extra[k] = v
    r = rec.get("reactions")
    if not isinstance(r, dict):
        r = {}
    reactions = {}
    for k, dv in _FEED_ZERO_REACTIONS.items():
        v = r.get(k, dv)
        reactions[k] = bool(v) if k == "is_poisoned" else int(v or 0)
    me = str(rec.get("_me") or "")
    uid = str(rec.get("user_id") or "")
    return {
        "comments_enabled": bool(rec.get("comments_enabled", True)),
        "create_at": _feed_iso(rec.get("create_at"), True),
        "expire_at": _feed_iso(rec.get("expire_at"), False),
        "extra_data": extra,
        "followed": False,
        "friend": False,
        "local_creation": bool(me and uid and me == uid),
        "message": rec.get("message") or "",
        "pool_name": rec.get("pool_name") or "",
        "pool_type": rec.get("pool_type") or "",
        "reactions": reactions,
        "social_feed_id": rec.get("social_feed_id") or "",
        "state": "normal",
        "tags": rec.get("tags"),
        "user_id": uid,
    }


def _feed_item_compat(rec):
    """老客户端(get_curated_feeds/get_user_feeds)能吃的条目 —— 两份参考实现的并集。

    · create_at/expire_at 用 ISO 字符串（参考实现 _refs/win2 与官方抓包都是 ISO；
      只有更新版的 get_by_ids 用毫秒数字）。
    · 同时给扁平 total_* / user_reactions（libBootloader.so 的社交动态键表里出现过）。
    · local_creation / followed / friend（参考实现里有，缺了客户端可能不认）。
    · extra_data 必须带（小船在世界里的 pos/ori/scale 全靠它）。
    """
    extra = dict(_FEED_DEFAULT_EXTRA)
    up = rec.get("extra_data")
    if isinstance(up, dict):
        for k, v in up.items():
            if v is not None:
                extra[k] = v
    r = rec.get("reactions")
    if not isinstance(r, dict):
        r = {}
    reactions = {}
    for k, dv in _FEED_ZERO_REACTIONS.items():
        v = r.get(k, dv)
        reactions[k] = bool(v) if k == "is_poisoned" else int(v or 0)
    reactions.setdefault("score", 0)
    reactions.setdefault("total_attempt_count", 0)
    reactions.setdefault("total_succeed_count", 0)
    me = str(rec.get("_me") or "")
    uid = str(rec.get("user_id") or "")
    return {
        "social_feed_id": rec.get("social_feed_id") or "",
        "user_id": uid,
        "author_id": uid,
        "pool_type": rec.get("pool_type") or "",
        "pool_name": rec.get("pool_name") or "",
        # 两种时间表示都给不了（同名键），以参考实现的 ISO 为准
        "create_at": _feed_iso(rec.get("create_at"), True),
        "expire_at": _feed_iso(rec.get("expire_at"), False),
        "message": rec.get("message") or "",
        "reactions": reactions,
        "state": "normal",
        "tags": rec.get("tags"),
        "extra_data": extra,
        "local_creation": bool(me and uid and me == uid),
        "followed": False,
        "friend": False,
        "comments_enabled": bool(rec.get("comments_enabled", True)),
        # 扁平键（.so 键表里出现过，老解析路径用）
        "total_like_count": reactions.get("total_like_count", 0),
        "total_hide_count": reactions.get("total_hide_count", 0),
        "total_report_count": reactions.get("total_report_count", 0),
        "user_reactions": [],
    }


def _feed_query(data, default_limit):
    types = _feed_as_list(data.get("pool_type")) or _feed_as_list(data.get("pool_types"))
    names = _feed_as_list(data.get("pool_name")) or _feed_as_list(data.get("pool_names"))
    if not names:
        names = _feed_as_list(data.get("level_id"))
    try:
        skip = max(0, int(data.get("skip") or 0))
    except (TypeError, ValueError):
        skip = 0
    lim = data.get("batch_size") or data.get("n_public") or data.get("count")
    try:
        lim = max(1, min(int(lim), 200))
    except (TypeError, ValueError):
        lim = default_limit
    now = int(time.time())

    if _feed_ensure():
        try:
            sql = "SELECT * FROM social_feed_post WHERE (expire_at = 0 OR expire_at > ?)"
            params = [now]
            if types:
                sql += " AND pool_type IN (%s)" % ",".join(["?"] * len(types))
                params.extend(types)
            if names:
                sql += " AND pool_name IN (%s)" % ",".join(["?"] * len(names))
                params.extend(names)
            sql += " ORDER BY created_at DESC LIMIT %d OFFSET %d" % (lim, skip)
            rows = database.query_all(sql, tuple(params)) or []
            return [_row_to_rec(r) for r in rows]
        except Exception as e:
            _feed_log("db.query.err", repr(e))
    keep = []
    for rec in reversed(_feed_file_load()):
        try:
            exp = int(rec.get("expire_at") or 0)
        except (TypeError, ValueError):
            exp = 0
        if exp and exp < now:
            continue
        if types and rec.get("pool_type") not in types:
            continue
        if names and rec.get("pool_name") not in names:
            continue
        keep.append(rec)
    return keep[skip:skip + lim]


def _feed_response(data, default_limit, tag):
    try:
        items = _feed_query(data, default_limit)
        _feed_log(tag, "pool_type=%r pool_name=%r hits=%d user=%s" % (
            data.get("pool_type"), data.get("pool_name"), len(items),
            str(data.get("user") or data.get("user_id") or "")[:8]))
        me = str(data.get("user") or data.get("user_id") or "")
        for r in items:
            try:
                r["_me"] = me
            except Exception:
                pass
        # ★ p79：get_curated_feeds 走官方 v2 的"三层环"结构 —— 客户端在
        #   inner_ring.public 里取别人的留言小船/共享空间蜡烛；以前只回 feeds，
        #   所以服务端明明有数据、地图上却一条都不显示。
        v1 = [_feed_item(r) for r in items]
        compat = [_feed_item_compat(r) for r in items]
        if tag == "get_curated_feeds":
            # ★ p82：feeds 用"参考实现并集"条目（ISO 时间 + local_creation/friend +
            #   extra_data + 扁平 total_*），顶层再带上 total_friends/total_available_results。
            return {
                "feeds": compat,
                "local": compat,
                "random": compat,
                "friends": [],
                "followed": [],
                "total_friends": 0,
                "total_available_results": len(compat),
                # 新客户端的 v2 环形结构（老客户端会忽略）
                "inner_ring": {
                    "followed": None,
                    "friends": None,
                    "local": None,
                    "public": compat,
                    "total_available_results": len(compat),
                    "total_followed": 0,
                    "total_friends": 0,
                },
                "outer_ring": None,
                "result": "ok",
            }
        # get_user_feeds：参考实现也回 {"feeds": [...]}，同样用兼容条目
        return {"feeds": compat, "result": "ok", "total_available_results": len(compat)}
    except Exception as e:
        _feed_log(tag + ".err", repr(e))
        if tag == "get_curated_feeds":
            return {"feeds": [], "local": [], "friends": [], "random": [], "followed": [],
                    "inner_ring": {"followed": None, "friends": None, "local": None,
                                   "public": [], "total_available_results": 0,
                                   "total_followed": 0, "total_friends": 0},
                    "outer_ring": None, "result": "ok"}
        return {"feeds": [], "result": "ok"}


def _feed_create(data):
    message = data.get("message")
    if message is None:
        message = ""
    message = str(message)
    try:
        fid = str(uuid.uuid4())
        pid = _feed_public_id(fid, message)
        now = int(time.time())
        extra = data.get("extra_data")
        rec = {
            "social_feed_id": pid,
            "user_id": str(data.get("user") or data.get("user_id") or ""),
            "pool_type": str(data.get("pool_type") or ""),
            "pool_name": str(data.get("pool_name") or ""),
            "message": message,
            "create_at": now,
            "expire_at": now + 1209600,
            "extra_data": extra if isinstance(extra, dict) else {},
            "tags": data.get("tags"),
            "comments_enabled": data.get("comments_enabled", True),
            "private": bool(data.get("private") or False),
        }
        db_ok = _feed_insert(rec)
        _feed_log("create", "user=%s pool=%s/%s id=%s db=%s msg=%r extra=%s" % (
            rec["user_id"][:8], rec["pool_type"], rec["pool_name"], pid,
            "mysql" if db_ok else "file", message,
            json.dumps(extra, ensure_ascii=False) if extra else "-"))
        return {"result": "success", "social_feed_id": pid}
    except Exception as e:
        _feed_log("create.err", repr(e))
        return {"result": "success",
                "social_feed_id": _feed_public_id(str(uuid.uuid4()), message)}


def _feed_mutate(kind, data):
    out = {"result": ""}
    if kind == "like":
        out = {"result": "created"}
    elif kind == "super_like":
        out = {"result": "success", "update_currency": {"candles": 0}}
    elif kind in ("hide", "edit", "delete"):
        out = {"result": "ok"}
    _rec, rid = _feed_find(data)
    if _rec is not None and kind in ("like", "super_like", "hide", "report", "edit", "delete"):
        _feed_apply(kind, rid, data)
    _feed_log(kind, "id=%s found=%s user=%s" % (
        rid or "-", _rec is not None,
        str(data.get("user") or data.get("user_id") or "")[:8]))
    return out


def _feed_reactions(data):
    rec, _rid = _feed_find(data)
    if rec is None:
        return {"reactions": dict(_FEED_ZERO_REACTIONS), "user_reactions": None}
    return {"reactions": _feed_item(rec)["reactions"], "user_reactions": None}


@account_bp.route("/social_feed/get_reactions", methods=["POST"])
def social_feed_get_reactions():
    return jsonify(_feed_reactions(request.get_json(force=True, silent=True) or {}))


@account_bp.route("/social_feed/like", methods=["POST"])
def social_feed_like():
    return jsonify(_feed_mutate("like", request.get_json(force=True, silent=True) or {}))


@account_bp.route("/social_feed/super_like", methods=["POST"])
def social_feed_super_like():
    return jsonify(_feed_mutate("super_like", request.get_json(force=True, silent=True) or {}))


@account_bp.route("/social_feed/hide", methods=["POST"])
def social_feed_hide():
    return jsonify(_feed_mutate("hide", request.get_json(force=True, silent=True) or {}))


@account_bp.route("/social_feed/report", methods=["POST"])
def social_feed_report():
    return jsonify(_feed_mutate("report", request.get_json(force=True, silent=True) or {}))


@account_bp.route("/social_feed/edit", methods=["POST"])
def social_feed_edit():
    return jsonify(_feed_mutate("edit", request.get_json(force=True, silent=True) or {}))


@account_bp.route("/social_feed/delete", methods=["POST"])
def social_feed_delete():
    return jsonify(_feed_mutate("delete", request.get_json(force=True, silent=True) or {}))
# ============ /account/buff/remove_buff1 (取消魔法) 2026-10-05 ============
# 原来**完全没有这个路由** —— 客户端点「取消魔法」调它, 命中 index.py 的
# 404->200 {} 兜底, 拿不到 "remove_buff":"success", 所以取消不掉。
# 官方形状(win3 account/remove_buff1 + 抓包):
#   请求 {"user","session","buff_id"|"buff_name"|"id","giver"}
#   响应 {"get_buffs":[...], "get_buffs_sign":"<sha256>", "remove_buff":"success"}
# 官方实现里找不到这个 buff 也照回 success, 这里保持一致。
@account_bp.route("/buff/remove_buff1", methods=["POST"])
@account_bp.route("/buff/remove_buff", methods=["POST"])
def remove_buff1():
    req = request.get_json(force=True, silent=True) or {}
    _social_log("remove_buff1.req", req)
    user = req.get("user") or req.get("user_id")
    ident = req.get("buff_id")
    if ident in (None, ""):
        ident = req.get("id")
    if ident in (None, ""):
        ident = req.get("buff_name")
    key = None
    try:
        cur = _buffs_store(user) or {}
    except Exception as e:
        _social_log("remove_buff1.store.err", repr(e))
        cur = {}
    if ident not in (None, ""):
        key = str(ident)
        if key not in cur:
            # 也允许客户端传 buff 名字(官方注释: buff_id 可以是 id 或名字)
            key = None
            for k in list(cur.keys()):
                d = _buff_def(k) or {}
                if str(d.get("name") or "") == str(ident):
                    key = k
                    break
    if key is not None and key in cur:
        cur.pop(key, None)
        try:
            _set_prefs(user, {"buffs": cur})
        except Exception as e:
            _social_log("remove_buff1.save.err", repr(e))
    try:
        buffs, sign = _buffs_payload(user)
    except Exception as e:
        _social_log("remove_buff1.payload.err", repr(e))
        buffs, sign = [], ""
    _social_log("remove_buff1.resp", {"user": user, "removed": key is not None,
                                      "left": len(buffs)})
    return jsonify({"get_buffs": buffs, "get_buffs_sign": sign,
                    "remove_buff": "success"})
# ============ 补齐客户端缺失接口 · 第 1 批 2026-10-05 ============
# 这些接口原来一条路由都没有, 客户端调用会命中 index.py 的 404->200 {} 兜底,
# 表现就是"点了没反应"。响应形状全部对齐 win3 static_responses 官方抓包。
import json as _gf_json
import os as _gf_os
import threading as _gf_threading
import time as _gf_time

_GF_PATH = "/wbsky/config/gapfill_state.json"
_GF_LOCK = _gf_threading.Lock()


def _gf_load():
    try:
        with open(_GF_PATH, encoding="utf-8") as f:
            d = _gf_json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _gf_save(d):
    try:
        tmp = _GF_PATH + ".tmp"
        with _GF_LOCK:
            with open(tmp, "w", encoding="utf-8") as f:
                _gf_json.dump(d, f, ensure_ascii=False)
            _gf_os.replace(tmp, _GF_PATH)
        return True
    except Exception as e:
        _social_log("gapfill.save.err", repr(e))
        return False


def _gf_user(uid):
    st = _gf_load()
    u = st.get(uid)
    if not isinstance(u, dict):
        u = {}
        st[uid] = u
    return st, u


def _gf_req_user(req):
    return req.get("user") or req.get("user_id") or ""


def _gf_forge_rates(tier=None):
    """forge_rates 列表(官方形状), 第一条 reset_acknowledged=True。"""
    try:
        fr = _gf_json.load(open("/wbsky/config/forge_rates.json", encoding="utf-8-sig"))
        fr = fr.get("forge_rates") or []
    except Exception:
        fr = []
    out = []
    for i, e in enumerate(fr):
        e = dict(e) if isinstance(e, dict) else {}
        if i == 0:
            e["reset_acknowledged"] = True
            if tier is not None:
                e["tier_completion"] = round(max(0.0, min(1.0, float(tier))), 6)
        out.append(e)
    return out


def _gf_candles(uid):
    try:
        return _int_or_none(database.get_user_field(uid, "candles"))
    except Exception:
        return None


def _gf_wax_grant(uid, amount):
    """加烛火 -> 锻造 -> 返回 (wax_remainder, forged, candles)。"""
    amount = int(amount or 0)
    wax = 0
    if amount:
        try:
            _ok, wax = database.currency_add(uid, "wax", amount)
        except Exception as e:
            _social_log("gapfill.wax.err", repr(e))
    if wax is None:
        try:
            wax = int((database.currency_get(uid) or {}).get("wax") or 0)
        except Exception:
            wax = 0
    wax = int(wax or 0)
    cost = 150
    try:
        _c = (load_config() or {}).get("candle_forge_cost")
        if not _c:
            _v = _gf_json.load(open("/wbsky/config/vars.json", encoding="utf-8-sig"))
            _c = (_v.get("vars") or {}).get("candle_forge_cost")
        if _c:
            cost = int(_c)
    except Exception:
        cost = 150
    if cost <= 0:
        cost = 150
    forged = 0
    if wax >= cost:
        try:
            forged = wax // cost
            database.currency_add(uid, "wax", -(forged * cost))
            wax -= forged * cost
            cur = _int_or_none(database.get_user_field(uid, "candles")) or 0
            database.set_user_field(uid, "candles", cur + forged)
            database.currency_add(uid, "candles", forged)
        except Exception as e:
            _social_log("gapfill.forge.err", repr(e))
    return wax, forged, _gf_candles(uid)


def _gf_currency(uid):
    try:
        return build_currency(uid, _gf_candles(uid))
    except Exception as e:
        _social_log("gapfill.currency.err", repr(e))
        return {"candles": 0}


def _gf_wax_remainder(uid):
    try:
        return int((database.currency_get(uid) or {}).get("wax") or 0)
    except Exception:
        return 0


def _gf_grant_consumable(uid, cid, qty=1):
    """给背包加一件魔法(复用 user_prefs 里已有的 consumables 结构)。"""
    if not uid or not cid:
        return 0
    try:
        cid = int(cid)
    except (TypeError, ValueError):
        return 0
    try:
        prefs = _get_prefs(uid) or {}
        inv = prefs.get("consumables")
        if not isinstance(inv, dict):
            inv = {}
        cur = inv.get(str(cid))
        if not isinstance(cur, dict):
            cur = {}
        cur["consumable_id"] = cid
        cur["quantity"] = int(cur.get("quantity") or 0) + int(qty or 1)
        cur.setdefault("cooldown_until", 0)
        inv[str(cid)] = cur
        _set_prefs(uid, {"consumables": inv})
        return cur["quantity"]
    except Exception as e:
        _social_log("gapfill.grant_consumable.err", repr(e))
        return 0


# ---------------- 别名 / 无状态 ----------------

@account_bp.route("/find_prev_or_empty", methods=["POST"])
def find_prev_or_empty():
    """官方把 find_previous_or_empty 简写成 find_prev_or_empty, 回包同构。"""
    return find_previous_or_empty()


@account_bp.route("/set_device_token", methods=["POST"])
def set_device_token():
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    tok = req.get("device_token") or req.get("token") or ""
    if uid:
        st, u = _gf_user(uid)
        u["device_token"] = str(tok)
        _gf_save(st)
    _social_log("set_device_token", {"user": uid, "len": len(str(tok))})
    # 参考实现(win3/win2 set_device_token 完全一致): 直接回空对象 200
    return jsonify({})


@account_bp.route("/get_local_notifications", methods=["POST"])
def get_local_notifications():
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    st, u = _gf_user(uid) if uid else ({}, {})
    notes = u.get("local_notifications")
    return jsonify({"local_notifications": notes if isinstance(notes, list) else []})


@account_bp.route("/report/create_report", methods=["POST"])
def create_report():
    req = request.get_json(force=True, silent=True) or {}
    _social_log("create_report", {"user": _gf_req_user(req)})
    return jsonify({})


@account_bp.route("/support/get", methods=["POST"])
def support_get():
    """取客服工单。参考 support_has_reply 的做法: 一律回一个空的成功工单。"""
    req = request.get_json(force=True, silent=True) or {}
    return jsonify({"result": "ok", "tickets": []})


@account_bp.route("/add_currency", methods=["POST"])
def add_currency():
    """关卡内蜡烛门/斗篷升级触发的加币。客户端字符串表里只有 `currency` 一个键,
    所以请求体是 {user, session, currency:{币种名:数量}} 这种字典形状;
    也兼容 {currency_type, amount} 的写法。回包带上更新后的完整 currency 字典,
    否则关卡内 HUD 不刷新(照抄 give_candle 的做法)。
    """
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    added = {}
    cur_arg = req.get("currency")
    if isinstance(cur_arg, dict):
        for k, v in cur_arg.items():
            amt = _int_or_none(v)
            if amt:
                added[str(k).lower()] = int(amt)
    else:
        ctype = req.get("currency_type") or req.get("type") or req.get("name") or "candles"
        amt = _int_or_none(req.get("amount") if req.get("amount") is not None else req.get("count"))
        if amt:
            added[str(ctype).lower()] = int(amt)
    if uid:
        for k, amt in added.items():
            try:
                if k in ("candles", "candle"):
                    cur = _int_or_none(database.get_user_field(uid, "candles")) or 0
                    database.set_user_field(uid, "candles", cur + amt)
                    database.currency_add(uid, "candles", amt)
                elif k in ("wax", "candle_wax"):
                    _gf_wax_grant(uid, amt)
                else:
                    database.currency_add(uid, k, amt)
            except Exception as e:
                _social_log("add_currency.err", repr(e))
    _social_log("add_currency", {"user": uid, "added": added})
    return jsonify({"currency": _gf_currency(uid), "res": "ok", "result": True})


@account_bp.route("/get_generic_shop_item_tracking_info", methods=["POST"])
def get_generic_shop_item_tracking_info():
    return jsonify({"generic_shop_item_tracking_info": []})


# ---------------- 收集品(表情/收集物) ----------------

_GF_COLLECTIBLES = [
    {"id": "butterfly", "level": 2, "permanent_level": 1, "candle_space": False, "carrying": True, "used": False},
    {"id": "cape", "level": 2, "permanent_level": 1, "candle_space": False, "carrying": False, "used": False},
    {"id": "flame", "level": 1, "permanent_level": 0, "candle_space": True, "carrying": False, "used": True},
    {"id": "home", "level": 1, "permanent_level": 0, "candle_space": True, "carrying": False, "used": True},
    {"id": "normal", "level": 1, "permanent_level": 0, "candle_space": True, "carrying": False, "used": True},
    {"id": "point", "level": 2, "permanent_level": 1, "candle_space": False, "carrying": False, "used": True},
    {"id": "sit", "level": 1, "permanent_level": 0, "candle_space": True, "carrying": False, "used": True},
    {"id": "skykid", "level": 1, "permanent_level": 0, "candle_space": True, "carrying": False, "used": True},
]


def _gf_collectibles(uid):
    """全量收集品/动作列表。★ 必须回**全量** —— 客户端拿它刷新动作/站姿列表,
    回空或回少会把游戏里的动作清空(参考实现明确警告过)。
    数据源与 /account/get_collectibles 完全一致: config/all_collect.json
    (全服默认全四级动作)。
    """
    try:
        with open("/wbsky/config/all_collect.json", encoding="utf-8-sig") as f:
            d = _gf_json.load(f)
        items = d.get("collectibles") if isinstance(d, dict) else d
        if isinstance(items, list) and items:
            st, u = _gf_user(uid) if uid else ({}, {})
            saved = u.get("collectibles")
            by = {}
            if isinstance(saved, list):
                for x in saved:
                    if isinstance(x, dict) and x.get("id"):
                        by[str(x["id"])] = x
            out = []
            for base in items:
                if not isinstance(base, dict):
                    continue
                it = dict(base)
                it.update(by.get(str(it.get("id")), {}) or {})
                out.append(it)
            return out
    except Exception as ex:
        _social_log("gapfill.collectibles.err", repr(ex))
    st, u = _gf_user(uid) if uid else ({}, {})
    saved = u.get("collectibles")
    by_id = {}
    if isinstance(saved, list):
        for x in saved:
            if isinstance(x, dict) and x.get("id"):
                by_id[str(x["id"])] = x
    out = []
    for d0 in _GF_COLLECTIBLES:
        it = dict(d0)
        it.update(by_id.pop(d0["id"], {}) or {})
        out.append(it)
    for k, v in by_id.items():
        if isinstance(v, dict):
            out.append(v)
    return out
def _gf_collectibles_save(uid, items):
    st, u = _gf_user(uid)
    u["collectibles"] = items
    _gf_save(st)


@account_bp.route("/collect_collectible", methods=["POST"])
def collect_collectible():
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    cid = req.get("id") or req.get("collectible_id") or req.get("name")
    items = _gf_collectibles(uid)
    if uid and cid:
        for e in items:
            if str(e.get("id")) == str(cid):
                try:
                    e["level"] = int(e.get("level") or 0) + 1
                except (TypeError, ValueError):
                    e["level"] = 1
                e["permanent_level"] = max(int(e.get("permanent_level") or 0), e["level"] - 1)
                break
        _gf_collectibles_save(uid, items)
    _social_log("collect_collectible", {"user": uid, "id": cid})
    return jsonify({"collectibles": items})


@account_bp.route("/collect_collectible_list", methods=["POST"])
def collect_collectible_list():
    req = request.get_json(force=True, silent=True) or {}
    return jsonify({"collectibles": _gf_collectibles(_gf_req_user(req))})


@account_bp.route("/update_collectible", methods=["POST"])
def update_collectible():
    """客户端发的是 {name, carrying}(与 collect_collectible 同一个序列化器),
    服务端语义是"改状态, 不升级"。响应回全量 collectibles。"""
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    cid = req.get("name") or req.get("id") or req.get("collectible_id")
    items = _gf_collectibles(uid)
    if uid and cid:
        for e in items:
            if str(e.get("id")) == str(cid):
                if req.get("carrying") is not None:
                    e["carrying"] = bool(req.get("carrying"))
                for k in ("level", "permanent_level", "candle_space", "used"):
                    if req.get(k) is not None:
                        e[k] = req.get(k)
                break
        _gf_collectibles_save(uid, items)
    _social_log("update_collectible", {"user": uid, "name": cid})
    return jsonify({"collectibles": items})
@account_bp.route("/lock_collectibles", methods=["POST"])
def lock_collectibles():
    """官方响应: {collectible_duplicates:int, collectibles:[全量]}。
    ★ 不能回空 collectibles —— 客户端会拿它刷新动作/站姿列表, 空数组会清空。"""
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    items = _gf_collectibles(uid)
    _social_log("lock_collectibles", {"user": uid, "n": len(items)})
    return jsonify({"collectible_duplicates": 0, "collectibles": items})
@account_bp.route("/lootboxes/redeem_box", methods=["POST"])
def lootboxes_redeem_box():
    """开箱。box_id 是**名字**(实测 lootbox_common_52), 本服没有箱子定义表 ——
    绝不能凭空造 consumable_id(客户端在 get_consumable_defs 里查不到会直接闪退),
    所以只把**整包背包**原样回给客户端(参考实现: 只回一件会把背包截断)。
    货币不足的语义由 result 表达(非 2xx 会让客户端弹"非法请求。请重试。")。
    """
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    box_id = req.get("box_id") or req.get("lootbox_id") or req.get("id")
    try:
        inv = _consumables_inventory(uid)
    except Exception:
        inv = []
    _social_log("lootboxes.redeem_box", {"user": uid, "box": box_id, "bag": len(inv)})
    return jsonify({"currency": _gf_currency(uid), "get_consumables": inv,
                    "get_lootboxes": [], "result": "ok"})
@account_bp.route("/lootboxes/redeem_item", methods=["POST"])
def lootboxes_redeem_item():
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    cid = _resolve_consumable_id(req.get("item_id"), req.get("consumable_id"), req.get("id"))
    if uid and cid:
        try:
            _gf_grant_consumable(uid, cid, int(_int_or_none(req.get("count")) or 1))
        except Exception as e:
            _social_log("redeem_item.err", repr(e))
    try:
        inv = _consumables_inventory(uid)
    except Exception:
        inv = []
    _social_log("lootboxes.redeem_item", {"user": uid, "item": cid})
    return jsonify({"currency": _gf_currency(uid), "get_consumables": inv,
                    "get_lootboxes": [], "result": "ok"})


@account_bp.route("/claim_quest_reward", methods=["POST"])
def claim_quest_reward():
    """世界任务奖励(官方抓包: bonus_amount / bonus_wax_amount / update_world_quest)。"""
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    did = str(req.get("world_quest_def_id") or req.get("id") or "")
    bonus = _int_or_none(req.get("bonus_wax_amount") if req.get("bonus_wax_amount") is not None
                         else req.get("bonus_amount"))
    bonus = int(bonus) if bonus else 23
    wax, forged, candles = _gf_wax_grant(uid, bonus)
    try:
        st, u = _gf_user(uid)
        wq = u.setdefault("world_quests", {})
        wq[did] = {"ever_completed": True, "cooldown_over_time": _gf_time.time() + 86400}
        _gf_save(st)
    except Exception:
        pass
    _social_log("claim_quest_reward", {"user": uid, "quest": did, "bonus": bonus,
                                       "forged": forged})
    return jsonify({
        "bonus_amount": bonus,
        "bonus_wax_amount": bonus,
        "currency": _gf_currency(uid),
        "result": "ok",
        "update_forge_rates": _gf_forge_rates(wax / 150.0),
        "update_world_quest": {"cooldown_over_time": int(_gf_time.time()) + 86400,
                               "ever_completed": True,
                               "world_quest_def_id": did},
    })


@account_bp.route("/claim_achievement_reward", methods=["POST"])
def claim_achievement_reward():
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    aid = req.get("achievement_id") or req.get("id") or ""
    bonus = int(_int_or_none(req.get("reward")) or 1)
    _gf_wax_grant(uid, bonus * 4)
    _social_log("claim_achievement_reward", {"user": uid, "achievement": aid})
    return jsonify({"result": "ok", "currency": _gf_currency(uid),
                    "update_forge_rates": _gf_forge_rates(),
                    "update_achievement_stats": []})


@account_bp.route("/claim_survey_reward", methods=["POST"])
def claim_survey_reward():
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    _gf_wax_grant(uid, 4)
    _social_log("claim_survey_reward", {"user": uid})
    return jsonify({"result": "ok", "currency": _gf_currency(uid),
                    "update_forge_rates": _gf_forge_rates()})


@account_bp.route("/season_finalize", methods=["POST"])
def season_finalize():
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    sid = str(req.get("season_id") or req.get("season") or "")
    _social_log("season_finalize", {"user": uid, "season": sid})
    return jsonify({
        "currency": _gf_currency(uid),
        "result": [sid] if sid else [],
        "update_forge_rates": _gf_forge_rates(_gf_wax_remainder(uid) / 150.0),
        "update_unlocks": [{"ack": False, "created_at": 0,
                            "name": ("season_finish_" + sid) if sid else "season_finish",
                            "type": "season"}],
    })


@account_bp.route("/season_start", methods=["POST"])
def season_start():
    """官方响应是 {"result": ["season_N"]} —— result 是**数组**。
    只有"本次新开始的季节"才回一条(触发开场动画), 之后必须回空数组,
    否则每次登录都重播开场动画。请求体只有 user/user_id/session。
    """
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    sid = str(req.get("season_id") or req.get("season") or "")
    first = False
    if uid and sid:
        st, u = _gf_user(uid)
        started = u.get("season_started")
        if not isinstance(started, list):
            started = []
        if sid not in started:
            started.append(sid)
            u["season_started"] = started
            _gf_save(st)
            first = True
    _social_log("season_start", {"user": uid, "season": sid, "first": first})
    return jsonify({"result": [sid] if (first and sid) else [],
                    "update_unlocks": [], "delete_unlocks": []})
@account_bp.route("/rebirth", methods=["POST"])
def rebirth():
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    _social_log("rebirth", {"user": uid})
    return jsonify({"result": "ok", "currency": _gf_currency(uid),
                    "update_unlocks": [], "update_forge_rates": _gf_forge_rates()})


@account_bp.route("/reset_account_world_quests", methods=["POST"])
@account_bp.route("/reset_account_world_quest_cooldowns", methods=["POST"])
def reset_account_world_quests():
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    try:
        st, u = _gf_user(uid)
        u["world_quests"] = {}
        _gf_save(st)
    except Exception:
        pass
    _social_log("reset_account_world_quests", {"user": uid})
    return jsonify({"result": "ok", "update_world_quests": [],
                    "currency": _gf_currency(uid)})


@account_bp.route("/purchase_unlock_list", methods=["POST"])
def purchase_unlock_list():
    """批量购买解锁: 回 update_unlocks 让客户端入库。"""
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    want = req.get("unlocks") or req.get("names") or req.get("list") or []
    if not isinstance(want, list):
        want = [want] if want else []
    out = []
    for w in want:
        if isinstance(w, dict):
            out.append({"ack": bool(w.get("ack") or False),
                        "created_at": int(w.get("created_at") or _gf_time.time()),
                        "name": str(w.get("name") or ""), "type": str(w.get("type") or "")})
        elif w:
            out.append({"ack": False, "created_at": int(_gf_time.time()),
                        "name": str(w), "type": str(req.get("type") or "")})
    _social_log("purchase_unlock_list", {"user": uid, "n": len(out)})
    return jsonify({"result": "ok", "update_unlocks": out, "currency": _gf_currency(uid)})


# ---------------- 外部账号关联(Apple/Switch 等) ----------------
# 客户端这一族端点只看成功/失败(没有响应字段名), 所以极简结构就够;
# 登录响应里的 external_links / external_account_friends 用同一套存储。

def _gf_links(uid):
    st, u = _gf_user(uid) if uid else ({}, {})
    v = u.get("external_links")
    return v if isinstance(v, list) else []


@account_bp.route("/link_info_external", methods=["POST"])
def link_info_external():
    req = request.get_json(force=True, silent=True) or {}
    return jsonify({"external_links": _gf_links(_gf_req_user(req))})


@account_bp.route("/link_external", methods=["POST"])
def link_external():
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    eid = str(req.get("external_id") or "")
    if uid:
        st, u = _gf_user(uid)
        lst = u.setdefault("external_links", [])
        if eid and not any(str(x.get("external_id")) == eid for x in lst if isinstance(x, dict)):
            lst.append({"external_id": eid,
                        "external_account_type": str(req.get("external_account_type") or ""),
                        "external_account_source": str(req.get("external_account_source") or "")})
        _gf_save(st)
    _social_log("link_external", {"user": uid, "external_id": eid})
    return jsonify({"external_links": _gf_links(uid)})


@account_bp.route("/unlink_external", methods=["POST"])
def unlink_external():
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    eid = str(req.get("external_id") or "")
    if uid and eid:
        st, u = _gf_user(uid)
        u["external_links"] = [x for x in (u.get("external_links") or [])
                               if not (isinstance(x, dict) and str(x.get("external_id")) == eid)]
        _gf_save(st)
    _social_log("unlink_external", {"user": uid, "external_id": eid})
    return jsonify({})


@account_bp.route("/accept_external_friend", methods=["POST"])
def accept_external_friend():
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    inviter = str(req.get("external_inviter_id") or "")
    if uid and inviter:
        st, u = _gf_user(uid)
        acc = u.setdefault("external_accepted", [])
        if inviter not in acc:
            acc.append(inviter)
        _gf_save(st)
    _social_log("accept_external_friend", {"user": uid, "inviter": inviter})
    return jsonify({})


@account_bp.route("/confirm_external_friend", methods=["POST"])
def confirm_external_friend():
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    fid = str(req.get("external_friend_id") or "")
    if uid and fid:
        st, u = _gf_user(uid)
        lst = u.setdefault("external_account_friends", [])
        if not any(str(x.get("external_friend_id")) == fid for x in lst if isinstance(x, dict)):
            lst.append({"external_friend_id": fid,
                        "external_friend_alias": str(req.get("external_friend_alias") or ""),
                        "created_at": int(_gf_time.time())})
        _gf_save(st)
    st, u = _gf_user(uid) if uid else ({}, {})
    lst = u.get("external_account_friends")
    _social_log("confirm_external_friend", {"user": uid, "friend": fid})
    return jsonify({"external_account_friends": lst if isinstance(lst, list) else []})


@account_bp.route("/invite_external_friend", methods=["POST"])
def invite_external_friend():
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    _social_log("invite_external_friend", {"user": uid,
                                           "retract": bool(req.get("retract")),
                                           "invitee": str(req.get("external_invitee_id") or "")})
    return jsonify({})


@account_bp.route("/external_update_friends", methods=["POST"])
def external_update_friends():
    """客户端推送上来的外部好友全量列表(字段名 user_friends, 高置信度)。"""
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    uf = req.get("user_friends")
    if uid and isinstance(uf, list):
        st, u = _gf_user(uid)
        u["external_user_friends"] = uf
        _gf_save(st)
    _social_log("external_update_friends", {"user": uid,
                                            "n": len(uf) if isinstance(uf, list) else 0})
    return jsonify({})


@account_bp.route("/join_random_game", methods=["POST"])
def join_random_game():
    """随机匹配进房: 与 join_friend_game/join_previous_game 同一套房间分配。"""
    return join_previous_game()
# ============ 补齐客户端缺失接口 · 第 2 批 2026-10-05 ============
# /account/star/* 五连(客户端解析: star_tag_def_name / bound_to_user / bound_to_other /
#   linked_to / same_pair / star_tag_links / pending_unlinked; bind 带 ecc)
# /account/buy_candle_wax  (currency=产出, forge_currency=原料, cost=单价, count=客户端算的数量;
#   官方一次只锻 1 个; 原料不足必须回 200 + result:"insufficient_funds", 非 2xx 客户端会弹错)
# /account/consumable/give_consumable1 (consumable_id/consumable_count/giver)
# /account/buff/give_buff1             (buff_id/giver -> get_buffs + get_buffs_sign)


def _gf_col(name):
    """货币名 -> currency 表列名(经 database.CURRENCY_COLUMNS)。"""
    try:
        m = getattr(database, "CURRENCY_COLUMNS", {}) or {}
        return m.get(str(name or "").lower())
    except Exception:
        return None


def _gf_cur_get(uid, name):
    name = str(name or "").lower()
    if name in ("candles", "candle"):
        return int(_int_or_none(database.get_user_field(uid, "candles")) or 0)
    col = _gf_col(name)
    if not col:
        return 0
    try:
        return int((database.currency_get(uid) or {}).get(col) or 0)
    except Exception:
        return 0


def _gf_cur_add(uid, name, delta):
    name = str(name or "").lower()
    delta = int(delta or 0)
    if not uid or not delta:
        return _gf_cur_get(uid, name)
    if name in ("candles", "candle"):
        cur = max(0, _gf_cur_get(uid, "candles") + delta)
        try:
            database.set_user_field(uid, "candles", cur)
            database.currency_add(uid, "candles", delta)
        except Exception as e:
            _social_log("gapfill.cur_add.err", repr(e))
        return cur
    try:
        _ok, val = database.currency_add(uid, name, delta)
        if val is None:
            return _gf_cur_get(uid, name)
        return int(val)
    except Exception as e:
        _social_log("gapfill.cur_add.err", repr(e))
        return _gf_cur_get(uid, name)


def _gf_star_links(uid):
    st, u = _gf_user(uid) if uid else ({}, {})
    v = u.get("star_tag_links")
    return v if isinstance(v, list) else []


def _gf_star_save(uid, links):
    st, u = _gf_user(uid)
    u["star_tag_links"] = links
    _gf_save(st)


@account_bp.route("/star/verify", methods=["POST"])
def star_verify():
    """扫 NFC 星标看归属。私服没有官方公钥 -> 不校验 ecc, 一律回 ok。"""
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    name = str(req.get("star_tag_def_name") or req.get("name") or "")
    links = _gf_star_links(uid)
    mine = any(str(x.get("star_tag_def_name")) == name for x in links if isinstance(x, dict))
    _social_log("star.verify", {"user": uid, "def": name, "mine": mine})
    return jsonify({"result": "ok", "star_tag_def_name": name,
                    "bound_to_user": bool(mine), "bound_to_other": False,
                    "linked_to": "", "same_pair": False,
                    "star_tag_links": links, "pending_unlinked": False})


@account_bp.route("/star/bind", methods=["POST"])
def star_bind():
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    name = str(req.get("star_tag_def_name") or req.get("name")
               or req.get("ecc") or req.get("target_sku") or "")
    links = _gf_star_links(uid)
    if uid and name:
        if not any(str(x.get("star_tag_def_name")) == name for x in links if isinstance(x, dict)):
            links = links + [{"star_tag_def_name": name,
                              "ecc": str(req.get("ecc") or ""),
                              "target_sku": str(req.get("target_sku") or ""),
                              "linked_to": "", "bound_at": int(_gf_time.time())}]
            _gf_star_save(uid, links)
    _social_log("star.bind", {"user": uid, "def": name, "links": len(links)})
    return jsonify({"result": "ok", "star_tag_links": links, "pending_unlinked": False})


@account_bp.route("/star/link", methods=["POST"])
def star_link():
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    sku = str(req.get("target_sku") or "")
    peer = str(req.get("linked_to") or req.get("friend") or req.get("user_id") or "")
    links = _gf_star_links(uid)
    if uid and links:
        links = [dict(x) if isinstance(x, dict) else x for x in links]
        for x in links:
            if isinstance(x, dict):
                if sku:
                    x["target_sku"] = sku
                if peer:
                    x["linked_to"] = peer
        _gf_star_save(uid, links)
    _social_log("star.link", {"user": uid, "sku": sku, "peer": peer})
    return jsonify({"result": "ok", "star_tag_links": links,
                    "linked_to": peer, "same_pair": False})


@account_bp.route("/star/undo", methods=["POST"])
def star_undo():
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    name = str(req.get("star_tag_def_name") or req.get("name") or "")
    links = _gf_star_links(uid)
    if uid:
        if name:
            links = [x for x in links
                     if not (isinstance(x, dict) and str(x.get("star_tag_def_name")) == name)]
        else:
            links = []
        _gf_star_save(uid, links)
    _social_log("star.undo", {"user": uid, "def": name, "left": len(links)})
    return jsonify({"result": "ok", "star_tag_links": links})


@account_bp.route("/star/join", methods=["POST"])
def star_join():
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    peer = str(req.get("linked_to") or req.get("friend") or req.get("user_id") or "")
    _social_log("star.join", {"user": uid, "peer": peer})
    return jsonify({"result": "ok", "linked_to": peer, "same_pair": False,
                    "star_tag_links": _gf_star_links(uid)})


@account_bp.route("/buy_candle_wax", methods=["POST"])
def buy_candle_wax():
    """烛火锻造: 扣 cost 个 forge_currency, 换 1 个 currency。
    官方一次只锻 1 个(客户端会连着发很多发)。任何非 2xx 都会让客户端弹
    "非法请求。请重试。" —— 原料不足也必须 200 + result:"insufficient_funds"。
    """
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    out_cur = str(req.get("currency") or "candles")
    in_cur = str(req.get("forge_currency") or "wax")
    cost = int(_int_or_none(req.get("cost")) or 0)
    if cost <= 0:
        # 没给 cost 就按官方变量推: wax 用 candle_forge_cost, 其它用 <name>_forge_cost
        try:
            _v = _gf_json.load(open("/wbsky/config/vars.json", encoding="utf-8-sig"))
            _vars = _v.get("vars") or {}
        except Exception:
            _vars = {}
        if in_cur == "wax":
            cost = int(_vars.get("candle_forge_cost") or 150)
        else:
            cost = int(_vars.get(in_cur.replace("_wax", "") + "_forge_cost")
                       or _vars.get(out_cur + "_forge_cost") or 1)
    result = "ok"
    have = _gf_cur_get(uid, in_cur) if uid else 0
    if uid and cost > 0:
        if have >= cost:
            _gf_cur_add(uid, in_cur, -cost)
            _gf_cur_add(uid, out_cur, 1)
        else:
            result = "insufficient_funds"
    _social_log("buy_candle_wax", {"user": uid, "out": out_cur, "in": in_cur,
                                   "cost": cost, "have": have, "result": result})
    fr = _gf_forge_rates() if in_cur == "wax" else []
    return jsonify({"result": result, "currency": _gf_currency(uid),
                    "update_forge_rates": fr})


@account_bp.route("/consumable/give_consumable1", methods=["POST"])
def give_consumable1():
    """别人送你的魔法在你这边落袋: user=我, giver=送的人。"""
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    cid = _resolve_consumable_id(req.get("consumable_id"), req.get("id"), req.get("name"))
    cnt = _int_or_none(req.get("consumable_count") if req.get("consumable_count") is not None
                       else req.get("count"))
    cnt = int(cnt or 1)
    if uid and cid:
        _gf_grant_consumable(uid, cid, cnt)
    try:
        inv = _consumables_inventory(uid)
    except Exception:
        inv = []
    _social_log("give_consumable1", {"user": uid, "consumable": cid, "n": cnt,
                                     "giver": str(req.get("giver") or "")[:8]})
    return jsonify({"result": "ok", "currency": _gf_currency(uid),
                    "get_consumables": inv})


@account_bp.route("/buff/give_buff1", methods=["POST"])
def give_buff1():
    """别人给你的 buff 在你这边生效(user=我, giver=发起方)。必须重算 get_buffs_sign。"""
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    bid = _buff_def(req.get("buff_id")) or {}
    bid_id = bid.get("id") if bid else req.get("buff_id")
    giver = str(req.get("giver") or req.get("giver_user_id") or uid or "")
    if uid and bid_id:
        try:
            _buff_add(uid, bid_id, giver=giver)
        except Exception as e:
            _social_log("give_buff1.err", repr(e))
    try:
        buffs, sign = _buffs_payload(uid)
    except Exception:
        buffs, sign = [], ""
    _social_log("give_buff1", {"user": uid, "buff": bid_id, "left": len(buffs)})
    return jsonify({"result": "ok", "get_buffs": buffs, "get_buffs_sign": sign})
# ============ 补齐客户端缺失接口 · 第 3 批 2026-10-05 ============
# wing_buffs/{drop,deposit,convert}  (光翼)
#   drop    : names[]        -> {result:"ok", update_wing_buffs:[{collected:false,name}]}
#   deposit : name_deposit_id_pairs [[name,deposit_id]] (二维数组!)
#             -> {result:"ok", update_wing_buffs:[{collected:false,deposit_id,deposited:true,name}]}
#   convert : 无业务字段 -> 全部重置 collected/deposited=false, last_conversion=now
#             -> {result:"ok", currency, wing_buffs:[{collected,name,last_conversion[,deposit_id,deposited]}]}
# /service/stage/api/v1/{get,set} (共享空间摆放)
#   没有摆放时必须回 props:[] + status:"OK"(回 "no result" 会导致放置栏打不开)
# /service/status/api/v1/{ack_unlock,add_unlocks_batch,delete_unlocks_batch}
# /service/inventory/api/v1/unlocks/delete_many
# /service/message/api/v1/redeem  (messages:[{id}] -> merge_messages)
# 全部只回 200 + 合法 JSON(非 2xx 客户端会弹"非法请求")


def _gf_wb_list(uid):
    try:
        raw = database.get_user_field(uid, "wing_buffs")
        v = _gf_json.loads(raw) if raw else []
    except Exception:
        v = []
    return v if isinstance(v, list) else []


def _gf_wb_set(uid, names):
    try:
        database.set_user_field(uid, "wing_buffs", _gf_json.dumps(sorted(set(names))))
        return True
    except Exception as e:
        _social_log("wing_buffs.set.err", repr(e))
        return False


@account_bp.route("/wing_buffs/drop", methods=["POST"])
def wing_buffs_drop():
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    names = req.get("names")
    if not isinstance(names, list):
        names = [req.get("name")] if req.get("name") else []
    names = [str(n) for n in names if n]
    if uid and names:
        keep = [n for n in _gf_wb_list(uid) if n not in set(names)]
        _gf_wb_set(uid, keep)
    _social_log("wing_buffs.drop", {"user": uid, "n": len(names)})
    return jsonify({"result": "ok",
                    "update_wing_buffs": [{"collected": False, "name": n} for n in names]})


@account_bp.route("/wing_buffs/deposit", methods=["POST"])
def wing_buffs_deposit():
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    pairs = req.get("name_deposit_id_pairs") or []
    if not isinstance(pairs, list):
        pairs = []
    upd, dep = [], {}
    if uid:
        st, u = _gf_user(uid)
        dep = u.get("wing_buff_deposits")
        if not isinstance(dep, dict):
            dep = {}
    for p in pairs:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            continue
        name, did = str(p[0]), p[1]
        try:
            did = int(did)
        except (TypeError, ValueError):
            did = 0
        dep[name] = did
        upd.append({"collected": False, "deposit_id": did, "deposited": True, "name": name})
    if uid:
        st, u = _gf_user(uid)
        u["wing_buff_deposits"] = dep
        _gf_save(st)
    _social_log("wing_buffs.deposit", {"user": uid, "n": len(upd)})
    # 客户端会发空数组表示"什么都不存" -> 回 200 + 空列表, 不要 400
    return jsonify({"result": "ok", "update_wing_buffs": upd})


@account_bp.route("/wing_buffs/convert", methods=["POST"])
def wing_buffs_convert():
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    now = int(_gf_time.time())
    names = _gf_wb_list(uid) if uid else []
    if uid:
        st, u = _gf_user(uid)
        old_dep = u.get("wing_buff_deposits")
        if not isinstance(old_dep, dict):
            old_dep = {}
        out = []
        for n in names:
            item = {"collected": False, "name": n, "last_conversion": now}
            if old_dep.get(n):
                item["deposit_id"] = old_dep[n]
                item["deposited"] = False
            out.append(item)
        u["wing_buff_deposits"] = {}
        u["wing_buff_last_conversion"] = now
        _gf_save(st)
    else:
        out = []
    _social_log("wing_buffs.convert", {"user": uid, "buffs": len(out)})
    return jsonify({"result": "ok", "currency": _gf_currency(uid), "wing_buffs": out})


# ---------------- 共享空间摆放(stage) ----------------

def _gf_stage_key(owner, stage_id):
    return "%s|%s" % (owner or "self", stage_id or "")


def _gf_stage_get(owner, stage_id):
    st = _gf_load()
    stages = st.get("stages")
    if not isinstance(stages, dict):
        return None
    v = stages.get(_gf_stage_key(owner, stage_id))
    return v if isinstance(v, dict) else None


def _gf_stage_put(owner, stage_id, level_id, props, sequence=None):
    st = _gf_load()
    stages = st.setdefault("stages", {})
    k = _gf_stage_key(owner, stage_id)
    old = stages.get(k) if isinstance(stages.get(k), dict) else {}
    try:
        seq = int(sequence) if sequence else 0
    except (TypeError, ValueError):
        seq = 0
    if seq <= 0:
        seq = int(old.get("sequence") or 0) + 1
    stages[k] = {"props": props, "sequence": seq, "level_id": level_id,
                 "stage_id": stage_id, "updated_at": int(_gf_time.time())}
    _gf_save(st)
    return stages[k]


def _gf_req_names(req):
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


def _gf_unlock_apply(uid, names, ack=None, remove=False):
    """把 status 解锁写进 gapfill 存储, 并回客户端形状。"""
    st, u = _gf_user(uid)
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
            e = by.get(n) or {"name": n, "type": "level", "created_at": int(_gf_time.time())}
            if ack is not None:
                e["ack"] = bool(ack)
            e["unlocked_at"] = int(e.get("unlocked_at") or 0)
            by[n] = e
            touched.append(e)
    u["status_unlocks"] = list(by.values())
    if remove:
        tomb = u.get("status_unlock_tombstone")
        if not isinstance(tomb, list):
            tomb = []
        u["status_unlock_tombstone"] = sorted(set(tomb + names))
    _gf_save(st)
    return touched


def _gn_load():
    try:
        with open(_GF_GNOTE_PATH, encoding="utf-8") as f:
            d = _gf_json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _gn_save(items):
    try:
        tmp = _GF_GNOTE_PATH + ".tmp"
        with _GF_GNOTE_LOCK:
            with open(tmp, "w", encoding="utf-8") as f:
                _gf_json.dump(items[-5000:], f, ensure_ascii=False)
            _gf_os.replace(tmp, _GF_GNOTE_PATH)
        return True
    except Exception as e:
        _social_log("geonote.save.err", repr(e))
        return False


def _gn_trim(msg):
    b = str(msg if msg is not None else "").encode("utf-8")[:99]
    try:
        return b.decode("utf-8", "ignore")
    except Exception:
        return str(msg or "")[:99]


def _gn_vec3(v):
    out = [0.0, 0.0, 0.0]
    if isinstance(v, (list, tuple)):
        for i in range(min(3, len(v))):
            try:
                out[i] = float(v[i])
            except (TypeError, ValueError):
                out[i] = 0.0
    return out


def _gn_nick(uid):
    if not uid:
        return ""
    for getter in (lambda: database.get_user_field(uid, "nickname"),
                   lambda: (database.query_one("SELECT nickname FROM users WHERE id = ?",
                                               (uid,)) or {}).get("nickname")):
        try:
            n = getter()
            if n:
                return str(n)
        except Exception:
            continue
    return str(uid)[:8]


def _gn_item(rec):
    return {
        "id": rec.get("id") or "",
        "creator_nickname": rec.get("creator_nickname") or "",
        "message": rec.get("message") or "",
        "pos": _gn_vec3(rec.get("pos")),
        "created_time": int(rec.get("created_time") or 0),
        "parent_guid": int(_int_or_none(rec.get("parent_guid")) or 0),
        "parent_rel_pos": _gn_vec3(rec.get("parent_rel_pos")),
        "cam_dir": _gn_vec3(rec.get("cam_dir")),
    }


def _gn_payload(uid, level_id=None, limit=0, bug=False, only_id=None):
    items = [r for r in _gn_load() if bool(r.get("bug")) == bool(bug)]
    if only_id:
        items = [r for r in items if str(r.get("id")) == str(only_id)]
    elif level_id not in (None, "", 0, "0"):
        try:
            lid = int(level_id)
            items = [r for r in items if int(r.get("level_id") or 0) == lid]
        except (TypeError, ValueError):
            pass
    items = sorted(items, key=lambda r: -int(r.get("created_time") or 0))
    if limit:
        try:
            items = items[:max(1, min(int(limit), 200))]
        except (TypeError, ValueError):
            pass
    likes, social = [], []
    for r in items:
        gid = r.get("id")
        likes.append({"geonote_id": gid, "likes": int(r.get("likes") or 0)})
        social.append({"geonote_id": gid,
                       "liked": 1 if r.get("liked") else 0,
                       "visible": 1 if r.get("visible", True) else 0,
                       "reported": 1 if r.get("reported") else 0,
                       "emoji_id": int(r.get("emoji_id") or 0)})
    return {"geonotes": [_gn_item(r) for r in items], "likes": likes,
            "emojis": [], "social": social}


def _gn_create(bug):
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    items = _gn_load()
    rec = {
        "id": str(uuid.uuid4()),
        "creator": uid,
        "creator_nickname": _gn_nick(uid),
        "level_id": req.get("level_id") or 0,
        "level_hash": req.get("level_hash") or 0,
        "pos": _gn_vec3(req.get("pos")),
        "message": _gn_trim(req.get("message")),
        "parent_guid": int(_int_or_none(req.get("parent_guid")) or 0),
        "parent_rel_pos": _gn_vec3(req.get("parent_rel_pos")),
        "cam_dir": _gn_vec3(req.get("cam_dir")),
        "created_time": int(_gf_time.time()),
        "bug": bool(bug),
        "likes": 0, "liked": False, "visible": True, "reported": False, "emoji_id": 0,
    }
    items.append(rec)
    _gn_save(items)
    _social_log("geonote.create", {"user": uid, "bug": bug, "id": rec["id"],
                                   "level": rec["level_id"], "msg": rec["message"][:30]})
    # 回该关卡的完整列表(含刚建这条), 客户端才能立刻看到自己放的留言
    return _gn_payload(uid, rec["level_id"], bug=bug)


def _gn_find(items, gid):
    for i, r in enumerate(items):
        if str(r.get("id")) == str(gid):
            return i, r
    return -1, None


@account_bp.route("/geonote/create_geonote2", methods=["POST"])
def geonote_create2():
    return jsonify(_gn_create(bug=False))


@account_bp.route("/geonote/create_bug_report_geonote", methods=["POST"])
def geonote_create_bug():
    return jsonify(_gn_create(bug=True))


@account_bp.route("/geonote/delete_geonote", methods=["POST"])
def geonote_delete():
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    gid = req.get("geonote_id") or req.get("id")
    items = _gn_load()
    i, rec = _gn_find(items, gid)
    if i >= 0:
        if not rec.get("creator") or str(rec.get("creator")) == str(uid):
            items.pop(i)
            _gn_save(items)
        else:
            _social_log("geonote.delete.denied", {"user": uid, "id": gid})
    _social_log("geonote.delete", {"user": uid, "id": gid, "found": i >= 0})
    return jsonify({})


@account_bp.route("/geonote/delete_bug_report_geonote", methods=["POST"])
def geonote_delete_bug():
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    gid = req.get("geonote_id") or req.get("id")
    items = _gn_load()
    i, rec = _gn_find([r for r in items if r.get("bug")], gid)
    if i >= 0:
        items.remove(rec)
        _gn_save(items)
    _social_log("geonote.delete_bug", {"user": uid, "id": gid, "found": i >= 0})
    return jsonify({})


@account_bp.route("/geonote/edit_message_geonote", methods=["POST"])
def geonote_edit():
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    gid = req.get("geonote_id") or req.get("id")
    items = _gn_load()
    i, rec = _gn_find(items, gid)
    if i >= 0 and (not rec.get("creator") or str(rec.get("creator")) == str(uid)):
        rec["message"] = _gn_trim(req.get("message"))
        rec["edited_time"] = int(_gf_time.time())
        items[i] = rec
        _gn_save(items)
    _social_log("geonote.edit", {"user": uid, "id": gid})
    lvl = (rec or {}).get("level_id")
    return jsonify(_gn_payload(uid, lvl, bug=bool((rec or {}).get("bug"))))


@account_bp.route("/geonote/social_geonote2", methods=["POST"])
def geonote_social():
    """点赞 / emoji / 可见性 / 举报。客户端这三个布尔是用 AddInt 写 0/1。"""
    req = request.get_json(force=True, silent=True) or {}
    uid = _gf_req_user(req)
    gid = req.get("geonote_id") or req.get("id")
    items = _gn_load()
    i, rec = _gn_find(items, gid)
    if i >= 0:
        def _bit(key):
            v = req.get(key)
            if v is None:
                return None
            return bool(int(v)) if str(v).lstrip("-").isdigit() else bool(v)
        liked = _bit("liked")
        if liked is not None:
            if liked and not rec.get("liked"):
                rec["likes"] = int(rec.get("likes") or 0) + 1
            elif not liked and rec.get("liked"):
                rec["likes"] = max(0, int(rec.get("likes") or 0) - 1)
            rec["liked"] = liked
        vis = _bit("visible")
        if vis is not None:
            rec["visible"] = vis
        rep = _bit("reported")
        if rep is not None:
            rec["reported"] = rep
        if req.get("emoji_id") is not None:
            rec["emoji_id"] = int(_int_or_none(req.get("emoji_id")) or 0)
        items[i] = rec
        _gn_save(items)
    _social_log("geonote.social", {"user": uid, "id": gid, "liked": req.get("liked"),
                                   "reported": req.get("reported")})
    lvl = (rec or {}).get("level_id")
    return jsonify(_gn_payload(uid, lvl, bug=bool((rec or {}).get("bug"))))


@account_bp.route("/geonote/request_n_bug_report_geonotes", methods=["POST"])
def geonote_bug_list():
    req = request.get_json(force=True, silent=True) or {}
    return jsonify(_gn_payload(_gf_req_user(req), req.get("level_id"),
                               req.get("n") or req.get("limit"), bug=True))


@account_bp.route("/geonote/get_user_geonotes", methods=["POST"])
def geonote_user_list():
    req = request.get_json(force=True, silent=True) or {}
    return jsonify(_gn_payload(_gf_req_user(req), req.get("level_id"), req.get("limit")))


@account_bp.route("/geonote/request_geonotes_for_n_friends", methods=["POST"])
def geonote_friends_list():
    req = request.get_json(force=True, silent=True) or {}
    return jsonify(_gn_payload(_gf_req_user(req), req.get("level_id"),
                               req.get("n") or req.get("limit")))
