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
    perform_update,
    query_status,
    run_console_command,
)
from map_snapshot import WORLDS, build_snapshot

BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
ADMIN_USER_ID = int(os.environ.get("DISCORD_ADMIN_ID", "0") or "0")
MAP_URL = "https://vntaikohub-map.novaseele.com/"
SERVER_ADDRESS = "vntaikohub.novaseele.com"
EXTRA_ADMINS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "extra_admins.json")

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


@bot.event
async def on_ready():
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
    lines = [
        f"{'🟢' if p['online'] else '⚫'} **{p['name']}** — {p['gamemode'] or '?'}, OP level {p['op_level']}"
        for p in players
    ]
    text = "\n".join(lines)
    embed = discord.Embed(title=f"Người chơi ({len(players)})", description=text[:4000], color=discord.Color.blue())
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="biomes", description="Danh sách biome người chơi đã từng ghé qua")
@app_commands.describe(player="Tên người chơi (đúng hoa/thường)")
async def biomes_cmd(interaction: discord.Interaction, player: str):
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
@app_commands.describe(player="Tên người chơi (đang online)", mode="Gamemode mới")
@app_commands.choices(mode=[
    app_commands.Choice(name="survival", value="survival"),
    app_commands.Choice(name="creative", value="creative"),
    app_commands.Choice(name="adventure", value="adventure"),
    app_commands.Choice(name="spectator", value="spectator"),
])
async def gamemode_cmd(interaction: discord.Interaction, player: str, mode: app_commands.Choice[str]):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Bạn không có quyền dùng lệnh này.", ephemeral=True)
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
@app_commands.describe(player="Tên người chơi (đúng hoa/thường)", level="OP level (0 = xoá OP, 1-4)")
async def oplevel_cmd(interaction: discord.Interaction, player: str, level: app_commands.Range[int, 0, 4]):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Bạn không có quyền dùng lệnh này.", ephemeral=True)
        return
    try:
        result = apply_player_action(player, "op_level", str(level))
    except Exception as e:
        await interaction.response.send_message(f"❌ Lỗi: {e}", ephemeral=True)
        return
    if result["success"]:
        note = result.get("note", "")
        await interaction.response.send_message(f"✅ Đã đặt OP level của **{player}** = {level}. {note}")
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


@bot.tree.command(name="map", description="Xem bản đồ Overworld/Nether/The End (ảnh + link xem trực tiếp)")
async def map_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    files = []
    missing = []
    for world, label in WORLDS:
        try:
            img_path = build_snapshot(world)
        except Exception:
            missing.append(label)
            continue
        files.append(discord.File(img_path, filename=f"{world}.png"))

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
