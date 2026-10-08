# db.py - 统一数据库模块
# 支持 MySQL/MariaDB（推荐用于 1Panel）和 SQLite（兼容旧版）
# 在 config.json 中配置 use_mysql: true 即可切换到 MySQL

import json
import os
import re
import sqlite3
import logging
import threading
import time

logger = logging.getLogger(__name__)

# 线程本地存储，每个线程一个连接
_local = threading.local()

_db_config = {
    "use_mysql": False,
    "mysql_host": "127.0.0.1",
    "mysql_port": 3306,
    "mysql_user": "root",
    "mysql_password": "",
    "mysql_database": "skygame",
    "mysql_charset": "utf8mb4",
    "sqlite_path": "",
}

_pymysql_available = False
try:
    import pymysql
    _pymysql_available = True
except ImportError:
    pass


def load_db_config():
    """从 config.json 加载数据库配置（环境变量优先，供 Docker 部署用）。

    ★ 2026-10：加了环境变量覆盖（WB_SKY_* / MYSQL_*）。
      原来只读 config.json，于是容器化部署时"改 .env 一处"根本做不到：
      config.json 是 bind mount 的宿主机文件，改它等于改线上配置，
      而且运行期改 JSON 会和后台面板的写操作打架。
      现在优先级：环境变量 > config.json > 内置默认值。
      不设任何环境变量时，行为与老版本**完全一致**。
    """
    global _db_config

    def _env(*names):
        for n in names:
            v = os.environ.get(n)
            if v is not None and str(v).strip() != "":
                return str(v).strip()
        return None

    def _env_bool(name):
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return None
        return str(v).strip().lower() not in ("0", "false", "no", "off")

    try:
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)

        # --- 是否走 MySQL：环境变量优先 ---
        env_mysql = _env_bool("WB_SKY_USE_MYSQL_DSN")
        if env_mysql is None:
            env_mysql = _env_bool("WB_SKY_USE_MYSQL")
        _db_config["use_mysql"] = (cfg.get("use_mysql", False)
                                   if env_mysql is None else env_mysql)

        def pick(env_names, cfg_key, default):
            v = _env(*env_names)
            if v is not None:
                return v
            return cfg.get(cfg_key, default)

        _db_config["mysql_host"] = pick(("MYSQL_HOST", "WB_SKY_MYSQL_HOST"),
                                        "mysql_host", "127.0.0.1")
        try:
            _db_config["mysql_port"] = int(pick(("MYSQL_PORT", "WB_SKY_MYSQL_PORT"),
                                                "mysql_port", 3306))
        except (TypeError, ValueError):
            _db_config["mysql_port"] = 3306
        _db_config["mysql_user"] = pick(("MYSQL_USER", "WB_SKY_MYSQL_USER"),
                                        "mysql_user", "root")
        _db_config["mysql_password"] = pick(("MYSQL_PASSWORD", "WB_SKY_MYSQL_PASSWORD"),
                                            "mysql_password", "")
        _db_config["mysql_database"] = pick(("MYSQL_DATABASE", "WB_SKY_MYSQL_DATABASE"),
                                            "mysql_database", "skygame")
        _db_config["mysql_charset"] = pick(("MYSQL_CHARSET", "WB_SKY_MYSQL_CHARSET"),
                                           "mysql_charset", "utf8mb4")

        db_dir = os.path.join(os.path.dirname(__file__), "db")
        _db_config["sqlite_path"] = os.path.join(db_dir, "users.db")

        if _db_config["use_mysql"]:
            if not _pymysql_available:
                logger.warning("MySQL 已启用但 pymysql 未安装，将回退到 SQLite")
                logger.warning("请执行: pip install pymysql")
                _db_config["use_mysql"] = False
            else:
                logger.info(f"数据库模式: MySQL ({_db_config['mysql_user']}@{_db_config['mysql_host']}:{_db_config['mysql_port']}/{_db_config['mysql_database']})")
        else:
            logger.info(f"数据库模式: SQLite ({_db_config['sqlite_path']})")

    except Exception as e:
        logger.warning(f"加载数据库配置失败，使用默认 SQLite: {e}")
        _db_config["use_mysql"] = False


def _get_mysql_conn():
    """获取 MySQL 连接（线程本地）"""
    conn = getattr(_local, 'mysql_conn', None)
    if conn is None:
        try:
            conn = pymysql.connect(
                host=_db_config["mysql_host"],
                port=_db_config["mysql_port"],
                user=_db_config["mysql_user"],
                password=_db_config["mysql_password"],
                database=_db_config["mysql_database"],
                charset=_db_config["mysql_charset"],
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=False
            )
            _local.mysql_conn = conn
        except Exception as e:
            logger.error(f"MySQL 连接失败: {e}")
            raise
    else:
        # 检查连接是否存活
        try:
            conn.ping(reconnect=True)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            conn = pymysql.connect(
                host=_db_config["mysql_host"],
                port=_db_config["mysql_port"],
                user=_db_config["mysql_user"],
                password=_db_config["mysql_password"],
                database=_db_config["mysql_database"],
                charset=_db_config["mysql_charset"],
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=False
            )
            _local.mysql_conn = conn
    return conn


def _get_sqlite_conn():
    """获取 SQLite 连接（线程本地）"""
    conn = getattr(_local, 'sqlite_conn', None)
    if conn is None:
        os.makedirs(os.path.dirname(_db_config["sqlite_path"]), exist_ok=True)
        conn = sqlite3.connect(_db_config["sqlite_path"])
        conn.row_factory = sqlite3.Row
        _local.sqlite_conn = conn
    return conn


# ============================================================
# ★ 2026-10：INSERT IGNORE 的后端适配
# ------------------------------------------------------------
# `INSERT IGNORE` 是 **MySQL 专有语法**，SQLite 下会报
#     sqlite3.OperationalError: near "IGNORE": syntax error
# 而 db.py 里有三处写死了它（currency_get /
# commerce_record / chat_inbox），于是 `use_mysql=0`（SQLite 兜底）时
# 这些路径全部失效（内购、聊天收件箱都会报错）。
# 现在统一走这个函数：按后端选对应语法，行为一致。
# ============================================================
def insert_ignore(sql, params=None):
    """执行一条 INSERT IGNORE（自动适配 MySQL / SQLite）。

    传入的 sql 用 `INSERT IGNORE INTO ...` 写法；这里会换成对应后端的语法。
    返回受影响行数（0 = 已存在被忽略）。
    """
    sql2 = sql
    if not _db_config.get("use_mysql"):
        sql2 = re.sub(r"\bINSERT\s+IGNORE\b", "INSERT OR IGNORE", sql, flags=re.I)
    return execute(sql2, params)


def md5_hex(text):
    """短摘要（admin 会话令牌用）。"""
    import hashlib
    return hashlib.md5(str(text).encode("utf-8")).hexdigest()


def get_conn():
    """获取数据库连接（根据配置自动选择 MySQL 或 SQLite）"""
    if _db_config["use_mysql"]:
        return _get_mysql_conn()
    else:
        return _get_sqlite_conn()


def execute(sql, params=None):
    """
    执行 SQL 语句（INSERT/UPDATE/DELETE）
    返回受影响的行数
    """
    conn = get_conn()
    cursor = _cursor_with_ddl_fix(conn)
    try:
        if _db_config["use_mysql"]:
            # MySQL 使用 %s 占位符
            mysql_sql = sql.replace("?", "%s")
            cursor.execute(mysql_sql, params or ())
        else:
            cursor.execute(sql, params or ())
        _rc_barrier()
        conn.commit()
        return cursor.rowcount
    except Exception as e:
        conn.rollback()
        raise
    finally:
        cursor.close()


def query_one(sql, params=None):
    """
    查询单行数据
    返回 dict 或 None
    """
    conn = get_conn()
    cursor = _cursor_with_ddl_fix(conn)
    try:
        if _db_config["use_mysql"]:
            mysql_sql = sql.replace("?", "%s")
            cursor.execute(mysql_sql, params or ())
        else:
            cursor.execute(sql, params or ())
        row = cursor.fetchone()
        if row is None:
            return None
        # MySQL 返回 dict，SQLite 返回 Row 对象，统一转 dict
        if _db_config["use_mysql"]:
            return row
        else:
            return dict(row)
    finally:
        cursor.close()


def query_all(sql, params=None):
    """
    查询多行数据
    返回 list[dict]
    """
    conn = get_conn()
    cursor = _cursor_with_ddl_fix(conn)
    try:
        if _db_config["use_mysql"]:
            mysql_sql = sql.replace("?", "%s")
            cursor.execute(mysql_sql, params or ())
        else:
            cursor.execute(sql, params or ())
        rows = cursor.fetchall()
        if _db_config["use_mysql"]:
            return rows
        else:
            return [dict(row) for row in rows]
    finally:
        cursor.close()


def init_db():
    """初始化数据库表结构"""
    if _db_config["use_mysql"]:
        _init_mysql()
    else:
        _init_sqlite()
    _init_social_tables()


# ============================================================
# ★ 2026-10：SQLite 兼容改写
# ------------------------------------------------------------
# 背景：_init_social_tables() 与内购表那两段 DDL 是按 MySQL 写的，
#       里面用了 SQLite **不认识** 的语法：
#         · BIGINT AUTO_INCREMENT PRIMARY KEY
#         · 表内联的 INDEX idx_xxx (col)
#         · 表内联的 UNIQUE KEY uniq_xxx (cols)
#       SQLite 会在第一条上就抛
#           sqlite3.OperationalError: near "INDEX": syntax error
#       于是 WB_SKY_USE_MYSQL=0（SQLite 兜底）时**一张表都建不出来**，
#       表现为"服务起来了但注册/登录全废"。
#       （MySQL 分支不受影响 —— use_mysql 那条路原样保留。）
#
# 改写规则（只对 SQLite 生效）：
#   BIGINT AUTO_INCREMENT PRIMARY KEY  ->  INTEGER PRIMARY KEY AUTOINCREMENT
#   UNIQUE KEY <name> (cols)           ->  UNIQUE (cols)
#   INDEX <name> (cols)                ->  删掉（SQLite 不支持内联索引）
# ============================================================
_SQLITE_DDL_FIXES = (
    (re.compile(r"\bBIGINT\s+AUTO_INCREMENT\s+PRIMARY\s+KEY\b", re.I),
     "INTEGER PRIMARY KEY AUTOINCREMENT"),
    (re.compile(r"\bUNIQUE\s+KEY\s+\w+\s*(\([^)]*\))", re.I), r"UNIQUE \1"),
    (re.compile(r"^[ \t]*\bINDEX\s+\w+\s*\([^)]*\)[ \t]*,?[ \t]*\n", re.I | re.M), ""),
)


def _ddl_for(sql):
    """按当前后端把 DDL 调整成该后端能吃的写法（MySQL 原样返回）。"""
    if _db_config["use_mysql"]:
        return sql
    out = sql
    for pat, rep in _SQLITE_DDL_FIXES:
        out = pat.sub(rep, out)
    # 上一行删掉后可能留下 ",）" 这种悬空逗号
    out = re.sub(r",[ \t]*\n([ \t]*)\)", r"\n\1)", out)
    return out


def _cursor_with_ddl_fix(conn):
    """包一层 cursor：DDL 自动按后端改写，其余 SQL 原样透传。

    这样只包建表那几段，**不碰任何业务 SQL**
    （业务 SQL 走 execute()/query_one()/query_all()，仍然直连原 cursor）。
    """
    if _db_config["use_mysql"]:
        return conn.cursor()

    class _Cur(object):
        def __init__(self, cur):
            self._cur = cur

        def execute(self, sql, params=None):
            if isinstance(sql, str) and "CREATE TABLE" in sql.upper():
                sql = _ddl_for(sql)
            if params is None:
                return self._cur.execute(sql)
            return self._cur.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._cur, name)

    return _Cur(conn.cursor())


def _init_social_tables():
    """★ 2026-10-03 新增：社交链路（加好友邀请 + 礼物消息）。

    背景：客户端有一整套社交接口，我们以前**一个都没实现**，全被 catch-all 兜成 `{}`，
    所以「给蜡烛后对方看不到接受按钮」。表结构只用 VARCHAR/BIGINT/INT ——
    MySQL 与 SQLite 都能建（TEXT 不能做 PRIMARY KEY / 不能有默认值）。
    """
    conn = get_conn()
    cursor = _cursor_with_ddl_fix(conn)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pending_invites (
            token_id   VARCHAR(64) PRIMARY KEY,
            from_user  VARCHAR(64) NOT NULL,
            to_user    VARCHAR(64) NOT NULL,
            nickname   VARCHAR(191) DEFAULT '',
            level_id   BIGINT DEFAULT 0,
            created_at BIGINT DEFAULT 0,
            status     VARCHAR(16) DEFAULT 'pending'
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS gift_messages (
            msg_id         VARCHAR(64) PRIMARY KEY,
            from_user      VARCHAR(64) NOT NULL,
            to_user        VARCHAR(64) NOT NULL,
            gift_type      INT DEFAULT 0,
            currency_type  INT DEFAULT 0,
            currency_count INT DEFAULT 0,
            raw_message    VARCHAR(255) DEFAULT '',
            sent_at        BIGINT DEFAULT 0,
            claimed        INT DEFAULT 0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS friends (
            user_id    VARCHAR(64) NOT NULL,
            friend_id  VARCHAR(64) NOT NULL,
            nickname   VARCHAR(191) DEFAULT '',
            level      INT DEFAULT 0,
            created_at BIGINT DEFAULT 0
        )
    ''')
    # ★ 2026-10-03 新增：游戏内聊天记录。
    #   字段对齐上游 xysky 的 chat_messages（已从真实生产库 dump 核对）：
    #   signature 形如 1/<sent_at>/<channel>/<message_id>/<hex>，
    #   sent_at_ms 是毫秒时间戳。客户端会带 channel 与 msg 上报。
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_messages (
            id          BIGINT AUTO_INCREMENT PRIMARY KEY,
            message_id  VARCHAR(64) NOT NULL,
            from_user_id VARCHAR(64) NOT NULL,
            to_user_id  VARCHAR(64) DEFAULT '0',
            level_id    VARCHAR(64) DEFAULT '',
            channel     VARCHAR(50) DEFAULT 'local',
            message     TEXT,
            sent_at     BIGINT DEFAULT 0,
            sent_at_ms  BIGINT DEFAULT 0,
            invalid     INT DEFAULT 0,
            signature   VARCHAR(255) DEFAULT NULL,
            recordable  INT DEFAULT 0,
            source_id   VARCHAR(64) DEFAULT NULL,
            INDEX idx_chat_level (level_id),
            INDEX idx_chat_from (from_user_id),
            INDEX idx_chat_sent (sent_at)
        )
    ''')
    # ★ 2026-10-03 新增：聊天投递收件箱。
    #   机制来自参考实现（SkyMoon / Windows端私服的 websocket_handler.py）：
    #   「客户端发聊天 -> 服务端写进每个用户的待发队列 -> 所有客户端轮询
    #     /account/get_pending_messages 取走」。这条链路才是聊天真正的投递方式，
    #   既不是 UDP 也不是 WebSocket（参考实现里那个函数名虽叫 broadcast_to_websocket，
    #   注释明确写着"使用HTTP轮询队列方式（绕过WebSocket）"）。
    #   这里用表而不是内存 dict：服务端重启不丢消息，且多进程也安全。
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_inbox (
            id          BIGINT AUTO_INCREMENT PRIMARY KEY,
            recipient   VARCHAR(64) NOT NULL,
            sender_id   VARCHAR(64) NOT NULL,
            msg_id      VARCHAR(64) NOT NULL,
            msg_type    VARCHAR(32) DEFAULT 'chat',
            msg         TEXT,
            ch          VARCHAR(50) DEFAULT 'local',
            sent_at_ms  BIGINT DEFAULT 0,
            delivered   INT DEFAULT 0,
            UNIQUE KEY uniq_chat_inbox (recipient, msg_id),
            INDEX idx_chat_inbox_recipient (recipient, delivered)
        )
    ''')
    # ★★ 2026-10-03 新增：好友关系表。
    #   表名与列名**照抄上游 xysky 生产库的 friendships 表**（用户提供的
    #   xysky_202609201918238pyl1.sql 转储，第 623 行）。真实库里一行的样子：
    #     user_id, friend_id, custom_name='ColorSky', relationship_level=65,
    #     abilities='[1,2,3,…]', hints='[6,8,9,…]', given=1, recvd=1, soft_deleted=0
    #   ⇒ 客户端不是只看"有没有你这行"，还要看 abilities / relationship_level，
    #     所以只往 friends 表塞一行是不够的。
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS friendships (
            id                 BIGINT AUTO_INCREMENT PRIMARY KEY,
            user_id            VARCHAR(64) NOT NULL,
            friend_id          VARCHAR(64) NOT NULL,
            custom_name        VARCHAR(191) DEFAULT '',
            relationship_level INT DEFAULT 0,
            abilities          TEXT,
            hints              TEXT,
            given              INT DEFAULT 0,
            recvd              INT DEFAULT 0,
            soft_deleted       INT DEFAULT 0,
            created_at         BIGINT DEFAULT 0,
            UNIQUE KEY uniq_friendship (user_id, friend_id),
            INDEX idx_friendships_user (user_id)
        )
    ''')
    # ★ 同上：好友星座页。上游表 friend_constellation_pages.pages 是一个 10 元素数组，
    #   每页形如 {"name":"","friends":[{"friend_id":"…","constellation_node_index":0}]}。
    #   客户端「星座图」上能不能看到这个好友，就看这里挂没挂上。
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS friend_constellation_pages (
            id         BIGINT AUTO_INCREMENT PRIMARY KEY,
            user_id    VARCHAR(64) NOT NULL,
            friend_id  VARCHAR(64) DEFAULT '',
            page_id    VARCHAR(64) DEFAULT '',
            page_data  TEXT,
            pages      TEXT,
            created_at BIGINT DEFAULT 0,
            updated_at BIGINT DEFAULT 0,
            UNIQUE KEY uniq_constellation_page (user_id, friend_id, page_id)
        )
    ''')
    _rc_barrier()
    conn.commit()
    try:
        cursor.close()
    except Exception:
        pass

    # ★★ 2026-10-04 新增：内购（IAP / commerce）。
    #   currency 列名对齐上游 xysky 生产库的同名表。
    #   客户端买东西走 /account/commerce/receipt，服务端按 config/iaplist.json
    #   里该商品的 currencyType/currencyCount + currencyType2/currencyCount2 往这里加。
    cursor = _cursor_with_ddl_fix(conn)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS currency (
            user_id            VARCHAR(64) NOT NULL PRIMARY KEY,
            candles            INT DEFAULT 0,
            hearts             INT DEFAULT 0,
            heart_wax          INT DEFAULT 0,
            season_candle      INT DEFAULT 0,
            season_heart       INT DEFAULT 0,
            season_pass_token  INT DEFAULT 0,
            wax                INT DEFAULT 0,
            season_wax         INT DEFAULT 0,
            prestige           INT DEFAULT 0,
            prestige_wax       INT DEFAULT 0,
            updated_at         BIGINT DEFAULT 0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS commerce_orders (
            id             BIGINT AUTO_INCREMENT PRIMARY KEY,
            user_id        VARCHAR(64) NOT NULL,
            product_id     VARCHAR(64) NOT NULL,
            transaction_id VARCHAR(128) NOT NULL,
            status         VARCHAR(16) DEFAULT 'verified',
            payload        TEXT,
            created_at     BIGINT DEFAULT 0,
            UNIQUE KEY uniq_commerce_tx (user_id, transaction_id),
            INDEX idx_commerce_user (user_id)
        )
    ''')
    _rc_barrier()
    conn.commit()
    try:
        cursor.close()
    except Exception:
        pass


# ---------------------------------------------------------------- 内购 / 货币
# config/iaplist.json 里的 currencyType -> currency 表列名
CURRENCY_COLUMNS = {
    "candles": "candles",
    "heart": "hearts",
    "hearts": "hearts",
    "heart_wax": "heart_wax",
    "season_candle": "season_candle",
    "season_heart": "season_heart",
    "season_pass_token": "season_pass_token",
    "wax": "wax",
    "season_wax": "season_wax",
    "prestige": "prestige",
    "prestige_wax": "prestige_wax",
}


def currency_get(user_id):
    """取该账号的货币字典（没有就建一行）。"""
    try:
        row = query_one("SELECT * FROM currency WHERE user_id = ?", (user_id,))
        if not row:
            insert_ignore("INSERT IGNORE INTO currency (user_id, updated_at) VALUES (?,?)",
                    (user_id, int(time.time())))
            row = query_one("SELECT * FROM currency WHERE user_id = ?", (user_id,)) or {}
        return row
    except Exception as e:
        logger.warning(f"[内购] 读 currency 失败: {e}")
        return {}


def currency_add(user_id, ctype, count):
    """给某账号加一种货币。返回 (是否成功, 加后的值)。"""
    col = CURRENCY_COLUMNS.get(str(ctype or "").lower())
    try:
        count = int(count or 0)
    except (TypeError, ValueError):
        count = 0
    if not col or count == 0:
        return False, None
    try:
        currency_get(user_id)
        execute("UPDATE currency SET %s = COALESCE(%s,0) + ?, updated_at = ? WHERE user_id = ?"
                % (col, col), (count, int(time.time()), user_id))
        row = query_one("SELECT %s AS v FROM currency WHERE user_id = ?" % col, (user_id,)) or {}
        return True, row.get("v")
    except Exception as e:
        logger.warning(f"[内购] 加货币失败 {ctype}: {e}")
        return False, None


def commerce_record(user_id, product_id, transaction_id, status="verified", payload=""):
    """记一笔内购订单。返回 True = 这是一笔**新**订单（可以发放奖励）。

    ★ 用 INSERT IGNORE 的受影响行数判断，不能再靠"查得到行"判断 ——
      那样重复提交同一 transaction_id 会一直被当成新订单，导致重复发货
      （2026-10-04 实测：同一凭据提交两次，season_candle 30→60→90）。
    """
    try:
        n = insert_ignore("INSERT IGNORE INTO commerce_orders "
                    "(user_id, product_id, transaction_id, status, payload, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (user_id, product_id, transaction_id, status, str(payload)[:2000],
                     int(time.time())))
        return bool(n)
    except Exception as e:
        logger.warning(f"[内购] 记订单失败: {e}")
        return False


def commerce_orders_of(user_id, status=None):
    try:
        if status:
            return query_all("SELECT * FROM commerce_orders WHERE user_id = ? AND status = ? "
                             "ORDER BY id ASC", (user_id, status)) or []
        return query_all("SELECT * FROM commerce_orders WHERE user_id = ? ORDER BY id ASC",
                         (user_id,)) or []
    except Exception as e:
        logger.warning(f"[内购] 查订单失败: {e}")
        return []


def commerce_set_status(user_id, transaction_id, status):
    try:
        execute("UPDATE commerce_orders SET status = ? WHERE user_id = ? AND transaction_id = ?",
                (status, user_id, transaction_id))
        return True
    except Exception as e:
        logger.warning(f"[内购] 改订单状态失败: {e}")
        return False


# ---------------------------------------------------------------- 好友关系
# 上游 xysky 生产库里 friendships.abilities 的常见取值（满解锁那一档）。
# 客户端好友树用这些 id 决定哪些互动已解锁，缺了会表现为"好友关系没生效"。
DEFAULT_FRIEND_ABILITIES = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 19, 26, 27, 33, 35, 36, 37, 38, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 78, 79, 80, 81, 82, 83, 84, 85, 86, 87, 90, 91, 92, 93, 94, 95, 96, 97, 100, 101, 102, 103, 104, 252, 253, 254]
# 上游 xysky 生产库里 friendships.hints 的常见取值（满解锁那一档）。
# hints 是"这个关系节点已经提示过/已解锁"的标记列表，客户端好友树的红点与
# "必须先解锁前置节点"(relationship_unlock_required) 判定都会看它。
DEFAULT_FRIEND_HINTS = [6, 8, 9, 10, 19, 26, 27, 34, 35, 36, 37, 38, 40, 41, 42, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 56, 57, 58, 59, 60, 61, 62, 63, 64, 67, 68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 78, 80, 82, 83, 84, 86, 87, 90, 91, 92, 93, 94, 95, 96, 97, 100, 103, 104, 252, 253, 254]
# 满解锁关系等级（上游满解锁那行是 65）
FULL_RELATIONSHIP_LEVEL = 65
# 客户端 kAccountFriendPageMaxCount = 10
CONSTELLATION_PAGE_COUNT = 10


def empty_constellation_pages():
    """一份空的好友星座页（10 页）。"""
    return json.dumps([{"name": "", "friends": []}
                       for _ in range(CONSTELLATION_PAGE_COUNT)], ensure_ascii=False)


# ---------------------------------------------------------------- 好友默认昵称
# ★ 2026-10：没名字的好友不再回退成"对方 uuid 前 8 位"（`a3f9c2b1` 这种东西），
#   改成从 wbsky/config/friend_name_pool.json（528 条，用户提供）里**随机取一个**，
#   并且**落库**，所以刷新/重登/换设备都是同一个名字。
#
# 为什么落库而不是每次现算：
#   名字是客户端拿来渲染好友列表的，同一个人每次刷新都换名字会很难受；
#   而且用户需求原话就是"没名字的好友自动随机取一个"——取一次就定下来。
#
# 三处存储都写（与 _ensure_friendship 保持一致）：
#   friends.nickname            —— 客户端 /account/get_friends 直接读这个
#   friendships.custom_name     —— 好友树/关系接口读这个
#   （另一方向的 friends 行不写：那是"对方给我起的名字"，不该被我这边覆盖）
def _stored_friend_nickname(user_id, friend_id):
    """读库里已经记下的好友名字（friends.nickname 优先，再看 friendships.custom_name）。

    ★ 必须先读的原因：本函数是"取该好友对我显示的名字"的唯一入口，
      每次接口调用都会走到。如果上来就 random 一个，同一个好友**每次刷新都换名字**
      （实测就是这么错的：第一次「夜」、第二次「澹」）。
      读库这一步顺带也保证了"已经有名字的好友不会被改"。
    """
    try:
        row = query_one("SELECT nickname FROM friends WHERE user_id = ? AND friend_id = ?",
                        (user_id, friend_id))
        cur = str((row or {}).get("nickname") or "").strip()
        if cur:
            return cur
    except Exception:
        pass
    try:
        row = query_one("SELECT custom_name FROM friendships WHERE user_id = ? AND friend_id = ?",
                        (user_id, friend_id))
        return str((row or {}).get("custom_name") or "").strip()
    except Exception:
        return ""


def get_or_assign_friend_nickname(user_id, friend_id, current=None, persist=True):
    """取该好友对我显示的名字；没有就随机分配一个并落库。

    current 传调用方已经读到的名字（省一次查库）。
    任何异常都退回 current 或 uuid 前 8 位 —— 昵称问题**绝不能**影响好友接口。
    """
    fid = str(friend_id or "")
    cur = str(current or "").strip()
    if cur:
        return cur
    if not fid:
        return ""
    # ★ 先查库：已经有名字就直接回，绝不重新随机
    cur = _stored_friend_nickname(user_id, fid)
    if cur:
        return cur

    fallback = fid[:8]
    try:
        import friend_name as _fn
        if not _fn.enabled():
            return fallback
        name = _fn.random_name()
        if not name:
            return fallback
    except Exception as e:
        logger.warning("[好友] 生成随机昵称失败，退回 uuid 前 8 位: %r" % (e,))
        return fallback

    if not persist:
        return name
    try:
        import friend_name as _fn
        persist = _fn.persist_enabled()
    except Exception:
        persist = True

    if persist and user_id:
        try:
            # ★ WHERE 里带 `nickname = '' OR nickname IS NULL`：
            #   并发/重复调用时不会把别人刚写好的名字覆盖掉（幂等）。
            execute("UPDATE friends SET nickname = ? "
                    "WHERE user_id = ? AND friend_id = ? "
                    "AND (nickname IS NULL OR nickname = '')",
                    (name, user_id, fid))
            # friendships.custom_name 是"关系表"里的名字，同样只在空的时候补
            execute("UPDATE friendships SET custom_name = ? "
                    "WHERE user_id = ? AND friend_id = ? "
                    "AND (custom_name IS NULL OR custom_name = '')",
                    (name, user_id, fid))
            # ★ 并发兜底：两个请求同时进来会各随机一个、各写一次；
            #   后写的被 WHERE 挡住 → 这里回读一次，回**库里真正生效的那个**，
            #   保证"同一好友同一时刻只有一个名字"。
            real = _stored_friend_nickname(user_id, fid)
            if real:
                name = real
        except Exception as e:
            logger.warning("[好友] 写随机昵称失败（不影响接口）: %r" % (e,))
    return name


def get_friendship(user_id, friend_id):
    try:
        return query_one(
            "SELECT * FROM friendships WHERE user_id = ? AND friend_id = ?",
            (user_id, friend_id))
    except Exception as e:
        logger.warning(f"[好友] 读 friendships 失败: {e}")
        return None


def upsert_friendship(user_id, friend_id, ability_id=None, custom_name=None,
                      relationship_level=None, given=None, recvd=None,
                      full_unlock=None):
    """写入/更新一条好友关系（幂等，只升级不降级）。

    ★ 2026-10-03 二次修复：用户反馈「一边是完整的，一边还是未解锁」。
      原因是**两个方向写得不一致**：给蜡烛那一侧 given=1/recvd=0，另一侧
      given=0/recvd=1。客户端按 given 判"我有没有为这段关系付出过"，
      于是有一侧永远认为前置节点没解锁（界面提示 relationship_unlock_required
      =「你必须先解锁前置节点」）。
      现在两端都写 given=1/recvd=1（上游满解锁行也是 given=1, recvd=1），
      并且 abilities / hints 都补齐到满解锁列表。
    """
    if not user_id or not friend_id or user_id == friend_id:
        return False
    now = int(time.time())
    if full_unlock is None:
        full_unlock = True
    lvl = int(relationship_level if relationship_level is not None
              else (FULL_RELATIONSHIP_LEVEL if full_unlock else 1))
    g = 1 if (given is None and full_unlock) else int(given or 0)
    r = 1 if (recvd is None and full_unlock) else int(recvd or 0)
    try:
        row = get_friendship(user_id, friend_id)
        if not row:
            execute(
                "INSERT INTO friendships (user_id, friend_id, custom_name, "
                "relationship_level, abilities, hints, given, recvd, soft_deleted, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,0,?)",
                (user_id, friend_id, custom_name or "", lvl,
                 # ★ full_unlock=False 时只写这次买到的那一个节点（正常玩法），
                 #   不能再无脑塞满 DEFAULT_FRIEND_ABILITIES。
                 json.dumps(sorted(set(DEFAULT_FRIEND_ABILITIES)) if full_unlock
                            else ([int(ability_id)] if ability_id is not None else [])),
                 json.dumps(DEFAULT_FRIEND_HINTS if full_unlock else []), g, r, now))
            return True
        sets, params = [], []
        if ability_id is not None:
            try:
                cur = json.loads(row.get("abilities") or "[]")
            except Exception:
                cur = []
            if ability_id not in cur:
                cur.append(ability_id)
            # 满解锁模式下直接写整份列表，避免"有的节点没解锁"
            if full_unlock:
                cur = sorted(set(cur) | set(DEFAULT_FRIEND_ABILITIES))
            sets.append("abilities = ?")
            params.append(json.dumps(cur))
        elif full_unlock:
            try:
                cur = json.loads(row.get("abilities") or "[]")
            except Exception:
                cur = []
            if not set(DEFAULT_FRIEND_ABILITIES).issubset(set(cur)):
                sets.append("abilities = ?")
                params.append(json.dumps(sorted(set(cur) | set(DEFAULT_FRIEND_ABILITIES))))
        if full_unlock:
            try:
                ch = json.loads(row.get("hints") or "[]")
            except Exception:
                ch = []
            if not set(DEFAULT_FRIEND_HINTS).issubset(set(ch)):
                sets.append("hints = ?")
                params.append(json.dumps(sorted(set(ch) | set(DEFAULT_FRIEND_HINTS))))
            sets.append("given = 1")
            sets.append("recvd = 1")
            sets.append("relationship_level = GREATEST(COALESCE(relationship_level,0), ?)")
            params.append(lvl)
        else:
            if relationship_level is not None:
                sets.append("relationship_level = GREATEST(COALESCE(relationship_level,0), ?)")
                params.append(lvl)
            if given:
                sets.append("given = 1")
            if recvd:
                sets.append("recvd = 1")
        if custom_name:
            sets.append("custom_name = ?")
            params.append(custom_name)
        if not sets:
            return True
        params.extend([user_id, friend_id])
        execute("UPDATE friendships SET " + ", ".join(sets) +
                " WHERE user_id = ? AND friend_id = ?", tuple(params))
        return True
    except Exception as e:
        logger.warning(f"[好友] upsert_friendship 失败: {e}")
        return False


def add_to_constellation(user_id, friend_id, node_index=None):
    """把好友挂进星座页；已在页里就不重复挂。

    返回该好友的 constellation_node_index（挂不上返回 -1）。
    """
    if not user_id or not friend_id:
        return -1
    now = int(time.time())
    try:
        row = query_one(
            "SELECT pages FROM friend_constellation_pages "
            "WHERE user_id = ? ORDER BY id ASC LIMIT 1", (user_id,))
        if row and row.get("pages"):
            try:
                pages = json.loads(row["pages"])
            except Exception:
                pages = None
        else:
            pages = None
        if not isinstance(pages, list) or len(pages) != CONSTELLATION_PAGE_COUNT:
            pages = [{"name": "", "friends": []} for _ in range(CONSTELLATION_PAGE_COUNT)]

        for page in pages:
            for f in page.get("friends") or []:
                if f.get("friend_id") == friend_id:
                    return int(f.get("constellation_node_index") or 0)

        used = {int(f.get("constellation_node_index") or 0)
                for page in pages for f in (page.get("friends") or [])}
        idx = 0
        if node_index is None:
            while idx in used:
                idx += 1
        else:
            idx = int(node_index)
        pages[0].setdefault("friends", []).append(
            {"friend_id": friend_id, "constellation_node_index": idx})
        blob = json.dumps(pages, ensure_ascii=False)

        if row:
            execute("UPDATE friend_constellation_pages SET pages = ?, updated_at = ? "
                    "WHERE user_id = ?", (blob, now, user_id))
        else:
            execute("INSERT INTO friend_constellation_pages "
                    "(user_id, friend_id, page_id, page_data, pages, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (user_id, "", "", "", blob, now, now))
        return idx
    except Exception as e:
        logger.warning(f"[好友] 写星座页失败: {e}")
        return -1


def query_all(sql, params=None):
    """查询多行，返回 list[dict]。"""
    conn = get_conn()
    cursor = _cursor_with_ddl_fix(conn)
    try:
        if _db_config["use_mysql"]:
            cursor.execute(sql.replace("?", "%s"), params or ())
        else:
            cursor.execute(sql, params or ())
        rows = cursor.fetchall()
        out = []
        for r in rows:
            out.append(r if _db_config["use_mysql"] else dict(r))
        return out
    finally:
        try:
            cursor.close()
        except Exception:
            pass


def _init_sqlite():
    """初始化 SQLite 数据库表"""
    conn = _get_sqlite_conn()
    cursor = _cursor_with_ddl_fix(conn)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id         TEXT PRIMARY KEY,
            device_id  TEXT NOT NULL,
            recovery   TEXT NOT NULL,
            achievements   TEXT DEFAULT NULL,
            candles    INTEGER NOT NULL DEFAULT 3,
            unlocks    TEXT DEFAULT NULL,
            collects   TEXT DEFAULT NULL,
            wing_buffs TEXT DEFAULT NULL,
            checkpoint INTEGER DEFAULT NULL,
            visited_home INTEGER DEFAULT 0,
            outfit_wing   INTEGER DEFAULT 0,
            outfit_prop   INTEGER DEFAULT 0,
            outfit_face   INTEGER DEFAULT 0,
            outfit_mask   INTEGER DEFAULT 0,
            outfit_hair   INTEGER DEFAULT 0,
            outfit_body   INTEGER DEFAULT 0,
            outfit_arms   INTEGER DEFAULT 0,
            outfit_feet   INTEGER DEFAULT 0,
            outfit_hat    INTEGER DEFAULT 0,
            outfit_horn   INTEGER DEFAULT 0,
            outfit_neck   INTEGER DEFAULT 0,
            outfit_height REAL DEFAULT 0,
            purchase  TEXT DEFAULT NULL,
            user_data TEXT DEFAULT NULL
        )
    ''')
    _rc_barrier()
    conn.commit()
    # 迁移：老库补 visited_home 列（记录账号是否到过遇境 CandleSpace）
    cols = [r['name'] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "visited_home" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN visited_home INTEGER DEFAULT 0")
        _rc_barrier()
        conn.commit()
    # 卡密表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS license_keys (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            key_code     TEXT NOT NULL UNIQUE,
            note         TEXT DEFAULT '',
            duration_h   INTEGER NOT NULL DEFAULT 0,
            created_at   INTEGER NOT NULL DEFAULT 0,
            activated_at INTEGER NOT NULL DEFAULT 0,
            expire_at    INTEGER NOT NULL DEFAULT 0,
            device_id    TEXT DEFAULT '',
            last_ip      TEXT DEFAULT '',
            last_seen    INTEGER NOT NULL DEFAULT 0,
            use_count    INTEGER NOT NULL DEFAULT 0,
            status       TEXT DEFAULT 'unused'
        )
    ''')
    _rc_barrier()
    conn.commit()
    cursor.close()


def _init_mysql():
    """初始化 MySQL 数据库表"""
    conn = _get_mysql_conn()
    cursor = _cursor_with_ddl_fix(conn)

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id VARCHAR(64) PRIMARY KEY,
            device_id VARCHAR(64) NOT NULL,
            recovery VARCHAR(128) NOT NULL,
            achievements TEXT DEFAULT NULL,
            candles INT NOT NULL DEFAULT 3,
            unlocks TEXT DEFAULT NULL,
            collects TEXT DEFAULT NULL,
            wing_buffs TEXT DEFAULT NULL,
            checkpoint BIGINT DEFAULT NULL,
            visited_home INT DEFAULT 0,
            outfit_wing BIGINT DEFAULT 0,
            outfit_prop BIGINT DEFAULT 0,
            outfit_face BIGINT DEFAULT 0,
            outfit_mask BIGINT DEFAULT 0,
            outfit_hair BIGINT DEFAULT 0,
            outfit_body BIGINT DEFAULT 0,
            outfit_arms BIGINT DEFAULT 0,
            outfit_feet BIGINT DEFAULT 0,
            outfit_hat BIGINT DEFAULT 0,
            outfit_horn BIGINT DEFAULT 0,
            outfit_neck BIGINT DEFAULT 0,
            outfit_height FLOAT DEFAULT 0,
            purchase TEXT DEFAULT NULL,
            user_data TEXT DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_device_id (device_id),
            INDEX idx_checkpoint (checkpoint)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    ''')
    _rc_barrier()
    conn.commit()
    # 迁移：老库补 visited_home 列
    row = query_one("SELECT COUNT(*) AS cnt FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users' AND COLUMN_NAME = 'visited_home'")
    if not row or row.get("cnt") == 0:
        cursor.execute("ALTER TABLE users ADD COLUMN visited_home INT DEFAULT 0")
        _rc_barrier()
        conn.commit()

    # 卡密表（鼓启私服 卡密系统）
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS license_keys (
            id INT AUTO_INCREMENT PRIMARY KEY,
            key_code VARCHAR(64) NOT NULL UNIQUE,
            note VARCHAR(191) DEFAULT '',
            duration_h INT NOT NULL DEFAULT 0,
            created_at BIGINT NOT NULL DEFAULT 0,
            activated_at BIGINT NOT NULL DEFAULT 0,
            expire_at BIGINT NOT NULL DEFAULT 0,
            device_id VARCHAR(64) DEFAULT '',
            last_ip VARCHAR(64) DEFAULT '',
            last_seen BIGINT NOT NULL DEFAULT 0,
            use_count INT NOT NULL DEFAULT 0,
            status VARCHAR(16) DEFAULT 'unused',
            INDEX idx_status (status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    ''')
    _rc_barrier()
    conn.commit()
    cursor.close()


# ---------------------------------------------------------------- 聊天投递

def enqueue_chat_message(recipients, sender_id, msg_id, msg, ch="local",
                         msg_type="chat", sent_at_ms=0):
    """把一条聊天消息投进若干收件人的收件箱（幂等：主键 recipient+msg_id 去重）。"""
    n = 0
    for r in recipients or []:
        try:
            insert_ignore(
                "INSERT IGNORE INTO chat_inbox(recipient,sender_id,msg_id,msg_type,"
                "msg,ch,sent_at_ms,delivered) VALUES (?,?,?,?,?,?,?,0)",
                (r, sender_id, msg_id, msg_type, msg, ch, sent_at_ms))
            n += 1
        except Exception:
            # insert_ignore 已经做过后端适配；走到这里说明是真的写失败
            # （表不存在 / 字段不匹配），不再退回普通插入（那会把重复当成成功）
            logger.warning("[聊天] 写收件箱失败 recipient=%s: %r" % (r, "see above"))
    return n


def take_chat_inbox(recipient, limit=100):
    """取走某收件人的待发聊天消息（取走即标记已发，等同参考实现的 del queue[user]）。"""
    if not recipient:
        return []
    try:
        limit = max(1, min(int(limit), 500))
    except Exception:
        limit = 100
    try:
        rows = query_all(
            "SELECT msg_id,sender_id,msg_type,msg,ch,sent_at_ms FROM chat_inbox "
            "WHERE recipient = ? AND delivered = 0 ORDER BY id ASC LIMIT " + str(limit),
            (recipient,))
    except Exception:
        return []
    if not rows:
        return []
    ids = [r.get("msg_id") for r in rows if r.get("msg_id")]
    for mid in ids:
        try:
            execute("UPDATE chat_inbox SET delivered = 1 WHERE recipient = ? AND msg_id = ?",
                    (recipient, mid))
        except Exception:
            pass
    return rows


def user_exists(user_id):
    """检查用户是否存在"""
    row = query_one("SELECT 1 FROM users WHERE id = ?", (user_id,))
    return row is not None


# ---------------------------------------------------------------- 聊天记录

def save_chat_message(message_id, from_user, message, channel="local",
                      level_id="", to_user="0", sent_at=0, sent_at_ms=0,
                      signature=None, invalid=0, recordable=0, source_id=None):
    """写入一条聊天记录。字段与上游 xysky 的 chat_messages 对齐。"""
    return execute(
        "INSERT INTO chat_messages(message_id,from_user_id,to_user_id,level_id,"
        "channel,message,sent_at,sent_at_ms,invalid,signature,recordable,source_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (message_id, from_user, to_user, level_id, channel, message,
         sent_at, sent_at_ms, invalid, signature, recordable, source_id)
    )


def recent_chat_messages(level_id=None, channel=None, limit=50, since_ms=0):
    """取最近聊天记录（用于回补/排障）。"""
    sql = "SELECT * FROM chat_messages WHERE sent_at_ms >= ?"
    params = [since_ms or 0]
    if level_id:
        sql += " AND level_id = ?"
        params.append(level_id)
    if channel:
        sql += " AND channel = ?"
        params.append(channel)
    try:
        limit = max(1, min(int(limit), 500))
    except Exception:
        limit = 50
    sql += " ORDER BY id DESC LIMIT " + str(limit)
    try:
        return query_all(sql, tuple(params)) or []
    except Exception:
        return []


def create_user(user_id, device_id, recovery):
    """创建新用户（★ 2026-10-06：建号即按 config.json 的 new_user_candles 发蜡烛）"""
    execute(
        "INSERT INTO users(id, device_id, recovery) VALUES (?, ?, ?)",
        (user_id, device_id, recovery)
    )
    try:
        import json as _json
        _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        with open(_p, encoding="utf-8-sig") as _f:
            _n = int((_json.load(_f) or {}).get("new_user_candles") or 0)
        if _n:
            execute("UPDATE users SET candles = ? WHERE id = ?", (_n, user_id))
    except Exception as _e:
        try:
            logger.warning("[建号] 发初始蜡烛失败 %s: %r" % (user_id, _e))
        except Exception:
            pass


def get_user_field(user_id, field):
    """获取用户单个字段"""
    row = query_one(f"SELECT {field} FROM users WHERE id = ?", (user_id,))
    if row is None:
        return None
    return row.get(field)


def set_user_field(user_id, field, value):
    """设置用户单个字段"""
    return execute(f"UPDATE users SET {field} = ? WHERE id = ?", (value, user_id))


def db_status():
    """给 /healthz 用的数据库探针（只读，不建表、不改数据）。

    为什么需要它：sitecustomize 打开了「连不上库也继续启动」的容错，
    于是"服务活着"不再等于"数据库通了"。容器编排 / 1Panel 需要一个
    明确的信号来判断到底哪一层坏了，否则只能等玩家报错。
    """
    mode = "mysql" if _db_config.get("use_mysql") else "sqlite"
    info = {
        "ok": False,
        "mode": mode,
        "host": _db_config.get("mysql_host") if mode == "mysql" else _db_config.get("sqlite_path"),
        "database": _db_config.get("mysql_database") if mode == "mysql" else None,
    }
    try:
        row = query_one("SELECT 1 AS ok")
        info["ok"] = bool(row is not None)
    except Exception as e:
        info["error"] = str(e).split("\n")[0][:200]
    return info


# 模块加载时自动初始化配置
load_db_config()


# ============================================================
# p89: 请求级 SQL 去重缓存（见 /root/p89_req_cache.py 的说明）
#   只对**读**做去重；任何写（conn.commit() 前）都会清空本次请求的缓存。
# ============================================================
_req_cache = threading.local()
_rc_query_one = query_one      # 包住最终生效的那份定义（本块位于文件末尾）
_rc_query_all = query_all


def begin_request_cache():
    """一次 HTTP 请求开始时调用（index.py 的 before_request）。"""
    _req_cache.data = {}


def end_request_cache():
    """请求结束时调用（teardown_request）。"""
    try:
        del _req_cache.data
    except AttributeError:
        pass


def _rc_barrier():
    """写操作屏障：本次请求的缓存全部作废。"""
    d = getattr(_req_cache, "data", None)
    if d is not None:
        d.clear()


def _rc_key(sql, params):
    return (sql, repr(params))


def query_one(sql, params=None):
    d = getattr(_req_cache, "data", None)
    if d is None:
        return _rc_query_one(sql, params)
    k = _rc_key(sql, params)
    if k in d:
        v = d[k]
        return dict(v) if v else None
    v = _rc_query_one(sql, params)
    d[k] = dict(v) if v else None
    return dict(v) if v else None


def query_all(sql, params=None):
    d = getattr(_req_cache, "data", None)
    if d is None:
        return _rc_query_all(sql, params)
    k = _rc_key(sql, params)
    if k in d:
        return [dict(r) for r in d[k]]
    rows = _rc_query_all(sql, params) or []
    d[k] = [dict(r) for r in rows]
    return [dict(r) for r in d[k]]


# ============================================================
# p90: 批量版好友查询（见 /root/p90_batch_friends.py）
#   把"每个好友一次查询"变成"一次 IN (...) 查询"，语义与逐条调用一致。
# ============================================================
def get_friendships_batch(user_id, friend_ids):
    """一次取回 user 与这批好友的关系行：{friend_id: row}。"""
    out = {}
    ids = [str(f) for f in (friend_ids or []) if f]
    if not user_id or not ids:
        return out
    marks = ",".join(["?"] * len(ids))
    try:
        rows = query_all(
            "SELECT * FROM friendships WHERE user_id = ? AND friend_id IN (%s)" % marks,
            tuple([user_id] + ids)) or []
    except Exception as e:
        logger.warning(f"[好友] 批量读 friendships 失败: {e}")
        return out
    for r in rows:
        fid = str(r.get("friend_id") or "")
        if fid:
            out[fid] = r
    return out


def constellation_ensure_batch(user_id, friend_ids):
    """确保这批好友都在星座页里，返回 {friend_id: node_index}。

    与 add_to_constellation 的行为一致（同一份 pages JSON 结构、同样的索引分配），
    区别是**只读一次、最多写一次**（原来是每个好友读一次，缺人时还要各写一次）。
    """
    out = {}
    ids = []
    for f in (friend_ids or []):
        f = str(f or "")
        if f and f not in ids:
            ids.append(f)
    if not user_id or not ids:
        return out
    try:
        row = query_one(
            "SELECT pages FROM friend_constellation_pages "
            "WHERE user_id = ? ORDER BY id ASC LIMIT 1", (user_id,))
        pages = None
        if row and row.get("pages"):
            try:
                pages = json.loads(row["pages"])
            except Exception:
                pages = None
        if not isinstance(pages, list) or len(pages) != CONSTELLATION_PAGE_COUNT:
            pages = [{"name": "", "friends": []} for _ in range(CONSTELLATION_PAGE_COUNT)]

        used = set()
        for page in pages:
            for f in (page.get("friends") or []):
                fid = str(f.get("friend_id") or "")
                idx = int(f.get("constellation_node_index") or 0)
                used.add(idx)
                if fid and fid not in out:
                    out[fid] = idx

        missing = [f for f in ids if f not in out]
        if missing:
            idx = 0
            for f in missing:
                while idx in used:
                    idx += 1
                pages[0].setdefault("friends", []).append(
                    {"friend_id": f, "constellation_node_index": idx})
                used.add(idx)
                out[f] = idx
            blob = json.dumps(pages, ensure_ascii=False)
            now = int(time.time())
            if row:
                execute("UPDATE friend_constellation_pages SET pages = ?, updated_at = ? "
                        "WHERE user_id = ?", (blob, now, user_id))
            else:
                execute("INSERT INTO friend_constellation_pages "
                        "(user_id, friend_id, page_id, page_data, pages, created_at, updated_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (user_id, "", "", "", blob, now, now))
    except Exception as e:
        logger.warning(f"[好友] 批量写星座页失败: {e}")
    return out


# ============================================================
# p98: 好友星座页分页修复（见 /root/p98_constellation_pages.py）
#   覆盖式重定义 p90 的 constellation_ensure_batch，并让 add_to_constellation 委托过来。
# ============================================================
CONSTELLATION_NODES_PER_PAGE = 10


def _constellation_rebalance(pages):
    """把所有好友按"每页 N 个"重排到多页；返回 (新 pages, 是否有改动)。

    - 页数保持不变（客户端按页渲染，页数是固定的 CONSTELLATION_PAGE_COUNT）；
    - 页内节点号从 0 开始（越界的历史数据在这里被拉回合法范围）；
    - 超出总容量（页数 × 每页容量）的好友不再挂星（宁可不显示，也不能让客户端崩）。
    """
    page_count = CONSTELLATION_PAGE_COUNT if isinstance(CONSTELLATION_PAGE_COUNT, int) else 10
    per_page = CONSTELLATION_NODES_PER_PAGE
    src = pages if isinstance(pages, list) and len(pages) == page_count else None
    if src is None:
        src = [{"name": "", "friends": []} for _ in range(page_count)]
    ordered = []
    seen = set()
    for pg in src:
        for f in ((pg or {}).get("friends") or []):
            fid = str(f.get("friend_id") or "")
            if fid and fid not in seen:
                seen.add(fid)
                ordered.append(fid)
    new_pages = []
    for i in range(page_count):
        name = ""
        try:
            name = (src[i] or {}).get("name") or ""
        except Exception:
            name = ""
        new_pages.append({"name": name, "friends": []})
    for i, fid in enumerate(ordered):
        p = i // per_page
        if p >= page_count:
            break
        new_pages[p]["friends"].append({
            "friend_id": fid,
            "constellation_node_index": i % per_page,
        })
    changed = json.dumps(new_pages, ensure_ascii=False, sort_keys=True) !=         json.dumps(src, ensure_ascii=False, sort_keys=True)
    return new_pages, changed


def _constellation_row(user_id):
    return query_one(
        "SELECT id, pages FROM friend_constellation_pages "
        "WHERE user_id = ? ORDER BY id ASC LIMIT 1", (user_id,))


def _constellation_write(user_id, pages, row):
    blob = json.dumps(pages, ensure_ascii=False)
    now = int(time.time())
    if row and row.get("id") is not None and row.get("pages") is not None:
        execute("UPDATE friend_constellation_pages SET pages = ?, updated_at = ? WHERE id = ?",
                (blob, now, row["id"]))
    elif row:
        execute("UPDATE friend_constellation_pages SET pages = ?, updated_at = ? WHERE user_id = ?",
                (blob, now, user_id))
    else:
        execute("INSERT INTO friend_constellation_pages "
                "(user_id, friend_id, page_id, page_data, pages, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)", (user_id, "", "", "", blob, now, now))


def constellation_ensure_batch(user_id, friend_ids):
    """确保这批好友都在星座页里（**按页容量分页**），返回 {friend_id: node_index}。

    与 p90 版语义相同，区别：先把历史数据 rebalance 回合法范围（自愈），
    再按页容量补挂；页满返回 -1（不再无上限往第 0 页堆）。
    """
    out = {}
    ids = []
    for f in (friend_ids or []):
        f = str(f or "")
        if f and f not in ids:
            ids.append(f)
    if not user_id or not ids:
        return out
    try:
        row = _constellation_row(user_id)
        pages = None
        if row and row.get("pages"):
            try:
                pages = json.loads(row["pages"])
            except Exception:
                pages = None
        page_count = CONSTELLATION_PAGE_COUNT if isinstance(CONSTELLATION_PAGE_COUNT, int) else 10
        if not isinstance(pages, list) or len(pages) != page_count:
            pages = [{"name": "", "friends": []} for _ in range(page_count)]
        pages, changed = _constellation_rebalance(pages)
        per_page = CONSTELLATION_NODES_PER_PAGE
        used = {int(f.get("constellation_node_index") or 0)
                for pg in pages for f in (pg.get("friends") or [])}
        # 先登记已挂上的
        for pi, pg in enumerate(pages):
            for f in (pg.get("friends") or []):
                fid = str(f.get("friend_id") or "")
                if fid and fid not in out:
                    out[fid] = int(f.get("constellation_node_index") or 0)
        missing = [f for f in ids if f not in out]
        if missing:
            for fid in missing:
                placed = False
                for pi in range(page_count):
                    if len(pages[pi]["friends"]) >= per_page:
                        continue
                    node = len(pages[pi]["friends"])
                    while node in used and node < per_page:
                        node += 1
                    if node >= per_page:
                        continue
                    pages[pi]["friends"].append({
                        "friend_id": fid, "constellation_node_index": node})
                    used.add(node)
                    out[fid] = node
                    placed = True
                    break
                if not placed:
                    out[fid] = -1     # 星座满了：不挂星，但也不越界
            changed = True
        if changed:
            _constellation_write(user_id, pages, row)
    except Exception as e:
        logger.warning(f"[好友] 星座页分页修复失败: {e}")
    return out


def add_to_constellation(user_id, friend_id, node_index=None):
    """p98：委托给批量版（保持"返回该好友 node_index、挂不上 -1"的原语义）。"""
    try:
        r = constellation_ensure_batch(user_id, [friend_id])
        v = r.get(str(friend_id))
        return -1 if v is None else int(v)
    except Exception as e:
        logger.warning(f"[好友] 写星座页失败: {e}")
        return -1
