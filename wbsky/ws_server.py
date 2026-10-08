#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wbsky Player WebSocket 服务器（真正的聊天中继）

背景（2026-10-03 查证）：
  * nginx 访问日志里，游戏客户端确实会发起 `GET /account/ws`（带 Basic 认证，
    $remote_user 就是玩家 uuid），说明 WS 是客户端真实使用的通道。
  * 当时站点配置里没有 /account/ws 的 location，请求被丢给 Flask:2999，
    客户端拿到 400/非 101，重试 4 次后彻底放弃 —— 所以聊天谁都收不到。
  * Flask 那边的 /account/chat/send 会把聊天消息写进 logs/ws_outbox.jsonl，
    本进程 tail 这个文件并广播给所有已连接的客户端。

本进程只做四件事：
  1. 完成 /account/ws 的 WebSocket 握手（任何路径都接受）；
  2. 把收到的每一帧原样落盘到 logs/ws_frames.log（用来确认真实协议）；
  3. 把 outbox 里 Flask 写进来的聊天帧广播出去；
  4. 把客户端通过 WS 发来的聊天帧转发给其他客户端。

运行期开关（改完不用重启，每次事件都重新读）：
  config/ws_options.json
    {
      "send_welcome": true,      # 连上后是否先推一条 {"type":"connected"}
      "relay_incoming": true,    # 是否把客户端发来的帧转发给其他客户端
      "broadcast_outbox": true,  # 是否广播 logs/ws_outbox.jsonl 里的帧
      "log_frames": true         # 是否把帧写进 logs/ws_frames.log
    }

用法: python3 ws_server.py [port]   默认 2500
"""
import base64
import hashlib
import json
import logging
import os
import socket
import struct
import sys
import threading
import time
import uuid

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s',
                    datefmt='%H:%M:%S')
log = logging.getLogger('ws')

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 2500
HOST = '0.0.0.0'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, 'logs')
OUTBOX = os.path.join(LOG_DIR, 'ws_outbox.jsonl')
OPTIONS = os.path.join(BASE_DIR, 'config', 'ws_options.json')

WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

# ---------------------------------------------------------------- 连接管理
clients = {}          # conn -> {'user':..., 'addr':..., 'path':...}
clients_lock = threading.Lock()


def load_options():
    opts = {
        'send_welcome': True,
        'relay_incoming': True,
        'broadcast_outbox': True,
        'log_frames': True,
    }
    try:
        with open(OPTIONS, encoding='utf-8') as f:
            opts.update(json.load(f) or {})
    except Exception:
        pass
    return opts


def append_log(path, line):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def printable_runs(payload, minlen=4, limit=8):
    runs, cur = [], ''
    for b in payload:
        if 0x20 <= b <= 0x7E:
            cur += chr(b)
        else:
            if len(cur) >= minlen:
                runs.append(cur)
            cur = ''
    if len(cur) >= minlen:
        runs.append(cur)
    return runs[:limit]


# ---------------------------------------------------------------- 帧读写
def read_exact(conn, n):
    data = b''
    while len(data) < n:
        chunk = conn.recv(n - len(data))
        if not chunk:
            raise ConnectionError('closed')
        data += chunk
    return data


def read_frame_from(conn, buf):
    """从 buf 里解析一帧；buf 不足时继续从 socket 读。返回 (opcode, payload, rest)"""
    while len(buf) < 2:
        chunk = conn.recv(4096)
        if not chunk:
            raise ConnectionError('closed')
        buf += chunk
    b0, b1 = buf[0], buf[1]
    opcode = b0 & 0x0F
    masked = (b1 >> 7) & 1
    length = b1 & 0x7F
    idx = 2
    if length == 126:
        while len(buf) < idx + 2:
            buf += conn.recv(4096)
        length = struct.unpack('>H', buf[idx:idx + 2])[0]
        idx += 2
    elif length == 127:
        while len(buf) < idx + 8:
            buf += conn.recv(4096)
        length = struct.unpack('>Q', buf[idx:idx + 8])[0]
        idx += 8
    if masked:
        while len(buf) < idx + 4:
            buf += conn.recv(4096)
        mask = buf[idx:idx + 4]
        idx += 4
    else:
        mask = None
    while len(buf) < idx + length:
        chunk = conn.recv(4096)
        if not chunk:
            raise ConnectionError('closed')
        buf += chunk
    payload = buf[idx:idx + length]
    rest = buf[idx + length:]
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload, rest


def ws_handshake(headers):
    key = ''
    for line in headers:
        if line.lower().startswith('sec-websocket-key:'):
            key = line.split(':', 1)[1].strip()
            break
    if not key:
        raise ValueError('no Sec-WebSocket-Key')
    accept = base64.b64encode(
        hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    return ("HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Accept: " + accept + "\r\n"
            "\r\n")


def write_frame(conn, opcode, payload=b''):
    header = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header += bytes([n])
    elif n < 65536:
        header += bytes([126]) + struct.pack('>H', n)
    else:
        header += bytes([127]) + struct.pack('>Q', n)
    conn.sendall(header + payload)


def send_json(conn, obj):
    write_frame(conn, 0x1, json.dumps(obj, ensure_ascii=False).encode('utf-8'))


# ---------------------------------------------------------------- 广播
def broadcast(obj, exclude=None):
    """把 obj 作为一条文本帧推给所有客户端（默认包括发送者自己）。
    参考实现就是「所有人包括自己都收」，客户端本地再按 sender_id 决定渲染样式。"""
    payload = json.dumps(obj, ensure_ascii=False).encode('utf-8')
    dead = []
    with clients_lock:
        conns = list(clients.items())
    for conn, info in conns:
        if conn is exclude:
            continue
        try:
            write_frame(conn, 0x1, payload)
        except Exception as exc:
            log.info(f'[WS] 广播失败 {info.get("user")}: {exc}')
            dead.append(conn)
    for conn in dead:
        with clients_lock:
            clients.pop(conn, None)
        try:
            conn.close()
        except Exception:
            pass


def parse_identity(raw_headers, path):
    """从 Basic 认证 / query string 里取玩家 id。"""
    user, session = '', ''
    for line in raw_headers:
        low = line.lower()
        if low.startswith('authorization:') and 'basic' in low:
            try:
                cred = line.split(' ', 2)[2].strip()
                decoded = base64.b64decode(cred).decode('utf-8', 'ignore')
                if ':' in decoded:
                    user, session = decoded.split(':', 1)
            except Exception:
                pass
    if not user and '?' in path:
        try:
            from urllib.parse import parse_qs, unquote
            qs = parse_qs(path.split('?', 1)[1])
            user = unquote(qs.get('user', [''])[0])
            session = unquote(qs.get('session', [''])[0])
        except Exception:
            pass
    return user, session


# ---------------------------------------------------------------- outbox 广播
def outbox_loop():
    """tail logs/ws_outbox.jsonl，把 Flask 写进来的聊天帧广播出去。"""
    pos = 0
    if os.path.exists(OUTBOX):
        pos = os.path.getsize(OUTBOX)
    log.info(f'[WS] outbox 监听 {OUTBOX}（从 offset {pos} 开始）')
    while True:
        try:
            if not os.path.exists(OUTBOX):
                time.sleep(0.3)
                continue
            size = os.path.getsize(OUTBOX)
            if size < pos:            # 文件被截断/轮转
                pos = 0
            if size == pos:
                time.sleep(0.2)
                continue
            with open(OUTBOX, 'r', encoding='utf-8') as f:
                f.seek(pos)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if load_options().get('broadcast_outbox', True):
                        log.info(f'[WS] outbox 广播: {line[:160]}')
                        append_log(os.path.join(LOG_DIR, 'ws_frames.log'),
                                   '%s OUTBOX %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), line[:800]))
                        broadcast(obj)
                pos = f.tell()
        except Exception as exc:
            log.warning(f'[WS] outbox 异常: {exc}')
            time.sleep(1)


# ---------------------------------------------------------------- 单连接
def handle_client(conn, addr):
    try:
        conn.settimeout(300)
        data = b''
        while b'\r\n\r\n' not in data:
            chunk = conn.recv(4096)
            if not chunk:
                raise ConnectionError('closed before handshake')
            data += chunk
            if len(data) > 16384:
                raise ConnectionError('handshake too large')

        head, _, leftover = data.partition(b'\r\n\r\n')
        text = head.decode('latin-1', 'ignore')
        lines = text.split('\r\n')
        request_line = lines[0] if lines else ''
        path = request_line.split(' ')[1] if ' ' in request_line else '/'
        user, session = parse_identity(lines[1:], path)

        conn.sendall(ws_handshake(lines[1:]).encode('latin-1'))
        with clients_lock:
            clients[conn] = {'user': user, 'session': session,
                             'addr': f'{addr[0]}:{addr[1]}', 'path': path,
                             'connected_at': time.time()}
        log.info(f'[WS] 连接 {addr[0]}:{addr[1]} path={path} user={user or "-"} '
                 f'(当前 {len(clients)})')
        append_log(os.path.join(LOG_DIR, 'ws_frames.log'),
                   '%s CONNECT %s path=%s user=%s ua=%s' % (
                       time.strftime('%Y-%m-%d %H:%M:%S'), addr[0], path, user or '-',
                       next((l[12:] for l in lines if l.lower().startswith('user-agent:')), '-')[:120]))

        opts = load_options()
        if opts.get('send_welcome', True):
            try:
                send_json(conn, {'type': 'connected', 'user': user,
                                 'status': 'success',
                                 'timestamp': int(time.time() * 1000)})
            except Exception:
                pass

        buf = leftover          # ★ 关键：握手同一段里可能已经带了第一帧，不能丢
        while True:
            try:
                opcode, payload, buf = read_frame_from(conn, buf)
            except socket.timeout:
                try:
                    write_frame(conn, 0x9, b'')     # 空闲 ping 保活
                except Exception:
                    break
                continue

            if opcode == 0x8:
                log.info(f'[WS] 客户端关闭 {addr[0]}')
                break
            if opcode == 0x9:
                write_frame(conn, 0xA, payload)
                continue
            if opcode == 0xA:
                continue

            opts = load_options()
            if opts.get('log_frames', True):
                append_log(os.path.join(LOG_DIR, 'ws_frames.log'),
                           '%s IN opcode=%d len=%d user=%s text=%r hex=%s' % (
                               time.strftime('%Y-%m-%d %H:%M:%S'), opcode, len(payload),
                               user or '-', printable_runs(payload), payload[:400].hex()))
            log.info(f'[WS] 收到 opcode={opcode} len={len(payload)}: '
                     f'{payload[:200]!r}')

            if opcode != 0x1:
                continue

            try:
                obj = json.loads(payload.decode('utf-8', 'ignore'))
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue

            mtype = obj.get('type')
            if mtype == 'ping':
                send_json(conn, {'type': 'pong',
                                 'timestamp': int(time.time() * 1000)})
                continue

            msg = obj.get('msg')
            if msg and opts.get('relay_incoming', True):
                frame = {
                    'type': 'chat',
                    'sender_id': obj.get('sender_id') or user or 'unknown',
                    'result': 'success',
                    'msg_id': obj.get('msg_id') or str(uuid.uuid4()),
                    'msg': msg,
                    'ch': obj.get('ch', 'global'),
                    'timestamp': obj.get('timestamp') or int(time.time() * 1000),
                }
                log.info(f'[WS] 转发聊天: {frame["msg"][:80]!r} ch={frame["ch"]}')
                broadcast(frame)
                try:
                    send_json(conn, {'type': 'ack', 'msg_id': frame['msg_id'],
                                     'result': 'success', 'status': 'sent',
                                     'timestamp': int(time.time() * 1000)})
                except Exception:
                    pass
    except Exception as exc:
        log.debug(f'[WS] {addr[0]} 断开: {exc}')
    finally:
        with clients_lock:
            clients.pop(conn, None)
        try:
            conn.close()
        except Exception:
            pass
        append_log(os.path.join(LOG_DIR, 'ws_frames.log'),
                   '%s DISCONNECT %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), addr[0]))


def main():
    threading.Thread(target=outbox_loop, daemon=True).start()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(128)
    log.info(f'[WS] 聊天 WebSocket 监听 {HOST}:{PORT} /account/ws')
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()


if __name__ == '__main__':
    main()
