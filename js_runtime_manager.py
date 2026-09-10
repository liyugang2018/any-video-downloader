# -*- coding: utf-8 -*-
"""deno（JS 运行时）自动获取：yt-dlp 解析 YouTube 需要 JS 运行时解签名挑战。

yt-dlp 官方已弃用无 JS 运行时的 YouTube 提取（部分格式会缺失），
支持的运行时中默认仅启用 deno（要求 >= 2.3.0）。

查找顺序：
  1. exe 同目录下的 deno\deno.exe     （便于高级用户手动放置）
  2. %LOCALAPPDATA%\VideoDownloader\deno\deno.exe
  3. 系统 PATH

下载源按顺序尝试（约 41MB，只需一次）：
  1. npmmirror（国内镜像）
  2. GitHub 官方 release（走用户代理）
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

# 固定版本而非 latest：下载行为可预期，升级时改这一处并重新验证
DENO_VERSION = "v2.9.6"
DENO_ZIP_NAME = "deno-x86_64-pc-windows-msvc.zip"

DOWNLOAD_SOURCES: list[dict] = [
    {
        "name": "npmmirror.com（国内镜像）",
        "url": f"https://registry.npmmirror.com/-/binary/deno/{DENO_VERSION}/{DENO_ZIP_NAME}",
        "proxy": False,   # 国内源直连
    },
    {
        "name": "github.com",
        "url": f"https://github.com/denoland/deno/releases/download/{DENO_VERSION}/{DENO_ZIP_NAME}",
        "proxy": True,    # GitHub 需走代理
    },
]

_MIN_VERSION = (2, 3, 0)      # yt-dlp DenoJsRuntime 的最低要求
_READ_TIMEOUT = 120
_CHUNK_SIZE = 256 * 1024


def _app_dir() -> Path:
    """exe 同目录（打包后）或脚本目录（开发时）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def _data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "VideoDownloader"


def _version_tuple(deno_exe: Path) -> Optional[tuple[int, ...]]:
    """运行 deno --version 解析版本，失败返回 None。"""
    try:
        proc = subprocess.run(
            [str(deno_exe), "--version"], capture_output=True, text=True,
            timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in (proc.stdout or "").splitlines():
        if line.lower().startswith("deno "):
            parts = line.split()[1].split(".")
            try:
                return tuple(int(p.split("-")[0]) for p in parts)
            except ValueError:
                return None
    return None


def find_deno() -> Optional[Path]:
    """查找可用的 deno.exe（含版本校验），找不到或版本过低返回 None。"""
    for cand in (_app_dir() / "deno" / "deno.exe", _data_dir() / "deno" / "deno.exe"):
        if cand.is_file():
            vt = _version_tuple(cand)
            if vt and vt >= _MIN_VERSION:
                return cand
            return None    # 版本过低：视为不可用，走重新下载
    which = shutil.which("deno")
    if which:
        p = Path(which)
        vt = _version_tuple(p)
        if vt and vt >= _MIN_VERSION:
            return p
    return None


def _download_to(url: str, dest: Path, log: Callable[[str], None],
                 proxy: Optional[str] = None) -> None:
    """下载单个文件到 dest（先写 .part 再改名）。与 ffmpeg_manager 同模式。"""
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler(
            {"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    part = dest.with_suffix(dest.suffix + ".part")
    with opener.open(req, timeout=_READ_TIMEOUT) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        last_pct = -1
        with open(part, "wb") as f:
            while True:
                chunk = resp.read(_CHUNK_SIZE)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 // total
                    if pct >= last_pct + 5:
                        last_pct = pct
                        log(f"deno 下载进度：{pct}%（{done // 1048576}MB / {total // 1048576}MB）")
    part.replace(dest)


def download_deno(log: Callable[[str], None],
                  proxy: Optional[str] = None) -> Optional[Path]:
    """获取 deno：已有（手动放置/此前下载过/PATH）直接复用，否则下载。

    所有来源均失败时返回 None。
    """
    existing = find_deno()
    if existing:
        return existing

    _data_dir().mkdir(parents=True, exist_ok=True)
    zip_path = _data_dir() / "deno-download.zip"
    target_dir = _data_dir() / "deno"
    target_dir.mkdir(parents=True, exist_ok=True)
    # 清理上次中断下载留下的半成品
    if target_dir.is_dir():
        for p in target_dir.glob("*.part"):
            p.unlink(missing_ok=True)

    for source in DOWNLOAD_SOURCES:
        try:
            log(f"正在从 {source['name']} 下载 deno（约 41MB，只需一次，YouTube 解析需要）…")
            use_proxy = proxy if source["proxy"] else None
            _download_to(source["url"], zip_path, log, proxy=use_proxy)
            log("正在解压 deno…")
            with zipfile.ZipFile(zip_path) as zf:
                names = [n for n in zf.namelist()
                         if os.path.basename(n).lower() == "deno.exe"]
                if not names:
                    raise RuntimeError("压缩包中没有 deno.exe")
                with zf.open(names[0]) as src, \
                        open(target_dir / "deno.exe", "wb") as dst:
                    shutil.copyfileobj(src, dst)
            vt = _version_tuple(target_dir / "deno.exe")
            if not vt or vt < _MIN_VERSION:
                raise RuntimeError(f"deno 版本校验失败：{vt}")
            zip_path.unlink(missing_ok=True)
            log("deno 安装完成。")
            return target_dir / "deno.exe"
        except Exception as exc:  # 单个源失败不终止，尝试下一个源
            log(f"该下载源失败：{exc}")
            zip_path.unlink(missing_ok=True)
            (target_dir / "deno.exe").unlink(missing_ok=True)

    log("deno 下载失败。本次将按无 JS 运行时模式解析 YouTube（部分清晰度可能缺失）。")
    return None
