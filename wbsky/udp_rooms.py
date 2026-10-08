# -*- coding: utf-8 -*-
"""UDP 房间状态（HTTP 侧好友传送用）

数据源：本机 UDP 服务的 /stats（server.js describePeers() 里带 uuid / levelId）。
用途：
  · /account/join_friend_game 必须知道**好友当前在哪张图**：官方抓包
    （static_responses/account__join_friend_game.json）里 move_to_game.level 是
    3526133726（CandleSpace）、level_hash 32977、port 41218 —— 都是好友那份房间的
    真实值；我们原来恒发 level=0 / port=0 ⇒ 客户端不知道去哪张图，好友传送走不动。
  · level_id -> level_hash 映射由客户端自己上报（set_checkpoint），
    存 config/level_hash_map.json（官方 level_hash 是每张图的内容哈希，发错客户端会拒绝加载）。
"""
import json
import os
import threading
import time
import urllib.request

_ROOT = os.path.dirname(os.path.abspath(__file__))
_MAP = os.path.join(_ROOT, "config", "level_hash_map.json")
_lock = threading.Lock()
_cache = {"t": 0.0, "data": None}
_map_cache = {"mtime": -1, "data": {}}


def _cfg():
    try:
        with io_open(os.path.join(_ROOT, "config.json")) as f:
            return json.load(f) or {}
    except Exception:
        return {}


def io_open(p):
    return open(p, encoding="utf-8-sig")


def stats(max_age=1.0):
    """读本机 UDP /stats（默认 1 秒缓存；失败返回 None，绝不抛）。"""
    now = time.time()
    with _lock:
        if _cache["data"] is not None and now - _cache["t"] < max_age:
            return _cache["data"]
    data = None
    try:
        port = int(_cfg().get("udp_server_port", 8125) or 8125)
    except (TypeError, ValueError):
        port = 8125
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/stats" % port, timeout=1.5) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        data = None
    with _lock:
        _cache["t"] = now
        _cache["data"] = data
    return data


def peer_of_user(user_id):
    """按账号 uuid 找 UDP 房间里的条目；不在线返回 None。"""
    u = str(user_id or "").strip().lower()
    if not u or u == "0":
        return None
    d = stats() or {}
    for p in (d.get("peers") or []):
        if str(p.get("uuid") or "").strip().lower() == u:
            return p
    return None


def is_in_game(user_id):
    p = peer_of_user(user_id)
    return bool(p and p.get("inGame"))


def level_of_user(user_id, default=0):
    p = peer_of_user(user_id)
    if not p or not p.get("inGame"):
        return default
    try:
        return int(p.get("levelId") or 0) or default
    except (TypeError, ValueError):
        return default


# 同一张可见地图的不同关卡 id（别名图）：SkyHub2 ↔ CandleSpace（遇境）。
# UDP 侧 roomKey() 也是这么归一的（server.js），这里保持一致。
ALIAS_PAIRS = {}  # ★ 2026-10-06：SkyHub2 与 CandleSpace 是两张图，不能互相借 level_hash


def _map_data():
    try:
        mt = os.path.getmtime(_MAP)
    except OSError:
        return {}
    with _lock:
        if _map_cache["mtime"] != mt:
            try:
                with open(_MAP, encoding="utf-8") as f:
                    _map_cache["data"] = json.load(f) or {}
                _map_cache["mtime"] = mt
            except Exception:
                _map_cache["data"] = {}
        return _map_cache["data"]


def level_hash(level_id, default=0):
    """level_id -> 客户端上报过的 level_hash（查不到就试别名图）。

    ★ level_hash 随客户端/资源版本变：官方抓包（0.23+）CandleSpace 是 32977，
      我们自己的 0.15.5 客户端上报的是 7139 —— 所以只用**本服客户端报过**的值
      （config/level_hash_map.json，由 set_checkpoint 写入）。
    """
    try:
        lid = int(level_id)
    except (TypeError, ValueError):
        return default
    m = _map_data()
    try:
        v = m.get(str(lid))
        if v:
            return int(v)
    except (TypeError, ValueError):
        pass
    alias = ALIAS_PAIRS.get(lid)
    if alias:
        try:
            v = m.get(str(alias))
            if v:
                return int(v)
        except (TypeError, ValueError):
            pass
    return default


def record_level_hash(level_id, value):
    """记下客户端上报的 (level_id, level_hash) 供好友传送使用。"""
    try:
        lid = int(level_id)
        lh = int(value)
    except (TypeError, ValueError):
        return False
    if not lid or not lh:
        return False
    with _lock:
        data = {}
        try:
            with open(_MAP, encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            data = {}
        if str(lid) in data:
            try:
                if int(data[str(lid)]) == lh:
                    return False
            except (TypeError, ValueError):
                pass
        data[str(lid)] = lh
        try:
            tmp = _MAP + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, _MAP)
        except Exception:
            return False
        _map_cache["mtime"] = -1
    return True
