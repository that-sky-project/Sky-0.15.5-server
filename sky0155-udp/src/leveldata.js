// leveldata.js — 关卡数据（NetLevelData / NetLevelDataElect 的载荷）
//
// ★★ 2026-10-01 真机校准：头部是 **23 字节**，不是 22。
//   客户端 BinaryStream 依次读：
//     u8 @0, u32 @1, u16 @5, u8 @7, u16 @8, u16 @10, u32 @12, u8 @16, u16 @17, u16 @19, u16 @21
//   最后那个 u16（length）在 offset 21..23 → 整个头部 23 字节。
//   旧实现写成 22 字节、length 放在 20（还和 unk6 重叠 1 字节）→ 客户端读到 offset 21
//   时只剩 1 字节，直接断言崩溃：
//     E/CRASH ASSERT! BinaryStream.cpp(181) Read past end of bit stream
//             prevBitIndexNotZero 0 currBitIndex: 0 next: buffer+21 bytes read 2 size: 22
//   （size:22 就是我们那个 22 字节头，"next 21 还想读 2 字节"就是 length 字段。）
//   只有"玩家不是本关卡权威"时才走 GM_LEVEL_DATA 流，所以单人（自己是 host）不崩，
//   同图有第二个人（host 变成别人）时必崩 —— 现象与"地图有人就闪退"完全吻合。
//
// 布局:
//   0     elected_player:u8
//   1     level_id:u32
//   5     unk1:u16
//   7     has_initial_data:u8
//   8     unk2:u16
//   10    unk3:u16
//   12    level_hash:u32
//   16    merge_state:u8
//   17    unk5:u16
//   19    unk6:u16
//   21    length:u16            ← data 的长度
//   23    data[length]

const LEVEL_DATA_HEADER_SIZE = 23;

export class LevelData {
  constructor(electedPlayer = 0, levelId = 0, hasInitialData = 0, data = Buffer.alloc(0), levelHash = 0, mergeState = 0) {
    this.electedPlayer = electedPlayer & 0xFF;
    this.levelId = levelId >>> 0;
    this.hasInitialData = hasInitialData & 0xFF;
    this.levelHash = levelHash >>> 0;
    this.mergeState = mergeState & 0xFF;
    this.data = Buffer.from(data);
  }

  toBytes() {
    const header = Buffer.allocUnsafe(LEVEL_DATA_HEADER_SIZE);
    header[0] = this.electedPlayer;
    header.writeUInt32LE(this.levelId, 1);
    header.writeUInt16LE(0, 5);          // unk1
    header[7] = this.hasInitialData;
    header.writeUInt16LE(0, 8);          // unk2
    header.writeUInt16LE(0, 10);         // unk3
    header.writeUInt32LE(this.levelHash, 12);
    header[16] = this.mergeState;
    header.writeUInt16LE(0, 17);         // unk5
    header.writeUInt16LE(0, 19);         // unk6
    header.writeUInt16LE(this.data.length, 21);
    return Buffer.concat([header, this.data]);
  }

  static fromBytes(buf) {
    if (!buf || buf.length < LEVEL_DATA_HEADER_SIZE) return null;
    const elected = buf[0];
    const levelId = buf.readUInt32LE(1);
    const hasInit = buf[7];
    const levelHash = buf.readUInt32LE(12);
    const mergeState = buf[16];
    const length = buf.readUInt16LE(21);
    const data = Buffer.from(buf.subarray(LEVEL_DATA_HEADER_SIZE));
    if (length !== data.length) return null;
    return new LevelData(elected, levelId, hasInit, data, levelHash, mergeState);
  }

  update(other) {
    // 保留自己的权威，其余用新数据覆盖
    if (!other || other.data.length > 0xFFFF) return false;
    const keepAuthority = this.electedPlayer;
    this.levelId = other.levelId;
    this.hasInitialData = other.hasInitialData;
    this.levelHash = other.levelHash;
    this.mergeState = other.mergeState;
    this.data = other.data;
    this.electedPlayer = keepAuthority;
    return true;
  }
}
