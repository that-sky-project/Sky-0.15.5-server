// main.js — 0.15.5 (Light Awaits) ENet UDP 房间服务器入口
// 基于 xysky-udp 的 sky-enet 架构，适配 0.15.5 协议（移植自 udp_relay_enet.py）
// 已删除多余接口：34.5 的 MoveGame / 房间迁移 / qwd 注册等
import { readFileSync, existsSync } from 'node:fs';
import { createServer } from 'node:http';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Server } from 'sky-enet';
import { RoomServer, PeerEntry } from './server.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = dirname(__dirname);

// ─── 日志 ───────────────────────────────────────────────────────
const ts = () => new Date().toISOString().replace('T', ' ').slice(0, 19);
const logger = {
  info: (...a) => console.log(ts(), '[INFO]', ...a),
  warning: (...a) => console.log(ts(), '[WARN]', ...a),
  error: (...a) => console.log(ts(), '[ERROR]', ...a),
  debug: (...a) => { if (cfg?.udp_debug_packets) console.log(ts(), '[DEBUG]', ...a); },
};
let cfg = {};

// ─── 配置加载 ───────────────────────────────────────────────────
function loadConfig() {
  const candidates = [
    join(ROOT, 'config.json'),
    '/app/config.json',          // 容器内服务端配置（/wbsky 挂载点）
  ];
  const merged = {};
  for (const p of candidates) {
    if (!existsSync(p)) continue;
    try {
      const raw = readFileSync(p, 'utf-8');
      const j = JSON.parse(raw);
      // 兼容 /wbsky/config.json 的 udp_ 前缀键 + 本服务器独立键
      for (const [k, v] of Object.entries(j)) {
        if (k.startsWith('udp_') || k === 'http_enable' || k === 'http_port') merged[k] = v;
      }
    } catch (err) {
      logger.warning(`读取配置 ${p} 失败: ${err.message}`);
    }
  }
  // 本地 config.json 优先
  const local = join(ROOT, 'config.json');
  if (existsSync(local)) {
    try {
      const j = JSON.parse(readFileSync(local, 'utf-8'));
      Object.assign(merged, j);
    } catch { /* ignore */ }
  }
  return merged;
}

// ─── 健康检查 HTTP（极简，仅探活/状态，无多余接口）───────────────
function startHealthServer(port) {
  const srv = createServer((req, res) => {
    const url = req.url || '/';
    if (url === '/health') {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ status: 'ok', online: room.getStats().online }));
      return;
    }
    if (url === '/stats' || url === '/peers') {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify(room.getStats(), null, 2));
      return;
    }
    res.writeHead(404, { 'content-type': 'text/plain' });
    res.end('not found');
  });
  srv.listen(port, '0.0.0.0', () => {
    logger.info(`[HTTP] 健康检查监听 0.0.0.0:${port} (/health, /stats)`);
  });
  srv.on('error', err => logger.warning(`[HTTP] ${port} 监听失败（可能被占用，忽略）: ${err.message}`));
  return srv;
}

// ─── 主流程 ─────────────────────────────────────────────────────
cfg = loadConfig();

// ─── 环境变量覆盖（Docker / 1Panel 部署用；不设就完全按老行为走）─────
// 为什么要有这一段：本进程原来只认 config.json 里的 udp_server_port，
// 换服务器时"改 .env 一处"做不到 —— 必须同时改 config.json、
// 还要记得同步宿主机端口映射，漏一个就变成"端口对不上、进游戏看不到人"。
// 现在统一成：
//   WB_SKY_UDP_PORT       本进程 UDP 监听端口（容器内）＝下发给客户端的端口
//   WB_SKY_UDP_HOST       本进程绑定地址（默认 0.0.0.0）
//   WB_SKY_UDP_HTTP_PORT  /health 与 /stats 的监听端口
//                         （默认跟 UDP 同端口；UDP 与 TCP 是两套命名空间，
//                          同号不冲突，与原行为一致）
function _envPort(name) {
  const raw = String(process.env[name] ?? '').trim();
  if (!raw) return null;
  const n = Number(raw);
  return Number.isInteger(n) && n > 0 && n < 65536 ? n : null;
}

const port = _envPort('WB_SKY_UDP_PORT') ?? Number(cfg.udp_server_port ?? 8125);
const host = String(process.env.WB_SKY_UDP_HOST ?? '').trim() || cfg.udp_server_host || '0.0.0.0';
// ★ /health 与 /stats 的端口必须和 UDP 端口**分开**：
//   TCP 与 UDP 虽然是两套命名空间（同号不冲突），但 Docker 的端口映射
//   一个容器端口只能映射一种协议，两处都写同一个号时宿主侧就会打架。
//   所以默认挪到 11925，并由 WB_SKY_UDP_HTTP_PORT 控制（缺省回退到
//   config.json 的 http_port，再缺省才跟 UDP 同号）。
const httpPort = _envPort('WB_SKY_UDP_HTTP_PORT')
  ?? Number(cfg.http_port ?? 11925);
cfg.udp_server_port = port;      // 让 selftest / 日志 / 内部逻辑看到的是最终值
cfg.udp_server_host = host;
cfg.http_port = httpPort;
const maxClients = Number(cfg.udp_max_clients ?? 64);
const channels = Number(cfg.udp_channel_count ?? 2);

const room = new RoomServer(cfg, logger);

logger.info('='.repeat(62));
logger.info('  Sky 光遇 0.15.5 ENet 游戏服务器 (Node.js)');
logger.info(`  监听        : ${host}:${port}/udp`);
logger.info(`  最大连接    : ${maxClients}    通道数: ${channels}`);
logger.info(`  房间模式    : ${cfg.udp_room_mode ?? 'level'}`);
logger.info(`  客户端超时  : ${cfg.udp_client_timeout ?? 90}s    tick: ${cfg.udp_tick_rate ?? 10}Hz`);
logger.info(`  CRC32 校验  : ${cfg.udp_enable_crc32 ?? true ? '开启' : '关闭'}`);
logger.info(`  关卡 ID 归一: ${cfg.udp_level_id_canon ?? true ? '开启' : '关闭'}    严格分图: ${cfg.udp_strict_level_group ?? true ? '是' : '否'}`);
logger.info(`  名单刷新    : ${cfg.udp_roster_refresh ?? 'entergame'}      level_seq: ${cfg.udp_lvseq_mode ?? 'follow'}`);
logger.info(`  下行包头    : ${cfg.udp_tx_header ?? 'len3'} ([packet_id][len:u16 LE] 三字节)   PlayerState: ${cfg.udp_player_state_framing ?? 'aggregate'}${(cfg.udp_state_raw_only !== false) ? ' (只发 Raw 全量帧，不发差分)' : ''}`);
logger.info(`  关卡权威    : ${cfg.udp_authority_mode ?? 'shared'}${(cfg.udp_authority_mode ?? 'shared') === 'self' ? ` (人人都是自己的 host；服务端一帧关卡数据都不发，ELECT=${cfg.udp_self_elect ? '发' : '不发'})` : ' (整图共享权威，服务端只转发权威真正上传过的存档)'}`);
logger.info('='.repeat(62));

// 关联 PeerEntry 的发送/断开回调
const wireSend = (peerId, channel, data, reliable = true) => {
  try {
    return enetServer.send(peerId, channel, data, reliable);
  } catch (err) {
    logger.debug(`发送失败: ${err.message}`);
    return -1;
  }
};
const wireDisconnect = peerId => {
  try { enetServer.disconnectNow(peerId); } catch { /* ignore */ }
};

let enetServer;
async function main() {
  enetServer = await Server.create({
    ip: host,
    port,
    maxPeer: maxClients,
    maxPeers: maxClients,
    channelLimit: channels,
    checksum: cfg.udp_enable_crc32 ?? true, // 光遇客户端启用 CRC32，必须一致
  });

  for (const entry of room.peers.values()) {
    entry.setSendFn?.(wireSend);
  }
  // 让新 PeerEntry 也能拿到发送回调
  const origOnConnect = room.onConnect.bind(room);
  room.onConnect = (peerId, addr) => {
    origOnConnect(peerId, addr);
    const e = room.peers.get(peerId);
    if (e) {
      e.setSendFn(wireSend);
      e.setDisconnectFn(wireDisconnect);
    }
  };

  enetServer
    .on('ready', () => logger.info(`[ENET] 已启用 CRC32 校验，开始监听 ${host}:${port}…`))
    .on('connect', ev => room.onConnect(ev.peer, String(ev.peer)))
    .on('disconnect', ev => room.onDisconnect(ev.peer))
    .on('receive', ev => room.onReceive(ev.peer, ev.channelID, ev.data))
    .on('error', err => logger.error(`[ENET] 传输错误: ${err.message}`));

  await new Promise((resolve, reject) => {
    const onReady = () => { enetServer.off('error', onError); resolve(); };
    const onError = err => { enetServer.off('ready', onReady); reject(err); };
    enetServer.once('ready', onReady);
    enetServer.once('error', onError);
    enetServer.listen().catch(err => { logger.error(`[ENET] listen 失败: ${err.message}`); reject(err); });
  });

  room.setSendFn?.(wireSend);
  room.startTicking(wireSend);

  if (cfg.http_enable) {
    startHealthServer(Number(cfg.http_port ?? port));
  }

  const stop = () => {
    logger.info('收到退出信号，正在关闭…');
    room.stopTicking();
    try { enetServer.stop(); } catch { /* ignore */ }
    try { enetServer.deinitialize(); } catch { /* ignore */ }
    process.exit(0);
  };
  process.on('SIGINT', stop);
  process.on('SIGTERM', stop);

  logger.info('服务器已启动');
}

main().catch(err => {
  logger.error(`启动失败: ${err?.stack || err}`);
  process.exit(1);
});
