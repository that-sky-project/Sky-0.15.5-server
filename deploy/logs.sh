#!/usr/bin/env bash
# =====================================================================
# 看日志 / 进容器（省得每次手敲一长串 compose 参数）
#
# 用法:
#   bash deploy/logs.sh            # 三个容器一起 tail
#   bash deploy/logs.sh wbsky      # 只看主服务
#   bash deploy/logs.sh udp        # 只看房间服
#   bash deploy/logs.sh ws         # 只看聊天 WS
#   bash deploy/logs.sh shell      # 进 wbsky 容器开 bash（排障）
#   bash deploy/logs.sh live       # 直接看实时面板 JSON（本机）
# =====================================================================
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
ENVF="deploy/.env"
[ -f "${ENVF}" ] && { set -a; . "${ENVF}"; set +a; }
HTTPS_PORT="${HTTPS_PORT:-2007}"

case "${1:-all}" in
  all)
    docker compose -f deploy/docker-compose.yml --env-file deploy/.env logs -f --tail 80
    ;;
  wbsky|server|main)
    docker logs -f --tail 200 wbsky
    ;;
  udp|node)
    docker logs -f --tail 200 wbsky-udp
    ;;
  ws|chat)
    docker logs -f --tail 200 wbsky-ws
    ;;
  shell)
    docker exec -it wbsky bash
    ;;
  live)
    curl -sS -u "${WB_SKY_LIVE_USER:-admin}:${WB_SKY_LIVE_PASSWORD:-admin123}" \
      -k "https://127.0.0.1:${HTTPS_PORT}/live/data" \
      || curl -sS -u "${WB_SKY_LIVE_USER:-admin}:${WB_SKY_LIVE_PASSWORD:-admin123}" \
           "http://127.0.0.1:${HTTPS_PORT}/live/data"
    echo
    ;;
  health)
    (curl -kfsS "https://127.0.0.1:${HTTPS_PORT}/healthz" 2>/dev/null \
      || curl -fsS "http://127.0.0.1:${HTTPS_PORT}/healthz" 2>/dev/null) | python3 -m json.tool 2>/dev/null \
      || echo "主服务没响应"
    ;;
  *)
    echo "用法: bash deploy/logs.sh [all|wbsky|udp|ws|shell|live|health]"
    exit 2
    ;;
esac
