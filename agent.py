# -*- coding: utf-8 -*-
"""
hwmon-agent — 硬件遥测聚合服务
读 LHM(:8085) + GPU-Z CSV，输出统一 JSON，托管前端页面。

用法：
    python agent.py                     # 默认监听 0.0.0.0:8090
    python agent.py --port 8090
    python agent.py --dump              # 只打印所有传感器路径，不启服务
"""
import argparse
import copy
import csv
import ctypes
import datetime as dt
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from contextlib import contextmanager
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

LHM_URL = "http://127.0.0.1:8085/data.json"
GPZ_CSV = r"C:\hwsensor\gpuz.csv"
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("HWMON_DB", os.path.join(STATIC_DIR, "hwmon.db"))
DB_RETENTION_DAYS = 7

HIST_LEN = 120
POLL_INTERVAL = 2.0
HIST_WINDOW_MS = int(HIST_LEN * POLL_INTERVAL * 1000)
GPUZ_STALE_MS = 10_000
SNAPSHOT_WARN_MS = 8_000
SNAPSHOT_BAD_MS = 15_000
ALERT_HOLD_SAMPLES = 3

# 产品预警策略，不等同于所有传感器的官方损坏限值。
ALERT_RULES = {
    "gpu.core": {"label": "GPU 核心", "unit": "°C", "warn": 80, "bad": 90, "hysteresis": 2},
    "gpu.hot": {"label": "GPU 热点", "unit": "°C", "warn": 90, "bad": 105, "hysteresis": 2},
    "gpu.mem": {"label": "GPU 显存结温", "unit": "°C", "warn": 90, "bad": 100, "hysteresis": 2},
    "cpu.temp": {"label": "CPU 封装温度", "unit": "°C", "warn": 85, "bad": 95, "hysteresis": 2},
    "cpu.core_max": {"label": "CPU 核心最高", "unit": "°C", "warn": 85, "bad": 95, "hysteresis": 2},
    "gpu.pin_volt": {
        "label": "16pin 整路电压", "unit": "V", "warn_low": 11.8,
        "bad_low": 11.4, "bad_high": 12.6, "dp": 2,
    },
}

_lock = threading.Lock()
_state = {"snapshot": None, "ts": 0, "err": None}
_hist = {
    "t": deque(maxlen=HIST_LEN),
    "gpu_hot": deque(maxlen=HIST_LEN),
    "gpu_core": deque(maxlen=HIST_LEN),
    "gpu_power": deque(maxlen=HIST_LEN),
    "cpu_temp": deque(maxlen=HIST_LEN),
    "cpu_power": deque(maxlen=HIST_LEN),
}


# ---------------- 工具 ----------------

def num(s):
    """'40.0 °C' -> 40.0 ; '' / '-' -> None"""
    if s is None:
        return None
    s = str(s).strip()
    if s in ("", "-", "N/A"):
        return None
    buf = []
    for ch in s:
        if ch.isdigit() or ch == ".":
            buf.append(ch)
        elif ch == "-" and not buf:
            buf.append(ch)
        elif buf:
            break
    try:
        return float("".join(buf))
    except ValueError:
        return None


def first_valid(*values):
    for value in values:
        if value is not None:
            return value
    return None


def flatten(node, path="", out=None):
    """LHM 递归树 -> 叶子列表 [{path, text, value}]"""
    if out is None:
        out = []
    text = str(node.get("Text", ""))
    p = (path + "/" + text) if path else text
    children = node.get("Children") or []
    if not children:
        out.append({"path": p, "text": text, "value": node.get("Value", "")})
    else:
        for c in children:
            flatten(c, p, out)
    return out


def pick(leaves, category, name_like, hw_like=None, exclude=None):
    """按「类别 + 名称包含」取第一个有效数值，可限定同一硬件节点。"""
    nl = name_like.lower()
    for lf in leaves:
        parts = lf["path"].split("/")
        if len(parts) < 3:
            continue
        if parts[-2] != category:
            continue
        if nl not in parts[-1].lower():
            continue
        low = lf["path"].lower()
        if hw_like and hw_like.lower() not in low:
            continue
        if exclude and exclude.lower() in low:
            continue
        v = num(lf["value"])
        if v is not None:
            return v
    return None


class _MEMSTATUS(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def mem_info():
    """用系统 API 取物理内存，LHM 的 Used/Available 含页面文件，不可直接相加"""
    try:
        m = _MEMSTATUS()
        m.dwLength = ctypes.sizeof(_MEMSTATUS)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return None
        gb = 1024.0 ** 3
        total = m.ullTotalPhys / gb
        used = (m.ullTotalPhys - m.ullAvailPhys) / gb
        return {
            "total": round(total, 1),
            "used": round(used, 1),
            "avail": round(total - used, 1),
            "pct": round(used / total * 100, 1) if total else None,
            "semantics": "physical",
        }
    except Exception:
        return None


# ---------------- LHM ----------------

def fetch_lhm():
    req = urllib.request.Request(LHM_URL, headers={"User-Agent": "hwmon-agent"})
    with urllib.request.urlopen(req, timeout=4) as r:
        raw = r.read()
    root = json.loads(raw.decode("utf-8", "ignore"))
    leaves = flatten(root)
    if not leaves:
        raise ValueError("LHM 未返回传感器数据")
    return leaves


# ---------------- GPU-Z CSV ----------------

GPZ_ALIASES = {
    "gpu_core": ("gpu temperature",),
    "gpu_hot": ("hot spot", "gpu hot spot"),
    "gpu_mem": ("memory junction", "gpu memory junction", "memory temperature", "gpu memory temperature", "vram temperature"),
    "board_power": ("board power draw", "board power"),
    "pin_power": ("16-pin power", "16 pin power", "12vhpwr power"),
    "pin_volt": ("16-pin voltage", "16 pin voltage", "12vhpwr voltage"),
    "tdp": ("tdp",),
    "gpu_volt": ("gpu voltage",),
}


def _norm_header(value):
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def resolve_gpuz_columns(header):
    """根据 CSV 表头解析列号，禁止把固定列号当成长期协议。"""
    normalized = [_norm_header(v) for v in header]
    result = {}
    for key, aliases in GPZ_ALIASES.items():
        targets = [_norm_header(v) for v in aliases]
        index = None
        for target in targets:
            for i, name in enumerate(normalized):
                if name == target:
                    index = i
                    break
            if index is not None:
                break
        if index is None:
            for target in targets:
                for i, name in enumerate(normalized):
                    if target and target in name:
                        index = i
                        break
                if index is not None:
                    break
        result[key] = index
    return result


def fetch_gpuz():
    """返回 (values, meta)。过期、错列或不完整的数据绝不覆盖 LHM。"""
    meta = {
        "available": False,
        "expected": os.path.exists(GPZ_CSV),
        "age_ms": None,
        "error": None,
    }
    if not os.path.exists(GPZ_CSV):
        meta["error"] = "GPU-Z 日志不存在"
        return None, meta

    try:
        meta["age_ms"] = max(0, int((time.time() - os.path.getmtime(GPZ_CSV)) * 1000))
    except OSError as e:
        meta["error"] = str(e)[:120]
        return None, meta
    if meta["age_ms"] > GPUZ_STALE_MS:
        meta["error"] = "GPU-Z 日志已停止更新"
        return None, meta

    tmp = os.path.join(tempfile.gettempdir(), "hwmon_gpuz.csv")
    try:
        shutil.copyfile(GPZ_CSV, tmp)
        with open(tmp, "r", encoding="gbk", errors="ignore", newline="") as f:
            header_line = f.readline()
            last = ""
            for line in f:
                if line.strip():
                    last = line
    except Exception as e:
        meta["error"] = str(e)[:120]
        return None, meta
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    if not header_line.strip() or not last.strip():
        meta["error"] = "GPU-Z 日志缺少表头或数据行"
        return None, meta

    try:
        header = next(csv.reader([header_line]))
        cols = next(csv.reader([last]))
    except (csv.Error, StopIteration) as e:
        meta["error"] = "GPU-Z CSV 解析失败: " + str(e)[:80]
        return None, meta

    indexes = resolve_gpuz_columns(header)
    out = {}
    for key, idx in indexes.items():
        out[key] = num(cols[idx]) if idx is not None and idx < len(cols) else None
    meta["headers"] = [str(v) for v in header]
    meta["unmatched"] = [key for key, idx in indexes.items() if idx is None]
    if not any(v is not None for v in out.values()):
        meta["error"] = "GPU-Z 表头未匹配到有效传感器"
        return None, meta

    meta["available"] = True
    meta["columns"] = {k: v for k, v in indexes.items() if v is not None}
    return out, meta


# ---------------- 告警 ----------------

def _path_value(snap, path):
    cur = snap
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _temp_state(value, rule, active):
    if value is None:
        return "ok"
    warn, bad = rule["warn"], rule["bad"]
    h = rule.get("hysteresis", 0)
    if value >= bad or (active == "bad" and value >= bad - h):
        return "bad"
    if value >= warn or (active in ("warn", "bad") and value >= warn - h):
        return "warn"
    return "ok"


def _pin_state(value, active):
    if value is None:
        return "ok"
    if value < 11.4 or value > 12.6:
        return "bad"
    if active == "bad" and (value < 11.5 or value > 12.5):
        return "bad"
    if value <= 11.8 or (active in ("warn", "bad") and value <= 11.9):
        return "warn"
    return "ok"


def _rule_text(key, rule):
    if key == "gpu.pin_volt":
        return "预警 ≤11.8V；持续 <11.4V 或 >12.6V 为严重"
    return f"预警 ≥{rule['warn']}{rule['unit']}；严重 ≥{rule['bad']}{rule['unit']}"


def evaluate_metric_alerts(snap, tracker):
    """普通越限连续 3 次才激活；达到 GPU 官方核心上限可立即严重。"""
    now = int(time.time() * 1000)
    alerts = []
    seen = set()
    for key, rule in ALERT_RULES.items():
        seen.add(key)
        value = _path_value(snap, key)
        if not isinstance(value, (int, float)):
            tracker.pop(key, None)
            continue
        entry = tracker.get(key, {})
        active = entry.get("severity", "ok")
        desired = _pin_state(value, active) if key == "gpu.pin_volt" else _temp_state(value, rule, active)
        if desired == "ok":
            tracker.pop(key, None)
            continue

        immediate = key == "gpu.core" and desired == "bad" and value >= rule["bad"]
        if active == desired:
            entry["pending_severity"] = None
            entry["pending_count"] = 0
        else:
            if entry.get("pending_severity") == desired:
                entry["pending_count"] = entry.get("pending_count", 0) + 1
            else:
                entry["pending_severity"] = desired
                entry["pending_count"] = 1
            if immediate or entry["pending_count"] >= ALERT_HOLD_SAMPLES:
                entry["severity"] = desired
                entry["since"] = now
                entry["pending_severity"] = None
                entry["pending_count"] = 0
            elif active == "ok":
                tracker[key] = entry
                continue
            # 已有状态在降级/升级确认期间保持原级别。

        severity = entry.get("severity", "ok")
        if severity == "ok":
            tracker[key] = entry
            continue
        entry["value"] = value
        tracker[key] = entry
        dp = rule.get("dp", 0)
        value_text = f"{value:.{dp}f}{rule['unit']}"
        alerts.append({
            "key": key,
            "label": rule["label"],
            "value": value,
            "unit": rule["unit"],
            "severity": severity,
            "state": severity,
            "since": entry["since"],
            "message": f"{rule['label']} {value_text}",
            "rule": _rule_text(key, rule),
        })

    for key in list(tracker):
        if key not in seen:
            tracker.pop(key, None)
    return alerts


def source_alerts(snap):
    now = int(time.time() * 1000)
    alerts = []
    sources = snap.get("sources") or {}
    meta = snap.get("source_meta") or {}
    caps = snap.get("capabilities") or {}

    def add(key, label, severity, message):
        alerts.append({
            "key": key, "label": label, "value": None, "unit": "",
            "severity": severity, "state": severity, "since": now,
            "message": message, "rule": "数据源必须持续更新",
        })

    if not sources.get("lhm"):
        add("source.lhm", "LHM", "bad", "LHM 断开，CPU / 内存 / 磁盘数据不可用")
    elif (snap.get("cpu") or {}).get("temp") is None:
        add("data.cpu_temp", "CPU 温度", "warn", "LHM 在线，但 CPU 温度无有效数据")

    if caps.get("gpuz_expected") and not sources.get("gpuz"):
        age = (meta.get("gpuz") or {}).get("age_ms")
        suffix = f"，已 {round(age / 1000)} 秒未更新" if isinstance(age, (int, float)) else ""
        add("source.gpuz", "GPU-Z", "warn", "GPU-Z 数据不可用" + suffix)
    return alerts


def resource_alerts(snap):
    """资源容量提示与硬件温度告警分开描述，但仍进入统一摘要。"""
    now = int(time.time() * 1000)
    alerts = []

    def add(key, label, value, warn, bad, unit="%"):
        if not isinstance(value, (int, float)) or value < warn:
            return
        severity = "bad" if value >= bad else "warn"
        alerts.append({
            "key": key, "label": label, "value": value, "unit": unit,
            "severity": severity, "state": severity, "since": now,
            "message": f"{label} {value:.0f}{unit}",
            "rule": f"资源提示：预警 ≥{warn}{unit}；严重 ≥{bad}{unit}",
            "category": "resource",
        })

    add("mem.pct", "内存占用", (snap.get("mem") or {}).get("pct"), 75, 90)
    for i, disk in enumerate(snap.get("disk") or []):
        add(f"disk.{i}", f"磁盘 {disk.get('name') or i + 1}", disk.get("used_pct"), 80, 90)
    return alerts


def build_health(snap):
    alerts = snap.get("alerts") or []
    bad = sum(a.get("severity") == "bad" for a in alerts)
    warn = sum(a.get("severity") == "warn" for a in alerts)
    status = "bad" if bad else "warn" if warn else "ok"
    label = "运行正常" if status == "ok" else f"{bad} 项严重" if bad else f"{warn} 项预警"
    if warn and bad:
        label += f" · {warn} 项预警"
    return {"status": status, "label": label, "bad": bad, "warn": warn}


def with_freshness(snapshot):
    """在响应时重新计算快照年龄，避免采集线程卡住后仍显示实时正常。"""
    snap = copy.deepcopy(snapshot)
    age = max(0, int(time.time() * 1000) - int(snap.get("ts") or 0))
    snap["snapshot_age_ms"] = age
    alerts = [a for a in snap.get("alerts", []) if a.get("key") != "snapshot.stale"]
    if age > SNAPSHOT_BAD_MS:
        alerts.append({
            "key": "snapshot.stale", "label": "采样快照", "value": age,
            "unit": "ms", "severity": "bad", "state": "bad", "since": snap.get("ts"),
            "message": f"采样快照已 {round(age / 1000)} 秒未更新",
            "rule": "快照超过 15 秒未更新",
        })
    elif age > SNAPSHOT_WARN_MS:
        alerts.append({
            "key": "snapshot.stale", "label": "采样快照", "value": age,
            "unit": "ms", "severity": "warn", "state": "warn", "since": snap.get("ts"),
            "message": f"采样快照更新延迟 {round(age / 1000)} 秒",
            "rule": "快照超过 8 秒未更新",
        })
    snap["alerts"] = alerts
    snap["health"] = build_health(snap)
    return snap


# ---------------- 组装快照 ----------------

def build_snapshot(alert_tracker=None):
    if alert_tracker is None:
        alert_tracker = {}
    now = int(time.time() * 1000)
    snap = {
        "device": socket.gethostname(),
        "ts": now,
        "online": True,
        "sources": {"lhm": False, "gpuz": False},
        "source_meta": {},
        "cpu": {}, "gpu": {}, "mem": {}, "disk": [],
        "capabilities": {},
    }

    try:
        leaves = fetch_lhm()
        snap["sources"]["lhm"] = True
        snap["source_meta"]["lhm"] = {"available": True, "error": None}
    except Exception as e:
        leaves = []
        snap["source_meta"]["lhm"] = {"available": False, "error": str(e)[:120]}
        snap["lhm_error"] = str(e)[:120]

    if leaves:
        cpu_name = _hw_name_any(leaves, ("Core i", "Ryzen", "Core Ultra"))
        gpu_name = _hw_name_any(leaves, ("RTX", "GTX", "Radeon", "Iris", "UHD", "Arc")) or ""
        snap["cpu"] = {
            "name": cpu_name,
            "temp": pick(leaves, "Temperatures", "CPU Package", cpu_name),
            "core_max": pick(leaves, "Temperatures", "Core Max", cpu_name),
            "power": pick(leaves, "Powers", "CPU Package", cpu_name),
            "load": pick(leaves, "Load", "CPU Total", cpu_name),
            "volt": first_valid(
                pick(leaves, "Voltages", "CPU Core", cpu_name),
                pick(leaves, "Voltages", "Vcore", cpu_name),
            ),
            "clock": first_valid(
                pick(leaves, "Clocks", "Core", cpu_name),
                pick(leaves, "Clocks", "P-Core", cpu_name),
            ),
        }
        snap["gpu"] = {
            "name": gpu_name,
            "core": pick(leaves, "Temperatures", "GPU Core", gpu_name),
            "hot": pick(leaves, "Temperatures", "GPU Hot Spot", gpu_name),
            "mem": pick(leaves, "Temperatures", "GPU Memory Junction", gpu_name),
            "clock": pick(leaves, "Clocks", "GPU Core", gpu_name),
            "fan": pick(leaves, "Fans", "GPU Fan 1", gpu_name),
            "power": pick(leaves, "Powers", "GPU Power", gpu_name),
            "load": pick(leaves, "Load", "GPU Core", gpu_name),
        }
        snap["mem"] = mem_info() or {
            "used": pick(leaves, "Data", "Memory Used"),
            "avail": pick(leaves, "Data", "Memory Available"),
            "total": None, "pct": None, "semantics": "lhm_includes_pagefile",
        }
        snap["disk"] = _disks(leaves)
        snap["fan_cpu"] = pick(leaves, "Fans", "CPU Fan", cpu_name)

    g = snap.get("gpu") or {}
    integrated = any(k in (g.get("name") or "").lower() for k in ("iris", "uhd"))
    snap["capabilities"] = {
        "device_class": "laptop" if integrated else "desktop",
        "integrated_gpu": integrated,
        "gpu_temperature": not integrated,
        "gpuz_expected": bool(os.path.exists(GPZ_CSV)) and not integrated,
    }

    # 核显设备不读取独显 GPU-Z 日志，避免适配器身份错配。
    if integrated:
        gz, gz_meta = None, {"available": False, "expected": False, "age_ms": None, "error": "核显设备不适用"}
    else:
        gz, gz_meta = fetch_gpuz()
        snap["capabilities"]["gpuz_expected"] = bool(gz_meta.get("expected"))
    snap["source_meta"]["gpuz"] = gz_meta
    snap["sources"]["gpuz"] = bool(gz_meta.get("available"))
    snap["capabilities"]["gpuz_expected"] = bool(gz_meta.get("expected") or (not integrated and g.get("name")))

    if gz:
        mapping = {
            "core": gz.get("gpu_core"), "hot": gz.get("gpu_hot"),
            "mem": gz.get("gpu_mem"), "power": gz.get("board_power"),
            "tdp": gz.get("tdp"), "pin_power": gz.get("pin_power"),
            "pin_volt": gz.get("pin_volt"), "volt": gz.get("gpu_volt"),
        }
        for k, v in mapping.items():
            if v is not None:
                g[k] = v

    name = g.get("name") or ""
    if "5090" in name:
        snap["gpu_tdp_w"] = 575
    elif "4090" in name:
        snap["gpu_tdp_w"] = 450
    if g.get("tdp") is None and g.get("power") is not None and snap.get("gpu_tdp_w"):
        g["tdp"] = round(g["power"] / snap["gpu_tdp_w"] * 100, 1)

    snap["alerts"] = evaluate_metric_alerts(snap, alert_tracker) + source_alerts(snap) + resource_alerts(snap)
    snap["health"] = build_health(snap)
    return snap


def _hw_name_any(leaves, keywords):
    for keyword in keywords:
        kw = keyword.lower()
        for lf in leaves:
            parts = lf["path"].split("/")
            if len(parts) >= 4 and kw in parts[2].lower():
                return parts[2]
    return None


def _disks(leaves):
    out = []
    seen = set()
    for lf in leaves:
        parts = lf["path"].split("/")
        if len(parts) >= 5 and parts[-2] == "Load" and "Used Space" in parts[-1]:
            v = num(lf["value"])
            name = parts[2].strip()
            if v is not None and name not in seen:
                seen.add(name)
                out.append({"name": name, "used_pct": v})
    return out


# ---------------- SQLite 采样与今日统计 ----------------

SAMPLE_FIELDS = {
    "cpu_temp": "cpu.temp", "cpu_core_max": "cpu.core_max", "cpu_power": "cpu.power",
    "cpu_load": "cpu.load", "gpu_core": "gpu.core", "gpu_hot": "gpu.hot",
    "gpu_mem": "gpu.mem", "gpu_power": "gpu.power", "gpu_load": "gpu.load",
    "pin_volt": "gpu.pin_volt", "mem_pct": "mem.pct",
}


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=3000")
    return conn


@contextmanager
def db_session():
    conn = db_connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def db_init():
    with db_session() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS samples (
                ts INTEGER PRIMARY KEY,
                cpu_temp REAL, cpu_core_max REAL, cpu_power REAL, cpu_load REAL,
                gpu_core REAL, gpu_hot REAL, gpu_mem REAL, gpu_power REAL, gpu_load REAL,
                pin_volt REAL, mem_pct REAL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts)")


def store_sample(snap):
    names = ["ts"] + list(SAMPLE_FIELDS)
    values = [snap["ts"]] + [_path_value(snap, path) for path in SAMPLE_FIELDS.values()]
    placeholders = ",".join("?" for _ in names)
    with db_session() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO samples ({','.join(names)}) VALUES ({placeholders})",
            values,
        )


def cleanup_samples():
    cutoff = int((time.time() - DB_RETENTION_DAYS * 86400) * 1000)
    with db_session() as conn:
        conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))


def local_day_start(date_value=None):
    day = date_value or dt.datetime.now().date()
    return int(dt.datetime.combine(day, dt.time.min).timestamp() * 1000)


def query_history(window):
    now = int(time.time() * 1000)
    if window == "1h":
        start = now - 3_600_000
    elif window == "today":
        start = local_day_start()
    else:
        raise ValueError("window 仅支持 1h 或 today")
    bucket = max(2000, int((now - start) / 119))
    fields = ["CAST(AVG(ts) AS INTEGER) AS t"] + [f"AVG({name}) AS {name}" for name in SAMPLE_FIELDS]
    with db_session() as conn:
        rows = conn.execute(
            f"SELECT {','.join(fields)} FROM samples "
            "WHERE ts >= ? AND ts <= ? GROUP BY CAST(ts / ? AS INTEGER) ORDER BY t",
            (start, now, bucket),
        ).fetchall()
    data = {"t": [row["t"] for row in rows]}
    for name in SAMPLE_FIELDS:
        data[name] = [row[name] for row in rows]
    data["_meta"] = {"window": window, "start": start, "end": now, "bucket_ms": bucket}
    return data


def daily_summary(date_text=None):
    day = dt.date.fromisoformat(date_text) if date_text else dt.datetime.now().date()
    start = local_day_start(day)
    now = int(time.time() * 1000)
    end = min(now, start + 86_400_000)
    selects = ["COUNT(*) AS samples", "MIN(ts) AS first_sample", "MAX(ts) AS last_sample"]
    for name in SAMPLE_FIELDS:
        selects.extend((f"MIN({name}) AS min_{name}", f"AVG({name}) AS avg_{name}", f"MAX({name}) AS max_{name}"))
    selects.extend((
        "SUM(CASE WHEN cpu_temp >= 85 THEN 1 ELSE 0 END) AS cpu_warn_count",
        "SUM(CASE WHEN gpu_core >= 80 THEN 1 ELSE 0 END) AS gpu_warn_count",
        "SUM(CASE WHEN gpu_hot >= 90 THEN 1 ELSE 0 END) AS hot_warn_count",
        "SUM(CASE WHEN pin_volt <= 11.8 THEN 1 ELSE 0 END) AS pin_warn_count",
    ))
    with db_session() as conn:
        row = conn.execute(
            f"SELECT {','.join(selects)} FROM samples WHERE ts >= ? AND ts < ?",
            (start, end),
        ).fetchone()

    count = int(row["samples"] or 0)
    metrics = {}
    for name in SAMPLE_FIELDS:
        metrics[name] = {
            "min": row[f"min_{name}"], "avg": row[f"avg_{name}"], "max": row[f"max_{name}"],
        }
    first, last = row["first_sample"], row["last_sample"]
    span_seconds = max(0, (last - first) / 1000) if first and last else 0
    coverage = min(100, count * POLL_INTERVAL / span_seconds * 100) if span_seconds > 0 else (100 if count else 0)
    return {
        "date": day.isoformat(), "generated_at": now, "start": start, "end": end,
        "samples": count, "first_sample": first, "last_sample": last,
        "coverage_pct": round(coverage, 1), "retention_days": DB_RETENTION_DAYS,
        "metrics": metrics,
        "storage": {"ok": True, "source": "database_readable"},
        "events_seconds": {
            "cpu_temp_ge_85": int((row["cpu_warn_count"] or 0) * POLL_INTERVAL),
            "gpu_core_ge_80": int((row["gpu_warn_count"] or 0) * POLL_INTERVAL),
            "gpu_hot_ge_90": int((row["hot_warn_count"] or 0) * POLL_INTERVAL),
            "pin_volt_le_11_8": int((row["pin_warn_count"] or 0) * POLL_INTERVAL),
        },
    }


# ---------------- 采集线程 ----------------

def poller():
    global _state
    alert_tracker = {}
    last_cleanup = 0
    while True:
        try:
            snap = build_snapshot(alert_tracker)
            try:
                store_sample(snap)
                snap["storage"] = {"ok": True, "error": None}
                if time.time() - last_cleanup > 3600:
                    cleanup_samples()
                    last_cleanup = time.time()
            except Exception as storage_error:
                snap["storage"] = {"ok": False, "error": str(storage_error)[:120]}
                snap["alerts"].append({
                    "key": "storage.daily", "label": "今日统计存储", "value": None, "unit": "",
                    "severity": "warn", "state": "warn", "since": snap["ts"],
                    "message": "今日统计采样写入失败", "rule": str(storage_error)[:120],
                })
                snap["health"] = build_health(snap)
            with _lock:
                _state = {"snapshot": snap, "ts": time.time(), "err": None}
                g = snap.get("gpu") or {}
                c = snap.get("cpu") or {}
                _hist["t"].append(snap["ts"])
                _hist["gpu_hot"].append(g.get("hot"))
                _hist["gpu_core"].append(g.get("core"))
                _hist["gpu_power"].append(g.get("power"))
                _hist["cpu_temp"].append(c.get("temp"))
                _hist["cpu_power"].append(c.get("power"))
        except Exception as e:
            with _lock:
                _state = {"snapshot": None, "ts": time.time(), "err": str(e)[:200]}
        time.sleep(POLL_INTERVAL)


# ---------------- HTTP ----------------

class Handler(BaseHTTPRequestHandler):
    server_version = "hwmon/0.5"

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

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/api/snapshot":
            with _lock:
                snap = _state.get("snapshot")
                err = _state.get("err")
            if snap:
                self._json(with_freshness(snap))
            else:
                self._json({"online": False, "error": err or "waiting"}, 503)
            return

        if path == "/api/history":
            window = (query.get("window") or ["4m"])[0]
            if window in ("1h", "today"):
                try:
                    self._json(query_history(window))
                except Exception as e:
                    self._json({"error": str(e)[:160]}, 503)
                return
            with _lock:
                data = {k: list(v) for k, v in _hist.items()}
            now = int(time.time() * 1000)
            data["_meta"] = {"window": "4m", "start": now - HIST_WINDOW_MS, "end": now, "bucket_ms": 2000}
            self._json(data)
            return

        if path == "/api/daily":
            try:
                date_text = (query.get("date") or [None])[0]
                data = daily_summary(date_text)
                with _lock:
                    snap = _state.get("snapshot")
                if snap and snap.get("storage"):
                    data["storage"] = snap["storage"]
                self._json(data)
            except ValueError as e:
                self._json({"error": "date 格式应为 YYYY-MM-DD", "detail": str(e)}, 400)
            except Exception as e:
                self._json({"error": str(e)[:160]}, 503)
            return

        if path in ("/", "/index.html"):
            self._serve_file("ui.html", "text/html; charset=utf-8")
            return

        if path == "/health":
            with _lock:
                snap = _state.get("snapshot")
            if not snap:
                self._json({"ok": False, "device": socket.gethostname(), "error": "waiting"}, 503)
            else:
                current = with_freshness(snap)
                self._json({
                    "ok": current["health"]["status"] != "bad",
                    "device": current["device"],
                    "health": current["health"],
                    "snapshot_age_ms": current["snapshot_age_ms"],
                    "sources": current["sources"],
                })
            return

        self.send_error(404)

    def _serve_file(self, name, ctype):
        fp = os.path.join(STATIC_DIR, name)
        if not os.path.exists(fp):
            self.send_error(404, "ui.html not found")
            return
        with open(fp, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def dump_sensors():
    leaves = fetch_lhm()
    print(f"共 {len(leaves)} 个叶子节点\n")
    for lf in leaves:
        v = lf["value"]
        if str(v).strip() in ("", "-"):
            continue
        print(f"{lf['path']}\t{v}")
    print("\n--- GPU-Z 最后一行 ---")
    gz, meta = fetch_gpuz()
    print(json.dumps({"values": gz, "meta": meta}, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--dump", action="store_true")
    args = ap.parse_args()

    if args.dump:
        dump_sensors()
        return

    try:
        db_init()
    except Exception as e:
        print(f"今日统计数据库初始化失败: {e}")

    t = threading.Thread(target=poller, daemon=True)
    t.start()
    time.sleep(1.2)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    ip = get_lan_ip()
    print("hwmon-agent 已启动")
    print(f"  本机:   http://127.0.0.1:{args.port}")
    print(f"  局域网: http://{ip}:{args.port}")
    print(f"  设备名: {socket.gethostname()}")
    print("  Ctrl+C 退出")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    main()
