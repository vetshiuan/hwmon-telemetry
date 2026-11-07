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

echo "== start container =="
$D rm -f hwmon-hub >/dev/null 2>&1 || true
$D run -d --name hwmon-hub \
  --network host \
  --restart unless-stopped \
  -v "$DIR":/app:ro \
  -w /app \
  -e HWMON_LAPTOP=http://10.1.1.251:8090 \
  -e HWMON_DESKTOP=http://10.1.1.252:8090 \
  -e HWMON_WOL=http://10.1.1.254:8088/cgi-bin/wol \
  -e HWMON_PORT=8089 \
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
