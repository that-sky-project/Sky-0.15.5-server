#!/usr/bin/env bash
# =====================================================================
# 光遇私服 wbsky · 容器入口
# ---------------------------------------------------------------------
#   entrypoint.sh server   主服务 index.py（Flask 2007 + 全部游戏 API）
#   entrypoint.sh ws       聊天 WebSocket 中继 ws_server.py（2500）
#   entrypoint.sh shell    交互式 bash（排障用）
#   entrypoint.sh python … 直通：原样 exec 后面的命令
#                          （compose 的 command 写 ["python","-u","index.py"]
#                            时走的就是这条，不能被当成"未知指令"）
# =====================================================================
set -uo pipefail

APP_DIR="${APP_DIR:-/app}"
cd "$APP_DIR" || exit 1

# sitecustomize 靠 PYTHONPATH 生效（解释器启动时自动 import）
export PYTHONPATH="${APP_DIR}/deploy${PYTHONPATH:+:$PYTHONPATH}"

HTTP_PORT="${WB_SKY_HTTP_PORT:-2007}"
WS_PORT="${WB_SKY_WS_PORT_INTERNAL:-2500}"

log() { echo "[entrypoint] $*"; }

# ---------------------------------------------------------------------
# 依赖自检
# ---------------------------------------------------------------------
# 背景（老包踩过三次）：容器里报
#     ModuleNotFoundError: No module named 'pymysql'
# 而它是从 db._get_mysql_conn 里冒出来的，看着像网络/密码问题，实际是
# **镜像没重建** —— 依赖是 build 时按 deploy/requirements.txt 装的，改了
# requirements 不 rebuild 就没用，而 `docker compose up -d` 不会自动 rebuild。
#
# 这里在启动前明确检查一遍，缺了就打印"怎么修"，而不是让服务在
# 第一次连库时抛一个误导性的栈。
# ---------------------------------------------------------------------
dep_check() {
  if ! command -v python >/dev/null 2>&1; then
    log "[!]  镜像里没有 python，跳过依赖自检"
    return 0
  fi

  local missing
  missing=$(python - <<'PY' 2>/dev/null
import importlib
import os

mods = ["flask", "pymysql", "requests"]
if os.environ.get("WB_SKY_USE_MYSQL", "1").strip().lower() not in ("0", "false", "no", "off"):
    # MySQL 8 的 caching_sha2_password 认证需要 cryptography。
    # 它已经在 deploy/requirements.txt 里了；这里一起自检，
    # 缺了就明确报出来（否则连接阶段会抛一条看着像网络问题的错）。
    mods.append("cryptography")

missing = []
for m in mods:
    try:
        importlib.import_module(m)
    except Exception:
        missing.append(m)
print(" ".join(missing))
PY
)

  if [ -z "${missing}" ]; then
    log "依赖自检通过（flask / pymysql / requests"$([ "${WB_SKY_USE_MYSQL:-1}" != "0" ] && printf ' / cryptography')"）"
    return 0
  fi

  log "[X] 缺少 Python 依赖: ${missing}"
  log "   镜像大概率是旧的（改了 requirements 但没重建）。三种修法："
  log "     A) 重建镜像（推荐，up -d 不会 rebuild）："
  log "        docker compose -f deploy/docker-compose.yml build --no-cache wbsky"
  log "        docker compose -f deploy/docker-compose.yml up -d"
  log "     B) 容器还活着时手动补（立刻生效，但重建容器就丢）："
  log "        docker exec -it wbsky pip install ${missing}"
  log "        docker restart wbsky"
  log "   现在继续启动 —— 真正缺的依赖会在首个请求时报错。"
  return 1
}

# ---------------------------------------------------------------------
# 证书自检
# ---------------------------------------------------------------------
# index.py 的规则：wbsky/fullchain.pem + wbsky/privkey.key 都在就起 HTTPS，
# 否则退回明文 HTTP（老包里没有证书文件，实际跑的就是明文）。
# 客户端在 TLS 层跳过证书校验，所以自签完全够用。
# ---------------------------------------------------------------------
cert_check() {
  if [ -s "${APP_DIR}/wbsky/fullchain.pem" ] && [ -s "${APP_DIR}/wbsky/privkey.key" ]; then
    log "已有证书: $(openssl x509 -in "${APP_DIR}/wbsky/fullchain.pem" -noout -subject 2>/dev/null || echo '(解析失败，仍会尝试使用)')"
    return 0
  fi
  log "[!]  没找到 ${APP_DIR}/wbsky/fullchain.pem + privkey.key"
  log "    → 主服务将以【明文 HTTP】启动在 ${HTTP_PORT}（这是老包的原有行为）。"
  log "    → 想要 HTTPS：bash deploy/make-cert.sh 生成，或在 .env 里设"
  log "      WB_SKY_AUTO_CERT=1 让 entrypoint 自动签一张自签证书。"
  if [ "$(printf '%s' "${WB_SKY_AUTO_CERT:-0}" | tr 'A-Z' 'a-z')" = "1" ]; then
    if command -v openssl >/dev/null 2>&1; then
      log "    正在生成自签证书 (CN=${WB_SKY_CERT_CN:-beta.admin.xyz}) …"
      openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "${APP_DIR}/wbsky/privkey.key" \
        -out "${APP_DIR}/wbsky/fullchain.pem" \
        -subj "/C=CN/O=skywb/CN=${WB_SKY_CERT_CN:-beta.admin.xyz}" \
        -addext "subjectAltName=DNS:${WB_SKY_CERT_CN:-beta.admin.xyz},IP:${WB_SKY_CERT_IP:-127.0.0.1}" \
        >/dev/null 2>&1 && log "    自签证书已生成" || log "    自签证书生成失败"
    else
      log "    镜像里没有 openssl，无法自动签名"
    fi
  fi
  return 0
}

# ---------------------------------------------------------------------
# 目录兜底
# ---------------------------------------------------------------------
for d in wbsky/logs wbsky/db wbsky/config sky0155-udp; do
  [ -d "${APP_DIR}/${d}" ] || mkdir -p "${APP_DIR}/${d}"
done

# ---------------------------------------------------------------------
# 直通模式（必须在 case 之前判断，否则会被当成"未知指令"）
# ---------------------------------------------------------------------
case "${1:-}" in
  python|python3|/usr/local/bin/python*|/usr/bin/python*)
    dep_check || true
    log "直通执行: $*"
    exec "$@"
    ;;
esac

# ---------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------
case "${1:-server}" in
  server)
    dep_check || true
    cert_check
    log "启动主服务 index.py：${WB_SKY_BIND:-0.0.0.0}:${HTTP_PORT}（sitecustomize 已接管端口/调试器）"
    exec python -u index.py
    ;;
  ws)
    dep_check || true
    log "启动聊天 WebSocket 中继 ws_server.py：0.0.0.0:${WS_PORT}"
    exec python -u ws_server.py "${WS_PORT}"
    ;;
  shell)
    exec bash
    ;;
  *)
    log "未知指令: $1"
    log "可用: server / ws / shell，或直接 python -u <脚本>"
    exit 2
    ;;
esac
