// protocol.js — 0.15.5 (Light Awaits) ENet UDP 协议常量与编解码
// 移植自 udp_relay_enet.py（Python 版，已验证），按 xysky-udp 的 Node.js 风格重写
// 已删除 34.5 才需要的 MoveGame / CancelMove / qwd 房间迁移等多余接口

// ─── 顶层 PacketId ─────────────────────────────────────────────
// 客户端 -> 服务端: [packet_id:u8][seq:u8][payload]
// 服务端 -> 客户端: [packet_id:u8][payload_len:u16 LE][payload]
export const PKT_CLIENT_CONNECT = 0;
export const PKT_DISCONNECT = 1;
export const PKT_KICK = 2;
export const PKT_JOIN_GAME = 3;
export const PKT_LEVEL_UPDATE = 4;
export const PKT_ENTER_GAME_DEPRECATED = 5;
export const PKT_PLAYER_JOINED = 6;
export const PKT_PLAYER_LEFT = 7;
export const PKT_PLAYER_CHANGED_LEVEL = 8;
// 9 = MoveGame, 10 = MoveResult, 11 = CancelMove （34.5 房间迁移，0.15.5 不需要 → 已删除）
export const PKT_NET_TIME_PING = 12;
export const PKT_NET_TIME_PONG = 13;
export const PKT_GAME_MSG = 14;
export const PKT_ENTER_GAME = 17;

export const KNOWN_PACKET_IDS = new Set([
  PKT_CLIENT_CONNECT, PKT_DISCONNECT, PKT_KICK, PKT_JOIN_GAME,
  PKT_LEVEL_UPDATE, PKT_ENTER_GAME_DEPRECATED, PKT_PLAYER_JOINED,
  PKT_PLAYER_LEFT, PKT_PLAYER_CHANGED_LEVEL,
  PKT_NET_TIME_PING, PKT_NET_TIME_PONG, PKT_GAME_MSG, PKT_ENTER_GAME,
]);

// ─── GameMsgId（GameMsg 子消息）────────────────────────────────
// GameMsg 头部: [msg_id:u8][level_seq:u8][source_player:u8][payload]
export const GM_NET_RPC = 2;
export const GM_PLAYER_STATE = 3;
export const GM_CRITTERS = 4;
export const GM_AFFINITY = 7;
export const GM_LEVEL_DATA_ELECT = 8;
export const GM_LEVEL_DATA_REVOKE = 9;
export const GM_LEVEL_DATA_REVOKE_ACK = 10;
export const GM_LEVEL_DATA = 11;
export const GM_LEVEL_DATA_HEARTBEAT = 12;
export const GM_SNAPSHOT_ACK = 14;
export const GM_MUSIC_SYNC = 15;
export const GM_METRICS = 16;
export const GM_ELECTION_NOMINEE = 17;
export const GM_AUDIENCE_HINT = 19;
export const GM_AUDIENCE_SOCIAL_BROADCAST = 20;
export const GM_AUDIENCE_CONSENSUS_VOTE = 21;
export const GM_AUDIENCE_CHAT = 22;
export const GM_UNKNOWN1 = 23;
export const GM_AUDIENCE_SPOTLIGHT_REQ = 24;

export const KNOWN_GAME_MSG_IDS = new Set([
  GM_NET_RPC, GM_PLAYER_STATE, GM_CRITTERS, GM_AFFINITY,
  GM_LEVEL_DATA_ELECT, GM_LEVEL_DATA_REVOKE, GM_LEVEL_DATA_REVOKE_ACK,
  GM_LEVEL_DATA, GM_LEVEL_DATA_HEARTBEAT, GM_SNAPSHOT_ACK, GM_MUSIC_SYNC,
  GM_METRICS, GM_ELECTION_NOMINEE, GM_AUDIENCE_HINT,
  GM_AUDIENCE_SOCIAL_BROADCAST, GM_AUDIENCE_CONSENSUS_VOTE,
  GM_AUDIENCE_CHAT, GM_UNKNOWN1, GM_AUDIENCE_SPOTLIGHT_REQ,
]);

// 需要服务端特殊处理，其余一律原样转发同关卡玩家
export const GM_SPECIAL = new Set([
  GM_PLAYER_STATE, GM_LEVEL_DATA, GM_LEVEL_DATA_REVOKE,
  GM_LEVEL_DATA_HEARTBEAT, GM_SNAPSHOT_ACK,
]);

// NetRpc 里玩家 UUID 需要被服务端改写的魔数前缀
export const NETRPC_UUID_MAGIC = Buffer.from([0xB3, 0xB9, 0xD9, 0x20, 0x00, 0x24, 0x00]);

// ─── 关卡 ID 规范化 ─────────────────────────────────────────────
// 关卡 ID = FNV-1a-32(地图代号)，见 config/level_pickups_config/MAPID.txt。
// 客户端在不同报文里上报关卡 ID 时，字节序 / 字段位置可能不一致（join 与
// level_update 走的是两条不同的序列化路径）。只要两端算出来的 key 不一样，
// 「同图玩家」就会被判成不同关卡 —— 表现就是开启关卡同步后互相看不见。
// 所以这里把任何形态的关卡 ID 都归一化到同一个权威值。
export const KNOWN_LEVEL_IDS = new Map([
  [1649439303, 'Dawn 晨岛'],
  [1649439403, 'Dawn(old)'],
  [3526133726, 'CandleSpace 遇境'],
  [2825107789, 'SkyHub2 遇境'],
  [748712866, 'DawnCave 试炼洞窟'],
  [2394719185, 'DayHubCave 八人门'],
  [2018906977, 'HubReveal 遇境枢纽'],
  [3265870096, 'Prairie(old) 云野'],
  [927037567, 'Prairie 云野'],
  [2889552629, 'Forest(old) 雨林'],
  [164626931, 'Rain 雨林'],
  [1007551460, 'Valley(old) 霞谷'],
  [1638008359, 'Sunset 霞谷'],
  [2518601, 'NightArchive'],
  [72617792, 'WorldEmpty'],
  [128844448, 'RainEnd'],
  [170656205, 'DuskOasis'],
  [214074919, 'Night_InfiniteDesert'],
  [261807733, 'Credits'],
  [263580627, 'SunsetColosseum'],
  [295816905, 'TGCOffice'],
  [312004957, 'Prairie_NestAndKeeper'],
  [507487826, 'SunsetEnd2'],
  [567986524, 'OrbitEnd'],
  [571720490, 'SunsetRace'],
  [649101397, 'Sunset_YetiPark'],
  [817373972, 'DuskStart'],
  [864432821, 'DuskGraveyard'],
  [1147491976, 'Dusk'],
  [1190972738, 'DayEnd'],
  [1241316521, 'Dawn_TrialsFire'],
  [1597085778, 'DuskMid'],
  [1705189686, 'Storm'],
  [1759178769, 'SunsetVillage'],
  [1844499196, 'Sunset_FlyRace'],
  [1887730855, 'Dawn_TrialsEarth'],
  [2050064391, 'Dawn_TrialsAir'],
  [2060214456, 'NightDesert'],
  [2081768701, 'Skyway'],
  [2159642775, 'RainMid'],
  [2179549040, 'Sunset_Citadel'],
  [2199862534, 'Nintendo_CandleSpace'],
  [2251284635, 'CandleSpaceEnd'],
  [2267185542, 'NightEnd'],
  [2307461961, 'Night2'],
  [2350532176, 'Prairie_Village'],
  [2358907137, 'Night'],
  [2360310676, 'SunsetEnd'],
  [2477345666, 'Prairie_ButterflyFields'],
  [2650921869, 'Dusk_CrabField'],
  [2720691892, 'RainShelter'],
  [2824771136, 'NightDesert_Planets'],
  [2839585646, 'RainForest'],
  [3051518728, 'NightDesert_Beach'],
  [3057325709, 'Prairie_Island'],
  [3082753918, 'Event_DaysOfMischief'],
  [3106577860, 'TitleWater'],
  [3110721718, 'StormStart'],
  [3244931597, 'Prairie_Cave'],
  [3258961934, 'Night_JarCave'],
  [3317260872, 'Rain_BaseCamp'],
  [3437135515, 'OrbitMid'],
  [3479786579, 'StormEnd'],
  [3884142720, 'Dawn_TrialsWater'],
  [3980609111, 'NightEntrance'],
  [4133595729, 'Rain_Cave'],
  [4158956653, 'DuskEnd'],
  [1230250653, 'Day'],
]);

export function byteswap32(v) {
  const x = v >>> 0;
  return (((x & 0xff) << 24) | ((x & 0xff00) << 8) | ((x >>> 8) & 0xff00) | ((x >>> 24) & 0xff)) >>> 0;
}

// 把任意来源的 u32 关卡值归一化成稳定 key。
// 返回 { value, recognized, from }，from 说明是怎么归一的，便于排障。
export function canonicalLevelValue(raw) {
  const v = raw >>> 0;
  if (KNOWN_LEVEL_IDS.has(v)) return { value: v, recognized: true, from: 'as-is' };
  const bsw = byteswap32(v);
  if (KNOWN_LEVEL_IDS.has(bsw)) return { value: bsw, recognized: true, from: 'byteswap' };
  // 未知关卡（墓土/禁阁/暴风眼等没进表）：仍然把互为字节序的两个值收敛成同一个，
  // 否则同一张图只要两条路径字节序不同就永远对不上。
  return { value: Math.min(v, bsw), recognized: false, from: 'min(le,be)' };
}

// 从一段缓冲区里探测关卡 ID。
// 候选偏移 / 宽度的组合都试一遍，命中已知关卡表就用它 —— 这样即便字段偏移
// 与预期差一格、或者值是 8 字节对齐的，也能正确识别。
export function probeLevelId(buf, offsets = [], widths = [4, 8]) {
  const tryAt = off => {
    if (off < 0 || off + 4 > buf.length) return null;
    const le = buf.readUInt32LE(off);
    const r1 = canonicalLevelValue(le);
    if (r1.recognized) return { ...r1, offset: off, width: 4 };
    if (widths.includes(8) && off + 8 <= buf.length) {
      const le2 = buf.readUInt32LE(off + 4);
      const r2 = canonicalLevelValue(le2);
      if (r2.recognized) return { ...r2, offset: off + 4, width: 8 };
    }
    return null;
  };
  const seen = new Set();
  for (const off of offsets) {
    if (seen.has(off)) continue;
    seen.add(off);
    const hit = tryAt(off);
    if (hit) return hit;
  }
  return null;
}

// ─── 顶层包解析（带连接魔数的稳定处理）────────────────────────────
// 客户端会在包前加 4 字节 connection magic。旧实现只看「data[0] 是不是合法
// packet_id」，一旦 magic 的首字节恰好落在合法 id 范围内（0x00~0x08/0x0C~0x0E/0x11），
// 整个包就会按错位解析 —— 这条连接此后所有包全废，表现为随机「看不到别人」。
// 现在第一次识别后就把长度记在连接上，之后稳定剥离。
function alignmentScore(data, off) {
  if (data.length < off + 2) return -1;
  const id = data[off];
  if (!KNOWN_PACKET_IDS.has(id)) return -1;
  const bodyLen = data.length - off - 2;
  if (id === PKT_JOIN_GAME && bodyLen < 20) return 0;
  if (id === PKT_LEVEL_UPDATE && bodyLen < 18) return 0;
  return 1;
}

export function readPacketWithMagic(data, entry = null) {
  if (!data || data.length < 2) return null;

  // 已知长度 → 直接用
  if (entry && (entry.magicLen === 0 || entry.magicLen === 4)) {
    const off = entry.magicLen;
    if (off + 2 <= data.length && KNOWN_PACKET_IDS.has(data[off])) {
      if (off > 0) entry.magic = Buffer.from(data.subarray(0, 4));
      return parseClientPacket(data.subarray(off));
    }
    // 本包没带 magic（部分包可能不带）→ 按无前缀再试一次
    if (KNOWN_PACKET_IDS.has(data[0])) {
      return parseClientPacket(data);
    }
  }

  const s0 = alignmentScore(data, 0);
  const s4 = alignmentScore(data, 4);
  if (s0 >= 1 && s0 >= s4) {
    if (entry) { entry.magic = Buffer.from(data.subarray(0, Math.min(4, data.length))); entry.magicLen = 0; }
    return parseClientPacket(data);
  }
  if (s4 >= 1) {
    if (entry) { entry.magic = Buffer.from(data.subarray(0, 4)); entry.magicLen = 4; }
    return parseClientPacket(data.subarray(4));
  }
  if (s0 === 0) return parseClientPacket(data);
  return null;
}

// ─── 编解码 ─────────────────────────────────────────────────────
// ★★ 2026-10-01 用「len3 / seq2」A/B 对照 + 真机 logcat 定论（别再翻案）：
//
//   客户端 -> 服务端：[packet_id:u8][seq:u8][payload]          ← 2 字节，seq 每包 +1
//   服务端 -> 客户端：[packet_id:u8][payload_len:u16 LE][payload] ← 3 字节
//
//   两个方向的包头**不一样**。曾经以为"对称"而把下行也改成 2 字节 → 客户端从
//   第 2 个字节开始读 payload，整体错位 1 字节，报
//     E/CRASH ASSERT! BinaryStream.cpp(181) Read past end of bit stream
//             next: buffer+3 size: 30
//   改回 3 字节后这个崩溃立刻消失。
//   注意：下行既然带长度，PeerEntry.send() 就不能再往 data[1] 盖 seq
//   （会污染长度低字节）—— 见 server.js 里的 stampTxSeq。
let PACKET_HEADER_MODE = 'len3'; // 'len3'（正确）| 'seq2'（错的，仅留作对照）

export function setPacketHeaderMode(mode) {
  PACKET_HEADER_MODE = String(mode || 'len3').toLowerCase();
}

export function getPacketHeaderMode() {
  return PACKET_HEADER_MODE;
}

export function buildPacket(packetId, payload, seq = 0) {
  if (PACKET_HEADER_MODE === 'len3') {
    // 正确：[packet_id][payload_len:u16 LE][payload]
    if (payload.length > 0xFFFF) payload = payload.subarray(0, 0xFFFF);
    const head = Buffer.allocUnsafe(3);
    head[0] = packetId & 0xFF;
    head.writeUInt16LE(payload.length, 1);
    return Buffer.concat([head, payload]);
  }
  // 错：[packet_id][seq][payload]；seq 由 PeerEntry.send 按连接逐包填。
  const head = Buffer.allocUnsafe(2);
  head[0] = packetId & 0xFF;
  head[1] = seq & 0xFF;
  return Buffer.concat([head, payload]);
}

// 仅供测试/排障：按当前下行包头切出 payload
export function splitServerPacket(data) {
  if (!data || data.length < 2) return null;
  if (PACKET_HEADER_MODE === 'len3') {
    if (data.length < 3) return null;
    return { id: data[0], len: data.readUInt16LE(1), payload: data.subarray(3) };
  }
  return { id: data[0], seq: data[1], payload: data.subarray(2) };
}


export function parseClientPacket(data) {
  // 客户端 -> 服务端：[packet_id][seq][payload]
  if (!data || data.length < 2) return null;
  const packetId = data[0];
  if (!KNOWN_PACKET_IDS.has(packetId)) return null;
  return { id: packetId, seq: data[1], body: data.subarray(2) };
}

export function stripConnectionMagic(data, entry = null) {
  // 去掉客户端包头前面可能存在的 4 字节 connection magic
  if (!data || data.length === 0) return data;
  if (KNOWN_PACKET_IDS.has(data[0])) {
    if (entry && data.length >= 4) entry.connMagic = Buffer.from(data.subarray(0, 4));
    return data;
  }
  if (data.length >= 5 && KNOWN_PACKET_IDS.has(data[4])) {
    if (entry) entry.connMagic = Buffer.from(data.subarray(0, 4));
    return data.subarray(4);
  }
  return data;
}

export function buildGameMsg(msgId, levelSeq, sourcePlayer, payload) {
  const head = Buffer.from([msgId & 0xFF, levelSeq & 0xFF, sourcePlayer & 0xFF]);
  return Buffer.concat([head, payload]);
}

export function parseGameMsg(payload) {
  if (!payload || payload.length < 3) return null;
  return { msgId: payload[0], levelSeq: payload[1], source: payload[2], body: payload.subarray(3) };
}

export function uuidString(raw) {
  if (!raw || raw.length !== 16) return '?';
  const h = raw.toString('hex');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}
