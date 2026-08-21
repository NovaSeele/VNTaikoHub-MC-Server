# VNTaikoHub MC Server

Hạ tầng + dashboard quản trị cho server Minecraft Paper (Java Edition) của
một nhóm bạn, chạy trên 1 VPS dùng chung với các website khác
(`chunithm-app`, `chunithm-api`, dashboard này) trên cùng một máy
(163.61.72.134, Ubuntu 24.04).

## 1. Bối cảnh — tại sao kiến trúc lại phức tạp thế này

VPS này có 2 giới hạn ảnh hưởng trực tiếp tới thiết kế:

1. **Chặn cổng 25565** (cổng mặc định của Minecraft) và **cấm host server
   game qua ToS**.
2. **Không có UDP và IPv6** ở tầng mạng, không chỉ tầng firewall guest OS.
   Bằng chứng:
   - `journalctl -u systemd-timesyncd` cho thấy timeout liên tục khi gọi NTP
     (UDP/123) tới `ntp.ubuntu.com`, dù đã thử nhiều server khác nhau.
   - IPv6 trên VPS chỉ có link-local, không có route ra ngoài — outbound
     IPv6 thật sự luôn thất bại.
   - Thử nghiệm với playit.gg: UDP outbound tới cổng điều khiển của nó bị
     drop âm thầm dù đã tắt IPv6 (loại trừ nguyên nhân IPv6) — chặn xảy ra ở
     tầng router/switch phía nhà cung cấp, không phải guest OS, nên không thể
     bypass bằng cách chỉnh firewall trong VPS.

Hệ quả thiết kế:
- **Không thể** dùng UDP cho bất cứ việc gì (Bedrock/RakNet, voice chat
  plugin, WireGuard, NTP chuẩn...) — xem mục 6.
- **Không thể** mở port riêng cho Minecraft — phải giấu traffic Minecraft
  đằng sau port 443 vốn đã có traffic HTTPS thật của các site khác.

Các phương án khác đã cân nhắc và loại bỏ trước khi chọn hướng hiện tại:
Cloudflare Spectrum (trả phí, giá không rõ ràng), ngrok TCP tunnel (free tier
không có Reserved Address, có hard cap băng thông/tháng — từng gây outage
thật), playit.gg (chết vì lý do UDP ở trên).

## 2. Kiến trúc mạng

```
Client Minecraft (Java Edition, TCP thuần)
      │
      ▼
nginx :443  ── stream{} + ssl_preread ──┬── có TLS ClientHello ──► 127.0.0.1:18443 (vhost web thật)
      │                                 └── không có ClientHello ──► 127.0.0.1:25567 (relay.py)
      │ proxy_protocol on
      ▼
relay.py :25567  (bóc PROXY v1 header, ghi IP/username thật, pipe raw)
      │
      ▼
Paper server :8443  (online-mode=false, offline/cracked)
```

### 2.1. nginx `stream{}` + `ssl_preread` — cơ chế phân biệt traffic

`ssl_preread` đọc byte đầu của gói tin TCP để tìm **TLS ClientHello**
(handshake mở đầu của mọi kết nối HTTPS) mà **không cần** terminate TLS.
Traffic Minecraft (Handshake packet của protocol Minecraft, VarInt-length-
prefixed, không phải TLS) sẽ khiến `$ssl_preread_protocol` trả về chuỗi rỗng
`""`. Dựa vào đó, `map` trong `nginx/stream-mc.conf` định tuyến:

```nginx
map $ssl_preread_protocol $mc_backend {
    default 127.0.0.1:18443;   # có ClientHello → web thật
    ""      127.0.0.1:25567;   # không có → Minecraft → relay
}
```

Toàn bộ website khác trên máy (`chunithm-app`, `chunithm-api`, dashboard)
phải đổi `listen 443` gốc thành `listen 127.0.0.1:18443 proxy_protocol;` —
không còn nghe trực tiếp ở public 443 nữa, chỉ nhận traffic được nginx
`stream{}` forward vào.

**Giới hạn quan trọng**: `stream{}` chỉ hoạt động ở context top-level của
`nginx.conf`, không đặt được trong `sites-available/*.conf` hay `conf.d/`
(context http). Cần cài `libnginx-mod-stream` riêng — `nginx -V` có thể liệt
kê `--with-stream=dynamic` mà module vẫn chưa thật sự được cài/load.

**Vì sao không dùng được cho Bedrock**: cơ chế này chỉ multiplex được TCP.
Bedrock Edition dùng RakNet trên UDP — không có khái niệm "ClientHello" để
`ssl_preread` phân biệt, và UDP tới port bất kỳ đều bị chặn ở tầng hạ tầng
(mục 1), nên thủ thuật này không áp dụng được để hỗ trợ Bedrock.

### 2.2. `relay.py` — vì sao cần một lớp trung gian riêng

`proxy_protocol on` ở nginx bọc mỗi kết nối forward bằng 1 dòng PROXY
protocol v1 (`PROXY TCP4 <src_ip> <dst_ip> <src_port> <dst_port>\r\n`) chứa
IP thật của client. Netty listener thô của Paper **không tự parse được**
header này — nếu forward thẳng vào Paper, dòng PROXY sẽ bị hiểu nhầm thành
dữ liệu protocol Minecraft và làm hỏng handshake.

`relay.py` (asyncio, thuần stdlib) đứng giữa để:
1. Đọc và bóc dòng PROXY v1, lấy IP thật (`real_ip`).
2. "Peek" 2 packet đầu tiên (Handshake + LoginStart) bằng cách tự decode
   VarInt, lấy username mà **không tiêu thụ** dữ liệu — buffer được giữ lại
   và replay nguyên vẹn vào kết nối upstream tới Paper ngay sau đó.
3. Ghi `{ip, username, connected_at}` vào `/run/mc-proxy/connections.json`
   (dashboard đọc file này để hiện IP người chơi ở tab Quản lý).
4. Từ đó chỉ pipe byte thô 2 chiều giữa client và Paper (`asyncio.gather`
   trên 2 coroutine `pipe()`), không đụng gì thêm vào traffic.

Vì "peek" có timeout (2.5s) và không bắt buộc phải thành công, một client
không gửi đúng format Minecraft (ví dụ port scanner) vẫn được forward bình
thường, không bị relay chặn — relay chỉ *cố gắng* đọc username, không xác
thực protocol.

## 3. Dashboard (`app.py`) — chi tiết kỹ thuật

Toàn bộ dashboard là 1 file Python duy nhất, chỉ dùng thư viện chuẩn (không
`pip install` gì), chạy bằng `http.server.BaseHTTPRequestHandler` thô.

### 3.1. Server List Ping (SLP) tự viết

Không dùng thư viện `mcstatus` — tự implement giao thức SLP (Handshake +
StatusRequest + đọc StatusResponse) bằng cách tự encode/decode VarInt.
Lưu ý khi viết: **VarInt âm trong Python không bao giờ right-shift về 0**
(khác Java, do Python int không giới hạn bit) — dùng protocol version `0`
(không phải `-1`) trong Handshake, vì server không validate version với gói
StatusRequest.

### 3.2. NBT parser tự viết (`_NBTReader`)

Đọc trực tiếp file `.dat` của người chơi (gzip + NBT nhị phân) để lấy
`playerGameType` khi người chơi **offline** — hỗ trợ đủ 12 loại tag NBT
(End/Byte/Short/Int/Long/Float/Double/ByteArray/String/List/Compound/
IntArray/LongArray). Khi người chơi **online**, gamemode được lấy qua lệnh
console `/data get entity <name> playerGameType` thay vì đọc NBT trên đĩa —
vì file `.dat` chỉ được ghi khi disconnect/autosave, dữ liệu trên đĩa lúc
đang chơi là cũ.

### 3.3. UUID offline tự tính (`offline_uuid`)

Replicate chính xác `UUID.nameUUIDFromBytes(("OfflinePlayer:"+name)
.getBytes())` của Java: MD5 hash của chuỗi, rồi set thủ công version bit
(0x30 vào byte 6) và variant bit (0x80 vào byte 8) theo UUID v3. Đã verify
khớp UUID thật của người chơi thật trong `usercache.json`.

**Hệ quả quan trọng cần nhớ**: hàm hash này **phân biệt hoa/thường** và phụ
thuộc `online-mode`:
- `online-mode=false`: UUID luôn tính từ tên client gửi lên, bất kể tài
  khoản Microsoft thật hay cracked.
- `online-mode=true`: server xác thực qua Mojang, dùng UUID thật (v4, ngẫu
  nhiên) của tài khoản — khác hoàn toàn UUID offline (v3) của cùng tên.

→ Nếu `online-mode` bị đổi qua lại (vô tình hay do sự cố), **cùng 1 người sẽ
có 2 UUID khác nhau**, bị dashboard/`ops.json`/`usercache.json` coi là 2
nhân vật riêng biệt. Đã xảy ra thật (xem mục 7).

### 3.4. Quản lý OP level — vì sao KHÔNG dùng lệnh `/op`

`apply_player_action()` **không bao giờ** gọi `/op <name>` hay `/deop <name>`
qua console — luôn ghi thẳng vào `ops.json` (hàm `update_ops_json()`, dùng
`offline_uuid(name)` để tính UUID chính xác theo đúng tên, match theo tên
chính xác không phân biệt hoa/thường lẫn lộn).

Lý do: lệnh `/op` gốc của Minecraft resolve tên **không phân biệt hoa/thường**
qua GameProfileCache — nếu trong cache đã có 1 profile viết hoa khác (ví dụ
`NovaSeele`) và ta gọi `/op novaseele` (viết thường), lệnh sẽ khớp nhầm vào
profile đã cache sẵn thay vì tạo entry mới cho UUID thật của tên viết thường
— khiến OP không được áp dụng cho đúng người, trong khi dashboard vẫn báo
"thành công" (lệnh chạy không lỗi). Bug này xảy ra thật với 2 tài khoản
`NovaSeele`/`novaseele` của cùng 1 người (xem mục 7).

Cũng **không** gọi `/reload` sau khi đổi `ops.json` để áp dụng ngay — Paper's
plugin reload đã từng làm crash server thật (mục 7). Thay đổi OP level chỉ
có hiệu lực khi người chơi **rejoin**, vì Bukkit luôn đọc lại `ops.json` mới
mỗi lần có người vào server.

### 3.5. Xác thực admin

- Mật khẩu hash bằng **PBKDF2-HMAC-SHA256, 200,000 iteration**, salt 16 byte
  ngẫu nhiên, lưu ở `/etc/mc-dashboard-auth.env` (mode 600, ngoài repo).
- So sánh mật khẩu và username đều dùng `hmac.compare_digest` (constant-time)
  để tránh timing attack.
- Session token: `secrets.token_urlsafe(32)`, lưu trong dict in-memory (mất
  khi restart service — chấp nhận đánh đổi để giữ code đơn giản), cookie
  `HttpOnly; Secure; SameSite=Strict`, hạn 30 ngày.
- Chặn brute-force đơn giản: 5 lần sai → khoá 300 giây (in-memory, global,
  không phân biệt theo IP).
- Mọi endpoint có side-effect (`/api/console`, `/api/player-action`,
  `/api/update`) đều bắt buộc qua `_require_auth()` — không có auth thì các
  nút hành động trên dashboard vẫn hiện nhưng gọi API sẽ bị từ chối.

## 4. Backup world tự động lên Google Drive

### 4.1. Vì sao world không nằm trong git repo

World save là dữ liệu binary lớn, thay đổi liên tục (hiện ~2.2GB, còn tăng).
Git không phù hợp để version dữ liệu kiểu này — mỗi commit gần như lưu
nguyên bản mới, repo phình rất nhanh, và GitHub free tier chặn cứng file
>100MB. Backup world dùng cơ chế snapshot + object storage (Google Drive),
không phải version control.

### 4.2. `backup/world_backup.sh`

```
save-off → save-all flush → sleep 10 → tar czf → save-on → rclone copy → rclone delete --min-age 14d
```

Vài chi tiết dễ bị bỏ sót khi tự viết lại:

- **`trap ... EXIT`** đảm bảo `save-on` luôn được gọi lại dù script lỗi giữa
  chừng (network fail, tar lỗi...) — tránh treo server ở trạng thái tắt ghi
  đĩa vĩnh viễn. Đây là bug thật gặp phải ở lần chạy thử đầu tiên: dùng
  `set -e` khiến script thoát ngay khi `tar` trả về exit code khác 0, bỏ qua
  luôn dòng `save-on`.
- **`tar` exit code 1 là bình thường, không phải lỗi**: "file changed as we
  read it" xảy ra ngay cả khi đã `save-off`, vì 1 số file (log, usercache...)
  vẫn có thể được ghi bởi thread khác của Paper. Script chỉ coi exit code
  `>= 2` là lỗi thật.
- Retention 14 ngày dùng `rclone delete --min-age 14d` — chạy **sau** khi
  upload xong bản mới, không phải trước, để không bao giờ có lúc 0 backup
  nào tồn tại nếu script bị gián đoạn giữa chừng.

### 4.3. Vì sao dùng OAuth client_id riêng + scope `drive.file`

rclone có 1 "shared client_id" mặc định cho backend Google Drive, dùng
chung cho hàng triệu người dùng rclone toàn cầu — Google sẽ khai tử client_id
này trong 2026 do chính sách quota/chống lạm dụng. Giải pháp là tạo OAuth
client riêng trên Google Cloud Console, gắn với 1 project của chính mình —
không nằm trong diện bị khai tử vì không phải tài nguyên dùng chung.

Ban đầu định dùng scope mặc định rclone gợi ý (`drive`, full access) —
nhưng scope này thuộc nhóm **"restricted"** của Google, và Google **bắt
buộc quy trình verify chính thức** (quay video demo, giải trình từng scope,
có thể cần đánh giá bảo mật CASA trả phí) mới cho phép app ở trạng thái
Production — hoàn toàn không hợp lý cho 1 script cá nhân dùng một mình.

Đổi sang scope `drive.file` (nhóm "non-sensitive") giải quyết cả 2 vấn đề:
- Không cần verify — chỉ cần Publish app (Testing → Production) là xong,
  tránh được giới hạn refresh token hết hạn sau 7 ngày (áp dụng cho app ở
  trạng thái Testing).
- Nguyên tắc least-privilege: app chỉ truy cập được file/folder **do chính
  nó tạo ra** (`VnTaikoHub-MinecraftBackUps/`), không đụng được gì khác
  trong Drive cá nhân — token bị lộ cũng không rò rỉ ảnh/tài liệu riêng tư.

Cấu hình đầy đủ (Client ID/Secret/refresh token) không nằm trong repo — xem
`secrets.txt` lưu local. Hướng dẫn tạo lại từ đầu:
https://rclone.org/drive/#making-your-own-client-id (nhớ dùng
`--drive-scope=drive.file` khi `rclone authorize`, và bấm **Publish app**
ở OAuth consent screen).

## 5. Đồng hồ hệ thống — `htpdate` thay cho NTP

Vì UDP/123 bị chặn (mục 1), `systemd-timesyncd` không bao giờ đồng bộ được,
để mặc định có thể khiến đồng hồ trôi lệch hàng chục phút mà không có cảnh
báo nào — phát hiện thực tế: lệch ~16 phút, đủ để làm timer 5h sáng chạy sai
giờ thật.

Đã tắt hẳn `systemd-timesyncd` (`timedatectl set-ntp false`), thay bằng
`htpdate` — lấy giờ từ HTTP header `Date:` của một site HTTPS bất kỳ
(`www.google.com`), hoạt động qua TCP/443 nên không bị chặn. Chạy qua
`htpdate-sync.timer` lúc 4h45 sáng — trước giờ backup 15 phút để đảm bảo
timestamp file backup và thời điểm chạy timer đều chính xác.

**Lưu ý đã gặp**: chỉ sửa system clock (`htpdate -s`) là chưa đủ — RTC
(đồng hồ phần cứng) vẫn giữ giá trị cũ, và có cơ chế (kernel hoặc hypervisor)
định kỳ đồng bộ ngược system clock từ RTC, khiến giờ bị lệch lại y hệt sau
một thời gian. Cần đảm bảo RTC cũng được cập nhật (máy này không có lệnh
`hwclock`, nhưng kernel tự đồng bộ RTC↔system time khi gọi `settimeofday`,
nên chỉ cần gọi `htpdate -s` là đủ trong trường hợp cụ thể này — nếu dùng
distro/kernel khác, kiểm tra lại bằng `timedatectl status`, so `RTC time`
với `Universal time`).

## 6. Giới hạn UDP — ảnh hưởng tới các tính năng tương lai

Vì hạ tầng chặn UDP toàn bộ (không riêng port 25565), các tính năng sau
**không triển khai được** trên VPS này, bất kể kỹ thuật xử lý thế nào:

| Tính năng | Lý do không được |
|---|---|
| Minecraft Bedrock Edition (qua Geyser+Floodgate) | RakNet chạy trên UDP, không có TCP fallback |
| Plugin voice chat (Simple Voice Chat, Mumble) | Dùng UDP cho audio stream |
| VPN tự host (WireGuard, OpenVPN chế độ UDP) | Cần UDP |
| K8s multi-node (nếu sau này containerize) | Overlay network (Flannel VXLAN, Calico) dùng UDP encapsulation |
| NTP chuẩn | UDP/123 — đã giải quyết bằng `htpdate` (mục 5) |

Nếu tương lai cần bất kỳ tính năng nào ở trên, giải pháp không phải "tìm
cách lách" trên hạ tầng này (đã thử và xác nhận không khả thi ở tầng router
của nhà cung cấp), mà là chuyển sang nhà cung cấp khác không chặn UDP cho
riêng phần đó.

## 7. Sự cố thật đã xảy ra (lessons learned)

**(a) Đổi OP level qua `/reload` làm crash server** — fix bằng cách bỏ hẳn
`/reload`, chỉ sửa `ops.json` trực tiếp, áp dụng lúc rejoin (mục 3.4).

**(b) `server.properties` bị revert về mặc định sau VPS crash đột ngột** —
`server-port` (về `25565`) và `online-mode` (về `true`) đều từng bị revert
riêng lẻ ở 2 lần crash khác nhau. Luôn kiểm tra lại 2 giá trị này sau bất kỳ
sự cố VPS/reboot bất thường nào.

**(c) `online-mode=true` tạm thời → nhân vật bị nhân đôi** — khi (b) xảy ra
và có người chơi có tài khoản Microsoft thật (không phải cracked) đăng nhập
trong lúc `online-mode=true`, server gán UUID thật (v4) cho họ thay vì UUID
offline (v3) như bình thường — tạo ra 1 "nhân vật" hoàn toàn mới, tách biệt
với nhân vật gốc đã có OP/inventory từ trước. Khi phát hiện, cần: xác nhận
UUID nào đang thật sự hoạt động (`list uuids` trong console), xoá file
`.dat` mồ côi trong `world/players/data/` và entry tương ứng trong
`usercache.json`.

**(d) `/op` khớp nhầm profile do không phân biệt hoa/thường** — xem mục 3.4.
2 tài khoản thật `NovaSeele` và `novaseele` (viết hoa/thường khác nhau,
KHÔNG phải cùng 1 người) bị dashboard set OP level đè lên nhau vì UI ban đầu
giả định chúng là lỗi chính tả của cùng 1 người — luôn xác nhận với người
dùng trước khi merge/xoá dữ liệu tưởng là trùng lặp.

## 8. Thư mục

- `dashboard/app.py` — dashboard web quản trị (SLP client, NBT parser, auth,
  quản lý người chơi, console, kiểm tra/cập nhật phiên bản).
- `dashboard/relay.py` — relay PROXY-protocol giữa nginx và Paper.
- `backup/world_backup.sh` — nén + upload world lên Google Drive.
- `systemd/*.service`, `systemd/*.timer` — 5 unit: `minecraft`,
  `mc-dashboard`, `mc-proxy-relay`, `mc-world-backup` (+ timer),
  `htpdate-sync` (+ timer).
- `nginx/stream-mc.conf` — đoạn `stream {}` dán vào `/etc/nginx/nginx.conf`.
- `scripts/run.sh` — script khởi động Paper (Aikar's flags).

## 9. Triển khai trên VPS mới (tóm tắt)

1. Cài Paper build mới nhất từ `fill.papermc.io`, đặt vào `/home/minecraft/`.
2. Copy `scripts/run.sh` vào `/home/minecraft/run.sh`, sửa tên file jar cho
   khớp bản đang dùng.
3. Copy `dashboard/*.py` vào `/opt/mc-dashboard/`, `backup/world_backup.sh`
   vào `/opt/mc-dashboard/backup/`.
4. Cài `libnginx-mod-stream` (`apt-get install libnginx-mod-stream`), dán
   nội dung `nginx/stream-mc.conf` vào `/etc/nginx/nginx.conf` (top-level).
   Đổi `listen 443` của các vhost khác trên máy thành
   `listen 127.0.0.1:18443 proxy_protocol;`.
5. Copy toàn bộ `systemd/*.service` + `*.timer` vào `/etc/systemd/system/`,
   `systemctl daemon-reload && systemctl enable --now minecraft mc-dashboard
   mc-proxy-relay mc-world-backup.timer htpdate-sync.timer`.
6. Tạo `/etc/mc-dashboard-auth.env` cho đăng nhập admin (xem
   `_load_auth_config` trong `dashboard/app.py`).
7. Cài `rclone`, cấu hình remote `gdrive:` theo mục 4.3.
8. Nếu NTP/UDP-123 bị chặn ở nhà cung cấp mới, kiểm tra bằng
   `journalctl -u systemd-timesyncd` — nếu timeout liên tục, áp dụng mục 5.

## 10. KHÔNG có trong repo này (cố ý)

- World save (`world/`) — xem mục 4.1.
- File jar Paper — tải trực tiếp từ PaperMC, không version trong git.
- Mọi secret/credential (mật khẩu, token, salt/hash, OAuth client secret) —
  xem `secrets.txt` lưu local, KHÔNG commit vào đây.
