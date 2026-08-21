#!/bin/bash
# Nén world Minecraft và đẩy lên Google Drive (qua rclone), giữ 7 bản gần nhất.
set -uo pipefail

WORLD_DIR="/home/minecraft/world"
TMP_DIR="/tmp/mc-backup"
DATE_TAG="$(date +%F_%H-%M)"
ARCHIVE_NAME="world-${DATE_TAG}.tar.gz"
REMOTE="gdrive:VnTaikoHub-MinecraftBackUps"
RETENTION_DAYS=7
LOG_TAG="mc-world-backup"
ARCHIVE_PATH="${TMP_DIR}/${ARCHIVE_NAME}"

mkdir -p "$TMP_DIR"
logger -t "$LOG_TAG" "Bắt đầu backup world"

# Luôn bật lại save-on khi script kết thúc, dù thành công hay lỗi giữa chừng
trap 'screen -p 0 -S minecraft -X eval "stuff \"save-on\015\"" || true' EXIT

screen -p 0 -S minecraft -X eval 'stuff "save-off\015"' || true
screen -p 0 -S minecraft -X eval 'stuff "save-all flush\015"' || true
sleep 10

tar -czf "$ARCHIVE_PATH" -C /home/minecraft world
tar_rc=$?
# tar exit 1 = "một số file thay đổi trong lúc đọc" — bình thường với server
# đang chạy dù đã save-off, không phải lỗi thật. Chỉ exit >=2 mới là lỗi thật.
if [ "$tar_rc" -ge 2 ]; then
    logger -t "$LOG_TAG" "LỖI: tar thất bại (exit $tar_rc)"
    rm -f "$ARCHIVE_PATH"
    exit 1
fi

# Bật ghi đĩa lại ngay sau khi tar xong, không chờ upload (trap vẫn sẽ chạy
# lại lúc script thoát, nhưng save-on gọi 2 lần không sao)
screen -p 0 -S minecraft -X eval 'stuff "save-on\015"' || true

if rclone copy "$ARCHIVE_PATH" "$REMOTE" --no-traverse; then
    logger -t "$LOG_TAG" "Upload thành công: ${ARCHIVE_NAME}"
else
    logger -t "$LOG_TAG" "LỖI: upload thất bại cho ${ARCHIVE_NAME}"
    rm -f "$ARCHIVE_PATH"
    exit 1
fi

rm -f "$ARCHIVE_PATH"
rclone delete "$REMOTE" --min-age "${RETENTION_DAYS}d" || true
logger -t "$LOG_TAG" "Hoàn tất backup world"
