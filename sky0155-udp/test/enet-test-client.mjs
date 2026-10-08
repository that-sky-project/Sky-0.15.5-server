// enet-test-client.mjs — 用 sky-enet 模拟 0.15.5 客户端，验证新 UDP 服务器
// 用法: node enet-test-client.mjs [host] [port]
import { Client } from 'sky-enet';

const host = process.argv[2] || '127.0.0.1';
const port = Number(process.argv[3] || 8125);
const levelId = 0x30B82314; // 任意关卡 ID

console.log(`连接 ${host}:${port} (CRC32)...`);

const client = new Client({
  ip: host,
  port,
  channelLimit: 2,
  checksum: true,
});

const timeout = setTimeout(() => {
  console.log('❌ 超时：未收到 EnterGame');
  process.exit(1);
}, 8000);

client.on('error', err => console.log('ERR', err.message));
client.on('disconnect', () => console.log('disconnect'));

client.on('connect', () => {
  console.log('✓ ENet 连接成功 (CRC32 握手通过)');
  // JoinGame: [packet_id=3][seq=0][uuid16][level u32]
  const uuid = Buffer.alloc(16);
  uuid.writeUInt8(0xAB, 0); uuid.writeUInt8(0xCD, 1);
  const level = Buffer.alloc(4);
  level.writeUInt32LE(levelId, 0);
  const body = Buffer.concat([uuid, level]);
  const pkt = Buffer.concat([Buffer.from([3, 0]), body]);
  client.send(0, pkt, true);
  console.log('✓ 已发送 JoinGame, level=0x' + levelId.toString(16));
});

client.on('receive', ev => {
  const d = ev.data;
  const id = d[0];
  if (id === 17) {
    // EnterGame
    const count = d[3];
    console.log(`✓ 收到 EnterGame (packet_id=17, len=${d.length}, 玩家数=${count})`);
    console.log('协议闭环验证通过 ✓');
    clearTimeout(timeout);
    process.exit(0);
  } else if (id === 14) {
    console.log(`✓ 收到 GameMsg (packet_id=14, len=${d.length}, 头=${d.subarray(3, 8).toString('hex')})`);
  } else {
    console.log(`收到 packet_id=${id} len=${d.length}`);
  }
});

// 启动事件循环并保持运行
client.listen(2, 32).catch(err => {
  console.log('listen error', err.message);
  clearTimeout(timeout);
  process.exit(1);
});

await new Promise(r => setTimeout(r, 200));
// 发起连接（connect 内部会阻塞 listen；这里用后台 listen + native connect）
try {
  const peerId = client.native.connect(host, port, 2, 0);
  if (peerId) {
    client.serverPeer = peerId;
    client.peers.set(peerId, { address: host, port, connected: false });
    console.log('✓ 已发起 ENet 连接请求');
  } else {
    console.log('❌ connect 返回空 peerId');
    process.exit(1);
  }
} catch (e) {
  console.log('connect 调用异常:', e.message);
  process.exit(1);
}
