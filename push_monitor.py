#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
硬件监控推送器 — 轮询两台 agent，按独立阈值推送告警与心跳。

设计要点
--------
1. 独立进程运行，不依附 agent.py：agent 崩掉时恰恰最需要告警。
2. 推送阈值独立于网页 ALERT_RULES：网页要"早提示"，推送要"真出事才响"。
   跑大模型 GPU 热点 96°C 属正常，推送线设在 108°C。
3. 心跳 30 分钟一条；有告警时自动加密到 5 分钟一条，直到恢复。
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
LOG_PATH = os.path.join(BASE_DIR, "push_monitor.log")

POLL_SECONDS = 30
LOG_MAX_BYTES = 512 * 1024

DEFAULT_CONFIG = {
    "channel": "wecom",
    "wecom_webhook": "",
    "pushplus_token": "",
    "poll_seconds": 30,
    "heartbeat_minutes": 30,
    "alert_repeat_minutes": 5,
    "confirm_samples": 2,
    "recover_samples": 3,
    "flap_guard_minutes": 10,
    "devices": [
        {
            "id": "desktop",
            "name": "台式机",
            "url": "http://192.168.1.10:8090",
            "enabled": True,
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
    if "【告警】" in title or "【离线】" in title:
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

def fmt(value, unit="", dp=1):
    if value is None:
        return "--"
    if isinstance(value, float):
        return ("%." + str(dp) + "f") % value + unit
    return str(value) + unit


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
    hb_seconds = max(5, int(cfg.get("heartbeat_minutes", 30))) * 60
    repeat_seconds = max(1, int(cfg.get("alert_repeat_minutes", 5))) * 60
    confirm = max(1, int(cfg.get("confirm_samples", 2)))
    recover = max(1, int(cfg.get("recover_samples", 3)))
    flap_guard = int(cfg.get("flap_guard_minutes", 10))

    devices = [d for d in cfg.get("devices", []) if d.get("enabled", True)]
    if not devices:
        log("没有启用任何设备，退出")
        return

    states = {}
    last_snap = {}
    offline = {d["id"]: {"fails": 0, "active": False} for d in devices}

    def key_of(dev_id, rule_idx):
        return "%s#%d" % (dev_id, rule_idx)

    for d in devices:
        for i, _ in enumerate(d.get("rules") or []):
            states[key_of(d["id"], i)] = MetricState()

    log("推送器启动：%d 台设备，轮询 %ds，心跳 %dmin，告警重复 %dmin"
        % (len(devices), poll, hb_seconds // 60, repeat_seconds // 60))

    last_heartbeat = 0.0
    first_round = True

    while True:
        now = time.time()
        any_alert = False
        summary_lines = []
        alert_lines = []

        for d in devices:
            dev_id = d["id"]
            name = d.get("name", dev_id)
            try:
                snap = fetch_json(d["url"].rstrip("/") + "/api/snapshot")
                offline[dev_id]["fails"] = 0
                if offline[dev_id]["active"]:
                    offline[dev_id]["active"] = False
                    notify("【恢复】%s 重新在线" % name,
                             "%s 于 %s 恢复响应。\n设备: %s"
                             % (name, datetime.now().strftime("%m-%d %H:%M"), snap.get("device")))
                last_snap[dev_id] = snap
            except Exception as e:
                offline[dev_id]["fails"] += 1
                if offline[dev_id]["fails"] >= 3 and not offline[dev_id]["active"]:
                    offline[dev_id]["active"] = True
                    notify("【离线】%s 采集服务失联" % name,
                             "%s 连续 %d 次请求失败（约 %d 分钟）。\n最后一次错误: %s\n请检查 agent.py 是否在运行。"
                             % (name, offline[dev_id]["fails"],
                                offline[dev_id]["fails"] * poll // 60, e))
                summary_lines.append("%s  离线" % name)
                any_alert = any_alert or offline[dev_id]["active"]
                continue

            snap = last_snap[dev_id]
            rules = d.get("rules") or []
            dev_alert = False
            parts = []

            for i, rule in enumerate(rules):
                st = states[key_of(dev_id, i)]
                val = dig(snap, rule["key"])
                violated = compare(val, rule["op"], rule["value"])
                ev = st.update(violated, now, confirm, recover, flap_guard)

                if ev in ("enter", "repeat"):
                    pass
                if ev == "enter":
                    dev_alert = True
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
                    dev_alert = True
                    alert_lines.append(
                        "%s %s %s%s 持续（推送线 %s%s）"
                        % (name, rule["label"], fmt(val), rule.get("unit", ""),
                           rule["op"], fmt(rule["value"])))

                if st.active:
                    dev_alert = True

            any_alert = any_alert or dev_alert
            gpu_part = ""
            if dig(snap, "gpu.hot") is not None:
                gpu_part = " GPU %s / %s" % (fmt(dig(snap, "gpu.hot"), "°C"),
                                             fmt(dig(snap, "gpu.power"), "W", 0))
            cpu_part = " CPU %s" % fmt(dig(snap, "cpu.temp"), "°C")
            mem_part = " 内存 %s" % fmt(dig(snap, "mem.pct"), "%")
            parts.append(name + cpu_part + gpu_part + mem_part)
            summary_lines.append(parts[0])

        if alert_lines:
            notify("【告警】硬件指标越线",
                     "\n\n".join(alert_lines) + "\n\n时间: %s"
                     % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        due_hb = (now - last_heartbeat) >= hb_seconds
        if due_hb and not any_alert and not first_round:
            notify("【心跳】硬件状态正常",
                     "\n".join(summary_lines) + "\n\n时间: %s"
                     % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            last_heartbeat = now
        elif first_round:
            log("首轮采样完成：%s" % " | ".join(summary_lines))
            last_heartbeat = now

        first_round = False
        time.sleep(poll)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("收到中断，退出")
    except Exception as exc:
        log("异常退出: %r" % exc)
        raise
