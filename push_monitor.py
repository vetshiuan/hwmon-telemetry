#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
硬件监控推送器 — 轮询两台 agent，只在真故障时推送，另加每天一条日报。

设计要点
--------
1. 独立进程运行，不依附 agent.py：agent 崩掉时恰恰最需要告警。
2. 推送阈值独立于网页 ALERT_RULES：网页要"早提示"，推送要"真出事才响"。
   跑大模型 GPU 热点 96°C 属正常，推送线设在 108°C。
3. 不发心跳：越线告警、持续告警（5 分钟重复）、恢复、离线、重新在线才推。
   心跳的替代是每天一条日报——它同时充当"推送器还活着"的信号。
4. token 为空时进入 dry-run：只写日志不发送，便于先跑通逻辑。

用法
----
    python push_monitor.py            # 前台运行（调试用）
    pythonw push_monitor.py           # 后台无窗口（自启动用）

日志：同目录 push_monitor.log
"""

import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "push_config.json")
STATE_PATH = os.path.join(BASE_DIR, "push_state.json")
LOG_PATH = os.path.join(BASE_DIR, "push_monitor.log")

POLL_SECONDS = 30
LOG_MAX_BYTES = 512 * 1024

DEFAULT_CONFIG = {
    "channel": "wecom",
    "wecom_webhook": "",
    "pushplus_token": "",
    "poll_seconds": 30,
    "daily_report": True,
    "daily_report_time": "21:00",
    "alert_repeat_minutes": 5,
    "confirm_samples": 2,
    "recover_samples": 3,
    "flap_guard_minutes": 10,
    "devices": [
        {
            "id": "desktop",
            "name": "台式机",
            "url": "http://10.1.1.252:8090",
            "enabled": True,
            # 台式机不常开：离线/重新在线不推送，日报里照常记录状态
            "offline_alert": False,
            # GPU-Z 数据缺失按警告级推送；用户会主动关 GPU-Z，故只在状态变化时推
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
            "url": "http://127.0.0.1:8090",
            "enabled": True,
            "rules": [
                {"key": "mem.pct", "label": "内存占用", "op": ">=", "value": 95, "unit": "%"},
            ],
        },
    ],
}


# ---------------- 日志 ----------------

def log(msg, also_print=True):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    if also_print:
        try:
            print(line, flush=True)
        except Exception:
            pass
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
                tail = f.readlines()[-2000:]
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                f.writelines(tail)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------- 配置 ----------------

def load_config():
    if not os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
            log("已生成默认配置 %s，请填入 pushplus_token" % CONFIG_PATH)
        except Exception as e:
            log("写默认配置失败: %s" % e)
        return DEFAULT_CONFIG
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
    except Exception as e:
        log("读取配置失败，回退默认值: %s" % e)
        return DEFAULT_CONFIG
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg.update({k: v for k, v in user_cfg.items() if k != "devices"})
    if isinstance(user_cfg.get("devices"), list) and user_cfg["devices"]:
        cfg["devices"] = user_cfg["devices"]
    return cfg


# ---------------- 运行状态（跨重启保留） ----------------

def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(data):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log("写状态文件失败: %s" % e)


# ---------------- 取数 ----------------

def fetch_json(url, timeout=8):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": "hwmon-push/1.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def dig(obj, path):
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


# ---------------- 推送 ----------------

def pushplus(token, title, content, timeout=12):
    if not token:
        log("DRY-RUN 未配置 token，跳过发送：%s | %s" % (title, content.replace("\n", " ")[:120]))
        return False
    body = json.dumps(
        {"token": token, "title": title, "content": content, "template": "txt"},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://www.pushplus.plus/send",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode("utf-8", errors="replace"))
        code = resp.get("code")
        if code == 200:
            log("推送成功：%s" % title)
            return True
        log("推送被拒 code=%s msg=%s" % (code, resp.get("msg")))
        return False
    except Exception as e:
        log("推送失败: %s" % e)
        return False


def wecom_send(webhook, title, content, timeout=12):
    """企业微信群机器人。markdown 支持 info/comment/warning 三种颜色。"""
    if not webhook:
        log("DRY-RUN 未配置 webhook，跳过发送：%s | %s"
            % (title, content.replace("\n", " ")[:120]))
        return False
    color = "comment"
    if any(k in title for k in ("【告警】", "【离线】", "【警告】")):
        color = "warning"
    elif "【恢复】" in title:
        color = "info"
    md = '<font color="%s">**%s**</font>\n%s' % (color, title, content)
    if len(md.encode("utf-8")) > 4000:
        md = md.encode("utf-8")[:3900].decode("utf-8", errors="ignore") + "\n…"
    body = json.dumps(
        {"msgtype": "markdown", "markdown": {"content": md}}, ensure_ascii=False
    ).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode("utf-8", errors="replace"))
        if resp.get("errcode") == 0:
            log("推送成功：%s" % title)
            return True
        log("推送被拒 errcode=%s errmsg=%s" % (resp.get("errcode"), resp.get("errmsg")))
        return False
    except Exception as e:
        log("推送失败: %s" % e)
        return False


_NOTIFIER = None


def init_notifier(cfg):
    global _NOTIFIER
    channel = (cfg.get("channel") or "wecom").strip().lower()
    if channel == "wecom":
        url = (cfg.get("wecom_webhook") or "").strip()
        _NOTIFIER = lambda t, c: wecom_send(url, t, c)
        log("通知渠道：企业微信群机器人（%s）" % ("已配置" if url else "未配置，DRY-RUN"))
    else:
        tok = (cfg.get("pushplus_token") or "").strip()
        _NOTIFIER = lambda t, c: pushplus(tok, t, c)
        log("通知渠道：PushPlus（%s）" % ("已配置" if tok else "未配置，DRY-RUN"))


def notify(title, content):
    if _NOTIFIER is None:
        log("DRY-RUN 未初始化通知器：%s" % title)
        return False
    return _NOTIFIER(title, content)


# ---------------- 判定 ----------------

def compare(value, op, target):
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


class MetricState:
    """单个指标的越限状态机，用于抑制抖动与重复推送。"""

    def __init__(self):
        self.hit_count = 0
        self.clear_count = 0
        self.active = False
        self.last_push = 0.0
        self.last_recover = 0.0

    def update(self, violated, now, confirm, recover, flap_guard):
        """返回 'enter' / 'repeat' / 'recover' / None"""
        if violated:
            self.hit_count += 1
            self.clear_count = 0
            if not self.active and self.hit_count >= confirm:
                if (now - self.last_recover) < flap_guard * 60:
                    log("抖动抑制：指标 %ds 前刚恢复，不重复告警" % int(now - self.last_recover))
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


# ---------------- 主循环 ----------------

def blank_stats():
    """一台设备当天的统计槽位。"""
    return {"cpu_max": None, "gpu_hot_max": None, "gpu_power_max": None,
            "mem_max": None, "alerts": 0, "offline": 0}


def build_report(devices, stats, offline=None):
    """生成日报正文（不含标题）。离线静默的设备仍如实显示在线状态。"""
    lines = []
    total_alerts = 0
    for d in devices:
        s = stats.get(d["id"]) or blank_stats()
        total_alerts += s["alerts"]
        parts = []
        if s["gpu_hot_max"] is not None:
            parts.append("GPU 热点最高 %s" % fmt(s["gpu_hot_max"], "°C"))
        if s["cpu_max"] is not None:
            parts.append("CPU 最高 %s" % fmt(s["cpu_max"], "°C"))
        if s["gpu_power_max"] is not None:
            parts.append("GPU 峰值 %s" % fmt(s["gpu_power_max"], "W", 0))
        if s["mem_max"] is not None:
            parts.append("内存最高 %s" % fmt(s["mem_max"], "%"))
        if not parts:
            parts.append("当天未取到数据")
        if s["offline"]:
            parts.append("离线 %d 次" % s["offline"])
        if offline is not None:
            parts.append("当前离线" if offline.get(d["id"], {}).get("active") else "当前在线")
        lines.append("%s  %s" % (d.get("name", d["id"]), " · ".join(parts)))
    lines.append("越线告警 %d 次" % total_alerts if total_alerts else "当天没有越线告警")
    lines.append("推送器运行正常")
    return "\n".join(lines)


def fmt(value, unit="", dp=1):
    if value is None:
        return "--"
    if isinstance(value, float):
        return ("%." + str(dp) + "f") % value + unit
    return str(value) + unit


def source_alert_event(dev_id, name, snap, state, enabled):
    """GPU-Z 数据缺失状态机：返回 (标题, 正文) 或 None，并更新 state。

    只在「有 -> 无 / 无 -> 有」时返回消息，长期缺失不重复推；
    首次观测（prev is None）不推，避免重启推送器就发一条。
    """
    if not enabled:
        return None
    expected = (snap.get("capabilities") or {}).get(
        "gpuz_expected", dev_id == "desktop")
    if not expected:
        return None
    gpuz_ok = bool((snap.get("sources") or {}).get("gpuz"))
    prev = state.get(dev_id)
    state[dev_id] = gpuz_ok
    stamp = datetime.now().strftime("%m-%d %H:%M")
    if prev is True and not gpuz_ok:
        return ("【警告】%s GPU-Z 数据中断" % name,
                "%s 的 GPU-Z 数据不可用，16pin 电压等读数缺失。\n"
                "若是你主动关闭 GPU-Z，可忽略本条。\n时间: %s" % (name, stamp))
    if prev is False and gpuz_ok:
        return ("【恢复】%s GPU-Z 数据已恢复" % name,
                "%s 的 GPU-Z 数据已恢复。\n时间: %s" % (name, stamp))
    return None


def acquire_single_instance(port=18090):
    """占用一个本地端口做单实例锁；进程退出时 socket 关闭，锁自动释放。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        return s
    except OSError:
        try:
            s.close()
        except Exception:
            pass
        return None


def main():
    lock_sock = acquire_single_instance()
    if lock_sock is None:
        log("已有实例在运行（本机 18090 被占用），本次退出，避免重复推送")
        return

    cfg = load_config()
    init_notifier(cfg)
    poll = max(10, int(cfg.get("poll_seconds", POLL_SECONDS)))
    repeat_seconds = max(1, int(cfg.get("alert_repeat_minutes", 5))) * 60
    confirm = max(1, int(cfg.get("confirm_samples", 2)))
    recover = max(1, int(cfg.get("recover_samples", 3)))
    flap_guard = int(cfg.get("flap_guard_minutes", 10))
    report_enabled = bool(cfg.get("daily_report", True))
    report_at = str(cfg.get("daily_report_time", "21:00")).strip() or "21:00"

    devices = [d for d in cfg.get("devices", []) if d.get("enabled", True)]
    if not devices:
        log("没有启用任何设备，退出")
        return

    states = {}
    source_state = {}
    last_snap = {}
    offline = {d["id"]: {"fails": 0, "active": False} for d in devices}

    def key_of(dev_id, rule_idx):
        return "%s#%d" % (dev_id, rule_idx)

    for d in devices:
        for i, _ in enumerate(d.get("rules") or []):
            states[key_of(d["id"], i)] = MetricState()

    log("推送器启动：%d 台设备，轮询 %ds，告警重复 %dmin，日报 %s"
        % (len(devices), poll, repeat_seconds // 60,
           ("每天 " + report_at) if report_enabled else "关闭"))
    silent = [d.get("name", d["id"]) for d in devices if not d.get("offline_alert", True)]
    if silent:
        log("离线静默设备：%s（不推离线/重连，日报仍记录状态）" % "、".join(silent))

    # ---- 日报统计：进程内累积，跨天重置 ----
    stats = {d["id"]: blank_stats() for d in devices}
    stats_day = datetime.now().date()
    state = load_state()
    last_report_day = None
    if state.get("last_report_day"):
        try:
            last_report_day = datetime.strptime(state["last_report_day"], "%Y-%m-%d").date()
            log("当天日报已发过（%s），本次重启不补推" % state["last_report_day"])
        except ValueError:
            last_report_day = None

    def bump(dev_id, key, value):
        if isinstance(value, (int, float)):
            cur = stats[dev_id][key]
            if cur is None or value > cur:
                stats[dev_id][key] = value

    while True:
        now = time.time()
        alert_lines = []

        for d in devices:
            dev_id = d["id"]
            name = d.get("name", dev_id)
            # 不常开的设备只记录离线状态，不推消息；日报仍是唯一存活信号
            offline_alert = bool(d.get("offline_alert", True))
            try:
                snap = fetch_json(d["url"].rstrip("/") + "/api/snapshot")
                offline[dev_id]["fails"] = 0
                if offline[dev_id]["active"]:
                    offline[dev_id]["active"] = False
                    if offline_alert:
                        notify("【恢复】%s 重新在线" % name,
                                 "%s 于 %s 恢复响应。\n设备: %s"
                                 % (name, datetime.now().strftime("%m-%d %H:%M"), snap.get("device")))
                last_snap[dev_id] = snap
            except Exception as e:
                offline[dev_id]["fails"] += 1
                if offline[dev_id]["fails"] >= 3 and not offline[dev_id]["active"]:
                    offline[dev_id]["active"] = True
                    stats[dev_id]["offline"] += 1
                    if offline_alert:
                        notify("【离线】%s 采集服务失联" % name,
                                 "%s 连续 %d 次请求失败（约 %d 分钟）。\n最后一次错误: %s\n请检查 agent.py 是否在运行。"
                                 % (name, offline[dev_id]["fails"],
                                    offline[dev_id]["fails"] * poll // 60, e))
                continue

            snap = last_snap[dev_id]

            # 数据源缺失：只在「有 -> 无 / 无 -> 有」时推一条警告或恢复
            ev = source_alert_event(dev_id, name, snap, source_state,
                                    d.get("source_alert"))
            if ev:
                notify(*ev)

            bump(dev_id, "cpu_max", dig(snap, "cpu.temp"))
            bump(dev_id, "gpu_hot_max", dig(snap, "gpu.hot"))
            bump(dev_id, "gpu_power_max", dig(snap, "gpu.power"))
            bump(dev_id, "mem_max", dig(snap, "mem.pct"))
            rules = d.get("rules") or []

            for i, rule in enumerate(rules):
                st = states[key_of(dev_id, i)]
                val = dig(snap, rule["key"])
                violated = compare(val, rule["op"], rule["value"])
                ev = st.update(violated, now, confirm, recover, flap_guard)

                if ev == "enter":
                    stats[dev_id]["alerts"] += 1
                    alert_lines.append(
                        "%s %s %s%s（推送线 %s%s）\n设备: %s"
                        % (name, rule["label"], fmt(val), rule.get("unit", ""),
                           rule["op"], fmt(rule["value"]), snap.get("device"))
                    )
                elif ev == "recover":
                    notify("【恢复】%s %s 已回落" % (name, rule["label"]),
                             "%s 的 %s 已回落到 %s%s，恢复正常。\n设备: %s"
                             % (name, rule["label"], fmt(val), rule.get("unit", ""),
                                snap.get("device")))
                elif st.due_repeat(now, repeat_seconds):
                    st.last_push = now
                    alert_lines.append(
                        "%s %s %s%s 持续（推送线 %s%s）"
                        % (name, rule["label"], fmt(val), rule.get("unit", ""),
                           rule["op"], fmt(rule["value"])))

        if alert_lines:
            notify("【告警】硬件指标越线",
                     "\n\n".join(alert_lines) + "\n\n时间: %s"
                     % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        # ---- 跨天重置日报统计 ----
        today = datetime.now().date()
        if today != stats_day:
            stats_day = today
            stats = {d["id"]: blank_stats() for d in devices}
            log("跨天，日报统计已重置")

        # ---- 每天一条日报（兼作推送器存活信号）----
        if report_enabled:
            hhmm = datetime.now().strftime("%H:%M")
            if hhmm >= report_at and last_report_day != today:
                # 无论推送成败都记下日期：失败时重试会变成每 30 秒刷屏
                last_report_day = today
                ok = notify("【日报】硬件监控 %s" % today.strftime("%m-%d"),
                            build_report(devices, stats, offline))
                if ok:
                    state["last_report_day"] = today.isoformat()
                    save_state(state)

        time.sleep(poll)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("收到中断，退出")
    except Exception as exc:
        log("异常退出: %r" % exc)
        raise
