# -*- coding: utf-8 -*-
"""ffmpeg 自动获取：检测本机 ffmpeg，缺失时从镜像源下载到用户数据目录。

查找顺序：
  1. exe 同目录下的 ffmpeg\            （便于高级用户手动放置）
  2. %LOCALAPPDATA%\VideoDownloader\ffmpeg\
  3. 系统 PATH

下载源按顺序尝试：
  1. npmmirror（国内镜像，两个独立 exe 直链）
  2. gyan.dev 官方 essentials zip
  3. BtbN GitHub 构建 zip
"""
from __future__ import annotations

import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

NPMMIRROR_BASE = "https://registry.npmmirror.com/-/binary/ffmpeg-static/b6.0"

DOWNLOAD_SOURCES: list[dict] = [
    {
        "kind": "direct",
        "name": "npmmirror.com（国内镜像）",
        "files": [
            (f"{NPMMIRROR_BASE}/ffmpeg-win32-x64", "ffmpeg.exe"),
            (f"{NPMMIRROR_BASE}/ffprobe-win32-x64", "ffprobe.exe"),
        ],
    },
    {
        "kind": "zip",
        "name": "gyan.dev",
        "url": "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    },
    {
        "kind": "zip",
        "name": "github.com",
        "url": "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip",
    },
]

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


def _has_ffmpeg(directory: Path) -> bool:
    return (directory / "ffmpeg.exe").is_file() and (directory / "ffprobe.exe").is_file()


def find_ffmpeg_dir() -> Optional[Path]:
    """按顺序查找可用的 ffmpeg 目录，找不到返回 None。"""
    for cand in (_app_dir() / "ffmpeg", _data_dir() / "ffmpeg"):
        if _has_ffmpeg(cand):
            return cand
    which = shutil.which("ffmpeg")
    if which and shutil.which("ffprobe"):
        return Path(which).parent
    return None


def _download_to(url: str, dest: Path, log: Callable[[str], None]) -> None:
    """下载单个文件到 dest（先写 .part 再改名）。"""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    part = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=_READ_TIMEOUT) as resp:
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
                    if pct >= last_pct + 5:  # 每 5% 报告一次，避免刷屏
                        last_pct = pct
                        log(f"{dest.name} 下载进度：{pct}%（{done // 1048576}MB / {total // 1048576}MB）")
    part.replace(dest)


def _extract_binaries(zip_path: Path, dest_dir: Path) -> None:
    """从官方 zip 中提取 ffmpeg.exe / ffprobe.exe 到 dest_dir。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    wanted = {"ffmpeg.exe", "ffprobe.exe"}
    found: dict[str, str] = {}
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            base = os.path.basename(name).lower()
            if base in wanted and base not in found:
                found[base] = name
    missing = wanted - set(found)
    if missing:
        raise RuntimeError(f"压缩包中缺少文件：{', '.join(missing)}")
    for base, name in found.items():
        with zf.open(name) as src, open(dest_dir / base, "wb") as dst:
            shutil.copyfileobj(src, dst)


def _cleanup_partial(target_dir: Path) -> None:
    """移除下载失败留下的半成品。"""
    if target_dir.is_dir():
        for p in target_dir.iterdir():
            p.unlink(missing_ok=True)


def download_ffmpeg(log: Callable[[str], None]) -> Optional[Path]:
    """获取 ffmpeg：已有（手动放置/此前下载过/PATH）直接复用，否则下载。

    所有来源均失败时返回 None。
    """
    # 关键：先全量复用检查（exe 同目录 > 数据目录 > PATH），避免重复下载
    existing = find_ffmpeg_dir()
    if existing:
        return existing

    target_dir = _data_dir() / "ffmpeg"
    zip_path = _data_dir() / "ffmpeg-download.zip"
    _data_dir().mkdir(parents=True, exist_ok=True)
    # 清理上次中断下载留下的半成品
    if target_dir.is_dir():
        for p in target_dir.glob("*.part"):
            p.unlink(missing_ok=True)

    errors = []
    for source in DOWNLOAD_SOURCES:
        try:
            log(f"正在从 {source['name']} 下载 ffmpeg（约 80MB x2，只需一次）…")
            if source["kind"] == "direct":
                target_dir.mkdir(parents=True, exist_ok=True)
                for url, filename in source["files"]:
                    _download_to(url, target_dir / filename, log)
            else:
                _download_to(source["url"], zip_path, log)
                log("正在解压 ffmpeg…")
                _extract_binaries(zip_path, target_dir)
            if _has_ffmpeg(target_dir):
                zip_path.unlink(missing_ok=True)
                log("ffmpeg 安装完成。")
                return target_dir
            raise RuntimeError("下载完成但未找到 ffmpeg.exe / ffprobe.exe")
        except Exception as exc:  # 单个源失败不终止，尝试下一个源
            errors.append(f"{source['name']} -> {exc}")
            log(f"该下载源失败：{exc}")
            _cleanup_partial(target_dir)
            zip_path.unlink(missing_ok=True)

    log("所有下载源均失败。你也可以手动获取 ffmpeg.exe 和 ffprobe.exe，"
        "放到程序目录下的 ffmpeg 文件夹中后重试。")
    for e in errors:
        log(f"  失败详情：{e}")
    return None
