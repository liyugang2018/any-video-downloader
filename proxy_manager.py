# -*- coding: utf-8 -*-
"""本地代理探测：访问被墙站点（X/YouTube 等）时自动使用本地代理软件的端口。

探测策略：
  1. TCP 扫描常见代理软件端口（Clash 7890 / v2rayN 10809 等）
  2. 经候选端口代理访问 google generate_204 验证确实可翻墙
  3. 结果缓存，整个进程生命周期内只探测一次
"""
from __future__ import annotations

import socket
import threading
import urllib.request
from typing import Optional

# 常见代理软件的本地 HTTP 端口（按流行度排序）
PROXY_CANDIDATE_PORTS = [7890, 7897, 10809, 10808, 1080, 8118, 8888, 8080, 2080, 20171]

# 需要代理才能访问的站点（直连会被 TLS 阻断）
NEED_PROXY_DOMAINS = [
    "x.com", "twitter.com", "t.co",
    "youtube.com", "youtu.be", "googlevideo.com",
    "tiktok.com", "instagram.com", "facebook.com",
]

_VERIFY_URL = "https://www.google.com/generate_204"
_CONNECT_TIMEOUT = 0.4
_VERIFY_TIMEOUT = 6.0

_lock = threading.Lock()
_cached: Optional[str] = None     # 探测结果：'http://127.0.0.1:port' 或 None
_detected = False                 # 是否已探测过


def _port_open(port: int) -> bool:
    s = socket.socket()
    s.settimeout(_CONNECT_TIMEOUT)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def _verify_proxy(port: int) -> bool:
    """经该端口代理访问 google，验证代理真的可用。"""
    proxy = f"http://127.0.0.1:{port}"
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    opener = urllib.request.build_opener(handler)
    try:
        with opener.open(_VERIFY_URL, timeout=_VERIFY_TIMEOUT) as r:
            return r.status in (200, 204)
    except Exception:
        return False


def detect_proxy(force: bool = False) -> Optional[str]:
    """探测本地可用代理。找到返回 'http://127.0.0.1:port'，否则 None。"""
    global _cached, _detected
    with _lock:
        if _detected and not force:
            return _cached
        _detected = True
        _cached = None
        for port in PROXY_CANDIDATE_PORTS:
            if _port_open(port) and _verify_proxy(port):
                _cached = f"http://127.0.0.1:{port}"
                break
        return _cached


def needs_proxy(url: str) -> bool:
    """该 URL 的站点是否属于需要代理的站点。"""
    host = url.split("//")[-1].split("/")[0].lower()
    return any(host == d or host.endswith("." + d) for d in NEED_PROXY_DOMAINS)


def proxy_for(url: str) -> Optional[str]:
    """按 URL 决定使用的代理：被墙站点返回探测到的代理，其他返回 None（直连）。"""
    if not needs_proxy(url):
        return None
    return detect_proxy()
