#!/usr/bin/env python3
"""Discord bot for managing the Minecraft server.

Uses mc_lib.py for all the actual logic (query_status, apply_player_action,
run_console_command, perform_update, ...) — including the safety fixes
already baked in there (no `/op`/`/reload`, direct ops.json edits,
confirmation required for dangerous console commands). The web dashboard
this used to mirror has been retired; mc_lib.py is what's left of it.

Requires: pip install discord.py
Config via environment variables (set in the systemd unit, not hardcoded):
  DISCORD_BOT_TOKEN — bot token from the Discord Developer Portal
  DISCORD_ADMIN_ID  — Discord user ID allowed to run state-changing commands
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import discord
from discord import app_commands
from discord.ext import commands

from mc_lib import (
    DANGEROUS_COMMANDS,
    MC_HOST,
    MC_PORT,
    apply_player_action,
    get_all_players,
    get_current_jar,
    get_latest_stable,
    get_player_biomes,
    get_system_stats,
    known_player_names,
    perform_update,
    query_status,
    run_console_command,
    set_chat_bridge,
    set_ingame_nickname,
)
from map_snapshot import WORLDS, best_detail_snapshot, build_snapshot

BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
ADMIN_USER_ID = int(os.environ.get("DISCORD_ADMIN_ID", "0") or "0")
MAP_URL = "https://vntaikohub-map.novaseele.com/"
SERVER_ADDRESS = "vntaikohub.novaseele.com"
EXTRA_ADMINS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "extra_admins.json")
PLAYER_LINKS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "player_links.json")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!mc-unused-", intents=intents)


def _load_extra_admins() -> set[int]:
    try:
        with open(EXTRA_ADMINS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def _save_extra_admins() -> None:
    with open(EXTRA_ADMINS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(extra_admins), f)


extra_admins: set[int] = _load_extra_admins()


def is_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id == ADMIN_USER_ID


def is_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.id == ADMIN_USER_ID or interaction.user.id in extra_admins


def _load_player_links() -> dict[int, str]:
    try:
        with open(PLAYER_LINKS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {int(k): v for k, v in raw.items()}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_player_links() -> None:
    with open(PLAYER_LINKS_FILE, "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in player_links.items()}, f, ensure_ascii=False, indent=2)


player_links: dict[int, str] = _load_player_links()


def resolve_player(player: str | None, discord_user: discord.Member | None) -> tuple[str | None, str | None]:
    """Returns (name, error). discord_user takes priority when both given."""
    if discord_user is not None:
        name = player_links.get(discord_user.id)
        if not name:
            return None, f"{discord_user.mention} chưa `/link` tài khoản Minecraft."
        return name, None
    if player:
        return player, None
    return None, "Cần nhập `player` hoặc chọn `discord_user`."


def _format_delta(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "vài giây"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} phút"
    hours = minutes // 60
    minutes %= 60
    if hours < 24:
        return f"{hours} giờ {minutes} phút" if minutes else f"{hours} giờ"
    days = hours // 24
    hours %= 24
    return f"{days} ngày {hours} giờ" if hours else f"{days} ngày"


def _time_ago(iso_ts: str) -> str:
    dt = datetime.fromisoformat(iso_ts)
    seconds = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()
    return _format_delta(seconds)


START_TIME_MS = int(time.time() * 1000)


@bot.event
async def on_ready():
    # Rich presence with an elapsed timer, like a real game client shows.
    # DiscordSRV's own DiscordGameStatus config must stay off — both it and
    # this process share one bot token, and it'd overwrite this every
    # StatusUpdateRateInMinutes otherwise.
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.playing,
        name="Minecraft",
        timestamps={"start": START_TIME_MS},
    ))
    await bot.tree.sync()
    print(f"Logged in as {bot.user} (admin id: {ADMIN_USER_ID})")


@bot.tree.command(name="join", description="Xem địa chỉ để vào server Minecraft")
async def join_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"🎮 Địa chỉ server: `{SERVER_ADDRESS}`\n"
        f"Dán vào ô Server Address trong Minecraft (Java Edition) — không cần nhập thêm cổng."
    )


@bot.tree.command(name="help", description="Xem danh sách tất cả lệnh")
async def help_cmd(interaction: discord.Interaction):
    lines = sorted(f"`/{cmd.name}` — {cmd.description}" for cmd in bot.tree.get_commands())
    embed = discord.Embed(title="Danh sách lệnh", description="\n".join(lines), color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="opinfo", description="Xem OP level nào dùng được lệnh gì")
async def opinfo_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="OP level dùng được gì (trên server này)",
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="Level 0 — Người chơi thường",
        value="Không có quyền cheat.",
        inline=False,
    )
    embed.add_field(
        name="Level 1 — Moderator",
        value="Như level 0, cộng thêm bỏ qua spawn protection (đào/đặt block gần điểm spawn).",
        inline=False,
    )
    embed.add_field(
        name="Level 2 — GameMaster",
        value=(
            "Hầu hết lệnh cheat: `/gamemode`, `/give`, `/tp`, `/effect`, `/gamerule`, "
            "`/weather`, `/clear`, `/summon`, `/setblock`, `/fill`, `/kill`, `/locate`, "
            "`/enchant`, `/xp`... và WorldEdit (`//`)."
        ),
        inline=False,
    )
    embed.add_field(
        name="Level 3 — Admin",
        value=(
            "Như level 2, cộng thêm `/ban`, `/kick`, `/whitelist`, `/banlist`, `/pardon`, "
            "`/save-all`, `/save-off`/`/save-on`, `/setidletimeout`.\n"
            "Riêng server này: **command block cũng cần level 3** mới dùng được."
        ),
        inline=False,
    )
    embed.add_field(
        name="Level 4 — Owner",
        value=(
            "Như level 3, cộng thêm `/stop`, `/op`, `/deop`.\n"
            "**Chỉ `NovaSeele`/`novaseele` mới được cấp level 4.**"
        ),
        inline=False,
    )
    embed.add_field(
        name="Lưu ý",
        value=(
            "OP level đổi qua `/oplevel` trên Discord chỉ có hiệu lực sau khi người chơi "
            "**rejoin** — không áp dụng ngay cho phiên đang chơi."
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="status", description="Xem trạng thái server Minecraft")
async def status_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        status = query_status(MC_HOST, MC_PORT, timeout=3.0)
    except Exception as e:
        await interaction.followup.send(f"❌ Server offline hoặc không phản hồi: {e}")
        return
    try:
        stats = get_system_stats()
    except Exception as e:
        await interaction.followup.send(f"❌ Lỗi khi đọc tài nguyên hệ thống: {e}")
        return
    embed = discord.Embed(title="Trạng thái server", color=discord.Color.green())
    embed.add_field(name="Người chơi", value=f"{status['players_online']}/{status['players_max']}", inline=True)
    embed.add_field(name="Ping", value=f"{status['latency_ms']}ms", inline=True)
    embed.add_field(name="Phiên bản", value=status["version"], inline=True)
    embed.add_field(name="CPU", value=f"{stats['cpu_percent']}%", inline=True)
    embed.add_field(name="RAM", value=f"{stats['ram_used_mb']}/{stats['ram_total_mb']} MB", inline=True)
    embed.add_field(name="Disk", value=f"{stats['disk_used_gb']}/{stats['disk_total_gb']} GB", inline=True)
    if status.get("players_sample"):
        embed.add_field(name="Đang online", value=", ".join(status["players_sample"]), inline=False)
    await interaction.followup.send(embed=embed)


def _format_player_row(p: dict) -> str:
    name = p["name"][:15].ljust(16)
    gamemode = (p["gamemode"] or "?")[:9].ljust(10)
    op = (f"OP{p['op_level']}" if p["op_level"] else "").ljust(4)
    time_part = ""
    if p.get("last_seen"):
        ago = _time_ago(p["last_seen"])
        time_part = ago if p["online"] else f"{ago} trước"
    return f"{name}{gamemode}{op} {time_part}".rstrip()


def _format_player_section(title: str, players: list) -> str:
    rows = "\n".join(_format_player_row(p) for p in players)
    return f"**{title}**\n```\n{rows}\n```"


@bot.tree.command(name="players", description="Danh sách người chơi đã từng vào server")
async def players_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        players = get_all_players()
    except Exception as e:
        await interaction.followup.send(f"❌ Lỗi: {e}")
        return
    if not players:
        await interaction.followup.send("Chưa có người chơi nào.")
        return

    online = sorted((p for p in players if p["online"]), key=lambda p: p["name"].lower())
    offline = sorted((p for p in players if not p["online"]), key=lambda p: p.get("last_seen") or "", reverse=True)

    sections = []
    if online:
        sections.append(_format_player_section(f"🟢 Đang online ({len(online)})", online))
    if offline:
        sections.append(_format_player_section(f"⚫ Offline ({len(offline)})", offline))
    text = "\n\n".join(sections)
    if len(text) > 4000:
        text = text[:4000].rsplit("\n", 1)[0] + "\n```\n… (còn nữa)"

    embed = discord.Embed(title=f"Người chơi ({len(players)})", description=text, color=discord.Color.blue())
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="link", description="Liên kết Discord của bạn với tên nhân vật Minecraft")
@app_commands.describe(player="Tên nhân vật Minecraft (đúng hoa/thường, phải từng vào server)")
async def link_cmd(interaction: discord.Interaction, player: str):
    if player not in known_player_names():
        await interaction.response.send_message(
            f"❌ Không tìm thấy `{player}` (chưa từng vào server, hoặc gõ sai hoa/thường?).", ephemeral=True
        )
        return
    player_links[interaction.user.id] = player
    _save_player_links()
    await interaction.response.send_message(f"✅ Đã liên kết Discord của bạn với **{player}**.")


@bot.tree.command(name="discordname", description="Bật/tắt hiện tên Discord của bạn trong game (chat, đầu nhân vật, tab list)")
@app_commands.describe(enabled="Bật hay tắt")
async def discordname_cmd(interaction: discord.Interaction, enabled: bool):
    name = player_links.get(interaction.user.id)
    if not name:
        await interaction.response.send_message("❌ Bạn chưa `/link` tài khoản Minecraft.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        set_ingame_nickname(name, interaction.user.display_name if enabled else None)
    except Exception as e:
        await interaction.followup.send(f"❌ Lỗi: {e}")
        return
    state = "Bật" if enabled else "Tắt"
    await interaction.followup.send(f"✅ {state} hiện tên Discord trong game cho **{name}**.")


@bot.tree.command(name="biomes", description="Danh sách biome người chơi đã từng ghé qua")
@app_commands.describe(
    player="Tên người chơi (đúng hoa/thường)",
    discord_user="Hoặc chọn user Discord đã /link (thay vì gõ tên)",
)
async def biomes_cmd(interaction: discord.Interaction, player: str = None, discord_user: discord.Member = None):
    player, err = resolve_player(player, discord_user)
    if err:
        await interaction.response.send_message(f"❌ {err}", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        result = get_player_biomes(player)
    except Exception as e:
        await interaction.followup.send(f"❌ Lỗi: {e}")
        return
    if "error" in result:
        await interaction.followup.send(f"❌ {result['error']}")
        return
    biomes = result["biomes"]
    if not biomes:
        await interaction.followup.send(f"**{player}** chưa ghé qua biome nào (chưa đi đâu xa?).")
        return
    lines = [f"{b['biome']} — {b['first_visited'][:10]}" for b in biomes]
    text = "\n".join(lines)
    title = f"Biome {player} đã ghé qua ({len(biomes)})"
    if result["completed"]:
        title += " ✅ Adventuring Time hoàn thành"
    embed = discord.Embed(title=title, description=text[:4000], color=discord.Color.green())
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="gamemode", description="Đổi gamemode người chơi")
@app_commands.describe(
    mode="Gamemode mới",
    player="Tên người chơi (đang online)",
    discord_user="Hoặc chọn user Discord đã /link (thay vì gõ tên)",
)
@app_commands.choices(mode=[
    app_commands.Choice(name="survival", value="survival"),
    app_commands.Choice(name="creative", value="creative"),
    app_commands.Choice(name="adventure", value="adventure"),
    app_commands.Choice(name="spectator", value="spectator"),
])
async def gamemode_cmd(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
    player: str = None,
    discord_user: discord.Member = None,
):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Bạn không có quyền dùng lệnh này.", ephemeral=True)
        return
    player, err = resolve_player(player, discord_user)
    if err:
        await interaction.response.send_message(f"❌ {err}", ephemeral=True)
        return
    try:
        result = apply_player_action(player, "gamemode", mode.value)
    except Exception as e:
        await interaction.response.send_message(f"❌ Lỗi: {e}", ephemeral=True)
        return
    if result["success"]:
        await interaction.response.send_message(f"✅ Đã đổi gamemode của **{player}** thành `{mode.value}`.")
    else:
        await interaction.response.send_message(f"❌ {result.get('error')}", ephemeral=True)


@bot.tree.command(name="oplevel", description="Đổi OP level người chơi")
@app_commands.describe(
    level="OP level (0 = xoá OP)",
    player="Tên người chơi (đúng hoa/thường)",
    discord_user="Hoặc chọn user Discord đã /link (thay vì gõ tên)",
)
@app_commands.choices(level=[
    app_commands.Choice(name="0 (xoá OP)", value=0),
    app_commands.Choice(name="1", value=1),
    app_commands.Choice(name="2", value=2),
    app_commands.Choice(name="3", value=3),
])
async def oplevel_cmd(
    interaction: discord.Interaction,
    level: app_commands.Choice[int],
    player: str = None,
    discord_user: discord.Member = None,
):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Bạn không có quyền dùng lệnh này.", ephemeral=True)
        return
    player, err = resolve_player(player, discord_user)
    if err:
        await interaction.response.send_message(f"❌ {err}", ephemeral=True)
        return
    try:
        result = apply_player_action(player, "op_level", str(level.value))
    except Exception as e:
        await interaction.response.send_message(f"❌ Lỗi: {e}", ephemeral=True)
        return
    if result["success"]:
        applied = result.get("level", level.value)
        note = result.get("note", "")
        await interaction.response.send_message(f"✅ Đã đặt OP level của **{player}** = {applied}. {note}")
    else:
        await interaction.response.send_message(f"❌ {result.get('error')}", ephemeral=True)


class ConfirmDangerView(discord.ui.View):
    """Mirrors the web dashboard's confirmation dialog for stop/end/restart."""

    def __init__(self, command: str):
        super().__init__(timeout=30)
        self.command = command

    @discord.ui.button(label="Xác nhận", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            result = run_console_command(self.command, confirmed=True)
        except Exception as e:
            await interaction.response.edit_message(content=f"❌ Lỗi khi chạy `{self.command}`: {e}", view=None)
            self.stop()
            return
        output = result.get("output") or []
        text = "\n".join(output[-10:]) or "(không có output)"
        await interaction.response.edit_message(content=f"✅ Đã chạy `{self.command}`:\n```\n{text[:1800]}\n```", view=None)
        self.stop()

    @discord.ui.button(label="Huỷ", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Đã huỷ.", view=None)
        self.stop()


@bot.tree.command(name="chatbridge", description="Bật/tắt chat 2 chiều Discord <-> Minecraft (DiscordSRV)")
@app_commands.describe(direction="Chiều muốn đổi", enabled="Bật hay tắt")
@app_commands.choices(direction=[
    app_commands.Choice(name="Minecraft → Discord", value="mc_to_discord"),
    app_commands.Choice(name="Discord → Minecraft", value="discord_to_mc"),
    app_commands.Choice(name="Cả 2 chiều", value="both"),
])
async def chatbridge_cmd(interaction: discord.Interaction, direction: app_commands.Choice[str], enabled: bool):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Bạn không có quyền dùng lệnh này.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        result = set_chat_bridge(direction.value, enabled)
    except Exception as e:
        await interaction.followup.send(f"❌ Lỗi: {e}")
        return
    if result["success"]:
        state = "Bật" if enabled else "Tắt"
        await interaction.followup.send(f"✅ {state} chat {direction.name}.")
    else:
        await interaction.followup.send(f"❌ {result.get('error')}")


@bot.tree.command(name="console", description="Chạy lệnh console Minecraft")
@app_commands.describe(command="Lệnh console (không kèm dấu /), ví dụ: say hello")
async def console_cmd(interaction: discord.Interaction, command: str):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Bạn không có quyền dùng lệnh này.", ephemeral=True)
        return
    try:
        result = run_console_command(command, confirmed=False)
    except Exception as e:
        await interaction.response.send_message(f"❌ Lỗi: {e}", ephemeral=True)
        return
    if result.get("needs_confirm"):
        view = ConfirmDangerView(command)
        await interaction.response.send_message(f"⚠️ {result['error']}", view=view, ephemeral=True)
        return
    output = result.get("output") or []
    text = "\n".join(output[-15:]) or "(không có output)"
    await interaction.response.send_message(f"```\n{text[:1900]}\n```")


@bot.tree.command(name="version", description="Kiểm tra phiên bản Paper hiện tại/mới nhất")
async def version_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    current = get_current_jar()
    try:
        latest = get_latest_stable()
    except Exception as e:
        await interaction.followup.send(f"❌ Không kiểm tra được bản mới: {e}")
        return
    is_latest = bool(current and current.get("jar_name") == latest["jar_name"])
    embed = discord.Embed(
        title="Phiên bản Paper",
        color=discord.Color.green() if is_latest else discord.Color.orange(),
    )
    embed.add_field(name="Đang chạy", value=current["jar_name"] if current else "?", inline=False)
    embed.add_field(name="Mới nhất", value=latest["jar_name"], inline=False)
    embed.add_field(name="Trạng thái", value="✅ Đã mới nhất" if is_latest else "🔶 Có bản mới", inline=False)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="update", description="Cập nhật Paper lên bản mới nhất, tự restart server")
async def update_cmd(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Bạn không có quyền dùng lệnh này.", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        result = perform_update()
    except Exception as e:
        await interaction.followup.send(f"❌ Lỗi khi cập nhật: {e}")
        return
    if result.get("success"):
        jar_name = (result.get("to") or {}).get("jar_name", "?")
        await interaction.followup.send(f"✅ Đã cập nhật lên `{jar_name}` và khởi động lại server.")
    else:
        await interaction.followup.send(f"❌ {result.get('error')}")


@bot.tree.command(name="grantadmin", description="Cấp quyền dùng lệnh admin cho 1 user Discord")
@app_commands.describe(user="User cần cấp quyền (ping)")
async def grantadmin_cmd(interaction: discord.Interaction, user: discord.Member):
    if not is_owner(interaction):
        await interaction.response.send_message("❌ Chỉ NovaSeele mới dùng được lệnh này.", ephemeral=True)
        return
    if user.id == ADMIN_USER_ID:
        await interaction.response.send_message("ℹ️ User này đã là admin gốc.", ephemeral=True)
        return
    if user.id in extra_admins:
        await interaction.response.send_message(f"ℹ️ {user.mention} đã có quyền admin rồi.", ephemeral=True)
        return
    extra_admins.add(user.id)
    _save_extra_admins()
    await interaction.response.send_message(f"✅ Đã cấp quyền admin cho {user.mention}.")


@bot.tree.command(name="revokeadmin", description="Thu hồi quyền dùng lệnh admin của 1 user Discord")
@app_commands.describe(user="User cần thu hồi quyền (ping)")
async def revokeadmin_cmd(interaction: discord.Interaction, user: discord.Member):
    if not is_owner(interaction):
        await interaction.response.send_message("❌ Chỉ NovaSeele mới dùng được lệnh này.", ephemeral=True)
        return
    if user.id not in extra_admins:
        await interaction.response.send_message(f"ℹ️ {user.mention} không có trong danh sách được cấp quyền.", ephemeral=True)
        return
    extra_admins.discard(user.id)
    _save_extra_admins()
    await interaction.response.send_message(f"✅ Đã thu hồi quyền admin của {user.mention}.")


@bot.tree.command(name="admins", description="Xem danh sách ai đang có quyền admin")
async def admins_cmd(interaction: discord.Interaction):
    lines = [f"👑 <@{ADMIN_USER_ID}> — owner (cố định)"]
    for uid in sorted(extra_admins):
        lines.append(f"🛡️ <@{uid}> — được cấp qua /grantadmin")
    embed = discord.Embed(title="Danh sách admin", description="\n".join(lines), color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="map", description="Xem bản đồ Overworld/Nether/The End (ảnh + link xem trực tiếp)")
@app_commands.describe(world="Chỉ xem 1 map, ảnh chi tiết hơn (bỏ trống = xem cả 3, ảnh tổng quan)")
@app_commands.choices(world=[
    app_commands.Choice(name=label, value=key) for key, label in WORLDS
])
async def map_cmd(interaction: discord.Interaction, world: app_commands.Choice[str] = None):
    await interaction.response.defer()

    if world is not None:
        try:
            img_path, zoom = best_detail_snapshot(world.value)
        except Exception as e:
            await interaction.followup.send(f"❌ Không ghép được ảnh {world.name}: {e}")
            return
        await interaction.followup.send(
            content=f"🗺️ {world.name} (zoom {zoom}) — Live Map: {MAP_URL}",
            file=discord.File(img_path, filename=f"{world.value}.png"),
        )
        return

    files = []
    missing = []
    for w, label in WORLDS:
        try:
            img_path = build_snapshot(w)
        except Exception:
            missing.append(label)
            continue
        files.append(discord.File(img_path, filename=f"{w}.png"))

    content = (
        f"🗺️ Live Map: {MAP_URL}\n"
        f"-# Nếu mở web thấy thiếu/cũ, thử tab ẩn danh — trình duyệt hay giữ cache ảnh bản đồ cũ dù đã bấm Ctrl+F5."
    )
    if missing:
        content += f"\n-# Chưa có ảnh: {', '.join(missing)} (world chưa render lần nào)."
    if not files:
        await interaction.followup.send(content)
        return
    await interaction.followup.send(content=content, files=files)


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("Thiếu DISCORD_BOT_TOKEN")
    if not ADMIN_USER_ID:
        raise SystemExit("Thiếu DISCORD_ADMIN_ID")
    bot.run(BOT_TOKEN)
