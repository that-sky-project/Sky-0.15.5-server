# -*- coding: utf-8 -*-
"""服饰 / 道具解锁注入 —— 数据源 = 客户端版本自带的 OutfitDefs.json

背景（2026-10-06 用户提供 0.15.5 客户端的 assets/Data/Resources/OutfitDefs.json，
装到 config/outfit_defs_0.15.5.json）:

  · 客户端**衣柜里的装扮**来自 /account/get_unlocks 下发的 unlocks 里那些
    `CharSkyKid_*` / `CharSkyNPC_*` 名字（type="spiritshop", ack=true）。
    本服原本一条都没有（只有 54 条 type="level" 的关卡解锁）
    ⇒ 衣柜是空的、只能穿默认装 ⇒ "装扮显示不出来"。
  · 共享空间「物品放置栏」来自 /service/status/api/v1/get_unlocks 的
    status_unlocks 里 `CharSkyKid_Prop_*`（type="level"）—— 本服同样一条都没有
    ⇒ 放置栏打不开 / 放不下道具（小船那类）。

形状照参考实现与官方抓包：
  _refs/win3/Sky/Sky/full_unlock_helper.py   (full_unlocks  → unlocks, spiritshop)
  _refs/win3/Sky/Sky/shared_space_props.py   (prop_unlocks  → status_unlocks, level)

体积闸门：<0.26.0 的客户端解锁明文超过 86016 字节会闪退（参考实现实测
224799 崩、60616 正常）。超了就**从尾部丢**条目；尾部是 NPC 服饰，
玩家服饰与原有游戏性解锁排在最前面，绝不会被裁掉。

开关（config.json，都可以随时改、即时生效）：
  outfit_unlock_enabled      默认 true   —— 关掉就不注入服饰
  outfit_unlock_defs         默认 "outfit_defs_0.15.5.json"
  outfit_unlock_include_npc  默认 true   —— 是否连 NPC 服饰一起给
  outfit_unlock_max_bytes    默认 86016  —— 只对 unlocks 明文生效
  prop_unlock_enabled        默认 true   —— 关掉就不注入道具
"""
import json
import os
import threading
import time

_ROOT = os.path.dirname(os.path.abspath(__file__))
_CONFIG = os.path.join(_ROOT, "config.json")
_LOG = os.path.join(_ROOT, "logs", "outfit_unlock.log")
_DEFS_DEFAULT = "outfit_defs_0.15.5.json"
_CREATED_AT = 1654570099
_MAX_BYTES = 86016
_PROP_TYPE = "level"
_OUTFIT_TYPE = "spiritshop"

_lock = threading.Lock()
_cfg_cache = {"mtime": 0, "data": {}}
_names_cache = {"path": None, "mtime": 0, "names": []}
_log_last = {"t": 0.0}


def _cfg():
    """读 config.json（按 mtime 缓存）。失败返回空 dict，绝不让接口 500。"""
    try:
        mt = os.path.getmtime(_CONFIG)
    except OSError:
        return {}
    with _lock:
        if _cfg_cache["mtime"] == mt:
            return _cfg_cache["data"]
    try:
        with open(_CONFIG, encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        data = {}
    with _lock:
        _cfg_cache["mtime"] = mt
        _cfg_cache["data"] = data
    return data


def _log(msg):
    """排障留痕（同一进程内 120 秒最多写一行，避免刷爆日志）。"""
    now = time.time()
    if now - _log_last["t"] < 120:
        return
    _log_last["t"] = now
    try:
        d = os.path.dirname(_LOG)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg))
    except Exception:
        pass


def defs_path():
    cfg = _cfg()
    nm = str(cfg.get("outfit_unlock_defs") or _DEFS_DEFAULT)
    return nm if os.path.isabs(nm) else os.path.join(_ROOT, "config", nm)


def outfit_names(include_npc=None):
    """OutfitDefs.json 里的全部服饰名；**玩家服饰在前、NPC 服饰在后**。

    顺序很重要：体积闸门从尾部裁剪，NPC 服饰先被丢掉。
    """
    path = defs_path()
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return []
    with _lock:
        if _names_cache["path"] == path and _names_cache["mtime"] == mt:
            return list(_names_cache["names"])
    names = []
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        data = json.loads(raw.replace("\x00", "").strip())
        if isinstance(data, dict):
            data = data.get("outfit_defs") or data.get("defs") or []
        for e in data:
            if isinstance(e, dict):
                nm = e.get("name")
            else:
                nm = e if isinstance(e, str) else None
            if nm:
                names.append(nm)
    except Exception as e:
        _log("load defs failed %s: %r" % (path, e))
        names = []
    names = list(dict.fromkeys(names))
    kid = [n for n in names if n.startswith("CharSkyKid")]
    npc = [n for n in names if not n.startswith("CharSkyKid")]
    names = kid + npc
    if include_npc is None:
        include_npc = bool(_cfg().get("outfit_unlock_include_npc", True))
    if not include_npc:
        names = [n for n in names if not n.startswith("CharSkyNPC")]
    with _lock:
        _names_cache.update({"path": path, "mtime": mt, "names": names})
    return list(names)


def plain_size(payload, key):
    """该键的明文 JSON 体积（与客户端实际收到的字节数同一套写法）。"""
    try:
        return len(json.dumps({key: payload.get(key) or []},
                              ensure_ascii=False).encode("utf-8"))
    except Exception:
        return 0


def _items_and_have(payload, key):
    items = payload.get(key)
    if not isinstance(items, list):
        return None, set()
    have = set()
    for it in items:
        if isinstance(it, dict):
            have.add(it.get("name"))
        elif isinstance(it, str):
            have.add(it)
    return items, have


def apply_outfit_unlocks(payload, key="unlocks"):
    """把该客户端版本的全部服饰名并进 unlocks（type=spiritshop）。返回新增条数。"""
    if not _cfg().get("outfit_unlock_enabled", True):
        return 0
    items, have = _items_and_have(payload, key)
    if items is None:
        return 0
    names = outfit_names()
    if not names:
        return 0
    add = [{"name": n, "type": _OUTFIT_TYPE, "ack": True, "created_at": _CREATED_AT}
           for n in names if n not in have]
    if not add:
        return 0
    items.extend(add)
    try:
        cap = int(_cfg().get("outfit_unlock_max_bytes") or _MAX_BYTES)
    except (TypeError, ValueError):
        cap = _MAX_BYTES
    base = len(items) - len(add)          # 原有条目一条都不能丢
    dropped = 0
    while len(items) > base and plain_size(payload, key) > cap:
        items.pop()
        dropped += 1
    added = len(add) - dropped
    _log("outfit_unlocks +%d (dropped %d) size=%d cap=%d"
         % (added, dropped, plain_size(payload, key), cap))
    return added


def prop_names():
    """共享空间可放置道具：只要玩家道具 CharSkyKid_Prop_*（不要 NPC 拿着的）。"""
    return [n for n in outfit_names()
            if n.startswith("CharSkyKid_") and "_Prop_" in n]


def apply_prop_unlocks(payload, key="status_unlocks"):
    """把 CharSkyKid_Prop_* 并进 status_unlocks（type=level）。返回新增条数。"""
    if not _cfg().get("prop_unlock_enabled", True):
        return 0
    items, have = _items_and_have(payload, key)
    if items is None:
        return 0
    add = [{"name": n, "type": _PROP_TYPE, "ack": True, "unlocked_at": _CREATED_AT}
           for n in prop_names() if n not in have]
    if not add:
        return 0
    items.extend(add)
    _log("prop_unlocks +%d size=%d" % (len(add), plain_size(payload, key)))
    return len(add)
