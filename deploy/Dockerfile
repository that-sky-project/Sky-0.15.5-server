# ---- 光遇私服 wbsky · 运行镜像 ----
# =====================================================================
# 为什么是 python:3.10-slim：
#   现网实测环境就是 Python 3.10.12（见 VERSIONS.txt），依赖版本也按那份
#   pip freeze 钉的。换大版本（3.12/3.13）虽然大概率能跑，但这份包的目标是
#   "照着现在的机器一模一样地复刻出来"，所以不冒险升级。
# =====================================================================
FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    APP_DIR=/app \
    # ★ 让 deploy/sitecustomize.py 在解释器启动阶段被自动加载。
    #   它负责：关调试器、覆盖端口/监听地址、房间服地址、live_panel 适配、
    #   建库容错。它是 bind mount 进来的，**改完不用重建镜像**，重启容器即可。
    PYTHONPATH=/app/deploy

# curl  → 健康检查与 deploy/check.sh 要用
# iproute2 → 提供 ss，实时面板 /live 靠 `ss -tinH` 采样客户端 TCP RTT
#            （p94 的功能；没有 ss 只是面板不显示 RTT，不影响游戏）
# tzdata → 日志时间用 Asia/Shanghai
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl tzdata iproute2 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖，利用层缓存（requirements 不变时不用重装）
COPY deploy/requirements.txt /app/deploy/requirements.txt
RUN pip install --no-cache-dir -U pip \
 && pip install --no-cache-dir -r /app/deploy/requirements.txt

# 再拷源码。注意 compose 里 /app 会被宿主机目录覆盖，
# 这里的 COPY 是为了让镜像「单独 docker run 也能跑」。
COPY . /app

RUN chmod +x /app/deploy/entrypoint.sh \
 && mkdir -p /app/wbsky/logs /app/wbsky/db

# 容器内固定监听：
#   2007/tcp  主服务（HTTPS 或明文 HTTP，由有无证书决定）
#   2500/tcp  聊天 WebSocket 中继（ws_server.py，纯标准库）
#
#   这两个端口号与 .env 的 HTTPS_PORT / WS_PORT 保持一致 ——
#   容器内外同号，`docker exec` 里自查时不用换算，排障少一层困惑。
# 房间服（sky0155-udp）是单独的 node 容器，不在这里。
EXPOSE 2007 2500

# 这里刻意【不写 HEALTHCHECK】：
# 同一个镜像被 wbsky / ws 两个服务复用，而它们监听不同端口。
# 写在镜像里就会有一个必然失败，容器永远显示 unhealthy。
# 健康检查在 docker-compose.yml 里按服务分别定义。

ENTRYPOINT ["/app/deploy/entrypoint.sh"]
CMD ["server"]
