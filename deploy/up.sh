#!/usr/bin/env bash
# =====================================================================
# 一键起服务（等价于 docker compose up -d --build，但会先做几件必要的检查）
#
# 用法:  bash deploy/up.sh            # 构建 + 启动
#        bash deploy/up.sh --no-build # 只重启，不重新构建镜像
#
# 它比直接敲 compose 多做的事：
#   1. 检查 deploy/.env 存在且**没有 BOM**（Windows 编辑器常加的坑）
#   2. 检查 WB_SKY_UDP_HOST 是公网 IPv4（填域名会让客户端崩）
#   3. 检查房间服端口一致性（不一致 = 能进图看不到人，且不报错）
#   4. 检查 1Panel MySQL 是否在宿主机 3306 上可达
#   5. 起完之后自动跑一遍 deploy/check.sh
# =====================================================================
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
ENVF="${ROOT}/deploy/.env"
COMPOSE=(docker compose -f deploy/docker-compose.yml --env-file deploy/.env)

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; RST=$'\033[0m'
ok()   { printf '%s  [OK]%s %s\n' "${GRN}" "${RST}" "$*"; }
warn() { printf '%s  [!!]%s %s\n' "${YEL}" "${RST}" "$*"; }
err()  { printf '%s  [XX]%s %s\n' "${RED}" "${RST}" "$*"; }
die()  { err "$*"; exit 1; }

echo "============================================================"
echo "  skywb 启动    $(date '+%F %T')"
echo "  目录: ${ROOT}"
echo "============================================================"

command -v docker >/dev/null 2>&1 || die "宿主机上没有 docker"
docker compose version >/dev/null 2>&1 || die "没有 'docker compose' 子命令（旧版是 docker-compose，请升级）"

# ---------------------------------------------------------------------
# 1) .env
# ---------------------------------------------------------------------
if [ ! -f "${ENVF}" ]; then
  warn "缺 deploy/.env，从模板复制一份"
  cp deploy/.env.example deploy/.env || die "复制 .env 失败"
fi
if head -c 3 "${ENVF}" | od -An -tx1 | grep -q 'ef bb bf'; then
  die "deploy/.env 带 UTF-8 BOM，Docker 会报 unexpected character。
       修法：用 VSCode/Notepad++ 另存为「UTF-8（无 BOM）」"
fi
ok "deploy/.env 就绪（无 BOM）"

set -a
# shellcheck disable=SC1090
. "${ENVF}"
set +a

UDP_PORT="${UDP_PORT:-19458}"
HTTPS_PORT="${HTTPS_PORT:-2007}"

# ---------------------------------------------------------------------
# 2) 下发给客户端的联机地址
# ---------------------------------------------------------------------
UDP_HOST="${WB_SKY_UDP_HOST:-}"
if [ -z "${UDP_HOST}" ]; then
  die "WB_SKY_UDP_HOST 为空。它决定客户端连哪台机联机，必须填**公网 IPv4**"
elif ! printf '%s' "${UDP_HOST}" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$'; then
  err "WB_SKY_UDP_HOST=${UDP_HOST} 不是 IPv4。"
  err "客户端实时层要求 IPv4：填域名会让部分客户端断言崩溃，"
  err "症状是「能登录、能进图，但看不到其他玩家」。"
  die "请改成公网 IP（例如 103.24.218.98）"
fi
ok "对外联机地址: ${UDP_HOST}:${UDP_PORT}"

# ---------------------------------------------------------------------
# 3) 房间服端口一致性
# ---------------------------------------------------------------------
CFG_PORT=$(python3 -c "import json;print(json.load(open('sky0155-udp/config.json')).get('udp_server_port'))" 2>/dev/null || true)
if [ -n "${CFG_PORT}" ] && [ "${CFG_PORT}" != "${UDP_PORT}" ]; then
  warn "端口不一致：下发 ${UDP_PORT}，房间服 config.json 是 ${CFG_PORT}"
  warn "（.env 里设了 WB_SKY_UDP_PORT 的话，进程会用 .env 的值覆盖，运行时不受影响）"
else
  ok "房间服端口一致（${UDP_PORT}）"
fi

# ---------------------------------------------------------------------
# 4) 房间服依赖：node_modules 不能是"空壳"
# ---------------------------------------------------------------------
# 这个检查是踩坑后加的：compose 把 ../sky0155-udp/node_modules 挂进容器时，
# **Docker 会在宿主机上自动创建这个空目录**。于是老版本那句
#     [ -d node_modules ] || npm install
# 在第一次启动时直接为真、跳过安装，紧接着 node 报
#     Cannot find package 'sky-enet'
# 并进入崩溃循环，日志刷成一片栈。
# 现在启动命令改成判断 node_modules/sky-enet/package.json 了；
# 这里再加一道：发现空壳就地清掉，让容器这次能真正装上。
if [ -d sky0155-udp/node_modules ] && [ ! -f sky0155-udp/node_modules/sky-enet/package.json ]; then
  warn "sky0155-udp/node_modules 存在但没有 sky-enet（空壳/半成品）"
  warn "→ 已清掉，容器首次启动会重新安装（native 模块，可能要几分钟）"
  rm -rf sky0155-udp/node_modules
elif [ -f sky0155-udp/node_modules/sky-enet/package.json ]; then
  ok "房间服依赖就绪（node_modules/sky-enet）"
else
  warn "还没装过房间服依赖 —— 首次启动会自动 npm install，可能几分钟，"
  warn "想先看到安装日志：bash deploy/build-udp-node.sh"
fi

# ---------------------------------------------------------------------
# 5) MySQL 可达性（只提示，不阻塞）
# ---------------------------------------------------------------------
if [ "${WB_SKY_USE_MYSQL:-1}" != "0" ]; then
  MH="${MYSQL_HOST:-host.docker.internal}"
  MP="${MYSQL_PORT:-3306}"
  probe="127.0.0.1"
  [ "${MH}" = "host.docker.internal" ] || probe="${MH}"
  if command -v bash >/dev/null 2>&1; then
    if (exec 3<>"/dev/tcp/${probe}/${MP}") 2>/dev/null; then
      ok "MySQL 可达: ${probe}:${MP}"
    else
      warn "宿主机 ${probe}:${MP} 连不上 —— 确认 1Panel 里 MySQL 在跑，"
      warn "且它的端口已映射到宿主机（1Panel → 数据库 → MySQL → 端口）"
      warn "容器里连的是 host.docker.internal:${MP}，就是宿主机这个端口。"
    fi
  fi
fi

# ---------------------------------------------------------------------
# 6) 起服务
# ---------------------------------------------------------------------
echo
BUILD_FLAG="--build"
[ "${1:-}" = "--no-build" ] && BUILD_FLAG=""
echo "  ${COMPOSE[*]} up -d ${BUILD_FLAG}"
${COMPOSE[@]} up -d ${BUILD_FLAG} || die "compose up 失败（上面有原因）"

echo
echo "  等待容器进入 running（首次启动要装 sky-enet，可能要几分钟）…"
for i in $(seq 1 60); do
  sleep 5
  n=$(docker ps --filter "name=^wbsky$" --filter "name=^wbsky-udp$" --filter "name=^wbsky-ws$" \
        --format '{{.Names}}' | wc -l | tr -d ' ')
  [ "${n:-0}" -ge 3 ] && break
done
${COMPOSE[@]} ps || true

# ---------------------------------------------------------------------
# 7) 数据库连通性（在容器里真连一次，报错会直接给出该执行的 SQL）
# ---------------------------------------------------------------------
# 为什么放在这里：最常见的一类故障是 MySQL 的 1045 ——
# 账号建的是 'wbsky'@'localhost'，而容器连过去被看成 172.18.0.1（Docker 网关），
# 匹配不到授权就拒绝。应用只会抛 pymysql 的原始栈，看不出该怎么修。
if [ "${WB_SKY_USE_MYSQL:-1}" != "0" ]; then
  echo
  if docker ps --format '{{.Names}}' | grep -qx wbsky; then
    if ! docker exec wbsky bash deploy/db-check.sh 2>/dev/null; then
      warn "数据库诊断发现问题（详见上面输出）。应用会以「降级」状态继续跑："
      warn "  联机/聊天不受影响；注册、登录等依赖数据库的功能不可用。"
      warn "  最快的复看方式：docker exec wbsky bash deploy/db-check.sh"
    fi
  else
    warn "wbsky 容器没起来，跳过数据库诊断（先看：docker logs wbsky --tail 80）"
  fi
fi

# ---------------------------------------------------------------------
# 8) 自检
# ---------------------------------------------------------------------
echo
if [ -x deploy/check.sh ]; then
  bash deploy/check.sh || true
else
  echo "  （没有 deploy/check.sh，跳过自检）"
fi

cat <<EOF

============================================================
  常用命令
    看日志   : docker logs -f wbsky            （房间服: wbsky-udp，聊天: wbsky-ws）
    重启主服 : docker compose -f deploy/docker-compose.yml --env-file deploy/.env restart wbsky
    停全部   : docker compose -f deploy/docker-compose.yml --env-file deploy/.env down
    改端口   : 编辑 deploy/.env 后重新 up.sh
============================================================
EOF
