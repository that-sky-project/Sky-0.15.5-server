#!/usr/bin/env bash
# =====================================================================
# 数据库导入（空库首次部署用）
# ---------------------------------------------------------------------
# 用法（宿主机上，项目根目录）：
#     bash deploy/db-import.sh              # 按 deploy/.env 里的配置导
#     SKYWB_DB_IMPORT=1 bash deploy/db-import.sh
#     bash deploy/db-import.sh /path/to/other.sql.gz
#
# 这个脚本做三件事，每件都是"宁可拒绝执行也不破坏数据"：
#   1. 检查 dump 文件**是不是真的内容**（包里那份是 113 字节的空备份，
#      只有一行注释，导了等于没导 —— 必须显式识别出来并告警）
#   2. 检查目标库是不是空的；**非空就拒绝导入**（除非加 --force）
#   3. 导入并数一次表，把结果打印出来
#
# [!] 已有玩家数据的线上机器：**不要跑这个脚本**，保持 SKYWB_DB_IMPORT=0。
# =====================================================================
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
ENVF="${ROOT}/deploy/.env"

if [ -f "${ENVF}" ]; then
  set -a
  # shellcheck disable=SC1090
  . "${ENVF}"
  set +a
fi

DUMP="${1:-${ROOT}/db/wbsky-full.sql.gz}"
DB_HOST="${MYSQL_HOST:-host.docker.internal}"
DB_PORT="${MYSQL_PORT:-3306}"
DB_USER="${MYSQL_USER:-wbsky}"
DB_PASS="${MYSQL_PASSWORD:-}"
DB_NAME="${MYSQL_DATABASE:-skygame}"
ROOT_USER="${MYSQL_ROOT_USER:-root}"
ROOT_PASS="${MYSQL_ROOT_PASSWORD:-}"
FORCE=0
[ "${2:-}" = "--force" ] && FORCE=1

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; RST=$'\033[0m'
ok()   { printf '%s  [OK]%s %s\n' "${GRN}" "${RST}" "$*"; }
warn() { printf '%s  [!!]%s %s\n' "${YEL}" "${RST}" "$*"; }
err()  { printf '%s  [XX]%s %s\n' "${RED}" "${RST}" "$*"; }
die()  { err "$*"; exit 1; }

# 宿主机上连 1Panel 的 MySQL，用 127.0.0.1（host.docker.internal 只在容器里有意义）
case "${DB_HOST}" in
  host.docker.internal|mysql|udp|wbsky) DB_HOST_LOCAL="127.0.0.1" ;;
  *) DB_HOST_LOCAL="${DB_HOST}" ;;
esac

echo "============================================================"
echo "  wbsky 数据库导入"
echo "  dump : ${DUMP}"
echo "  目标 : ${DB_USER}@${DB_HOST_LOCAL}:${DB_PORT}/${DB_NAME}"
echo "============================================================"

# ---------------------------------------------------------------------
# 0) 有没有 mysql 客户端
# ---------------------------------------------------------------------
if ! command -v mysql >/dev/null 2>&1; then
  die "宿主机上没有 mysql 客户端。装一个：apt install -y mysql-client
       （或者用 1Panel 的「数据库 → 导入」界面手工导）"
fi

# ---------------------------------------------------------------------
# 1) dump 文件真实性检查
# ---------------------------------------------------------------------
[ -f "${DUMP}" ] || die "找不到 dump 文件: ${DUMP}"

case "${DUMP}" in
  *.gz) READ="zcat" ;;
  *)    READ="cat"  ;;
esac

DUMP_BYTES=$(${READ} "${DUMP}" 2>/dev/null | wc -c | tr -d ' ')
if [ -z "${DUMP_BYTES}" ]; then
  die "读不出 dump 内容（文件损坏？）: ${DUMP}"
fi
# 统计真正的 SQL 语句条数（以分号结尾、且不是纯注释/空行）
STMT=$(${READ} "${DUMP}" 2>/dev/null | grep -cE ';\s*$' || true)

echo "  dump 解压后 ${DUMP_BYTES} 字节，看起来有 ${STMT} 条语句"
if [ "${DUMP_BYTES}" -lt 2048 ] || [ "${STMT:-0}" -lt 3 ]; then
  err "这份 dump 基本是空的（只有注释/表头），导入它没有任何意义。"
  echo
  echo "  为什么：打包出来的 db/wbsky-full.sql.gz 只有 113 字节，"
  echo "          内容就是 '-- wbsky full dump (schema + data)' 一行，"
  echo "          **不含任何建表语句和数据**。"
  echo
  echo "  怎么办（三选一）："
  echo "    A) 从现网导出真正的全量（推荐，在**老机器**上执行）："
  echo "         mysqldump -u root -p --single-transaction --routines \\"
  echo "           --default-character-set=utf8mb4 wbsky | gzip > wbsky-full.sql.gz"
  echo "       然后把这个文件放到本项目根目录，再跑一次本脚本。"
  echo "    B) 老机器还在跑，就只导结构+账号等必要表，玩家数据走逐表同步。"
  echo "    C) 只要空库也能跑：本服务启动时会自动建表"
  echo "         （wbsky/db.py 的 _init_mysql()，CREATE TABLE IF NOT EXISTS，幂等），"
  echo "       但表结构比现网少（现网 67 张表，代码里只建核心几张）。"
  exit 2
fi

# ---------------------------------------------------------------------
# 2) 目标库是不是空的
# ---------------------------------------------------------------------
MYSQL_ARGS=(-h "${DB_HOST_LOCAL}" -P "${DB_PORT}" -u "${ROOT_USER}")
[ -n "${ROOT_PASS}" ] && MYSQL_ARGS+=("-p${ROOT_PASS}")

TABLES=$(mysql "${MYSQL_ARGS[@]}" -N -B -e \
  "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='${DB_NAME}'" 2>/dev/null)

if [ -z "${TABLES}" ]; then
  warn "用 root 连不上（${ROOT_USER}@${DB_HOST_LOCAL}）。"
  warn "请在 .env 里填 MYSQL_ROOT_USER / MYSQL_ROOT_PASSWORD，或用 1Panel 界面导入。"
  warn "（本步只是为了确认库是不是空的；导入本身用业务账号也行）"
else
  echo "  目标库里现有 ${TABLES} 张表"
  if [ "${TABLES}" -gt 0 ] && [ "${FORCE}" -ne 1 ]; then
    die "目标库**不是空的**，拒绝导入（防止把玩家数据覆盖掉）。
       确实要覆盖：bash deploy/db-import.sh '${DUMP}' --force
       （建议先备份： mysqldump -u root -p ${DB_NAME} | gzip > backup-$(date +%F).sql.gz）"
  fi
fi

# ---------------------------------------------------------------------
# 3) 导入
# ---------------------------------------------------------------------
echo
echo "  正在导入（大库可能要几分钟）…"
if ${READ} "${DUMP}" | mysql "${MYSQL_ARGS[@]}" "${DB_NAME}"; then
  ok "导入完成"
else
  die "导入失败。常见原因：
       · 库不存在（先在 1Panel → 数据库 → MySQL 里建库 ${DB_NAME}，字符集 utf8mb4）
       · 账号没权限
       · dump 是用别的 MySQL 版本导的（8.0 导 5.7 常见）"
fi

AFTER=$(mysql "${MYSQL_ARGS[@]}" -N -B -e \
  "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='${DB_NAME}'" 2>/dev/null || echo "?")
echo
ok "现在库里有 ${AFTER} 张表（现网快照是 67 张）"
echo
echo "接着做："
echo "  1) 确认 .env 的 MYSQL_* 指向这个库"
echo "  2) docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d"
echo "  3) bash deploy/check.sh"
echo "  4) curl -s http://127.0.0.1:2007/healthz   # 看 db.ok 是不是 true"
