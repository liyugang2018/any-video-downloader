# -*- coding: utf-8 -*-
"""核心下载模块：封装 yt-dlp 的解析、下载、进度回调与错误分类。

事件协议（emit(dict)，在下载线程中被调用）：
  log      {'msg': str}                                   普通日志
  progress {'percent': float|None, 'speed': str,
            'eta': str, 'label': str}                     进度更新
  state    {'state': str}                                 extracting/downloading/merging/finished/cancelled
  error    {'title': str, 'detail': str}                  友好错误信息
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

import yt_dlp

EventCallback = Callable[[dict], None]

# 状态常量
STATE_EXTRACTING = "extracting"
STATE_DOWNLOADING = "downloading"
STATE_MERGING = "merging"
STATE_FINISHED = "finished"
STATE_CANCELLED = "cancelled"

# 界面上可选的浏览器（名称 -> yt-dlp 参数值）
BROWSERS = {
    "Chrome": "chrome",
    "Edge": "edge",
    "Firefox": "firefox",
    "Brave": "brave",
    "Opera": "opera",
}

# 高清格式（需要 ffmpeg 合并音视频）/ 无 ffmpeg 时退化为单流
FORMAT_HIGH = "bestvideo*+bestaudio/best"
FORMAT_FALLBACK = "best"

# 错误关键词 -> (标题, 处理建议, 是否可通过登录解决)。按顺序匹配，命中即返回
_ERROR_RULES = [
    ("no such file or directory", "无法连接到该网站",
     "网络连接在加密握手阶段被中断（X/YouTube 等被墙站点的典型表现）。\n"
     "请开启你的代理软件，然后重试。程序会自动探测常见代理端口"
     "（Clash 7890、v2rayN 10809 等）。若端口特殊，可在代理软件中"
     "开启\"系统代理\"或改为常见端口。", False),
    ("cookies are needed", "该网站需要验证信息",
     "程序将打开该网站的窗口获取验证信息，获取后会自动继续下载。", True),
    ("fresh cookies", "该网站需要验证信息",
     "程序将打开该网站的窗口获取验证信息，获取后会自动继续下载。", True),
    ("unsupported url", "不支持该网站",
     "yt-dlp 暂不支持这个网址。请确认链接是具体的视频页面（而不是主页或搜索页）。", False),
    ("sign in to confirm", "该视频需要登录",
     "程序将打开登录窗口，登录后自动继续下载。", True),
    ("login required", "该视频需要登录",
     "程序将打开登录窗口，登录后自动继续下载。", True),
    ("requires authentication", "该视频需要登录",
     "程序将打开登录窗口，登录后自动继续下载。", True),
    ("must be logged in", "该视频需要登录",
     "程序将打开登录窗口，登录后自动继续下载。", True),
    ("you are not logged in", "该视频需要登录",
     "程序将打开登录窗口，登录后自动继续下载。", True),
    ("requested format", "没有可用的清晰度",
     "该视频的音视频是分离的，需要 ffmpeg 合并。请确认界面底部显示 \"ffmpeg：已就绪\" 后重试；"
     "若自动获取失败，可手动下载 ffmpeg 并放入程序目录的 ffmpeg 文件夹。", False),
    ("private video", "这是私密视频",
     "视频作者设置了私密权限，需要视频所有者账号的登录状态才能下载。", True),
    ("members-only", "这是会员专属内容",
     "需要该平台大会员账号的登录状态才能下载。", True),
    ("http error 404", "视频不存在",
     "视频可能已被删除，或链接不完整。", False),
    ("http error 403", "没有访问权限",
     "站点拒绝了下载请求，通常登录后即可解决。", True),
    ("precondition failed", "请求过于频繁",
     "站点风控限制（短时间内请求过多）。请等几分钟再试；登录该站点"
     "（自动或手动登录一次）通常可以缓解。", False),
    ("unable to extract", "解析页面失败",
     "网站可能改版了，或该页面不是视频页。可稍后重试。", False),
    ("nsig extraction", "解析失败（站点改版）",
     "网站更新了反爬机制，当前版本的解析规则已失效。", False),
    ("is not a valid url", "网址格式不正确",
     "请输入完整的视频页面链接，例如 https://www.bilibili.com/video/BVxxxx。", False),
    ("no video", "页面中没有找到视频",
     "请确认链接指向的页面包含可下载的视频。", False),
    ("premiere", "视频尚未开始",
     "这是一场预告/首播，要等开始后才能下载。", False),
    ("live event", "正在直播",
     "直播内容暂不支持下载。", False),
]

# 浏览器 cookies 读取失败的识别关键词
_COOKIE_ERROR_KEYS = (
    "could not find", "unable to decrypt", "database is locked",
    "app-bound encryption", "could not copy", "cookies database",
    "failed to get cookies", "no such file",
)


_URL_RE = re.compile(r"https?://\S+")
# URL 尾部需要剥掉的标点（半角/全角括号引号、中文句读等）
_URL_TAIL_TRIM = ")），,。．！？；;、》>」』\"'"


def normalize_url(raw: str) -> str:
    """从粘贴内容提取 URL 并规范化已知站点的特殊格式。

    处理两种常见情况：
      1. App 分享文案：整段文字中混着一个短链，取第一个 URL
      2. 抖音用户页弹窗链接（/user/xxx?modal_id=NNN）-> 视频直链 /video/NNN
    """
    text = raw.strip()
    match = _URL_RE.search(text)
    if not match:
        return text
    url = match.group(0).rstrip(_URL_TAIL_TRIM)
    if "douyin.com" in url:
        modal = parse_qs(urlparse(url).query).get("modal_id", [None])[0]
        if modal and modal.isdigit():
            url = f"https://www.douyin.com/video/{modal}"
    return url


def normalize_urls(text: str) -> tuple[list[str], list[str]]:
    """多行输入 -> (规范化 URL 列表, 无法识别的行)。

    逐行复用 normalize_url（App 分享文案 -> 短链 -> 站点直链），
    跳过空行，去重保持先后顺序；非 URL 的行返回给上层提示用户。
    """
    urls: list[str] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        url = normalize_url(raw_line)
        if not url.startswith(("http://", "https://")):
            if url:  # 空行静默忽略；有内容但识别不了的行提示用户
                dropped.append(url[:80])
            continue
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls, dropped


@dataclass
class DownloadOptions:
    """一次下载任务的全部可配置项。"""
    url: str
    output_dir: str
    browser: Optional[str] = None        # yt-dlp 浏览器名（见 BROWSERS 的值）
    cookies_file: Optional[str] = None   # cookies.txt 文件路径（优先于 browser）
    ffmpeg_dir: Optional[str] = None     # 含 ffmpeg.exe / ffprobe.exe 的目录
    user_agent: Optional[str] = None     # 与 cookies 配套的 UA，降低站点风控概率
    proxy: Optional[str] = None          # 代理（访问被墙站点时由上层探测填入）
    js_runtime_path: Optional[str] = None  # deno.exe 路径（YouTube 解析需要 JS 运行时）
    noplaylist: bool = True              # 合集/列表页只下载当前视频


class _YtLogger:
    """把 yt-dlp 内部日志转发到事件回调，避免直接写控制台。"""

    def __init__(self, emit: EventCallback):
        self._emit = emit

    def debug(self, msg: str) -> None:
        # yt-dlp 把进度行也走 debug，这里过滤掉纯进度行
        if msg.startswith("[download]  ") and "%" in msg.split("\r")[-1][:20]:
            return
        self._emit({"type": "log", "msg": msg})

    def info(self, msg: str) -> None:
        self._emit({"type": "log", "msg": msg})

    def warning(self, msg: str) -> None:
        self._emit({"type": "log", "msg": "[提示] " + msg})

    def error(self, msg: str) -> None:
        self._emit({"type": "log", "msg": "[错误] " + msg})


def format_size(num_bytes: Optional[float]) -> str:
    """字节数转可读字符串。"""
    if not num_bytes:
        return ""
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TB"


def format_eta(seconds: Optional[float]) -> str:
    """秒数转 mm:ss / hh:mm:ss。"""
    if seconds is None or seconds < 0:
        return ""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def classify_error(exc: Exception) -> tuple[str, str, bool]:
    """把 yt-dlp 抛出的异常翻译成 (标题, 建议, 是否可通过登录解决)。"""
    raw = str(exc)
    lowered = raw.lower()

    # 浏览器 cookies 读取失败：yt-dlp 的报错信息指向浏览器本身
    if any(key in lowered for key in _COOKIE_ERROR_KEYS) and (
            "cookie" in lowered or "browser" in lowered or "chrome" in lowered
            or "edge" in lowered or "firefox" in lowered):
        return ("读取浏览器 cookies 失败",
                "常见原因：新版 Chrome/Edge 加密了 cookies、或浏览器正在运行锁定了数据。\n"
                "建议：1) 关闭对应浏览器后重试；2) 换一个已登录的浏览器；"
                "3) 改用\"登录 X (Twitter)…\"等内置登录窗口。", False)

    for key, title, detail, needs_login in _ERROR_RULES:
        if key in lowered:
            return title, detail, needs_login

    return ("下载失败", raw.strip() or exc.__class__.__name__, False)


class _Hooks:
    """progress_hook 与 postprocessor_hook 的共享上下文。"""

    def __init__(self, emit: EventCallback, stop_event):
        self.emit = emit
        self.stop_event = stop_event
        self.finished_files: list[str] = []   # 下载完成的分片文件路径
        self._last_label = ""

    def check_cancel(self) -> None:
        if self.stop_event is not None and self.stop_event.is_set():
            raise yt_dlp.utils.DownloadCancelled()

    def progress(self, d: dict) -> None:
        self.check_cancel()
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            percent = (d.get("downloaded_bytes", 0) / total * 100.0) if total else None
            info = d.get("info_dict") or {}
            # 区分视频流/音频流，多文件下载时让用户知道当前在下什么
            label = self._stream_label(info)
            self.emit({
                "type": "progress",
                "percent": percent,
                "speed": format_size(d.get("speed")) + "/s" if d.get("speed") else "",
                "eta": format_eta(d.get("eta")),
                "label": label,
                "downloaded": format_size(d.get("downloaded_bytes")),
            })
        elif status == "finished":
            filename = d.get("filename") or ""
            if filename:
                self.finished_files.append(filename)
            self.emit({"type": "log", "msg": f"片段完成：{os.path.basename(filename)}"})

    def _stream_label(self, info: dict) -> str:
        vcodec = info.get("vcodec")
        if vcodec and vcodec != "none":
            label = "视频流"
        elif info.get("acodec") and info.get("acodec") != "none":
            label = "音频流"
        else:
            label = self._last_label or "下载中"
        self._last_label = label
        return label

    def postprocessor(self, d: dict) -> None:
        self.check_cancel()
        if d.get("status") != "started":
            return
        name = str(d.get("postprocessor") or "")
        if "Merger" in name:
            self.emit({"type": "state", "state": STATE_MERGING})
            self.emit({"type": "log", "msg": "正在合并视频和音频…"})
        elif "VideoRemuxer" in name or "VideoConvertor" in name:
            self.emit({"type": "log", "msg": "正在转换封装格式…"})


def run_download(opts: DownloadOptions, emit: EventCallback, stop_event=None) -> dict:
    """执行一次下载。返回 {'ok': bool, 'title': str, 'files': [str], ...}。

    本函数为阻塞调用，应在工作线程中运行；进度通过 emit 事件推送。
    """
    emit({"type": "state", "state": STATE_EXTRACTING})
    emit({"type": "log", "msg": f"正在解析：{opts.url}"})

    hooks = _Hooks(emit, stop_event)

    ydl_opts = {
        "logger": _YtLogger(emit),
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "outtmpl": os.path.join(opts.output_dir, "%(title).100B.%(ext)s"),
        "noplaylist": opts.noplaylist,
        "concurrent_fragment_downloads": 4,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
        "progress_hooks": [hooks.progress],
        "postprocessor_hooks": [hooks.postprocessor],
        # 中文标题、特殊字符等交给 yt-dlp 的跨平台净化；限制长度防止超 Windows 路径上限
        "windowsfilenames": True,
    }

    has_ffmpeg = bool(opts.ffmpeg_dir)
    if has_ffmpeg:
        ydl_opts["ffmpeg_location"] = opts.ffmpeg_dir
        ydl_opts["format"] = FORMAT_HIGH
        ydl_opts["merge_output_format"] = "mp4"
    else:
        # 没有 ffmpeg 时只能取单流（清晰度通常较低），且无法合并
        ydl_opts["format"] = FORMAT_FALLBACK
        emit({"type": "log", "msg": "[提示] 未找到 ffmpeg，本次仅下载单流（清晰度可能较低）"})

    if opts.cookies_file:
        ydl_opts["cookiefile"] = opts.cookies_file
    elif opts.browser:
        ydl_opts["cookiesfrombrowser"] = (opts.browser,)
    if opts.user_agent:
        ydl_opts["http_headers"] = {"User-Agent": opts.user_agent}
    if opts.proxy:
        ydl_opts["proxy"] = opts.proxy
        emit({"type": "log", "msg": f"使用代理：{opts.proxy}"})
    if opts.js_runtime_path:
        # 显式指定 deno 路径，不依赖 PATH（打包环境无全局 deno）
        ydl_opts["js_runtimes"] = {"deno": {"path": opts.js_runtime_path}}

    result = {"ok": False, "title": "", "files": [], "cancelled": False, "error": None}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(opts.url, download=True)
        result["ok"] = True
        result["title"] = (info or {}).get("title") or ""
        result["files"] = [f for f in hooks.finished_files if os.path.exists(f)]
        if stop_event is not None and stop_event.is_set():
            result["ok"] = False
            result["cancelled"] = True
    except yt_dlp.utils.DownloadCancelled:
        result["cancelled"] = True
        emit({"type": "state", "state": STATE_CANCELLED})
        return result
    except yt_dlp.utils.DownloadError as exc:
        title, detail, needs_login = classify_error(
            exc.exc_msg if hasattr(exc, "exc_msg") else exc)
        result["error"] = {"title": title, "detail": detail, "needs_login": needs_login}
        emit({"type": "error", "title": title, "detail": detail, "needs_login": needs_login})
        return result
    except Exception as exc:  # 兜底：任何未预期异常都不允许无声失败
        title, detail, needs_login = classify_error(exc)
        result["error"] = {"title": title, "detail": detail, "needs_login": needs_login}
        emit({"type": "error", "title": title, "detail": detail, "needs_login": needs_login})
        return result

    if result["ok"]:
        emit({"type": "state", "state": STATE_FINISHED})
    return result
