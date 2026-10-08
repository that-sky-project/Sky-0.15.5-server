#!/usr/bin/env bash
# =====================================================================
# 把 deploy/nginx/*.conf 装进 1Panel 的站点目录（可选步骤）
# ---------------------------------------------------------------------
# 1Panel 的 OpenResty 配置目录（按你的实际安装调整）：
#     /opt/1panel/www/sites/<域名>/
#         index/            站点根
#         proxy/            本脚本写入的位置（*.conf 会被 include）
#         ssl/              站点证书
#
# 它做的事：
#   1. 探测后端到底是 http 还是 https（wbsky/ 里有没有证书）
#   2. 从 1Panel 现有站点目录里找到证书路径，填进 __SSL_CERT__ / __SSL_KEY__
#   3. 替换占位符，写到 <站点>/proxy/skywb.conf
#   4. 用 1Panel 的 openresty 容器跑 nginx -t 校验（不通过就不 reload）
#
# 用法:
#   bash deploy/install-nginx.sh              # 装主域名 + 面板域名（存在才装）
#   bash deploy/install-nginx.sh beta.admin.xyz
#
# 装之前请先在 1Panel 里把站点建好并签发证书，否则脚本会拒绝写配置
#（写出无效 conf 会把 nginx 弄挂，这是故意加的保险）。
# =====================================================================
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
ENVF="${ROOT}/deploy/.env"
[ -f "${ENVF}" ] && { set -a; . "${ENVF}"; set +a; }

SITES_ROOT="${SITES_ROOT:-/opt/1panel/www/sites}"
MAIN_DOMAIN="${1:-${WB_SKY_DOMAIN:-beta.admin.xyz}}"
LIVE_DOMAIN="${WB_SKY_LIVE_DOMAIN:-}"
ADMIN_DOMAIN="${WB_SKY_ADMIN_DOMAIN:-}"

HTTPS_PORT="${HTTPS_PORT:-2007}"
WS_PORT="${WS_PORT:-2500}"

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; RST=$'\033[0m'
ok()   { printf '%s  [OK]%s %s\n' "${GRN}" "${RST}" "$*"; }
warn() { printf '%s  [!!]%s %s\n' "${YEL}" "${RST}" "$*"; }
err()  { printf '%s  [XX]%s %s\n' "${RED}" "${RST}" "$*"; }
die()  { err "$*"; exit 1; }

[ -d "${SITES_ROOT}" ] || die "找不到 1Panel 站点目录 ${SITES_ROOT}
       （不是 1Panel 环境？那就手工把 deploy/nginx/*.conf 贴到你的 nginx 里，
         或者直接从 443 反代到 127.0.0.1:${HTTPS_PORT}）"

# ---------------------------------------------------------------------
# 1) 后端协议：有没有证书
# ---------------------------------------------------------------------
if [ -s "${ROOT}/wbsky/fullchain.pem" ] && [ -s "${ROOT}/wbsky/privkey.key" ]; then
  SCHEME="https"
  SSL_EXTRA=$'        # 自签证书：不加这一行反代会 502（上游证书校验不过）\n        proxy_ssl_verify off;\n        proxy_ssl_server_name on;'
  ok "检测到 wbsky 证书 → 后端是 https://127.0.0.1:${HTTPS_PORT}"
  warn "注意：证书一旦重新签发/过期，反代要重新探测（再跑一次本脚本即可）"
else
  SCHEME="http"
  SSL_EXTRA=""
  ok "没找到 wbsky 证书 → 后端是 http://127.0.0.1:${HTTPS_PORT}（明文）"
fi

# ---------------------------------------------------------------------
# 2) 找 1Panel 站点证书
# ---------------------------------------------------------------------
find_cert() {
  local dom="$1" base="${SITES_ROOT}/${1}" f
  for f in "${base}/ssl/fullchain.pem" "${base}/ssl/cert.pem" \
           "/opt/1panel/www/ssl/${dom}/fullchain.pem" \
           "/opt/1panel/apps/openresty/openresty/www/ssl/${dom}/fullchain.pem"; do
    [ -s "$f" ] && { printf '%s' "$f"; return 0; }
  done
  return 1
}
find_key() {
  local dom="$1" base="${SITES_ROOT}/${1}" f
  for f in "${base}/ssl/privkey.pem" "${base}/ssl/key.pem" \
           "/opt/1panel/www/ssl/${dom}/privkey.pem" \
           "/opt/1panel/apps/openresty/openresty/www/ssl/${dom}/privkey.pem"; do
    [ -s "$f" ] && { printf '%s' "$f"; return 0; }
  done
  return 1
}

install_one() {
  local dom="$1" src="$2"
  local sitedir="${SITES_ROOT}/${dom}"

  echo
  echo "── ${dom} ──"
  [ -d "${sitedir}" ] || { warn "站点目录不存在: ${sitedir}（先在 1Panel 里建站点）"; return 1; }

  local cert key
  cert=$(find_cert "${dom}") || { warn "在 ${sitedir} 下找不到站点证书；先在 1Panel 里申请证书"; return 1; }
  key=$(find_key "${dom}")   || { warn "找不到站点私钥（配对的 privkey.pem）"; return 1; }
  ok "证书: ${cert}"

  mkdir -p "${sitedir}/proxy"
  local suffix=""
  [ "${dom}" = "${MAIN_DOMAIN}" ] || suffix="-$(printf '%s' "${dom}" | cut -d. -f1)"
  local out="${sitedir}/proxy/skywb${suffix}.conf"

  # 用 python 做替换（避免 sed 对多行内容/特殊字符的各种坑）
  python3 - "$src" "$out" "$SCHEME" "$SSL_EXTRA" "$cert" "$key" "$HTTPS_PORT" "$WS_PORT" "$dom" <<'PY'
import sys
src, out, scheme, ssl_extra, cert, key, https_port, ws_port, dom = sys.argv[1:10]
s = open(src, encoding='utf-8').read()
s = s.replace('__BACKEND_SCHEME__', scheme)
s = s.replace('__BACKEND_SSL__', ssl_extra.rstrip('\n'))
s = s.replace('__SSL_CERT__', cert)
s = s.replace('__SSL_KEY__', key)
s = s.replace('__HTTPS_PORT__', https_port)
s = s.replace('__WS_PORT__', ws_port)
s = s.replace('__DOMAIN__', dom)
open(out, 'w', encoding='utf-8', newline='\n').write(s)
print('  写入:', out)
PY

  # nginx -t 校验
  local oc
  oc=$(docker ps --format '{{.Names}}' 2>/dev/null | grep -E '1Panel-openresty|openresty' | head -1)
  if [ -n "${oc}" ]; then
    if docker exec "${oc}" nginx -t >/dev/null 2>&1; then
      ok "nginx -t 通过"
      docker exec "${oc}" nginx -s reload >/dev/null 2>&1 && ok "已 reload" || warn "reload 失败，手工执行：docker exec ${oc} nginx -s reload"
    else
      err "nginx -t **失败**，配置可能有错。下面是详情（已写入文件，但没 reload）："
      docker exec "${oc}" nginx -t 2>&1 | sed 's/^/        /'
      return 1
    fi
  else
    warn "没找到 openresty 容器，跳过 nginx -t；请手工校验并 reload"
  fi
  return 0
}

FAIL=0
install_one "${MAIN_DOMAIN}" "${ROOT}/deploy/nginx/${MAIN_DOMAIN}.conf" \
  || install_one "${MAIN_DOMAIN}" "${ROOT}/deploy/nginx/beta.admin.xyz.conf" \
  || FAIL=$((FAIL+1))

if [ -n "${LIVE_DOMAIN}" ]; then
  install_one "${LIVE_DOMAIN}" "${ROOT}/deploy/nginx/live.admin.xyz.conf" || FAIL=$((FAIL+1))
fi

# 管理后台（可写！）——只有 .env 里填了 WB_SKY_ADMIN_DOMAIN 才装。
# 强烈建议：要么不装（走 SSH 隧道），要么在站点里再套一层 Basic 认证。
if [ -n "${ADMIN_DOMAIN}" ]; then
  warn "即将为管理后台 ${ADMIN_DOMAIN} 写反代配置 —— 这是**可写**的后台，"
  warn "请确认已改掉默认口令，并考虑在 1Panel 里再加一层 Basic 认证。"
  install_one "${ADMIN_DOMAIN}" "${ROOT}/deploy/nginx/admin.admin.xyz.conf" || FAIL=$((FAIL+1))
fi

echo
echo "============================================================"
if [ "${FAIL}" -eq 0 ]; then
  printf '%s  nginx 反代安装完成%s\n' "${GRN}" "${RST}"
else
  printf '%s  有 %d 个站点没装成，看上面的提示%s\n' "${YEL}" "${FAIL}" "${RST}"
fi
cat <<EOF
  自检（必须回 101）：
    curl -i -N -H 'Connection: Upgrade' -H 'Upgrade: websocket' \\
         -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \\
         -H 'Sec-WebSocket-Version: 13' \\
         https://${MAIN_DOMAIN}/account/ws
============================================================
EOF
exit "${FAIL}"
