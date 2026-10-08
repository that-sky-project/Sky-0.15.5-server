# -*- coding: utf-8 -*-
"""好友默认昵称池 —— 从 config/friend_name_pool.json 里随机取名字。

## 为什么需要它

游戏里"好友的名字"是**加好友那一方给他起的备注**，存在 `friends.nickname`。
如果一直没名字，老代码的兜底是**对方 uuid 的前 8 位**（`fid[:8]`），
客户端好友列表里就显示成 `a3f9c2b1` 这种东西 —— 能认出是谁但很难看。

现在改成：**没名字的好友自动从名字池里随机取一个当默认昵称**，
并且**落库**（`friends.nickname` / `friendships.custom_name`），
所以刷新、重登、换设备都还是同一个名字。

## 名字池

`wbsky/config/friend_name_pool.json`，528 条（用户提供）。
想增删直接改那个 JSON，**不用重启**（本模块按 mtime 缓存，改了就重读）。
把文件删掉或清空 `names` 也能用 —— 自动退回"uuid 前 8 位"的老行为。

## 开关（config.json）

    "friend_random_name": true        总开关，false = 完全回到老行为
    "friend_name_pool_file": "friend_name_pool.json"
    "friend_name_len": 1              生成的默认名由几个字组成（1 或 2）
    "friend_name_persist": true       是否把生成的名字写回库里

## 一致性

同一个 (user_id, friend_id) 只要库里已经写过一次名字，后面永远返回那一个 ——
本模块只负责"生成"，"记住"由 db.py 的 `get_or_assign_friend_nickname()` 负责。
"""
import json
import os
import random
import threading
import time

# 名字池缓存：{path: (mtime, names)}
_CACHE = {}
_LOCK = threading.Lock()

DEFAULT_POOL_FILE = "friend_name_pool.json"

# 没读到池子时的最后兜底（保证接口永远有东西可回）
_FALLBACK = ["浩", "文", "夏", "雪", "云", "星", "月", "风"]


def _config_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")


def _load_config():
    """读 wbsky/config.json（每次都读，因为后台面板会改它）。"""
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        with open(p, encoding="utf-8-sig") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def pool_path(cfg=None):
    cfg = cfg if cfg is not None else _load_config()
    name = str(cfg.get("friend_name_pool_file") or DEFAULT_POOL_FILE).strip() or DEFAULT_POOL_FILE
    if os.path.isabs(name):
        return name
    return os.path.join(_config_dir(), name)


def load_pool(cfg=None):
    """读名字池（按文件 mtime 缓存，改了自动重载）。永远返回非空列表。"""
    path = pool_path(cfg)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return list(_FALLBACK)
    with _LOCK:
        hit = _CACHE.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
    names = []
    try:
        with open(path, encoding="utf-8-sig") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            raw = raw.get("names") or []
        for n in (raw or []):
            s = str(n or "").strip()
            if s:
                names.append(s)
    except Exception:
        names = []
    if not names:
        names = list(_FALLBACK)
    with _LOCK:
        _CACHE[path] = (mtime, names)
    return names


def _env(key):
    """读环境变量（部署时由 deploy/.env 注入）；空串当没设。"""
    v = os.environ.get(key)
    if v is None or str(v).strip() == "":
        return None
    return str(v).strip()


def _boolish(v):
    return str(v).strip().lower() not in ("0", "false", "no", "off")


def enabled(cfg=None):
    """总开关。优先级：环境变量 > config.json > 默认开。"""
    e = _env("WB_SKY_FRIEND_RANDOM_NAME")
    if e is not None:
        return _boolish(e)
    cfg = cfg if cfg is not None else _load_config()
    v = cfg.get("friend_random_name", True)
    if isinstance(v, str):
        return _boolish(v)
    return bool(v)


def name_len(cfg=None):
    e = _env("WB_SKY_FRIEND_NAME_LEN")
    if e is not None:
        try:
            return 2 if int(e) >= 2 else 1
        except (TypeError, ValueError):
            pass
    cfg = cfg if cfg is not None else _load_config()
    try:
        n = int(cfg.get("friend_name_len") or 1)
    except (TypeError, ValueError):
        n = 1
    return 2 if n >= 2 else 1


def random_name(cfg=None, length=None):
    """随机取一个默认昵称。

    length=1 -> 单字（如「浩」）
    length=2 -> 双字（从池子里取两个字拼起来，如「浩文」）
    """
    names = load_pool(cfg)
    n = name_len(cfg) if length is None else int(length)
    if n <= 1:
        return random.choice(names)
    # 两字：取两个不同的字拼（池子只有 1 个字时退化成单字重复没有意义，直接返回它）
    if len(names) < 2:
        return names[0]
    a, b = random.sample(names, 2)
    return a + b


def persist_enabled(cfg=None):
    e = _env("WB_SKY_FRIEND_NAME_PERSIST")
    if e is not None:
        return _boolish(e)
    cfg = cfg if cfg is not None else _load_config()
    v = cfg.get("friend_name_persist", True)
    if isinstance(v, str):
        return _boolish(v)
    return bool(v)


def pool_info(cfg=None):
    """给后台面板/排障用：池子在哪、有多少个、开关状态。"""
    cfg = cfg if cfg is not None else _load_config()
    path = pool_path(cfg)
    names = load_pool(cfg)
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    return {
        "enabled": enabled(cfg),
        "persist": persist_enabled(cfg),
        "length": name_len(cfg),
        "path": path,
        "file_exists": os.path.isfile(path),
        "size": size,
        "count": len(names),
        "sample": names[:12],
    }
