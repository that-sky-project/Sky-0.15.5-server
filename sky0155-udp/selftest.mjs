// selftest.mjs — 协议自检（对照 udp_relay_enet.py 的 selftest）
import { snapshotChecksum, rleEncode, rleDecode, SnapshotWriter, SnapshotReader, KEYFRAME_INTERVAL } from './src/snapshot.js';
import { LevelData } from './src/leveldata.js';
import { buildPacket, parseClientPacket, PKT_CLIENT_CONNECT, PKT_ENTER_GAME } from './src/protocol.js';

let ok = true;
const check = (name, cond) => {
  console.log(`${cond ? '  ✓' : '  ✗'} ${name}`);
  if (!cond) ok = false;
};

// 1) 快照校验和：使用参考实现里给出的测试向量
const vectors = [
  ['00a750008507001d05001d0b00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff003f', 0x50],
  ['00a7b3008506001d06001d0a00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff003f', 0xB3],
];
for (const [hexs, expect] of vectors) {
  const d = Buffer.from(hexs, 'hex');
  const got = snapshotChecksum(d.subarray(3));
  check(`snapshot_checksum = 0x${got.toString(16)}`, got === d[2] && got === expect);
}

// 2) RLE 往返
const d = Buffer.alloc(40);
check('rle 往返', rleDecode(rleEncode(d)).equals(d));

// 3) 快照 writer/reader 往返
const w = new SnapshotWriter(false);
const r = new SnapshotReader();
const s1 = Buffer.alloc(100, 0xAA);
const f1 = w.generateDelta(s1);
check('首帧(raw)往返', f1[0] === 1 && f1[1] === 0 && r.applyDelta(f1).equals(s1));
let deltasOk = true;
for (let i = 0; i < KEYFRAME_INTERVAL; i++) {
  const s = Buffer.alloc(100, (0xAA + i + 1) & 0xFF);
  const f = w.generateDelta(s);
  if (f[0] !== 0 || f[1] !== 1 || !r.applyDelta(f).equals(s)) { deltasOk = false; break; }
}
check(`delta 帧往返 (${KEYFRAME_INTERVAL} 个)`, deltasOk);
const sk = Buffer.alloc(100, 0xBB);
const fk = w.generateDelta(sk);
check('关键帧往返', fk[0] === 2 && fk[1] === 1 && r.applyDelta(fk).equals(sk));

// 4) LevelData 头长度 22 + 往返
const ld = new LevelData(0xE8, 0x30B82314, 0, Buffer.alloc(0));
check('LevelData 头部 22 字节', ld.toBytes().length === 22);
check('LevelData 往返', LevelData.fromBytes(ld.toBytes()).levelId === 0x30B82314);

// 5) 顶层包编解码
const p = buildPacket(PKT_ENTER_GAME, Buffer.from([1, 2, 3]));
// 下行包头必须是 [packet_id][seq] 两字节（客户端上行同构）。
// seq 由 PeerEntry.send 按连接逐包盖号，这里只验证未盖号时的占位值 0。
check('build_packet 输出', p.equals(Buffer.from([17, 0, 1, 2, 3])));
const parsed = parseClientPacket(Buffer.concat([Buffer.from([PKT_CLIENT_CONNECT, 7]), p.subarray(1)]));
check('parse_client_packet', parsed !== null && parsed.id === PKT_CLIENT_CONNECT);

// 6) 大 blob 聚合往返（模拟 syncFrame 的 blob）
const w2 = new SnapshotWriter(true);
const r2 = new SnapshotReader();
const blob = Buffer.concat([
  Buffer.from([1]), Buffer.from([100, 0, 0, 0]), Buffer.alloc(100, 0x11),
  Buffer.from([2]), Buffer.from([100, 0, 0, 0]), Buffer.alloc(100, 0x22),
]);
const fb = w2.generateDelta(blob);
const rb = r2.applyDelta(fb);
check('多玩家 blob 往返', rb !== null && rb.equals(blob));

console.log();
console.log('自检结果:', ok ? '全部通过 ✓' : '存在失败项 ✗');
process.exit(ok ? 0 : 1);
