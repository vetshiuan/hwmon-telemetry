#!/bin/sh
# /data/www/cgi-bin/peers — 路由器本地 EasyTier 节点列表（uhttpd CGI）
# Local 节点即路由器本身；供 hwmon-hub 反代，不依赖笔记本在线。
echo "Content-Type: application/json; charset=utf-8"
echo "Cache-Control: no-store"
echo ""
exec /mnt/usb/mi_docker/docker-binaries/docker exec easytier easytier-cli -o json peer
