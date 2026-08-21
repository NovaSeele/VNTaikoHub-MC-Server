#!/usr/bin/env python3
"""Discord bot mirroring the web dashboard's functionality.

Reuses the exact same helper functions as app.py (query_status,
apply_player_action, run_console_command, perform_update, ...) so behaviour
stays identical between the web dashboard and the bot — including the
safety fixes already made there (no `/op`/`/reload`, direct ops.json edits,
confirmation required for dangerous console commands).

Requires: pip install discord.py
Config via environment variables (set in the systemd unit, not hardcoded):
  DISCORD_BOT_TOKEN — bot token from the Discord Developer Portal
  DISCORD_ADMIN_ID  — Discord user ID allowed to run state-changing commands
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import discord
from discord import app_commands
from discord.ext import commands

from app import (
    DANGEROUS_COMMANDS,
    MC_HOST,
    MC_PORT,
    apply_player_action,
    get_all_players,
    get_current_jar,
    get_latest_stable,
    get_system_stats,
    perform_update,
    query_status,
    run_console_command,
)
from map_render import OUTPUT_PATH as MAP_PATH
from map_render import get_cached_or_render

BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
ADMIN_USER_ID = int(os.environ.get("DISCORD_ADMIN_ID", "0") or "0")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!mc-unused-", intents=intents)


def is_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.id == ADMIN_USER_ID


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user} (admin id: {ADMIN_USER_ID})")


@bot.tree.command(name="status", description="Xem trạng thái server Minecraft")
async def status_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        status = query_status(MC_HOST, MC_PORT, timeout=3.0)
    except Exception as e:
        await interaction.followup.send(f"❌ Server offline hoặc không phản hồi: {e}")
        return
    stats = get_system_stats()
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
    players = get_all_players()
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


@bot.tree.command(name="gamemode", description="Đổi gamemode người chơi (chỉ admin)")
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
    result = apply_player_action(player, "gamemode", mode.value)
    if result["success"]:
        await interaction.response.send_message(f"✅ Đã đổi gamemode của **{player}** thành `{mode.value}`.")
    else:
        await interaction.response.send_message(f"❌ {result.get('error')}", ephemeral=True)


@bot.tree.command(name="oplevel", description="Đổi OP level người chơi (chỉ admin)")
@app_commands.describe(player="Tên người chơi (đúng hoa/thường)", level="OP level (0 = xoá OP, 1-4)")
async def oplevel_cmd(interaction: discord.Interaction, player: str, level: app_commands.Range[int, 0, 4]):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Bạn không có quyền dùng lệnh này.", ephemeral=True)
        return
    result = apply_player_action(player, "op_level", str(level))
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
        result = run_console_command(self.command, confirmed=True)
        output = result.get("output") or []
        text = "\n".join(output[-10:]) or "(không có output)"
        await interaction.response.edit_message(content=f"✅ Đã chạy `{self.command}`:\n```\n{text[:1800]}\n```", view=None)
        self.stop()

    @discord.ui.button(label="Huỷ", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Đã huỷ.", view=None)
        self.stop()


@bot.tree.command(name="console", description="Chạy lệnh console Minecraft (chỉ admin)")
@app_commands.describe(command="Lệnh console (không kèm dấu /), ví dụ: say hello")
async def console_cmd(interaction: discord.Interaction, command: str):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Bạn không có quyền dùng lệnh này.", ephemeral=True)
        return
    result = run_console_command(command, confirmed=False)
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


@bot.tree.command(name="update", description="Cập nhật Paper lên bản mới nhất, tự restart server (chỉ admin)")
async def update_cmd(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Bạn không có quyền dùng lệnh này.", ephemeral=True)
        return
    await interaction.response.defer()
    result = perform_update()
    if result.get("success"):
        jar_name = (result.get("to") or {}).get("jar_name", "?")
        await interaction.followup.send(f"✅ Đã cập nhật lên `{jar_name}` và khởi động lại server.")
    else:
        await interaction.followup.send(f"❌ {result.get('error')}")


_map_render_lock = asyncio.Lock()


@bot.tree.command(name="map", description="Xem bản đồ tổng quan Overworld (ảnh, cập nhật định kỳ)")
@app_commands.describe(refresh="Render lại bản đồ mới nhất — mất khoảng 30-45 phút")
async def map_cmd(interaction: discord.Interaction, refresh: bool = False):
    if refresh:
        if _map_render_lock.locked():
            await interaction.response.send_message("⏳ Đang có 1 lượt render khác chạy rồi, đợi xong rồi thử lại.", ephemeral=True)
            return
        await interaction.response.send_message("🗺️ Đang render lại bản đồ, mất khoảng 30-45 phút — dùng `/map` lại sau để xem bản mới.")

        async def _render_locked():
            async with _map_render_lock:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, get_cached_or_render, True)

        asyncio.create_task(_render_locked())
        return

    await interaction.response.defer()
    if not os.path.exists(MAP_PATH):
        await interaction.followup.send("Chưa có bản đồ nào được render. Dùng `/map refresh:true` để tạo lần đầu.")
        return
    updated_str = time.strftime("%d/%m/%Y %H:%M", time.localtime(os.path.getmtime(MAP_PATH)))
    await interaction.followup.send(
        content=f"🗺️ Bản đồ Overworld (cập nhật lúc {updated_str}):",
        file=discord.File(MAP_PATH),
    )


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("Thiếu DISCORD_BOT_TOKEN")
    if not ADMIN_USER_ID:
        raise SystemExit("Thiếu DISCORD_ADMIN_ID")
    bot.run(BOT_TOKEN)
