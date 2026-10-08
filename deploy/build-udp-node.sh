#!/usr/bin/env bash
# =====================================================================
# 提前编译房间服的 sky-enet 原生模块
# ---------------------------------------------------------------------
# 为什么要单独跑一次：
#   sky-enet 是原生模块（node-gyp），npm 包里只带 Windows 预编译，
#   Linux 上要现场编译（需要 python3 + make + g++）。这个编译放在
#   `up -d` 的首次启动里做的话，容器要等好几分钟，而且日志混在一起
#   容易被误判成"起不来"。先跑本脚本能**看着编译日志**做完这一次。
#
# 编好的结果落在 sky0155-udp/node_modules（宿主机上），
# 换服务器时把这个目录一起拷过去即可（同架构 + 同 Node 大版本）。
#
# 用法: bash deploy/build-udp-node.sh
# =====================================================================
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
UDPDIR="${ROOT}/sky0155-udp"

command -v docker >/dev/null 2>&1 || { echo "没有 docker 命令"; exit 1; }
[ -f "${UDPDIR}/package.json" ] || { echo "找不到 ${UDPDIR}/package.json"; exit 1; }

if [ -f "${UDPDIR}/node_modules/sky-enet/package.json" ]; then
  echo "  sky-enet 已存在，跳过编译（要强制重编就删掉 sky0155-udp/node_modules）"
  exit 0
fi
# ★ 注意这里是判断**包文件**在不在，不是判断 node_modules 目录在不在。
#   踩过的坑：compose 挂载 ../sky0155-udp/node_modules 时 Docker 会自动创建
#   这个空目录，于是"目录存在"永远为真 —— 判断一旦写成 -d node_modules，
#   就会误判成"已经装好了"，然后 node 起来报 Cannot find package 'sky-enet'。
if [ -d "${UDPDIR}/node_modules" ] && [ ! -f "${UDPDIR}/node_modules/sky-enet/package.json" ]; then
  echo "  检测到 node_modules 目录存在但缺少 sky-enet（很可能是上次装了一半的残留）"
  echo "  先清掉它再重装：rm -rf ${UDPDIR}/node_modules"
  rm -rf "${UDPDIR}/node_modules"
fi

echo "============================================================"
echo "  编译 sky-enet（node:20 完整版镜像，带 python3/make/g++）"
echo "  目录: ${UDPDIR}"
echo "  这一步可能要 3~10 分钟，取决于机器"
echo "============================================================"

docker run --rm \
  -v "${UDPDIR}:/app" \
  -w /app \
  -e npm_config_registry="${NPM_REGISTRY:-https://registry.npmmirror.com}" \
  node:20 \
  sh -lc 'npm install --omit=dev --no-audit --no-fund'

echo
if [ -f "${UDPDIR}/node_modules/sky-enet/package.json" ]; then
  echo "  [OK] 依赖就绪：$(ls -d "${UDPDIR}"/node_modules/sky-enet)"
  echo "    （node_modules 请一起备份/迁移，但不要提交到 git）"
else
  echo "  [X] 没编出来。常见原因："
  echo "     · 机器访问 npm 源超时 —— 换源："
  echo "         NPM_REGISTRY=https://registry.npmjs.org bash deploy/build-udp-node.sh"
  echo "     · 换 yarn/pnpm 产生的 lock 不兼容"
  echo "     · 磁盘空间不足"
  exit 1
fi
