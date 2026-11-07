#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
路由器侧聚合入口 hwmon-router-hub。

纯 Python 标准库，无第三方依赖，可跑在 python:3.12-alpine 里。
职责（刻意做得极小）：
  1. 托管工具页 index.html
  2. /api/status    探测笔记本与台式是否在线
  3. /api/easytier  反代笔记本的 peer 列表；笔记本离线时返回最后一次拓扑
  4. /wol (POST)    代理路由器的 WOL CGI，60 秒防抖

硬件监控主页仍由两台 PC 各自的 agent.py 提供，本服务不采集任何传感器。
所有地址用环境变量覆盖，换个网络不用改代码。
"""
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LAPTOP = os.environ.get("HWMON_LAPTOP", "http://10.1.1.251:8090").rstrip("/")
DESKTOP = os.environ.get("HWMON_DESKTOP", "http://10.1.1.252:8090").rstrip("/")
WOL_URL = os.environ.get("HWMON_WOL", "http://10.1.1.254:8088/cgi-bin/wol")
DNS_CGI = os.environ.get("HWMON_DNS_CGI", "http://10.1.1.254:8088/cgi-bin/dns")
PEERS_CGI = os.environ.get("HWMON_PEERS_CGI", "http://10.1.1.254:8088/cgi-bin/peers")
PC_URLS = {"laptop": LAPTOP, "desktop": DESKTOP}
PROXY_TIMEOUT = float(os.environ.get("HWMON_PROXY_TIMEOUT", "5"))
PORT = int(os.environ.get("HWMON_PORT", "8089"))
PROBE_TTL = float(os.environ.get("HWMON_PROBE_TTL", "5"))
PEER_TTL = float(os.environ.get("HWMON_PEER_TTL", "10"))
TIMEOUT = float(os.environ.get("HWMON_TIMEOUT", "3"))

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
    """在线状态缓存 5 秒，避免每次页面请求都等两次超时。"""
    now = time.time()
    with _lock:
        if now - _state["ts"] < PROBE_TTL:
            return dict(_state)
    st = {"ts": now, "laptop": probe(LAPTOP), "desktop": probe(DESKTOP)}
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
            _, body = _get(url, TIMEOUT)
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
              "target": "VeT-PC", "ts": now}
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
            # 前端据此把「本机」标给实际访问设备；静态模式无此字段时回落标路由器
            d["viewer_ip"] = self.client_address[0]
            return self._json(d)
        if path == "/api/dns":
            return self._dns()
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
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("hwmon-router-hub listening on 0.0.0.0:%d" % PORT, flush=True)
    print("  laptop=%s  desktop=%s" % (LAPTOP, DESKTOP), flush=True)
    print("  wol=%s" % WOL_URL, flush=True)
    srv.serve_forever()
