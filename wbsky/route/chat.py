# route/chat.py
"""游戏内聊天指令模块。

玩家在聊天栏输入 /cmd xxx，客户端会把消息 POST 到 /account/chat/send，
这里解析并执行指令，然后把结果返回给客户端。

支持的指令：
    /cmd unlock_outfits              解锁全部装扮
    /cmd unlock_wings [数量]          设置光翼数量，不填=无翼，最大 256
    /cmd unlock_collectibles         解锁全部动作/表情
    /cmd height [数值]               改身高，范围 -4 ~ 4
    /cmd scale [数值]                改体型，范围 -4 ~ 12
    /cmd consumables [数量]           设置魔法数量
    /cmd candles [数量]               给蜡烛
    /cmd season_candles [数量]         给季节蜡烛
    /cmd hearts [数量]                给爱心
    /cmd help                         查看指令列表
"""

import json
import os
import sys
import re
import time
import uuid
import hashlib
import logging

from flask import Blueprint, current_app, jsonify, request

# 导入统一数据库模块
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import db as database

chat_bp = Blueprint("chat", __name__, url_prefix="/account")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(BASE_DIR, "config")

COMMAND_PREFIX = "/cmd"

# 客户端翼数上限
MAX_WINGS = 256

logger = logging.getLogger(__name__)


# ★ 2026-10-03：chat.py 里原来直接调用了 load_config()，但这个函数定义在 index.py，
#   本模块里根本没有 —— 每次都被下面的 try/except 吞成 NameError，
#   于是 config.json 里的 "chat_response_shape" 永远是摆设，一直走默认值。
#   这里补一个同名函数，读的仍然是 /wbsky/config.json（和 index.py 一致）。
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------
# WS 广播出口（outbox）
# --------------------------------------------------------------------------
# ws_server.py 是独立进程（9002），Flask 没法直接推帧给它，
# 所以这里把要广播的消息追加到 logs/ws_outbox.jsonl，
# ws_server 那边 tail 这个文件并原样推给所有已连接的客户端。
WS_OUTBOX = os.path.join(BASE_DIR, "logs", "ws_outbox.jsonl")


def ws_outbox_push(payload):
    """把一条待广播消息写进 outbox 文件（失败不影响聊天主流程）"""
    try:
        os.makedirs(os.path.dirname(WS_OUTBOX), exist_ok=True)
        with open(WS_OUTBOX, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return True
    except Exception as exc:
        logger.warning(f"[聊天] WS outbox 写入失败: {exc}")
        return False


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def load_json_config(name, key=None, default=None):
    """读取 config 目录下的 JSON 配置文件"""
    try:
        with open(os.path.join(CONFIG_DIR, name), encoding="utf-8") as f:
            data = json.load(f)
        return data if key is None else data.get(key, default)
    except Exception as e:
        logger.warning(f"加载配置 {name} 失败: {e}")
        return default


def walk_strings(obj):
    """递归取出对象中所有字符串"""
    if isinstance(obj, dict):
        for value in obj.values():
            yield from walk_strings(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from walk_strings(value)
    elif isinstance(obj, str):
        yield obj


def find_command(body):
    """在请求体里找 /cmd 开头的字符串，返回 [cmd, arg...]"""
    for text in walk_strings(body):
        stripped = text.strip()
        if stripped.lower().startswith(COMMAND_PREFIX):
            return stripped.split()
    return None


def find_message_text(body):
    """取聊天正文。客户端上报的是 {"user":…,"msg":"你好","ch":"local"}。"""
    if isinstance(body, dict):
        for key in ("msg", "message", "text", "content", "chat_message", "body"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value
    if isinstance(body, str) and body.strip():
        return body
    # 兜底：取请求体里第一个非 UUID、非指令的字符串
    uuid_re = re.compile(r"^[0-9a-fA-F-]{36}$")
    for text in walk_strings(body):
        stripped = text.strip()
        if not stripped or uuid_re.match(stripped):
            continue
        if stripped.lower().startswith(COMMAND_PREFIX):
            continue
        return stripped
    return ""


def find_channel(body):
    """取频道名。客户端字段是 ch，取值 local / level / table / bench* / chat* 等。"""
    if isinstance(body, dict):
        for key in ("ch", "channel", "chat_channel", "chan"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:50]
    return "local"


def find_level_id(body):
    """取关卡 id（客户端发聊天时会带当前地图）。"""
    if isinstance(body, dict):
        for key in ("level", "level_id", "levelId", "level_name", "map", "level_server"):
            value = body.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()[:64]
    return ""


def find_client_message_id(body):
    """取客户端自带的消息 id（有就用它做幂等，避免重试写重）。"""
    if isinstance(body, dict):
        for key in ("message_id", "messageId", "msg_id", "id"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:64]
    return ""


def find_user(body):
    """在请求体里找玩家 id"""
    if isinstance(body, dict):
        for key in ("user", "user_id", "userid", "uuid", "UUID", "id", "player", "player_id"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
    uuid_re = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
    for text in walk_strings(body):
        if uuid_re.match(text.strip()):
            return text.strip()
    return None


def ensure_user(user_id):
    """确保用户存在，不存在则创建"""
    if not database.user_exists(user_id):
        database.create_user(user_id, user_id, user_id)
    return True


# --------------------------------------------------------------------------
# 指令实现
# --------------------------------------------------------------------------
def cmd_help(user, args):
    """显示帮助信息"""
    help_text = (
        "🎮 服务器指令列表：\n"
        "\n"
        "【解锁类】\n"
        "/cmd unlock_outfits - 解锁全部装扮\n"
        "/cmd unlock_wings [数量] - 设置光翼数量(0=无翼，最大256)\n"
        "/cmd unlock_collectibles - 解锁全部动作/表情\n"
        "\n"
        "【外观类】\n"
        "/cmd height [数值] - 改身高(-4~4)\n"
        "/cmd scale [数值] - 改体型(-4~12)\n"
        "\n"
        "【资源类】\n"
        "/cmd candles [数量] - 给蜡烛\n"
        "/cmd season_candles [数量] - 给季节蜡烛\n"
        "/cmd hearts [数量] - 给爱心\n"
        "/cmd consumables [数量] - 设置魔法数量\n"
        "\n"
        "提示：大部分指令需要重进游戏或回一次遇境才生效"
    )
    return help_text


def cmd_unlock_outfits(user, args):
    """解锁全部装扮"""
    def flatten(items):
        for item in items or []:
            if isinstance(item, list):
                yield from flatten(item)
            elif isinstance(item, dict):
                yield item

    items = []
    for fname in ("get_shop.json", "spirit_shops.json"):
        data = load_json_config(fname) or {}
        for key in ("get_shop", "spirit_shops"):
            for item in flatten(data.get(key, [])):
                name = item.get("name") or item.get("nm")
                unlock_id = item.get("unlockid") or item.get("id")
                if name and unlock_id:
                    items.append({"name": name, "unlockid": unlock_id, "id": unlock_id, "type": "outfit"})

    if not items:
        return "❌ 没找到商店数据，解锁失败"

    # ★★★ 2026-10-03：只解锁**客户端真的能渲染**的装扮。
    #   客户端的装扮表是它自带的 `assets/Data/Resources/OutfitDefs.json`
    #   （432 条，只有 body / hair / horn / mask / neck / prop / wing 七个槽位；
    #    `.so` 里既没有 `outfit_defs` 也没有 `get_outfit_defs` 字符串
    #    ⇒ 客户端**从不调用** `/account/get_outfit_defs`）。
    #   而商店数据里有 354 个名字，其中 **294 个是客户端版本里根本不存在的**
    #   （更晚赛季的物品）。把它们写进 unlocks → 玩家在衣柜里选中 → 客户端
    #   找不到网格 → 渲染成默认模型（现象：**卤蛋无斗篷**）。
    #   所以这里按白名单过滤，保证衣柜里出现的东西一定能渲染出来。
    wl = load_json_config("outfit_id_whitelist.json") or {}
    allowed = set((wl.get("ids") or {}).values())
    skipped = 0
    if allowed:
        kept = [i for i in items if i["name"] in allowed]
        skipped = len(items) - len(kept)
        items = kept
    if not items:
        return "❌ 解锁清单与客户端装扮表没有交集，解锁失败"

    now = int(time.time())
    ensure_user(user)

    # 写入 users 表
    names = [{"name": i["name"], "ack": True, "unlocked_at": now} for i in items]
    purchases = [{"name": i["name"], "type": "outfit", "ack": True, "cost": 0} for i in items]

    database.set_user_field(user, "unlocks", json.dumps(names, ensure_ascii=False))
    database.set_user_field(user, "purchase", json.dumps(purchases, ensure_ascii=False))

    return (f"✅ 已解锁全部装扮（{len(items)} 件，已过滤 {skipped} 件客户端没有的物品）"
            f"，重进游戏生效")


def cmd_unlock_wings(user, args):
    """设置光翼数量"""
    try:
        count = int(args[0]) if args else 0
    except ValueError:
        return "❌ 数量要是数字，例如 /cmd unlock_wings 10"

    all_buffs = load_json_config("all_wing_buffs.json", "wing_buffs", []) or []
    count = max(0, min(count, len(all_buffs), MAX_WINGS))
    buffs = all_buffs[:count]

    ensure_user(user)
    database.set_user_field(user, "wing_buffs", json.dumps(buffs, ensure_ascii=False))

    note = "（无翼）" if count == 0 else (f"（客户端上限 {MAX_WINGS}）" if count >= MAX_WINGS else "")
    return f"✅ 光翼已设置为 {count} 个{note}，重进游戏生效"


def cmd_unlock_collectibles(user, args):
    """解锁全部动作/表情"""
    collectibles = load_json_config("all_collect.json", "collectibles", []) or []
    if not collectibles:
        return "❌ 没找到动作数据，解锁失败"

    ensure_user(user)
    ids = [c.get("id") for c in collectibles if c.get("id")]
    database.set_user_field(user, "collects", json.dumps(ids, ensure_ascii=False))

    return f"✅ 已解锁全部动作（{len(collectibles)} 个），重进游戏生效"


def cmd_height(user, args):
    """改身高"""
    if not args:
        return "用法：/cmd height [数值]，范围 -4 ~ 4"
    try:
        value = float(args[0])
    except ValueError:
        return "❌ 身高要是数字，例如 /cmd height 1.5"
    value = max(-4.0, min(4.0, value))

    ensure_user(user)
    database.set_user_field(user, "outfit_height", value)

    return f"✅ 身高已改为 {value}，重进游戏生效"


def cmd_scale(user, args):
    """改体型"""
    if not args:
        return "用法：/cmd scale [数值]，范围 -4 ~ 12"
    try:
        value = float(args[0])
    except ValueError:
        return "❌ 体型要是数字，例如 /cmd scale 3"
    value = max(-4.0, min(12.0, value))

    ensure_user(user)
    # 检查并添加 outfit_scale 字段
    try:
        database.set_user_field(user, "outfit_scale", value)
    except Exception:
        # 如果字段不存在，尝试 ALTER TABLE
        try:
            database.execute("ALTER TABLE users ADD COLUMN outfit_scale REAL DEFAULT 0")
            database.set_user_field(user, "outfit_scale", value)
        except Exception as e:
            return f"❌ 设置失败：{e}"

    return f"✅ 体型已改为 {value}，重进游戏生效"


def cmd_consumables(user, args):
    """设置魔法数量"""
    if not args:
        return "用法：/cmd consumables [数量]"
    try:
        count = int(args[0])
    except ValueError:
        return "❌ 数量要是数字，例如 /cmd consumables 99"

    # 从 stars_config.json 收集魔法名
    names = []
    stars = load_json_config("stars_config.json") or {}
    for value in walk_strings(stars):
        if value.startswith("consumable_") or value.endswith("_potion"):
            names.append(value)
    names = sorted(set(names))
    if not names:
        names = ["height_small_3x_handhold"]

    items = [{"name": n, "count": count} for n in names]

    # 存到 user_data 里（因为 users 表没有 consumables 字段）
    ensure_user(user)
    try:
        # 先读出现有 user_data
        user_data_str = database.get_user_field(user, "user_data")
        if user_data_str:
            user_data = json.loads(user_data_str)
        else:
            user_data = {"time": int(time.time()), "version": 1, "data": {}}

        if "data" not in user_data:
            user_data["data"] = {}
        user_data["data"]["consumables"] = items
        user_data["time"] = int(time.time())

        database.set_user_field(user, "user_data", json.dumps(user_data, ensure_ascii=False))
        return f"✅ 魔法数量已设置为 {count}（{len(names)} 种），重进游戏生效"
    except Exception as e:
        return f"❌ 设置失败：{e}"


def cmd_candles(user, args):
    """给蜡烛"""
    if not args:
        return "用法：/cmd candles [数量]，例如 /cmd candles 1000"
    try:
        amount = int(args[0])
    except ValueError:
        return "❌ 数量要是数字，例如 /cmd candles 1000"
    if amount <= 0:
        return "❌ 数量要大于 0"

    ensure_user(user)
    current = database.get_user_field(user, "candles") or 0
    new_amount = current + amount
    database.set_user_field(user, "candles", new_amount)

    return f"✅ 已给你 {amount} 个蜡烛（当前 {new_amount}），重进游戏生效"


def cmd_season_candles(user, args):
    """给季节蜡烛（存到 user_data 里）"""
    if not args:
        return "用法：/cmd season_candles [数量]，例如 /cmd season_candles 1000"
    try:
        amount = int(args[0])
    except ValueError:
        return "❌ 数量要是数字，例如 /cmd season_candles 1000"
    if amount <= 0:
        return "❌ 数量要大于 0"

    ensure_user(user)
    try:
        user_data_str = database.get_user_field(user, "user_data")
        if user_data_str:
            user_data = json.loads(user_data_str)
        else:
            user_data = {"time": int(time.time()), "version": 1, "data": {}}

        if "data" not in user_data:
            user_data["data"] = {}

        # 季节蜡烛通常在 currency 或 season_candles 字段
        if "currencies" not in user_data["data"]:
            user_data["data"]["currencies"] = {}
        user_data["data"]["currencies"]["season_candle"] = amount
        user_data["time"] = int(time.time())

        database.set_user_field(user, "user_data", json.dumps(user_data, ensure_ascii=False))
        return f"✅ 已给你 {amount} 个季节蜡烛，重进游戏生效"
    except Exception as e:
        return f"❌ 设置失败：{e}"


def cmd_hearts(user, args):
    """给爱心（存到 user_data 里）"""
    if not args:
        return "用法：/cmd hearts [数量]，例如 /cmd hearts 500"
    try:
        amount = int(args[0])
    except ValueError:
        return "❌ 数量要是数字，例如 /cmd hearts 500"
    if amount <= 0:
        return "❌ 数量要大于 0"

    ensure_user(user)
    try:
        user_data_str = database.get_user_field(user, "user_data")
        if user_data_str:
            user_data = json.loads(user_data_str)
        else:
            user_data = {"time": int(time.time()), "version": 1, "data": {}}

        if "data" not in user_data:
            user_data["data"] = {}

        if "currencies" not in user_data["data"]:
            user_data["data"]["currencies"] = {}
        user_data["data"]["currencies"]["heart"] = amount
        user_data["time"] = int(time.time())

        database.set_user_field(user, "user_data", json.dumps(user_data, ensure_ascii=False))
        return f"✅ 已给你 {amount} 个爱心，重进游戏生效"
    except Exception as e:
        return f"❌ 设置失败：{e}"


# 指令注册表
COMMANDS = {
    "help": cmd_help,
    "unlock_outfits": cmd_unlock_outfits,
    "unlock_wings": cmd_unlock_wings,
    "unlock_collectibles": cmd_unlock_collectibles,
    "height": cmd_height,
    "scale": cmd_scale,
    "consumables": cmd_consumables,
    "candles": cmd_candles,
    "season_candles": cmd_season_candles,
    "hearts": cmd_hearts,
}


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------
@chat_bp.route("/chat/send", methods=["POST"])
def chat_send():
    """聊天消息发送接口。

    两条路径：
      1. /cmd 指令 —— 解析执行后把结果回给客户端；
      2. 普通聊天 —— 落库到 chat_messages 并回一条完整记录。

    ★ 2026-10-03：以前这里对普通消息直接 `return jsonify({})`，消息被丢掉，
      所以游戏内聊天完全不通。现在按上游 xysky 的 chat_messages 表结构落库，
      并回一个带 message_id / from_user_id / channel / signature 的记录。
      signature 形如 1/<sent_at>/<channel>/<message_id>/<hex>（从真实生产库核对）。
    """
    body = request.get_json(force=True, silent=True)
    if body is None:
        body = request.get_data(as_text=True)

    logger.info(f"[聊天] {json.dumps(body, ensure_ascii=False)[:300]}")

    user = find_user(body)
    command = find_command(body)

    # ---------- 1) /cmd 指令 ----------
    if command:
        if command[0].strip().lower() == COMMAND_PREFIX:
            name = command[1].lower() if len(command) > 1 else ""
            args = command[2:]
        else:
            name = command[0][len(COMMAND_PREFIX):].strip().lower()
            args = command[1:]

        handler = COMMANDS.get(name)
        if not handler:
            logger.warning(f"[指令] 未知指令: {command}")
            return jsonify({"result": "ok",
                            "message": f"❓ 未知指令 '{name}'，输入 /cmd help 查看指令列表"})
        if not user:
            logger.warning("[指令] 找不到玩家 id")
            return jsonify({"result": "ok", "message": "❌ 找不到你的玩家 id"})
        try:
            reply = handler(user, args)
        except Exception as exc:
            logger.exception(f"[指令] 执行失败: {command}")
            reply = f"❌ 执行失败：{exc}"
        logger.info(f"[指令] {' '.join(command)} -> {reply[:100]}")
        return jsonify({"result": "ok", "message": reply,
                        "chat_message": reply, "text": reply})

    # ---------- 2) 普通聊天 ----------
    text = find_message_text(body)
    if not text:
        logger.info("[聊天] 没有正文，忽略")
        return jsonify({})
    if not user:
        logger.warning(f"[聊天] 有正文但找不到玩家 id: {text[:60]}")
        return jsonify({})

    channel = find_channel(body)
    level_id = find_level_id(body)

    message_id = find_client_message_id(body) or str(uuid.uuid4())
    sent_at_ms = int(time.time() * 1000)
    sent_at = sent_at_ms // 1000
    # signature = 1/<sent_at>/<channel>/<message_id>/<hex>（对齐上游）
    signature = "1/%d/%s/%s/%s" % (
        sent_at, channel, message_id,
        hashlib.sha1(("%s|%s|%s" % (user, channel, message_id)).encode()).hexdigest()[:8])

    stored = False
    try:
        ensure_user(user)
        database.save_chat_message(
            message_id=message_id, from_user=user, message=text,
            channel=channel, level_id=level_id, to_user="0",
            sent_at=sent_at, sent_at_ms=sent_at_ms,
            signature=signature, invalid=0, recordable=0)
        stored = True
    except Exception as exc:
        # 落库失败也要把消息回给客户端，不能让聊天因为数据库问题直接哑掉
        logger.warning(f"[聊天] 落库失败: {exc}")

    # ★★ 2026-10-03 修聊天「发不出去 / 谁都看不到」的真正修复点 ★★
    #   实测客户端发聊天打的是 **/account/chat/send**（不是 /account/send）。
    #   投递两条腿一起走，哪条通用哪条：
    #     1) HTTP 轮询队列（chat_inbox）—— 参考实现的「绕过 WebSocket」方案；
    #     2) WebSocket 广播（/account/ws）—— 写进 ws_outbox，由 ws_server 推帧。
    #   nginx 访问日志确认客户端确实会去连 GET /account/ws（带 Basic 认证），
    #   所以 WS 这条路才是聊天真正的实时通道，必须两条都通。
    try:
        recipients = []
        try:
            rows = database.query_all("SELECT id FROM users") or []
            recipients = [r.get("id") for r in rows if r.get("id")]
        except Exception as exc:
            logger.warning(f"[聊天] 取用户列表失败: {exc}")
        if user not in recipients:
            recipients.append(user)
        queued = database.enqueue_chat_message(
            recipients, sender_id=user, msg_id=message_id, msg=text,
            ch=str(channel)[:50], msg_type="chat", sent_at_ms=sent_at_ms)
        logger.info(f"[聊天投递] from={user[:8]} ch={channel} 收件人={queued}")
    except Exception as exc:
        logger.warning(f"[聊天] 投递失败: {exc}")

    # WS 广播：参考实现（SkyMoon / Windows端私服 websocket_handler.py）里
    # 推给客户端的帧形状就是这一份，字段名照抄，别改名。
    ws_frame = {
        "type": "chat",
        "sender_id": user,
        "result": "success",
        "msg_id": message_id,
        "msg": text,
        "ch": str(channel),
        "timestamp": sent_at_ms,
    }
    ws_outbox_push(ws_frame)

    logger.info(f"[聊天] {user[:8]} @{channel} lv={level_id or '-'} "
                f"len={len(text)} stored={stored} :: {text[:80]}")

    record = {
        "message_id": message_id,
        "from_user_id": user,
        "to_user_id": "0",
        "level_id": level_id,
        "channel": channel,
        "message": text,
        "sent_at": sent_at,
        "sent_at_ms": sent_at_ms,
        "invalid": 0,
        "signature": signature,
        "recordable": 0,
    }

    # ★ 2026-10-03：客户端 POST /account/chat/send 之后不渲染任何东西。
    #   参考实现用的响应体是 {"result":"success","msg_id":...,"timestamp":...}
    #   （Sky__Sky__account__send__send.py handle_chat_message 结尾），
    #   这里默认就是这一份，并且把完整的 chat_message 记录作为附加字段一起带上，
    #   客户端认哪套字段都能吃到。形状仍可在 config.json 里用
    #   "chat_response_shape" 切换（success/simple/echo/named/list/record）。
    try:
        shape = str(load_config().get("chat_response_shape", "success")).lower()
    except Exception:
        shape = "success"

    # 所有形状都带上的「保底字段」——参考实现的原样字段
    base = {
        "result": "success",
        "msg_id": message_id,
        "timestamp": sent_at_ms,
    }

    if shape == "simple":
        resp = {"result": "ok"}
    elif shape == "echo":
        resp = dict(base, message_id=message_id, msg=text, ch=channel, user=user)
    elif shape == "named":
        resp = dict(base, message_id=message_id, msg=text, ch=channel, user=user,
                    from_user_id=user, sent_at=sent_at, sent_at_ms=sent_at_ms)
    elif shape == "list":
        resp = dict(base, messages=[record], chat_messages=[record])
    elif shape == "record":
        resp = dict(base, chat_message=record, message=record, stored=stored)
    else:  # "success"（默认）
        resp = dict(base, chat_message=record, message=record, stored=stored,
                    message_id=message_id, msg=text, ch=channel, user=user)

    logger.info(f"[聊天] 响应形状={shape} keys={sorted(resp.keys())}")
    return jsonify(resp)

# ---------- Catch-All 兜底路由 ----------

@chat_bp.route("/", defaults={"path": ""}, methods=["POST", "GET"])
@chat_bp.route("/<path:path>", methods=["POST", "GET"])
def chat_catch_all(path):
    return jsonify({}), 200
