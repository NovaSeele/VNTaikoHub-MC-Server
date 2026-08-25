# VNTaikoHub MC Server

Hạ tầng server Paper Minecraft, quản trị qua bot Discord. Chạy trên VPS
không có UDP/IPv6, port 25565 bị chặn nên game port nguỵ trang qua port 443.

## Kiến trúc

```
Client Minecraft
      │  raw TCP, không có TLS ClientHello
      ▼
nginx :443  (stream + ssl_preread — tách HTTPS thật khỏi Minecraft thô)
      │  proxy_protocol on
      ▼
relay.py :25567   (bóc PROXY protocol header, ghi lại IP thật)
      │
      ▼
Paper server :8443   (online-mode=false)
```

`discord_bot.py` truy vấn server qua Server List Ping tự viết, đọc/ghi
`ops.json`/`usercache.json` trực tiếp, gửi lệnh console qua `screen`. Toàn
bộ logic nằm ở `mc_lib.py`; bot chỉ là lớp slash command bên trên.

## Cấu trúc

- `dashboard/mc_lib.py` — logic lõi (stdlib only): truy vấn trạng thái, đọc
  ghi `ops.json`, quản lý người chơi, console, đọc NBT, kiểm tra/cập nhật
  phiên bản Paper. Thư viện thuần, không có entrypoint.
- `dashboard/relay.py` — relay PROXY-protocol giữa nginx và Paper.
- `dashboard/discord_bot.py` — slash command, import trực tiếp `mc_lib.py`.
  Lệnh xem (`/status`, `/players`, `/version`, `/map`, `/biomes`) public;
  lệnh thay đổi (`/gamemode`, `/oplevel`, `/console`, `/update`) check 1
  Discord user ID (`DISCORD_ADMIN_ID`) tại runtime. Cần `discord.py`.
- `dashboard/map_snapshot.py` — ghép tile có sẵn của squaremap thành 1 PNG
  cho Discord. Cần `Pillow`.
- `backup/world_backup.sh` — nén + upload `world/` lên Google Drive.
- `systemd/*.service`, `systemd/*.timer` — `minecraft`, `mc-proxy-relay`,
  `mc-discord-bot`, `mc-world-backup` (+ timer), `htpdate-sync` (+ timer).
- `nginx/stream-mc.conf` — block `stream {}` cho `nginx.conf` (top-level).
- `nginx/vntaikohub-map.conf` — vhost cho web viewer squaremap.
- `scripts/run.sh` — script khởi động Paper (Aikar's flags).

## Cài đặt

1. Cài Paper build mới nhất từ `fill.papermc.io` vào `/home/minecraft/`.
2. Copy `scripts/run.sh` vào `/home/minecraft/run.sh`, sửa tên jar cho khớp.
3. Copy `dashboard/mc_lib.py`, `relay.py`, `discord_bot.py`, `map_snapshot.py`
   vào `/opt/mc-dashboard/`; `backup/world_backup.sh` vào
   `/opt/mc-dashboard/backup/`.
4. `apt-get install libnginx-mod-stream`, dán `nginx/stream-mc.conf` vào
   `nginx.conf` (top-level). Đổi `listen 443` của vhost khác thành
   `listen 127.0.0.1:18443 proxy_protocol;`.
5. Copy `systemd/*` vào `/etc/systemd/system/`, rồi
   `systemctl daemon-reload && systemctl enable --now minecraft mc-proxy-relay
   mc-world-backup.timer htpdate-sync.timer`.
6. Cài `rclone`, cấu hình remote `gdrive:` (xem mục Backup).
7. `pip install discord.py Pillow`, tạo `/etc/mc-discord-bot.env` với
   `DISCORD_BOT_TOKEN=...` và `DISCORD_ADMIN_ID=...`, rồi
   `systemctl enable --now mc-discord-bot`.
8. Tuỳ chọn: bản đồ live — xem mục squaremap.

## Backup (Google Drive)

- `backup/world_backup.sh` — `save-off`/`save-all flush`, tar+gzip `world/`,
  `rclone copy` lên Drive, giữ 7 ngày. `save-on` luôn chạy khi thoát (trap),
  kể cả khi lỗi.
- `systemd/mc-world-backup.timer` — 05:00 hàng ngày.
- Remote `gdrive:` dùng OAuth client_id riêng (không dùng client_id dùng
  chung của rclone — Google sắp khai tử) với scope `drive.file` — chỉ truy
  cập file do chính app tạo. Credentials không có trong repo, xem
  `secrets.txt` local, hoặc setup theo
  https://rclone.org/drive/#making-your-own-client-id
  (`--drive-scope=drive.file`, publish OAuth consent screen để tránh token
  hết hạn sau 7 ngày).

## Bản đồ live (squaremap)

- Tải `squaremap-paper-mc<version>-<ver>.jar` đúng bản Minecraft đang chạy
  từ https://github.com/jpenilla/squaremap/releases.
- Bỏ vào `/home/minecraft/plugins/`, restart `minecraft.service`.
- Trong `plugins/squaremap/config.yml`, đặt `internal-webserver.bind` =
  `127.0.0.1` (mặc định `0.0.0.0` lộ port trực tiếp, bỏ qua nginx/Cloudflare).
  Áp dụng bằng `/squaremap reload`, không cần restart.
- `nginx/vntaikohub-map.conf` proxy `127.0.0.1:8080` ra
  `https://vntaikohub-map.novaseele.com`. Đè `Cache-Control:
  max-age=14400` của squaremap trên `/tiles/` thành `no-cache,
  must-revalidate` — nếu không bản đồ live sẽ như bị đứng hình. Cloudflare
  tự cache PNG bất kể header gốc — cần thêm Cache Rule bypass cho
  `https://vntaikohub-map.novaseele.com/*`.
- Render lần đầu: `/squaremap fullrender minecraft:overworld` trong
  console. Render 1 phần: `/squaremap radiusrender minecraft:overworld
  <bán_kính> <x> <z>` (cách nhau bằng dấu cách, không phải dấu phẩy).
- Tự cập nhật qua block event sau lần render đầu. Mỗi mức zoom (0-3) render
  độc lập, có thể lệch pha tạm thời sau thay đổi lớn/nhanh (vd elytra bay
  vào vùng mới) — tự bắt kịp, hoặc ép bằng `radiusrender`/`fullrender`.
- `/map` trên Discord gửi kèm ảnh preview + link. `map_snapshot.py` ghép
  tile zoom-0 có sẵn của squaremap — không render lại, <1s.

## WorldEdit

- Plugin từ https://modrinth.com/plugin/worldedit, bản khớp version server (vd
  `worldedit-bukkit-7.4.5.jar` cho MC 26.2), bỏ vào `plugins/`.
- Không có plugin phân quyền (LuckPerms...) trên server — quyền WorldEdit
  theo cơ chế mặc định của Bukkit: chỉ tài khoản op mới dùng được. Cấp qua
  `/oplevel` trên Discord, không cấp lẻ theo permission node.

## Đồng hồ hệ thống

UDP bị chặn nên `systemd-timesyncd` (NTP qua UDP/123) không sync được. Thay
bằng `htpdate` (sync giờ qua HTTPS): `systemd/htpdate-sync.timer` chạy 04:45
hàng ngày, trước backup lúc 05:00.

## Không có trong repo

- `world/` — dữ liệu binary, backup riêng lên Google Drive.
- File jar Paper — tải từ PaperMC, không version.
- Secret (bot token, admin user ID, token Cloudflare/Google Drive) — xem
  `secrets.txt` local.

## Lưu ý

- Không chạy `/reload` trong console — từng làm crash server.
- Không đặt `online-mode=true` — tài khoản cracked lỗi đăng nhập, có thể
  tạo nhân vật trùng khác UUID.
- Không dùng `/op`/`/deop` — resolve tên không phân biệt hoa/thường, có thể
  khớp nhầm tài khoản. `apply_player_action()` trong `mc_lib.py` ghi thẳng
  `ops.json` thay vì dùng lệnh này.
- Sau VPS crash/reboot bất thường, kiểm tra `server-port` và `online-mode`
  trong `server.properties` — cả 2 từng bị revert về mặc định.
- Không có UDP/IPv6: không host được Bedrock (Geyser), voice chat plugin,
  hay VPN trên máy này.
