#!/usr/bin/env python3
"""Shared Minecraft server logic - stdlib only.

Extracted from what used to be the web dashboard (app.py) after the web UI
was retired in favor of the Discord bot. This module has no server/UI of
its own — it's imported by discord_bot.py for all the actual work (status
queries, player management, console access, version updates)."""
import calendar
import glob
import gzip
import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import time
import urllib.request
import uuid as uuid_module
from datetime import datetime

MC_HOST = "127.0.0.1"
MC_PORT = 8443
MC_DIR = "/home/minecraft"
LOG_PATH = f"{MC_DIR}/logs/latest.log"
GAMEMODE_NAMES = {0: "survival", 1: "creative", 2: "adventure", 3: "spectator"}
RUN_SH = f"{MC_DIR}/run.sh"
FILL_API = "https://fill.papermc.io/v3/projects/paper"
DISCORDSRV_CONFIG = f"{MC_DIR}/plugins/DiscordSRV/config.yml"
CHAT_BRIDGE_KEYS = {
    "mc_to_discord": ["DiscordChatChannelMinecraftToDiscord"],
    "discord_to_mc": ["DiscordChatChannelDiscordToMinecraft"],
    "both": ["DiscordChatChannelMinecraftToDiscord", "DiscordChatChannelDiscordToMinecraft"],
}


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


def _last_seen_from_expiry(expires_on: str | None) -> str | None:
    """usercache.json's expiresOn is refreshed to "now + 1 month" every time
    a player connects (Mojang profile cache behavior) — subtracting a month
    back out gives their last-join timestamp for free, no extra state file
    or log parsing needed."""
    if not expires_on:
        return None
    try:
        dt = datetime.strptime(expires_on, "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return None
    year, month = dt.year, dt.month - 1
    if month == 0:
        month, year = 12, year - 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day).isoformat()


def known_player_names() -> set:
    try:
        with open(f"{MC_DIR}/usercache.json") as f:
            cache = json.load(f)
    except Exception:
        return set()
    return {e["name"] for e in cache if e.get("name")}


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
            "online": name in online_names,
            "last_seen": _last_seen_from_expiry(entry.get("expiresOn")),
        })
    result.sort(key=lambda p: p["name"].lower())
    return result


def get_player_biomes(name: str) -> dict:
    """Danh sách biome người chơi đã từng ghé qua. Lấy thẳng từ dữ liệu
    advancement "Adventuring Time" mà chính Minecraft đã tự track sẵn cho
    mỗi người chơi (mỗi biome hoàn thành 1 lần đầu tiên có timestamp riêng)
    — không cần tự cài đặt cơ chế theo dõi nào thêm."""
    try:
        with open(f"{MC_DIR}/usercache.json") as f:
            cache = json.load(f)
    except Exception:
        cache = []
    pid = None
    for entry in cache:
        if entry.get("name") == name:
            pid = entry.get("uuid")
            break
    if not pid:
        return {"error": f"Không tìm thấy người chơi '{name}'"}

    adv_path = f"{MC_DIR}/world/players/advancements/{pid}.json"
    if not os.path.exists(adv_path):
        return {"error": f"{name} chưa có dữ liệu advancement (chưa từng vào server?)"}

    try:
        with open(adv_path) as f:
            adv = json.load(f)
    except Exception as e:
        return {"error": f"Không đọc được file advancement: {e}"}

    entry = adv.get("minecraft:adventure/adventuring_time", {})
    criteria = entry.get("criteria", {})
    biomes = [
        {"biome": key.split(":", 1)[-1], "first_visited": ts}
        for key, ts in criteria.items()
    ]
    biomes.sort(key=lambda b: b["first_visited"])
    return {"biomes": biomes, "completed": bool(entry.get("done", False))}


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


def set_ingame_nickname(name: str, nickname: str | None) -> None:
    """Uses SimpleNicks (console-only, no permission plugin needed — console
    bypasses its require-permission checks). Strips MiniMessage tag
    delimiters from the input since it comes straight from a Discord
    display name the plugin will otherwise try to parse as formatting."""
    if nickname:
        safe = nickname.replace("<", "‹").replace(">", "›")[:32]
        send_console_command(f"nick admin set {name} {safe}")
    else:
        send_console_command(f"nick admin reset {name}")


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


def set_chat_bridge(direction: str, enabled: bool) -> dict:
    """Flips DiscordSRV's chat relay direction flag(s) directly in its
    config.yml, then `discordsrv reload` — confirmed live (checked per
    message, unlike BotToken which is only read at plugin startup)."""
    keys = CHAT_BRIDGE_KEYS.get(direction)
    if not keys:
        return {"success": False, "error": "direction không hợp lệ"}
    try:
        with open(DISCORDSRV_CONFIG) as f:
            lines = f.readlines()
    except Exception as e:
        return {"success": False, "error": f"không đọc được config DiscordSRV: {e}"}

    value = "true" if enabled else "false"
    remaining = set(keys)
    for i, line in enumerate(lines):
        for key in list(remaining):
            if line.startswith(f"{key}:"):
                lines[i] = f"{key}: {value}\n"
                remaining.discard(key)
    if remaining:
        return {"success": False, "error": f"không tìm thấy key trong config: {', '.join(remaining)}"}

    with open(DISCORDSRV_CONFIG, "w") as f:
        f.writelines(lines)
    send_console_command("discordsrv reload")
    return {"success": True}


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


USER_AGENT = "Mozilla/5.0 (compatible; VNTaikoHub-MC-bot)"


def _urlopen(url: str, timeout: float):
    # PaperMC's download CDN (fill-data.papermc.io) returns 403 for
    # urllib's default "Python-urllib/x.y" User-Agent — confirmed by
    # testing the exact same URL with/without one. The metadata API
    # (fill.papermc.io) happens to not care, but set it everywhere for
    # consistency in case that changes too.
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout)


def get_latest_stable() -> dict:
    with _urlopen(f"{FILL_API}", 10) as resp:
        project = json.load(resp)
    # Keys are ordered newest-group-first by the API.
    for group, versions in project["versions"].items():
        # Prefer a plain release name in the group (skip "-rc-"/"-pre" entries).
        stable_versions = [v for v in versions if "-" not in v] or versions
        mc_version = stable_versions[0]
        with _urlopen(f"{FILL_API}/versions/{mc_version}/builds", 10) as resp:
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
    with _urlopen(latest["url"], 60) as resp, open(tmp_path, "wb") as f:
        shutil.copyfileobj(resp, f)

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
