#!/usr/bin/env python3
"""Render a top-down overview map of the Overworld from the world save.

One pixel per chunk (16x16 blocks), sampled at a representative point in
each chunk, colored by the top non-air block using an approximate
Minecraft-map-style palette. This is a lightweight overview, not a full
per-block detailed map (that's Dynmap/BlueMap territory, and much heavier
to run continuously on a shared VPS) — rendered on demand and cached.

Requires: pip install anvil-parser2 Pillow
"""
import glob
import os
import re
import time

import anvil
from PIL import Image

WORLD_REGION_DIR = "/home/minecraft/world/dimensions/minecraft/overworld/region"
OUTPUT_PATH = "/opt/mc-dashboard/map/overworld_map.png"
CACHE_MAX_AGE = 3600  # giây — không render lại nếu bản cache còn mới hơn 1h
CHUNKS_PER_REGION = 32

# Bảng màu xấp xỉ theo loại khối trên cùng (đơn giản hoá, không đầy đủ hết
# mọi block trong game — đủ để phác hoạ hình dạng địa hình/biome tổng quan).
BLOCK_COLORS = {
    "grass_block": (127, 178, 56),
    "sand": (247, 233, 163),
    "sandstone": (216, 195, 138),
    "water": (64, 111, 191),
    "stone": (127, 127, 127),
    "deepslate": (77, 77, 77),
    "dirt": (150, 108, 74),
    "snow": (255, 255, 255),
    "snow_block": (255, 255, 255),
    "ice": (160, 188, 255),
    "gravel": (136, 126, 126),
    "podzol": (105, 76, 43),
    "mycelium": (111, 99, 105),
    "terracotta": (152, 94, 67),
    "netherrack": (110, 53, 51),
    "end_stone": (219, 219, 171),
    "oak_planks": (162, 130, 78),
    "cobblestone": (117, 117, 117),
    "bedrock": (10, 10, 10),
    "lava": (207, 92, 20),
    "obsidian": (20, 18, 29),
}
DEFAULT_COLOR = (150, 150, 150)


def block_color(block_id: str) -> tuple:
    name = block_id.split(":")[-1] if ":" in block_id else block_id
    if name in BLOCK_COLORS:
        return BLOCK_COLORS[name]
    if "leaves" in name:
        return (60, 120, 45)
    if "log" in name or "wood" in name:
        return (100, 75, 45)
    if "wool" in name or "concrete" in name:
        return (180, 150, 130)
    if "ore" in name:
        return (140, 140, 140)
    return DEFAULT_COLOR


def top_block_color(chunk, local_x: int, local_z: int) -> tuple:
    for y in range(319, -65, -1):
        try:
            block = chunk.get_block(local_x, y, local_z)
        except Exception:
            continue
        if block is None or block.id in ("air", "cave_air", "void_air"):
            continue
        return block_color(block.id)
    return (30, 30, 30)


def render_map() -> str:
    region_files = glob.glob(os.path.join(WORLD_REGION_DIR, "r.*.*.mca"))
    if not region_files:
        raise RuntimeError("Không tìm thấy file region nào")

    coords = []
    for path in region_files:
        m = re.search(r"r\.(-?\d+)\.(-?\d+)\.mca$", os.path.basename(path))
        if m:
            coords.append((int(m.group(1)), int(m.group(2)), path))

    min_rx = min(c[0] for c in coords)
    max_rx = max(c[0] for c in coords)
    min_rz = min(c[1] for c in coords)
    max_rz = max(c[1] for c in coords)

    width = (max_rx - min_rx + 1) * CHUNKS_PER_REGION
    height = (max_rz - min_rz + 1) * CHUNKS_PER_REGION

    img = Image.new("RGB", (width, height), (20, 20, 30))
    pixels = img.load()

    for rx, rz, path in coords:
        try:
            region = anvil.Region.from_file(path)
        except Exception:
            continue
        for cx in range(CHUNKS_PER_REGION):
            for cz in range(CHUNKS_PER_REGION):
                try:
                    chunk = region.get_chunk(cx, cz)
                except Exception:
                    continue
                if chunk is None:
                    continue
                color = top_block_color(chunk, 7, 7)
                px = (rx - min_rx) * CHUNKS_PER_REGION + cx
                py = (rz - min_rz) * CHUNKS_PER_REGION + cz
                pixels[px, py] = color

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    img.save(OUTPUT_PATH)
    return OUTPUT_PATH


def get_cached_or_render(force: bool = False) -> str:
    if not force and os.path.exists(OUTPUT_PATH):
        age = time.time() - os.path.getmtime(OUTPUT_PATH)
        if age < CACHE_MAX_AGE:
            return OUTPUT_PATH
    return render_map()


if __name__ == "__main__":
    start = time.time()
    out = render_map()
    print(f"Rendered to {out} in {time.time() - start:.1f}s")
