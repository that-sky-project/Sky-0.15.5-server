#!/usr/bin/env bash
# =====================================================================
# 部署自检：一次把"到底哪一层没起来"查清楚
#   用法:  bash deploy/check.sh
#   前提:  项目根目录（含 wbsky/ sky0155-udp/ deploy/）
# =====================================================================
cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
ENVF="${ROOT}/deploy/.env"

# shellcheck disable=SC1090
[ -f "${ENVF}" ] && set -a && . "${ENVF}" && set +a

HTTPS_PORT="${HTTPS_PORT:-2007}"
UDP_PORT="${UDP_PORT:-19458}"
WS_PORT="${WS_PORT:-2500}"
STATS_PORT="${STATS_PORT:-11925}"
DOMAIN="${WB_SKY_DOMAIN:-beta.admin.xyz}"
UDP_HOST="${WB_SKY_UDP_HOST:-}"

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; CYN=$'\033[36m'; RST=$'\033[0m'
ok()   { printf '%s  [OK]%s %s\n'   "${GRN}" "${RST}" "$*"; }
warn() { printf '%s  [!!]%s %s\n'   "${YEL}" "${RST}" "$*"; }
bad()  { printf '%s  [XX]%s %s\n'   "${RED}" "${RST}" "$*"; FAIL=$((FAIL+1)); }
hdr()  { printf '\n%s──── %s ────%s\n' "${CYN}" "$*" "${RST}"; }
note() { printf '        %s\n' "$*"; }

FAIL=0
COMPOSE="docker compose -f deploy/docker-compose.yml --env-file deploy/.env"

echo "============================================================"
echo "  skywb 部署自检    $(date '+%F %T')"
echo "  目录: ${ROOT}"
echo "============================================================"

# ---------------------------------------------------------------------
hdr "1/7 配置文件"
# ---------------------------------------------------------------------
if [ -f "${ENVF}" ]; then
  ok "deploy/.env 存在"
  # BOM 检查：Windows 编辑器很容易加 BOM，Docker 会报 unexpected character
  head -c 3 "${ENVF}" | od -An -tx1 | grep -q 'ef bb bf' \
    && bad "deploy/.env 带 UTF-8 BOM，请另存为「无 BOM 的 UTF-8」" \
    || ok "deploy/.env 无 BOM"
else
  bad "缺 deploy/.env（cp deploy/.env.example deploy/.env）"
fi

if [ -z "${UDP_HOST}" ]; then
  bad "WB_SKY_UDP_HOST 为空 —— 客户端拿不到联机地址，会出现「能进图但看不到人」"
elif printf '%s' "${UDP_HOST}" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
  ok "WB_SKY_UDP_HOST=${UDP_HOST}（公网 IPv4 格式正确）"
elif printf '%s' "${UDP_HOST}" | grep -qiE '^[a-z0-9.-]+$'; then
  warn "WB_SKY_UDP_HOST=${UDP_HOST} 看起来是**域名**。客户端实时层要求 IPv4，"
  note "域名会让部分客户端崩溃或连不上，建议改成公网 IP。"
else
  bad "WB_SKY_UDP_HOST=${UDP_HOST} 不是合法 IPv4"
fi

# 房间服端口一致性（最容易踩、最难查的坑）
CFG_PORT=$(python3 -c "import json;print(json.load(open('sky0155-udp/config.json')).get('udp_server_port'))" 2>/dev/null \
           || grep -o '"udp_server_port"[^,]*' sky0155-udp/config.json | grep -o '[0-9]\+' | head -1)
if [ -n "${CFG_PORT}" ] && [ "${CFG_PORT}" != "${UDP_PORT}" ]; then
  bad "端口不一致：下发给客户端的是 ${UDP_PORT}，房间服 config.json 里是 ${CFG_PORT}"
  note "症状 = 能进图但看不到其他玩家，且没有任何报错。"
  note "修法：把 sky0155-udp/config.json 改成 ${UDP_PORT}，"
  note "      或在 .env 里设 WB_SKY_UDP_PORT=${CFG_PORT}（房间服进程会用它覆盖）"
else
  ok "房间服端口一致（${UDP_PORT}）"
fi

# ---------------------------------------------------------------------
hdr "2/7 容器状态"
# ---------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  bad "宿主机上没有 docker 命令"
else
  for c in wbsky wbsky-udp wbsky-ws; do
    st=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo "missing")
    case "$st" in
      running) ok "容器 $c: running" ;;
      missing) bad "容器 $c 不存在（还没 up？）" ;;
      *)       bad "容器 $c: $st" ;;
    esac
  done
  echo
  ${COMPOSE} ps 2>/dev/null || warn "compose ps 执行失败"
fi

# ---------------------------------------------------------------------
hdr "3/7 端口监听"
# ---------------------------------------------------------------------
if command -v ss >/dev/null 2>&1; then
  ss -lntup 2>/dev/null | grep -E ":(${HTTPS_PORT}|${WS_PORT}|${STATS_PORT})\b" >/dev/null \
    && ok "TCP 端口在听（${HTTPS_PORT}/${WS_PORT}/${STATS_PORT}）" \
    || bad "TCP 端口没在听（${HTTPS_PORT}/${WS_PORT}/${STATS_PORT}）"
  ss -lunp 2>/dev/null | grep -E ":${UDP_PORT}\b" >/dev/null \
    && ok "UDP ${UDP_PORT} 在听（联机房间服）" \
    || bad "UDP ${UDP_PORT} 没在听 —— 联机一定连不上"
else
  warn "宿主机没有 ss 命令，跳过端口检查（apt install iproute2）"
fi

# ---------------------------------------------------------------------
hdr "4/7 主服务 /healthz"
# ---------------------------------------------------------------------
HZ_HTTPS=$(curl -kfsS -m 5 "https://127.0.0.1:${HTTPS_PORT}/healthz" 2>/dev/null)
HZ_HTTP=$(curl -fsS -m 5 "http://127.0.0.1:${HTTPS_PORT}/healthz" 2>/dev/null)
HZ="${HZ_HTTPS:-${HZ_HTTP}}"
if [ -n "${HZ}" ]; then
  ok "主服务响应: ${HZ}"
  printf '%s' "${HZ}" | grep -q '"status": *"degraded"' \
    && warn "数据库没通（上面 db 字段有 error）。UDP 联机不受影响，修好库后重启 wbsky 容器即可"
  if [ -z "${HZ_HTTPS}" ]; then
    warn "走的是【明文 HTTP】—— 说明 wbsky/fullchain.pem + privkey.key 不存在"
    note "客户端能正常连（TLS 层跳过校验），但浏览器访问会显示不安全。"
    note "想要 HTTPS：bash deploy/make-cert.sh"
  fi
else
  bad "主服务没响应（https/http 都试过）。看日志：docker logs wbsky --tail 100"
fi

# ---------------------------------------------------------------------
hdr "5/7 房间服 /stats"
# ---------------------------------------------------------------------
ST=$(curl -fsS -m 5 "http://127.0.0.1:${STATS_PORT}/stats" 2>/dev/null)
if [ -n "${ST}" ]; then
  ONLINE=$(printf '%s' "${ST}" | grep -o '"online": *[0-9]*' | grep -o '[0-9]*' | head -1)
  ok "房间服在跑，当前在线 ${ONLINE:-?} 人"
else
  bad "房间服 /stats 无响应（127.0.0.1:${STATS_PORT}）"
  # ★ 先查这个：房间服起不来的头号原因是 sky-enet 没装上。
  #   症状是容器崩溃循环、日志刷 "Cannot find package 'sky-enet'"。
  #   根因通常是"node_modules 目录存在但没有内容"（compose 挂载时 Docker
  #   会自动创建空目录），老版本据此判断"已装好"直接跳过 npm install。
  if [ -d "${ROOT}/sky0155-udp/node_modules" ] && [ ! -f "${ROOT}/sky0155-udp/node_modules/sky-enet/package.json" ]; then
    bad "sky0155-udp/node_modules 存在但里面没有 sky-enet（半成品/空目录）"
    note "修法：rm -rf sky0155-udp/node_modules && bash deploy/up.sh"
    note "或者先单独装（能看到安装日志）：bash deploy/build-udp-node.sh"
  elif [ ! -d "${ROOT}/sky0155-udp/node_modules" ]; then
    warn "还没装过依赖（首次启动会自动装，可能几分钟）"
  else
    ok "sky-enet 已在 sky0155-udp/node_modules 里"
  fi
  note "看容器日志：docker logs wbsky-udp --tail 100"
fi

# ---------------------------------------------------------------------
hdr "6/7 聊天 WebSocket"
# ---------------------------------------------------------------------
WS_HEAD=$(curl -sS -m 5 -i -N \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
  -H 'Sec-WebSocket-Version: 13' \
  "http://127.0.0.1:${WS_PORT}/account/ws" 2>/dev/null | head -1)
case "${WS_HEAD}" in
  *101*) ok "WS 握手成功（HTTP 101）" ;;
  "")    bad "WS 端口无响应（${WS_PORT}）—— 聊天会「发出去自己都看不到」" ;;
  *)     bad "WS 握手异常: ${WS_HEAD}" ;;
esac

if [ -n "${DOMAIN}" ]; then
  DNS_IP=$(getent hosts "${DOMAIN}" 2>/dev/null | awk '{print $1}' | head -1)
  if [ -n "${DNS_IP}" ]; then
    ok "${DOMAIN} 解析到 ${DNS_IP}"
    if [ -n "${UDP_HOST}" ] && [ "${DNS_IP}" != "${UDP_HOST}" ]; then
      warn "解析结果与 WB_SKY_UDP_HOST(${UDP_HOST}) 不一致，确认走的是哪台机"
    fi
  else
    warn "${DOMAIN} 在本机解析不到（可能只是本机 DNS 问题，用外部 ping 复核）"
  fi
fi

# ---------------------------------------------------------------------
hdr "7/7 最近错误日志"
# ---------------------------------------------------------------------
if command -v docker >/dev/null 2>&1; then
  for c in wbsky wbsky-udp wbsky-ws; do
    n=$(docker logs "$c" 2>&1 | grep -ciE "traceback|error|error:|failed|refused|denied" || true)
    if [ "${n:-0}" -gt 0 ]; then
      warn "$c 日志里有 ${n} 行含 error/失败关键字，最近 5 行："
      docker logs "$c" 2>&1 | grep -iE "traceback|error|failed|refused|denied" | tail -5 | sed 's/^/        /'
    else
      ok "$c 日志里没有明显错误"
    fi
  done
fi

echo
echo "============================================================"
if [ "${FAIL}" -eq 0 ]; then
  printf '%s  自检通过（0 个硬错误）%s\n' "${GRN}" "${RST}"
else
  printf '%s  自检发现 %d 个硬错误，按上面的 [XX] 逐条修%s\n' "${RED}" "${FAIL}" "${RST}"
fi
echo "============================================================"
exit "${FAIL}"
