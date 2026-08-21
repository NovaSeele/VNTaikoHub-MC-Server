# VNTaikoHub MC Server

Hạ tầng cho server Minecraft Paper của VNTaikoHub, quản trị qua bot Discord,
chạy trên 1 VPS không có UDP/IPv6 khả dụng, nguỵ trang qua port 443 (vì VPS
chặn port 25565 mặc định).

## Kiến trúc

```
Client Minecraft
      │  TCP, raw protocol (không có TLS ClientHello)
      ▼
nginx :443  (stream + ssl_preread, phân biệt HTTPS thật vs Minecraft thô)
      │  proxy_protocol on
      ▼
relay.py :25567   (bóc PROXY protocol header, ghi lại IP/username thật)
      │
      ▼
Paper server :8443   (online-mode=false, offline/cracked)
```

Bot Discord (`discord_bot.py`) đọc trạng thái server qua Server List Ping tự
viết, đọc/ghi `ops.json`/`usercache.json` trực tiếp, gửi lệnh console qua
`screen` — toàn bộ logic nằm ở `mc_lib.py`, bot chỉ là lớp giao diện slash
command bên trên. (Trước đây có thêm 1 web dashboard làm giao diện thay thế,
đã gỡ bỏ sau khi mọi người chuyển hẳn sang dùng bot — xem mục "Lịch sử" cuối
file.)

## Thư mục

- `dashboard/mc_lib.py` — toàn bộ logic xử lý (stdlib Python, không phụ
  thuộc ngoài): Server List Ping, đọc/ghi `ops.json`, quản lý người chơi
  (gamemode/OP level), gửi lệnh console, đọc NBT, kiểm tra/cập nhật phiên
  bản Paper. Không tự chạy gì — chỉ là thư viện để `discord_bot.py` import.
- `dashboard/relay.py` — relay PROXY-protocol giữa nginx và Paper, để biết
  được IP thật của người chơi dù đi qua nginx.
- `dashboard/discord_bot.py` — bot Discord (slash command), import trực
  tiếp từ `mc_lib.py`. Lệnh xem (`/status`, `/players`, `/version`, `/map`,
  `/biomes`) mở cho mọi người; lệnh thay đổi (`/gamemode`, `/oplevel`,
  `/console`, `/update`) chỉ 1 Discord user ID (`DISCORD_ADMIN_ID`) dùng
  được — kiểm tra bằng đúng user ID trong code, không dựa vào quyền
  Discord/role (không thể ẩn lệnh khỏi người khác trong danh sách autocomplete
  nếu không có quyền Administrator server Discord, nhưng vẫn chặn được việc
  thực thi). Cần `pip install discord.py`.
- `dashboard/map_snapshot.py` — ghép ảnh preview bản đồ từ tile có sẵn của
  squaremap để gửi kèm trong Discord (xem mục "Bản đồ live" bên dưới). Cần
  `pip install Pillow`.
- `backup/world_backup.sh` — nén + upload world lên Google Drive.
- `systemd/*.service`, `systemd/*.timer` — `minecraft`, `mc-proxy-relay`,
  `mc-discord-bot`, `mc-world-backup` (+ timer), `htpdate-sync` (+ timer).
- `nginx/stream-mc.conf` — đoạn cấu hình `stream {}` cần dán vào
  `/etc/nginx/nginx.conf` (top-level, không đặt được trong sites-available).
- `nginx/vntaikohub-map.conf` — vhost riêng cho web viewer plugin squaremap
  (xem mục "Bản đồ live" bên dưới).
- `scripts/run.sh` — script khởi động Paper (Aikar's flags).

## Triển khai trên VPS mới (tóm tắt)

1. Cài Paper build mới nhất từ `fill.papermc.io`, đặt vào `/home/minecraft/`.
2. Copy `scripts/run.sh` vào `/home/minecraft/run.sh`, sửa tên file jar cho
   khớp bản đang dùng.
3. Copy `dashboard/mc_lib.py`, `dashboard/relay.py`, `dashboard/discord_bot.py`,
   `dashboard/map_snapshot.py` vào `/opt/mc-dashboard/`, `backup/world_backup.sh`
   vào `/opt/mc-dashboard/backup/`.
4. Cài `libnginx-mod-stream` (`apt-get install libnginx-mod-stream`), dán nội
   dung `nginx/stream-mc.conf` vào `/etc/nginx/nginx.conf`. Đổi `listen 443`
   của các vhost khác trên máy thành `listen 127.0.0.1:18443 proxy_protocol;`.
5. Copy các file trong `systemd/` vào `/etc/systemd/system/`, rồi
   `systemctl daemon-reload && systemctl enable --now minecraft mc-proxy-relay
   mc-world-backup.timer htpdate-sync.timer`.
6. Cài `rclone`, cấu hình remote `gdrive:` (xem phần Backup bên dưới).
7. Bot Discord: `pip install discord.py Pillow`, tạo `/etc/mc-discord-bot.env`
   chứa `DISCORD_BOT_TOKEN=...` và `DISCORD_ADMIN_ID=...`, rồi
   `systemctl enable --now mc-discord-bot`.
8. (Tuỳ chọn) Bản đồ live: xem mục "Bản đồ live (squaremap)" bên dưới.

## Backup world tự động (Google Drive)

- `backup/world_backup.sh` — tạm dừng ghi đĩa (`save-off`/`save-all flush`),
  nén `world/` thành `.tar.gz`, đẩy lên Google Drive qua `rclone`, tự xoá
  bản backup cũ hơn 7 ngày trên Drive. Luôn bật lại `save-on` kể cả khi có
  lỗi giữa chừng (dùng `trap`).
- `systemd/mc-world-backup.timer` — chạy script trên lúc 5h sáng hàng ngày.
- `rclone` remote `gdrive:` dùng OAuth client_id riêng (không dùng client_id
  dùng chung mặc định của rclone — bị Google khai tử trong 2026) và scope
  `drive.file` (chỉ truy cập file/folder do chính app tạo ra, an toàn hơn và
  không cần qua quy trình verify của Google). Cấu hình đầy đủ không có trong
  repo — xem `secrets.txt` local, hoặc làm lại theo
  https://rclone.org/drive/#making-your-own-client-id (dùng
  `--drive-scope=drive.file`, nhớ Publish app để tránh token hết hạn 7 ngày).

## Bản đồ live (squaremap)

Ban đầu tự viết script render ảnh top-down từ file `.mca` (1 pixel/chunk,
chạy định kỳ vì mất 15-30 phút/lần) — bỏ hẳn sau khi so sánh thực tế với
plugin [squaremap](https://github.com/jpenilla/squaremap): plugin render
chi tiết hơn nhiều (đúng màu/texture như bản đồ vanilla, không bỏ sót build
trên cao vì script tự viết chỉ lấy mẫu 1 điểm/chunk), nhanh hơn (~7 phút
full-render 300 region so với ~16 phút), và **RAM gần như không đổi** khi đo
thực tế trên VPS đang chạy (baseline ~3.6GB có/không có plugin, kể cả lúc
đang full-render).

- Cài: tải `squaremap-paper-mc<version>-<ver>.jar` đúng bản Minecraft đang
  chạy từ https://github.com/jpenilla/squaremap/releases (thư mục
  `dimensions/minecraft/<dim>/` mới của Minecraft 26.x không phải vấn đề —
  squaremap tự nhận diện qua Bukkit World API, không đọc thẳng file).
- Bỏ vào `/home/minecraft/plugins/`, restart `minecraft.service`.
- **Bắt buộc** sửa `plugins/squaremap/config.yml`: đổi
  `internal-webserver.bind` từ `0.0.0.0` thành `127.0.0.1` — mặc định
  squaremap tự mở port ra ngoài trực tiếp (8080), không đi qua HTTPS/nginx
  disguise của server này. Áp dụng bằng lệnh console `/squaremap reload`
  (không cần restart lại).
- `nginx/vntaikohub-map.conf` — vhost riêng proxy `127.0.0.1:8080` ra
  `https://vntaikohub-map.novaseele.com`. **Quan trọng**: đè
  `Cache-Control: no-cache, must-revalidate` cho path `/tiles/` — mặc định
  squaremap set `max-age=14400` (4 tiếng), khiến bản đồ live trông như bị
  "đứng hình". Cloudflare cũng tự cache ảnh PNG bất kể header gốc — cần thêm
  1 Cache Rule ở Cloudflare Dashboard bypass hẳn cache cho
  `https://vntaikohub-map.novaseele.com/*` (Caching → Cache Rules), nếu
  không domain khác trên cùng zone có Cache Rule wildcard sẽ không đủ,
  Cloudflare match theo URL đầy đủ. Ngay cả sau khi sửa đúng, trình duyệt cá
  nhân đôi khi vẫn giữ cache đĩa cũ dù `Ctrl+F5` — tab ẩn danh luôn thấy bản
  mới nhất, dùng để kiểm tra khi nghi ngờ.
- Render toàn bộ world lần đầu: `/squaremap fullrender minecraft:overworld`
  trong console (lưu ý dùng đúng dạng `minecraft:overworld`, không phải
  `world` hay `minecraft_overworld` dù đó là tên thư mục data của nó). Render
  theo bán kính quanh 1 toạ độ cụ thể (nhanh hơn nhiều khi chỉ cần vá 1 vùng):
  `/squaremap radiusrender minecraft:overworld <bán_kính> <x> <z>` (toạ độ x,z
  cách nhau bằng dấu cách, không phải dấu phẩy).
- Sau lần render đầu, squaremap tự cập nhật khi có sự kiện block thay đổi
  (đặt/phá khối...) — không cần timer định kỳ như cách cũ. Lưu ý: mỗi mức
  zoom (0-3) là 1 bộ tile ảnh **render riêng biệt**, có thể tạm thời lệch
  pha với nhau ngay sau khi có thay đổi lớn/nhanh (vd người chơi bay elytra
  xa) — tự bắt kịp sau vài giây tới vài phút, hoặc ép đồng bộ ngay bằng
  `radiusrender`/`fullrender`.
- Discord bot `/map` gửi kèm cả ảnh preview lẫn link — không giới hạn admin
  vì đây là hành động chỉ đọc, không đổi dữ liệu gì. Ảnh không phải render
  lại từ đầu: `dashboard/map_snapshot.py` chỉ ghép các tile PNG có sẵn của
  squaremap (`plugins/squaremap/web/tiles/<world>/0/*.png`, zoom 0 = ít tile
  nhất = toàn cảnh) lại thành 1 ảnh bằng Pillow — mất dưới 1 giây vì chỉ đọc
  file có sẵn, không phải quét lại world.

## Đồng hồ hệ thống

VPS không có UDP nên NTP chuẩn (`systemd-timesyncd`, dùng UDP 123) **không
hoạt động được** — đồng hồ có thể trôi lệch hàng chục phút mà không tự sửa,
ảnh hưởng tới giờ chạy các timer. Đã tắt `systemd-timesyncd` và thay bằng
`htpdate` (đồng bộ giờ qua HTTPS/TCP): `systemd/htpdate-sync.timer` chạy lúc
4h45 sáng hàng ngày, trước giờ backup 15 phút.

## KHÔNG có trong repo này

- World save (`world/`) — dữ liệu binary lớn, không hợp với git. Backup riêng
  bằng script tự động lên Google Drive.
- File jar Paper — tải trực tiếp từ PaperMC, không cần versioning trong git.
- Mọi secret/credential (bot token, admin user ID, token Cloudflare/Google
  Drive) — xem file `secrets.txt` lưu local, KHÔNG commit vào đây.

## Lưu ý vận hành quan trọng

- **Không chạy `/reload`** trong console Minecraft — từng làm crash server.
- **Không đổi `online-mode=true`** — sẽ làm tài khoản cracked bị lỗi đăng
  nhập và có thể tạo nhân vật trùng UUID với tài khoản Microsoft thật.
- **Không dùng lệnh `/op`/`/deop`** để đổi quyền — resolve tên không phân
  biệt hoa/thường, có thể khớp nhầm sang tài khoản khác. `apply_player_action()`
  trong `mc_lib.py` luôn ghi thẳng vào `ops.json`.
- Sau mỗi lần VPS crash/reboot bất thường, kiểm tra lại `server-port` và
  `online-mode` trong `server.properties` — 2 giá trị này từng bị revert về
  mặc định sau sự cố.
- VPS không có UDP/IPv6 khả dụng — không host được Bedrock (Geyser), voice
  chat plugin, hay VPN tự host trên máy này.

## Lịch sử

Ban đầu có 1 web dashboard (`app.py`, HTTP server tự viết + đăng nhập admin
PBKDF2 + giao diện HTML/JS) chạy song song với bot Discord, cùng dùng chung
logic. Sau khi cả nhóm chuyển hẳn sang dùng bot, dashboard không còn ai dùng
— đã gỡ bỏ hoàn toàn (service, vhost nginx, DNS record, file auth) và tách
phần logic dùng chung ra `mc_lib.py` để bot tiếp tục dùng độc lập, không còn
phụ thuộc vào code của web server nữa.
