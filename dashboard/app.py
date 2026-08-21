#!/usr/bin/env python3
"""Minimal Minecraft server status dashboard - stdlib only."""
import glob
import gzip
import hashlib
import hmac
import http.cookies
import json
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import time
import urllib.request
import uuid as uuid_module
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MC_HOST = "127.0.0.1"
MC_PORT = 8443
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8090
MC_DIR = "/home/minecraft"
LOG_PATH = f"{MC_DIR}/logs/latest.log"
GAMEMODE_NAMES = {0: "survival", 1: "creative", 2: "adventure", 3: "spectator"}
RUN_SH = f"{MC_DIR}/run.sh"
AUTH_CONFIG_PATH = "/etc/mc-dashboard-auth.env"
SESSION_COOKIE_NAME = "mc_dash_session"
SESSION_MAX_AGE = 30 * 24 * 3600  # 30 days
MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 300
FILL_API = "https://fill.papermc.io/v3/projects/paper"


def _write_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value != 0:
            byte |= 0x80
        out.append(byte)
        if value == 0:
            break
    return bytes(out)


def _read_varint(sock: socket.socket) -> int:
    num = 0
    for i in range(5):
        b = sock.recv(1)
        if not b:
            raise ConnectionError("socket closed while reading varint")
        byte = b[0]
        num |= (byte & 0x7F) << (7 * i)
        if not (byte & 0x80):
            break
    return num


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed early")
        buf += chunk
    return buf


def _write_string(s: str) -> bytes:
    data = s.encode("utf-8")
    return _write_varint(len(data)) + data


def query_status(host: str, port: int, timeout: float = 3.0) -> dict:
    start = time.monotonic()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        # Handshake packet (id 0x00)
        handshake = (
            _write_varint(0x00)
            + _write_varint(0)  # protocol version placeholder, not validated for status pings
            + _write_string(host)
            + struct.pack(">H", port)
            + _write_varint(1)  # next state = status
        )
        sock.sendall(_write_varint(len(handshake)) + handshake)

        # Status request packet (id 0x00, empty body)
        req = _write_varint(0x00)
        sock.sendall(_write_varint(len(req)) + req)

        # Read status response packet
        _packet_len = _read_varint(sock)
        packet_id = _read_varint(sock)
        if packet_id != 0x00:
            raise ValueError(f"unexpected packet id {packet_id}")
        json_len = _read_varint(sock)
        json_bytes = _recv_exact(sock, json_len)
        payload = json.loads(json_bytes.decode("utf-8"))

        # Ping for latency
        ping_payload = struct.pack(">q", int(time.time() * 1000))
        ping_pkt = _write_varint(0x01) + ping_payload
        sock.sendall(_write_varint(len(ping_pkt)) + ping_pkt)
        _read_varint(sock)  # pong packet length
        pong_id = _read_varint(sock)
        _recv_exact(sock, 8)  # discard pong payload
        latency_ms = round((time.monotonic() - start) * 1000)

    players = payload.get("players", {})
    description = payload.get("description", "")
    if isinstance(description, dict):
        description = description.get("text", "") or "".join(
            e.get("text", "") for e in description.get("extra", [])
        )

    return {
        "online": True,
        "motd": description,
        "version": payload.get("version", {}).get("name", "unknown"),
        "players_online": players.get("online", 0),
        "players_max": players.get("max", 0),
        "players_sample": [p.get("name") for p in players.get("sample", []) or []],
        "latency_ms": latency_ms,
    }


RELAY_STATE_FILE = "/run/mc-proxy/connections.json"


def get_player_ips() -> dict:
    """username -> real ip, from the relay's live connection state file."""
    try:
        with open(RELAY_STATE_FILE) as f:
            entries = json.load(f)
    except Exception:
        return {}
    return {e["username"]: e["ip"] for e in entries if e.get("username")}


# =============================================================================
# Auth: single admin account, credentials never held in plaintext at rest.
# /etc/mc-dashboard-auth.env (mode 600, root-only) holds a PBKDF2 hash + salt,
# generated once out-of-band — this module only ever reads and verifies it.
# Sessions are opaque random tokens kept in this process's memory (not a
# file/db), sent back as an HttpOnly+Secure+SameSite cookie. Restarting the
# service invalidates all sessions — acceptable for a single-admin tool.
# =============================================================================
_active_sessions: dict[str, float] = {}   # token -> created_at
_failed_attempts: dict[str, list] = {}    # "global" -> [timestamps of recent failures]


def _load_auth_config() -> dict:
    config = {}
    try:
        with open(AUTH_CONFIG_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                config[key] = value
    except Exception:
        pass
    return config


def verify_login(username: str, password: str) -> bool:
    config = _load_auth_config()
    expected_user = config.get("AUTH_USERNAME", "")
    salt_hex = config.get("AUTH_SALT_HEX", "")
    hash_hex = config.get("AUTH_HASH_HEX", "")
    iterations = int(config.get("AUTH_ITERATIONS", "200000") or 200000)
    if not (expected_user and salt_hex and hash_hex):
        return False
    if not hmac.compare_digest(username, expected_user):
        return False
    try:
        salt = bytes.fromhex(salt_hex)
        expected_hash = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    actual_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual_hash, expected_hash)


def is_rate_limited() -> bool:
    now = time.time()
    attempts = _failed_attempts.get("global", [])
    attempts = [t for t in attempts if now - t < LOGIN_LOCKOUT_SECONDS]
    _failed_attempts["global"] = attempts
    return len(attempts) >= MAX_LOGIN_ATTEMPTS


def record_failed_attempt():
    _failed_attempts.setdefault("global", []).append(time.time())


def create_session() -> str:
    token = secrets.token_urlsafe(32)
    _active_sessions[token] = time.time()
    return token


def is_valid_session(token: str) -> bool:
    if not token or token not in _active_sessions:
        return False
    if time.time() - _active_sessions[token] > SESSION_MAX_AGE:
        del _active_sessions[token]
        return False
    return True


def destroy_session(token: str):
    _active_sessions.pop(token, None)


class _NBTReader:
    """Minimal reader for Minecraft's gzip-compressed NBT playerdata files."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def _take(self, n: int) -> bytes:
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def read_u8(self) -> int:
        return self._take(1)[0]

    def read_string(self) -> str:
        length = struct.unpack(">H", self._take(2))[0]
        return self._take(length).decode("utf-8", errors="replace")

    def read_payload(self, tag_type: int):
        if tag_type == 1:
            return struct.unpack(">b", self._take(1))[0]
        if tag_type == 2:
            return struct.unpack(">h", self._take(2))[0]
        if tag_type == 3:
            return struct.unpack(">i", self._take(4))[0]
        if tag_type == 4:
            return struct.unpack(">q", self._take(8))[0]
        if tag_type == 5:
            return struct.unpack(">f", self._take(4))[0]
        if tag_type == 6:
            return struct.unpack(">d", self._take(8))[0]
        if tag_type == 7:
            n = struct.unpack(">i", self._take(4))[0]
            return self._take(n)
        if tag_type == 8:
            return self.read_string()
        if tag_type == 9:
            elem_type = self.read_u8()
            n = struct.unpack(">i", self._take(4))[0]
            return [self.read_payload(elem_type) for _ in range(n)]
        if tag_type == 10:
            result = {}
            while True:
                t = self.read_u8()
                if t == 0:
                    break
                name = self.read_string()
                result[name] = self.read_payload(t)
            return result
        if tag_type == 11:
            n = struct.unpack(">i", self._take(4))[0]
            return [struct.unpack(">i", self._take(4))[0] for _ in range(n)]
        if tag_type == 12:
            n = struct.unpack(">i", self._take(4))[0]
            return [struct.unpack(">q", self._take(8))[0] for _ in range(n)]
        raise ValueError(f"unknown NBT tag type {tag_type}")


def parse_nbt_file(path: str) -> dict:
    with gzip.open(path, "rb") as f:
        data = f.read()
    reader = _NBTReader(data)
    root_type = reader.read_u8()
    if root_type != 10:
        raise ValueError("root tag is not a compound")
    reader.read_string()  # root name, usually empty
    return reader.read_payload(10)


def offline_uuid(name: str) -> str:
    """Replicates Java's UUID.nameUUIDFromBytes(("OfflinePlayer:"+name)) used
    for offline-mode (online-mode=false) player UUIDs."""
    data = f"OfflinePlayer:{name}".encode("utf-8")
    h = bytearray(hashlib.md5(data).digest())
    h[6] = (h[6] & 0x0F) | 0x30
    h[8] = (h[8] & 0x3F) | 0x80
    return str(uuid_module.UUID(bytes=bytes(h)))


def get_ops() -> dict:
    try:
        with open(f"{MC_DIR}/ops.json") as f:
            ops = json.load(f)
        return {o["name"]: o["level"] for o in ops}
    except Exception:
        return {}


def update_ops_json(name: str, level: int | None):
    ops_path = f"{MC_DIR}/ops.json"
    try:
        with open(ops_path) as f:
            ops = json.load(f)
    except Exception:
        ops = []
    ops = [o for o in ops if o.get("name") != name]
    if level and level > 0:
        ops.append({
            "uuid": offline_uuid(name),
            "name": name,
            "level": level,
            "bypassesPlayerLimit": False,
        })
    with open(ops_path, "w") as f:
        json.dump(ops, f, indent=2)


def get_all_players() -> list:
    try:
        with open(f"{MC_DIR}/usercache.json") as f:
            cache = json.load(f)
    except Exception:
        cache = []
    ops = get_ops()
    ips = get_player_ips()
    online_names = set()
    try:
        status = query_status(MC_HOST, MC_PORT, timeout=2.0)
        online_names = set(status.get("players_sample") or [])
    except Exception:
        pass

    result = []
    for entry in cache:
        name = entry.get("name")
        pid = entry.get("uuid")
        if not name:
            continue
        gamemode = None
        if name in online_names:
            gamemode = get_live_gamemode(name)
        if gamemode is None:
            dat_path = f"{MC_DIR}/world/players/data/{pid}.dat"
            if os.path.exists(dat_path):
                try:
                    nbt = parse_nbt_file(dat_path)
                    gm = nbt.get("playerGameType")
                    if gm is not None:
                        gamemode = GAMEMODE_NAMES.get(gm, str(gm))
                except Exception:
                    gamemode = None
        result.append({
            "name": name,
            "uuid": pid,
            "gamemode": gamemode,
            "op_level": ops.get(name, 0),
            "ip": ips.get(name),
        })
    result.sort(key=lambda p: p["name"].lower())
    return result


def _send_and_capture(command: str, wait: float) -> list:
    try:
        start_size = os.path.getsize(LOG_PATH)
    except Exception:
        start_size = 0
    escaped = command.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(
        ["screen", "-p", "0", "-S", "minecraft", "-X", "eval", f'stuff "{escaped}\\015"'],
        check=True, timeout=5,
    )
    time.sleep(wait)
    try:
        with open(LOG_PATH, "rb") as f:
            f.seek(start_size)
            new_data = f.read()
        return new_data.decode("utf-8", errors="replace").splitlines()
    except Exception:
        return []


def send_console_command(command: str) -> list:
    return _send_and_capture(command, wait=1.2)


def get_live_gamemode(name: str) -> str | None:
    """Queries the player's live entity data instead of their NBT file on
    disk, which Paper only flushes on disconnect or world auto-save — a
    just-changed gamemode wouldn't show up there until then."""
    output = _send_and_capture(f"data get entity {name} playerGameType", wait=0.5)
    for line in output:
        m = re.search(r"has the following entity data: (\d+)", line)
        if m:
            return GAMEMODE_NAMES.get(int(m.group(1)))
    return None


def is_player_online(name: str) -> bool:
    try:
        status = query_status(MC_HOST, MC_PORT, timeout=2.0)
        return name in (status.get("players_sample") or [])
    except Exception:
        return False


def apply_player_action(name: str, action: str, value: str) -> dict:
    if action == "gamemode":
        if value not in ("survival", "creative", "adventure", "spectator"):
            return {"success": False, "error": "gamemode không hợp lệ"}
        if not is_player_online(name):
            return {"success": False, "error": f"{name} đang offline — chỉ đổi gamemode được khi người chơi đang online"}
        send_console_command(f"gamemode {value} {name}")
        return {"success": True}
    if action == "op_level":
        try:
            level = int(value)
        except ValueError:
            return {"success": False, "error": "level không hợp lệ"}
        if level <= 0:
            update_ops_json(name, None)
            return {"success": True, "note": "Đã xoá quyền OP — có hiệu lực khi người chơi vào lại (rejoin)."}
        # Always edit ops.json directly instead of running the game's own
        # /op or /deop command: Minecraft resolves player names
        # case-insensitively against the profile cache, so `/op <name>`
        # can silently match a different cached UUID that only differs by
        # letter case (this happened for real — two distinct players,
        # "NovaSeele" and "novaseele", got merged onto one via /op). Direct
        # edits here are matched by exact name, so they always target the
        # right UUID. We also deliberately do NOT run `/reload` to apply
        # it live — Paper's plugin reload is known to be unstable (it
        # crashed the server here once already) and isn't worth it for a
        # permission tweak. The edit takes effect the next time the player
        # joins, which Bukkit always reads ops.json fresh for.
        update_ops_json(name, level)
        return {"success": True, "note": "Đã lưu — có hiệu lực khi người chơi vào lại (rejoin), không áp dụng ngay để tránh reload server hoặc trùng tên không phân biệt hoa/thường."}
    return {"success": False, "error": "hành động không hợp lệ"}


DANGEROUS_COMMANDS = {"stop", "end", "restart"}


def run_console_command(command: str, confirmed: bool) -> dict:
    normalized = command.strip().lower().split()
    head = normalized[0] if normalized else ""
    if head in DANGEROUS_COMMANDS and not confirmed:
        return {
            "success": False,
            "needs_confirm": True,
            "error": f'Lệnh "{head}" sẽ dừng server. Cần xác nhận để tiếp tục.',
        }
    output = send_console_command(command)
    return {"success": True, "output": output}


def _read_cpu_jiffies():
    with open("/proc/stat") as f:
        parts = f.readline().split()
    values = list(map(int, parts[1:]))
    idle = values[3] + values[4]  # idle + iowait
    total = sum(values)
    return idle, total


def get_system_stats() -> dict:
    idle1, total1 = _read_cpu_jiffies()
    time.sleep(0.25)
    idle2, total2 = _read_cpu_jiffies()
    idle_delta = idle2 - idle1
    total_delta = total2 - total1
    cpu_percent = round((1 - idle_delta / total_delta) * 100, 1) if total_delta else 0.0

    meminfo = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, _, rest = line.partition(":")
            meminfo[key] = int(rest.strip().split()[0])  # kB
    mem_total_kb = meminfo.get("MemTotal", 0)
    mem_available_kb = meminfo.get("MemAvailable", 0)
    mem_used_kb = mem_total_kb - mem_available_kb

    disk = shutil.disk_usage("/")

    return {
        "cpu_percent": cpu_percent,
        "ram_used_mb": round(mem_used_kb / 1024),
        "ram_total_mb": round(mem_total_kb / 1024),
        "disk_used_gb": round(disk.used / (1024 ** 3), 1),
        "disk_total_gb": round(disk.total / (1024 ** 3), 1),
    }


def get_current_jar() -> dict | None:
    jars = glob.glob(f"{MC_DIR}/paper-*.jar")
    if not jars:
        return None
    name = os.path.basename(jars[0])
    m = re.match(r"paper-([\w.-]+)-(\d+)\.jar$", name)
    if not m:
        return {"jar_name": name, "mc_version": None, "build": None}
    return {"jar_name": name, "mc_version": m.group(1), "build": int(m.group(2))}


def get_latest_stable() -> dict:
    with urllib.request.urlopen(f"{FILL_API}", timeout=10) as resp:
        project = json.load(resp)
    # Keys are ordered newest-group-first by the API.
    for group, versions in project["versions"].items():
        # Prefer a plain release name in the group (skip "-rc-"/"-pre" entries).
        stable_versions = [v for v in versions if "-" not in v] or versions
        mc_version = stable_versions[0]
        with urllib.request.urlopen(f"{FILL_API}/versions/{mc_version}/builds", timeout=10) as resp:
            builds = json.load(resp)
        stable_builds = [b for b in builds if b["channel"] == "STABLE"]
        if not stable_builds:
            continue
        build = stable_builds[0]
        dl = build["downloads"]["server:default"]
        return {
            "mc_version": mc_version,
            "build": build["id"],
            "jar_name": dl["name"],
            "url": dl["url"],
            "sha256": dl["checksums"]["sha256"],
        }
    raise RuntimeError("no stable Paper build found")


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def perform_update() -> dict:
    current = get_current_jar()
    latest = get_latest_stable()

    if current and current.get("jar_name") == latest["jar_name"]:
        return {"success": False, "error": "already up to date", "current": current}

    tmp_path = f"{MC_DIR}/{latest['jar_name']}.downloading"
    urllib.request.urlretrieve(latest["url"], tmp_path)

    actual_sha = _sha256_file(tmp_path)
    if actual_sha != latest["sha256"]:
        os.remove(tmp_path)
        return {"success": False, "error": f"checksum mismatch (got {actual_sha[:12]}...)"}

    final_path = f"{MC_DIR}/{latest['jar_name']}"
    os.replace(tmp_path, final_path)

    with open(RUN_SH) as f:
        run_sh = f.read()
    run_sh = re.sub(r"paper-[\w.-]+-\d+\.jar", latest["jar_name"], run_sh)
    with open(RUN_SH, "w") as f:
        f.write(run_sh)

    subprocess.run(["systemctl", "stop", "minecraft.service"], check=True, timeout=90)

    if current and current.get("jar_name") and current["jar_name"] != latest["jar_name"]:
        old_path = f"{MC_DIR}/{current['jar_name']}"
        if os.path.exists(old_path):
            os.remove(old_path)

    subprocess.run(["systemctl", "start", "minecraft.service"], check=True, timeout=30)

    deadline = time.monotonic() + 90
    last_error = "timed out waiting for server to come back online"
    while time.monotonic() < deadline:
        time.sleep(3)
        try:
            query_status(MC_HOST, MC_PORT, timeout=2.0)
            return {"success": True, "from": current, "to": latest}
        except Exception as e:
            last_error = str(e)
    return {"success": False, "error": f"server did not come back online: {last_error}", "to": latest}


DASHBOARD_HTML = """<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<title>Minecraft Server Status</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    background: #0f1115; color: #e6e6e6; font-family: -apple-system, Segoe UI, Roboto, sans-serif;
    margin: 0; padding: 0;
  }
  .shell { display: flex; min-height: 100vh; }
  .sidebar {
    width: 220px; flex-shrink: 0; background: #12141a; border-right: 1px solid #22252d;
    padding: 1.25rem 0.85rem; display: flex; flex-direction: column; gap: 0.25rem;
    position: sticky; top: 0; height: 100vh; overflow-y: auto; box-sizing: border-box;
  }
  .brand { display: flex; align-items: center; gap: 0.55rem; font-weight: 600; font-size: 0.95rem; padding: 0.3rem 0.6rem 1.4rem; }
  .nav-item {
    background: none; border: none; color: #9096a2; text-align: left; padding: 0.65rem 0.8rem;
    border-radius: 8px; font-size: 0.87rem; cursor: pointer; font-family: inherit; white-space: nowrap;
  }
  .nav-item:hover { background: #1c1f27; color: #e6e6e6; }
  .nav-item.active { background: #232733; color: #fff; font-weight: 600; }
  .sidebar-footer { padding: 0.9rem 0.6rem 0; font-size: 0.78rem; color: #9096a2; line-height: 1.5; }
  .auth-box { margin-top: auto; padding: 0.9rem 0.6rem 0; border-top: 1px solid #22252d; flex-shrink: 0; }
  .auth-note { font-size: 0.72rem; color: #6b7280; line-height: 1.4; margin-bottom: 0.55rem; }
  .auth-input {
    width: 100%; background: #1c1f27; color: #e6e6e6; border: 1px solid #2a2e38; border-radius: 6px;
    padding: 0.4rem 0.55rem; font-size: 0.8rem; margin-bottom: 0.4rem; font-family: inherit;
  }
  .auth-btn {
    width: 100%; background: #3b82f6; color: #fff; border: none; border-radius: 6px;
    padding: 0.45rem; font-size: 0.8rem; cursor: pointer; font-family: inherit;
  }
  .auth-btn:hover { background: #2f6fd6; }
  .auth-btn.logout { background: #232733; color: #9096a2; }
  .auth-btn.logout:hover { background: #2a2e3a; color: #e6e6e6; }
  .auth-msg { font-size: 0.72rem; margin-top: 0.4rem; min-height: 1em; }
  .auth-msg.err { color: #e5484d; }
  .auth-status-ok { font-size: 0.78rem; color: #3ecf6a; margin-bottom: 0.5rem; display: flex; align-items: center; gap: 0.35rem; }
  .content { flex: 1; padding: 2rem; max-width: 1200px; }
  .tab-panel { display: flex; flex-direction: column; gap: 1rem; }
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; align-items: start; }
  .grid-console { display: grid; grid-template-columns: 1fr 1.3fr; gap: 1rem; align-items: start; }
  @media (max-width: 860px) {
    .shell { flex-direction: column; }
    .sidebar {
      width: 100%; flex-direction: row; flex-wrap: wrap; align-items: center; padding: 0.65rem 0.8rem;
      border-right: none; border-bottom: 1px solid #22252d; position: static; height: auto;
    }
    .brand { padding: 0 0.8rem 0 0; }
    .sidebar-footer { display: none; }
    .auth-box { flex-basis: 100%; margin-top: 0.6rem; border-top: none; max-width: 280px; }
    .content { padding: 1rem; }
    .grid-2, .grid-console { grid-template-columns: 1fr; }
  }
  .card {
    background: #171a21; border: 1px solid #2a2e38; border-radius: 12px;
    padding: 1.75rem; width: 100%; box-sizing: border-box;
  }
  h1 { font-size: 1.15rem; margin: 0 0 1rem; display: flex; align-items: center; gap: 0.5rem; }
  h1.card-title { font-size: 0.8rem; color: #9096a2; text-transform: uppercase; letter-spacing: 0.05em; margin: 0 0 1rem; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: #555; flex-shrink: 0; }
  .dot.online { background: #3ecf6a; box-shadow: 0 0 8px #3ecf6a; }
  .dot.offline { background: #e5484d; }
  .row { display: flex; justify-content: space-between; padding: 0.5rem 0; border-bottom: 1px solid #22252d; font-size: 0.92rem; }
  .row:last-child { border-bottom: none; }
  .label { color: #9096a2; }
  .players { margin-top: 1rem; }
  .players h2 { font-size: 0.85rem; color: #9096a2; text-transform: uppercase; letter-spacing: 0.04em; margin: 0 0 0.5rem; }
  .chip { display: inline-block; background: #232733; border-radius: 999px; padding: 0.25rem 0.7rem; margin: 0.2rem 0.25rem 0.2rem 0; font-size: 0.85rem; }
  .muted { color: #6b7280; font-size: 0.8rem; margin-top: 1.25rem; text-align: center; }
  .error { color: #e5484d; }
  .section-title { font-size: 0.85rem; color: #9096a2; text-transform: uppercase; letter-spacing: 0.04em; margin: 1.25rem 0 0.5rem; }
  .note { color: #6b7280; font-size: 0.78rem; margin-top: 0.75rem; line-height: 1.4; }
  .gauge-row { margin: 0.5rem 0; }
  .gauge-label { display: flex; justify-content: space-between; font-size: 0.85rem; margin-bottom: 0.3rem; }
  .gauge-track { background: #22252d; border-radius: 999px; height: 8px; overflow: hidden; }
  .gauge-fill { height: 100%; border-radius: 999px; background: #3b82f6; transition: width 0.4s ease; }
  .gauge-fill.warn { background: #eab308; }
  .gauge-fill.crit { background: #e5484d; }
  .btn {
    background: #232733; color: #e6e6e6; border: 1px solid #2a2e38; border-radius: 8px;
    padding: 0.55rem 1rem; font-size: 0.85rem; cursor: pointer; width: 100%; margin-top: 0.5rem;
  }
  .btn:hover { background: #2a2e3a; }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn.primary { background: #3ecf6a; color: #0f1115; border-color: #3ecf6a; font-weight: 600; }
  .btn.primary:hover { background: #34b85c; }
  .version-msg { font-size: 0.82rem; margin-top: 0.6rem; line-height: 1.4; }
  .version-msg.ok { color: #3ecf6a; }
  .version-msg.err { color: #e5484d; }
  .spinner {
    display: inline-block; width: 12px; height: 12px; border: 2px solid #4a4f5c;
    border-top-color: #e6e6e6; border-radius: 50%; animation: spin 0.7s linear infinite; margin-right: 0.4rem;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .ptable { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  .ptable th { text-align: left; color: #9096a2; font-weight: 500; padding: 0.4rem 0.3rem; border-bottom: 1px solid #2a2e38; }
  .ptable td { padding: 0.4rem 0.3rem; border-bottom: 1px solid #1c1f27; vertical-align: middle; }
  .ptable tr:last-child td { border-bottom: none; }
  .pname { display: flex; align-items: center; gap: 0.4rem; }
  .online-dot { width: 7px; height: 7px; border-radius: 50%; background: #444; flex-shrink: 0; }
  .online-dot.on { background: #3ecf6a; }
  .sel {
    background: #1c1f27; color: #e6e6e6; border: 1px solid #2a2e38; border-radius: 6px;
    padding: 0.3rem 0.4rem; font-size: 0.8rem; width: 100%;
  }
  .sel:disabled { opacity: 0.4; cursor: not-allowed; }
  .cell-action { min-width: 110px; }
  .oplevel-table td { vertical-align: top; line-height: 1.5; }
  .oplevel-table code { background: #1c1f27; padding: 0.1rem 0.35rem; border-radius: 4px; color: #6cb6ff; font-size: 0.78rem; }
  .lvl-badge {
    display: inline-block; padding: 0.2rem 0.6rem; border-radius: 999px; font-size: 0.78rem; font-weight: 600; white-space: nowrap;
  }
  .lvl-0 { background: #232733; color: #9096a2; }
  .lvl-1 { background: #1e3a2e; color: #4ade80; }
  .lvl-2 { background: #1e2f4a; color: #6cb6ff; }
  .lvl-3 { background: #4a3a1e; color: #f0b93b; }
  .lvl-4 { background: #4a1e1e; color: #f87171; }
  .action-msg { font-size: 0.72rem; margin-top: 0.15rem; min-height: 1em; }
  .action-msg.err { color: #e5484d; }
  .action-msg.ok { color: #3ecf6a; }
  .console-out {
    background: #0b0d11; border: 1px solid #2a2e38; border-radius: 8px; padding: 0.6rem 0.75rem;
    font-family: 'Consolas', 'SF Mono', monospace; font-size: 0.75rem; height: 220px; overflow-y: auto;
    white-space: pre-wrap; word-break: break-all; color: #b8bfcc; margin-bottom: 0.5rem;
  }
  .console-line.cmd { color: #6cb6ff; }
  .console-row { display: flex; gap: 0.5rem; }
  .console-input {
    flex: 1; background: #1c1f27; color: #e6e6e6; border: 1px solid #2a2e38; border-radius: 6px;
    padding: 0.5rem 0.7rem; font-family: 'Consolas', 'SF Mono', monospace; font-size: 0.85rem;
  }
  .btn-send { background: #3b82f6; color: #fff; border: none; border-radius: 6px; padding: 0 1.1rem; font-size: 0.85rem; cursor: pointer; }
  .btn-send:hover { background: #2f6fd6; }
  details.cmd-ref summary { cursor: pointer; font-size: 0.85rem; color: #b8bfcc; padding: 0.4rem 0; }
  .cmd-tabs { display: flex; gap: 0.35rem; flex-wrap: wrap; margin: 0.75rem 0 0.9rem; }
  .cmd-tab {
    background: #1c1f27; color: #9096a2; border: 1px solid #2a2e38; border-radius: 999px;
    padding: 0.32rem 0.8rem; font-size: 0.75rem; cursor: pointer; font-family: inherit;
  }
  .cmd-tab:hover { background: #232733; color: #e6e6e6; }
  .cmd-tab.active { background: #3b82f6; color: #fff; border-color: #3b82f6; font-weight: 600; }
  .cmd-group-title { font-size: 0.72rem; color: #6b7280; text-transform: uppercase; letter-spacing: 0.05em; margin: 1rem 0 0.3rem; }
  .cmd-group-title:first-child { margin-top: 0.3rem; }
  .cmd-item { display: flex; flex-direction: column; gap: 0.2rem; padding: 0.5rem 0.4rem; border-bottom: 1px solid #1c1f27; font-size: 0.78rem; cursor: pointer; border-radius: 6px; }
  .cmd-item:hover { background: #1c1f27; }
  .cmd-item code { color: #6cb6ff; white-space: nowrap; overflow-x: auto; }
  .cmd-item code::-webkit-scrollbar { display: none; }
  .cmd-item span.desc { color: #9096a2; }
  #cmdRefList {
    max-height: 640px; overflow-y: auto;
    scrollbar-width: none; -ms-overflow-style: none;
  }
  #cmdRefList::-webkit-scrollbar { display: none; }
</style>
</head>
<body>
<div class="shell">
  <nav class="sidebar">
    <div class="brand"><span class="dot" id="dot"></span>MC Dashboard</div>
    <button class="nav-item active" data-tab="overview" onclick="switchTab('overview')">Tổng quan</button>
    <button class="nav-item" data-tab="players" onclick="switchTab('players')">Người chơi</button>
    <button class="nav-item" data-tab="console" onclick="switchTab('console')">Console</button>

    <div class="auth-box" id="authBox"></div>

    <div class="sidebar-footer">
      <div id="title">Đang tải...</div>
      <div>Tự làm mới mỗi 1 giây</div>
      <div id="updated"></div>
    </div>
  </nav>
  <main class="content">
    <section class="tab-panel" id="tab-overview">
      <div class="grid-2">
        <div class="card">
          <h1 class="card-title">Người chơi đang online</h1>
          <div id="playerContent"></div>
        </div>
        <div class="card">
          <h1 class="card-title">Tài nguyên VPS</h1>
          <div id="sysContent"></div>
        </div>
      </div>
      <div class="card">
        <h1 class="card-title">Phiên bản Server</h1>
        <div class="row"><span class="label">Đang chạy</span><span id="curVersion">...</span></div>
        <button class="btn" id="checkBtn" onclick="checkVersion()">Kiểm tra cập nhật</button>
        <div id="versionMsg"></div>
      </div>
    </section>

    <section class="tab-panel" id="tab-players" style="display:none">
      <div class="card">
        <h1 class="card-title">Quản lý người chơi</h1>
        <div class="note" style="margin-top:0; margin-bottom:1rem">Chỉ đổi được Gamemode khi người chơi đang online (chấm xanh cạnh tên). OP level đổi được cả khi offline.</div>
        <table class="ptable">
          <thead><tr><th>Tên</th><th>Gamemode</th><th>OP level</th></tr></thead>
          <tbody id="playerTableBody"><tr><td colspan="3" class="label">Đang tải...</td></tr></tbody>
        </table>
      </div>
      <div class="card">
        <h1 class="card-title">OP level là gì?</h1>
        <table class="ptable oplevel-table">
          <thead><tr><th>Level</th><th>Có quyền gì</th></tr></thead>
          <tbody>
            <tr><td><span class="lvl-badge lvl-0">Level 0</span></td><td>Người chơi thường, không có quyền đặc biệt gì (mặc định của mọi người khi chưa OP).</td></tr>
            <tr><td><span class="lvl-badge lvl-1">Level 1</span></td><td>Bỏ qua vùng bảo vệ spawn (spawn protection) — được đặt/phá block gần điểm spawn.</td></tr>
            <tr><td><span class="lvl-badge lvl-2">Level 2</span></td><td>Dùng được các lệnh admin cơ bản: <code>gamemode</code>, <code>give</code>, <code>tp</code>, <code>effect</code>, <code>gamerule</code>, <code>weather</code>, <code>time</code>, <code>kill</code>, <code>summon</code>, <code>setblock</code>, <code>fill</code>, dùng được command block.</td></tr>
            <tr><td><span class="lvl-badge lvl-3">Level 3</span></td><td>Thêm quyền quản lý người chơi khác: <code>ban</code>, <code>kick</code>, <code>whitelist</code>, <code>banlist</code>, <code>op</code>/<code>deop</code> người khác.</td></tr>
            <tr><td><span class="lvl-badge lvl-4">Level 4</span></td><td>Toàn quyền — bao gồm cả <code>stop</code> (dừng hẳn server) và mọi lệnh quản trị khác. Cẩn thận khi cấp mức này.</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="tab-panel" id="tab-console" style="display:none">
      <div class="grid-console">
        <div class="card">
          <h1 class="card-title">Console</h1>
          <div class="console-out" id="consoleOut"></div>
          <div class="console-row">
            <input type="text" class="console-input" id="consoleInput" placeholder="Nhập lệnh, vd: say hello" autocomplete="off">
            <button class="btn-send" onclick="sendConsoleCommand()">Gửi</button>
          </div>
          <div class="note">Lệnh nguy hiểm (stop, end, restart) sẽ hỏi xác nhận trước khi thực thi — tránh dừng nhầm server.</div>
        </div>
        <div class="card">
          <h1 class="card-title">Lệnh hữu ích</h1>
          <div class="note" style="margin-top:0">Bấm 1 dòng để điền vào ô console.</div>
          <div class="cmd-tabs" id="cmdTabs"></div>
          <div id="cmdRefList"></div>
        </div>
      </div>
    </section>
  </main>
</div>
<script>
let onlinePlayers = new Set();

// --- Auth ---------------------------------------------------------------
let isLoggedIn = false;

function renderAuthBox() {
  const el = document.getElementById('authBox');
  if (isLoggedIn) {
    el.innerHTML = `
      <div class="auth-status-ok">🔓 Đã đăng nhập</div>
      <button class="auth-btn logout" onclick="doLogout()">Đăng xuất</button>
    `;
  } else {
    el.innerHTML = `
      <div class="auth-note">🔒 Cần đăng nhập để chỉnh sửa: đổi gamemode, OP level, gửi lệnh console, cập nhật version. Xem thông tin thì không cần.</div>
      <input type="text" class="auth-input" id="authUser" placeholder="Tài khoản" autocomplete="username">
      <input type="password" class="auth-input" id="authPass" placeholder="Mật khẩu" autocomplete="current-password">
      <button class="auth-btn" onclick="doLogin()">Đăng nhập</button>
      <div class="auth-msg" id="authMsg"></div>
    `;
    document.getElementById('authPass').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') doLogin();
    });
  }
}

async function checkAuthStatus() {
  try {
    const res = await fetch('/api/auth-status', { cache: 'no-store' });
    const data = await res.json();
    isLoggedIn = !!data.logged_in;
  } catch (e) {
    isLoggedIn = false;
  }
  renderAuthBox();
}

async function doLogin() {
  const username = document.getElementById('authUser').value;
  const password = document.getElementById('authPass').value;
  const msg = document.getElementById('authMsg');
  msg.textContent = '';
  msg.className = 'auth-msg';
  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    const data = await res.json();
    if (data.success) {
      isLoggedIn = true;
      renderAuthBox();
    } else {
      msg.textContent = data.error || 'Đăng nhập thất bại';
      msg.className = 'auth-msg err';
    }
  } catch (e) {
    msg.textContent = 'Lỗi: ' + e;
    msg.className = 'auth-msg err';
  }
}

async function doLogout() {
  try {
    await fetch('/api/logout', { method: 'POST' });
  } catch (e) {}
  isLoggedIn = false;
  renderAuthBox();
}

function handleAuthRequired(data) {
  if (!data || !data.auth_required) return false;
  isLoggedIn = false;
  renderAuthBox();
  const box = document.getElementById('authBox');
  box.style.outline = '2px solid #e5484d';
  setTimeout(() => { box.style.outline = ''; }, 1500);
  return true;
}

function switchTab(name) {
  document.querySelectorAll('.tab-panel').forEach(el => {
    el.style.display = el.id === 'tab-' + name ? 'flex' : 'none';
  });
  document.querySelectorAll('.nav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.tab === name);
  });
  localStorage.setItem('mc-dashboard-tab', name);
}
switchTab(localStorage.getItem('mc-dashboard-tab') || 'overview');
checkAuthStatus();
function pct(used, total) {
  if (!total) return 0;
  return Math.round((used / total) * 100);
}
function gaugeRow(label, percent, unit, sub) {
  const p = Math.max(0, Math.min(100, percent));
  const cls = p >= 90 ? 'crit' : (p >= 70 ? 'warn' : '');
  return `
    <div class="gauge-row">
      <div class="gauge-label"><span class="label">${label}</span><span>${sub ? sub + ' &middot; ' : ''}${p}${unit}</span></div>
      <div class="gauge-track"><div class="gauge-fill ${cls}" style="width:${p}%"></div></div>
    </div>
  `;
}
function renderSys(data) {
  return `
    ${gaugeRow('CPU', data.cpu_percent, '%')}
    ${gaugeRow('RAM', pct(data.ram_used_mb, data.ram_total_mb), '%', `${data.ram_used_mb} / ${data.ram_total_mb} MB`)}
    ${gaugeRow('Ổ đĩa (SSD)', pct(data.disk_used_gb, data.disk_total_gb), '%', `${data.disk_used_gb} / ${data.disk_total_gb} GB`)}
  `;
}
async function refresh() {
  const dot = document.getElementById('dot');
  const title = document.getElementById('title');
  const playerContent = document.getElementById('playerContent');
  const sysContent = document.getElementById('sysContent');
  const updated = document.getElementById('updated');
  try {
    const res = await fetch('/api/status', { cache: 'no-store' });
    const data = await res.json();
    if (data.online) {
      dot.className = 'dot online';
      title.textContent = 'Server đang chạy';
      onlinePlayers = new Set(data.players_sample || []);
      playerContent.innerHTML = `
        <div class="row"><span class="label">Người chơi</span><span>${data.players_online} / ${data.players_max}</span></div>
        <div class="row"><span class="label">Version</span><span>${data.version}</span></div>
        <div class="row"><span class="label">Ping</span><span>${data.latency_ms} ms</span></div>
      `;
    } else {
      dot.className = 'dot offline';
      title.textContent = 'Server offline';
      playerContent.innerHTML = `<div class="row error">${data.error || 'Không kết nối được'}</div>`;
    }
    if (data.cpu_percent !== undefined) {
      sysContent.innerHTML = renderSys(data);
    }
  } catch (e) {
    dot.className = 'dot offline';
    title.textContent = 'Không tải được trạng thái';
    playerContent.innerHTML = '';
  }
  updated.textContent = 'Cập nhật lúc ' + new Date().toLocaleTimeString('vi-VN');
}
refresh();
setInterval(refresh, 1000);

async function loadCurrentVersion() {
  try {
    const res = await fetch('/api/version', { cache: 'no-store' });
    const data = await res.json();
    const c = data.current;
    document.getElementById('curVersion').textContent = c
      ? `Paper ${c.mc_version} build ${c.build}`
      : 'Không rõ';
  } catch (e) {
    document.getElementById('curVersion').textContent = 'Lỗi tải';
  }
}

async function checkVersion() {
  const btn = document.getElementById('checkBtn');
  const msg = document.getElementById('versionMsg');
  btn.disabled = true;
  msg.innerHTML = '<span class="spinner"></span>Đang kiểm tra...';
  try {
    const res = await fetch('/api/version', { cache: 'no-store' });
    const data = await res.json();
    if (data.error) {
      msg.innerHTML = `<div class="version-msg err">${data.error}</div>`;
      btn.disabled = false;
      return;
    }
    const cur = data.current;
    const latest = data.latest;
    if (cur && cur.jar_name === latest.jar_name) {
      msg.innerHTML = `<div class="version-msg ok">Đã ở bản mới nhất (Paper ${latest.mc_version} build ${latest.build})</div>`;
      btn.disabled = false;
    } else {
      msg.innerHTML = `
        <div class="version-msg">Có bản mới: <b>Paper ${latest.mc_version} build ${latest.build}</b></div>
        <button class="btn primary" id="updateBtn" onclick="doUpdate()">Cập nhật ngay (server sẽ khởi động lại)</button>
      `;
      btn.disabled = false;
    }
  } catch (e) {
    msg.innerHTML = `<div class="version-msg err">Không kiểm tra được: ${e}</div>`;
    btn.disabled = false;
  }
}

async function doUpdate() {
  if (!confirm('Cập nhật sẽ dừng server vài chục giây, đuổi hết người đang chơi. Tiếp tục?')) return;
  const msg = document.getElementById('versionMsg');
  const updateBtn = document.getElementById('updateBtn');
  if (updateBtn) updateBtn.disabled = true;
  document.getElementById('checkBtn').disabled = true;
  msg.innerHTML = '<span class="spinner"></span>Đang tải + cập nhật + khởi động lại (có thể mất 1-2 phút)...';
  try {
    const res = await fetch('/api/update', { method: 'POST' });
    const data = await res.json();
    if (handleAuthRequired(data)) {
      msg.innerHTML = `<div class="version-msg err">${data.error}</div>`;
    } else if (data.success) {
      msg.innerHTML = `<div class="version-msg ok">Cập nhật xong! Đang chạy Paper ${data.to.mc_version} build ${data.to.build}</div>`;
      loadCurrentVersion();
    } else {
      msg.innerHTML = `<div class="version-msg err">Cập nhật thất bại: ${data.error}</div>`;
    }
  } catch (e) {
    msg.innerHTML = `<div class="version-msg err">Lỗi khi cập nhật: ${e}</div>`;
  }
  document.getElementById('checkBtn').disabled = false;
}

loadCurrentVersion();

// --- Player management ------------------------------------------------
function playerRow(p) {
  const isOnline = onlinePlayers.has(p.name);
  const gm = p.gamemode || 'survival';
  const modes = ['survival', 'creative', 'adventure', 'spectator'];
  const gmOptions = modes.map(m => `<option value="${m}" ${m === gm ? 'selected' : ''}>${m}</option>`).join('');
  const levels = [0, 1, 2, 3, 4];
  const levelOptions = levels.map(l => `<option value="${l}" ${l === p.op_level ? 'selected' : ''}>Level ${l}</option>`).join('');
  return `
    <tr>
      <td><div class="pname"><span class="online-dot ${isOnline ? 'on' : ''}"></span>${p.name}${p.ip ? ' <span class="label">· ' + p.ip + '</span>' : ''}</div></td>
      <td class="cell-action">
        <select class="sel" ${isOnline ? '' : 'disabled title="Chỉ đổi được khi online"'} onchange="handlePlayerAction('${p.name}','gamemode',this.value,this)">
          ${gmOptions}
        </select>
        <div class="action-msg" id="msg-gm-${p.name}"></div>
      </td>
      <td class="cell-action">
        <select class="sel" onchange="handlePlayerAction('${p.name}','op_level',this.value,this)">
          ${levelOptions}
        </select>
        <div class="action-msg" id="msg-op-${p.name}"></div>
      </td>
    </tr>
  `;
}

async function loadPlayers() {
  try {
    const res = await fetch('/api/players', { cache: 'no-store' });
    const data = await res.json();
    const tbody = document.getElementById('playerTableBody');
    if (!data.players || !data.players.length) {
      tbody.innerHTML = '<tr><td colspan="3" class="label">Chưa có ai từng vào server</td></tr>';
      return;
    }
    tbody.innerHTML = data.players.map(playerRow).join('');
  } catch (e) {
    document.getElementById('playerTableBody').innerHTML = `<tr><td colspan="3" class="error">Lỗi tải danh sách: ${e}</td></tr>`;
  }
}

async function handlePlayerAction(name, action, value, selectEl) {
  const msgId = action === 'gamemode' ? `msg-gm-${name}` : `msg-op-${name}`;
  const msg = document.getElementById(msgId);
  selectEl.disabled = true;
  msg.textContent = 'Đang xử lý...';
  msg.className = 'action-msg';
  let hasNote = false;
  try {
    const res = await fetch('/api/player-action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, action, value }),
    });
    const data = await res.json();
    if (handleAuthRequired(data)) {
      msg.textContent = data.error;
      msg.className = 'action-msg err';
    } else if (data.success) {
      msg.textContent = data.note || 'Đã áp dụng';
      msg.className = 'action-msg ok';
      hasNote = !!data.note;
    } else {
      msg.textContent = data.error || 'Thất bại';
      msg.className = 'action-msg err';
    }
  } catch (e) {
    msg.textContent = 'Lỗi: ' + e;
    msg.className = 'action-msg err';
  }
  selectEl.disabled = false;
  setTimeout(() => { msg.textContent = ''; }, hasNote ? 8000 : 4000);
}

// --- Console ------------------------------------------------------------
const DANGEROUS_HEADS = ['stop', 'end', 'restart'];

function appendConsoleLine(text, isCmd) {
  const out = document.getElementById('consoleOut');
  const div = document.createElement('div');
  div.className = 'console-line' + (isCmd ? ' cmd' : '');
  div.textContent = text;
  out.appendChild(div);
  out.scrollTop = out.scrollHeight;
}

async function sendConsoleCommand(forceConfirm) {
  const input = document.getElementById('consoleInput');
  const command = input.value.trim();
  if (!command) return;
  const head = command.toLowerCase().split(/\\s+/)[0];
  if (DANGEROUS_HEADS.includes(head) && !forceConfirm) {
    if (!confirm(`Lệnh "${command}" có thể dừng/khởi động lại server. Chắc chắn muốn gửi?`)) return;
  }
  appendConsoleLine('> ' + command, true);
  input.value = '';
  try {
    const res = await fetch('/api/console', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command, confirm: true }),
    });
    const data = await res.json();
    if (handleAuthRequired(data)) {
      appendConsoleLine('[lỗi] ' + data.error, false);
    } else {
      if (data.output) data.output.forEach(l => appendConsoleLine(l, false));
      if (!data.success && data.error) appendConsoleLine('[lỗi] ' + data.error, false);
    }
  } catch (e) {
    appendConsoleLine('[lỗi] ' + e, false);
  }
}

document.getElementById('consoleInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') sendConsoleCommand();
});

// --- Useful commands reference -------------------------------------------
const USEFUL_COMMANDS = [
  { group: 'Người chơi', items: [
    ['list', 'Xem ai đang online'],
    ['tp <tên1> <tên2>', 'Dịch chuyển người chơi 1 tới người chơi 2'],
    ['gamemode <mode> <tên>', 'Đổi chế độ chơi'],
    ['give <tên> <item> <số lượng>', 'Cho vật phẩm'],
    ['clear <tên>', 'Xoá túi đồ người chơi'],
    ['effect give <tên> <hiệu ứng> <giây>', 'Thêm hiệu ứng, vd speed, jump_boost'],
    ['effect clear <tên>', 'Xoá hết hiệu ứng'],
    ['xp add <tên> <số> levels', 'Cộng level kinh nghiệm'],
    ['spawnpoint <tên>', 'Đặt điểm hồi sinh tại vị trí hiện tại'],
    ['enchant <tên> <phù_phép> <cấp>', 'Thêm phù phép vào vật phẩm đang cầm'],
    ['attribute <tên> <thuộc_tính> get', 'Xem chỉ số thuộc tính (máu, tốc độ...)'],
    ['advancement grant <tên> everything', 'Mở khoá hết thành tựu'],
    ['recipe give <tên> *', 'Mở khoá hết công thức chế tạo'],
    ['msg <tên> <nội dung>', 'Nhắn tin riêng cho 1 người'],
    ['team join <team> <tên>', 'Thêm người chơi vào team'],
    ['kick <tên>', 'Đuổi người chơi'],
    ['ban <tên>', 'Cấm người chơi'],
    ['ban-ip <ip>', 'Cấm theo địa chỉ IP'],
    ['pardon <tên>', 'Gỡ cấm'],
    ['pardon-ip <ip>', 'Gỡ cấm IP'],
    ['banlist', 'Xem danh sách bị cấm'],
  ]},
  { group: 'Thế giới', items: [
    ['tps', 'Xem TPS (độ mượt server)'],
    ['weather clear', 'Trời quang'],
    ['weather rain', 'Trời mưa'],
    ['time set day', 'Chuyển sang ban ngày'],
    ['time set night', 'Chuyển sang ban đêm'],
    ['difficulty peaceful', 'Đổi độ khó (peaceful/easy/normal/hard)'],
    ['gamerule keepInventory true', 'Không mất đồ khi chết'],
    ['gamerule doDaylightCycle false', 'Dừng chu kỳ ngày đêm'],
    ['gamerule doMobSpawning false', 'Tắt quái tự nhiên sinh ra'],
    ['gamerule mobGriefing false', 'Chặn mob phá block (creeper, enderman...)'],
    ['gamerule doFireTick false', 'Tắt lửa lan'],
    ['gamerule randomTickSpeed 0', 'Tắt random tick (cây/mía ngừng lớn)'],
    ['gamerule showDeathMessages false', 'Ẩn thông báo khi ai đó chết'],
    ['gamerule naturalRegeneration false', 'Tắt hồi máu tự nhiên'],
    ['kill @e[type=item]', 'Dọn item rơi vãi (giảm lag)'],
    ['seed', 'Xem seed world hiện tại'],
    ['setworldspawn', 'Đặt điểm spawn chung tại vị trí hiện tại'],
    ['worldborder set <số>', 'Đặt kích thước biên giới world'],
    ['worldborder center <x> <z>', 'Đặt tâm biên giới world'],
    ['locate structure <tên_công_trình>', 'Tìm công trình gần nhất (village, stronghold...)'],
    ['fill <x1 y1 z1> <x2 y2 z2> <block>', 'Đổ đầy 1 vùng bằng khối chỉ định'],
  ]},
  { group: 'Quản trị', items: [
    ['whitelist add <tên>', 'Thêm vào whitelist'],
    ['whitelist on', 'Bật whitelist'],
    ['whitelist off', 'Tắt whitelist'],
    ['whitelist list', 'Xem danh sách whitelist'],
    ['whitelist reload', 'Tải lại whitelist từ file'],
    ['op <tên>', 'Cấp quyền admin'],
    ['deop <tên>', 'Gỡ quyền admin'],
    ['save-all', 'Lưu world ngay lập tức'],
    ['reload confirm', 'Tải lại plugin/cấu hình (giật nhẹ)'],
    ['version', 'Xem phiên bản Paper đang chạy'],
    ['plugins', 'Xem danh sách plugin đã cài'],
    ['spark profiler start', 'Bắt đầu đo hiệu năng chi tiết server'],
    ['spark profiler stop', 'Dừng đo, xuất báo cáo hiệu năng'],
  ]},
  { group: 'Khác', items: [
    ['say <nội dung>', 'Gửi thông báo cho cả server'],
    ['tellraw @a {"text":"xin chào"}', 'Gửi tin nhắn định dạng nâng cao'],
    ['title @a title {"text":"Xin chào"}', 'Hiện chữ to giữa màn hình'],
    ['title @a actionbar {"text":"..."}', 'Hiện chữ nhỏ phía trên thanh hotbar'],
    ['playsound minecraft:entity.experience_orb.pickup master @a', 'Phát âm thanh cho cả server'],
    ['particle minecraft:flame <x> <y> <z>', 'Tạo hiệu ứng hạt tại toạ độ'],
    ['summon <entity> <x> <y> <z>', 'Triệu hồi 1 thực thể/mob'],
    ['setblock <x> <y> <z> <block>', 'Đặt 1 khối tại toạ độ'],
    ['execute as @a run <lệnh>', 'Chạy 1 lệnh cho tất cả người chơi'],
    ['datapack list', 'Xem danh sách datapack đang bật'],
  ]},
];

function escHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

let activeCmdCategory = 'all';

function renderCmdTabs() {
  const tabsEl = document.getElementById('cmdTabs');
  const categories = ['all', ...USEFUL_COMMANDS.map(g => g.group)];
  tabsEl.innerHTML = categories.map(c => `
    <button class="cmd-tab ${c === activeCmdCategory ? 'active' : ''}" data-cat="${escHtml(c)}">${c === 'all' ? 'Tất cả' : escHtml(c)}</button>
  `).join('');
}

function renderCmdRef() {
  const el = document.getElementById('cmdRefList');
  const groups = activeCmdCategory === 'all'
    ? USEFUL_COMMANDS
    : USEFUL_COMMANDS.filter(g => g.group === activeCmdCategory);
  const showTitles = activeCmdCategory === 'all';
  el.innerHTML = groups.map(g => `
    ${showTitles ? `<div class="cmd-group-title">${g.group}</div>` : ''}
    ${g.items.map(([cmd, desc]) => `
      <div class="cmd-item" data-cmd="${escHtml(cmd)}">
        <code>${escHtml(cmd)}</code><span class="desc">${desc}</span>
      </div>
    `).join('')}
  `).join('');
}

document.getElementById('cmdTabs').addEventListener('click', (e) => {
  const btn = e.target.closest('.cmd-tab');
  if (!btn) return;
  activeCmdCategory = btn.dataset.cat;
  renderCmdTabs();
  renderCmdRef();
});
document.getElementById('cmdRefList').addEventListener('click', (e) => {
  const item = e.target.closest('.cmd-item');
  if (!item) return;
  document.getElementById('consoleInput').value = item.dataset.cmd;
  document.getElementById('consoleInput').focus();
});
renderCmdTabs();
renderCmdRef();
loadPlayers();
setInterval(loadPlayers, 5000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/api/status":
            try:
                data = query_status(MC_HOST, MC_PORT)
            except Exception as e:
                data = {"online": False, "error": str(e)}
            try:
                data.update(get_system_stats())
            except Exception:
                pass
            try:
                data["player_ips"] = get_player_ips()
            except Exception:
                data["player_ips"] = {}
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/version":
            try:
                current = get_current_jar()
                latest = get_latest_stable()
                data = {"current": current, "latest": latest}
            except Exception as e:
                data = {"current": get_current_jar(), "error": str(e)}
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/players":
            try:
                data = {"players": get_all_players()}
            except Exception as e:
                data = {"players": [], "error": str(e)}
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/auth-status":
            self._send_json({"logged_in": self._is_authed()})
        elif self.path == "/" or self.path == "/index.html":
            body = DASHBOARD_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _session_token(self) -> str:
        raw = self.headers.get("Cookie", "")
        cookies = http.cookies.SimpleCookie()
        try:
            cookies.load(raw)
        except Exception:
            return ""
        morsel = cookies.get(SESSION_COOKIE_NAME)
        return morsel.value if morsel else ""

    def _is_authed(self) -> bool:
        return is_valid_session(self._session_token())

    def _send_json(self, data: dict, status: int = 200, set_cookie: str | None = None, clear_cookie: bool = False):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if set_cookie:
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE_NAME}={set_cookie}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age={SESSION_MAX_AGE}",
            )
        if clear_cookie:
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE_NAME}=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0",
            )
        self.end_headers()
        self.wfile.write(body)

    def _require_auth(self) -> bool:
        if self._is_authed():
            return True
        self._send_json({"success": False, "error": "Cần đăng nhập trước", "auth_required": True}, status=401)
        return False

    def do_POST(self):
        if self.path == "/api/login":
            payload = self._read_json_body()
            if is_rate_limited():
                self._send_json({"success": False, "error": "Nhập sai quá nhiều lần, thử lại sau vài phút."}, status=429)
                return
            username = payload.get("username", "")
            password = payload.get("password", "")
            if verify_login(username, password):
                token = create_session()
                self._send_json({"success": True}, set_cookie=token)
            else:
                record_failed_attempt()
                self._send_json({"success": False, "error": "Sai tài khoản hoặc mật khẩu"}, status=401)
        elif self.path == "/api/logout":
            destroy_session(self._session_token())
            self._send_json({"success": True}, clear_cookie=True)
        elif self.path == "/api/update":
            if not self._require_auth():
                return
            try:
                data = perform_update()
            except Exception as e:
                data = {"success": False, "error": str(e)}
            self._send_json(data)
        elif self.path == "/api/player-action":
            if not self._require_auth():
                return
            payload = self._read_json_body()
            try:
                data = apply_player_action(
                    payload.get("name", ""), payload.get("action", ""), payload.get("value", "")
                )
            except Exception as e:
                data = {"success": False, "error": str(e)}
            self._send_json(data)
        elif self.path == "/api/console":
            if not self._require_auth():
                return
            payload = self._read_json_body()
            try:
                data = run_console_command(
                    payload.get("command", ""), bool(payload.get("confirm", False))
                )
            except Exception as e:
                data = {"success": False, "error": str(e)}
            self._send_json(data)
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"Serving on http://{LISTEN_HOST}:{LISTEN_PORT}")
    server.serve_forever()
