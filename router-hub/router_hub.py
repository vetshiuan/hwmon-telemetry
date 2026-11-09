#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
路由器侧聚合入口 hwmon-router-hub。

纯 Python 标准库，无第三方依赖，可跑在 python:3.12-alpine 里。
职责：
  1. 托管工具页 index.html / 监控页 monitor.html
  2. /api/status    探测笔记本与台式是否在线
  3. /api/easytier  反代路由器的 peer 列表；离线时返回最后一次拓扑
  4. /wol (POST)    代理路由器的 WOL CGI，60 秒防抖
  5. 告警引擎（2026-11-09 从笔记本 push_monitor.py 迁移而来）
     所有告警判定都在路由器上跑 —— 笔记本掉线时依然能推送告警。
     这是迁移的核心动机：原来推送器在笔记本上，笔记本一挂就没人报警。

硬件监控主页仍由两台 PC 各自的 agent.py 提供，本服务不采集任何传感器；
传感器必须由 PC 本机读取（LHM / GPU-Z / Win32 API），这是硬件限制。
"""
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LAPTOP = os.environ.get("HWMON_LAPTOP", "http://127.0.0.1:8090").rstrip("/")
DESKTOP = os.environ.get("HWMON_DESKTOP", "http://127.0.0.1:8090").rstrip("/")
WOL_URL = os.environ.get("HWMON_WOL", "http://127.0.0.1:8088/cgi-bin/wol")
DNS_CGI = os.environ.get("HWMON_DNS_CGI", "http://127.0.0.1:8088/cgi-bin/dns")
PEERS_CGI = os.environ.get("HWMON_PEERS_CGI", "http://127.0.0.1:8088/cgi-bin/peers")
PC_URLS = {"laptop": LAPTOP, "desktop": DESKTOP}
PROXY_TIMEOUT = float(os.environ.get("HWMON_PROXY_TIMEOUT", "5"))
PORT = int(os.environ.get("HWMON_PORT", "8089"))
PROBE_TTL = float(os.environ.get("HWMON_PROBE_TTL", "5"))
PEER_TTL = float(os.environ.get("HWMON_PEER_TTL", "10"))
TIMEOUT = float(os.environ.get("HWMON_TIMEOUT", "3"))

# ---- 告警引擎配置 ----
PUSH_CFG = os.environ.get("HWMON_PUSH_CFG", os.path.join(BASE_DIR, "push_config.json"))
STATE_DIR = os.environ.get("HWMON_STATE_DIR", "/state")
PUSH_STATE = os.path.join(STATE_DIR, "push_state.json")
PUSH_LOG = os.path.join(STATE_DIR, "hub_push.log")
PUSH_LOG_MAX = 512 * 1024

# 容器时区通常是 UTC；日报时间按北京时间算，不依赖 tzdata
CN_TZ = timezone(timedelta(hours=8))

_lock = threading.Lock()
_state = {"ts": 0.0, "laptop": False, "desktop": False}
_peers = {"ts": 0.0, "data": None}
_wol = {"ts": 0.0, "result": None}


def _get(url, timeout=None):
    req = urllib.request.Request(url, headers={"User-Agent": "hwmon-router-hub/1.0"})
    with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as r:
        return r.status, r.read()


def probe(url):
    try:
        status, _ = _get(url + "/health", 1.5)
        return status == 200
    except Exception:
        return False


def device_status():
    """在线状态缓存 5 秒。

    两台必须并行探测：串行时一台掉线会让另一台陪着等 1.5 秒，
    总耗时超过前端 2.5 秒超时后整页会降级成 static 模式，
    连带 EasyTier 数据也去问笔记本（而笔记本恰好就是掉的那台）。
    """
    now = time.time()
    with _lock:
        if now - _state["ts"] < PROBE_TTL:
            return dict(_state)
    res = {}

    def _probe(dev, url):
        res[dev] = probe(url)

    threads = [threading.Thread(target=_probe, args=("laptop", LAPTOP)),
               threading.Thread(target=_probe, args=("desktop", DESKTOP))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    st = {"ts": now,
          "laptop": bool(res.get("laptop")),
          "desktop": bool(res.get("desktop"))}
    with _lock:
        _state.update(st)
    return st


def easytier_payload():
    """优先读路由器自己的 EasyTier（Local 即路由器），失败回退笔记本。"""
    now = time.time()
    with _lock:
        cached = _peers["data"]
        if cached is not None and now - _peers["ts"] < PEER_TTL:
            return dict(cached, cached=True, stale=False)
    last_err = None
    for url, origin in ((PEERS_CGI, "router"),
                        (LAPTOP + "/api/easytier", "laptop")):
        try:
            # 路由器 CGI 每次要 docker exec，拓扑震荡时会变慢，给足时间
            _, body = _get(url, 8 if origin == "router" else TIMEOUT)
            data = json.loads(body)
            if isinstance(data, list):
                data = {"ok": True, "peers": data}
            if not isinstance(data, dict) or not isinstance(data.get("peers"), list):
                raise RuntimeError("unexpected payload")
            data.update(source=origin, offline=False, stale=False)
            with _lock:
                _peers.update(ts=time.time(), data=data)
            return data
        except Exception as e:
            last_err = str(e)[:160]
    if cached is not None:
        return dict(cached, cached=True, stale=True, error=last_err)
    return {"ok": False, "peers": [], "offline": True,
            "error": last_err, "ts": now}


def trigger_wol():
    """代理路由器 WOL CGI；60 秒内重复点击直接返回上次结果。"""
    now = time.time()
    with _lock:
        prev = _wol["result"]
        if prev is not None and now - _wol["ts"] < 60:
            return dict(prev, throttled=True)
    result = {"ok": False, "http_status": None, "error": None,
              "target": "DESKTOP-PC", "ts": now}
    try:
        req = urllib.request.Request(
            WOL_URL, headers={"User-Agent": "hwmon-router-hub/1.0"}
        )
        with urllib.request.urlopen(req, timeout=6) as r:
            result["http_status"] = getattr(r, "status", 200)
            r.read(2048)
        result["ok"] = True
    except Exception as e:
        result["http_status"] = getattr(e, "code", None)
        result["error"] = str(e)[:200]
    with _lock:
        _wol.update(ts=now, result=result)
    return result


def dns_call(action, method="GET"):
    """反代路由器的 DNS 切换 CGI；切换动作由 uhttpd 执行，不依赖本容器。"""
    url = DNS_CGI + "?action=" + action
    req = urllib.request.Request(
        url,
        data=b"" if method == "POST" else None,
        method=method,
        headers={"User-Agent": "hwmon-router-hub/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, r.read()


def proxy_pc(dev, sub_path, query):
    """把 /hw/<dev>/... 反代到对应 PC 的 agent；离线返回结构化 503。"""
    base = PC_URLS.get(dev)
    if not base:
        return 404, {"ok": False, "error": "unknown device: " + dev}
    url = base + sub_path
    if query:
        url += "?" + query
    try:
        status, body = _get(url, PROXY_TIMEOUT)
        return status, body
    except Exception as e:
        return 503, {"ok": False, "offline": True, "device": dev,
                     "error": str(e)[:160]}


# =====================================================================
# 告警引擎（迁移自笔记本 C:\hwsensor\push_monitor.py）
# =====================================================================
DEFAULT_PUSH_CFG = {
    "channel": "wecom",
    "wecom_webhook": "",
    "poll_seconds": 30,
    "daily_report": True,
    "daily_report_time": "21:00",
    "alert_repeat_minutes": 5,
    "confirm_samples": 2,
    "recover_samples": 3,
    "flap_guard_minutes": 10,
    "offline_confirm": 3,
    "devices": [
        {
            "id": "desktop",
            "name": "台式机",
            "enabled": True,
            "offline_alert": False,
            "source_alert": True,
            "rules": [
                {"key": "gpu.hot", "label": "GPU 热点", "op": ">=", "value": 108, "unit": "°C"},
                {"key": "gpu.core", "label": "GPU 核心", "op": ">=", "value": 88, "unit": "°C"},
                {"key": "gpu.mem", "label": "GPU 显存", "op": ">=", "value": 100, "unit": "°C"},
                {"key": "cpu.temp", "label": "CPU 温度", "op": ">=", "value": 95, "unit": "°C"},
                {"key": "gpu.pin_volt", "label": "16pin 电压", "op": "<", "value": 11.6, "unit": "V"},
                {"key": "gpu.pin_volt", "label": "16pin 电压", "op": ">", "value": 12.8, "unit": "V"},
                {"key": "mem.pct", "label": "内存占用", "op": ">=", "value": 95, "unit": "%"},
            ],
        },
        {
            "id": "laptop",
            "name": "笔记本",
            "enabled": True,
            "offline_alert": True,
            "rules": [
                {"key": "mem.pct", "label": "内存占用", "op": ">=", "value": 95, "unit": "%"},
            ],
        },
    ],
}


def plog(msg):
    """告警引擎日志，落在 /state，超限自轮转。"""
    line = "[%s] %s" % (datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        if not os.path.isdir(STATE_DIR):
            os.makedirs(STATE_DIR, exist_ok=True)
        if os.path.exists(PUSH_LOG) and os.path.getsize(PUSH_LOG) > PUSH_LOG_MAX:
            with open(PUSH_LOG, "r", encoding="utf-8", errors="ignore") as f:
                tail = f.readlines()[-2000:]
            with open(PUSH_LOG, "w", encoding="utf-8") as f:
                f.writelines(tail)
        with open(PUSH_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line, flush=True)
    except Exception:
        pass


def load_push_cfg():
    cfg = json.loads(json.dumps(DEFAULT_PUSH_CFG))
    try:
        with open(PUSH_CFG, "r", encoding="utf-8") as f:
            user = json.load(f)
    except Exception as e:
        plog("未读到 %s（%s），使用内置默认规则" % (PUSH_CFG, str(e)[:80]))
        return cfg
    for k, v in user.items():
        if k != "devices":
            cfg[k] = v
    if isinstance(user.get("devices"), list) and user["devices"]:
        cfg["devices"] = user["devices"]
    return cfg


def load_push_state():
    try:
        with open(PUSH_STATE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_push_state(d):
    try:
        if not os.path.isdir(STATE_DIR):
            os.makedirs(STATE_DIR, exist_ok=True)
        with open(PUSH_STATE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        plog("状态写入失败: %s" % str(e)[:100])


def dig(obj, path):
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


def cmp_val(value, op, target):
    if value is None:
        return False
    if op == ">=":
        return value >= target
    if op == "<=":
        return value <= target
    if op == ">":
        return value > target
    if op == "<":
        return value < target
    return False


def fmt_val(value, unit="", dp=1):
    if value is None:
        return "--"
    if isinstance(value, float):
        return ("%." + str(dp) + "f") % value + unit
    return str(value) + unit


def wecom_send(webhook, title, content, timeout=12):
    if not webhook:
        plog("DRY-RUN 未配置 webhook：%s" % title)
        return False
    color = "comment"
    if any(k in title for k in ("【告警】", "【离线】", "【警告】")):
        color = "warning"
    elif "【恢复】" in title:
        color = "info"
    md = '<font color="%s">**%s**</font>\n%s' % (color, title, content)
    if len(md.encode("utf-8")) > 4000:
        md = md.encode("utf-8")[:3900].decode("utf-8", errors="ignore") + "\n…"
    body = json.dumps({"msgtype": "markdown", "markdown": {"content": md}},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode("utf-8", errors="replace"))
        if resp.get("errcode") == 0:
            plog("推送成功：%s" % title)
            return True
        plog("推送被拒 errcode=%s errmsg=%s" % (resp.get("errcode"), resp.get("errmsg")))
        return False
    except Exception as e:
        plog("推送失败: %s" % str(e)[:160])
        return False


class MetricState(object):
    """单个指标的越限状态机，抑制抖动与重复推送。"""

    def __init__(self):
        self.hit_count = 0
        self.clear_count = 0
        self.active = False
        self.last_push = 0.0
        self.last_recover = 0.0

    def update(self, violated, now, confirm, recover, flap_guard):
        if violated:
            self.hit_count += 1
            self.clear_count = 0
            if not self.active and self.hit_count >= confirm:
                if (now - self.last_recover) < flap_guard * 60:
                    plog("抖动抑制：指标 %ds 前刚恢复，不重复告警" % int(now - self.last_recover))
                    self.active = True
                    self.last_push = now
                    return None
                self.active = True
                self.last_push = now
                return "enter"
            return None
        self.clear_count += 1
        self.hit_count = 0
        if self.active and self.clear_count >= recover:
            self.active = False
            self.last_recover = now
            return "recover"
        return None

    def due_repeat(self, now, repeat_seconds):
        return self.active and (now - self.last_push) >= repeat_seconds


def blank_stats():
    return {"cpu_max": None, "gpu_hot_max": None, "gpu_power_max": None,
            "mem_max": None, "alerts": 0, "offline": 0}


def build_report(devices, stats, offline):
    lines = []
    total = 0
    for d in devices:
        s = stats.get(d["id"]) or blank_stats()
        total += s["alerts"]
        parts = []
        if s["gpu_hot_max"] is not None:
            parts.append("GPU 热点最高 %s" % fmt_val(s["gpu_hot_max"], "°C"))
        if s["cpu_max"] is not None:
            parts.append("CPU 最高 %s" % fmt_val(s["cpu_max"], "°C"))
        if s["gpu_power_max"] is not None:
            parts.append("GPU 峰值 %s" % fmt_val(s["gpu_power_max"], "W", 0))
        if s["mem_max"] is not None:
            parts.append("内存最高 %s" % fmt_val(s["mem_max"], "%"))
        if not parts:
            parts.append("当天未取到数据")
        if s["offline"]:
            parts.append("离线 %d 次" % s["offline"])
        parts.append("当前离线" if offline.get(d["id"], {}).get("active") else "当前在线")
        lines.append("%s  %s" % (d.get("name", d["id"]), " · ".join(parts)))
    lines.append("越线告警 %d 次" % total if total else "当天没有越线告警")
    lines.append("告警引擎运行在路由器（笔记本宕机不影响推送）")
    return "\n".join(lines)


def source_alert_event(dev_id, name, snap, state, enabled):
    """GPU-Z 数据缺失状态机：只在「有->无 / 无->有」时返回消息。"""
    if not enabled:
        return None
    expected = (snap.get("capabilities") or {}).get("gpuz_expected", dev_id == "desktop")
    if not expected:
        return None
    gpuz_ok = bool((snap.get("sources") or {}).get("gpuz"))
    prev = state.get(dev_id)
    state[dev_id] = gpuz_ok
    stamp = datetime.now(CN_TZ).strftime("%m-%d %H:%M")
    if prev is True and not gpuz_ok:
        return ("【警告】%s GPU-Z 数据中断" % name,
                "%s 的 GPU-Z 数据不可用，16pin 电压等读数缺失。\n"
                "若是你主动关闭 GPU-Z，可忽略本条。\n时间: %s" % (name, stamp))
    if prev is False and gpuz_ok:
        return ("【恢复】%s GPU-Z 数据已恢复" % name,
                "%s 的 GPU-Z 数据已恢复。\n时间: %s" % (name, stamp))
    return None


def alert_loop():
    """常驻告警线程：与 HTTP 服务并行，路由器 24h 常开作为哨兵。"""
    time.sleep(20)  # 等 HTTP 服务先起来
    cfg = load_push_cfg()
    webhook = (cfg.get("wecom_webhook") or "").strip()
    poll = max(10, int(cfg.get("poll_seconds", 30)))
    repeat_s = max(1, int(cfg.get("alert_repeat_minutes", 5))) * 60
    confirm = max(1, int(cfg.get("confirm_samples", 2)))
    recover = max(1, int(cfg.get("recover_samples", 3)))
    flap = int(cfg.get("flap_guard_minutes", 10))
    offline_confirm = max(1, int(cfg.get("offline_confirm", 3)))
    rep_on = bool(cfg.get("daily_report", True))
    rep_at = str(cfg.get("daily_report_time", "21:00")).strip() or "21:00"

    devices = [d for d in cfg.get("devices", []) if d.get("enabled", True)]
    if not devices:
        plog("没有启用任何设备，告警引擎退出")
        return

    plog("=== 告警引擎启动（路由器） ===")
    plog("设备 %d 台，轮询 %ds，重复告警 %dmin，日报 %s，webhook %s"
         % (len(devices), poll, repeat_s // 60,
            ("每天 " + rep_at) if rep_on else "关闭",
            "已配置" if webhook else "未配置(DRY-RUN)"))

    states = {}
    for d in devices:
        for i, _ in enumerate(d.get("rules") or []):
            states["%s#%d" % (d["id"], i)] = MetricState()

    source_state = {}
    offline = {d["id"]: {"fails": 0, "active": False} for d in devices}
    stats = {d["id"]: blank_stats() for d in devices}
    stats_day = datetime.now(CN_TZ).date()

    pstate = load_push_state()
    last_report_day = None
    if pstate.get("last_report_day"):
        try:
            last_report_day = datetime.strptime(pstate["last_report_day"], "%Y-%m-%d").date()
            plog("当天日报已发过（%s），本次重启不补推" % pstate["last_report_day"])
        except ValueError:
            last_report_day = None

    while True:
        now = time.time()
        alert_lines = []

        for d in devices:
            dev_id = d["id"]
            name = d.get("name", dev_id)
            base = PC_URLS.get(dev_id) or (d.get("url") or "").rstrip("/")
            if not base:
                continue
            offline_alert = bool(d.get("offline_alert", True))

            try:
                _, body = _get(base + "/api/snapshot", 8)
                snap = json.loads(body)
                offline[dev_id]["fails"] = 0
                if offline[dev_id]["active"]:
                    offline[dev_id]["active"] = False
                    if offline_alert:
                        wecom_send(webhook, "【恢复】%s 重新在线" % name,
                                   "%s 于 %s 恢复响应。\n通过路由器检测。"
                                   % (name, datetime.now(CN_TZ).strftime("%m-%d %H:%M")))
            except Exception as e:
                offline[dev_id]["fails"] += 1
                if offline[dev_id]["fails"] >= offline_confirm and not offline[dev_id]["active"]:
                    offline[dev_id]["active"] = True
                    stats[dev_id]["offline"] += 1
                    if offline_alert:
                        wecom_send(webhook, "【离线】%s 采集服务失联" % name,
                                   "%s 连续 %d 次请求失败（约 %d 分钟）。\n最后一次错误: %s\n"
                                   "可能原因：机器关闭 / EasyTier 掉线 / agent 未运行。"
                                   % (name, offline[dev_id]["fails"],
                                      offline[dev_id]["fails"] * poll // 60, str(e)[:120]))
                continue

            ev = source_alert_event(dev_id, name, snap, source_state, d.get("source_alert"))
            if ev:
                wecom_send(webhook, ev[0], ev[1])

            for key, field in (("cpu_max", "cpu.temp"), ("gpu_hot_max", "gpu.hot"),
                               ("gpu_power_max", "gpu.power"), ("mem_max", "mem.pct")):
                v = dig(snap, field)
                if isinstance(v, (int, float)):
                    cur = stats[dev_id][key]
                    if cur is None or v > cur:
                        stats[dev_id][key] = v

            for i, rule in enumerate(d.get("rules") or []):
                st = states["%s#%d" % (dev_id, i)]
                val = dig(snap, rule["key"])
                violated = cmp_val(val, rule["op"], rule["value"])
                e = st.update(violated, now, confirm, recover, flap)

                if e == "enter":
                    stats[dev_id]["alerts"] += 1
                    alert_lines.append(
                        "%s %s %s%s（推送线 %s%s）\n设备: %s"
                        % (name, rule["label"], fmt_val(val), rule.get("unit", ""),
                           rule["op"], fmt_val(rule["value"]), snap.get("device")))
                elif e == "recover":
                    wecom_send(webhook, "【恢复】%s %s 已回落" % (name, rule["label"]),
                               "%s 的 %s 已回落到 %s%s，恢复正常。\n设备: %s"
                               % (name, rule["label"], fmt_val(val),
                                  rule.get("unit", ""), snap.get("device")))
                elif st.due_repeat(now, repeat_s):
                    st.last_push = now
                    alert_lines.append(
                        "%s %s %s%s 持续（推送线 %s%s）"
                        % (name, rule["label"], fmt_val(val), rule.get("unit", ""),
                           rule["op"], fmt_val(rule["value"])))

        if alert_lines:
            wecom_send(webhook, "【告警】硬件指标越线",
                       "\n\n".join(alert_lines) + "\n\n时间: %s"
                       % datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"))

        today = datetime.now(CN_TZ).date()
        if today != stats_day:
            stats_day = today
            stats = {d["id"]: blank_stats() for d in devices}
            plog("跨天，日报统计已重置")

        if rep_on:
            hhmm = datetime.now(CN_TZ).strftime("%H:%M")
            if hhmm >= rep_at and last_report_day != today:
                last_report_day = today
                ok = wecom_send(webhook, "【日报】硬件监控 %s" % today.strftime("%m-%d"),
                                build_report(devices, stats, offline))
                if ok:
                    pstate["last_report_day"] = today.isoformat()
                    save_push_state(pstate)

        time.sleep(poll)


class Handler(BaseHTTPRequestHandler):
    server_version = "hwmon-router-hub/1.0"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cache-Control", "no-store")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _serve(self, name, ctype):
        fp = os.path.join(BASE_DIR, name)
        if not os.path.exists(fp):
            self.send_error(404, "%s not found" % name)
            return
        with open(fp, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _dns(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        action = (q.get("action") or ["status"])[0]
        if action not in ("status", "fallback", "restore"):
            return self._json({"ok": False, "error": "bad action"}, 400)
        method = "POST" if (self.command == "POST" and action != "status") else "GET"
        try:
            _, body = dns_call(action, method)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            return self._json({"ok": False, "error": str(e)[:200]}, 502)

    def _hw(self):
        u = urllib.parse.urlparse(self.path)
        dev, _, sub = u.path[len("/hw/"):].partition("/")
        code, body = proxy_pc(dev, "/" + sub if sub else "/", u.query)
        raw = (json.dumps(body, ensure_ascii=False).encode("utf-8")
               if isinstance(body, dict) else body)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/favicon.ico":
            # 浏览器默认请求图标；返回空响应，避免控制台刷 404。
            self.send_response(204)
            self.end_headers()
            return
        if path.startswith("/hw/"):
            return self._hw()
        if path in ("/", "/index.html", "/monitor", "/monitor.html"):
            return self._serve("monitor.html", "text/html; charset=utf-8")
        if path in ("/tools", "/tools.html"):
            return self._serve("index.html", "text/html; charset=utf-8")
        if path == "/api/status":
            st = device_status()
            return self._json({"ok": True, **st,
                               "laptop_url": LAPTOP, "desktop_url": DESKTOP})
        if path == "/api/easytier":
            d = dict(easytier_payload())
            d["viewer_ip"] = self.client_address[0]
            return self._json(d)
        if path == "/api/dns":
            return self._dns()
        if path == "/api/push":
            # 只读诊断：不返回 webhook 明文
            cfg = load_push_cfg()
            return self._json({
                "ok": True,
                "configured": bool((cfg.get("wecom_webhook") or "").strip()),
                "devices": [{"id": d.get("id"), "name": d.get("name"),
                             "rules": len(d.get("rules") or []),
                             "offline_alert": bool(d.get("offline_alert", True))}
                            for d in cfg.get("devices", [])],
                "daily_report": cfg.get("daily_report"),
                "daily_report_time": cfg.get("daily_report_time"),
                "state_dir": STATE_DIR,
            })
        if path == "/health":
            return self._json({"ok": True, "service": "hwmon-router-hub"})
        self.send_error(404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/wol", "/api/wol"):
            return self._json(trigger_wol())
        if path == "/api/dns":
            return self._dns()
        self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=alert_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("hwmon-router-hub listening on 0.0.0.0:%d" % PORT, flush=True)
    print("  laptop=%s  desktop=%s" % (LAPTOP, DESKTOP), flush=True)
    print("  wol=%s" % WOL_URL, flush=True)
    print("  alert engine: state=%s cfg=%s" % (STATE_DIR, PUSH_CFG), flush=True)
    srv.serve_forever()
