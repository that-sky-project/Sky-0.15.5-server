# route/client.py
# -*- coding: utf-8 -*-
"""客户端版本接口 —— 给包体里的「检查更新」用。

客户端（com.hwb.sky / cn.gvqi.sky 的 classes3.dex → com.hwb.sky.UpdateCheck）
启动时请求 `GET /client/version`，拿到 JSON 里的 build 号与自己的 MY_BUILD 比对，
大了就在游戏内面板下方挂一条「有新版本，点此更新」的提示条（点了用浏览器打 url）。

返回示例：
    {
      "status": "ok",
      "build": 2026100301,          # 最新包的构建号（单调递增整数）
      "name":  "2026-10-03",        # 给玩家看的版本名
      "url":   "http://beta.admin.xyz/dl/sky.apk",
      "note":  "修了联机看不到人",   # 可选，显示在提示条第二行
      "min_build": 0                # 可选：低于它的版本视为必须更新（预留）
    }

配置（config.json）：
    "client_latest_build": 2026100301,
    "client_latest_name":  "2026-10-03",
    "client_apk_url":      "http://beta.admin.xyz/dl/sky.apk",
    "client_update_note":  ""

★ 出包纪律：**每次重新出包都要把 `client_latest_build` 调大**（并把客户端的
  UpdateCheck.MY_BUILD 设成同一个值），否则新包不会被老客户端识别成"有新版本"。
★ APK 直链：没配 client_apk_url 时按 config.json 的 udp_server_host（公网 IPv4）
  拼出来，见下面的 _default_apk_url。想让直链走域名（推荐）就显式配
  client_apk_url；nginx 侧的 /dl/ 目录说明见 deploy/nginx/beta.admin.xyz.conf。
"""
import json
import os

from flask import Blueprint, jsonify, request

client_bp = Blueprint("client", __name__, url_prefix="/client")

# 与客户端 UpdateCheck.MY_BUILD 保持一致；配置缺项时用这个值 = "大家都已是最新"
DEFAULT_BUILD = 2026100301
DEFAULT_NAME = "2026-10-03"


def _load_config():
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def _default_apk_url(cfg):
    """没配 client_apk_url 时，按 udp_server_host 推一个下载直链出来。"""
    host = str(cfg.get("udp_server_host", "") or "").strip()
    if not host or host in ("auto", "0.0.0.0", "127.0.0.1", "localhost"):
        try:
            host = request.host.split(":")[0]
        except Exception:
            host = ""
    port = int(cfg.get("client_apk_port", 80) or 80)
    suffix = "" if port == 80 else ":%d" % port
    return "http://%s%s/dl/sky.apk" % (host, suffix)


@client_bp.route("/version", methods=["GET", "POST"])
def version():
    try:
        cfg = _load_config()
    except Exception as e:
        # 读不到配置也不能让客户端卡住：回一个"已是最新"
        return jsonify({"status": "error", "error": repr(e),
                        "build": DEFAULT_BUILD, "name": DEFAULT_NAME, "url": ""})

    try:
        build = int(cfg.get("client_latest_build", DEFAULT_BUILD))
    except (TypeError, ValueError):
        build = DEFAULT_BUILD

    name = str(cfg.get("client_latest_name", DEFAULT_NAME) or DEFAULT_NAME)
    url = str(cfg.get("client_apk_url", "") or "").strip() or _default_apk_url(cfg)
    note = str(cfg.get("client_update_note", "") or "")
    try:
        min_build = int(cfg.get("client_min_build", 0) or 0)
    except (TypeError, ValueError):
        min_build = 0

    return jsonify({
        "status": "ok",
        "build": build,
        "name": name,
        "url": url,
        "note": note,
        "min_build": min_build,
    })
