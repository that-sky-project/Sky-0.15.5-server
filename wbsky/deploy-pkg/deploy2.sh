#!/usr/bin/env bash
# wbSky 部署 阶段2 —— 用 /tmp/wbsky-deploy/x 与 /tmp/gc 里已解压的文件
set -uo pipefail

APP=/wbsky
UDP=/opt/sky0155-udp
SRC=/tmp/wbsky-deploy/x
BIG=/tmp/gc/config
MYSQL_C=1Panel-mysql-KC7W
DB=wbsky
DBU=wbsky
DBP='SkyServer2026!'

G=$'\033[32m'; R=$'\033[33m'; E=$'\033[31m'; C=$'\033[36m'; N=$'\033[0m'
ok(){ echo "${G}[OK]${N} $*"; }; wr(){ echo "${R}[!!]${N} $*"; }
er(){ echo "${E}[XX]${N} $*"; }; hd(){ echo; echo "${C}=== $* ===${N}"; }

hd "0. 前置检查"
[ -d "$SRC/wbsky" ] && ok "源码 $SRC/wbsky" || { er "缺 $SRC/wbsky"; exit 1; }
[ -d "$SRC/sky0155-udp" ] && ok "房间服 $SRC/sky0155-udp" || wr "无 sky0155-udp"
[ -f "$BIG/outfit_defs.json" ] && ok "大配置 outfit_defs.json" || wr "缺大配置"
docker ps --format '{{.Names}}' | grep -qx "$MYSQL_C" && ok "MySQL 容器 $MYSQL_C" || wr "没找到 $MYSQL_C"

hd "1. 停旧房间服（释放 8125）"
systemctl stop sky0155-udp 2>/dev/null && ok "已停 sky0155-udp" || echo "  - 未运行"
pkill -f 'sky0155-udp' 2>/dev/null; sleep 1
ss -lunp 2>/dev/null | grep -q ':8125' && wr "8125 仍被占用" || ok "8125 已释放"

hd "2. 部署文件"
mkdir -p "$APP/logs" "$UDP"
[ -d "$UDP" ] && cp -a "$UDP" "/opt/sky0155-udp.bak.$(date +%s)" 2>/dev/null && echo "  旧房间服已备份"
cp -a "$SRC/wbsky/." "$APP/" && ok "源码 -> $APP"
cp -a "$SRC/sky0155-udp/." "$UDP/" && ok "房间服 -> $UDP"
mkdir -p "$APP/config"
if [ -f "$BIG/outfit_defs.json" ]; then
  cp -f "$BIG/outfit_defs.json" "$APP/config/"
  cp -f "$BIG/outfit_defs_client.json" "$APP/config/" 2>/dev/null
  ok "大配置已放入 $APP/config"
fi

hd "3. 重建数据库"
mysql_run(){ docker exec -i "$MYSQL_C" sh -c 'exec mysql -uroot -p"$MYSQL_ROOT_PASSWORD"' 2>/tmp/myerr; }
ERRLOG=/tmp/myerr
printf '%s\n' \
  "DROP DATABASE IF EXISTS \`$DB\`;" \
  "CREATE DATABASE \`$DB\` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;" \
  "CREATE USER IF NOT EXISTS '$DBU'@'%' IDENTIFIED BY '$DBP';" \
  "ALTER USER '$DBU'@'%' IDENTIFIED BY '$DBP';" \
  "GRANT ALL PRIVILEGES ON \`$DB\`.* TO '$DBU'@'%';" \
  "FLUSH PRIVILEGES;" | mysql_run >/dev/null
if grep -qiE 'error|denied' "$ERRLOG"; then er "建库报错:"; head -3 "$ERRLOG"; else ok "数据库 $DB 已重建 + 授权 $DBU"; fi
echo "  表: $(printf 'SHOW TABLES;\n' | docker exec -i "$MYSQL_C" sh -c 'exec mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -N -B '"$DB" 2>/dev/null | tr '\n' ' ')"

hd "4. 配置 config.json"
python3 - "$APP/config.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p, encoding='utf-8'))
d.update({
    "udp_server_host": "103.24.218.98",
    "udp_server_port": 8125,
    "use_mysql": True,
    "mysql_host": "127.0.0.1",
    "mysql_port": 3306,
    "mysql_user": "wbsky",
    "mysql_password": "SkyServer2026!",
    "mysql_database": "wbsky",
})
json.dump(d, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
print("    udp_server_host=%s  mysql_host=%s" % (d["udp_server_host"], d["mysql_host"]))
PY
ok "config.json 已配好"

hd "5. Python 环境"
python3 -m venv "$APP/venv" 2>/dev/null && ok "venv 已建" || wr "venv 已存在或失败"
"$APP/venv/bin/pip" install -q --upgrade pip 2>&1 | tail -1
"$APP/venv/bin/pip" install -q flask pymysql requests 2>&1 | tail -2
"$APP/venv/bin/python" -c "import flask, pymysql, requests; print('    依赖 OK: flask', flask.__version__)" && ok "依赖安装完成"

hd "6. 起服务"
cat > /etc/systemd/system/wbsky-server.service <<'EOF'
[Unit]
Description=wbSky Flask account server (2999)
After=network.target docker.service
[Service]
Type=simple
WorkingDirectory=/wbsky
Environment=PYTHONUNBUFFERED=1
Environment=TZ=Asia/Shanghai
ExecStart=/wbsky/venv/bin/python -u index.py
Restart=always
RestartSec=3
StandardOutput=append:/wbsky/logs/server.out.log
StandardError=append:/wbsky/logs/server.out.log
[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/wbsky-ws.service <<'EOF'
[Unit]
Description=wbSky WebSocket server (9002)
After=network.target
[Service]
Type=simple
WorkingDirectory=/wbsky
Environment=PYTHONUNBUFFERED=1
Environment=TZ=Asia/Shanghai
ExecStart=/wbsky/venv/bin/python -u ws_server.py 9002
Restart=always
RestartSec=3
StandardOutput=append:/wbsky/logs/ws.out.log
StandardError=append:/wbsky/logs/ws.out.log
[Install]
WantedBy=multi-user.target
EOF

NODEBIN=$(command -v node)
cat > /etc/systemd/system/sky0155-udp.service <<EOF
[Unit]
Description=sky0155-udp ENet room server
After=network.target
[Service]
Type=simple
WorkingDirectory=$UDP
Environment=TZ=Asia/Shanghai
ExecStart=$NODEBIN src/main.js
Restart=always
RestartSec=2
[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable wbsky-server wbsky-ws sky0155-udp >/dev/null 2>&1
systemctl restart wbsky-server wbsky-ws sky0155-udp
sleep 6
for s in wbsky-server wbsky-ws sky0155-udp; do
  printf '  %-16s %s\n' "$s" "$(systemctl is-active $s 2>/dev/null)"
done

hd "7. 自检"
echo "--- 监听 ---"
ss -lntp 2>/dev/null | grep -E ':(2999|9002)\b' || wr "2999/9002 未监听"
ss -lunp 2>/dev/null | grep -E ':8125\b' || wr "8125/udp 未监听"
echo "--- API ---"
A=$(curl -s -m 8 -X POST http://127.0.0.1:2999/account/get_vars -H 'Content-Type: application/json' -d '{}' 2>/dev/null)
echo "$A" | grep -q vars && ok "账号服 API 正常" || { er "API 异常: ${A:0:200}"; }
echo "--- WS ---"
W=$(curl -s -m 8 -o /dev/null -w '%{http_code}' http://127.0.0.1:9002/account/ws -H 'Upgrade: websocket' -H 'Connection: Upgrade' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' -H 'Sec-WebSocket-Version: 13' 2>/dev/null)
[ "$W" = "101" ] && ok "WS 握手 101" || er "WS 返回 $W"
echo "--- UDP ---"
U=$(curl -s -m 8 http://127.0.0.1:8125/health 2>/dev/null)
echo "$U" | grep -q ok && ok "UDP: $U" || er "UDP 无响应"
echo
echo "--- 反代实测 ---"
for u in "http://127.0.0.1/account/get_vars" "https://live.admin.xyz/account/get_vars"; do
  c=$(curl -sk -m 10 -o /dev/null -w '%{http_code}' -X POST "$u" -H 'Content-Type: application/json' -d '{}' 2>/dev/null)
  echo "  $u -> $c"
done
echo "___DEPLOY2_DONE___"
