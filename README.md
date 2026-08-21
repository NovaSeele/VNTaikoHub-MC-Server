# VNTaikoHub MC Server

Hạ tầng + dashboard quản trị cho server Minecraft Paper của nhóm bạn, chạy
trên VPS BNIX (163.61.72.134), nguỵ trang qua port 443 cùng với các website
khác trên cùng máy (vì VPS provider chặn port 25565 mặc định và không cho
mở game server chính thức).

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
- `systemd/*.service` — 3 service: `minecraft.service` (chạy Paper qua
  `screen`), `mc-dashboard.service`, `mc-proxy-relay.service`.
- `nginx/stream-mc.conf` — đoạn cấu hình `stream {}` cần dán vào
  `/etc/nginx/nginx.conf` (top-level, không đặt được trong sites-available).
- `scripts/run.sh` — script khởi động Paper (Aikar's flags).

## Triển khai trên VPS mới (tóm tắt)

1. Cài Paper build mới nhất từ `fill.papermc.io`, đặt vào `/home/minecraft/`.
2. Copy `scripts/run.sh` vào `/home/minecraft/run.sh`, sửa tên file jar cho
   khớp bản đang dùng.
3. Copy `dashboard/*.py` vào `/opt/mc-dashboard/`.
4. Cài `libnginx-mod-stream` (`apt-get install libnginx-mod-stream`), dán nội
   dung `nginx/stream-mc.conf` vào `/etc/nginx/nginx.conf`.
5. Copy 3 file trong `systemd/` vào `/etc/systemd/system/`, rồi
   `systemctl daemon-reload && systemctl enable --now minecraft mc-dashboard mc-proxy-relay`.
6. Tạo `/etc/mc-dashboard-auth.env` cho đăng nhập admin dashboard (xem hướng
   dẫn hash mật khẩu trong `dashboard/app.py`, hàm `_load_auth_config`).

## Backup world tự động (Google Drive)

- `backup/world_backup.sh` — tạm dừng ghi đĩa (`save-off`/`save-all flush`),
  nén `world/` thành `.tar.gz`, đẩy lên Google Drive qua `rclone` (remote
  `gdrive:MinecraftBackups`), tự xoá bản backup cũ hơn 14 ngày trên Drive.
  Luôn bật lại `save-on` kể cả khi có lỗi giữa chừng (dùng `trap`).
- `systemd/mc-world-backup.{service,timer}` — chạy script trên lúc 5h sáng
  hàng ngày.
- Cấu hình `rclone` remote `gdrive:` (OAuth token) phải làm thủ công 1 lần —
  không có trong repo, xem `secrets.txt` local hoặc chạy lại
  `rclone authorize "drive"` trên máy có trình duyệt rồi
  `rclone config create gdrive drive scope=drive token='<json token>'` trên VPS.

### Đồng hồ hệ thống (quan trọng)

VPS này chặn UDP nên NTP chuẩn (`systemd-timesyncd`, dùng UDP 123) **không
hoạt động được** — đồng hồ có thể trôi lệch hàng chục phút mà không tự sửa,
ảnh hưởng tới giờ chạy backup/timer. Đã tắt `systemd-timesyncd` và thay bằng
`htpdate` (đồng bộ giờ qua HTTPS/TCP):
- `systemd/htpdate-sync.{service,timer}` — chạy `htpdate -s` lúc 4h45 sáng
  hàng ngày (trước giờ backup 15 phút) để đảm bảo giờ chính xác.

## KHÔNG có trong repo này (cố ý)

- World save (`world/`) — dữ liệu binary lớn, không hợp với git. Backup riêng
  bằng script tự động lên Google Drive (chạy lúc 5h sáng hàng ngày).
- File jar Paper — tải trực tiếp từ PaperMC, không cần versioning trong git.
- Mọi secret/credential (mật khẩu, token, salt/hash) — xem file
  `secrets.txt` lưu local, KHÔNG commit vào đây.

## Lưu ý vận hành quan trọng

- **Không chạy `/reload`** trong console Minecraft — từng làm crash server.
- **Không đổi `online-mode=true`** — sẽ làm tài khoản cracked bị lỗi đăng
  nhập và có thể tạo nhân vật trùng UUID với tài khoản Microsoft thật.
- Sau mỗi lần VPS crash/reboot bất thường, kiểm tra lại `server-port` và
  `online-mode` trong `server.properties` — 2 giá trị này từng bị revert về
  mặc định sau sự cố.
