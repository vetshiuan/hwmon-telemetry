#!/bin/sh
# /data/www/cgi-bin/dns — 全屋 DNS 模式切换（小米 RC01 / uhttpd CGI）
# GET  -> 只读状态；POST + action=fallback|restore -> 切换
# 切换前预检保底上游、切换后校验 UCI，再重启 dnsmasq。
ADG='127.0.0.1#5353'
RESOLV=/tmp/resolv.conf.auto
BASE=/mnt/usb/hwmon-hub/dhcp.adguard.bak
LOCK=/tmp/hwmon_dns.lock
COOL=/tmp/hwmon_dns.last

echo "Content-Type: application/json; charset=utf-8"
echo "Cache-Control: no-store"
echo ""

emit(){ printf '{"ok":%s,"mode":"%s","message":"%s"}\n' "$1" "$2" "$3"; }
mode(){
  if uci show dhcp.@dnsmasq[0] 2>/dev/null | grep -q "$ADG"; then
    echo adguard
  else
    echo fallback
  fi
}

# 首次运行保存 AdGuard 模式基准快照到 SSD（不占 /data 的 880K）
[ -f "$BASE" ] || cp /etc/config/dhcp "$BASE" 2>/dev/null

case "$QUERY_STRING" in
  *action=fallback*) ACT=fallback ;;
  *action=restore*)  ACT=restore ;;
  *)                 ACT=status ;;
esac

if [ "$ACT" = status ] || [ "$REQUEST_METHOD" != POST ]; then
  emit true "$(mode)" "只读状态"
  exit 0
fi

# 预检：保底模式必须有可用上游，否则拒绝切换（防止全屋断 DNS）
if [ "$ACT" = fallback ] && ! grep -q '^nameserver' "$RESOLV" 2>/dev/null; then
  emit false "$(mode)" "保底上游文件为空，已拒绝切换（防止全屋断 DNS）"
  exit 0
fi

NOW=$(date +%s)
LAST=$(cat "$COOL" 2>/dev/null || echo 0)
if [ $((NOW-LAST)) -lt 10 ]; then
  emit false "$(mode)" "操作太频繁，请 10 秒后再试"
  exit 0
fi
mkdir "$LOCK" 2>/dev/null || { emit false "$(mode)" "上一次操作尚未结束"; exit 0; }
trap 'rmdir "$LOCK" 2>/dev/null' 0

if [ "$ACT" = fallback ]; then
  # 删掉 AdGuard 上游后，dnsmasq 自动使用 resolv-file 中的运营商 DNS
  uci del_list dhcp.@dnsmasq[0].server="$ADG" 2>/dev/null
  uci delete dhcp.@dnsmasq[0].strictorder 2>/dev/null
  uci set dhcp.@dnsmasq[0].allservers='1' 2>/dev/null
  WANT=fallback
else
  # 幂等：先删再加，避免 server 列表出现重复项
  uci del_list dhcp.@dnsmasq[0].server="$ADG" 2>/dev/null
  uci add_list dhcp.@dnsmasq[0].server="$ADG" 2>/dev/null
  uci set dhcp.@dnsmasq[0].strictorder='1' 2>/dev/null
  uci set dhcp.@dnsmasq[0].allservers='0' 2>/dev/null
  WANT=adguard
fi

if ! uci commit dhcp 2>/dev/null; then
  emit false "$(mode)" "uci commit 失败，请检查 /data 剩余空间"
  exit 0
fi

GOT=$(mode)
if [ "$GOT" != "$WANT" ]; then
  emit false "$GOT" "提交后状态不符，未重启 dnsmasq；基准备份：$BASE"
  exit 0
fi

if ! /etc/init.d/dnsmasq restart >/dev/null 2>&1; then
  emit false "$GOT" "dnsmasq 重启失败，请检查 /etc/init.d/dnsmasq"
  exit 0
fi
echo "$NOW" > "$COOL"
emit true "$GOT" "切换完成，dnsmasq 已重启"
