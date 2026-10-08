#!/usr/bin/env bash
# =====================================================================
# 生成自签证书（放进 wbsky/，让主服务能起 HTTPS）
# ---------------------------------------------------------------------
# 为什么自签就够：
#   配套的客户端补丁在**系统 TLS 层跳过了证书校验**，所以不需要
#   （也**不要**）去申请 Let's Encrypt 覆盖它。
#
# 用法：
#     bash deploy/make-cert.sh                    # 用 .env 里的域名
#     bash deploy/make-cert.sh beta.example.com 1.2.3.4
#
# 生成物（覆盖前会先备份）：
#     wbsky/fullchain.pem     证书
#     wbsky/privkey.key       私钥（index.py 找的就是这两个名字）
# =====================================================================
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
ENVF="${ROOT}/deploy/.env"
[ -f "${ENVF}" ] && { set -a; . "${ENVF}"; set +a; }

CN="${1:-${WB_SKY_CERT_CN:-${WB_SKY_DOMAIN:-beta.admin.xyz}}}"
IP="${2:-${WB_SKY_CERT_IP:-${WB_SKY_UDP_HOST:-103.24.218.98}}}"
DAYS="${WB_SKY_CERT_DAYS:-3650}"

CRT="${ROOT}/wbsky/fullchain.pem"
KEY="${ROOT}/wbsky/privkey.key"
STAMP="$(date +%Y%m%d-%H%M%S)"

command -v openssl >/dev/null 2>&1 || { echo "宿主机没有 openssl：apt install -y openssl"; exit 1; }

echo "============================================================"
echo "  生成自签证书"
echo "  CN   : ${CN}"
echo "  SAN  : DNS:${CN}, IP:${IP}, DNS:localhost, IP:127.0.0.1"
echo "  有效期: ${DAYS} 天"
echo "  输出 : wbsky/fullchain.pem + wbsky/privkey.key"
echo "============================================================"

for f in "${CRT}" "${KEY}"; do
  if [ -s "${f}" ]; then
    cp -a "${f}" "${f}.bak_${STAMP}"
    echo "  已备份旧文件 → $(basename "${f}").bak_${STAMP}"
  fi
done

openssl req -x509 -newkey rsa:2048 -nodes -days "${DAYS}" \
  -keyout "${KEY}" -out "${CRT}" \
  -subj "/C=CN/O=skywb/CN=${CN}" \
  -addext "subjectAltName=DNS:${CN},IP:${IP},DNS:localhost,IP:127.0.0.1" \
  && echo "  [OK] 已生成" || { echo "  [X] openssl 失败"; exit 1; }

echo
echo "回读校验（确认证书与私钥是配对的）："
CRT_MOD=$(openssl x509 -noout -modulus -in "${CRT}" | openssl md5)
KEY_MOD=$(openssl rsa  -noout -modulus -in "${KEY}" | openssl md5)
echo "  cert modulus: ${CRT_MOD}"
echo "  key  modulus: ${KEY_MOD}"
if [ "${CRT_MOD}" = "${KEY_MOD}" ]; then
  echo "  [OK] 配对正确"
else
  echo "  [X] 不配对！请检查是不是手工替换过文件"
  exit 1
fi
openssl x509 -in "${CRT}" -noout -subject -dates -ext subjectAltName | sed 's/^/  /'

echo
echo "接下来：docker compose -f deploy/docker-compose.yml --env-file deploy/.env restart wbsky"
echo "自检  ：curl -kI https://127.0.0.1:${HTTPS_PORT:-2007}/healthz"
