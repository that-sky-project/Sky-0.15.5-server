# -*- coding: utf-8 -*-
"""admin 面板的数据访问层 —— 全部走 wbsky 自己的裸 SQL（不依赖 SQLAlchemy）。

## 为什么不直接搬 skrJ2 的 admin

skrJ2（包2 / v4）的 admin 全程用 SQLAlchemy ORM：
`from models import User, UserCurrency, UnlockItem, Collectible, FriendStatus, ...`
表名是 `user` / `user_currency` / `unlock_item` / `friend_status`…，
而 wbsky 没有 models.py、没有 SQLAlchemy，数据分布也完全不同：

    wbsky 的存法                          对应 skrJ2 的
    users.unlocks     (TEXT/JSON 字符串数组)   unlock_item 表
    users.collects    (TEXT/JSON 数字数组)     collectible 表
    users.wing_buffs  (TEXT/JSON 字符串数组)   user_wing_buff 表
    users.achievements(TEXT/JSON)             achievement 表
    currency 表（一行一账号，11 列）           user_currency 表
    friends + friendships（两张表）            friend_status 一张表

所以这里**重写**数据层，只把 admin 的功能与交互保留下来。
每个函数都返回 dict / list[dict]，字段名尽量贴近 skrJ2，方便前端复用写法。

## 兼容性

所有 SQL 都用 `?` 占位符（db.py 会在 MySQL 下自动换成 %s），
`INSERT IGNORE` 通过 db.insert_ignore() 做后端适配，SQLite 与 MySQL 都能跑。
"""
import json
import os
import time

import db as database

# 货币列（与 db.CURRENCY_COLUMNS 一致，供前端展示中文名）
CURRENCY_FIELDS = [
    ("candles", "蜡烛"),
    ("hearts", "爱心"),
    ("heart_wax", "心蜡"),
    ("season_candle", "季节蜡烛"),
    ("season_heart", "季节心"),
    ("season_pass_token", "季卡代币"),
    ("wax", "烛光"),
    ("season_wax", "季节烛光"),
    ("prestige", "升华蜡烛"),
    ("prestige_wax", "升华烛光"),
]

JSON_ARRAY_FIELDS = ("unlocks", "collects", "wing_buffs")


# ---------------------------------------------------------------- 工具
def _parse_json(raw, default):
    if raw is None:
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        v = json.loads(raw)
        return v if v is not None else default
    except Exception:
        return default


def _as_list(raw):
    v = _parse_json(raw, [])
    if isinstance(v, list):
        return v
    if isinstance(v, dict) and "items" in v:
        return v.get("items") or []
    return []


def _now():
    return int(time.time())


def _entry(row, keys, field):
    """把一行 JSON 数组转成 [{name/id, ...}] 形式给前端。"""
    if field in ("unlocks", "wing_buffs"):
        return [str(x) for x in _as_list(row.get(field))]
    return list(_as_list(row.get(field)))


# ---------------------------------------------------------------- 总览
def overview():
    out = {"db": database.db_status(), "time": _now()}
    q = database.query_one
    try:
        out["users"] = (q("SELECT COUNT(*) AS n FROM users") or {}).get("n", 0)
    except Exception as e:
        out["users"] = None
        out["error"] = str(e)[:200]
    for key, sql in (
        ("friends", "SELECT COUNT(*) AS n FROM friends"),
        ("friendships", "SELECT COUNT(*) AS n FROM friendships"),
        ("currency_rows", "SELECT COUNT(*) AS n FROM currency"),
        ("pending_invites", "SELECT COUNT(*) AS n FROM pending_invites"),
        ("gift_messages", "SELECT COUNT(*) AS n FROM gift_messages"),
        ("chat_messages", "SELECT COUNT(*) AS n FROM chat_messages"),
        ("ct", "SELECT COUNT(*) AS n FROM friend_constellation_pages"),
    ):
        try:
            out[key] = (q(sql) or {}).get("n", 0)
        except Exception:
            out[key] = None
    try:
        cols = ", ".join("COALESCE(SUM(%s),0) AS %s" % (c, c)
                         for c, _ in CURRENCY_FIELDS)
        out["currency_sum"] = q("SELECT %s FROM currency" % cols) or {}
    except Exception:
        out["currency_sum"] = {}
    try:
        out["recent_users"] = database.query_all(
            "SELECT id, device_id, candles FROM users ORDER BY rowid DESC LIMIT 10") or []
    except Exception:
        # MySQL 没有 rowid：退回按 id 倒序（uuid 倒序，只是个稳定的顺序）
        try:
            out["recent_users"] = database.query_all(
                "SELECT id, device_id, candles FROM users ORDER BY id DESC LIMIT 10") or []
        except Exception:
            out["recent_users"] = []
    try:
        import friend_name as _fn
        out["name_pool"] = _fn.pool_info()
    except Exception:
        out["name_pool"] = {}
    # 表清单（排障用：一眼看出哪些表还没建）
    try:
        if database._db_config.get("use_mysql"):
            rows = database.query_all(
                "SELECT table_name AS t FROM information_schema.tables "
                "WHERE table_schema = DATABASE() ORDER BY table_name") or []
            out["tables"] = [r.get("t") for r in rows]
        else:
            rows = database.query_all(
                "SELECT name AS t FROM sqlite_master WHERE type='table' ORDER BY name") or []
            out["tables"] = [r.get("t") for r in rows]
    except Exception:
        out["tables"] = []
    return out


# ---------------------------------------------------------------- 用户
def list_users(keyword="", limit=50, offset=0, order="candles_desc"):
    """用户列表（分页）。

    ⚠️ wbsky 的 `users` 表**没有时间戳列**（建表语句里只有 id / device_id /
       recovery / candles / 各种 JSON 与装扮字段，没有 created_at）。
       所以这里**不能**按注册时间排序 —— 一开始我按 created_at 写，
       结果整个接口报错、列表永远是空的。
       可用的排序只有：蜡烛数、id（uuid，稳定但无时间含义）。
    """
    limit = max(1, min(int(limit or 50), 500))
    offset = max(0, int(offset or 0))
    where, params = "", []
    kw = (keyword or "").strip()
    if kw:
        # 三个字段都能搜：uuid / 设备号 / 存档码
        where = "WHERE id LIKE ? OR device_id LIKE ? OR recovery LIKE ?"
        like = "%" + kw + "%"
        params = [like, like, like]
    order_sql = {
        "candles_desc": "candles DESC, id ASC",
        "candles_asc": "candles ASC, id ASC",
        "id": "id ASC",
        "id_desc": "id DESC",
    }.get(order, "candles DESC, id ASC")
    try:
        total = (database.query_one("SELECT COUNT(*) AS n FROM users " + where, params) or {}).get("n", 0)
        rows = database.query_all(
            "SELECT id, device_id, recovery, candles, checkpoint, visited_home "
            "FROM users " + where + " ORDER BY " + order_sql + " LIMIT ? OFFSET ?",
            params + [limit, offset]) or []
    except Exception as e:
        return {"error": str(e)[:300], "total": 0, "rows": [], "limit": limit, "offset": offset}
    # 好友数：批量取，避免 N+1
    ids = [r.get("id") for r in rows if r.get("id")]
    fcount = {}
    if ids:
        marks = ",".join(["?"] * len(ids))
        try:
            for r in (database.query_all(
                    "SELECT user_id, COUNT(*) AS n FROM friends "
                    "WHERE user_id IN (%s) GROUP BY user_id" % marks, ids) or []):
                fcount[str(r.get("user_id"))] = r.get("n")
        except Exception:
            pass
    for r in rows:
        r["friends"] = fcount.get(str(r.get("id")), 0)
    return {"total": total, "rows": rows, "limit": limit, "offset": offset}


def get_user(uid):
    uid = str(uid or "").strip()
    if not uid:
        return None
    row = database.query_one("SELECT * FROM users WHERE id = ?", (uid,))
    if not row:
        return None
    u = dict(row)
    for f in JSON_ARRAY_FIELDS:
        u[f + "_list"] = len(_as_list(u.get(f)))
    u["achievements_list"] = len(_as_list(u.get("achievements")))
    u["user_data"] = _parse_json(u.get("user_data"), {})
    u["purchase"] = _parse_json(u.get("purchase"), [])
    try:
        u["currency"] = database.currency_get(uid)
    except Exception:
        u["currency"] = {}
    return u


def set_candles(uid, value):
    v = max(0, int(value))
    database.execute("UPDATE users SET candles = ? WHERE id = ?", (v, uid))
    return v


def set_currency(uid, field, value):
    col = str(field or "").strip()
    if col not in [c for c, _ in CURRENCY_FIELDS]:
        raise ValueError("不支持的货币字段: %s" % col)
    v = max(0, int(value))
    database.currency_get(uid)          # 没有就建行
    database.execute("UPDATE currency SET %s = ?, updated_at = ? WHERE user_id = ?"
                     % col, (v, _now(), uid))
    return v


def add_currency(uid, field, delta):
    col = str(field or "").strip()
    if col not in [c for c, _ in CURRENCY_FIELDS]:
        raise ValueError("不支持的货币字段: %s" % col)
    d = int(delta)
    database.currency_get(uid)
    database.execute(
        "UPDATE currency SET %s = COALESCE(%s,0) + ?, updated_at = ? WHERE user_id = ?"
        % (col, col), (d, _now(), uid))
    row = database.query_one("SELECT %s AS v FROM currency WHERE user_id = ?" % col, (uid,)) or {}
    return row.get("v")


def delete_user(uid):
    """删账号：连带清掉好友/关系/星座页/货币/收件箱。返回各表删除行数。"""
    uid = str(uid or "").strip()
    if not uid:
        return {}
    out = {}
    for t, sql in (
        ("friends", "DELETE FROM friends WHERE user_id = ? OR friend_id = ?"),
        ("friendships", "DELETE FROM friendships WHERE user_id = ? OR friend_id = ?"),
        ("friend_constellation_pages", "DELETE FROM friend_constellation_pages WHERE user_id = ?"),
        ("currency", "DELETE FROM currency WHERE user_id = ?"),
        ("chat_inbox", "DELETE FROM chat_inbox WHERE recipient = ? OR sender_id = ?"),
        ("pending_invites", "DELETE FROM pending_invites WHERE from_user = ? OR to_user = ?"),
        ("gift_messages", "DELETE FROM gift_messages WHERE from_user = ? OR to_user = ?"),
        ("users", "DELETE FROM users WHERE id = ?"),
    ):
        try:
            n = database.execute(sql, (uid, uid) if sql.count("?") == 2 else (uid,))
            out[t] = n
        except Exception as e:
            out[t] = "err: %s" % str(e)[:80]
    return out


# ---------------------------------------------------------------- 解锁 / 收集 / 光翼
def _get_raw(uid, field):
    return database.get_user_field(uid, field)


def _set_raw(uid, field, value_list):
    database.set_user_field(uid, field, json.dumps(value_list, ensure_ascii=False))


def list_unlocks(uid, keyword="", limit=2000):
    items = [str(x) for x in _as_list(_get_raw(uid, "unlocks"))]
    kw = (keyword or "").strip().lower()
    if kw:
        items = [x for x in items if kw in x.lower()]
    return {"count": len(items), "items": items[:limit]}


def add_unlocks(uid, names):
    cur = [str(x) for x in _as_list(_get_raw(uid, "unlocks"))]
    have = set(cur)
    added = []
    for n in (names or []):
        s = str(n or "").strip()
        if s and s not in have:
            have.add(s)
            cur.append(s)
            added.append(s)
    if added:
        _set_raw(uid, "unlocks", cur)
    return {"added": len(added), "total": len(cur), "items": added[:200]}


def remove_unlocks(uid, names):
    drop = {str(n or "").strip() for n in (names or []) if str(n or "").strip()}
    cur = [str(x) for x in _as_list(_get_raw(uid, "unlocks"))]
    keep = [x for x in cur if x not in drop]
    if len(keep) != len(cur):
        _set_raw(uid, "unlocks", keep)
    return {"removed": len(cur) - len(keep), "total": len(keep)}


def list_collects(uid, limit=3000):
    raw = _get_raw(uid, "collects")
    items = _as_list(raw)
    return {"count": len(items), "items": items[:limit]}


def add_collects(uid, ids):
    cur = _as_list(_get_raw(uid, "collects"))
    have = set(str(x) for x in cur)
    added = 0
    for i in (ids or []):
        key = str(i).strip()
        if not key:
            continue
        try:
            v = int(key)
        except ValueError:
            v = key
        if str(v) not in have:
            have.add(str(v))
            cur.append(v)
            added += 1
    if added:
        _set_raw(uid, "collects", cur)
    return {"added": added, "total": len(cur)}


def list_wing_buffs(uid):
    items = [str(x) for x in _as_list(_get_raw(uid, "wing_buffs"))]
    return {"count": len(items), "items": items}


def add_wing_buffs(uid, names):
    cur = [str(x) for x in _as_list(_get_raw(uid, "wing_buffs"))]
    have = set(cur)
    added = []
    for n in (names or []):
        s = str(n or "").strip()
        if s and s not in have:
            have.add(s)
            cur.append(s)
            added.append(s)
    if added:
        _set_raw(uid, "wing_buffs", cur)
    return {"added": len(added), "total": len(cur), "items": added[:200]}


def clear_collect(uid, field):
    """把 unlocks / collects / wing_buffs / achievements 清空。"""
    if field not in ("unlocks", "collects", "wing_buffs", "achievements"):
        raise ValueError("不支持清空的字段: %s" % field)
    _set_raw(uid, field, [])
    return True


# ---------------------------------------------------------------- 好友
def list_friends(uid):
    rows = database.query_all(
        "SELECT user_id, friend_id, nickname, level, created_at FROM friends "
        "WHERE user_id = ? ORDER BY created_at ASC", (uid,)) or []
    fr = {}
    try:
        for r in (database.query_all(
                "SELECT friend_id, custom_name, relationship_level, given, recvd, "
                "abilities, hints FROM friendships WHERE user_id = ?", (uid,)) or []):
            fr[str(r.get("friend_id"))] = r
    except Exception:
        pass
    out = []
    for r in rows:
        fid = str(r.get("friend_id"))
        f = fr.get(fid) or {}
        out.append({
            "friend_id": fid,
            "nickname": str(r.get("nickname") or "").strip(),
            "custom_name": str(f.get("custom_name") or "").strip(),
            "level": r.get("level"),
            "relationship_level": f.get("relationship_level"),
            "given": f.get("given"),
            "recvd": f.get("recvd"),
            "abilities": len(_as_list(f.get("abilities"))),
            "created_at": r.get("created_at"),
            "has_friendship": bool(f),
        })
    return out


def add_friend(a, b, nickname=None):
    """建立双向好友关系（friends + friendships + 星座页），与游戏内同一条逻辑。

    昵称没给就用名字池随机取一个（见 db.get_or_assign_friend_nickname）。
    """
    import friend_name as _fn
    a, b = str(a or "").strip(), str(b or "").strip()
    if not a or not b:
        raise ValueError("需要两个 user id")
    if a == b:
        raise ValueError("不能加自己")
    for u in (a, b):
        if not database.query_one("SELECT id FROM users WHERE id = ?", (u,)):
            raise ValueError("账号不存在: %s" % u)

    now = _now()
    made = []
    for me, other in ((a, b), (b, a)):
        row = database.query_one(
            "SELECT user_id, nickname FROM friends WHERE user_id = ? AND friend_id = ?",
            (me, other))
        if not row:
            nick = (nickname or "").strip() if me == a else ""
            if me == b and (nickname or "").strip():
                nick = (nickname or "").strip()      # 双向都给同一个名字，方便辨认
            if not nick:
                nick = database.get_or_assign_friend_nickname(me, other, "")
            database.execute(
                "INSERT INTO friends (user_id, friend_id, nickname, level, created_at) "
                "VALUES (?,?,?,?,?)", (me, other, nick, 0, now))
            made.append(me)
        else:
            # 已有行：只补空名字，不动用户自己起的名
            database.get_or_assign_friend_nickname(me, other, row.get("nickname"))
        # 关系表（满解锁那一档，与 _ensure_friendship 一致）
        try:
            database.upsert_friendship(me, other, full_unlock=True)
        except Exception:
            pass
        # 星座页（不加就没星星）
        try:
            database.constellation_ensure_batch(me, [other])
        except Exception:
            pass
    return {"ok": True, "created_rows": made}


def remove_friend(a, b):
    a, b = str(a or "").strip(), str(b or "").strip()
    out = {}
    for t, sql in (
        ("friends", "DELETE FROM friends WHERE (user_id = ? AND friend_id = ?) "
                    "OR (user_id = ? AND friend_id = ?)"),
        ("friendships", "DELETE FROM friendships WHERE (user_id = ? AND friend_id = ?) "
                        "OR (user_id = ? AND friend_id = ?)"),
    ):
        try:
            out[t] = database.execute(sql, (a, b, b, a))
        except Exception as e:
            out[t] = "err: %s" % str(e)[:80]
    # 星座页里也要摘掉
    removed = 0
    try:
        for me, other in ((a, b), (b, a)):
            row = database._constellation_row(me)
            if not row or not row.get("pages"):
                continue
            pages = _parse_json(row.get("pages"), None)
            if not isinstance(pages, list):
                continue
            changed = False
            for pg in pages:
                if not isinstance(pg, dict):
                    continue
                fr = pg.get("friends") or []
                keep = [x for x in fr if str((x or {}).get("friend_id")) != other]
                if len(keep) != len(fr):
                    pg["friends"] = keep
                    changed = True
            if changed:
                database._constellation_write(me, pages, row)
                removed += 1
    except Exception:
        pass
    out["constellation_updated"] = removed
    return out


def set_friend_nickname(uid, fid, nickname):
    nick = str(nickname or "").strip()[:190]
    n1 = database.execute("UPDATE friends SET nickname = ? WHERE user_id = ? AND friend_id = ?",
                          (nick, uid, fid))
    n2 = database.execute("UPDATE friendships SET custom_name = ? WHERE user_id = ? AND friend_id = ?",
                          (nick, uid, fid))
    return {"friends": n1, "friendships": n2, "nickname": nick}


def reroll_nickname(uid, fid):
    """强制重新随机一个名字（不管原来有没有）。"""
    import friend_name as _fn
    name = _fn.random_name()
    database.execute("UPDATE friends SET nickname = ? WHERE user_id = ? AND friend_id = ?",
                     (name, uid, fid))
    database.execute("UPDATE friendships SET custom_name = ? WHERE user_id = ? AND friend_id = ?",
                     (name, uid, fid))
    return {"nickname": name}


# ---------------------------------------------------------------- 社交动态
def list_feed(keyword="", limit=100):
    where, params = "", []
    kw = (keyword or "").strip()
    if kw:
        where = "WHERE user_id LIKE ? OR pool_name LIKE ? OR message LIKE ?"
        like = "%" + kw + "%"
        params = [like, like, like]
    try:
        rows = database.query_all(
            "SELECT id, social_feed_id, user_id, pool_type, pool_name, message, "
            "likes_count, comments_enabled, is_private, created_at, expire_at "
            "FROM social_feed_post " + where + " ORDER BY id DESC LIMIT ?",
            params + [max(1, min(int(limit or 100), 500))]) or []
    except Exception as e:
        return {"error": str(e)[:300], "rows": []}
    return {"rows": rows}


def delete_feed(post_id):
    return database.execute("DELETE FROM social_feed_post WHERE id = ?", (int(post_id),))


# ---------------------------------------------------------------- 其它
def list_pending_invites(limit=100):
    try:
        return database.query_all(
            "SELECT token_id, from_user, to_user, nickname, level_id, created_at, status "
            "FROM pending_invites ORDER BY created_at DESC LIMIT ?",
            (max(1, min(int(limit or 100), 500)),)) or []
    except Exception:
        return []


def delete_pending_invite(token_id):
    return database.execute("DELETE FROM pending_invites WHERE token_id = ?", (str(token_id),))


def db_health():
    """数据库探针 + 表清单（排障页用）。"""
    out = {"db": database.db_status()}
    try:
        if database._db_config.get("use_mysql"):
            rows = database.query_all(
                "SELECT table_name AS t, table_rows AS n FROM information_schema.tables "
                "WHERE table_schema = DATABASE() ORDER BY table_name") or []
        else:
            rows = database.query_all(
                "SELECT name AS t, 0 AS n FROM sqlite_master WHERE type='table' ORDER BY name") or []
        out["tables"] = rows
    except Exception as e:
        out["tables"] = []
        out["error"] = str(e)[:200]
    return out
