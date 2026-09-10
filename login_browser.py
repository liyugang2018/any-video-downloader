# -*- coding: utf-8 -*-
"""站点登录向导：弹出系统浏览器的独立实例，用户登录后通过 CDP 抓取 cookies 存档。

原理：以独立 user-data-dir 启动 Chrome/Edge 并开放调试端口，通过
DevTools Protocol 的 Storage.getCookies 拿到浏览器内已解密的 cookies
（含 httpOnly），写成 Netscape cookies.txt 供 yt-dlp 使用。
不读取用户日常浏览器的加密 cookies，绕开 Chrome App-Bound Encryption 限制。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional

import websocket

import proxy_manager

EventCallback = Callable[[str], None]   # 纯文本日志回调

# 浏览器候选路径（按顺序找）
BROWSER_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

# 支持登录的站点配置。login_markers：全部出现才视为登录成功
SITE_CONFIGS = {
    "twitter": {
        "name": "X (Twitter)",
        "domains": [".x.com", ".twitter.com", "x.com", "twitter.com"],
        "url": "https://x.com/",
        "login_markers": ["auth_token"],
        "cookie_file": "twitter.txt",
    },
    "youtube": {
        # google.com 域的 SID/SAPISID 等登录 cookie 一并导出（yt-dlp 按域使用）
        "name": "YouTube",
        "domains": ["youtube.com", "youtu.be", "google.com"],
        "url": "https://www.youtube.com/",
        "login_markers": ["LOGIN_INFO", "SAPISID"],  # 两者都只在登录后出现
        "cookie_file": "youtube.txt",
    },
    "bilibili": {
        "name": "Bilibili",
        "domains": ["bilibili.com", "biliapi.net", "bilivideo.com",
                    "hdslb.com", "biliapi.com", "acgvideo.com"],
        "url": "https://www.bilibili.com/",
        "login_markers": ["SESSDATA", "DedeUserID"],
        "cookie_file": "bilibili.txt",
    },
    "douyin": {
        "name": "抖音",
        "domains": ["douyin.com", "iesdouyin.com", "douyinvod.com",
                    "douyinpic.com", "snssdk.com", "amemv.com", "zjcdn.cn"],
        "url": "https://www.douyin.com/",
        "login_markers": ["sessionid"],   # 抖音 API 必须登录态，游客 cookies 实测无效
        "cookie_file": "douyin.txt",
    },
    "ixigua": {
        "name": "西瓜视频",
        "domains": ["ixigua.com"],
        "url": "https://www.ixigua.com/",
        "login_markers": ["ttwid"],       # 字节系：ttwid 即游客验证，不必登录
        "cookie_file": "ixigua.txt",
    },
    "weibo": {
        "name": "微博",
        "domains": ["weibo.com", "weibo.cn", "miaopai.com", "wbimg.cn"],
        "url": "https://weibo.com/",
        "login_markers": ["SUB"],         # 游客即有 SUB；登录后同样适用
        "cookie_file": "weibo.txt",
    },
}

_POLL_INTERVAL = 2.0        # 登录状态轮询间隔（秒）
_PAGE_LOAD_WAIT = 3.0       # 页面 ws 就绪后的首次等待


def cookies_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    d = Path(base) / "VideoDownloader" / "cookies"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _data_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "VideoDownloader"


def find_browser() -> Optional[str]:
    for cand in BROWSER_CANDIDATES:
        if Path(cand).is_file():
            return cand
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http_json(port: int, path: str, timeout: float = 2.0) -> Optional[object]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _wait_page_ws(port: int, deadline: float) -> Optional[str]:
    """等待调试端口就绪并返回第一个 page 级 WebSocket 地址。"""
    while time.time() < deadline:
        targets = _http_json(port, "/json/list")
        if isinstance(targets, list):
            for t in targets:
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    return t["webSocketDebuggerUrl"]
        time.sleep(0.5)
    return None


def _fetch_all_cookies(ws) -> list[dict]:
    ws.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == 1:
            return (msg.get("result") or {}).get("cookies", [])


def _domain_match(cookie_domain: str, domains: list[str]) -> bool:
    d = cookie_domain.lower().lstrip(".")
    return any(d == site or d.endswith("." + site)
               for site in (x.lower().lstrip(".") for x in domains))


def _to_netscape(cookies: list[dict]) -> str:
    """CDP cookies -> Netscape cookies.txt 文本。"""
    lines = ["# Netscape HTTP Cookie File",
             "# 由万能视频下载导出，供 yt-dlp 使用", ""]
    for c in cookies:
        domain = c.get("domain", "")
        if not domain:
            continue
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if c.get("secure") else "FALSE"
        expires = int(c.get("expires") or 0)
        if expires <= 0:
            expires = 0
        prefix = "#HttpOnly_" if c.get("httpOnly") else ""
        lines.append("\t".join([
            prefix + domain, include_sub, c.get("path", "/"), secure,
            str(expires), c.get("name", ""), c.get("value", ""),
        ]))
    return "\n".join(lines) + "\n"


def site_cookie_file(site_key: str) -> Optional[Path]:
    cfg = SITE_CONFIGS.get(site_key)
    if not cfg:
        return None
    f = cookies_dir() / cfg["cookie_file"]
    return f if f.is_file() else None


def site_user_agent(site_key: str) -> Optional[str]:
    """读取保存登录时记录的浏览器 User-Agent（同名 .ua 文件）。"""
    f = cookies_dir() / (SITE_CONFIGS[site_key]["cookie_file"] + ".ua")
    if f.is_file():
        ua = f.read_text(encoding="utf-8").strip()
        if ua:
            return ua
    return None


def match_site_for_url(url: str) -> Optional[str]:
    """根据 URL 自动判断对应哪个已配置站点。"""
    host = url.split("//")[-1].split("/")[0].lower()
    for key, cfg in SITE_CONFIGS.items():
        for d in cfg["domains"]:
            d = d.lstrip(".")
            if host == d or host.endswith("." + d):
                return key
    return None


class LoginSession:
    """一次站点登录会话：启动浏览器、轮询登录状态、保存 cookies。

    在工作线程中调用 run()；save_now() 可由界面在用户点击
    "完成登录"时调用（不校验特征 cookie，信任用户判断）。
    """

    def __init__(self, site_key: str, log: EventCallback):
        self.cfg = SITE_CONFIGS[site_key]
        self.site_key = site_key
        self.log = log
        self._ws = None
        self._proc: Optional[subprocess.Popen] = None
        self._port = 0
        self._saved = False

    # ---- 主流程（工作线程） ----

    def run(self, stop_event) -> bool:
        browser = find_browser()
        if not browser:
            self.log("[错误] 未找到 Chrome 或 Edge，无法打开登录窗口。")
            return False

        port = _free_port()
        profile = _data_root() / "login-profile"
        profile.mkdir(parents=True, exist_ok=True)
        self._port = port
        self.log(f"正在打开 {self.cfg['name']} 登录窗口…")
        # 被墙站点的登录窗口必须走代理，否则页面打不开
        proxy = proxy_manager.proxy_for(self.cfg["url"])
        proxy_args = [f"--proxy-server={proxy}"] if proxy else []
        if proxy:
            self.log(f"登录窗口使用代理：{proxy}")
        try:
            self._proc = subprocess.Popen([
                browser,
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile}",
                "--remote-allow-origins=*",
                "--no-first-run", "--no-default-browser-check",
                "--window-size=1000,760",
                *proxy_args,
                self.cfg["url"],
            ])
        except OSError as exc:
            self.log(f"[错误] 浏览器启动失败：{exc}")
            return False

        page_ws = _wait_page_ws(port, time.time() + 20)
        if not page_ws:
            self.log("[错误] 浏览器调试接口未就绪，登录窗口无法监控。")
            self.close_browser()
            return False
        time.sleep(_PAGE_LOAD_WAIT)
        try:
            self._ws = websocket.create_connection(page_ws, timeout=15, suppress_origin=True)
        except Exception as exc:
            self.log(f"[错误] 连接浏览器失败：{exc}")
            self.close_browser()
            return False

        self.log(f"请在弹出的窗口中登录{self.cfg['name']}。登录成功后会自动保存，"
                 "也可以在主界面点\"完成登录\"。")

        markers = self.cfg["login_markers"]
        while not stop_event.is_set():
            if self._proc.poll() is not None:
                if not self._saved:
                    self.log("浏览器窗口已被关闭，登录未完成。")
                self._ws = None
                return self._saved
            try:
                cookies = _fetch_all_cookies(self._ws)
            except Exception:
                if self._saved:
                    return True
                self.log("与登录窗口的连接已断开。")
                return False
            site_cookies = [c for c in cookies
                            if _domain_match(c.get("domain", ""), self.cfg["domains"])]
            names = {c.get("name") for c in site_cookies}
            if self._saved:   # save_now 已保存过
                return True
            if markers and all(m in names for m in markers):
                self._save(site_cookies)
                return True
            time.sleep(_POLL_INTERVAL)

        self.close_browser()
        return self._saved

    # ---- 保存与清理 ----

    def _fetch_user_agent(self) -> str:
        """取登录浏览器的 UA，保存后下载时使用，保持请求特征一致。"""
        try:
            self._ws.send(json.dumps({
                "id": 2, "method": "Runtime.evaluate",
                "params": {"expression": "navigator.userAgent",
                           "returnByValue": True}}))
            while True:
                msg = json.loads(self._ws.recv())
                if msg.get("id") == 2:
                    return ((msg.get("result") or {}).get("result") or {}).get("value") or ""
        except Exception:
            return ""

    def _save(self, site_cookies: list[dict]) -> None:
        target = cookies_dir() / self.cfg["cookie_file"]
        target.write_text(_to_netscape(site_cookies), encoding="utf-8")
        ua = self._fetch_user_agent()
        if ua:
            (cookies_dir() / (self.cfg["cookie_file"] + ".ua")).write_text(ua, encoding="utf-8")
        self._saved = True
        self.log(f"{self.cfg['name']} 登录成功，已保存 {len(site_cookies)} 条登录信息。")
        self.log("之后粘贴该站链接会自动使用此登录状态。")
        self.close_browser()

    def save_now(self) -> bool:
        """用户手动确认完成：立即抓取并保存。

        同样要求登录特征 cookie 存在——游客态的半截 cookies 无法通过
        站点风控，保存了反而有害。
        """
        if self._ws is None or self._saved:
            return self._saved
        try:
            cookies = _fetch_all_cookies(self._ws)
        except Exception:
            return False
        site_cookies = [c for c in cookies
                        if _domain_match(c.get("domain", ""), self.cfg["domains"])]
        names = {c.get("name") for c in site_cookies}
        markers = self.cfg["login_markers"]
        if not markers or not all(m in names for m in markers):
            self.log(f"尚未检测到登录信息（需要 {', '.join(markers)}），"
                     "请先在窗口中完成登录。")
            return False
        self._save(site_cookies)
        return True

    def close_browser(self) -> None:
        """尽量优雅关闭登录浏览器窗口。"""
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.send(json.dumps({"id": 9, "method": "Browser.close"}))
                time.sleep(1.0)
            except Exception:
                pass
            try:
                ws.close()
            except Exception:
                pass
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass
