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
  `last_seen` của `/players` đọc thẳng dòng "joined"/"left the game" gần
  nhất từ `logs/latest.log` + tối đa 60 file `.log.gz` cũ hơn
  (`_scan_last_events`) — **không** dùng `expiresOn` trong `usercache.json`
  làm nguồn chính, vì Mojang tính giá trị đó bằng `+1 tháng lịch` có clamp
  ngày, khiến việc suy ngược ra ngày gốc bị mơ hồ (vd 30/8 và 31/8 cùng ra
  1 kết quả khi tháng sau chỉ có 30 ngày) — `_last_seen_from_expiry` chỉ
  còn là fallback cho người chơi quá cũ, ngoài phạm vi 60 file quét.
- `dashboard/relay.py` — relay PROXY-protocol giữa nginx và Paper.
- `dashboard/discord_bot.py` — slash command, import trực tiếp `mc_lib.py`.
  Lệnh xem (`/status`, `/players`, `/version`, `/map`, `/biomes`) public;
  lệnh thay đổi (`/gamemode`, `/oplevel`, `/console`, `/update`) chỉ admin
  dùng được — gồm `DISCORD_ADMIN_ID` (owner, cố định qua env) và danh sách
  cấp thêm qua `/grantadmin`/`/revokeadmin` (lưu ở `extra_admins.json`, chỉ
  owner mới gọi được 2 lệnh này). `/link` cho tự liên kết Discord ↔ tên
  nhân vật (lưu ở `player_links.json`) — các lệnh nhận `player` (gamemode,
  oplevel, biomes) có thêm tham số `discord_user` để dùng thay, khỏi gõ tên.
  `/discordname` (tự phục vụ, cần `/link` trước) bật/tắt hiện tên Discord
  của mình trong game qua plugin SimpleNicks. Cần `discord.py`.
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

## Chat 2 chiều (DiscordSRV)

- Plugin từ https://modrinth.com/plugin/discordsrv, bỏ vào `plugins/`.
- Dùng **bot Discord riêng** (`VnTaikoHub-MC-Chat&Log`, application/token
  khác hoàn toàn với `discord_bot.py`) — cố ý tách ra sau khi share chung 1
  token với bot lệnh chính từng dùng tạm thời, để tránh 2 process (Python +
  JDA) cùng giành session trên 1 token. Token lưu trong `secrets.txt`, key
  riêng (không phải `DISCORD_BOT_TOKEN`).
- Cần bật 2 privileged intent cho app bot này trên Discord Developer Portal →
  Bot → Privileged Gateway Intents: **Server Members Intent** và
  **Message Content Intent**. Thiếu 1 trong 2 là JDA login xong nhưng bị
  disconnect ngay ("missing intents").
- `plugins/DiscordSRV/config.yml`: set `BotToken`, `Channels: {"global":
  "<channel_id>"}`. Đổi `BotToken` bắt buộc **restart** (chỉ đọc lúc init).
  Đổi `DiscordChatChannelMinecraftToDiscord` (Minecraft → Discord) áp dụng
  live được qua `discordsrv reload`, đã test xác nhận. Riêng
  `DiscordChatChannelDiscordToMinecraft` (Discord → Minecraft) **không tin
  được `reload`** — thực tế test thấy vẫn relay dù đã tắt + reload, phải
  **restart** mới chắc chắn áp dụng đúng.
- `/chatbridge` trên Discord (admin) — bật/tắt từng chiều hoặc cả 2 qua
  `discordsrv reload`; nếu tắt chiều Discord → Minecraft mà vẫn thấy relay,
  cần restart server thêm (xem ghi chú trên).
- `DiscordGameStatus: []` — tắt để tránh giẫm lên rich presence riêng của
  bot lệnh chính (`discord_bot.py`, xem `on_ready()`) — không còn bắt buộc
  từ khi tách token, nhưng giữ tắt cho gọn vì bot chat này không cần hiện
  status "Playing".

## Hiện tên Discord trong game (SimpleNicks)

- Plugin từ https://modrinth.com/plugin/simplenicks, bỏ vào `plugins/`.
- `plugins/SimpleNicks/config.yml` — mặc định chỉ cho phép nickname gồm
  chữ/số/gạch dưới (`nickname-regex: '[A-Za-z0-9_]+'`), không đủ cho tên
  Discord thật (dấu cách, tiếng Nhật, tiếng Việt có dấu...). Đã nới thành
  `'.+'`, `max-nickname-length: 32` (khớp giới hạn tên Discord), và bật
  `tablist-nick: true` để đổi luôn cả tab list, không chỉ tên trên đầu.
  Đổi 3 key này chỉ cần `nick reload` qua console, không cần restart.
- `set_ingame_nickname()` trong `mc_lib.py` chạy `nick admin set/reset` qua
  console (console luôn bypass `require-permission` của plugin, không cần
  permission plugin). Tự thay `<`/`>` trong tên Discord bằng `‹`/`›` trước
  khi gửi — nickname được plugin parse bằng MiniMessage, ký tự thật từ tên
  Discord có thể vô tình bị hiểu thành tag định dạng nếu không escape.
- `/discordname` trên Discord chỉ đổi tên của chính người gọi lệnh (tra qua
  `player_links.json`), không có tham số target — không có rủi ro chỉnh
  tên người khác.

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
- `dashboard/extra_admins.json` — danh sách user ID được cấp quyền admin
  qua `/grantadmin`, tạo runtime trên VPS, không version.
- `dashboard/player_links.json` — map Discord user ID → tên nhân vật,
  tạo qua `/link`, tạo runtime trên VPS, không version.
- Secret (bot token, admin user ID, token Cloudflare/Google Drive) — xem
  `secrets.txt` local.

## Lưu ý

- Không chạy `/reload` trong console — từng làm crash server.
- Không đặt `online-mode=true` — tài khoản cracked lỗi đăng nhập, có thể
  tạo nhân vật trùng khác UUID.
- Không dùng `/op`/`/deop` — resolve tên không phân biệt hoa/thường, có thể
  khớp nhầm tài khoản. `apply_player_action()` trong `mc_lib.py` ghi thẳng
  `ops.json` thay vì dùng lệnh này.
- OP level 4 chỉ dành cho `NovaSeele`/`novaseele` (`OWNER_NAMES` trong
  `mc_lib.py`, so khớp không phân biệt hoa/thường) — 2 tài khoản này đã cố
  định level 4 sẵn, không ai cần "set" level 4 qua lệnh cả (kể cả owner).
  Vì vậy `/oplevel` chỉ có dropdown cố định **0-3** cho tất cả mọi người
  (`app_commands.choices`, giống `/gamemode`) — level 4 không xuất hiện ở
  đâu trong UI. `apply_player_action()` trong `mc_lib.py` vẫn giữ chặn ngầm
  (tự hạ về 3 nếu ai đó gọi thẳng với level ≥4 ngoài 2 tài khoản trên) làm
  lớp phòng vệ thứ 2, phòng trường hợp có code khác gọi thẳng hàm này.
- `function-permission-level=3` trong `server.properties` — command block
  và `/function` chỉ chạy được với OP level ≥ 3 (mặc định vanilla là 2).
- Sau VPS crash/reboot bất thường, kiểm tra `server-port` và `online-mode`
  trong `server.properties` — cả 2 từng bị revert về mặc định.
- Không có UDP/IPv6: không host được Bedrock (Geyser), voice chat plugin,
  hay VPN trên máy này.
