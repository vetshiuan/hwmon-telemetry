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
from collections import deque, Counter
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

# LTX 资源库已迁到路由器（http://192.0.2.1:8089/ltx/），本机不再保留镜像副本。
# 保留环境变量仅为兼容：若哪天又指向本地目录，路由会自动生效。
LTX_DIR = os.environ.get("HWMON_LTX_DIR", "")

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

# 16pin 读数断更只是「看不见了」，不是电压异常：连续 2 分钟才提示，且只给 warn。
GPUZ_BLIND_SECONDS = 120
_gpuz_state = {"down_since": 0.0}

# 每周用 SQLite 在线备份 API 复制一次数据库，保留最近 4 份。
DB_BACKUP_INTERVAL_DAYS = 7
DB_BACKUP_KEEP = 4
_backup_state = {"last": None}

# 工具页依赖：EasyTier CLI 与路由器 WOL 地址（均可用环境变量覆盖）
EASYTIER_CLI = os.environ.get("EASYTIER_CLI") or default_easytier_cli()
WOL_URL = os.environ.get("HWMON_WOL_URL", "http://192.0.2.1:8088/cgi-bin/wol")
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
    coolest = min(warm, key=lambda c: c["temp"]) if warm else None
    # 核间温差比单看最热值更早反映硅脂/散热器问题；只有一颗核有温度时不计算。
    spread = round(hottest["temp"] - coolest["temp"], 1) if hottest and coolest else None
    threads.sort(key=lambda t: (t["index"], t["thread"]))
    return {
        "available": bool(cores),
        "count": len(cores),
        "groups": groups,
        "hottest": ({"id": hottest["id"], "label": hottest["label"], "temp": hottest["temp"]}
                    if hottest else None),
        "coolest": ({"id": coolest["id"], "label": coolest["label"], "temp": coolest["temp"]}
                    if coolest else None),
        "spread": spread,
        "threads": threads,
        "units": {"temp": "°C", "clock": "MHz", "volt": "V", "dist": "°C", "load": "%"},
    }


def core_layout(cores):
    """把每核温度压成 (核心 id 顺序, JSON 温度数组, 最热核心 id)，供 core_samples 存储。"""
    flat = [c for group in (cores or {}).get("groups", []) for c in group["cores"]]
    if not flat:
        return None
    layout = ",".join(c["id"] for c in flat)
    temps = json.dumps([c["temp"] for c in flat], separators=(",", ":"))
    hottest = (cores.get("hottest") or {}).get("id") or ""
    return layout, temps, hottest


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
    """数据源类告警：与硬件越限、资源提示分级，避免互相淹没。"""
    now = int(time.time() * 1000)
    alerts = []
    sources = snap.get("sources") or {}
    caps = snap.get("capabilities") or {}

    def add(key, label, severity, message, rule="数据源必须持续更新"):
        alerts.append({
            "key": key, "label": label, "value": None, "unit": "",
            "severity": severity, "state": severity, "since": now,
            "message": message, "rule": rule, "category": "source",
        })

    if not sources.get("lhm"):
        add("source.lhm", "LHM", "bad",
            "LHM 断开或 8085 无监听（需 SYSTEM/管理员启动），CPU / 内存 / 磁盘数据不可用")
    elif (snap.get("cpu") or {}).get("temp") is None:
        add("data.cpu_temp", "CPU 温度", "warn", "LHM 在线，但 CPU 温度无有效数据")

    # 16pin 读数断更 = 可观测性故障，不是电压异常：只给 warn，
    # 且连续 2 分钟才提示，避免 GPU-Z GUI 短暂不刷新造成误报。
    if caps.get("gpuz_expected") and not sources.get("gpuz"):
        if not _gpuz_state["down_since"]:
            _gpuz_state["down_since"] = time.time()
        down = time.time() - _gpuz_state["down_since"]
        if down >= GPUZ_BLIND_SECONDS:
            add("source.gpuz", "16pin 电压监测", "warn",
                f"GPU-Z 已 {int(down / 60)} 分钟无更新，16pin 电压读数不可用（非电压异常）",
                "连续 2 分钟无更新才提示；电压真实越限另有独立告警")
    else:
        _gpuz_state["down_since"] = 0.0
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
    """资源提示不参与健康状态判定，只单独计数，避免内存占用盖住硬件/数据源告警。"""
    alerts = snap.get("alerts") or []
    active = [a for a in alerts if a.get("category") != "resource"]
    bad = sum(a.get("severity") == "bad" for a in active)
    warn = sum(a.get("severity") == "warn" for a in active)
    resource = sum(a.get("category") == "resource" for a in alerts)
    status = "bad" if bad else "warn" if warn else "ok"
    label = "运行正常" if status == "ok" else f"{bad} 项严重" if bad else f"{warn} 项预警"
    if warn and bad:
        label += f" · {warn} 项预警"
    return {"status": status, "label": label, "bad": bad, "warn": warn,
            "resource": resource}


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
    }

    # 核显设备不读取独显 GPU-Z 日志，避免适配器身份错配。
    if integrated:
        gz, gz_meta = None, {"available": False, "expected": False, "age_ms": None, "error": "核显设备不适用"}
    else:
        gz, gz_meta = fetch_gpuz()
    snap["source_meta"]["gpuz"] = gz_meta
    snap["sources"]["gpuz"] = bool(gz_meta.get("available"))
    # gpuz_expected 只在这里赋值一次：设备已识别出独立显卡型号，或 GPU-Z 日志标记为预期存在。
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
                temps TEXT NOT NULL,
                hottest TEXT NOT NULL DEFAULT ''
            )
        """)
        # 旧库补 hottest 列（幂等），用于统计「哪颗核长期最热」。
        core_cols = {row[1] for row in conn.execute("PRAGMA table_info(core_samples)")}
        if "hottest" not in core_cols:
            conn.execute("ALTER TABLE core_samples ADD COLUMN hottest TEXT NOT NULL DEFAULT ''")


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
    layout, temps, hottest = packed
    with db_session() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO core_samples (ts, layout, temps, hottest) VALUES (?, ?, ?, ?)",
            (snap["ts"], layout, temps, hottest),
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
            "SELECT ts, layout, temps, hottest FROM core_samples WHERE ts >= ? AND ts <= ? ORDER BY ts",
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
    # 顺便统计窗口内「哪颗核最热」，供弹层展示是否长期同一颗核在顶。
    counts = Counter(r["hottest"] for r in rows if r["hottest"])
    top = counts.most_common(1)[0] if counts else None
    hottest_top = ({"id": top[0], "pct": round(top[1] * 100 / max(1, len(rows)))}
                   if top else None)
    return {
        "core": core_id, "window": window, "label": label,
        "t": times, "temp": values,
        "_meta": {"start": start, "end": end, "bucket_ms": bucket,
                  "samples": len(rows), "hottest_top": hottest_top},
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


# ---------------- 大模型用量统计（llm_usage） ----------------
# 数据源是 WorkBuddy 会话日志 ~/.workbuddy/projects/<项目slug>/<sessionId>.jsonl，
# 不是 ~/.workbuddy/traces/（trace 只有 span 时序，没有 model 也没有 token 用量）。
#
# 字段陷阱（2026-11-12 实测 27,113 条记录确认）：
#   · rawUsage.cached_tokens 恒为 0，缓存命中必须取 prompt_tokens_details.cached_tokens；
#     该字段缺失时回退 prompt_cache_hit_tokens（两者 99.1% 同值）。
#   · 顶层 id 全局唯一，直接做主键即天然幂等，不必用「文件 + 行号」这种会随重写失效的键。
#   · 日志里没有 duration / latency / ttft 任何耗时字段，只有 timestamp（毫秒）。
#     所以耗时用「同会话相邻调用的时间差」估算，字段名刻意叫 step_ms 而不是 duration_ms。
LLM_ROOT = os.environ.get("HWMON_LLM_ROOT") or os.path.join(
    os.path.expanduser("~"), ".workbuddy", "projects")
LLM_DB_PATH = os.environ.get("HWMON_LLM_DB") or os.path.join(STATIC_DIR, "llm_usage.db")
LLM_SCAN_INTERVAL = 15.0     # 最小扫描间隔：连续刷新页面时不重复扫盘
LLM_READ_CHUNK = 4 << 20     # 单文件单次最多读 4MB，首次建库也分片落地，不会一次吃满内存
LLM_LINE_MAX = 64 << 20      # 单行硬上限 64MB：超长行（Base64/大段工具输出）丢弃并推进 offset
LLM_STEP_MAX = 60_000        # 步进间隔只统计 60 秒内的连续步进（毫秒），排除人工停顿
LLM_WINDOWS = ("today", "7d", "30d", "all")
LLM_WINDOW_DAYS = {"today": 1, "7d": 7, "30d": 30}
LLM_CACHE_TTL = 45.0         # 比后台刷新周期（20s）长：正常状态下请求永远命中预聚合结果

_llm_lock = threading.Lock()        # 扫描互斥：多个 HTTP 线程同时触发只跑一次
_llm_scan_state = {"ts": 0.0, "rows": 0, "err": None, "done": False}
_llm_resp_cache = {}                # window -> (ts, payload)：纯读缓存，保证切换秒回


def llm_device_name():
    """与 /api/snapshot 的 device 保持一致口径：LAPTOP-01 -> laptop，DESKTOP-01 -> desktop。"""
    host = (os.environ.get("HWMON_DEVICE") or socket.gethostname() or "").lower()
    if "laptop" in host:
        return "laptop"
    if host.endswith("-pc") or "desktop" in host:
        return "desktop"
    return host or "unknown"


def llm_db_connect():
    conn = sqlite3.connect(LLM_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# 聚合查询专用常驻连接：每次请求新建连接会让 SQLite 页缓存从零开始，
# /all 首次查询要多花 ~110ms 把索引导回内存。常驻连接 + 20MB 页缓存后稳定在 ~40ms。
_llm_read = {"conn": None, "lock": threading.Lock()}


def llm_read_conn():
    conn = _llm_read["conn"]
    if conn is None:
        conn = sqlite3.connect(LLM_DB_PATH, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA cache_size=-20000")
        _llm_read["conn"] = conn
    return conn


def llm_db_init():
    """独立库，不塞进 hwmon.db：那边的 7 天清理与高频采样 WAL 不该牵连用量账本。"""
    conn = sqlite3.connect(LLM_DB_PATH, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS llm_calls (
                call_id TEXT PRIMARY KEY,
                ts INTEGER NOT NULL,
                ts_ms INTEGER NOT NULL DEFAULT 0,
                date_str TEXT NOT NULL,
                hour_str TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT 'unknown',
                session_id TEXT NOT NULL DEFAULT '',
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                cached_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                step_ms INTEGER
            )
        """)
        # 旧库补 ts_ms 列（幂等）：毫秒精度用于算同会话步进间隔
        cols = {row[1] for row in conn.execute("PRAGMA table_info(llm_calls)")}
        if "ts_ms" not in cols:
            conn.execute("ALTER TABLE llm_calls ADD COLUMN ts_ms INTEGER NOT NULL DEFAULT 0")
        # v6.1 迁移：早期入库的行没有 ts_ms，step_ms 也是按「600 秒」旧门限算的。
        # 明细完全能从 JSONL 重建，直接清空让后台线程重扫一次（约 4 秒），比逐行猜更干净。
        stale = conn.execute("SELECT COUNT(*) FROM llm_calls WHERE ts_ms=0").fetchone()
        if stale and stale[0]:
            conn.execute("DELETE FROM llm_calls")
            conn.execute("DELETE FROM llm_file_state")
            conn.commit()
        conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_ts ON llm_calls(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_model ON llm_calls(model)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_date ON llm_calls(date_str)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_session ON llm_calls(session_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_sess_ts"
                     " ON llm_calls(session_id, ts_ms)")
        # 覆盖索引：聚合查询只走索引不回表，/all 窗口三类聚合从 ~49ms 降到 ~36ms
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_agg ON llm_calls("
            "ts, model, date_str, session_id, prompt_tokens, cached_tokens,"
            " completion_tokens, reasoning_tokens, step_ms)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS llm_sessions (
                session_id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                cwd TEXT NOT NULL DEFAULT '',
                last_ts INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS llm_file_state (
                path TEXT PRIMARY KEY,
                mtime REAL NOT NULL DEFAULT 0,
                size INTEGER NOT NULL DEFAULT 0,
                byte_offset INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()
    finally:
        conn.close()


def _llm_iter_files():
    for dirpath, _dirnames, names in os.walk(LLM_ROOT):
        for name in names:
            if name.endswith(".jsonl"):
                yield os.path.join(dirpath, name)


def _llm_prev_ts(conn, known, session_id):
    """同会话上一次调用的**毫秒**时间戳；扫描期间缓存，避免每条都查库。

    必须用毫秒：同一秒内的连续步进（例如两三次快速工具调用）如果先截断到秒，
    差值恒为 0 会被当成异常丢弃，TPS 分母就只剩慢请求了。
    """
    if session_id in known:
        return known[session_id]
    last = None
    try:
        row = conn.execute(
            "SELECT MAX(ts_ms) FROM llm_calls WHERE session_id=? AND ts_ms>0",
            (session_id,)).fetchone()
        if row and row[0]:
            last = int(row[0])
        else:
            row = conn.execute("SELECT MAX(ts) FROM llm_calls WHERE session_id=?",
                               (session_id,)).fetchone()
            last = int(row[0]) * 1000 if row and row[0] else None
    except Exception:
        last = None
    known[session_id] = last
    return last


def _llm_upsert_session(conn, sid, title, cwd, last_ts):
    """会话标题来自同文件的 ai-title 行；后到的标题覆盖旧的，空的绝不覆盖。"""
    row = conn.execute("SELECT title, cwd, last_ts FROM llm_sessions WHERE session_id=?",
                       (sid,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO llm_sessions (session_id, title, cwd, last_ts) VALUES (?,?,?,?)",
                     (sid, title or "", cwd or "", last_ts))
        return
    merged_title = title or row["title"] or ""
    merged_cwd = cwd or row["cwd"] or ""
    merged_ts = max([v for v in (row["last_ts"], last_ts) if v] or [None]) if (
        row["last_ts"] or last_ts) else None
    conn.execute("UPDATE llm_sessions SET title=?, cwd=?, last_ts=? WHERE session_id=?",
                 (merged_title, merged_cwd, merged_ts, sid))


def _llm_handle_line(conn, raw, rows, sessions, known):
    """一行日志：要么是会话标题，要么是带 usage 的模型调用；其余直接丢弃。"""
    try:
        text = raw.decode("utf-8", "replace")
    except Exception:
        return
    if '"aiTitle"' in text:
        try:
            d = json.loads(text)
        except Exception:
            return
        title = (d.get("aiTitle") or "").strip()
        sid = d.get("sessionId") or ""
        if sid and title:
            sessions.append((sid, title[:120], d.get("cwd") or "", None))
        return
    if '"rawUsage"' not in text:
        return
    try:
        d = json.loads(text)
        pd = d.get("providerData") or {}
        ru = pd.get("rawUsage")
        if not isinstance(ru, dict):
            return
        call_id = d.get("id") or ""
        ts_ms = d.get("timestamp") or 0
        if not call_id or not ts_ms:
            return
        ts = int(ts_ms / 1000)
        local = dt.datetime.fromtimestamp(ts)
        prompt = int(ru.get("prompt_tokens") or 0)
        details = ru.get("prompt_tokens_details") or {}
        cached = int(details.get("cached_tokens") or 0)
        if not cached:
            cached = int(ru.get("prompt_cache_hit_tokens") or 0)
        completion = int(ru.get("completion_tokens") or 0)
        cdetail = ru.get("completion_tokens_details") or {}
        reasoning = int(cdetail.get("reasoning_tokens") or 0)
        if not reasoning:
            reasoning = int(ru.get("completion_thinking_tokens") or 0)
        sid = d.get("sessionId") or ""
        step = None
        last_ms = _llm_prev_ts(conn, known, sid) if sid else None
        if last_ms and 0 < ts_ms - last_ms <= LLM_STEP_MAX:
            step = ts_ms - last_ms          # 毫秒级差，60 秒内才算一次「连续步进」
        if sid:
            known[sid] = max(ts_ms, last_ms or 0)
        rows.append((
            call_id, ts, ts_ms, local.strftime("%Y-%m-%d"), local.strftime("%Y-%m-%d %H:00"),
            (pd.get("model") or pd.get("requestModelName") or "unknown"), sid,
            prompt, cached, completion, reasoning, step,
        ))
    except Exception:
        # 转义损坏 / 截断的半行 / 字段类型异常：跳过这一行，绝不让扫描器抛出去
        return


def _llm_read_file(conn, path, offset, known):
    """从 offset 处读新增行，返回 (明细行, 会话行, 新 offset)。

    用 readline 逐行读，而不是「一次读 4MB 再找最后一个换行」：
    旧写法遇到单行超过 4MB（大段工具输出 / Base64）时 rfind 返回 -1 直接 break，
    byte_offset 会永久卡死在那一行，之后所有新增数据全部漏统计。
    现在只有三种结局——完整行消费掉、末尾半行留到下次、超长行丢弃并推进到下个换行，
    offset 永远单调前进。
    """
    rows, sessions = [], []
    consumed = offset
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            while True:
                start = fh.tell()
                line = fh.readline(LLM_LINE_MAX + 1)
                if not line:
                    break
                if line.endswith(b"\n"):
                    consumed = fh.tell()
                    _llm_handle_line(conn, line.rstrip(b"\r\n"), rows, sessions, known)
                    continue
                if len(line) > LLM_LINE_MAX:
                    # 超长行：丢掉这一行，继续读到下一个换行，保证 offset 前进且内存有界
                    while True:
                        more = fh.readline(LLM_READ_CHUNK)
                        if not more or more.endswith(b"\n"):
                            break
                    consumed = fh.tell()
                    continue
                # 既没换行又没超限：文件正在追加，这半行留给下次扫描
                fh.seek(start)
                break
    except OSError:
        return [], [], offset
    return rows, sessions, consumed


def _llm_scan_locked():
    conn = llm_db_connect()
    added = 0
    known = {}
    try:
        states = {}
        for row in conn.execute("SELECT path, mtime, size, byte_offset FROM llm_file_state"):
            states[row["path"]] = (row["mtime"], row["size"], row["byte_offset"])
        for seq, path in enumerate(_llm_iter_files()):
            # 每 120 个文件让出 2ms：不然一轮扫盘（632 个 stat + 解析）会长时间占着 GIL，
            # 同一进程里的 HTTP 线程被硬生生拖慢 ~100ms，表现为接口偶发尖峰。
            if seq and seq % 120 == 0:
                time.sleep(0.002)
            try:
                st = os.stat(path)
            except OSError:
                continue
            mtime, size, offset = states.get(path, (0.0, -1, 0))
            if size == st.st_size and abs(mtime - st.st_mtime) < 0.001:
                continue
            if st.st_size < offset:
                offset = 0        # 文件被截断或重写：从头再来，主键保证不会重复计数
            rows, sessions, new_offset = _llm_read_file(conn, path, offset, known)
            if rows:
                conn.executemany(
                    "INSERT OR IGNORE INTO llm_calls (call_id, ts, ts_ms, date_str, hour_str,"
                    " model, session_id, prompt_tokens, cached_tokens, completion_tokens,"
                    " reasoning_tokens, step_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                added += len(rows)
            for sid, title, cwd, last_ts in sessions:
                _llm_upsert_session(conn, sid, title, cwd, last_ts)
            conn.execute(
                "INSERT OR REPLACE INTO llm_file_state (path, mtime, size, byte_offset)"
                " VALUES (?,?,?,?)", (path, st.st_mtime, st.st_size, new_offset))
            conn.commit()
    finally:
        conn.close()
    return added


def llm_scan(force=False, nonblock=False):
    """轻量增量扫描（默认 15 秒防抖）。任何异常都吞掉，绝不影响 agent 主服务。

    nonblock=True 时拿不到锁就直接返回：首屏请求不陪 3 秒冷扫描一起等，
    用上一次的结果（或 warming 标记）先出页面。
    """
    now = time.time()
    if nonblock:
        if not _llm_lock.acquire(blocking=False):
            return 0
    else:
        _llm_lock.acquire()
    try:
        if not force and now - _llm_scan_state["ts"] < LLM_SCAN_INTERVAL:
            return 0
        try:
            added = _llm_scan_locked()
            _llm_scan_state["rows"] = added
            _llm_scan_state["err"] = None
            _llm_scan_state["done"] = True
        except Exception as e:
            added = 0
            _llm_scan_state["err"] = str(e)[:160]
        _llm_scan_state["ts"] = time.time()
        if added:
            _llm_resp_cache.clear()
        return added
    finally:
        _llm_lock.release()


def llm_poller():
    """后台索引线程：启动即建库并全量补一次（458MB 约 2~3 秒），之后每 20 秒扫新增。

    扫完顺手把四个窗口的聚合结果预算好（预聚合）。这样请求路径只剩一次字典查找：
    实测在运行中的 agent 里现场聚合要 90~110ms（读线程与扫描线程争 GIL + WAL），
    预算后降到 2~3ms，且旧数据不会因为慢查询而闪白。
    """
    try:
        llm_db_init()
    except Exception as e:
        print("llm_usage 数据库初始化失败: %s" % e)
        return
    last_hour = None
    while True:
        added = 0
        try:
            added = llm_scan(force=True)
        except Exception as e:
            print("llm_usage 扫描失败: %s" % str(e)[:120])
        hour_key = dt.datetime.now().strftime("%Y-%m-%d %H")
        # 空闲时（没有新记录、也没跨小时）不重跑 4 轮聚合：数据没变就不必重算，
        # 只给缓存续期，避免 45 秒 TTL 到期后由请求线程去跑 ~100ms 的 SQL。
        if added or last_hour != hour_key or not _llm_resp_cache:
            for window in LLM_WINDOWS:
                try:
                    _llm_resp_cache.pop(window, None)   # 强制重算，别让后台刷新被 TTL 挡住
                    llm_summary(window)
                except Exception as e:
                    print("llm_usage 预聚合失败 %s: %s" % (window, str(e)[:120]))
            last_hour = hour_key
        else:
            now = time.time()
            for window in list(_llm_resp_cache.keys()):
                _llm_resp_cache[window] = (now, _llm_resp_cache[window][1])
        time.sleep(20)


def _llm_window_bounds(window):
    """返回 (start_ts, end_ts)。全部按本地自然日对齐，两台机器分桶后才能逐桶相加。"""
    end = int(time.time())
    if window == "all":
        return 0, end
    days = LLM_WINDOW_DAYS.get(window, 1)
    midnight = dt.datetime.combine(dt.datetime.now().date(), dt.time.min)
    start = midnight - dt.timedelta(days=days - 1)
    return int(start.timestamp()), end


def _llm_rate(part, whole):
    return round(part * 100.0 / whole, 2) if whole else 0.0


def _llm_tps(completion, step_sum_ms):
    return round(completion / (step_sum_ms / 1000.0), 1) if step_sum_ms else None


def llm_summary(window="all"):
    """GET /api/llm/summary 的数据体。first 请求会触发一次增量扫描。"""
    if window not in LLM_WINDOWS:
        raise ValueError("window=%s" % window)
    hit = _llm_resp_cache.get(window)
    if hit and time.time() - hit[0] < LLM_CACHE_TTL:
        return hit[1]

    # 扫描只在后台线程跑：请求路径上扫盘（632 个文件 stat + 首批解析）会让
    # /all 首次请求从 35ms 抖到 480ms。只有冷启动尚未索引完时才引导一次（不阻塞）。
    if not _llm_scan_state["done"]:
        llm_scan(nonblock=True)
    start, end = _llm_window_bounds(window)
    build_t0 = time.time()
    _llm_read["lock"].acquire()
    try:
        conn = llm_read_conn()
        # today 按小时分桶，其余按天；bucket 用带日期的完整串，方便路由器逐桶对齐合并
        key = "hour_str" if window == "today" else "date_str"
        buckets = conn.execute(
            "SELECT %s b, COUNT(*) c, SUM(prompt_tokens) p, SUM(cached_tokens) ca,"
            " SUM(completion_tokens) co, SUM(reasoning_tokens) re,"
            " SUM(COALESCE(step_ms,0)) st_sum, COUNT(step_ms) st_cnt"
            " FROM llm_calls WHERE ts>=? AND ts<=? GROUP BY b ORDER BY b" % key,
            (start, end)).fetchall()
        # KPI 直接对分桶求和：保证「四宫格口径」与「趋势图口径」永远一致，也少扫一遍全表
        total_calls = sum(r["c"] for r in buckets)
        prompt_sum = sum(r["p"] or 0 for r in buckets)
        cached_sum = sum(r["ca"] or 0 for r in buckets)
        completion_sum = sum(r["co"] or 0 for r in buckets)
        reasoning_sum = sum(r["re"] or 0 for r in buckets)
        step_sum = sum(r["st_sum"] or 0 for r in buckets)
        step_cnt = sum(r["st_cnt"] or 0 for r in buckets)
        today_str = dt.date.today().isoformat()
        today_calls = conn.execute("SELECT COUNT(*) FROM llm_calls WHERE date_str=?",
                                   (today_str,)).fetchone()[0]
        by_model = []
        for row in conn.execute(
                "SELECT model, COUNT(*) calls, SUM(prompt_tokens) p, SUM(cached_tokens) ca,"
                " SUM(completion_tokens) co, SUM(reasoning_tokens) re,"
                " AVG(step_ms) st, SUM(step_ms) st_sum"
                " FROM llm_calls WHERE ts>=? AND ts<=? GROUP BY model"
                " ORDER BY calls DESC, p DESC", (start, end)):
            by_model.append({
                "model": row["model"] or "unknown",
                "calls": row["calls"],
                "prompt_tokens": row["p"] or 0,
                "cached_tokens": row["ca"] or 0,
                "cache_hit_rate": _llm_rate(row["ca"] or 0, row["p"] or 0),
                "completion_tokens": row["co"] or 0,
                "reasoning_tokens": row["re"] or 0,
                "avg_step_ms": int(row["st"] or 0),
                "avg_tps": _llm_tps(row["co"] or 0, row["st_sum"] or 0),
            })
        timeline = [{
            "bucket": row["b"],
            "calls": row["c"],
            "prompt_tokens": row["p"] or 0,
            "cached_tokens": row["ca"] or 0,
            "completion_tokens": row["co"] or 0,
        } for row in buckets]
        tops = []
        for row in conn.execute(
                "SELECT session_id, COUNT(*) calls, SUM(prompt_tokens) p, SUM(cached_tokens) ca,"
                " SUM(completion_tokens) co, MAX(ts) last_ts"
                " FROM llm_calls WHERE ts>=? AND ts<=? AND session_id<>''"
                " GROUP BY session_id ORDER BY p DESC LIMIT 10", (start, end)):
            tops.append(row)
        titles = {}
        ids = [r["session_id"] for r in tops]
        if ids:
            marks = ",".join("?" for _ in ids)
            for row in conn.execute("SELECT session_id, title FROM llm_sessions"
                                    " WHERE session_id IN (%s)" % marks, ids):
                titles[row["session_id"]] = row["title"] or ""
        top_sessions = [{
            "session_id": r["session_id"],
            "title": (titles.get(r["session_id"]) or "").strip() or r["session_id"][:8],
            "calls": r["calls"],
            "prompt_tokens": r["p"] or 0,
            "uncached_tokens": max(0, (r["p"] or 0) - (r["ca"] or 0)),
            "completion_tokens": r["co"] or 0,
            "last_active": dt.datetime.fromtimestamp(r["last_ts"]).strftime("%Y-%m-%d %H:%M"),
        } for r in tops]
        index = conn.execute(
            "SELECT COUNT(*) files, COALESCE(SUM(byte_offset),0) bytes FROM llm_file_state"
        ).fetchone()
        span = conn.execute("SELECT MIN(ts) a, MAX(ts) b, COUNT(*) n FROM llm_calls").fetchone()
    finally:
        # 常驻连接不关闭：页缓存留着，下一次请求直接命中
        _llm_read["lock"].release()

    payload = {
        "device": llm_device_name(),
        "updated_at": int(time.time()),
        "window": window,
        # 让路由器能判断这份数据（尤其离线缓存）是不是已经跨天，避免把昨天的 today 算进今天
        "today_date": dt.date.today().isoformat(),
        "kpi": {
            "total_calls": total_calls,
            "today_calls": today_calls,
            "prompt_tokens": prompt_sum,
            "cached_tokens": cached_sum,
            "cache_hit_rate": _llm_rate(cached_sum, prompt_sum),
            "uncached_tokens": max(0, prompt_sum - cached_sum),
            "completion_tokens": completion_sum,
            "reasoning_tokens": reasoning_sum,
            "reasoning_ratio": _llm_rate(reasoning_sum, completion_sum),
            # 不是真实延迟：日志无耗时字段，这里取「同会话相邻调用间隔」的均值，前端标注为步进耗时
            "avg_step_ms": int(step_sum / step_cnt) if step_cnt else 0,
            "avg_tps": _llm_tps(completion_sum, step_sum),
        },
        "by_model": by_model,
        "timeline": timeline,
        "top_sessions": top_sessions,
        "index": {
            "files": index["files"],
            "indexed_bytes": index["bytes"],
            "all_calls": span["n"] or 0,
            "first_ts": span["a"],
            "last_ts": span["b"],
            "warmup": not _llm_scan_state["done"],
            "build_ms": int((time.time() - build_t0) * 1000),
            "last_scan": int(_llm_scan_state["ts"] or 0),
            "last_scan_error": _llm_scan_state["err"],
        },
    }
    _llm_resp_cache[window] = (time.time(), payload)
    return payload


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
                    # 每周在线备份；失败只记录，不影响采样与页面。
                    try:
                        _backup_state["last"] = maybe_backup_db()
                    except Exception as e:
                        _backup_state["last"] = {"ok": False, "error": str(e)[:120]}
                    last_cleanup = time.time()
            except Exception as storage_error:
                snap["storage"] = {"ok": False, "error": str(storage_error)[:120]}
                snap["alerts"].append({
                    "key": "storage.daily", "label": "今日统计存储", "value": None, "unit": "",
                    "severity": "warn", "state": "warn", "since": snap["ts"],
                    "message": "今日统计采样写入失败", "rule": str(storage_error)[:120],
                    "category": "source",
                })
                snap["health"] = build_health(snap)
            snap["db_backup"] = _backup_state["last"]
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
                  "target": "DESKTOP-01", "ts": now, "throttled": False}
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


# ---------------- 自检与备份 ----------------

def lhm_diagnostics(snap):
    """/health 用：8085 监听、LHM 错误与 agent 权限提示（毫秒级，不额外请求 LHM）。"""
    listening = False
    try:
        with socket.create_connection(("127.0.0.1", 8085), timeout=0.4):
            listening = True
    except OSError:
        pass
    meta = (snap.get("source_meta") or {}).get("lhm") or {}
    try:
        elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        elevated = False
    return {
        "available": bool((snap.get("sources") or {}).get("lhm")),
        "web_server_port": 8085,
        "port_listening": listening,
        "error": meta.get("error"),
        "agent_elevated": elevated,
        "hint": "" if listening else "8085 未监听：LHM 需以 SYSTEM/管理员身份启动",
    }


def maybe_backup_db():
    """每周用 SQLite 在线备份 API 复制一次 hwmon.db，保留最近 4 份。

    在线备份能正确处理 WAL，不能直接 copy 文件；可用 HWMON_BACKUP_DIR 改目标目录。
    """
    backup_dir = (os.environ.get("HWMON_BACKUP_DIR")
                  or os.path.join(os.path.dirname(DB_PATH), "backups", "db"))
    os.makedirs(backup_dir, exist_ok=True)
    stamp = os.path.join(backup_dir, ".last_backup")
    now = time.time()
    try:
        with open(stamp, "r", encoding="utf-8") as f:
            last = float((f.read() or "0").strip())
    except (OSError, ValueError):
        last = 0.0
    if now - last < DB_BACKUP_INTERVAL_DAYS * 86400:
        return {"ok": True, "skipped": True}
    target = os.path.join(backup_dir, "hwmon-%s.db" % dt.datetime.now().strftime("%Y%m%d"))
    src, dst = db_connect(), sqlite3.connect(target)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()
    with open(stamp, "w", encoding="utf-8") as f:
        f.write(str(now))
    files = sorted(n for n in os.listdir(backup_dir)
                   if n.startswith("hwmon-") and n.endswith(".db"))
    for name in files[:-DB_BACKUP_KEEP]:
        try:
            os.remove(os.path.join(backup_dir, name))
        except OSError:
            pass
    return {"ok": True, "skipped": False, "path": target,
            "kept": len(files[-DB_BACKUP_KEEP:])}


# ---------------- 应用时长与进程资源遥测（ctypes only） ----------------
# 不使用 psutil / pywin32，也不在轮询线程中启动任何外部进程。
from ctypes import wintypes

APP_POLL_INTERVAL = 5.0
APP_COMMIT_INTERVAL = 30.0
APP_DB_PATH = os.environ.get("HWMON_APP_DB", r"C:\hwsensor\app_usage.db")
APP_WINDOWS = ("today", "7d", "30d", "all")
TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]

class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong), ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_ulong), ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong), ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong), ("szExeFile", ctypes.c_wchar * 260),
    ]

class _PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]

_APP_CATEGORIES = {
    "生产力与AI": {"workbuddy.exe", "code.exe", "cursor.exe", "windsurf.exe", "devenv.exe", "pycharm64.exe", "idea64.exe", "obsidian.exe", "notion.exe", "winword.exe", "excel.exe", "powerpnt.exe", "wps.exe", "windowsterminal.exe", "powershell.exe", "cmd.exe", "python.exe"},
    "浏览器": {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "arc.exe", "browser.exe"},
    "游戏与娱乐": {"steam.exe", "steamwebhelper.exe", "epicgameslauncher.exe", "potplayermini64.exe", "vlc.exe", "cloudmusic.exe", "qqmusic.exe"},
    "社交与通讯": {"weixin.exe", "wechat.exe", "qq.exe", "telegram.exe", "discord.exe", "feishu.exe", "dingtalk.exe"},
    "系统与服务": {"explorer.exe", "dwm.exe", "svchost.exe", "searchhost.exe", "easytier-core.exe", "hipstray.exe", "wsctrlsvc.exe", "librehardwaremonitor.exe", "gpu-z.exe", "pythonw.exe"},
}
_APP_STATE_LOCK = threading.Lock()
_APP_STATE = {"updated_at": 0, "is_afk": True, "idle_sec": 0.0, "fg_app": "", "fg_category": "其他应用", "fg_title": "", "total_processes": 0, "total_ws_mb": 0, "total_commit_mb": 0, "top_live": [], "top_live_cpu": [], "rows": []}
_APP_RESP_CACHE_LOCK = threading.Lock()
_APP_RESP_CACHE = {}
_APP_PREV_PROC = {}
_APP_PREV_SYS = None
_APP_LAST_COMMIT = 0.0


def app_category(app_key):
    for category, names in _APP_CATEGORIES.items():
        if app_key in names:
            return category
    return "其他应用"


def _filetime_value(value):
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


def _configure_app_winapi():
    """声明指针/句柄签名，避免 64 位 Python 截断 HANDLE。"""
    k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    k32.Process32FirstW.restype = wintypes.BOOL
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    k32.Process32NextW.restype = wintypes.BOOL
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(_FILETIME)] * 4
    k32.GetProcessTimes.restype = wintypes.BOOL
    k32.GetSystemTimes.argtypes = [ctypes.POINTER(_FILETIME)] * 3
    k32.GetSystemTimes.restype = wintypes.BOOL
    k32.GetTickCount.restype = wintypes.DWORD
    u32.GetForegroundWindow.restype = wintypes.HWND
    u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u32.GetWindowThreadProcessId.restype = wintypes.DWORD
    u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u32.GetWindowTextW.restype = ctypes.c_int
    u32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]
    u32.GetLastInputInfo.restype = wintypes.BOOL
    fn = getattr(k32, "K32GetProcessMemoryInfo", None)
    if fn:
        fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESS_MEMORY_COUNTERS_EX), wintypes.DWORD]
        fn.restype = wintypes.BOOL
    else:
        psapi = ctypes.windll.psapi
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESS_MEMORY_COUNTERS_EX), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL


def app_db_connect():
    conn = sqlite3.connect(APP_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=3000")
    return conn


def app_db_init():
    parent = os.path.dirname(os.path.abspath(APP_DB_PATH))
    os.makedirs(parent, exist_ok=True)
    conn = app_db_connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS app_daily_stats (
            date_str TEXT NOT NULL, app_key TEXT NOT NULL, display_name TEXT NOT NULL,
            category TEXT NOT NULL, fg_active_sec REAL NOT NULL DEFAULT 0,
            fg_idle_sec REAL NOT NULL DEFAULT 0, bg_run_sec REAL NOT NULL DEFAULT 0,
            bg_busy_sec REAL NOT NULL DEFAULT 0, cpu_pct_sum REAL NOT NULL DEFAULT 0,
            cpu_peak_pct REAL NOT NULL DEFAULT 0, ws_mb_sum REAL NOT NULL DEFAULT 0,
            ws_peak_mb REAL NOT NULL DEFAULT 0, commit_mb_sum REAL NOT NULL DEFAULT 0,
            commit_peak_mb REAL NOT NULL DEFAULT 0, samples INTEGER NOT NULL DEFAULT 0,
            max_procs INTEGER NOT NULL DEFAULT 0, last_ts INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(date_str, app_key))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_app_daily_date ON app_daily_stats(date_str)")
        conn.execute("""CREATE TABLE IF NOT EXISTS app_hourly_timeline (
            hour_str TEXT PRIMARY KEY, date_str TEXT NOT NULL,
            fg_active_sec REAL NOT NULL DEFAULT 0, afk_sec REAL NOT NULL DEFAULT 0,
            cpu_sum REAL NOT NULL DEFAULT 0, ws_sum REAL NOT NULL DEFAULT 0,
            commit_sum REAL NOT NULL DEFAULT 0, samples INTEGER NOT NULL DEFAULT 0)""")
        conn.commit()
    finally:
        conn.close()


def _app_input_state():
    lii = _LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0, True
    idle = ((ctypes.windll.kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF) / 1000.0
    return idle, idle >= 180.0


def _app_foreground():
    u32 = ctypes.windll.user32
    hwnd = u32.GetForegroundWindow()
    pid = wintypes.DWORD(0)
    title = ""
    if hwnd:
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        buf = ctypes.create_unicode_buffer(512)
        u32.GetWindowTextW(hwnd, buf, len(buf))
        # 去除控制字符、路径式用户名片段，并限制长度，避免把完整敏感标题存入数据库。
        title = re.sub(r"[\x00-\x1f\x7f]", " ", buf.value)
        title = re.sub(r"(?i)[A-Z]:\\Users\\[^\\\s]+", r"[用户目录]", title)[:60]
    return int(pid.value), title


def _app_memory(handle):
    counters = _PROCESS_MEMORY_COUNTERS_EX()
    counters.cb = ctypes.sizeof(counters)
    k32 = ctypes.windll.kernel32
    fn = getattr(k32, "K32GetProcessMemoryInfo", None)
    if fn is None:
        fn = ctypes.windll.psapi.GetProcessMemoryInfo
    if not fn(handle, ctypes.byref(counters), counters.cb):
        return 0.0, 0.0
    return counters.WorkingSetSize / (1024.0 ** 2), counters.PrivateUsage / (1024.0 ** 2)


def app_sample_once():
    """单次 Win32 枚举与归并；进程句柄在 finally 中逐个关闭。"""
    global _APP_PREV_PROC, _APP_PREV_SYS
    t0 = time.perf_counter()
    k32 = ctypes.windll.kernel32
    idle_ft, kernel_ft, user_ft = _FILETIME(), _FILETIME(), _FILETIME()
    if not k32.GetSystemTimes(ctypes.byref(idle_ft), ctypes.byref(kernel_ft), ctypes.byref(user_ft)):
        raise OSError("GetSystemTimes failed")
    sys_total = _filetime_value(kernel_ft) + _filetime_value(user_ft)
    delta_sys = max(0, sys_total - (_APP_PREV_SYS if _APP_PREV_SYS is not None else sys_total))
    _APP_PREV_SYS = sys_total
    idle_sec, is_afk = _app_input_state()
    fg_pid, fg_title = _app_foreground()
    snapshot = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    invalid = ctypes.c_void_p(-1).value
    if not snapshot or ctypes.cast(snapshot, ctypes.c_void_p).value == invalid:
        raise OSError("CreateToolhelp32Snapshot failed")
    rows, current = [], {}
    fg_exe = ""
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            pid = int(entry.th32ProcessID)
            exe = entry.szExeFile or ""
            if pid not in (0, 4) and exe:
                if pid == fg_pid:
                    fg_exe = exe
                handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                if handle:
                    try:
                        c, e, kt, ut = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
                        if not k32.GetProcessTimes(handle, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut)):
                            ok = k32.Process32NextW(snapshot, ctypes.byref(entry))
                            continue
                        created = _filetime_value(c)
                        proc_key = (pid, created)
                        proc_total = _filetime_value(kt) + _filetime_value(ut)
                        prev = _APP_PREV_PROC.get(proc_key)
                        delta_proc = max(0, proc_total - prev) if prev is not None else 0
                        cpu = min(100.0, max(0.0, delta_proc * 100.0 / delta_sys)) if delta_sys else 0.0
                        ws_mb, commit_mb = _app_memory(handle)
                        app_key = exe.lower()
                        rows.append({"app_key": app_key, "display_name": exe, "category": app_category(app_key),
                                     "pid": pid, "cpu_pct": cpu, "ws_mb": ws_mb, "commit_mb": commit_mb})
                        current[proc_key] = proc_total
                    finally:
                        k32.CloseHandle(handle)
            ok = k32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snapshot)
    _APP_PREV_PROC = current
    grouped = {}
    for row in rows:
        g = grouped.setdefault(row["app_key"], {"app": row["display_name"], "category": row["category"],
            "procs": 0, "cpu_pct": 0.0, "ws_mb": 0.0, "commit_mb": 0.0, "is_fg": False, "pids": set()})
        g["procs"] += 1; g["cpu_pct"] += row["cpu_pct"]
        g["ws_mb"] += row["ws_mb"]; g["commit_mb"] += row["commit_mb"]
        g["is_fg"] = g["is_fg"] or row["pid"] == fg_pid
        g["pids"].add(row["pid"])
    live = []
    for g in grouped.values():
        g.pop("pids", None)
        for key in ("cpu_pct", "ws_mb", "commit_mb"):
            g[key] = round(g[key], 2 if key != "cpu_pct" else 1)
        live.append(g)
    live.sort(key=lambda x: (x["commit_mb"], x["ws_mb"], x["cpu_pct"]), reverse=True)
    fg = next((r for r in rows if r["pid"] == fg_pid), None)
    if fg and fg["app_key"] in ("lockapp.exe", "logonui.exe"):
        is_afk = True
    state = {"updated_at": int(time.time()), "is_afk": bool(is_afk), "idle_sec": round(idle_sec, 1),
             "fg_app": fg["display_name"] if fg and fg["app_key"] not in ("lockapp.exe", "logonui.exe") else "",
             "fg_category": fg["category"] if fg else "其他应用", "fg_title": fg_title,
             "total_processes": len(rows), "total_ws_mb": round(sum(r["ws_mb"] for r in rows),1),
             "total_commit_mb": round(sum(r["commit_mb"] for r in rows),1),
             "top_live": live[:10], "top_live_cpu": sorted(live,key=lambda x:x["cpu_pct"],reverse=True)[:10], "rows": rows,
             "sample_ms": round((time.perf_counter() - t0) * 1000, 2)}
    with _APP_STATE_LOCK:
        _APP_STATE.update(state)
    return state


def _app_store_sample(conn, state, dt_sec):
    now = int(time.time())
    local = dt.datetime.fromtimestamp(now)
    date_str, hour_str = local.strftime("%Y-%m-%d"), local.strftime("%Y-%m-%d %H:00")
    rows = state.get("rows") or []
    grouped = {}
    foreground = (state.get("fg_app") or "").lower()
    locked = foreground in ("lockapp.exe", "logonui.exe")
    for row in rows:
        key = row["app_key"]
        g = grouped.setdefault(key, {"display_name": row["display_name"], "category": row["category"],
            "procs": 0, "cpu_pct": 0.0, "ws_mb": 0.0, "commit_mb": 0.0})
        g["procs"] += 1
        g["cpu_pct"] += row["cpu_pct"]
        g["ws_mb"] += row["ws_mb"]
        g["commit_mb"] += row["commit_mb"]
    for key, g in grouped.items():
        is_fg = key == foreground and not locked
        fg_active = dt_sec if is_fg and not state["is_afk"] else 0.0
        fg_idle = dt_sec if is_fg and state["is_afk"] else 0.0
        bg_run = 0.0 if is_fg else dt_sec
        bg_busy = dt_sec if not is_fg and g["cpu_pct"] >= 1.0 else 0.0
        conn.execute("""INSERT INTO app_daily_stats
            (date_str,app_key,display_name,category,fg_active_sec,fg_idle_sec,bg_run_sec,bg_busy_sec,
             cpu_pct_sum,cpu_peak_pct,ws_mb_sum,ws_peak_mb,commit_mb_sum,commit_peak_mb,samples,max_procs,last_ts)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(date_str,app_key) DO UPDATE SET
             display_name=excluded.display_name,category=excluded.category,
             fg_active_sec=fg_active_sec+excluded.fg_active_sec,
             fg_idle_sec=fg_idle_sec+excluded.fg_idle_sec,bg_run_sec=bg_run_sec+excluded.bg_run_sec,
             bg_busy_sec=bg_busy_sec+excluded.bg_busy_sec,cpu_pct_sum=cpu_pct_sum+excluded.cpu_pct_sum,
             cpu_peak_pct=MAX(cpu_peak_pct,excluded.cpu_peak_pct),ws_mb_sum=ws_mb_sum+excluded.ws_mb_sum,
             ws_peak_mb=MAX(ws_peak_mb,excluded.ws_peak_mb),commit_mb_sum=commit_mb_sum+excluded.commit_mb_sum,
             commit_peak_mb=MAX(commit_peak_mb,excluded.commit_peak_mb),samples=samples+1,
             max_procs=MAX(max_procs,excluded.max_procs),last_ts=excluded.last_ts""",
            (date_str,key,g["display_name"],g["category"],fg_active,fg_idle,bg_run,bg_busy,
             g["cpu_pct"],g["cpu_pct"],g["ws_mb"],g["ws_mb"],g["commit_mb"],g["commit_mb"],
             1,g["procs"],now))
    total_cpu = sum(g["cpu_pct"] for g in grouped.values())
    total_ws = sum(g["ws_mb"] for g in grouped.values())
    total_commit = sum(g["commit_mb"] for g in grouped.values())
    fg_sec = dt_sec if foreground and not locked and not state["is_afk"] else 0.0
    afk_sec = dt_sec if state["is_afk"] else 0.0
    conn.execute("""INSERT INTO app_hourly_timeline
        (hour_str,date_str,fg_active_sec,afk_sec,cpu_sum,ws_sum,commit_sum,samples)
        VALUES (?,?,?,?,?,?,?,1) ON CONFLICT(hour_str) DO UPDATE SET
        fg_active_sec=fg_active_sec+excluded.fg_active_sec,afk_sec=afk_sec+excluded.afk_sec,
        cpu_sum=cpu_sum+excluded.cpu_sum,ws_sum=ws_sum+excluded.ws_sum,
        commit_sum=commit_sum+excluded.commit_sum,samples=samples+1""",
        (hour_str,date_str,fg_sec,afk_sec,total_cpu,total_ws,total_commit))


def _app_window_dates(window):
    today = dt.date.today()
    if window == "today":
        return today.isoformat(), today.isoformat()
    if window in ("7d", "30d"):
        days = 7 if window == "7d" else 30
        return (today - dt.timedelta(days=days - 1)).isoformat(), today.isoformat()
    if window == "all":
        return "0000-01-01", today.isoformat()
    raise ValueError("window 仅支持 today、7d、30d、all")


def _app_build_summary(window="today", include_realtime=True):
    if window not in APP_WINDOWS:
        raise ValueError("window 仅支持 today、7d、30d、all")
    start_date, end_date = _app_window_dates(window)
    conn = app_db_connect()
    try:
        records = [dict(r) for r in conn.execute(
            "SELECT app_key,display_name,category,SUM(fg_active_sec) fg_active_sec,"
            "SUM(fg_idle_sec) fg_idle_sec,SUM(bg_run_sec) bg_run_sec,SUM(bg_busy_sec) bg_busy_sec,"
            "SUM(cpu_pct_sum) cpu_pct_sum,MAX(cpu_peak_pct) cpu_peak_pct,"
            "SUM(ws_mb_sum) ws_mb_sum,MAX(ws_peak_mb) ws_peak_mb,"
            "SUM(commit_mb_sum) commit_mb_sum,MAX(commit_peak_mb) commit_peak_mb,"
            "SUM(samples) samples,MAX(max_procs) max_procs,MAX(last_ts) last_ts "
            "FROM app_daily_stats WHERE date_str>=? AND date_str<=? GROUP BY app_key ORDER BY fg_active_sec DESC",
            (start_date,end_date))]
        for r in records:
            samples=max(1,int(r["samples"] or 0))
            r["avg_cpu_pct"]=round((r["cpu_pct_sum"] or 0)/samples,2)
            r["avg_ws_mb"]=round((r["ws_mb_sum"] or 0)/samples,1)
            r["avg_commit_mb"]=round((r["commit_mb_sum"] or 0)/samples,1)
            r["peak_cpu_pct"]=round(r["cpu_peak_pct"] or 0,1)
            r["peak_ws_mb"]=round(r["ws_peak_mb"] or 0,1)
            r["peak_commit_mb"]=round(r["commit_peak_mb"] or 0,1)
            for key in ("fg_active_sec","fg_idle_sec","bg_run_sec","bg_busy_sec"):
                r[key]=round(r[key] or 0,1)
        fg_active=sum(r["fg_active_sec"] for r in records)
        afk_sec=sum(r["fg_idle_sec"] for r in records)
        prod_sec=sum(r["fg_active_sec"] for r in records if r["category"]=="生产力与AI")
        focus=round(fg_active*100/(fg_active+afk_sec),1) if fg_active+afk_sec else 0.0
        productivity=round(prod_sec*100/fg_active,1) if fg_active else 0.0
        category_map={}
        for r in records:
            c=category_map.setdefault(r["category"],{"category":r["category"],"fg_active_sec":0.0,"commit_sum":0.0,"samples":0})
            c["fg_active_sec"]+=r["fg_active_sec"]
            c["commit_sum"]+=(r["commit_mb_sum"] or 0)
            c["samples"]+=(r["samples"] or 0)
        by_category=[]
        for c in category_map.values():
            c["share_pct"]=round(c["fg_active_sec"]*100/fg_active,1) if fg_active else 0.0
            c["avg_commit_mb"]=round(c["commit_sum"]/max(1,c["samples"]),1)
            c["fg_active_sec"]=round(c["fg_active_sec"],1)
            c.pop("commit_sum",None);c.pop("samples",None)
            by_category.append(c)
        by_category.sort(key=lambda x:x["fg_active_sec"],reverse=True)
        if window=="today":
            tr=list(conn.execute("SELECT hour_str bucket,SUM(fg_active_sec) fg_active_sec,SUM(afk_sec) afk_sec,"
                "SUM(cpu_sum) cpu_sum,SUM(ws_sum) ws_sum,SUM(commit_sum) commit_sum,SUM(samples) samples "
                "FROM app_hourly_timeline WHERE date_str=? GROUP BY hour_str ORDER BY hour_str",(end_date,)))
        else:
            tr=list(conn.execute("SELECT date_str bucket,SUM(fg_active_sec) fg_active_sec,SUM(afk_sec) afk_sec,"
                "SUM(cpu_sum) cpu_sum,SUM(ws_sum) ws_sum,SUM(commit_sum) commit_sum,SUM(samples) samples "
                "FROM app_hourly_timeline WHERE date_str>=? AND date_str<=? GROUP BY date_str ORDER BY date_str",
                (start_date,end_date)))
        timeline=[]
        for r in tr:
            n=max(1,int(r["samples"] or 0))
            timeline.append({"bucket":r["bucket"],"fg_active_sec":round(r["fg_active_sec"] or 0,1),
                "afk_sec":round(r["afk_sec"] or 0,1),"avg_cpu_pct":round((r["cpu_sum"] or 0)/n,2),
                "avg_ws_mb":round((r["ws_sum"] or 0)/n,1),"avg_commit_mb":round((r["commit_sum"] or 0)/n,1)})
        today_rows=list(conn.execute("SELECT SUM(fg_active_sec) a,SUM(fg_idle_sec) i FROM app_daily_stats WHERE date_str=?",(dt.date.today().isoformat(),)))
        today_active=float(today_rows[0]["a"] or 0);today_afk=float(today_rows[0]["i"] or 0)
        peak=max(records,key=lambda x:x["peak_commit_mb"],default=None)
        if include_realtime:
            realtime, updated = app_realtime_snapshot()
        else:
            realtime, updated = {}, int(time.time())
        for r in records:
            r.pop("app_key",None);r.pop("cpu_pct_sum",None);r.pop("cpu_peak_pct",None)
            r.pop("ws_mb_sum",None);r.pop("ws_peak_mb",None);r.pop("commit_mb_sum",None);r.pop("commit_peak_mb",None)
            r.pop("last_ts",None)
        return {"device":llm_device_name(),"updated_at":updated,"today_date":dt.date.today().isoformat(),"window":window,
            "realtime":realtime,"kpi":{"fg_active_sec":round(fg_active,1),"today_fg_active_sec":round(today_active,1),
            "afk_sec":round(afk_sec,1),"focus_rate":focus,"productivity_sec":round(prod_sec,1),
            "productivity_rate":productivity,"active_apps_count":sum(1 for r in records if r["fg_active_sec"]>0),
            "peak_commit_app":peak["display_name"] if peak else "--","peak_commit_mb":peak["peak_commit_mb"] if peak else 0,
            "today_afk_sec":round(today_afk,1)},"by_category":by_category,"by_app":records,"timeline":timeline}
    finally:
        conn.close()


def app_realtime_snapshot():
    with _APP_STATE_LOCK:
        live = dict(_APP_STATE)
    realtime = {k:live.get(k) for k in ("is_afk","idle_sec","fg_app","fg_category","fg_title",
        "total_processes","total_ws_mb","total_commit_mb","top_live","top_live_cpu")}
    return realtime, live.get("updated_at") or int(time.time())


def app_refresh_response_cache():
    """采样器线程预生成聚合结果；请求路径仅拼装实时快照和缓存统计。"""
    prepared = {}
    for window in APP_WINDOWS:
        try:
            prepared[window] = _app_build_summary(window, include_realtime=False)
        except Exception as e:
            print("应用摘要预聚合失败 %s: %s" % (window,str(e)[:120]))
    if prepared:
        with _APP_RESP_CACHE_LOCK:
            _APP_RESP_CACHE.update(prepared)


def app_summary(window="today"):
    if window not in APP_WINDOWS:
        raise ValueError("window 仅支持 today、7d、30d、all")
    with _APP_RESP_CACHE_LOCK:
        cached = _APP_RESP_CACHE.get(window)
    if cached is None:
        # 冷启动或数据库尚未初始化时采用一次性回退，后续由 app_poller 预热。
        return _app_build_summary(window, include_realtime=True)
    out = dict(cached)
    out["realtime"], out["updated_at"] = app_realtime_snapshot()
    out["today_date"] = dt.date.today().isoformat()
    return out


def app_poller():

    """独立后台守护采样线程，SQLite 约每 30 秒提交一次。"""
    global _APP_LAST_COMMIT
    try:
        _configure_app_winapi()
        app_db_init()
        conn=app_db_connect()
    except Exception as e:
        print("应用遥测初始化失败: %s" % str(e)[:160])
        return
    last_mono=time.monotonic();_APP_LAST_COMMIT=last_mono
    try:
        # 启动先预热，避免首个 API 请求现场扫描 SQLite。
        conn.commit()
        app_refresh_response_cache()
    except Exception as e:
        print("应用摘要预热失败: %s" % str(e)[:120])
    while True:
        started=time.monotonic()
        try:
            state=app_sample_once()
            now_mono=time.monotonic();dt_sec=max(0.0,min(10.0,now_mono-last_mono));last_mono=now_mono
            _app_store_sample(conn,state,dt_sec)
            if now_mono-_APP_LAST_COMMIT>=APP_COMMIT_INTERVAL:
                conn.commit();_APP_LAST_COMMIT=now_mono
                app_refresh_response_cache()
        except Exception as e:
            try: conn.rollback()
            except Exception: pass
            print("应用遥测采样失败: %s" % str(e)[:120])
        time.sleep(max(0.05,APP_POLL_INTERVAL-(time.monotonic()-started)))


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

        # 全站公共导航组件（Banner）：CSS / JS 各一份，所有页面共享。
        # 走 _MIME 白名单，避免把目录下任意文件暴露成静态资源。
        if path in ("/common-nav.css", "/common-nav.js"):
            ctype = self._MIME.get(os.path.splitext(path)[1].lower())
            if ctype:
                self._serve_file(path.lstrip("/"), ctype)
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

        if path == "/api/llm/summary":
            window = (query.get("window") or ["all"])[0]
            try:
                self._json(llm_summary(window))
            except ValueError as e:
                self._json({"error": "window 仅支持 today、7d、30d、all", "detail": str(e)[:160]}, 400)
            except Exception as e:
                self._json({"error": str(e)[:160]}, 503)
            return

        if path == "/api/app/summary":
            window = (query.get("window") or ["today"])[0]
            try:
                self._json(app_summary(window))
            except ValueError as e:
                self._json({"error": "window 仅支持 today、7d、30d、all", "detail": str(e)[:160]}, 400)
            except Exception as e:
                self._json({"error": str(e)[:160]}, 503)
            return

        if path == "/api/easytier":
            self._json(easytier_status())
            return

        if path in ("/ltx", "/ltx/"):
            # 镜像副本已迁到路由器；本机未配置目录时直接 302 过去
            if not LTX_DIR or not os.path.isdir(LTX_DIR):
                self.send_response(302)
                self.send_header("Location", "http://192.0.2.1:8089/ltx/")
                self.end_headers()
                return
            self._serve_static("index.html")
            return

        if path.startswith("/ltx/"):
            if not LTX_DIR or not os.path.isdir(LTX_DIR):
                self.send_error(404)
                return
            self._serve_static(path[5:])
            return

        if path == "/tools":
            self._serve_file("tools.html", "text/html; charset=utf-8")
            return

        if path in ("/llm", "/llm.html"):
            # 直连本机时的模型监控页（单机视角）；路由器上的同名页面走 /api/llm/aggregate
            self._serve_file("llm.html", "text/html; charset=utf-8")
            return

        if path in ("/app", "/app.html"):
            self._serve_file("app.html", "text/html; charset=utf-8")
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
                    "lhm": lhm_diagnostics(current),
                    "db_backup": current.get("db_backup"),
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

    _MIME = {
        ".html": "text/html; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
    }

    def _serve_static(self, rel):
        """从 LTX_DIR 提供静态文件，带目录穿越防护与图片长缓存。"""
        rel = urllib.parse.unquote(rel or "").replace("\\", "/").lstrip("/")
        base = os.path.realpath(LTX_DIR)
        fp = os.path.realpath(os.path.join(base, rel))
        if not (fp == base or fp.startswith(base + os.sep)) or not os.path.isfile(fp):
            self.send_error(404)
            return
        ext = os.path.splitext(fp)[1].lower()
        ctype = self._MIME.get(ext, "application/octet-stream")
        try:
            with open(fp, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            self.send_header("Cache-Control", "public, max-age=604800")
        else:
            self.send_header("Cache-Control", "no-cache")
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
    # 模型用量索引独立线程：首次全量约 2~3 秒，不能在 HTTP 线程里做，否则首屏会卡
    threading.Thread(target=llm_poller, daemon=True).start()
    # 应用监控独立采样线程：Win32 API 采集不影响原硬件采样与 GPU-Z 流程。
    threading.Thread(target=app_poller, name="app_poller", daemon=True).start()
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
