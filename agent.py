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
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from contextlib import contextmanager
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# ---------------- 部署差异 ----------------

def default_easytier_cli():
    """两台机器 EasyTier 安装盘不同（笔记本 C:、台式机 D:），按存在性探测。"""
    candidates = (
        r"C:\Tools\EasyTier\easytier\easytier-cli.exe",
        r"D:\Tools\EasyTier\easytier\easytier-cli.exe",
    )
    return next((p for p in candidates if os.path.exists(p)), candidates[0])


# ---------------- 采集配置 ----------------

LHM_URL = "http://127.0.0.1:8085/data.json"   # LHM Web Server，必须 SYSTEM/管理员启动
GPZ_CSV = r"C:\hwsensor\gpuz.csv"              # GPU-Z 传感器日志，GUI 程序持续追加
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

# 每核历史只存温度、降频到 30 秒一行：24 核 × 7 天约 2 万行 / 几 MB。
# 它是实时诊断视图，不与主 samples 表同频，也不参与 /api/summary 聚合。
CORE_SAMPLE_EVERY = 15
CORE_ID_RE = re.compile(r"[PEC]\d{1,2}")

# 工具页依赖：EasyTier CLI 与路由器 WOL 地址（均可用环境变量覆盖）
EASYTIER_CLI = os.environ.get("EASYTIER_CLI") or default_easytier_cli()
WOL_URL = os.environ.get("HWMON_WOL_URL", "http://127.0.0.1:8088/cgi-bin/wol")
ET_CACHE_SECONDS = 10
WOL_THROTTLE_SECONDS = 60

_et_lock = threading.Lock()
_et_cache = {"ts": 0.0, "data": None}
_wol_lock = threading.Lock()
_last_wol = {"ts": 0.0, "result": None}

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


def _read_gpuz_edges(path, encoding="gbk"):
    """
    只取 CSV 的表头行与最后一条数据行。

    GPU-Z 日志会无限追加；旧实现每 2 秒复制整个文件再逐行遍历，
    运行几天后单次采样就要读几十 MB。改为开头只读一行、尾部按 4KB
    回退到行边界，耗时与文件大小无关。
    返回 (header_line, last_line)；文件为空时两者皆为空串。
    """
    with open(path, "rb") as f:
        header = f.readline().decode(encoding, errors="ignore")
        f.seek(0, os.SEEK_END)
        pos, tail = f.tell(), b""
        # 凑够两个换行才能确定取到一条完整数据行（末行可能正被写入）。
        while pos > 0 and tail.count(b"\n") < 2:
            step = min(4096, pos)
            pos -= step
            f.seek(pos)
            tail = f.read(step) + tail
    lines = [line for line in tail.decode(encoding, errors="ignore").splitlines() if line.strip()]
    return header, (lines[-1] if lines else "")


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

    try:
        header_line, last = _read_gpuz_edges(GPZ_CSV)
    except Exception as e:
        meta["error"] = str(e)[:120]
        return None, meta

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


# ---------------- 每核明细 ----------------
#
# LHM data.json 本来就有每颗物理核心的独立传感器（2026-11-09 实测）：
#   <CPU>/Temperatures/P-Core #1                     = 81.0 °C
#   <CPU>/Clocks/P-Core #1                           = 4214.1 MHz
#   <CPU>/Voltages/P-Core #1                         = 1.198 V
#   <CPU>/Temperatures/P-Core #1 Distance to TjMax  = 19.0 °C
#   <CPU>/Load/CPU Core #1 Thread #1                 = 30.8 %  ← 全局线程号
# 旧版 pick() 只取「第一个匹配值」，把这些明细全丢了。这里收敛成一个纯函数：
# 输入输出明确、可单独测试，且不改动任何原有聚合字段。

CORE_SENSOR_RE = re.compile(
    r"^(?P<kind>P-Core|E-Core|Core) #(?P<index>\d+)(?P<dist> Distance to TjMax)?$",
    re.IGNORECASE,
)
THREAD_LOAD_RE = re.compile(
    r"^CPU Core #(?P<index>\d+)(?: Thread #(?P<thread>\d+))?$", re.IGNORECASE,
)
CORE_FIELD_BY_CATEGORY = {
    "Temperatures": "temp",
    "Clocks": "clock",
    "Voltages": "volt",
}


def collect_cpu_cores(leaves, cpu_name=None):
    """
    从 LHM 展平叶子中提取每个物理核心的明细。纯函数、无副作用。

    返回的数据契约（/api/snapshot 的 cpu.cores，供前端 CoreInspector 消费）：
      {
        "available": bool,                  # 是否存在任意每核传感器
        "count": int,                       # 物理核心数
        "groups": [{"kind": "P", "label": "性能核 (P)",
                    "cores": [{"id": "P1", "label": "P 核 1", "index": 1,
                               "temp": 81.0, "clock": 4214.1,
                               "volt": 1.198, "dist": 19.0}]}],
        "hottest": {"id": "P2", "label": "P 核 2", "temp": 93.0} | None,
        "threads": [{"id": "CPU Core #1 Thread #1", "index": 1,
                     "thread": 1, "load": 30.8}],
        "units": {"temp": "°C", "clock": "MHz", "volt": "V", "dist": "°C", "load": "%"},
      }

    注意：温度的 P/E 编号与 Load 的「CPU Core #N」全局线程号不是同一套体系，
    按序号强行合并会张冠李戴，因此线程占用单独放 threads，不写进 cores。
    各平台传感器数量不同（笔记本 4P+8E、台式机 8P+16E，AMD 可能只有 CCD 温度），
    所有数值字段都允许为 None，调用方必须容忍缺项。
    """
    cores = {}
    threads = []
    for lf in leaves:
        parts = lf["path"].split("/")
        if len(parts) < 5:
            continue
        # path = Sensor/<主机名>/<硬件名>/<类别>/<传感器名>
        if cpu_name and parts[2] != cpu_name:
            continue
        category, sensor = parts[-2], parts[-1].strip()
        value = num(lf["value"])
        if value is None:
            continue

        m = CORE_SENSOR_RE.match(sensor)
        if m:
            field = CORE_FIELD_BY_CATEGORY.get(category)
            if not field:
                continue
            if m.group("dist"):
                if field != "temp":
                    continue
                field = "dist"
            raw_kind = m.group("kind").lower()
            prefix = "P" if raw_kind.startswith("p") else "E" if raw_kind.startswith("e") else "C"
            index = int(m.group("index"))
            cid = f"{prefix}{index}"
            entry = cores.setdefault(cid, {
                "id": cid, "label": f"{prefix} 核 {index}", "index": index,
                "kind": prefix, "temp": None, "clock": None, "volt": None, "dist": None,
            })
            entry[field] = value
            continue

        t = THREAD_LOAD_RE.match(sensor)
        if t and category == "Load":
            threads.append({
                "id": sensor,
                "index": int(t.group("index")),
                "thread": int(t.group("thread") or 0),
                "load": value,
            })

    groups = []
    for prefix, label in (("P", "性能核 (P)"), ("E", "能效核 (E)"), ("C", "核心")):
        items = sorted(
            (c for c in cores.values() if c["kind"] == prefix),
            key=lambda c: c["index"],
        )
        if items:
            groups.append({"kind": prefix, "label": label, "cores": items})

    warm = [c for c in cores.values() if c["temp"] is not None]
    hottest = max(warm, key=lambda c: c["temp"]) if warm else None
    threads.sort(key=lambda t: (t["index"], t["thread"]))
    return {
        "available": bool(cores),
        "count": len(cores),
        "groups": groups,
        "hottest": ({"id": hottest["id"], "label": hottest["label"], "temp": hottest["temp"]}
                    if hottest else None),
        "threads": threads,
        "units": {"temp": "°C", "clock": "MHz", "volt": "V", "dist": "°C", "load": "%"},
    }


def core_layout(cores):
    """把每核温度压成 (核心 id 顺序, JSON 数组)，供 core_samples 表存储。"""
    flat = [c for group in (cores or {}).get("groups", []) for c in group["cores"]]
    if not flat:
        return None
    layout = ",".join(c["id"] for c in flat)
    temps = json.dumps([c["temp"] for c in flat], separators=(",", ":"))
    return layout, temps


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
    """16pin 电压分级。阈值统一取自 ALERT_RULES，避免多处硬编码不一致。"""
    if value is None:
        return "ok"
    rule = ALERT_RULES["gpu.pin_volt"]
    warn_low, bad_low, bad_high = rule["warn_low"], rule["bad_low"], rule["bad_high"]
    if value < bad_low or value > bad_high:
        return "bad"
    if active == "bad" and (value < bad_low + 0.1 or value > bad_high - 0.1):
        return "bad"
    # 滞回只留 0.05V：11.9V 属于正常区间，不该因为曾经预警过就一直挂黄色。
    if value <= warn_low or (active in ("warn", "bad") and value <= warn_low + 0.05):
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
            # 每核明细与聚合字段并列；只供详情弹层使用，不进告警规则与 samples 表。
            "cores": collect_cpu_cores(leaves, cpu_name),
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
    integrated = any(k in (g.get("name") or "").lower() for k in ("iris", "uhd", "arc"))
    if integrated:
        # 核显的 LHM 传感器命名与独显不同：3D 引擎占用叫 "D3D 3D"，
        # 没有 "GPU Core"；顺带补共享显存，让笔记本面板不至于只剩两行。
        g["load"] = first_valid(g.get("load"), pick(leaves, "Load", "D3D 3D", g.get("name")))
        used = pick(leaves, "Data", "D3D Shared Memory Used", g.get("name"))
        total = pick(leaves, "Data", "D3D Shared Memory Total", g.get("name"))
        g["mem_used"] = used
        g["mem_total"] = total
        g["mem_pct"] = round(used / total * 100, 1) if used is not None and total else None
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
    "pin_volt": "gpu.pin_volt", "cpu_volt": "cpu.volt", "mem_pct": "mem.pct",
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
    """主表存聚合指标；每核温度另放 core_samples，不把宽表撑成几十列。"""
    with db_session() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS samples (
                ts INTEGER PRIMARY KEY,
                cpu_temp REAL, cpu_core_max REAL, cpu_power REAL, cpu_load REAL,
                cpu_volt REAL,
                gpu_core REAL, gpu_hot REAL, gpu_mem REAL, gpu_power REAL, gpu_load REAL,
                pin_volt REAL, mem_pct REAL
            )
        """)
        # 旧库没有 cpu_volt 列：补一列（幂等），否则 CPU 电压迷你曲线读不到历史。
        cols = {row[1] for row in conn.execute("PRAGMA table_info(samples)")}
        if "cpu_volt" not in cols:
            conn.execute("ALTER TABLE samples ADD COLUMN cpu_volt REAL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts)")
        # layout = 逗号分隔的核心 id；temps = 与 layout 同序的 JSON 温度数组。
        # 每行自带 layout，因此核心数变化（换机/BIOS 改动）后旧数据仍可正确解释。
        conn.execute("""
            CREATE TABLE IF NOT EXISTS core_samples (
                ts INTEGER PRIMARY KEY,
                layout TEXT NOT NULL,
                temps TEXT NOT NULL
            )
        """)


def store_sample(snap):
    names = ["ts"] + list(SAMPLE_FIELDS)
    values = [snap["ts"]] + [_path_value(snap, path) for path in SAMPLE_FIELDS.values()]
    placeholders = ",".join("?" for _ in names)
    with db_session() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO samples ({','.join(names)}) VALUES ({placeholders})",
            values,
        )


def store_core_sample(snap):
    """降频写入每核温度；无每核传感器时静默跳过。"""
    packed = core_layout((snap.get("cpu") or {}).get("cores"))
    if not packed:
        return
    layout, temps = packed
    with db_session() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO core_samples (ts, layout, temps) VALUES (?, ?, ?)",
            (snap["ts"], layout, temps),
        )


def cleanup_samples():
    cutoff = int((time.time() - DB_RETENTION_DAYS * 86400) * 1000)
    with db_session() as conn:
        conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM core_samples WHERE ts < ?", (cutoff,))


def local_day_start(date_value=None):
    day = date_value or dt.datetime.now().date()
    return int(dt.datetime.combine(day, dt.time.min).timestamp() * 1000)


def query_history(window):
    now = int(time.time() * 1000)
    if window == "5m":
        start = now - 300_000
    elif window == "1h":
        start = now - 3_600_000
    elif window == "today":
        start = local_day_start()
    else:
        raise ValueError("window 仅支持 5m、1h 或 today")
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


WINDOW_SECONDS = {"5m": 300, "1h": 3_600}
WINDOW_LABELS = {"5m": "近 5 分钟", "1h": "近 1 小时", "today": "今日"}


def window_bounds(window):
    """返回 (start_ms, end_ms, label)。today 为本地自然日 0 点起。"""
    now = int(time.time() * 1000)
    if window == "today":
        return local_day_start(), now, WINDOW_LABELS["today"]
    if window not in WINDOW_SECONDS:
        raise ValueError("window 仅支持 5m、1h 或 today")
    return now - WINDOW_SECONDS[window] * 1000, now, WINDOW_LABELS[window]


def query_core_history(window, core_id):
    """
    返回单个核心的温度历史，时间窗语义与 query_history 一致。

    每桶取第一个采样（core_samples 本身已按 30 秒降频），避免今日窗口返回上千点。
    每行自带 layout，按 core_id 在其中定位下标；定位不到就置 None（断线），
    这样核心布局变化不会把旧数据错误地画到另一颗核上。
    """
    start, end, label = window_bounds(window)
    bucket = max(CORE_SAMPLE_EVERY * int(POLL_INTERVAL * 1000), int((end - start) / 119))
    with db_session() as conn:
        rows = conn.execute(
            "SELECT ts, layout, temps FROM core_samples WHERE ts >= ? AND ts <= ? ORDER BY ts",
            (start, end),
        ).fetchall()

    times, values, last_bucket = [], [], None
    for row in rows:
        b = row["ts"] // bucket
        if b == last_bucket:
            continue
        last_bucket = b
        try:
            index = (row["layout"] or "").split(",").index(core_id)
            value = json.loads(row["temps"])[index]
        except (ValueError, IndexError, TypeError, json.JSONDecodeError):
            value = None
        times.append(row["ts"])
        values.append(value)
    return {
        "core": core_id, "window": window, "label": label,
        "t": times, "temp": values,
        "_meta": {"start": start, "end": end, "bucket_ms": bucket, "samples": len(rows)},
    }


def _aggregate(start, end):
    """区间内按采样统计 min/avg/max、采样数、覆盖度与阈值越线累计秒数。"""
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
            f"SELECT {','.join(selects)} FROM samples WHERE ts >= ? AND ts <= ?",
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
        "samples": count, "first_sample": first, "last_sample": last,
        "coverage_pct": round(coverage, 1),
        "metrics": metrics,
        "events_seconds": {
            "cpu_temp_ge_85": int((row["cpu_warn_count"] or 0) * POLL_INTERVAL),
            "gpu_core_ge_80": int((row["gpu_warn_count"] or 0) * POLL_INTERVAL),
            "gpu_hot_ge_90": int((row["hot_warn_count"] or 0) * POLL_INTERVAL),
            "pin_volt_le_11_8": int((row["pin_warn_count"] or 0) * POLL_INTERVAL),
        },
    }


def window_summary(window="today"):
    """按时间窗口产出使用小结：5m / 1h / today。"""
    start, end, label = window_bounds(window)
    now = int(time.time() * 1000)
    data = _aggregate(start, end)
    data.update({
        "window": window, "label": label,
        "date": dt.datetime.fromtimestamp(start / 1000).date().isoformat(),
        "generated_at": now, "start": start, "end": end,
        "retention_days": DB_RETENTION_DAYS,
        "storage": {"ok": True, "source": "database_readable"},
    })
    return data


def daily_summary(date_text=None):
    """兼容旧接口：不传日期等价于 window_summary('today')。"""
    if not date_text:
        return window_summary("today")
    day = dt.date.fromisoformat(date_text)
    start = local_day_start(day)
    now = int(time.time() * 1000)
    end = min(now, start + 86_400_000)
    data = _aggregate(start, end)
    data.update({
        "window": "today", "label": day.isoformat(), "date": day.isoformat(),
        "generated_at": now, "start": start, "end": end,
        "retention_days": DB_RETENTION_DAYS,
        "storage": {"ok": True, "source": "database_readable"},
    })
    return data


# ---------------- 采集线程 ----------------

def poller():
    global _state
    alert_tracker = {}
    last_cleanup = 0
    sample_count = 0
    while True:
        try:
            snap = build_snapshot(alert_tracker)
            sample_count += 1
            try:
                store_sample(snap)
                # 每核温度降频落库；它只是诊断视图，失败不影响主采样与页面告警。
                if sample_count % CORE_SAMPLE_EVERY == 0:
                    try:
                        store_core_sample(snap)
                    except Exception as e:
                        snap["core_storage"] = {"ok": False, "error": str(e)[:120]}
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


# ---------------- 工具页数据 ----------------

def easytier_status():
    """调用 easytier-cli -o json peer；10 秒缓存，失败时回退上一次结果。"""
    now = time.time()
    with _et_lock:
        cached = _et_cache["data"]
        if cached is not None and now - _et_cache["ts"] < ET_CACHE_SECONDS:
            return dict(cached, cached=True, stale=False)
    payload = {"ok": False, "peers": [], "error": None,
               "ts": now, "cached": False, "stale": False}
    try:
        kwargs = dict(capture_output=True, text=True, timeout=3,
                      encoding="utf-8", errors="replace")
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW，pythonw 下不弹窗
        proc = subprocess.run([EASYTIER_CLI, "-o", "json", "peer"], **kwargs)
        if proc.returncode != 0:
            raise RuntimeError("cli exit %d: %s"
                               % (proc.returncode, (proc.stderr or "")[:160]))
        peers = json.loads((proc.stdout or "").strip())
        if not isinstance(peers, list):
            raise RuntimeError("cli 输出不是 JSON 列表")
        payload.update(ok=True, peers=peers, ts=time.time())
        with _et_lock:
            _et_cache.update(ts=payload["ts"], data=payload)
        return payload
    except Exception as e:
        payload["error"] = str(e)[:200]
        with _et_lock:
            if _et_cache["data"] is not None:
                return dict(_et_cache["data"], cached=True, stale=True,
                            error=payload["error"])
        return payload


def trigger_wol():
    """代理路由器 WOL 的 GET；60 秒内重复点击直接返回上次结果。"""
    now = time.time()
    with _wol_lock:
        prev = _last_wol["result"]
        if prev is not None and now - _last_wol["ts"] < WOL_THROTTLE_SECONDS:
            return dict(prev, throttled=True)
        result = {"ok": False, "http_status": None, "error": None,
                  "target": "DESKTOP-PC", "ts": now, "throttled": False}
        try:
            req = urllib.request.Request(
                WOL_URL, headers={"User-Agent": "hwmon-tools/1.0"}
            )
            with urllib.request.urlopen(req, timeout=6) as r:
                result["http_status"] = getattr(r, "status", 200)
                r.read(2048)
            result["ok"] = True
        except Exception as e:
            result["http_status"] = getattr(e, "code", None)
            result["error"] = str(e)[:200]
        _last_wol.update(ts=now, result=result)
        return result


# ---------------- HTTP ----------------

class Handler(BaseHTTPRequestHandler):
    server_version = "hwmon/0.6"

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

        if path == "/favicon.ico":
            # 浏览器默认会请求图标；返回空响应，避免控制台刷 404。
            self.send_response(204)
            self.end_headers()
            return

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
            if window in ("5m", "1h", "today"):
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

        if path == "/api/core_history":
            window = (query.get("window") or ["5m"])[0]
            core_id = (query.get("core") or [""])[0]
            if not CORE_ID_RE.fullmatch(core_id):
                self._json({"error": "core 参数应为 P1 / E1 这类核心 id"}, 400)
                return
            try:
                self._json(query_core_history(window, core_id))
            except ValueError as e:
                self._json({"error": str(e)[:160]}, 400)
            except Exception as e:
                self._json({"error": str(e)[:160]}, 503)
            return

        if path == "/api/summary":
            window = (query.get("window") or ["today"])[0]
            try:
                data = window_summary(window)
                with _lock:
                    snap = _state.get("snapshot")
                if snap and snap.get("storage"):
                    data["storage"] = snap["storage"]
                self._json(data)
            except ValueError as e:
                self._json({"error": "window 仅支持 5m、1h 或 today", "detail": str(e)}, 400)
            except Exception as e:
                self._json({"error": str(e)[:160]}, 503)
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

        if path == "/api/easytier":
            self._json(easytier_status())
            return

        if path == "/tools":
            self._serve_file("tools.html", "text/html; charset=utf-8")
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

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/wol":
            self._json(trigger_wol())
            return
        self.send_error(404)

    def _serve_file(self, name, ctype):
        fp = os.path.join(STATIC_DIR, name)
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

    def log_message(self, fmt, *args):
        pass


def dump_sensors():
    """诊断入口：打印 LHM 全部叶子 + 每核汇总 + GPU-Z 末行。"""
    leaves = fetch_lhm()
    print(f"共 {len(leaves)} 个叶子节点\n")
    for lf in leaves:
        v = lf["value"]
        if str(v).strip() in ("", "-"):
            continue
        print(f"{lf['path']}\t{v}")
    cpu_name = _hw_name_any(leaves, ("Core i", "Ryzen", "Core Ultra"))
    cores = collect_cpu_cores(leaves, cpu_name)
    print(f"\n--- 每核汇总：{cores['count']} 核 · 最热 {cores['hottest']} ---")
    for group in cores["groups"]:
        print(f"{group['label']}: " + ", ".join(
            f"{c['id']}={c['temp']}°C/{c['clock']}MHz" for c in group["cores"]
        ))
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
