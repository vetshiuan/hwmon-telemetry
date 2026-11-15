#!/bin/sh
# hwmon-hub deployment on Xiaomi RC01 (AX10000).
# Execute on the router AFTER the python image has been pulled successfully.
# Files must live on the SSD (/mnt/usb), NOT /data (only ~900K free there).
set -e

D=/mnt/usb/mi_docker/docker-binaries/docker
DIR=/mnt/usb/hwmon-hub
IMG=docker.m.daocloud.io/library/python:3.12-alpine

echo "== precheck =="
echo "arch: $(uname -m)"
printf "port 8089: "
(netstat -ltnp 2>/dev/null | grep ':8089 ' || echo "free")
printf "uhttpd docroot: "
(grep -rE 'option (home|cgi_prefix)' /etc/config/uhttpd 2>/dev/null || echo "not found")

if ! $D images 2>/dev/null | grep -q 'python'; then
  echo "STOP: python image not present. Pull it first (watch for slow/blocked download)."
  exit 1
fi

echo "== files in $DIR =="
ls -l "$DIR"

# 大模型用量聚合的离线缓存目录：容器内 /app 是只读挂载，必须单独给一个可写目录，
# 否则台式机离线时没有缓存可回退（router_hub.py 会退到 /app/ltx/.llm-cache，但会污染资源库目录）
mkdir -p "$DIR/cache"
# 长期归档目录：hub_longterm.db 永久保存分钟级硬件采样、日级硬件/模型/应用汇总
mkdir -p "$DIR/data"
# 告警引擎状态目录：push_state.json（日报去重/跨天重置）与 hub_push.log 必须落在 SSD 上。
# 不挂载的话，这两样只会写进容器可写层，重建容器即归零 —— 表现为「推送悄悄停了、日报漏发」。
mkdir -p "$DIR/state"

echo "== start container =="
$D rm -f hwmon-hub >/dev/null 2>&1 || true
$D run -d --name hwmon-hub \
  --network host \
  --restart unless-stopped \
  -v "$DIR":/app:ro \
  -v "$DIR/ltx":/app/ltx \
  -v "$DIR/cache":/app/cache \
  -v "$DIR/data":/app/data \
  -v "$DIR/state":/state \
  -w /app \
  -e HWMON_STATE_DIR=/state \
  -e HWMON_LAPTOP=http://192.0.2.11:8090 \
  -e HWMON_DESKTOP=http://192.0.2.12:8090 \
  -e HWMON_WOL=http://192.0.2.1:8088/cgi-bin/wol \
  -e HWMON_PORT=8089 \
  -e HWMON_LTX_SYNC=1 \
  -e HWMON_LTX_HOURS=6 \
  -e LTX_PROXY=http://127.0.0.1:7890 \
  -e TZ=Asia/Shanghai \
  "$IMG" \
  python -u /app/router_hub.py

echo "== verify =="
i=0
until wget -qO- http://127.0.0.1:8089/health 2>/dev/null; do
  i=$((i+1))
  [ "$i" -ge 5 ] && break
  sleep 2
done
if [ "$i" -ge 5 ]; then
  echo "health not ready; check: $D logs hwmon-hub"
fi
