// snapshot.js — PlayerState / NetLevelData 快照差分编解码（RLE + XOR + 8bit 校验）
// 移植自 udp_relay_enet.py（已验证的 0.15.5 格式）
//
//   帧头 3 字节: [save_seq:u8][base_seq:u8][checksum:u8]
//   Raw 帧   : save_seq != 0, base_seq == 0, 数据 = 完整状态
//   Key 帧   : base_seq != 0 且 save_seq != 0, 数据 = RLE(XOR(新状态, 基准帧))
//   Delta 帧 : base_seq != 0 且 save_seq == 0, 数据 = RLE(XOR(新状态, 基准帧))

export const MAX_STATE_SIZE = 7500;
export const MAX_WINDOW = 16;
export const KEYFRAME_INTERVAL = 10;
export const MAX_MISSED_ACKS = 3;

export function snapshotChecksum(data) {
  // 8bit 滚动校验（与服务端/客户端一致）
  let h = 0xC5;
  for (let i = 0; i < data.length; i++) {
    h = (0x93 * ((h ^ data[i]) & 0xFF)) & 0xFF;
  }
  return h;
}

export function rleEncode(data) {
  // 零游程编码：一段 n 个 0 -> [0x00, n]；非 0 字节原样拷贝
  const out = [];
  let i = 0;
  const n = data.length;
  while (i < n) {
    if (data[i] === 0) {
      let c = 0;
      while (i < n && c < 255 && data[i] === 0) { c++; i++; }
      out.push(0x00, c);
    } else {
      out.push(data[i]);
      i++;
    }
  }
  return Buffer.from(out);
}

export function rleDecode(data) {
  // 零游程解码，数据被截断时返回 null
  const out = [];
  let i = 0;
  const n = data.length;
  while (i < n) {
    if (data[i] === 0x00) {
      if (i + 1 >= n) return null;
      const count = data[i + 1];
      for (let j = 0; j < count; j++) out.push(0);
      i += 2;
    } else {
      out.push(data[i]);
      i++;
    }
  }
  return Buffer.from(out);
}

export function xorDiff(newData, base, newLen) {
  // new 与 base 做 XOR，超出 base 长度的部分原样保留
  const buf = Buffer.allocUnsafe(newLen);
  const bl = Math.min(base.length, newLen);
  for (let i = 0; i < bl; i++) buf[i] = newData[i] ^ base[i];
  if (newLen > base.length) {
    newData.copy(buf, base.length, base.length, newLen);
  }
  return buf;
}

// ─── 客户端 -> 服务端 解码器（滑动窗口 + 差分重建）─────────────
export class SnapshotReader {
  constructor() {
    this.currentSequence = 0;
    this.window = []; // [{seq, data}] 越靠前越新
    this.latestFullState = Buffer.alloc(0);
    this.waitingForKeyframe = false;
  }

  applyDelta(delta) {
    if (!delta || delta.length < 3) return null;
    const saveSeq = delta[0];
    const baseSeq = delta[1];
    const expected = delta[2];
    const data = delta.subarray(3);

    // Raw 帧：save_seq != 0 且 base_seq == 0
    if (baseSeq === 0 && saveSeq !== 0) {
      if (snapshotChecksum(data) !== expected) return null;
      const state = Buffer.from(data);
      this.window = [];
      this._addToWindow(saveSeq, state);
      this.currentSequence = saveSeq;
      this.latestFullState = state;
      this.waitingForKeyframe = false;
      return state;
    }

    if (baseSeq !== 0) {
      if (this.waitingForKeyframe && saveSeq === 0) return null;
      if (snapshotChecksum(data) !== expected) return null;
      const xorBuf = rleDecode(data);
      if (xorBuf === null || xorBuf.length > MAX_STATE_SIZE) return null;

      let base = Buffer.alloc(0);
      for (const w of this.window) {
        if (w.seq === baseSeq) { base = w.data; break; }
      }

      const newState = Buffer.allocUnsafe(xorBuf.length);
      const bl = Math.min(base.length, xorBuf.length);
      for (let i = 0; i < bl; i++) newState[i] = base[i] ^ xorBuf[i];
      if (xorBuf.length > base.length) {
        xorBuf.copy(newState, base.length, base.length);
      }

      if (saveSeq !== 0) {
        // Key 帧：存窗口
        this._addToWindow(saveSeq, newState);
        this.currentSequence = saveSeq;
        this.waitingForKeyframe = false;
      }
      this.latestFullState = newState;
      return newState;
    }

    return null;
  }

  forceWaitForKeyframe() {
    this.window = [];
    this.latestFullState = Buffer.alloc(0);
    this.currentSequence = 0;
    this.waitingForKeyframe = true;
  }

  _addToWindow(sequence, data) {
    this.window = this.window.filter(w => w.seq !== sequence);
    this.window.push({ seq: sequence, data });
    while (this.window.length > MAX_WINDOW) this.window.shift();
  }
}

// ─── 服务端 -> 客户端 编码器（每个目标玩家独立窗口）─────────────
export class SnapshotWriter {
  constructor(trackAcks = true) {
    this.nextSaveSeq = 1;
    this.window = []; // [{seq, data}] 越靠前越新
    this.deltaCount = 0;
    this.resync = false;
    this.pendingAckSeq = null;
    this.missedAcks = 0;
    this.forceNextKeyframe = false;
    this.trackAcks = trackAcks;
    this.lastSavedSeq = null;
  }

  generateDelta(newState) {
    const newLen = Math.min(newState.length, MAX_STATE_SIZE);
    if (this.resync) return this._emitRaw(newState, newLen);
    if (this.window.length === 0) return this._emitRaw(newState, newLen);
    if (this.forceNextKeyframe) {
      this.forceNextKeyframe = false;
      return this._emitKeyframe(newState, newLen);
    }
    if (this.deltaCount >= KEYFRAME_INTERVAL) return this._emitKeyframe(newState, newLen);
    return this._emitDelta(newState, newLen);
  }

  pendingAck() { return this.pendingAckSeq; }

  ack(seq) {
    const matched = this.pendingAckSeq === seq;
    if (matched) {
      this.pendingAckSeq = null;
      this.missedAcks = 0;
      this.window = this.window.filter(w => ((w.seq - seq) & 0xFF) <= 127);
      if (this.resync) {
        this.resync = false;
        this.forceNextKeyframe = true;
      }
    }
    return matched;
  }

  forceKeyframe() {
    this.window = [];
    this.deltaCount = 0;
    this.resync = false;
    this.pendingAckSeq = null;
    this.missedAcks = 0;
    this.forceNextKeyframe = false;
    this.lastSavedSeq = null;
  }

  _nextSeq() {
    const sv = this.nextSaveSeq;
    const nxt = (sv + 1) & 0xFF;
    this.nextSaveSeq = nxt !== 0 ? nxt : 1;
    return sv;
  }

  _emitRaw(newState, newLen) {
    const sv = this._nextSeq();
    this.deltaCount = 0;
    const raw = Buffer.from(newState.subarray(0, newLen));
    const chk = snapshotChecksum(raw);
    this._addToWindow(sv, raw);
    this._markAckRequired(sv);
    return Buffer.concat([Buffer.from([sv, 0, chk]), raw]);
  }

  _emitKeyframe(newState, newLen) {
    const base = this._baseSnapshot();
    if (!base) return this._emitRaw(newState, newLen);
    const [baseSeq, baseData] = base;
    const sv = this._nextSeq();
    this.deltaCount = 0;
    const xorBuf = xorDiff(newState, baseData, newLen);
    const rleData = rleEncode(xorBuf);
    if (rleData.length > xorBuf.length) {
      this.resync = true;
      this.window = [];
      return this._emitRaw(newState, newLen);
    }
    const chk = snapshotChecksum(rleData);
    this._addToWindow(sv, Buffer.from(newState.subarray(0, newLen)));
    this._markAckRequired(sv);
    return Buffer.concat([Buffer.from([sv, baseSeq, chk]), rleData]);
  }

  _emitDelta(newState, newLen) {
    const base = this._baseSnapshot();
    if (!base) return this._emitRaw(newState, newLen);
    const [baseSeq, baseData] = base;
    const xorBuf = xorDiff(newState, baseData, newLen);
    const rleData = rleEncode(xorBuf);
    if (rleData.length > xorBuf.length) {
      this.resync = true;
      this.window = [];
      return this._emitRaw(newState, newLen);
    }
    this.deltaCount += 1;
    const chk = snapshotChecksum(rleData);
    return Buffer.concat([Buffer.from([0, baseSeq, chk]), rleData]);
  }

  _markAckRequired(seq) {
    if (!this.trackAcks) return;
    if (this.pendingAckSeq !== null) {
      this.missedAcks = Math.min(this.missedAcks + 1, 255);
      if (this.missedAcks >= MAX_MISSED_ACKS) this.resync = true;
    }
    this.pendingAckSeq = seq;
  }

  _baseSnapshot() {
    if (this.lastSavedSeq !== null) {
      for (const w of this.window) {
        if (w.seq === this.lastSavedSeq) return [w.seq, w.data];
      }
    }
    if (this.window.length > 0) return [this.window[0].seq, this.window[0].data];
    return null;
  }

  _addToWindow(sequence, data) {
    this.window = this.window.filter(w => w.seq !== sequence);
    this.window.unshift({ seq: sequence, data });
    if (this.window.length > MAX_WINDOW) this.window.pop();
    this.lastSavedSeq = sequence;
  }
}
