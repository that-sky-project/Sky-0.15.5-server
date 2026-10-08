# sky-0.15.5-server

**English:** A private server for *Sky: Children of the Light* (v0.15.5), providing full login, multiplayer, friend system, spirit trees, outfit unlocks, and a web admin panel. Built for self-hosting and educational research.

**中文：** 《光遇》私人服务端（支持 v0.15.5 客户端），包含完整登录、多人联机、好友系统、先祖兑换树、全装扮解锁和 Web 管理后台。仅供自学与研究用途。

---

## Features / 功能特性

| English | 中文 |
|---------|------|
| Full login & auto account creation | 完整登录与自动建号 |
| All realms unlocked (Isle → Home → Vault → Prairie → Forest → Valley → Wasteland → Eden) | 全地图开放（晨岛→遇境→空巢→云野→雨林→霞谷→暮土→伊甸） |
| All spirit exchange trees & emotes | 全先祖兑换树与动作解锁 |
| All outfits, capes, masks, props in wardrobe | 衣柜全服饰/斗篷/面具/道具解锁 |
| ENet UDP multiplayer (co-op, hand-holding, piggyback) | ENet UDP 多人联机（牵手/背背/同屏） |
| Friend system: candle gifting, friend tree unlock, QR invite | 好友系统：递蜡烛、好友树全解锁、扫码加好友 |
| In-game chat via WebSocket | WebSocket 游戏内聊天 |
| Mock in-app purchase (no Google Play) | 模拟内购（不走 Google Play） |
| Web admin panel (currency, unlocks, friends, logs) | Web 管理后台（货币/解锁/好友/日志） |
| License check disabled (emulator-friendly) | license 校验关闭（模拟器可玩） |

---

## Screenshots / 截图

### In-game / 游戏内

| | | |
|---|---|---|
| ![](screenshots/01.jpg) | ![](screenshots/02.jpg) | ![](screenshots/03.jpg) |
| ![](screenshots/04.jpg) | ![](screenshots/05.jpg) | ![](screenshots/06.jpg) |
| ![](screenshots/07.jpg) | ![](screenshots/08.jpg) | ![](screenshots/09.jpg) |
| ![](screenshots/10.jpg) | ![](screenshots/11.jpg) | ![](screenshots/12.jpg) |
| ![](screenshots/13.jpg) | | |

### Admin Panel / 管理后台

| | | |
|---|---|---|
| ![](screenshots/14.png) | ![](screenshots/15.png) | ![](screenshots/16.png) |
| ![](screenshots/17.png) | ![](screenshots/18.png) | ![](screenshots/19.png) |
| ![](screenshots/20.png) | ![](screenshots/21.png) | ![](screenshots/22.png) |
| ![](screenshots/23.png) | | |

---

## Tech Stack / 技术栈

| Component | Technology |
|-----------|-----------|
| HTTP API | Python / Flask (port 2007) |
| Multiplayer | Node.js / ENet UDP (port 19458) |
| Chat | Python WebSocket (port 2500) |
| Database | MySQL |
| Admin Panel | Built-in HTML/CSS/JS |
| Deployment | Docker / docker-compose / supervisor |

---

## Quick Start / 快速开始

### Requirements / 环境要求
- Ubuntu 22.04 / Debian 12, 2 vCPU / 4 GB RAM
- Docker & docker compose
- MySQL 8
- Sky v0.15.5 Android client (separate, not included)

### 1. Get the code / 获取代码
```bash
git clone https://github.com/that-sky-project/Sky-0.15.5-server.git
cd Sky-0.15.5-server
```

### 2. Configure / 配置
Edit `wbsky/config.json`:
```json
{
  "mysql_host": "host.docker.internal",
  "mysql_user": "skygame",
  "mysql_password": "your_password",
  "mysql_database": "skygame",
  "udp_server_host": "YOUR_SERVER_PUBLIC_IP",
  "admin_password": "change_me",
  "license_enabled": false,
  "all_users_allunlock": true
}
```

### 3. Initialize database / 初始化数据库
```bash
mysql -u root -p skygame < wbsky/sql/init.sql
```

### 4. Start / 启动
```bash
docker compose up -d
```

### 5. Open ports / 放行端口
- `2007/tcp` — HTTP API
- `19458/udp` — ENet multiplayer
- `2500/tcp` — WebSocket chat

### 6. Client / 客户端

**Download / 下载：** [Quark Pan / 夸克网盘](https://pan.quark.cn/s/d22b0d8e049a)

#### How to change server domain / 修改服务器地址

The prebuilt APK points to a default server. To point it to **your** server, edit `classes2.dex`:

预编译 APK 默认指向官方服务器。要改成你自己的服务器地址，修改 `classes2.dex`：

1. Open the APK in MT Manager / NP Manager / Dex Editor++ / 用 MT管理器/NP管理器/Dex编辑器++ 打开 APK
2. Enter `classes2.dex` → `com.tgc.sky` → `BuildConfig` / 进入 `classes2.dex` → `com.tgc.sky` → `BuildConfig`
3. Find `SKY_SERVER_HOSTNAME` and change the string to your server domain/IP / 找到 `SKY_SERVER_HOSTNAME`，把字符串改成你的服务器域名或 IP
4. Save and re-sign the APK / 保存后重新签名安装

| | |
|---|---|
| ![BuildConfig class](screenshots/24-buildconfig-class.jpg) | ![Edit SKY_SERVER_HOSTNAME](screenshots/25-sky-server-hostname.jpg) |
| ![APK structure](screenshots/26-apk-structure.jpg) | ![Dex editor](screenshots/27-dex-editor.jpg) |

---

## Admin Panel / 管理后台

Open `http://YOUR_IP:2007/admin` in a browser.

- **Users**: edit any player's candles / hearts / ascended candles / unlocks
- **Friends**: directly bind two User IDs as friends
- **Logs**: real-time request inspection

---

## FAQ / 常见问题

**Q: Client crashes on login? / 登录闪退？**
A: Check `docker logs wbsky` for HTTP 500 errors. Most often a MySQL config mismatch. / 看日志找 500 接口，通常是数据库配置错。

**Q: Can't see other players in-game? / 进图看不到人？**
A: Confirm UDP port 19458 is open in firewall. / 检查防火墙是否放行 19458 UDP。

**Q: Friend tree nodes locked? / 好友树节点锁着？**
A: In admin panel, set relationship_level to 65 and refill abilities. / 后台改 relationship_level=65 并刷全 abilities。

---

## Disclaimer / 免责声明

This project is for educational and research purposes only. It is not affiliated with, endorsed by, or connected to thatgamecompany or NetEase. Sky: Children of the Light is a trademark of thatgamecompany. All game assets belong to their respective owners. Do not use this to run a public service.

本项目仅供学习与研究使用，与 thatgamecompany、网易无任何关联。《光遇》商标及游戏素材版权归原作者所有。请勿用于公开运营。

---

## License / 许可

MIT — see source files for details.
