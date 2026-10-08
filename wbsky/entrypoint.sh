#!/usr/bin/env bash
# ============================================================================
# 【已弃用 · 仅作参考】wbsky 目录下这份 entrypoint 是老版本，
# 新部署请用 deploy/entrypoint.sh（项目根目录下的 deploy/）。
# ----------------------------------------------------------------------------
# 老版本的三个问题：
#   1) 每次启动都 pip install -r /app/requirements.txt —— 依赖装了两份，
#      启动慢、且容器无外网时直接失败。
#   2) 不加载 deploy/sitecustomize.py，于是端口/监听地址/面板口令/
#      建库容错全都不生效（源码里 debug=True 会真的开着）。
#   3) requirements.txt 只有三行（requests/flask/pymysql），没有版本约束。
#
# 新入口：deploy/entrypoint.sh   支持 server / ws / shell / 直通 四种模式
# 编排  ：deploy/docker-compose.yml
# ============================================================================
# 启动脚本 - 容器启动时先安装依赖再运行
#!/bin/bash
pip install --no-cache-dir -r /app/requirements.txt
python /app/index.py
