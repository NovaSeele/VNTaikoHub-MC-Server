#!/usr/bin/env python3
"""Stitch squaremap's own pre-rendered tiles into one PNG for Discord.

squaremap already renders detailed, vanilla-accurate tiles for its web
viewer — this just reads the existing tile files at the lowest zoom level
(fewest tiles, full-world overview) and composites them into a single
image, instead of re-rendering anything from the world save. Runs on
demand, fast (I/O + paste only, no chunk parsing).

Requires: pip install Pillow
"""
import os

from PIL import Image

TILES_ROOT = "/home/minecraft/plugins/squaremap/web/tiles"
TILE_SIZE = 512
SNAPSHOT_PATH = "/tmp/mc-map-snapshot.png"


def build_snapshot(world: str = "minecraft_overworld", zoom: int = 0) -> str:
    tiles_dir = os.path.join(TILES_ROOT, world, str(zoom))
    if not os.path.isdir(tiles_dir):
        raise RuntimeError(f"Không tìm thấy tile cho {world} ở zoom {zoom} — world chưa render lần nào?")

    coords = []
    for fname in os.listdir(tiles_dir):
        if not fname.endswith(".png"):
            continue
        x_str, z_str = fname[:-4].split("_")
        coords.append((int(x_str), int(z_str), fname))
    if not coords:
        raise RuntimeError(f"Không có tile nào trong {tiles_dir}")

    min_x = min(c[0] for c in coords)
    max_x = max(c[0] for c in coords)
    min_z = min(c[1] for c in coords)
    max_z = max(c[1] for c in coords)

    width = (max_x - min_x + 1) * TILE_SIZE
    height = (max_z - min_z + 1) * TILE_SIZE
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    for x, z, fname in coords:
        with Image.open(os.path.join(tiles_dir, fname)) as tile:
            canvas.paste(tile, ((x - min_x) * TILE_SIZE, (z - min_z) * TILE_SIZE))

    canvas.convert("RGB").save(SNAPSHOT_PATH)
    return SNAPSHOT_PATH


if __name__ == "__main__":
    print(build_snapshot())
