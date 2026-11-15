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
import hmac
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# LTX 资源库静态目录（容器内 /app/ltx，映射宿主 /mnt/usb/hwmon-hub/ltx）
LTX_DIR = os.environ.get("HWMON_LTX_DIR", os.path.join(BASE_DIR, "ltx"))
LAPTOP = os.environ.get("HWMON_LAPTOP", "http://192.0.2.11:8090").rstrip("/")
DESKTOP = os.environ.get("HWMON_DESKTOP", "http://192.0.2.12:8090").rstrip("/")
WOL_URL = os.environ.get("HWMON_WOL", "http://192.0.2.1:8088/cgi-bin/wol")
DNS_CGI = os.environ.get("HWMON_DNS_CGI", "http://192.0.2.1:8088/cgi-bin/dns")
PEERS_CGI = os.environ.get("HWMON_PEERS_CGI", "http://192.0.2.1:8088/cgi-bin/peers")
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


def _get(url, timeout=None, headers=None):
    h = {"User-Agent": "hwmon-router-hub/1.0"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
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


AMANE_URL = os.environ.get("HWMON_AMANE", "http://192.0.2.11:18000")
PEACH_URL = os.environ.get("HWMON_PEACH", "http://192.0.2.11:8900")
APPS_TTL = float(os.environ.get("HWMON_APPS_TTL", "15"))

# Amane 除 /api/health 外都要 `Authorization: Bearer <token>`。
# token 是笔记本上 %LOCALAPPDATA%\Amane\token 的内容，单独放一个文件，不写进脚本。
AMANE_TOKEN = os.environ.get("HWMON_AMANE_TOKEN", "")
if not AMANE_TOKEN:
    try:
        with open(os.path.join(BASE_DIR, ".amane_token"), encoding="utf-8") as _f:
            AMANE_TOKEN = _f.read().strip()
    except OSError:
        AMANE_TOKEN = ""
_AMANE_AUTH = {"Authorization": "Bearer " + AMANE_TOKEN} if AMANE_TOKEN else None

_apps_state = {"ts": 0.0, "data": {}}
_health_state = {"ts": 0.0, "data": {}}

PEACH_CHECK_CN = {
    "database": "数据库", "schema": "结构", "configured": "配置",
    "media_mounts": "媒体来源", "security": "安全", "ffmpeg": "转码",
}


def _get_json(url, timeout=3.0, headers=None):
    _st, body = _get(url, timeout, headers)
    return json.loads(body.decode("utf-8", "replace"))


def apps_status():
    """Amane / Peach 的健康与库容量。三个请求并行，整体缓存 15 秒。

    Amane 只有 /api/health 免鉴权（其余要 token）；Peach 用 /healthz + /api/stats。
    """
    now = time.time()
    with _lock:
        if now - _apps_state["ts"] < APPS_TTL:
            return dict(_apps_state["data"])

    res = {}

    def one(key, url, headers=None):
        try:
            res[key] = _get_json(url, 4.0, headers)
        except Exception:
            res[key] = None

    jobs = [("amane", AMANE_URL + "/api/health", None),
            ("amane_md", AMANE_URL + "/api/metadata?limit=1", _AMANE_AUTH),
            ("amane_lib", AMANE_URL + "/api/libraries", _AMANE_AUTH),
            ("peach", PEACH_URL + "/healthz", None),
            ("peach_stats", PEACH_URL + "/api/stats", None)]
    ts = [threading.Thread(target=one, args=j) for j in jobs]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    a = res.get("amane")
    amane = {"online": a is not None}
    if a:
        amane["ok"] = a.get("status") == "ok"
        amane["version"] = a.get("version")
    md, lib = res.get("amane_md"), res.get("amane_lib")
    # 带 token 才拿得到作品数和库数；没配 token 就只显示版本
    amane["auth"] = bool(_AMANE_AUTH) and md is not None
    if md:
        amane["items"] = md.get("total")
    if lib:
        amane["libraries"] = len(lib.get("items") or [])

    p, ps = res.get("peach"), res.get("peach_stats")
    peach = {"online": p is not None}
    if p:
        checks = p.get("checks") or {}
        bad = [k for k, v in checks.items() if (v or {}).get("status") == "error"]
        warn = [k for k, v in checks.items() if (v or {}).get("status") == "warning"]
        peach.update({
            "ok": bool(p.get("ok")),
            "version": p.get("version"),
            "bad": len(bad),
            "warn": len(warn),
            "notes": ["%s：%s" % (PEACH_CHECK_CN.get(k, k),
                                  (checks[k] or {}).get("reason", "")) for k in (bad + warn)],
        })
        if ps:
            locs = ps.get("by_loc") or []
            peach["videos"] = sum(int(x.get("videos") or 0) for x in locs)
            peach["bytes"] = sum(int(x.get("bytes") or 0) for x in locs)

    out = {"ts": now, "amane": amane, "peach": peach}
    with _lock:
        _apps_state.update({"ts": now, "data": out})
    return out


def machine_health():
    """两台 PC 的健康结论（来自 agent 的 /health），并行探测，缓存 PROBE_TTL。"""
    now = time.time()
    with _lock:
        if now - _health_state["ts"] < PROBE_TTL:
            return dict(_health_state["data"])

    res = {}

    def one(dev, url):
        try:
            d = _get_json(url + "/health", 2.0)
            h = d.get("health") or {}
            res[dev] = {"online": True, "status": h.get("status"), "label": h.get("label"),
                        "bad": h.get("bad", 0), "warn": h.get("warn", 0),
                        "device": d.get("device")}
        except Exception:
            res[dev] = {"online": False}

    ts = [threading.Thread(target=one, args=("laptop", LAPTOP)),
          threading.Thread(target=one, args=("desktop", DESKTOP))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    out = {"ts": now, "laptop": res.get("laptop", {"online": False}),
           "desktop": res.get("desktop", {"online": False})}
    with _lock:
        _health_state.update({"ts": now, "data": out})
    return out


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
              "target": "DESKTOP-01", "ts": now}
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
# 大模型用量双机聚合（2026-11-12）
# 台式机不常开，所以每台设备的成功响应都要落盘：离线时用缓存托底，
# 前端「双机聚合 / 笔记本 / 台式机」三个视角共用一次请求，切换零延迟。
# =====================================================================
LLM_WINDOWS = ("today", "7d", "30d", "all")
LLM_TIMEOUT = float(os.environ.get("HWMON_LLM_TIMEOUT", "2.5"))
LLM_DEVICES = ("laptop", "desktop")
LLM_LABEL = {"laptop": "笔记本", "desktop": "台式机"}
_llm_cache_state = {"checked": False, "dir": None}


def _llm_cache_probe(path):
    """能建目录、能写文件才算可用：容器里 /app 是只读挂载，必须探测而不是假设。"""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("1")
        os.remove(probe)
        return True
    except Exception:
        return False


def llm_cache_dir():
    """缓存目录优先级：显式配置 > BASE_DIR/cache（推荐，挂到 SSD）> /app/ltx/.llm-cache > /state。"""
    if _llm_cache_state["checked"]:
        return _llm_cache_state["dir"]
    candidates = [
        os.environ.get("HWMON_LLM_CACHE") or os.path.join(BASE_DIR, "cache"),
        os.path.join(LTX_DIR, ".llm-cache"),
        os.path.join(STATE_DIR, "llm-cache"),
    ]
    chosen = None
    for path in candidates:
        if path and _llm_cache_probe(path):
            chosen = path
            break
    _llm_cache_state["dir"] = chosen
    _llm_cache_state["checked"] = True
    if not chosen:
        plog("llm 聚合：找不到可写缓存目录，离线时将无法托底")
    return chosen


def _llm_cache_path(dev, window):
    folder = llm_cache_dir()
    return os.path.join(folder, "cache_llm_%s_%s.json" % (dev, window)) if folder else None


# 聚合只连内网 PC：显式绕过 HTTP_PROXY。开发机上设了全局代理时，urllib 会把
# 私网地址也丢给代理时，离线判定要白等满 2.5 秒才回退，前端也跟着卡住。
_LLM_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _llm_get_json(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "hwmon-router-hub/1.0"})
    with _LLM_OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _llm_blank(dev):
    return {
        "device": dev,
        "online": False,
        "cached_at": None,
        "kpi": {
            "total_calls": 0, "today_calls": 0, "prompt_tokens": 0,
            "cached_tokens": 0, "uncached_tokens": 0, "cache_hit_rate": 0.0,
            "completion_tokens": 0, "reasoning_tokens": 0, "reasoning_ratio": 0.0,
            "avg_step_ms": 0, "avg_tps": None,
        },
        "by_model": [], "timeline": [], "top_sessions": [], "index": {},
    }


def _llm_cached(dev, window):
    """读落盘缓存；返回的 payload 带 online=false 与 cached_at（缓存文件 mtime）。"""
    path = _llm_cache_path(dev, window)
    if not (path and os.path.isfile(path)):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            cached = json.load(f)
        if isinstance(cached, dict) and isinstance(cached.get("kpi"), dict):
            cached["online"] = False
            cached["cached_at"] = int(os.path.getmtime(path))
            return cached
    except Exception:
        pass
    return None


def _today_str():
    """路由器统一按北京时间算「今天」：容器时区通常是 UTC，不能直接 date.today()。"""
    return datetime.now(CN_TZ).strftime("%Y-%m-%d")


def _window_start_date(window):
    """滑动窗口的起始日期（含当天）。today=今天，7d=今天往前 6 天，30d=往前 29 天。"""
    days = {"today": 1, "7d": 7, "30d": 30}.get(window)
    if not days:
        return None
    return (datetime.now(CN_TZ).date() - timedelta(days=days - 1)).isoformat()


def _llm_rebuild_from_all(dev, window):
    """用缓存的 all 窗口「按当前自然日」重算 today / 7d / 30d。

    为什么必须重算：台式关机后，缓存里的 7d/30d 是关机那天算出来的，放着不动就永远
    不衰减；today 更危险——会把关机前当天的 8 亿 Token 当成「今天的实时消耗」。
    这里按 all 的日分桶重新过滤求和，保证滑动窗口随自然日准确衰减。
    """
    allc = _llm_cached(dev, "all")
    if not isinstance(allc, dict) or not allc.get("timeline"):
        return None
    today = _today_str()
    cache_date = allc.get("today_date") or ""
    stale = bool(cache_date) and cache_date != today
    start_date = _window_start_date(window)
    out = {
        "device": dev,
        "online": False,
        "cached_at": allc.get("cached_at"),
        "window": window,
        "today_date": cache_date,
        "stale_today": stale,
        "index": allc.get("index") or {},
        "top_sessions": [],
        "by_model": [],
        "kpi": {},
    }
    if window == "today" and stale:
        # 缓存是过去某天写的：今天的「今日」只能是 0，绝不把旧数据搬进今日实时口径
        out["kpi"] = {
            "total_calls": 0, "today_calls": 0, "prompt_tokens": 0, "cached_tokens": 0,
            "uncached_tokens": 0, "cache_hit_rate": 0.0, "completion_tokens": 0,
            "reasoning_tokens": 0, "reasoning_ratio": 0.0, "avg_step_ms": 0, "avg_tps": None,
        }
        out["timeline"] = []
        return out

    buckets = [b for b in allc["timeline"]
               if not start_date or (b.get("bucket") or "")[:10] >= start_date]
    calls = sum(int(b.get("calls") or 0) for b in buckets)
    prompt = sum(int(b.get("prompt_tokens") or 0) for b in buckets)
    cached = sum(int(b.get("cached_tokens") or 0) for b in buckets)
    completion = sum(int(b.get("completion_tokens") or 0) for b in buckets)
    ak = allc.get("kpi") or {}
    all_prompt = int(ak.get("prompt_tokens") or 0)
    ratio = (prompt / float(all_prompt)) if all_prompt else 0.0
    # 思考 Token 没有按天分桶，只能按输入占比缩放；前端用 approx 标记它是估算
    reasoning = int(round(int(ak.get("reasoning_tokens") or 0) * ratio))
    step = int(ak.get("avg_step_ms") or 0)
    out["kpi"] = {
        "total_calls": calls,
        "today_calls": 0 if stale else int(ak.get("today_calls") or 0),
        "prompt_tokens": prompt,
        "cached_tokens": cached,
        "uncached_tokens": max(0, prompt - cached),
        "cache_hit_rate": round(cached * 100.0 / prompt, 2) if prompt else 0.0,
        "completion_tokens": completion,
        "reasoning_tokens": reasoning,
        "reasoning_ratio": round(reasoning * 100.0 / completion, 2) if completion else 0.0,
        "avg_step_ms": step,
        "avg_tps": round(completion / (step / 1000.0), 1) if step and completion else None,
        "approx": True,
    }
    out["timeline"] = buckets
    models = []
    for m in allc.get("by_model") or []:
        item = {
            "model": m.get("model"),
            "calls": int(round(int(m.get("calls") or 0) * ratio)),
            "prompt_tokens": int(round(int(m.get("prompt_tokens") or 0) * ratio)),
            "cached_tokens": int(round(int(m.get("cached_tokens") or 0) * ratio)),
            "cache_hit_rate": m.get("cache_hit_rate") or 0.0,
            "completion_tokens": int(round(int(m.get("completion_tokens") or 0) * ratio)),
            "reasoning_tokens": int(round(int(m.get("reasoning_tokens") or 0) * ratio)),
            "avg_step_ms": m.get("avg_step_ms") or 0,
            "avg_tps": m.get("avg_tps"),
        }
        if item["calls"] > 0:
            models.append(item)
    out["by_model"] = models
    return out


def _llm_offline_payload(dev, window):
    """离线设备的数据来源：优先用 all 缓存按当前日期重算，其次退回该窗口的旧缓存。"""
    if window != "all":
        rebuilt = _llm_rebuild_from_all(dev, window)
        if rebuilt:
            return rebuilt
    return _llm_cached(dev, window)


def llm_fetch(dev, window):
    """拉一台设备的汇总；成功即写缓存，失败回退缓存并标记 online=false。"""
    base = PC_URLS.get(dev)
    if not base:
        return None
    path = _llm_cache_path(dev, window)
    try:
        data = _llm_get_json("%s/api/llm/summary?window=%s" % (base, window), LLM_TIMEOUT)
        if isinstance(data, dict) and isinstance(data.get("kpi"), dict):
            if path:
                try:
                    tmp = path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False)
                    os.replace(tmp, path)   # 原子替换：半截文件不会顶掉上一份好缓存
                except Exception as e:
                    plog("llm 缓存写入失败 %s/%s: %s" % (dev, window, str(e)[:80]))
            data["online"] = True
            data.pop("cached_at", None)
            if window == "all":
                # 拉全量成功时顺手把日账写进长期库（60 秒节流），PC 关机后也有完整历史
                try:
                    sync_llm_archive(dev, data)
                except Exception as e:
                    plog("同步模型日账失败 %s: %s" % (dev, str(e)[:100]))
            return data
    except Exception as e:
        # 404 = 这台 PC 的 agent 还是旧版（没有 /api/llm/summary），不是离线。
        # 区分开才不会让人以为台式机关机了、跑去开机房。
        if getattr(e, "code", None) == 404:
            fb = _llm_offline_payload(dev, window) or _llm_blank(dev)
            fb["online"] = False
            fb["unsupported"] = True
            return fb
    return _llm_offline_payload(dev, window)


def _llm_merge(nodes):
    """双机合并：累加计数，命中率与思考占比一律重新按 Token 加权，不用两机百分比的均值。"""
    kpi = {"total_calls": 0, "today_calls": 0, "prompt_tokens": 0, "cached_tokens": 0,
           "uncached_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    step_sum = 0.0
    models = {}
    buckets = {}
    sessions = []
    files = 0
    all_calls = 0
    warmup = False
    last_scan = 0
    for dev, data in nodes.items():
        k = data.get("kpi") or {}
        calls = int(k.get("total_calls") or 0)
        for field in kpi:
            kpi[field] += int(k.get(field) or 0)
        step_sum += float(k.get("avg_step_ms") or 0) * calls
        for m in data.get("by_model") or []:
            name = m.get("model") or "unknown"
            cur = models.setdefault(name, {"model": name, "calls": 0, "prompt_tokens": 0,
                                          "cached_tokens": 0, "completion_tokens": 0,
                                          "reasoning_tokens": 0, "step_sum": 0.0})
            mcalls = int(m.get("calls") or 0)
            cur["calls"] += mcalls
            for field in ("prompt_tokens", "cached_tokens", "completion_tokens", "reasoning_tokens"):
                cur[field] += int(m.get(field) or 0)
            cur["step_sum"] += float(m.get("avg_step_ms") or 0) * mcalls
        for t in data.get("timeline") or []:
            bucket = t.get("bucket")
            if bucket is None:
                continue
            cur = buckets.setdefault(bucket, {"bucket": bucket, "calls": 0, "prompt_tokens": 0,
                                             "cached_tokens": 0, "completion_tokens": 0,
                                             "laptop_calls": 0, "desktop_calls": 0})
            cur["calls"] += int(t.get("calls") or 0)
            for field in ("prompt_tokens", "cached_tokens", "completion_tokens"):
                cur[field] += int(t.get(field) or 0)
            cur[dev + "_calls"] = cur.get(dev + "_calls", 0) + int(t.get("calls") or 0)
        for s in data.get("top_sessions") or []:
            if isinstance(s, dict):
                sessions.append(s)
        idx = data.get("index") or {}
        files += int(idx.get("files") or 0)
        all_calls += int(idx.get("all_calls") or 0)
        warmup = warmup or bool(idx.get("warmup"))
        last_scan = max(last_scan, int(idx.get("last_scan") or 0))

    calls = kpi["total_calls"]
    out_kpi = dict(kpi)
    out_kpi["cache_hit_rate"] = round(kpi["cached_tokens"] * 100.0 / kpi["prompt_tokens"], 2) \
        if kpi["prompt_tokens"] else 0.0
    out_kpi["reasoning_ratio"] = round(kpi["reasoning_tokens"] * 100.0 / kpi["completion_tokens"], 2) \
        if kpi["completion_tokens"] else 0.0
    out_kpi["avg_step_ms"] = int(step_sum / calls) if calls else 0
    out_kpi["avg_tps"] = round(kpi["completion_tokens"] / (step_sum / 1000.0), 1) if step_sum else None

    by_model = []
    for cur in models.values():
        mcalls = cur["calls"]
        by_model.append({
            "model": cur["model"],
            "calls": mcalls,
            "prompt_tokens": cur["prompt_tokens"],
            "cached_tokens": cur["cached_tokens"],
            "cache_hit_rate": round(cur["cached_tokens"] * 100.0 / cur["prompt_tokens"], 2)
            if cur["prompt_tokens"] else 0.0,
            "completion_tokens": cur["completion_tokens"],
            "reasoning_tokens": cur["reasoning_tokens"],
            "avg_step_ms": int(cur["step_sum"] / mcalls) if mcalls else 0,
            "avg_tps": round(cur["completion_tokens"] / (cur["step_sum"] / 1000.0), 1)
            if cur["step_sum"] else None,
        })
    by_model.sort(key=lambda m: (-m["calls"], -m["prompt_tokens"]))

    timeline = [buckets[b] for b in sorted(buckets)]
    sessions.sort(key=lambda s: -(s.get("prompt_tokens") or 0))
    return {
        "device": "combined",
        "kpi": out_kpi,
        "by_model": by_model,
        "timeline": timeline,
        "top_sessions": sessions[:10],
        "index": {"files": files, "all_calls": all_calls, "warmup": warmup, "last_scan": last_scan},
    }


LLM_OFFLINE_TTL = 60.0   # 在线状态在这个时间内都信得过：台式关机时不必每次重等 TCP 超时


def _llm_known_online(dev):
    """读 device_status 的缓存值，不主动探测。

    值新鲜且明确是离线，就直接走缓存文件，省掉等 TCP 超时的 2.5 秒；
    值过期或未知（None）时照常去拉，由 LLM_TIMEOUT 兜底。
    """
    with _lock:
        ts = _state.get("ts", 0.0)
        if not ts or time.time() - ts > LLM_OFFLINE_TTL:
            return None
        return bool(_state.get(dev))


def llm_aggregate(window="all"):
    """双机并发拉取（各自 2.5 秒超时），超时立即回退到该机的缓存文件。"""
    if window not in LLM_WINDOWS:
        raise ValueError("window=%s" % window)
    # 顺带在后台刷新在线状态：下一次请求就能走「已知离线」快速路径，不占本次响应时间
    if time.time() - _state.get("ts", 0.0) > PROBE_TTL:
        threading.Thread(target=device_status, daemon=True).start()
    got = {}

    def worker(dev):
        if _llm_known_online(dev) is False:
            got[dev] = _llm_offline_payload(dev, window)   # 已知离线：不请求，直接用缓存重算
            return
        got[dev] = llm_fetch(dev, window)

    threads = [threading.Thread(target=worker, args=(dev,)) for dev in LLM_DEVICES]
    for t in threads:
        t.start()
    for t in threads:
        t.join(LLM_TIMEOUT + 0.8)

    nodes, devices = {}, {}
    for dev in LLM_DEVICES:
        data = got.get(dev)
        if not isinstance(data, dict):
            data = _llm_blank(dev)
        nodes[dev] = data
        idx = data.get("index") or {}
        devices[dev] = {
            "online": bool(data.get("online")),
            "cached_at": data.get("cached_at"),
            "warmup": bool(idx.get("warmup")),
            "stale_today": bool(data.get("stale_today")),
            "today_date": data.get("today_date"),
            "unsupported": bool(data.get("unsupported")),
            "label": LLM_LABEL.get(dev, dev),
        }
    return {
        "device": "router",
        "updated_at": int(time.time()),
        "window": window,
        "combined": _llm_merge(nodes),
        "laptop": nodes["laptop"],
        "desktop": nodes["desktop"],
        "devices": devices,
    }


# =====================================================================
# 应用监控双机聚合与长期缓存
# =====================================================================
APP_WINDOWS = ("today", "7d", "30d", "all")
APP_TIMEOUT = 2.5
APP_DEVICES = ("laptop", "desktop")
APP_CACHE_DIR = os.environ.get("HWMON_APP_CACHE", os.path.join(BASE_DIR, "cache"))


def _app_cache_path(dev, window):
    try:
        os.makedirs(APP_CACHE_DIR, exist_ok=True)
        return os.path.join(APP_CACHE_DIR, "cache_app_%s_%s.json" % (dev, window))
    except Exception:
        return None


def _app_blank(dev):
    return {"device":dev,"online":False,"today_date":None,"updated_at":0,
            "realtime":{"is_afk":True,"idle_sec":0,"fg_app":"","fg_category":"其他应用",
                "fg_title":"","total_processes":0,"total_ws_mb":0,"total_commit_mb":0,"top_live":[],"top_live_cpu":[]},
            "kpi":{"fg_active_sec":0,"today_fg_active_sec":0,"afk_sec":0,"focus_rate":0,
                "productivity_sec":0,"productivity_rate":0,"active_apps_count":0,
                "peak_commit_app":"--","peak_commit_mb":0},"by_category":[],"by_app":[],"timeline":[]}


def _app_cached(dev, window):
    path=_app_cache_path(dev,window)
    if not path or not os.path.isfile(path): return None
    try:
        with open(path,"r",encoding="utf-8") as f: data=json.load(f)
        if not isinstance(data,dict) or not isinstance(data.get("kpi"),dict): return None
        data=_app_normalize_payload(data)
        data["online"]=False;data["cached_at"]=int(os.path.getmtime(path))
        return data
    except Exception: return None


def _app_normalize_payload(data):
    """统一 PC 持久化统计字段为 router merge 的 app-key 契约。"""
    for row in data.get("by_app") or []:
        if not row.get("app"):
            row["app"] = row.get("display_name") or "unknown"
        row.setdefault("samples", 0)
    rt=data.get("realtime") or {}
    rt.setdefault("top_live_cpu", sorted(rt.get("top_live") or [],key=lambda x:float(x.get("cpu_pct") or 0),reverse=True)[:10])
    rt.setdefault("total_ws_mb", sum(float(x.get("ws_mb") or 0) for x in rt.get("top_live") or []))
    rt.setdefault("total_commit_mb", sum(float(x.get("commit_mb") or 0) for x in rt.get("top_live") or []))
    data["realtime"] = rt
    return data


def _app_fetch(dev, window):
    base=PC_URLS[dev];path=_app_cache_path(dev,window)
    try:
        data=_llm_get_json(base+"/api/app/summary?window="+window,APP_TIMEOUT)
        if not isinstance(data,dict) or not isinstance(data.get("kpi"),dict): raise ValueError("invalid app payload")
        if path:
            tmp=path+".tmp"
            with open(tmp,"w",encoding="utf-8") as f: json.dump(data,f,ensure_ascii=False)
            os.replace(tmp,path)
        data=_app_normalize_payload(data)
        data["online"]=True;data.pop("cached_at",None)
        return data
    except Exception:
        return _app_cached(dev,window) or _app_blank(dev)


def _app_zero_today(data, today):
    """离线 today cache 已过期：将该设备今日相关指标置 0，禁止跨天污染。"""
    out=json.loads(json.dumps(data,ensure_ascii=False))
    out["stale_today"]=True
    out["kpi"].update({"fg_active_sec":0,"today_fg_active_sec":0,"afk_sec":0,
            "focus_rate":0,"productivity_sec":0,"productivity_rate":0,"active_apps_count":0,
        "peak_commit_app":"--","peak_commit_mb":0})
    if isinstance(out.get("realtime"),dict):
        out["realtime"].update({"total_processes":0,"total_ws_mb":0,"total_commit_mb":0,"top_live":[]})
    out["by_category"]=[];out["by_app"]=[];out["timeline"]=[]
    out["today_date"]=today
    return out


def _app_merge(nodes):
    combined=_app_blank("combined");k=combined["kpi"];apps={};cats={};buckets={};live={}
    weighted_cpu={};weighted_ws={};weighted_commit={}
    for dev,data in nodes.items():
        src=data.get("kpi") or {}
        for key in ("fg_active_sec","today_fg_active_sec","afk_sec","productivity_sec"):
            k[key]+=float(src.get(key) or 0)
        combined["realtime"]["total_processes"]+=int((data.get("realtime") or {}).get("total_processes") or 0)
        combined["realtime"]["total_ws_mb"]+=float((data.get("realtime") or {}).get("total_ws_mb") or 0)
        combined["realtime"]["total_commit_mb"]+=float((data.get("realtime") or {}).get("total_commit_mb") or 0)
        cpu_top=(data.get("realtime") or {}).get("top_live_cpu") or (data.get("realtime") or {}).get("top_live") or []
        for item in ((data.get("realtime") or {}).get("top_live") or []):
            key=(item.get("app") or "unknown").lower()
            cur=live.setdefault(key,{"app":item.get("app") or key,"category":item.get("category") or "其他应用",
                "procs":0,"cpu_pct":0.0,"ws_mb":0.0,"commit_mb":0.0,"device_split":{"laptop":{},"desktop":{}}})
            cur["procs"]+=int(item.get("procs") or 0)
            cur["cpu_pct"]+=float(item.get("cpu_pct") or 0)
            cur["ws_mb"]+=float(item.get("ws_mb") or 0)
            cur["commit_mb"]+=float(item.get("commit_mb") or 0)
            cur["device_split"][dev]={"procs":int(item.get("procs") or 0),"cpu_pct":float(item.get("cpu_pct") or 0),
                "ws_mb":float(item.get("ws_mb") or 0),"commit_mb":float(item.get("commit_mb") or 0)}
        for item in cpu_top:
            key=(item.get("app") or "unknown").lower()
            cur=live.setdefault(key,{"app":item.get("app") or key,"category":item.get("category") or "其他应用",
                "procs":0,"cpu_pct":0.0,"ws_mb":0.0,"commit_mb":0.0,"device_split":{"laptop":{},"desktop":{}}})
            cur["cpu_pct"]=max(cur["cpu_pct"],float(item.get("cpu_pct") or 0))
        for row in data.get("by_app") or []:
            # agent 返回的是 display_name；旧版/缓存可能是 app。两种都兼容，
            # 否则 key 全落到 "unknown"，前端表格会显示一整列 None。
            row_name = row.get("app") or row.get("display_name") or "unknown"
            key = row_name.lower()
            a=apps.setdefault(key,{"app":row_name,"category":row.get("category") or "其他应用",
                "fg_active_sec":0,"fg_idle_sec":0,"bg_run_sec":0,"bg_busy_sec":0,"max_procs":0,
                "peak_cpu_pct":0,"peak_ws_mb":0,"peak_commit_mb":0,"device_split":{"laptop":0,"desktop":0},
                "_cpu":0,"_ws":0,"_commit":0,"_weight":0})
            for field in ("fg_active_sec","fg_idle_sec","bg_run_sec","bg_busy_sec"):
                a[field]+=float(row.get(field) or 0)
            weight=max(0,int(row.get("samples") or 0))
            a["_weight"]+=weight
            a["_cpu"]+=float(row.get("avg_cpu_pct") or 0)*weight
            a["_ws"]+=float(row.get("avg_ws_mb") or 0)*weight
            a["_commit"]+=float(row.get("avg_commit_mb") or 0)*weight
            a["peak_cpu_pct"]=max(a["peak_cpu_pct"],float(row.get("peak_cpu_pct") or 0))
            a["peak_ws_mb"]=max(a["peak_ws_mb"],float(row.get("peak_ws_mb") or 0))
            a["peak_commit_mb"]=max(a["peak_commit_mb"],float(row.get("peak_commit_mb") or 0))
            a["max_procs"]=max(a["max_procs"],int(row.get("max_procs") or 0))
            a["device_split"][dev]+=float(row.get("fg_active_sec") or 0)
        for row in data.get("by_category") or []:
            c=cats.setdefault(row.get("category") or "其他应用",{"category":row.get("category") or "其他应用","fg_active_sec":0})
            c["fg_active_sec"]+=float(row.get("fg_active_sec") or 0)
        for row in data.get("timeline") or []:
            b=row.get("bucket")
            if not b:continue
            cur=buckets.setdefault(b,{"bucket":b,"fg_active_sec":0,"afk_sec":0,"laptop_active_sec":0,"desktop_active_sec":0,
                "_cpu":0,"_ws":0,"_commit":0,"_n":0})
            cur["fg_active_sec"]+=float(row.get("fg_active_sec") or 0);cur["afk_sec"]+=float(row.get("afk_sec") or 0)
            cur[dev+"_active_sec"]+=float(row.get("fg_active_sec") or 0)
            n=max(1,int(row.get("samples") or 1));cur["_cpu"]+=float(row.get("avg_cpu_pct") or 0)*n
            cur["_ws"]+=float(row.get("avg_ws_mb") or 0)*n;cur["_commit"]+=float(row.get("avg_commit_mb") or 0)*n;cur["_n"]+=n
    total=k["fg_active_sec"];afk=k["afk_sec"]
    k["focus_rate"]=round(total*100/(total+afk),1) if total+afk else 0
    k["productivity_rate"]=round(k["productivity_sec"]*100/total,1) if total else 0
    k["active_apps_count"]=sum(1 for a in apps.values() if a["fg_active_sec"]>0)
    for a in apps.values():
        w=max(1,a.pop("_weight"));a["avg_cpu_pct"]=round(a.pop("_cpu")/w,2)
        a["avg_ws_mb"]=round(a.pop("_ws")/w,1);a["avg_commit_mb"]=round(a.pop("_commit")/w,1)
        a["device_split"]={d:round(v,1) for d,v in a["device_split"].items()}
    combined["by_app"]=sorted(apps.values(),key=lambda x:(-x["fg_active_sec"],-x["bg_busy_sec"]))
    for c in cats.values(): c["share_pct"]=round(c["fg_active_sec"]*100/total,1) if total else 0
    combined["by_category"]=sorted(cats.values(),key=lambda x:-x["fg_active_sec"])
    for b in buckets.values():
        n=max(1,b.pop("_n"));b["avg_cpu_pct"]=round(b.pop("_cpu")/n,2);b["avg_ws_mb"]=round(b.pop("_ws")/n,1);b["avg_commit_mb"]=round(b.pop("_commit")/n,1)
    combined["timeline"]=[buckets[key] for key in sorted(buckets)]
    combined["kpi"]=k
    combined["realtime"]["fg_app"]="双机实时状态见设备卡片"
    combined["realtime"]["top_live"]=sorted(live.values(),key=lambda x:(x["commit_mb"],x["ws_mb"],x["cpu_pct"]),reverse=True)[:10]
    combined["realtime"]["top_live_cpu"]=sorted(live.values(),key=lambda x:x["cpu_pct"],reverse=True)[:10]
    peaks=[a for a in combined["by_app"] if a.get("peak_commit_mb") is not None]
    peak=max(peaks,key=lambda x:x["peak_commit_mb"],default=None)
    k["peak_commit_app"]=peak["app"] if peak else "--"
    k["peak_commit_mb"]=round(peak["peak_commit_mb"],1) if peak else 0
    return combined


def app_aggregate(window="today"):
    if window not in APP_WINDOWS: raise ValueError("window 仅支持 today、7d、30d、all")
    got={}
    def worker(dev): got[dev]=_app_fetch(dev,window)
    threads=[threading.Thread(target=worker,args=(d,),daemon=True) for d in APP_DEVICES]
    for t in threads:t.start()
    for t in threads:t.join(APP_TIMEOUT+0.8)
    today=_today_str();nodes={};devices={}
    for dev in APP_DEVICES:
        data=got.get(dev) or _app_cached(dev,window) or _app_blank(dev)
        cached=not data.get("online",False);stale=window=="today" and cached and data.get("today_date")!=today
        if stale:data=_app_zero_today(data,today)
        nodes[dev]=data
        devices[dev]={"online":bool(data.get("online")),"cached_at":data.get("cached_at"),
            "today_date":data.get("today_date"),"stale_today":bool(stale),"label":LLM_LABEL[dev],
            "is_afk":(data.get("realtime") or {}).get("is_afk"),"fg_app":(data.get("realtime") or {}).get("fg_app"),
            "fg_category":(data.get("realtime") or {}).get("fg_category"),"fg_title":(data.get("realtime") or {}).get("fg_title"),
            "idle_sec":(data.get("realtime") or {}).get("idle_sec",0),
            "total_processes":(data.get("realtime") or {}).get("total_processes",0),
            "top_live":(data.get("realtime") or {}).get("top_live",[]),
            "top_live_cpu":(data.get("realtime") or {}).get("top_live_cpu",[])}
    return {"device":"router","updated_at":int(time.time()),"window":window,"combined":_app_merge(nodes),
            "laptop":nodes["laptop"],"desktop":nodes["desktop"],"devices":devices}


# =====================================================================
# 长期数据归档（2026-11-13）
# 背景：PC 上的 hwmon.db 只保留 7 天，台式机还常年关机。路由器把三类数据
# 永久落到 /app/data/hub_longterm.db（宿主 /mnt/usb/hwmon-hub/data/）：
#   hw_samples_1m      分钟级硬件采样，保留 90 天，用来画 30/90 天长曲线
#   hw_daily_archive   日级硬件汇总（PC /api/summary 的原始 JSON），永久
#   llm_daily_archive  双机模型日账（按天），永久
#   llm_model_archive  双机模型总量快照，永久
# 这样即使两台 PC 全关，路由器也能回放历史曲线与每日小结。
# =====================================================================
HW_SAMPLE_EVERY = float(os.environ.get("HWMON_HW_SAMPLE_EVERY", "60"))
HW_SAMPLE_KEEP_DAYS = int(os.environ.get("HWMON_HW_KEEP_DAYS", "90"))
HW_DAILY_EVERY = float(os.environ.get("HWMON_HW_DAILY_EVERY", "600"))
LT_DEVICES = {"laptop", "desktop"}
HW_SAMPLE_FIELDS = (
    ("cpu_temp", ("cpu", "temp")),
    ("cpu_core_max", ("cpu", "core_max")),
    ("cpu_power", ("cpu", "power")),
    ("cpu_load", ("cpu", "load")),
    # cpu_volt 2026-11-13 补入：前端「CPU 电压」行内 Sparkline 读 h.cpu_volt，
    # 之前归档表没有这一列，导致 7 天 / 30 天视角下该曲线恒显示「等待历史采样」。
    ("cpu_volt", ("cpu", "volt")),
    ("gpu_core", ("gpu", "core")),
    ("gpu_hot", ("gpu", "hot")),
    ("gpu_mem", ("gpu", "mem")),
    ("gpu_power", ("gpu", "power")),
    ("gpu_load", ("gpu", "load")),
    ("pin_volt", ("gpu", "pin_volt")),
    ("mem_pct", ("mem", "pct")),
)
_lt = {"conn": None, "lock": threading.Lock(), "dir": None, "checked": False,
       "last_llm_sync": {}, "errors": 0}


def lt_dir():
    """长期库目录：优先 /app/data（run-on-router.sh 挂载），逐级退到可写位置。"""
    if _lt["checked"]:
        return _lt["dir"]
    candidates = [
        os.environ.get("HWMON_DATA_DIR") or os.path.join(BASE_DIR, "data"),
        os.path.join(LTX_DIR, ".data"),
        os.path.join(STATE_DIR, "data"),
    ]
    for path in candidates:
        if path and _llm_cache_probe(path):
            _lt["dir"] = path
            break
    _lt["checked"] = True
    if not _lt["dir"]:
        plog("长期归档：找不到可写数据目录，历史数据将不会落盘")
    return _lt["dir"]


def lt_db_path():
    folder = lt_dir()
    return os.path.join(folder, "hub_longterm.db") if folder else None


def lt_conn():
    """单连接 + 锁：归档线程与 HTTP 查询共用，省得每次开库重读 WAL。"""
    if _lt["conn"] is not None:
        return _lt["conn"]
    path = lt_db_path()
    if not path:
        raise RuntimeError("no writable data dir")
    conn = sqlite3.connect(path, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hw_samples_1m (
            device TEXT NOT NULL, ts INTEGER NOT NULL,
            cpu_temp REAL, cpu_core_max REAL, cpu_power REAL, cpu_load REAL,
            cpu_volt REAL,
            gpu_core REAL, gpu_hot REAL, gpu_mem REAL, gpu_power REAL, gpu_load REAL,
            pin_volt REAL, mem_pct REAL,
            PRIMARY KEY (device, ts)
        )
    """)
    # 老库补列：CREATE TABLE IF NOT EXISTS 不会给已有表加字段，必须显式 ALTER。
    # 用 try/except 兜住「列已存在」，重复启动不报错。
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(hw_samples_1m)")}
        if "cpu_volt" not in cols:
            conn.execute("ALTER TABLE hw_samples_1m ADD COLUMN cpu_volt REAL")
            conn.commit()
            print("[archive] hw_samples_1m 已补列 cpu_volt", flush=True)
    except Exception as e:
        print("[archive] 补列 cpu_volt 失败: %s" % str(e)[:120], flush=True)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hw_day ON hw_samples_1m(device, ts)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hw_daily_archive (
            device TEXT NOT NULL, date_str TEXT NOT NULL,
            json_summary TEXT NOT NULL, updated_at INTEGER NOT NULL,
            PRIMARY KEY (device, date_str)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS llm_daily_archive (
            device TEXT NOT NULL, date_str TEXT NOT NULL,
            calls INTEGER NOT NULL DEFAULT 0, prompt_tokens INTEGER NOT NULL DEFAULT 0,
            cached_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
            reasoning_tokens INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL,
            PRIMARY KEY (device, date_str)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS llm_model_archive (
            device TEXT NOT NULL, model TEXT NOT NULL,
            calls INTEGER NOT NULL DEFAULT 0, prompt_tokens INTEGER NOT NULL DEFAULT 0,
            cached_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
            reasoning_tokens INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL,
            PRIMARY KEY (device, model)
        )
    """)
    conn.execute("""CREATE TABLE IF NOT EXISTS app_daily_archive (
            device TEXT NOT NULL, date_str TEXT NOT NULL,
            json_payload TEXT NOT NULL, updated_at INTEGER NOT NULL,
            PRIMARY KEY (device, date_str)
        )""")
    conn.commit()
    _lt["conn"] = conn
    return conn


def _snap_value(snap, path):
    cur = snap
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur if isinstance(cur, (int, float)) else None


def hw_sample_once():
    """拉一轮在线设备的快照并写 hw_samples_1m。离线设备直接跳过，不产生空行。"""
    written = 0
    conn = lt_conn()
    now = int(time.time())
    for dev in ("laptop", "desktop"):
        if _llm_known_online(dev) is False:
            continue
        try:
            snap = _llm_get_json(PC_URLS[dev] + "/api/snapshot", 2.5)
        except Exception:
            continue
        if not isinstance(snap, dict) or not snap.get("ts"):
            continue
        ts = int(snap["ts"] / 1000)
        if ts <= 0:
            ts = now
        values = [_snap_value(snap, path) for _name, path in HW_SAMPLE_FIELDS]
        with _lt["lock"]:
            conn.execute(
                "INSERT OR REPLACE INTO hw_samples_1m (device, ts, %s) VALUES (%s)"
                % (",".join(name for name, _p in HW_SAMPLE_FIELDS),
                   ",".join("?" * (len(HW_SAMPLE_FIELDS) + 2))),
                tuple([dev, ts] + values))
            conn.commit()
        written += 1
    return written


def hw_daily_once(force=False):
    """把 PC 的 /api/summary?window=today 原样存进 hw_daily_archive（当天一行，可反复覆盖）。"""
    today = _today_str()
    saved = 0
    for dev in ("laptop", "desktop"):
        if _llm_known_online(dev) is False:
            continue
        try:
            summary = _llm_get_json(PC_URLS[dev] + "/api/summary?window=today", 3.0)
        except Exception:
            continue
        if not isinstance(summary, dict) or not summary.get("label"):
            continue
        conn = lt_conn()
        with _lt["lock"]:
            conn.execute(
                "INSERT OR REPLACE INTO hw_daily_archive (device, date_str, json_summary, updated_at)"
                " VALUES (?,?,?,?)",
                (dev, today, json.dumps(summary, ensure_ascii=False), int(time.time())))
            conn.commit()
        saved += 1
    return saved


def sync_llm_archive(dev, payload):
    """把一次成功的 all 窗口拉取同步进长期库（日账 + 模型账）。60 秒内只写一次。"""
    if not isinstance(payload, dict) or not payload.get("timeline"):
        return 0
    if dev not in _lt["last_llm_sync"]:
        _lt["last_llm_sync"][dev] = 0.0
    if time.time() - _lt["last_llm_sync"][dev] < 60:
        return 0
    try:
        conn = lt_conn()
    except Exception:
        return 0
    now = int(time.time())
    rows = []
    for b in payload.get("timeline") or []:
        bucket = b.get("bucket") or ""
        if len(bucket) < 10:
            continue
        rows.append((dev, bucket[:10], int(b.get("calls") or 0),
                     int(b.get("prompt_tokens") or 0), int(b.get("cached_tokens") or 0),
                     int(b.get("completion_tokens") or 0), 0, now))
    models = [(dev, m.get("model") or "unknown", int(m.get("calls") or 0),
               int(m.get("prompt_tokens") or 0), int(m.get("cached_tokens") or 0),
               int(m.get("completion_tokens") or 0), int(m.get("reasoning_tokens") or 0), now)
              for m in payload.get("by_model") or []]
    try:
        with _lt["lock"]:
            if rows:
                conn.executemany(
                    "INSERT INTO llm_daily_archive (device, date_str, calls, prompt_tokens,"
                    " cached_tokens, completion_tokens, reasoning_tokens, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(device, date_str) DO UPDATE SET"
                    " calls=excluded.calls, prompt_tokens=excluded.prompt_tokens,"
                    " cached_tokens=excluded.cached_tokens,"
                    " completion_tokens=excluded.completion_tokens, updated_at=excluded.updated_at",
                    rows)
            if models:
                conn.executemany(
                    "INSERT INTO llm_model_archive (device, model, calls, prompt_tokens,"
                    " cached_tokens, completion_tokens, reasoning_tokens, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(device, model) DO UPDATE SET"
                    " calls=excluded.calls, prompt_tokens=excluded.prompt_tokens,"
                    " cached_tokens=excluded.cached_tokens,"
                    " completion_tokens=excluded.completion_tokens,"
                    " reasoning_tokens=excluded.reasoning_tokens, updated_at=excluded.updated_at",
                    models)
            conn.commit()
        _lt["last_llm_sync"][dev] = time.time()
    except Exception as e:
        plog("长期库写模型日账失败 %s: %s" % (dev, str(e)[:100]))
        return 0
    return len(rows)


def lt_purge():
    """hw_samples_1m 只留 90 天；日级归档永久保留。"""
    try:
        conn = lt_conn()
        cutoff = int(time.time()) - HW_SAMPLE_KEEP_DAYS * 86400
        with _lt["lock"]:
            cur = conn.execute("DELETE FROM hw_samples_1m WHERE ts < ?", (cutoff,))
            conn.commit()
        return cur.rowcount or 0
    except Exception:
        return 0


def longterm_history(device, window):
    """按天聚合的硬件长曲线，结构与 PC 的 /api/history 一致，前端图表代码可直接复用。"""
    if device not in LT_DEVICES:
        raise ValueError("device 仅支持 laptop / desktop")
    days = {"7d": 7, "30d": 30, "90d": 90}.get(window)
    now = int(time.time())
    if days:
        # 对齐到本地 0 点：date 对象没有 timestamp()，必须用 datetime
        midnight = datetime.now(CN_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        start = int((midnight - timedelta(days=days - 1)).timestamp())
    else:
        start = 0
    conn = lt_conn()
    with _lt["lock"]:
        rows = conn.execute(
            "SELECT (ts + 28800) / 86400 AS d, COUNT(*) n,"
            " AVG(cpu_temp) cpu_temp, MAX(cpu_temp) cpu_temp_max,"
            " AVG(cpu_core_max) cpu_core_max,"
            " AVG(cpu_power) cpu_power, AVG(cpu_load) cpu_load,"
            " AVG(cpu_volt) cpu_volt,"
            " AVG(gpu_hot) gpu_hot, MAX(gpu_hot) gpu_hot_max,"
            " AVG(gpu_core) gpu_core, AVG(gpu_power) gpu_power,"
            " AVG(gpu_load) gpu_load, AVG(pin_volt) pin_volt, AVG(mem_pct) mem_pct"
            " FROM hw_samples_1m WHERE device=? AND ts>=? GROUP BY d ORDER BY d",
            (device, start)).fetchall()
    data = {"t": [], "cpu_temp": [], "cpu_core_max": [], "cpu_power": [], "cpu_load": [],
            "cpu_volt": [],
            "gpu_hot": [], "gpu_core": [], "gpu_power": [], "gpu_load": [],
            "pin_volt": [], "mem_pct": [], "samples": []}
    for row in rows:
        # day 序号还原为当地时间 0 点的毫秒时间戳
        day_start = int(row["d"]) * 86400 - 28800
        data["t"].append(day_start * 1000)
        for name in ("cpu_temp", "cpu_core_max", "cpu_power", "cpu_load", "cpu_volt",
                     "gpu_hot", "gpu_core", "gpu_power", "gpu_load", "pin_volt", "mem_pct"):
            val = row[name]
            data[name].append(round(val, 2) if isinstance(val, (int, float)) else None)
        data["samples"].append(row["n"])
    data["_meta"] = {
        "window": window, "device": device, "bucket": "day", "source": "hub_longterm",
        "start": (start * 1000) if start else (data["t"][0] if data["t"] else 0),
        "end": now * 1000, "bucket_ms": 86400000,
        "note": "路由器长期库按天平均，来源为各设备在线期间的每分钟采样",
    }
    return data


def daily_summary_cache(device):
    """离线时回放「今日小结」：优先当天，其次最近一天。"""
    if device not in LT_DEVICES:
        raise ValueError("device 仅支持 laptop / desktop")
    conn = lt_conn()
    with _lt["lock"]:
        row = conn.execute(
            "SELECT date_str, json_summary, updated_at FROM hw_daily_archive"
            " WHERE device=? ORDER BY date_str DESC LIMIT 1", (device,)).fetchone()
    if not row:
        return {"ok": False, "device": device, "error": "no archive"}
    try:
        summary = json.loads(row["json_summary"])
    except Exception:
        return {"ok": False, "device": device, "error": "bad archive"}
    summary["ok"] = True
    summary["_cached"] = True
    summary["_cached_date"] = row["date_str"]
    summary["_cached_at"] = row["updated_at"]
    return summary


def app_daily_archive_once():
    """应用日统计快照按设备/日期幂等归档，永久保留。"""
    saved=0;today=_today_str();conn=lt_conn()
    for dev in APP_DEVICES:
        try:
            payload=_app_fetch(dev,"today")
            if not payload.get("online") and payload.get("today_date")!=today:
                continue
            with _lt["lock"]:
                conn.execute("INSERT INTO app_daily_archive(device,date_str,json_payload,updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(device,date_str) DO UPDATE SET json_payload=excluded.json_payload,updated_at=excluded.updated_at",
                    (dev,today,json.dumps(payload,ensure_ascii=False),int(time.time())))
                conn.commit()
            saved+=1
        except Exception as e: plog("应用统计归档失败 %s: %s"%(dev,str(e)[:100]))
    return saved


def hub_archiver_poller():
    """常驻归档线程：硬件分钟采样、日汇总与应用日账永久归档。"""
    try:
        lt_conn()
    except Exception as e:
        plog("长期库初始化失败：%s" % str(e)[:120])
        return
    last_daily = 0.0
    last_app_archive = 0.0
    last_purge = 0.0
    while True:
        try:
            hw_sample_once()
        except Exception as e:
            _lt["errors"] += 1
            plog("分钟采样失败：%s" % str(e)[:120])
        now = time.time()
        if now - last_daily > HW_DAILY_EVERY:
            try:
                hw_daily_once()
            except Exception as e:
                plog("日汇总失败：%s" % str(e)[:120])
            last_daily = now
        if now - last_app_archive > 600:
            try:
                app_daily_archive_once()
            except Exception as e:
                plog("应用日账归档失败：%s" % str(e)[:120])
            last_app_archive = now
        if now - last_purge > 3600:
            try:
                lt_purge()
            except Exception:
                pass
            last_purge = now
        time.sleep(HW_SAMPLE_EVERY)


def lt_status():
    """归档健康度，给 /api/hw/longterm 的 meta 与排障用。"""
    out = {"ok": True, "dir": lt_dir(), "db": lt_db_path(),
           "sample_every": HW_SAMPLE_EVERY, "keep_days": HW_SAMPLE_KEEP_DAYS,
           "errors": _lt["errors"], "counts": {}, "latest": {}}
    try:
        conn = lt_conn()
        with _lt["lock"]:
            out["counts"]["hw_samples_1m"] = conn.execute(
                "SELECT COUNT(*) FROM hw_samples_1m").fetchone()[0]
            out["counts"]["hw_daily_archive"] = conn.execute(
                "SELECT COUNT(*) FROM hw_daily_archive").fetchone()[0]
            out["counts"]["llm_daily_archive"] = conn.execute(
                "SELECT COUNT(*) FROM llm_daily_archive").fetchone()[0]
            out["counts"]["llm_model_archive"] = conn.execute(
                "SELECT COUNT(*) FROM llm_model_archive").fetchone()[0]
            out["counts"]["app_daily_archive"] = conn.execute(
                "SELECT COUNT(*) FROM app_daily_archive").fetchone()[0]
            for dev in ("laptop", "desktop"):
                row = conn.execute(
                    "SELECT MAX(ts) a, COUNT(*) n FROM hw_samples_1m WHERE device=?",
                    (dev,)).fetchone()
                out["latest"][dev] = {"last_ts": row["a"], "samples": row["n"]}
    except Exception as e:
        out["ok"] = False
        out["error"] = str(e)[:160]
    return out


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

    def _read_json(self, limit=64 * 1024 * 1024):
        """读请求体当 JSON。超限或解析失败都返回 None，不抛。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return None
        if n <= 0 or n > limit:
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8", "replace"))
        except Exception:
            return None

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

    def _serve_ltx(self, rel):
        """从 LTX_DIR 提供静态文件，带目录穿越防护与图片长缓存。

        白名单：只放行 index.html 与 covers/ 下的图片，避免把 data/ 原始
        JSON 和抓取脚本暴露在局域网上。
        """
        rel = urllib.parse.unquote(rel or "").replace("\\", "/").lstrip("/")
        base = os.path.realpath(LTX_DIR)
        fp = os.path.realpath(os.path.join(base, rel))
        if not (fp == base or fp.startswith(base + os.sep)) or not os.path.isfile(fp):
            self.send_error(404)
            return
        ext = os.path.splitext(fp)[1].lower()
        allowed = rel in ("", "index.html") or (
            rel.startswith("covers/")
            and ext in (".jpg", ".jpeg", ".png", ".webp", ".gif")
        )
        if not allowed:
            self.send_error(404)
            return
        ext = os.path.splitext(fp)[1].lower()
        ctype = self._MIME.get(ext, "application/octet-stream")
        # 页面本身 2.8MB 且内嵌全部数据，用 mtime+size 做 ETag，刷新走 304 而不是整份重传
        try:
            st = os.stat(fp)
            etag = '"%x-%x"' % (int(st.st_mtime), st.st_size)
        except OSError:
            self.send_error(404)
            return
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self._cors()
            self.end_headers()
            return
        try:
            with open(fp, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        if ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            self.send_header("Cache-Control", "public, max-age=604800")
        else:
            self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # 浏览器刷新/关标签时会中断，不算错误
            pass

    def _serve_backup(self, name):
        """下载 _backup/ 下的备份包。流式 + Range，别把几百 MB 读进内存，
        也不怕中途被同步任务挤断（断了 curl 可以接着传）。"""
        name = urllib.parse.unquote(name or "").replace("\\", "/").lstrip("/")
        if "/" in name or not LTX_BACKUP_RE.match(name):
            self.send_error(404)
            return
        fp = os.path.join(LTX_BACKUP_DIR, name)
        if not os.path.isfile(fp):
            self.send_error(404)
            return
        try:
            size = os.path.getsize(fp)
            f = open(fp, "rb")
        except OSError:
            self.send_error(404)
            return

        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)$", (self.headers.get("Range") or "").strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            else:
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.end_headers()
                f.close()
                return
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        else:
            self.send_response(200)

        left = end - start + 1
        self.send_header("Content-Type", "application/x-tar")
        self.send_header("Content-Length", str(left))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()
        try:
            if start:
                f.seek(start)
            while left > 0:
                chunk = f.read(min(256 * 1024, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            f.close()

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
        # 全站公共导航组件（Banner）：CSS / JS 各一份，五个页面共享。
        # 走 _MIME 白名单，只放行这两个固定文件名。
        if path in ("/common-nav.css", "/common-nav.js"):
            ctype = self._MIME.get(os.path.splitext(path)[1].lower())
            if ctype:
                return self._serve(os.path.basename(path), ctype)
        if path.startswith("/hw/"):
            return self._hw()

        if path == "/api/ltx":
            return self._json(ltx_status())
        if path == "/api/ltx/changes":
            return self._json(ltx_changes())
        if path == "/api/ltx/115":
            return self._json(ltx_115_status())
        if path == "/api/ltx/backup":
            return self._json(ltx_backup_status())
        if path.startswith("/ltxbackup/"):
            return self._serve_backup(path[11:])
        if path == "/ltx":
            # 页面里的封面是相对路径 covers/xxx.jpg，必须带斜杠才能正确解析
            self.send_response(302)
            self.send_header("Location", "/ltx/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/ltx/":
            return self._serve_ltx("index.html")
        if path.startswith("/ltx/"):
            return self._serve_ltx(path[5:])
        if path in ("/", "/index.html", "/monitor", "/monitor.html"):
            return self._serve("monitor.html", "text/html; charset=utf-8")
        if path in ("/llm", "/llm.html"):
            return self._serve("llm.html", "text/html; charset=utf-8")
        if path in ("/app", "/app.html"):
            return self._serve("app.html", "text/html; charset=utf-8")
        if path in ("/tools", "/tools.html"):
            return self._serve("index.html", "text/html; charset=utf-8")
        if path == "/api/app/aggregate":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            window = (q.get("window") or ["today"])[0]
            try:
                return self._json(app_aggregate(window))
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)[:160]}, 400)
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:160]}, 503)
        if path == "/api/llm/aggregate":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            window = (q.get("window") or ["all"])[0]
            try:
                return self._json(llm_aggregate(window))
            except ValueError:
                return self._json({"ok": False, "error": "window 仅支持 today、7d、30d、all",
                                   "window": window}, 400)
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:160]}, 503)
        if path == "/api/hw/longterm":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            device = (q.get("device") or ["laptop"])[0]
            window = (q.get("window") or ["7d"])[0]
            try:
                data = longterm_history(device, window)
                data["_meta"]["archive"] = {k: v for k, v in lt_status().items() if k != "latest"}
                return self._json(data)
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)[:160]}, 400)
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:160]}, 503)
        if path == "/api/hw/daily_cache":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            device = (q.get("device") or ["laptop"])[0]
            try:
                return self._json(daily_summary_cache(device))
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)[:160]}, 400)
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:160]}, 503)
        if path == "/api/hw/longterm/status":
            return self._json(lt_status())
        if path == "/api/status":
            st = device_status()
            return self._json({"ok": True, **st,
                               "laptop_url": LAPTOP, "desktop_url": DESKTOP})
        if path == "/api/apps":
            return self._json({"ok": True, **apps_status()})
        if path == "/api/health":
            return self._json({"ok": True, **machine_health()})
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
        if path == "/api/ltx/sync":
            return self._json(ltx_sync_async("manual"))
        if path == "/api/ltx/115-refresh":
            if self.headers.get("Origin") or self.headers.get("Sec-Fetch-Site"):
                return self._json({"ok": False, "error": "浏览器请求被拒绝"}, 403)
            supplied = self.headers.get("X-LTX-115-Key", "")
            try:
                with open(os.path.join(LTX_DIR, "data", "115.key"), encoding="ascii") as f:
                    expected = f.read().strip()
            except OSError:
                expected = ""
            if not supplied or not expected or not hmac.compare_digest(supplied, expected):
                return self._json({"ok": False, "error": "未授权"}, 403)
            return self._json(ltx_115_refresh())
        if path == "/api/ltx/ingest":
            body = self._read_json()
            if body is None:
                return self._json({"ok": False, "error": "请求体不是合法 JSON，或超过 64MB"}, 400)
            return self._json(ltx_ingest_async(body))
        if path == "/api/ltx/delete":
            body = self._read_json(1 * 1024 * 1024)
            if body is None:
                return self._json({"ok": False, "error": "请求体不是合法 JSON"}, 400)
            return self._json(ltx_delete(body.get("codes") if isinstance(body, dict) else None))
        if path == "/api/ltx/revive":
            body = self._read_json(1 * 1024 * 1024)
            if body is None:
                return self._json({"ok": False, "error": "请求体不是合法 JSON"}, 400)
            return self._json(ltx_revive(body.get("codes") if isinstance(body, dict) else None))
        if path == "/api/ltx/rebuild":
            return self._json(ltx_rebuild())
        if path == "/api/ltx/backup":
            return self._json(ltx_backup_async("manual"))
        if path == "/api/dns":
            return self._dns()
        self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def log_message(self, fmt, *args):
        pass


# ---------------------------------------------------------------- LTX 抓取调度
# 抓取/封面/建页全部在容器内跑，数据落在 /app/ltx（挂载到 SSD 的 rw 目录）。
# 代理走 host 网络的 127.0.0.1:7890（容器是 host 模式，即路由器自身的 mihomo）。

# 2026-11-12 实测：原站首页已无 <script id="ltx-items">，/api/items 带真实 Chrome UA
# 经路由器 mihomo 出口仍返回 403 {"reason":"verify"}（Cloudflare Turnstile）。
# 裸抓必然 rc=3，定时抓取与页面「立即更新」一律停用；数据只走 /api/ltx/ingest。
# 容器启动参数写死了 HWMON_LTX_SYNC=1，改默认值会被 env 覆盖，所以这里直接写死关闭。
LTX_SYNC_ON = False
LTX_INTERVAL = float(os.environ.get("HWMON_LTX_HOURS", "6")) * 3600
LTX_FIRST_DELAY = float(os.environ.get("HWMON_LTX_DELAY", "120"))

# 更新推送口径：成功静默，只报异常（失败 + 超时）。
# 高频 ingest 会放大「一次不完整数据标一大批 dead」，所以合并前先挡一道完整性校验。
LTX_MIN_RATIO = float(os.environ.get("HWMON_LTX_MIN_RATIO", "0.9"))
# 浏览器侧每 30 分钟推一轮，漏两轮就说明标签页/脚本/验证出问题了。
LTX_STALE_HOURS = float(os.environ.get("HWMON_LTX_STALE_HOURS", "1"))
LTX_LAST_INGEST = os.path.join(LTX_DIR, "data", "last_ingest.json")
# 同一次「停滞事件」只推一条；出现新的成功 ingest 后重置，下次停滞可以再告。
_ltx_stale_alerted = {"at": 0.0}

# 手动更新与定时任务共用一份运行状态，同一时刻只允许跑一个
_ltx_run = {"running": False, "started": 0.0, "last": None, "last_rc": None, "trigger": None}
_ltx_run_lock = threading.Lock()


def _ltx_covers_count():
    d = os.path.join(LTX_DIR, "covers")
    try:
        return len(os.listdir(d))
    except OSError:
        return 0


def ltx_status():
    """给前端看的抓取状态：条目数、封面数、最近一次同步时间。"""
    out = {
        "enabled": LTX_SYNC_ON,
        "interval_h": round(LTX_INTERVAL / 3600, 2),
        "covers": _ltx_covers_count(),
        "running": _ltx_run["running"],
        "run_started": _ltx_run["started"] or None,
        "last_run": _ltx_run["last"],
        "last_rc": _ltx_run["last_rc"],
        "last_trigger": _ltx_run["trigger"],
    }
    try:
        with open(os.path.join(LTX_DIR, "data", "state.json"), encoding="utf-8") as f:
            st = json.load(f)
        st.pop("sha256", None)
        out.update(st)
    except Exception as e:
        out["state_error"] = str(e)
    try:
        p = os.path.join(LTX_DIR, "index.html")
        out["index_size"] = os.path.getsize(p)
        out["index_mtime"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                           time.localtime(os.path.getmtime(p)))
    except OSError:
        pass
    return out


def ltx_changes():
    """给页面「同步日志」面板读的结构化变更历史（只读）。"""
    try:
        with open(os.path.join(LTX_DIR, "data", "changes.json"), encoding="utf-8") as f:
            d = json.load(f)
        rounds = d.get("rounds") if isinstance(d, dict) else []
        return {"ok": True,
                "updated_at": d.get("updated_at") if isinstance(d, dict) else None,
                "rounds": (rounds or [])[-100:]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:160], "rounds": []}


def ltx_log_failure(n, err):
    """合并失败 / 数据被拒收时补记一轮，好让页面日志能看到「哪次没成」。"""
    p = os.path.join(LTX_DIR, "data", "changes.json")
    try:
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {"rounds": []}
        rounds = d.get("rounds") if isinstance(d, dict) else None
        if not isinstance(rounds, list):
            rounds = []
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        rounds.append({"time": now, "mode": "ingest", "ok": False, "error": err,
                       "items": n, "count": 0,
                       "added": [], "removed": [], "revived": []})
        d["rounds"] = rounds[-100:]
        d["updated_at"] = now
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception as e:
        print("[ltx] 写失败日志异常：%s" % e, flush=True)


def _run_ltx(script, timeout, extra=None):
    """跑一个抓取脚本，把最后几行输出转到容器日志。"""
    cmd = [sys.executable, "-u", os.path.join(LTX_DIR, script)] + list(extra or [])
    try:
        r = subprocess.run(
            cmd, cwd=LTX_DIR, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        out = (r.stdout or b"").decode("utf-8", "replace").strip().splitlines()
        for line in out[-8:]:
            print("[ltx] " + line, flush=True)
        if r.returncode != 0:
            err = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            for line in err[-5:]:
                print("[ltx][err] " + line, flush=True)
        return r.returncode
    except Exception as e:
        print("[ltx] %s 执行失败：%s" % (script, e), flush=True)
        return -1


def _ltx_run_once(trigger):
    """跑一轮镜像。返回 rc；已在跑则返回 None（不排队）。"""
    with _ltx_run_lock:
        if _ltx_run["running"]:
            return None
        _ltx_run.update({"running": True, "started": time.time(), "trigger": trigger})
    rc = -1
    try:
        rc = _run_ltx("ltx_mirror.py", 5400)
    finally:
        with _ltx_run_lock:
            _ltx_run.update({"running": False, "last": time.time(), "last_rc": rc})
    return rc


def ltx_sync_async(trigger="manual"):
    """手动更新：立刻返回，后台线程跑，前端轮询 /api/ltx 的 running。"""
    if not LTX_SYNC_ON:
        return {"ok": False, "error": "抓取调度已关闭（HWMON_LTX_SYNC=0）"}
    with _ltx_run_lock:
        if _ltx_run["running"]:
            return {"ok": True, "started": False, "reason": "already running",
                    "run_started": _ltx_run["started"]}
    threading.Thread(target=_ltx_run_once, args=(trigger,), daemon=True).start()
    return {"ok": True, "started": True}


# ---------------------------------------------------------------- LTX 手工维护
# 站点改版后裸抓不可用（数据走 /api/items + 人机验证），所以数据可以由本机浏览器
# 取到后 POST 推过来；删除则写进 data/deleted.json 永久剔除。

def _ltx_solo(script, timeout, extra=None, trigger="local"):
    """独占跑一次脚本，跟定时/手动抓取共用一把锁。已在跑就返回 None，不排队。"""
    with _ltx_run_lock:
        if _ltx_run["running"]:
            return None
        _ltx_run.update({"running": True, "started": time.time(), "trigger": trigger})
    rc = -1
    try:
        rc = _run_ltx(script, timeout, extra)
    finally:
        with _ltx_run_lock:
            _ltx_run.update({"running": False, "last": time.time(), "last_rc": rc})
    return rc


def ltx_delete(codes):
    """番号写进 data/deleted.json 永久剔除，然后只重建页面（不联网，秒级）。"""
    if not isinstance(codes, list) or not codes:
        return {"ok": False, "error": "codes 不能为空"}
    codes = [c.strip() for c in codes if isinstance(c, str) and c.strip()]
    if not codes:
        return {"ok": False, "error": "codes 里没有有效番号"}
    if len(codes) > 2000:
        return {"ok": False, "error": "一次最多删 2000 条"}

    path = os.path.join(LTX_DIR, "data", "deleted.json")
    cur = {}
    try:
        with open(path, encoding="utf-8") as f:
            cur = json.load(f) or {}
        if not isinstance(cur, dict):
            cur = {}
    except Exception:
        cur = {}

    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    added = 0
    for c in codes:
        if c not in cur:
            cur[c] = now
            added += 1

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError as e:
        return {"ok": False, "error": "写 deleted.json 失败：%s" % e}

    rc = _ltx_solo("ltx_mirror.py", 900, ["--rebuild", "--no-img"], trigger="delete")
    if rc is None:
        return {"ok": False, "error": "后台任务正在跑（抓取/合并），等它结束再删"}
    print("[ltx] 手工删除 %d 条，黑名单共 %d 条" % (added, len(cur)), flush=True)
    if rc != 0:
        return {"ok": False, "error": "已写进黑名单，但重建页面失败 rc=%s" % rc}
    return {"ok": True, "added": added, "total": len(cur)}


def ltx_115_refresh():
    """仅提交预置文件名的候选快照；验证、替换和页面重建共享原有锁。"""
    with _ltx_run_lock:
        if _ltx_run["running"]:
            return {"ok": False, "error": "同步或重建正在进行，保留候选快照"}
        _ltx_run.update({"running": True, "started": time.time(), "trigger": "115-refresh"})
    rc = -1
    data_dir = os.path.join(LTX_DIR, "data")
    candidate = os.path.join(data_dir, "115.pending.json")
    target = os.path.join(data_dir, "115.json")
    backup = os.path.join(data_dir, "115.previous.json")
    try:
        with open(candidate, encoding="utf-8") as f:
            data = json.load(f)
        states = data.get("states")
        stamp = float(data.get("scanned_at_epoch", 0))
        if not isinstance(states, dict) or not states or len(states) > 20000:
            raise ValueError("候选状态为空或格式不正确")
        if not (0 <= time.time() - stamp <= 3600):
            raise ValueError("候选扫描时间无效或已过期")
        if any(not isinstance(k, str) or not k or v not in ("115已下载", "115有原版", "暂时未收录") for k, v in states.items()):
            raise ValueError("候选包含无效状态")
        if os.path.exists(target):
            with open(target, "rb") as src, open(backup + ".tmp", "wb") as dst:
                dst.write(src.read())
            os.replace(backup + ".tmp", backup)
        os.replace(candidate, target)
        rc = _run_ltx("ltx_mirror.py", 900, ["--rebuild", "--no-img"])
        if rc != 0:
            if os.path.exists(backup):
                os.replace(backup, target)
                _run_ltx("ltx_mirror.py", 900, ["--rebuild", "--no-img"])
            return {"ok": False, "error": "重建失败，已恢复上次状态并尝试恢复页面", "rc": rc}
        return {"ok": True, "count": len(states), "scanned_at": data.get("scanned_at")}
    except (OSError, ValueError, TypeError, AttributeError) as e:
        return {"ok": False, "error": str(e)}
    finally:
        with _ltx_run_lock:
            _ltx_run.update({"running": False, "last": time.time(), "last_rc": rc})


def ltx_115_status():
    """只返回状态统计，不暴露115路径。"""
    path = os.path.join(LTX_DIR, "data", "115.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        states = data.get("states") or {}
        return {"ok": True, "scanned_at": data.get("scanned_at"),
                "scanned_at_epoch": data.get("scanned_at_epoch"),
                "downloaded": sum(1 for x in states.values() if x == "115已下载"),
                "original": sum(1 for x in states.values() if x == "115有原版")}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"ok": False, "error": "尚无有效115快照"}


def ltx_rebuild():
    """只用本地库重建页面，不联网。"""
    rc = _ltx_solo("ltx_mirror.py", 900, ["--rebuild", "--no-img"], trigger="rebuild")
    if rc is None:
        return {"ok": False, "error": "后台任务正在跑，稍后再试"}
    return {"ok": rc == 0, "rc": rc}


def ltx_revive(codes):
    """把「原站已删」转回普通数据：清 dead 标记，并打上 keep（以后合并时不再被判死）。"""
    if not isinstance(codes, list) or not codes:
        return {"ok": False, "error": "codes 不能为空"}
    codes = [c.strip() for c in codes if isinstance(c, str) and c.strip()]
    if not codes:
        return {"ok": False, "error": "codes 里没有有效番号"}
    if len(codes) > 2000:
        return {"ok": False, "error": "一次最多处理 2000 条"}

    path = os.path.join(LTX_DIR, "data", "library.json")
    want = set(codes)

    with _ltx_run_lock:
        if _ltx_run["running"]:
            return {"ok": False, "error": "后台任务正在跑，稍后再试"}
        _ltx_run.update({"running": True, "started": time.time(), "trigger": "revive"})

    rc = -1
    hit = 0
    try:
        try:
            with open(path, encoding="utf-8") as f:
                lib = json.load(f)
        except Exception as e:
            return {"ok": False, "error": "读 library.json 失败：%s" % e}

        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        for it in lib.get("items") or []:
            if it.get("code") in want:
                it.pop("dead", None)
                it.pop("dead_at", None)
                it.pop("dead_seen", None)
                it["keep"] = True
                it["keep_at"] = now
                hit += 1

        if not hit:
            return {"ok": False, "error": "库里没找到这些番号"}

        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(lib, f, ensure_ascii=False)
        os.replace(tmp, path)

        rc = _run_ltx("ltx_mirror.py", 900, ["--rebuild", "--no-img"])
    finally:
        with _ltx_run_lock:
            _ltx_run.update({"running": False, "last": time.time(), "last_rc": rc})

    print("[ltx] 取消核实 %d 条" % hit, flush=True)
    if rc != 0:
        return {"ok": False, "error": "标记已清除，但重建页面失败 rc=%s" % rc}
    return {"ok": True, "count": hit}


def _ltx_alert(title, content):
    """复用告警引擎现成的企微 webhook；推不出去也不影响主流程。"""
    try:
        webhook = (load_push_cfg().get("wecom_webhook") or "").strip()
        wecom_send(webhook, title, content)
    except Exception as e:
        print("[ltx] 告警推送异常：%s" % e, flush=True)


def _ltx_last_items():
    """上一轮吃到的 items 数量，用来挡住不完整数据。"""
    try:
        with open(LTX_LAST_INGEST, encoding="utf-8") as f:
            d = json.load(f)
        n = d.get("items") if isinstance(d, dict) else None
        if isinstance(n, int) and n > 0:
            return n
    except Exception:
        pass
    try:
        with open(os.path.join(LTX_DIR, "data", "latest.json"), encoding="utf-8") as f:
            d = json.load(f)
        n = len(d.get("items") or [])
        if n:
            return n
    except Exception:
        pass
    return 0


def _ltx_state_line():
    try:
        with open(os.path.join(LTX_DIR, "data", "state.json"), encoding="utf-8") as f:
            st = json.load(f)
        return "库内 %s 条 · 原站已删 %s 条" % (st.get("count", "?"), st.get("dead", "?"))
    except Exception:
        return ""


def _ltx_ingest_worker(n):
    """后台合并：成功静默写时间戳，失败才推告警（用户口径：失败+超时，成功不推）。"""
    rc = _ltx_solo("ltx_mirror.py", 5400, ["--ingest", "data/incoming.json"], "ingest")
    if rc is None:
        return
    if rc != 0:
        ltx_log_failure(n, "合并脚本 rc=%s" % rc)
        _ltx_alert("【告警】LTX 更新失败",
                   "日期 %s\n收到 %d 条，合并脚本 rc=%s\n%s\n看 docker logs hwmon-hub | grep ltx"
                   % (time.strftime("%Y%m%d"), n, rc, _ltx_state_line()))
        return
    try:
        tmp = LTX_LAST_INGEST + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"at": time.time(),
                       "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "items": n}, f)
        os.replace(tmp, LTX_LAST_INGEST)
    except OSError as e:
        print("[ltx] 写 last_ingest.json 失败：%s" % e, flush=True)


def ltx_ingest_async(payload):
    """接收本机浏览器取到的 /api/items，落盘后后台合并（只增不删）。"""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return {"ok": False, "error": "payload 必须是 {items: [...]} 这样的对象"}
    if not payload["items"]:
        return {"ok": False, "error": "items 是空的，拒绝写入"}

    n = len(payload["items"])
    last = _ltx_last_items()
    if last and n < last * LTX_MIN_RATIO:
        msg = ("本次 %d 条，上一轮 %d 条，低于 %.0f%% 阈值，已拒绝合并。"
               "若原站确实批量下架，把 HWMON_LTX_MIN_RATIO 调小再推。"
               % (n, last, LTX_MIN_RATIO * 100))
        ltx_log_failure(n, "数据不完整，已拒收：%s" % msg)
        _ltx_alert("【告警】LTX 更新数据不完整",
                   "日期 %s\n%s\n%s" % (time.strftime("%Y%m%d"), msg, _ltx_state_line()))
        return {"ok": False, "error": msg}

    path = os.path.join(LTX_DIR, "data", "incoming.json")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        return {"ok": False, "error": "写 incoming.json 失败：%s" % e}

    with _ltx_run_lock:
        if _ltx_run["running"]:
            return {"ok": False, "error": "后台任务正在跑，稍后再推"}
    threading.Thread(target=_ltx_ingest_worker, args=(n,), daemon=True).start()
    print("[ltx] 收到外部数据 %d 条，开始合并" % n, flush=True)
    return {"ok": True, "started": True, "count": n}


# ---------------------------------------------------------------- LTX 每日备份
# 数据在这台路由器上，台式机和路由器同一个局域网，所以由台式机主动来拉最快。
# 容器里没有 ssh 客户端（alpine 镜像），路由器宿主机的 /etc 又是 tmpfs，
# 配好的密钥重启就没 —— 所以不走推，改成一个只读的下载口。
# 只放行 _backup/ltx-YYYYMMDD.tar，且流式发送，避免几百 MB 整个读进内存。

LTX_BACKUP_DIR = os.path.join(LTX_DIR, "_backup")
LTX_BACKUP_KEEP = int(os.environ.get("HWMON_LTX_BACKUP_KEEP", "3"))
LTX_BACKUP_AT = os.environ.get("HWMON_LTX_BACKUP_AT", "21:00")
LTX_BACKUP_RE = re.compile(r"^ltx-\d{8}\.tar$")

_ltx_bk = {"running": False, "started": 0.0, "last": None, "rc": None,
           "size": 0, "file": "", "msg": "", "trigger": None}
_ltx_bk_lock = threading.Lock()


def _ltx_backup_once(trigger="schedule"):
    """打包 covers/ + data/ + index.html。返回 0 成功 / None 跳过 / 非 0 失败。"""
    with _ltx_bk_lock:
        if _ltx_bk["running"]:
            return None
        if _ltx_run["running"]:
            # 正在抓取时打包会拿到半个状态，直接跳过这一轮
            _ltx_bk.update({"msg": "镜像同步进行中，跳过本轮备份"})
            print("[ltx] 备份跳过：同步进行中", flush=True)
            return None
        _ltx_bk.update({"running": True, "started": time.time(),
                        "rc": None, "msg": "", "trigger": trigger})
    name = "ltx-%s.tar" % time.strftime("%Y%m%d")
    path = os.path.join(LTX_BACKUP_DIR, name)
    tmp = path + ".part"
    rc, msg, size = 1, "", 0
    try:
        os.makedirs(LTX_BACKUP_DIR, exist_ok=True)
        # 不压缩：里面全是 jpg，gzip 压不动，白烧 CPU
        p = subprocess.run(["tar", "cf", tmp, "-C", LTX_DIR, "covers", "data", "index.html"],
                           capture_output=True, timeout=7200)
        if p.returncode != 0:
            msg = (p.stderr or b"").decode("utf-8", "replace").strip()[-300:]
        else:
            os.replace(tmp, path)
            rc = 0
            size = os.path.getsize(path)
    except Exception as e:
        msg = str(e)[-300:]
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
    if rc == 0:
        try:
            olds = sorted(f for f in os.listdir(LTX_BACKUP_DIR) if LTX_BACKUP_RE.match(f))
            for f in olds[:-LTX_BACKUP_KEEP] if LTX_BACKUP_KEEP > 0 else []:
                os.remove(os.path.join(LTX_BACKUP_DIR, f))
                print("[ltx] 清理旧备份 %s" % f, flush=True)
        except OSError:
            pass
    with _ltx_bk_lock:
        _ltx_bk.update({"running": False, "last": time.time(), "rc": rc,
                        "size": size, "file": name if rc == 0 else "", "msg": msg})
    print("[ltx] 备份结束 rc=%s size=%s msg=%s" % (rc, size, msg), flush=True)
    return rc


def ltx_backup_async(trigger="manual"):
    with _ltx_bk_lock:
        if _ltx_bk["running"]:
            return {"ok": True, "started": False, "reason": "already running"}
    threading.Thread(target=_ltx_backup_once, args=(trigger,), daemon=True).start()
    return {"ok": True, "started": True}


def ltx_backup_status():
    with _ltx_bk_lock:
        d = dict(_ltx_bk)
    files = []
    try:
        for f in sorted(os.listdir(LTX_BACKUP_DIR)):
            if not LTX_BACKUP_RE.match(f):
                continue
            fp = os.path.join(LTX_BACKUP_DIR, f)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            files.append({"name": f, "size": st.st_size, "mtime": int(st.st_mtime)})
    except OSError:
        pass
    d["files"] = files
    d["keep"] = LTX_BACKUP_KEEP
    d["at"] = LTX_BACKUP_AT
    return d


def backup_loop():
    """每天 LTX_BACKUP_AT（默认 21:00，容器 TZ=Asia/Shanghai）打一个包。"""
    while True:
        try:
            hh, mm = [int(x) for x in LTX_BACKUP_AT.split(":")]
        except ValueError:
            print("[ltx] 备份时间配置有误：%r" % LTX_BACKUP_AT, flush=True)
            return
        now = time.localtime()
        nxt = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, hh, mm, 0, 0, 0, -1))
        if nxt <= time.time() + 5:
            nxt += 86400
        print("[ltx] 下次备份：%s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(nxt)), flush=True)
        while True:
            left = nxt - time.time()
            if left <= 0:
                break
            time.sleep(min(left, 300))
        _ltx_backup_once("schedule")


def ltx_loop():
    if not LTX_SYNC_ON:
        print("[ltx] 抓取调度已关闭（HWMON_LTX_SYNC=0）", flush=True)
        return
    time.sleep(LTX_FIRST_DELAY)
    logp = os.path.join(LTX_DIR, "sync.log")
    while True:
        # sync.log 超过 2MB 就清空，避免长期运行无限增长
        try:
            if os.path.getsize(logp) > 2 * 1024 * 1024:
                open(logp, "w").close()
                print("[ltx] sync.log 已清空", flush=True)
        except OSError:
            pass
        t0 = time.time()
        # ltx_mirror.py 一把梭：抓整页 → 补图片 → 换本地路径 → 写 index.html
        rc = _ltx_run_once("schedule")
        if rc is None:
            print("[ltx] 手动更新正在进行，本轮跳过", flush=True)
        elif rc != 0:
            print("[ltx] 镜像失败，等下一轮", flush=True)
        spent = time.time() - t0
        print("[ltx] 本轮结束，用时 %.1fs，%.1fh 后再跑" % (spent, LTX_INTERVAL / 3600), flush=True)
        time.sleep(max(600.0, LTX_INTERVAL - spent))


def ltx_stale_loop():
    """每 5 分钟检查一次：距最后一次成功 ingest 超过 LTX_STALE_HOURS 就预警一次。

    自动抓取跑在笔记本浏览器里，路由器收不到请求时没有任何「失败」信号，
    只能靠「多久没收到数据」来发现。同一次停滞只推一条，不重复轰炸。
    """
    while True:
        try:
            at = 0.0
            try:
                with open(LTX_LAST_INGEST, encoding="utf-8") as f:
                    at = float(json.load(f).get("at") or 0)
            except Exception:
                pass
            if not at:
                try:
                    at = os.path.getmtime(os.path.join(LTX_DIR, "data", "state.json"))
                except OSError:
                    at = time.time()
            hours = (time.time() - at) / 3600.0
            if hours > LTX_STALE_HOURS:
                if _ltx_stale_alerted["at"] != at:
                    _ltx_alert("【告警】LTX 已 %.1f 小时没抓到数据" % hours,
                               "日期 %s\n最后成功 %s\n%s\n可能原因：原站标签页被关 / Edge 没开 / "
                               "Cloudflare 验证过期需手点一次\n（同一次停滞只推这一条，恢复后重置）"
                               % (time.strftime("%Y%m%d"),
                                  time.strftime("%Y-%m-%d %H:%M", time.localtime(at)),
                                  _ltx_state_line()))
                    _ltx_stale_alerted["at"] = at
            else:
                _ltx_stale_alerted["at"] = 0.0
        except Exception as e:
            print("[ltx] 超时检查异常：%s" % e, flush=True)
        time.sleep(300)


if __name__ == "__main__":
    threading.Thread(target=alert_loop, daemon=True).start()
    threading.Thread(target=ltx_loop, daemon=True).start()
    threading.Thread(target=ltx_stale_loop, daemon=True).start()
    threading.Thread(target=backup_loop, daemon=True).start()
    # 长期归档：分钟级硬件采样 + 日级汇总，PC 上的 7 天库删了也不影响历史
    threading.Thread(target=hub_archiver_poller, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("hwmon-router-hub listening on 0.0.0.0:%d" % PORT, flush=True)
    print("  laptop=%s  desktop=%s" % (LAPTOP, DESKTOP), flush=True)
    print("  wol=%s" % WOL_URL, flush=True)
    print("  alert engine: state=%s cfg=%s" % (STATE_DIR, PUSH_CFG), flush=True)
    srv.serve_forever()
