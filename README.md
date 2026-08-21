# VNTaikoHub MC Server

Hạ tầng + dashboard quản trị cho server Minecraft Paper của nhóm bạn, chạy
trên 1 VPS dùng chung với các website khác, nguỵ trang qua port 443 (vì VPS
chặn port 25565 mặc định, không cho mở game server chính thức, và không có
UDP/IPv6 khả dụng).

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

Dashboard (`app.py`) chạy song song ở `127.0.0.1:8090`, đọc trạng thái server
qua Server List Ping tự viết, đọc/ghi `ops.json`/`usercache.json` trực tiếp,
và gửi lệnh console qua `screen`.

## Thư mục

- `dashboard/app.py` — dashboard web quản trị (stdlib Python, không phụ thuộc
  ngoài) — xem trạng thái, quản lý người chơi (gamemode/OP level), console,
  kiểm tra/cập nhật phiên bản, đăng nhập admin.
- `dashboard/relay.py` — relay PROXY-protocol giữa nginx và Paper, để dashboard
  biết được IP thật của người chơi dù đi qua nginx.
- `backup/world_backup.sh` — nén + upload world lên Google Drive.
- `systemd/*.service`, `systemd/*.timer` — `minecraft`, `mc-dashboard`,
  `mc-proxy-relay`, `mc-world-backup` (+ timer), `htpdate-sync` (+ timer).
- `nginx/stream-mc.conf` — đoạn cấu hình `stream {}` cần dán vào
  `/etc/nginx/nginx.conf` (top-level, không đặt được trong sites-available).
- `scripts/run.sh` — script khởi động Paper (Aikar's flags).

## Triển khai trên VPS mới (tóm tắt)

1. Cài Paper build mới nhất từ `fill.papermc.io`, đặt vào `/home/minecraft/`.
2. Copy `scripts/run.sh` vào `/home/minecraft/run.sh`, sửa tên file jar cho
   khớp bản đang dùng.
3. Copy `dashboard/*.py` vào `/opt/mc-dashboard/`, `backup/world_backup.sh`
   vào `/opt/mc-dashboard/backup/`.
4. Cài `libnginx-mod-stream` (`apt-get install libnginx-mod-stream`), dán nội
   dung `nginx/stream-mc.conf` vào `/etc/nginx/nginx.conf`. Đổi `listen 443`
   của các vhost khác trên máy thành `listen 127.0.0.1:18443 proxy_protocol;`.
5. Copy các file trong `systemd/` vào `/etc/systemd/system/`, rồi
   `systemctl daemon-reload && systemctl enable --now minecraft mc-dashboard
   mc-proxy-relay mc-world-backup.timer htpdate-sync.timer`.
6. Tạo `/etc/mc-dashboard-auth.env` cho đăng nhập admin dashboard (xem
   hướng dẫn hash mật khẩu trong `dashboard/app.py`, hàm `_load_auth_config`).
7. Cài `rclone`, cấu hình remote `gdrive:` (xem phần Backup bên dưới).

## Backup world tự động (Google Drive)

- `backup/world_backup.sh` — tạm dừng ghi đĩa (`save-off`/`save-all flush`),
  nén `world/` thành `.tar.gz`, đẩy lên Google Drive qua `rclone`, tự xoá
  bản backup cũ hơn 14 ngày trên Drive. Luôn bật lại `save-on` kể cả khi có
  lỗi giữa chừng (dùng `trap`).
- `systemd/mc-world-backup.timer` — chạy script trên lúc 5h sáng hàng ngày.
- `rclone` remote `gdrive:` dùng OAuth client_id riêng (không dùng client_id
  dùng chung mặc định của rclone — bị Google khai tử trong 2026) và scope
  `drive.file` (chỉ truy cập file/folder do chính app tạo ra, an toàn hơn và
  không cần qua quy trình verify của Google). Cấu hình đầy đủ không có trong
  repo — xem `secrets.txt` local, hoặc làm lại theo
  https://rclone.org/drive/#making-your-own-client-id (dùng
  `--drive-scope=drive.file`, nhớ Publish app để tránh token hết hạn 7 ngày).

## Đồng hồ hệ thống

VPS không có UDP nên NTP chuẩn (`systemd-timesyncd`, dùng UDP 123) **không
hoạt động được** — đồng hồ có thể trôi lệch hàng chục phút mà không tự sửa,
ảnh hưởng tới giờ chạy các timer. Đã tắt `systemd-timesyncd` và thay bằng
`htpdate` (đồng bộ giờ qua HTTPS/TCP): `systemd/htpdate-sync.timer` chạy lúc
4h45 sáng hàng ngày, trước giờ backup 15 phút.

## KHÔNG có trong repo này (cố ý)

- World save (`world/`) — dữ liệu binary lớn, không hợp với git. Backup riêng
  bằng script tự động lên Google Drive.
- File jar Paper — tải trực tiếp từ PaperMC, không cần versioning trong git.
- Mọi secret/credential (mật khẩu, token, salt/hash) — xem file
  `secrets.txt` lưu local, KHÔNG commit vào đây.

## Lưu ý vận hành quan trọng

- **Không chạy `/reload`** trong console Minecraft — từng làm crash server.
- **Không đổi `online-mode=true`** — sẽ làm tài khoản cracked bị lỗi đăng
  nhập và có thể tạo nhân vật trùng UUID với tài khoản Microsoft thật.
- **Không dùng lệnh `/op`/`/deop`** để đổi quyền — resolve tên không phân
  biệt hoa/thường, có thể khớp nhầm sang tài khoản khác. Dashboard luôn ghi
  thẳng vào `ops.json`.
- Sau mỗi lần VPS crash/reboot bất thường, kiểm tra lại `server-port` và
  `online-mode` trong `server.properties` — 2 giá trị này từng bị revert về
  mặc định sau sự cố.
- VPS không có UDP/IPv6 khả dụng — không host được Bedrock (Geyser), voice
  chat plugin, hay VPN tự host trên máy này.
