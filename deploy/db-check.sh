#!/usr/bin/env bash
# =====================================================================
# MySQL 连通性诊断（在 **wbsky 容器里** 跑，用的就是应用那套依赖与配置）
# ---------------------------------------------------------------------
# 为什么要有它：
#   应用连接失败时报的是 pymysql 的原始栈，最常见的那条是
#       (1045, "Access denied for user 'wbsky'@'172.18.0.1' (using password: YES)")
#   它**只说明"账号/授权不对"**，但看不出到底是：
#       · 密码错了
#       · 账号没有从容器网段连的授权（'wbsky'@'localhost' ≠ 'wbsky'@'%'）  ← 最常见
#       · 库不存在
#       · 没装 cryptography（MySQL 8 的 caching_sha2_password 需要）
#       · 压根没连上（网络/端口）
#   本脚本逐个把这些区分开，并**直接给出该执行的 SQL**。
#
# 用法：
#   bash deploy/db-check.sh                      # 在容器里（推荐，up.sh 也会调）
#   docker exec wbsky bash deploy/db-check.sh    # 从宿主机调
#   bash deploy/db-check.sh --host 103.24.218.98 # 测别的地址（宿主机上也能跑）
# =====================================================================
set -uo pipefail

cd "$(dirname "$0")/.." 2>/dev/null || true
ROOT="$(pwd)"
ENVF="${ROOT}/deploy/.env"

# 允许用 --host / --port / --user / --password / --db 覆盖（命令行 > .env > 默认）
CLI_HOST=""; CLI_PORT=""; CLI_USER=""; CLI_PASS=""; CLI_DB=""
while [ $# -gt 0 ]; do
  case "$1" in
    --host)     CLI_HOST="${2:-}"; shift 2 ;;
    --port)     CLI_PORT="${2:-}"; shift 2 ;;
    --user)     CLI_USER="${2:-}"; shift 2 ;;
    --password) CLI_PASS="${2:-}"; shift 2 ;;
    --db)       CLI_DB="${2:-}";   shift 2 ;;
    *) shift ;;
  esac
done

if [ -f "${ENVF}" ]; then
  set -a
  # shellcheck disable=SC1090
  . "${ENVF}"
  set +a
fi

HOST="${CLI_HOST:-${MYSQL_HOST:-host.docker.internal}}"
PORT="${CLI_PORT:-${MYSQL_PORT:-3306}}"
USER_="${CLI_USER:-${MYSQL_USER:-wbsky}}"
PASS="${CLI_PASS:-${MYSQL_PASSWORD:-}}"
DB="${CLI_DB:-${MYSQL_DATABASE:-skygame}}"

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; CYN=$'\033[36m'; RST=$'\033[0m'
ok()   { printf '%s  [OK]%s %s\n'   "${GRN}" "${RST}" "$*"; }
warn() { printf '%s  [!!]%s %s\n'   "${YEL}" "${RST}" "$*"; }
bad()  { printf '%s  [XX]%s %s\n'   "${RED}" "${RST}" "$*"; FAIL=$((FAIL+1)); }
hdr()  { printf '\n%s---- %s ----%s\n' "${CYN}" "$*" "${RST}"; }
note() { printf '        %s\n' "$*"; }
FAIL=0

echo "============================================================"
echo "  MySQL 连通性诊断"
echo "  目标: ${USER_}@${HOST}:${PORT}/${DB}"
echo "============================================================"

PY=""
for c in python3 python; do command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }; done
if [ -z "${PY}" ]; then
  bad "容器里没有 python，无法诊断（这不该发生）"
  exit 1
fi

# 把配置通过环境变量传给 python（避免命令行里出现密码）
export DBCHK_HOST="${HOST}" DBCHK_PORT="${PORT}" DBCHK_USER="${USER_}" \
       DBCHK_PASS="${PASS}" DBCHK_DB="${DB}"

"${PY}" - <<'PY'
import os
import socket
import sys

host = os.environ["DBCHK_HOST"]
port = int(os.environ["DBCHK_PORT"] or 3306)
user = os.environ["DBCHK_USER"]
pw = os.environ["DBCHK_PASS"]
db = os.environ["DBCHK_DB"]

G = "\033[32m"; R = "\033[31m"; Y = "\033[33m"; C = "\033[36m"; N = "\033[0m"
fail = []


def ok(m):
    print("%s  [OK]%s %s" % (G, N, m))


def warn(m):
    print("%s  [!!]%s %s" % (Y, N, m))


def bad(m):
    print("%s  [XX]%s %s" % (R, N, m))
    fail.append(m)


def note(m):
    print("        %s" % m)


print()
print("%s---- 1) TCP 能连到吗 ----%s" % (C, N))
try:
    s = socket.create_connection((host, port), 5)
    peer = s.getpeername()
    s.close()
    ok("TCP 通: %s:%s" % (host, port))
    note("（如果你是在容器里看到这条，说明宿主机的 %s 端口是通的）" % port)
except Exception as e:
    bad("TCP 连不上 %s:%s —— %s" % (host, port, e))
    note("修法：")
    note("  · 容器里连宿主机：MYSQL_HOST 用 host.docker.internal（compose 已配 extra_hosts）")
    note("  · 确认 1Panel 里 MySQL 在跑，且 3306 已映射到宿主机")
    note("  · 宿主机上验证：ss -lntp | grep %s" % port)
    print()
    print("诊断到此为止（连不上就没法查账号）。")
    sys.exit(1)

print()
print("%s---- 2) 依赖齐吗 ----%s" % (C, N))
try:
    import pymysql  # noqa
    ok("pymysql 已安装")
except Exception as e:
    bad("没装 pymysql: %s" % e)
try:
    import cryptography  # noqa
    ok("cryptography 已安装（MySQL 8 的 caching_sha2_password 需要）")
    has_crypto = True
except Exception:
    has_crypto = False
    warn("没装 cryptography —— MySQL 8 默认认证插件下会连接失败")
    note("修法：把 cryptography 加进 deploy/requirements.txt 后重建镜像：")
    note("      bash deploy/up.sh")
    note("      （或临时：docker exec wbsky pip install cryptography && docker restart wbsky）")

print()
print("%s---- 3) 用业务账号连一次 ----%s" % (C, N))
import pymysql

conn = None
try:
    conn = pymysql.connect(host=host, port=port, user=user, password=pw,
                           database=db, charset="utf8mb4", connect_timeout=8)
    ok("连接成功：%s@%s/%s" % (user, host, db))
except pymysql.err.OperationalError as e:
    code = e.args[0] if e.args else 0
    msg = str(e.args[1]) if len(e.args) > 1 else str(e)
    if code == 1045:
        bad("1045 账号或授权被拒：%s" % msg)
        note("这一步的报错里会带上 MySQL 看到的来源主机，例如 'wbsky'@'172.18.0.1'。")
        note("★ 关键：MySQL 的账号是 `用户名@来源主机` 一起匹配的。")
        note("  老机器上建的通常是 'wbsky'@'localhost' —— **只允许本机连**，")
        note("  容器连过去被看成 172.18.0.1（Docker 网关），匹配不到就 1045。")
        note("")
        note("修法（用 root 在宿主机/1Panel 的 MySQL 里执行）：")
        note("   -- 1) 先看现有的账号与来源：")
        note("   SELECT user, host FROM mysql.user WHERE user = '%s';" % user)
        note("   -- 2) 补一条允许容器网段连的（Docker bridge 一般是 172.16.0.0/12）：")
        note("   CREATE USER IF NOT EXISTS '%s'@'%%' IDENTIFIED BY '<和 .env 里一致的密码>';" % user)
        note("   GRANT ALL PRIVILEGES ON `%s`.* TO '%s'@'%%';" % (db, user))
        note("   GRANT ALL PRIVILEGES ON `%s`.* TO '%s'@'172.16.0.0/255.240.0.0';" % (db, user))
        note("   FLUSH PRIVILEGES;")
        note("   -- 3) 改完验证：")
        note("   SELECT user, host FROM mysql.user WHERE user = '%s';" % user)
        note("   （1Panel 的图形界面也能改：数据库 → MySQL → 用户 → 该用户 → 授权/来源）")
        note("")
        note("另外顺带确认密码：MySQL 里该账号的密码必须和 deploy/.env 的 MYSQL_PASSWORD")
        note("完全一致（含 @ # 等特殊字符，原样写、不要自己转义）。")
    elif code == 1049:
        bad("1049 数据库不存在：%s" % msg)
        note("修法：1Panel → 数据库 → MySQL → 建库 `%s`（字符集 utf8mb4）" % db)
    elif code in (2003, 2002):
        bad("%s 连不上：%s" % (code, msg))
        note("修法：确认 MySQL 在跑、端口映射正确")
    else:
        bad("连接失败 (%s): %s" % (code, msg))
    sys.exit(1)
except Exception as e:
    bad("连接失败：%r" % (e,))
    sys.exit(1)

print()
print("%s---- 4) 权限够吗（能不能建表）----%s" % (C, N))
cur = conn.cursor()
try:
    cur.execute("SELECT DATABASE(), CURRENT_USER(), VERSION()")
    row = cur.fetchone()
    ok("当前库=%s  登录身份=%s  MySQL 版本=%s" % (row[0], row[1], row[2]))
    note("★ 注意 CURRENT_USER() 里的 host —— 它就是上面说的「来源主机」")
except Exception as e:
    warn("查身份失败: %r" % (e,))

try:
    cur.execute("CREATE TABLE IF NOT EXISTS _wbsky_perm_probe (id INT PRIMARY KEY)")
    cur.execute("DROP TABLE _wbsky_perm_probe")
    ok("有建表/删表权限（应用启动时的自动建表能跑）")
except Exception as e:
    bad("没有建表权限：%r" % (e,))
    note("修法：上面的 GRANT 语句补全即可")

print()
print("%s---- 5) 库里有多少张表 ----%s" % (C, N))
try:
    cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE()")
    n = cur.fetchone()[0]
    ok("当前 %d 张表" % n)
    if n == 0:
        warn("一张表都没有 —— 应用启动时会自动建表（12 张左右）；")
        note("要恢复玩家数据得先把老机器的 mysqldump 导进来：")
        note("  bash deploy/db-import.sh /root/wbsky-full-YYYY-MM-DD.sql.gz")
    else:
        cur.execute("SELECT COUNT(*) FROM users")
        u = cur.fetchone()[0]
        ok("users 表有 %d 个账号" % u)
        cur.execute("SELECT COUNT(*) FROM friends")
        ok("friends 表有 %d 行好友关系" % cur.fetchone()[0])
except Exception as e:
    bad("数表失败：%r" % (e,))
finally:
    try:
        cur.close()
        conn.close()
    except Exception:
        pass

print()
print("=" * 60)
if fail:
    print("%s  诊断发现 %d 个问题，按上面的 [XX] 修%s" % (R, len(fail), N))
    sys.exit(1)
print("%s  数据库连接与权限都没问题%s" % (G, N))
PY
RC=$?
exit "${RC}"
