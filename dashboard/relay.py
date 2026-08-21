#!/usr/bin/env python3
"""TCP relay between nginx (PROXY protocol v1) and the local Paper server.

nginx's stream module wraps every forwarded connection with a PROXY protocol
v1 header containing the real client IP. Paper's raw Netty listener can't
parse that, so this relay strips the header, records the real IP (and, on a
login, the username) to a state file the dashboard reads, then transparently
pipes the rest of the connection through to Paper unchanged.
"""
import asyncio
import json
import os
import time

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 25567
UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = 8443
STATE_FILE = "/run/mc-proxy/connections.json"

connections = {}


def save_state():
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(list(connections.values()), f)
    os.replace(tmp, STATE_FILE)


def read_varint(buf: bytes, pos: int):
    result = 0
    shift = 0
    start = pos
    while True:
        if pos >= len(buf):
            return None, start
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
        if shift > 35:
            return None, start
    return result, pos


async def peek_handshake(reader: asyncio.StreamReader) -> tuple:
    """Best-effort read of the Handshake + next packet to extract a username."""
    buf = bytearray()
    packets = []
    pos = 0
    deadline = asyncio.get_event_loop().time() + 2.0

    while len(packets) < 2:
        length, start = read_varint(bytes(buf), pos)
        if length is not None and start + length <= len(buf):
            packets.append(bytes(buf[start:start + length]))
            pos = start + length
            continue
        if asyncio.get_event_loop().time() > deadline:
            break
        try:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=1.0)
        except asyncio.TimeoutError:
            break
        if not chunk:
            break
        buf.extend(chunk)

    username = None
    if len(packets) >= 2:
        login_packet = packets[1]
        pid, p = read_varint(login_packet, 0)
        if pid == 0x00 and p is not None:
            name_len, p2 = read_varint(login_packet, p)
            if name_len is not None:
                username = login_packet[p2:p2 + name_len].decode("utf-8", errors="replace")

    return username, bytes(buf)


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peer = writer.get_extra_info("peername")
    real_ip = peer[0] if peer else "unknown"

    try:
        header_line = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=2.0)
        header = header_line.decode(errors="replace").strip()
        parts = header.split()
        if header.startswith("PROXY TCP") and len(parts) >= 3:
            real_ip = parts[2]
    except Exception:
        pass

    try:
        username, buffered = await asyncio.wait_for(peek_handshake(reader), timeout=2.5)
    except Exception:
        username, buffered = None, b""

    conn_id = f"{real_ip}:{peer[1] if peer else 0}:{time.time()}"
    connections[conn_id] = {"ip": real_ip, "username": username, "connected_at": time.time()}
    save_state()

    try:
        up_reader, up_writer = await asyncio.open_connection(UPSTREAM_HOST, UPSTREAM_PORT)
    except Exception:
        connections.pop(conn_id, None)
        save_state()
        writer.close()
        return

    if buffered:
        up_writer.write(buffered)
        await up_writer.drain()

    async def pipe(r, w):
        try:
            while True:
                data = await r.read(65536)
                if not data:
                    break
                w.write(data)
                await w.drain()
        except Exception:
            pass
        finally:
            try:
                w.close()
            except Exception:
                pass

    await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))
    connections.pop(conn_id, None)
    save_state()


async def main():
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    save_state()
    server = await asyncio.start_server(handle_client, LISTEN_HOST, LISTEN_PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
