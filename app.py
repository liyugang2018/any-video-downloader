# -*- coding: utf-8 -*-
"""万能视频下载 - 图形界面入口。

用法：python app.py（开发）或直接运行打包后的 exe。
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

import downloader
import ffmpeg_manager
import login_browser
import proxy_manager

# --onefile --windowed 打包后 stdout/stderr 为 None，重定向到空设备防崩溃
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

APP_TITLE = "万能视频下载"
DEFAULT_DIR_NAME = "Videos"

# 登录方式下拉选项 -> 处理方式
COOKIE_AUTO = "自动（已登录站点自动使用）"
COOKIE_GUEST = "不使用（游客模式）"
COOKIE_TXT = "使用 cookies.txt 文件…"
LOGIN_TWITTER = "登录 X (Twitter)…"
LOGIN_BILI = "登录 B站…"
LOGIN_DOUYIN = "登录抖音…"
LOGIN_IXIGUA = "登录西瓜视频…"
LOGIN_WEIBO = "登录微博…"


class App:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("720x580")
        self.root.minsize(640, 520)

        self.events: queue.Queue = queue.Queue()
        self.task_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.ffmpeg_dir: Path | None = None
        self.cookies_file: str | None = None
        self.login_session: login_browser.LoginSession | None = None
        self.login_stop = threading.Event()
        self._pending_urls: list[str] | None = None  # 登录成功后待续传的剩余链接
        self._retried_sites: set[str] = set()        # 本批次已触发过登录重试的站点（防死循环）
        self._batch_pos = (0, 0)                     # 当前批次进度 (第几个, 共几个)

        self._build_ui()
        self._update_auto_hint()
        self._check_ffmpeg_async()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll()

    # ---------- UI 构建 ----------

    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 4}
        frame = ttk.Frame(self.root, padding=10)
        frame.pack(fill="both", expand=True)

        # 链接输入（多行，每行一个）
        ttk.Label(frame, text="视频链接：").grid(row=0, column=0, sticky="nw")
        url_wrap = ttk.Frame(frame)
        url_wrap.grid(row=0, column=1, columnspan=2, sticky="ew")
        self.url_text = tk.Text(url_wrap, height=4, wrap="char")
        self.url_text.pack(fill="x")
        self.url_text.bind("<Control-Return>", self._on_ctrl_return)
        ttk.Label(url_wrap, foreground="#6b7280",
                  text="每行一个链接，可一次粘贴多个；App 分享文案会自动提取链接（Ctrl+Enter 开始下载）"
                  ).pack(anchor="w")
        self.url_text.focus_set()

        # 保存目录
        ttk.Label(frame, text="保存到：").grid(row=1, column=0, sticky="w")
        self.dir_var = tk.StringVar(value=str(Path.home() / DEFAULT_DIR_NAME))
        ttk.Entry(frame, textvariable=self.dir_var).grid(row=1, column=1, sticky="ew", ipady=3)
        ttk.Button(frame, text="选择…", command=self.choose_dir).grid(row=1, column=2, sticky="e")

        # 登录方式
        ttk.Label(frame, text="登录方式：").grid(row=2, column=0, sticky="w")
        self.browser_var = tk.StringVar(value=COOKIE_AUTO)
        browser_values = [COOKIE_AUTO, COOKIE_GUEST, COOKIE_TXT,
                          LOGIN_TWITTER, LOGIN_BILI, LOGIN_DOUYIN,
                          LOGIN_IXIGUA, LOGIN_WEIBO] + list(downloader.BROWSERS.keys())
        self.browser_box = ttk.Combobox(
            frame, textvariable=self.browser_var, values=browser_values,
            state="readonly")
        self.browser_box.grid(row=2, column=1, sticky="ew")
        self.browser_box.bind("<<ComboboxSelected>>", self._on_browser_selected)
        self.cookie_hint = ttk.Label(
            frame, text="下载 X / 会员视频：选择\"登录 X (Twitter)…\"等项，在弹出的窗口中登录一次即可",
            foreground="#6b7280")
        self.cookie_hint.grid(row=3, column=1, columnspan=2, sticky="w")

        # 操作按钮
        btn_row = ttk.Frame(frame)
        btn_row.grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 4))
        self.download_btn = ttk.Button(btn_row, text="下 载", command=self.start_download)
        self.download_btn.pack(side="left")
        self.stop_btn = ttk.Button(btn_row, text="停 止", command=self.stop_download, state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))
        self.state_var = tk.StringVar(value="就绪")
        ttk.Label(btn_row, textvariable=self.state_var).pack(side="left", padx=(16, 0))

        # 进度
        self.progress = ttk.Progressbar(frame, maximum=100.0)
        self.progress.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(6, 2))
        self.progress_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.progress_var).grid(row=6, column=0, columnspan=3, sticky="w")

        # 日志
        self.log_text = tk.Text(frame, height=10, state="disabled", wrap="word",
                                font=("Consolas", 9), background="#f9fafb")
        self.log_text.grid(row=7, column=0, columnspan=3, sticky="nsew", pady=(8, 0))

        # 底部状态栏
        self.ffmpeg_var = tk.StringVar(value="正在检查 ffmpeg…")
        ttk.Label(self.root, textvariable=self.ffmpeg_var,
                  foreground="#6b7280", padding=(12, 4)).pack(side="bottom", fill="x")

        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(7, weight=1)

    # ---------- ffmpeg ----------

    def _check_ffmpeg_async(self) -> None:
        def worker() -> None:
            found = ffmpeg_manager.find_ffmpeg_dir()
            if found:  # 存下路径，下载时直接复用，不再重复获取
                self.ffmpeg_dir = found
            self.events.put(("ffmpeg_status", bool(found)))
            proxy = proxy_manager.detect_proxy()
            self.events.put(("proxy_status", proxy))
        threading.Thread(target=worker, daemon=True).start()

    def _ensure_ffmpeg(self, log) -> None:
        """下载线程开头调用：确保 ffmpeg 可用（没有则自动下载）。"""
        if self.ffmpeg_dir is None:
            self.ffmpeg_dir = ffmpeg_manager.download_ffmpeg(log)
            self.events.put(("ffmpeg_status", self.ffmpeg_dir is not None))

    # ---------- 交互 ----------

    def choose_dir(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.dir_var.get() or ".")
        if chosen:
            self.dir_var.set(chosen)

    def _on_browser_selected(self, _event=None) -> None:
        choice = self.browser_var.get()
        if choice == COOKIE_TXT:
            path = filedialog.askopenfilename(
                title="选择 cookies.txt 文件",
                filetypes=[("cookies 文件", "*.txt"), ("所有文件", "*.*")])
            if path:
                self.cookies_file = path
                self.cookie_hint.configure(text=f"cookies 文件：{path}", foreground="#16a34a")
            else:
                self.browser_var.set(COOKIE_AUTO)
        elif choice in (LOGIN_TWITTER, LOGIN_BILI, LOGIN_DOUYIN,
                        LOGIN_IXIGUA, LOGIN_WEIBO):
            # 动作项：触发后下拉恢复默认，由弹出的登录窗口接管
            self.browser_var.set(COOKIE_AUTO)
            site_map = {
                LOGIN_TWITTER: "twitter", LOGIN_BILI: "bilibili",
                LOGIN_DOUYIN: "douyin", LOGIN_IXIGUA: "ixigua",
                LOGIN_WEIBO: "weibo",
            }
            self._start_login(site_map[choice])
        elif choice == COOKIE_AUTO:
            self.cookie_hint.configure(
                text="已保存登录信息的站点会自动使用其登录状态", foreground="#6b7280")

    # ---------- 站点登录向导 ----------

    def _start_login(self, site_key: str) -> None:
        if self.login_session is not None:
            messagebox.showinfo(APP_TITLE, "已有一个登录窗口在进行中，请先完成或关闭它。")
            return
        if self.task_thread is not None and self.task_thread.is_alive():
            messagebox.showinfo(APP_TITLE, "正在下载中，请等下载结束后再登录。")
            return
        self.login_stop.clear()
        self.login_session = login_browser.LoginSession(
            site_key, lambda msg: self.events.put(("login_log", msg)))
        self.download_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal", text="完成登录")
        self._set_state("等待登录…")

        def worker() -> None:
            ok = self.login_session.run(self.login_stop)
            self.events.put(("login_done", ok))
        threading.Thread(target=worker, daemon=True).start()

    def _finish_login_ui(self, ok: bool) -> None:
        self.login_session = None
        self.download_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled", text="停 止")
        self._set_state("登录完成，可以下载了" if ok else "就绪")
        self._update_auto_hint()
        # 登录成功且批次被中断：自动从断点继续剩余链接
        if ok and self._pending_urls:
            urls, self._pending_urls = self._pending_urls, None
            self._append_log("检测到登录成功，自动继续下载剩余链接…")
            self.root.after(800, lambda: self._start_batch(urls))

    def _update_auto_hint(self) -> None:
        """自动模式下，提示哪些站点已有登录存档。"""
        if self.browser_var.get() != COOKIE_AUTO:
            return
        saved = [cfg["name"] for key, cfg in login_browser.SITE_CONFIGS.items()
                 if login_browser.site_cookie_file(key)]
        text = ("已保存登录：" + "、".join(saved)) if saved else \
            "下载 X / 会员视频：选择\"登录 X (Twitter)…\"等项，在弹出的窗口中登录一次即可"
        self.cookie_hint.configure(text=text, foreground="#16a34a" if saved else "#6b7280")

    def _on_ctrl_return(self, _event=None) -> str:
        """Ctrl+Enter 开始下载（普通 Enter 用于换行，输入多个链接）。"""
        self.start_download()
        return "break"  # 阻止再插入一个换行

    def start_download(self) -> None:
        urls, dropped = downloader.normalize_urls(self.url_text.get("1.0", "end"))
        for line in dropped:
            self._append_log(f"已忽略无法识别的行：{line}")
        if not urls:
            messagebox.showinfo(APP_TITLE, "请先粘贴视频链接（每行一个，可一次粘贴多个）。")
            return
        # 回显规范化后的链接（如抖音弹窗链接 -> 视频直链、分享文案 -> 短链）
        self.url_text.delete("1.0", "end")
        self.url_text.insert("1.0", "\n".join(urls))
        out_dir = self.dir_var.get().strip()
        try:
            Path(out_dir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"保存目录无法创建：\n{exc}")
            return
        # 新批次：清空登录重试记录；记录批次内容，登录成功后从断点续传
        self._retried_sites = set()
        self._pending_urls = urls
        if len(urls) > 1:
            self._append_log(f"开始批量下载，共 {len(urls)} 个链接（顺序下载）")
        self._start_batch(urls)

    def _start_batch(self, urls: list[str]) -> None:
        """启动下载线程（首个批次与登录后续传共用）。"""
        ui = {  # 主线程一次性快照，避免工作线程读取 tk 变量
            "choice": self.browser_var.get(),
            "out_dir": self.dir_var.get().strip(),
            "cookies_txt": self.cookies_file,
        }
        self.stop_event.clear()
        self.download_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress.configure(value=0)
        self.progress_var.set("")
        self._set_state("正在准备…")
        self.task_thread = threading.Thread(
            target=self._worker, args=(urls, ui), daemon=True)
        self.task_thread.start()

    def stop_download(self) -> None:
        if self.login_session is not None:
            # 登录进行中：此按钮是"完成登录"，保存当前 cookies
            if not self.login_session.save_now():
                return
            self.login_stop.set()
            self._set_state("已保存登录信息")
            return
        self.stop_event.set()
        self._set_state("正在停止…")

    def _build_opts(self, url: str, ui: dict, emit) -> downloader.DownloadOptions:
        """按单条 URL 组装下载参数（批量时各链接站点不同，登录/代理需逐条匹配）。"""
        choice = ui["choice"]
        browser = downloader.BROWSERS.get(choice)
        cookies_file = ui["cookies_txt"] if choice == COOKIE_TXT else None
        user_agent = None
        if choice == COOKIE_AUTO:
            # 自动模式：按 URL 匹配已保存登录的站点（cookies + 配套 UA）
            site_key = login_browser.match_site_for_url(url)
            saved = login_browser.site_cookie_file(site_key) if site_key else None
            if saved:
                cookies_file = str(saved)
                user_agent = login_browser.site_user_agent(site_key)
                emit({"type": "log", "msg": f"自动使用已保存的 {site_key} 登录状态"})
        elif choice == COOKIE_GUEST:
            cookies_file = None
        return downloader.DownloadOptions(
            url=url, output_dir=ui["out_dir"], browser=browser,
            cookies_file=cookies_file,
            ffmpeg_dir=str(self.ffmpeg_dir) if self.ffmpeg_dir else None,
            user_agent=user_agent, proxy=proxy_manager.proxy_for(url))

    def _worker(self, urls: list[str], ui: dict) -> None:
        def emit(event: dict) -> None:
            self.events.put(("event", event))

        self._ensure_ffmpeg(lambda msg: emit({"type": "log", "msg": msg}))
        total = len(urls)
        ok = fail = not_started = 0
        for i, url in enumerate(urls, 1):
            if self.stop_event.is_set():
                not_started = total - i + 1
                break
            self._batch_pos = (i, total)
            self.events.put(("batch_pos", (i, total)))
            opts = self._build_opts(url, ui, emit)
            result = downloader.run_download(opts, emit, self.stop_event)
            if result.get("cancelled"):
                not_started = total - i + 1
                break
            if result.get("ok"):
                ok += 1
                continue
            error = result.get("error") or {}
            if error.get("needs_login"):
                site_key = login_browser.match_site_for_url(url)
                if site_key and site_key not in self._retried_sites:
                    # 方案B：中断批次 -> 打开登录窗 -> 登录成功后从当前条自动续传
                    self._retried_sites.add(site_key)
                    self.task_thread = None  # 让 _start_login 的"下载中"检查放行
                    self.events.put(("batch_needs_login", (site_key, urls[i - 1:])))
                    return
                emit({"type": "log",
                      "msg": "[提示] 该视频仍需要登录（登录未能解决），已跳过"})
            fail += 1
            emit({"type": "log", "msg": f"[失败] {error.get('title', '未知错误')}"})
        self.events.put(("batch_done", {
            "ok": ok, "fail": fail, "not_started": not_started,
            "cancelled": self.stop_event.is_set()}))

    # ---------- 事件轮询 ----------

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "event":
                    self._handle_event(payload)
                elif kind == "ffmpeg_status":
                    self._handle_ffmpeg_status(payload)
                elif kind == "batch_pos":
                    self._handle_batch_pos(payload)
                elif kind == "batch_needs_login":
                    self._handle_batch_needs_login(payload)
                elif kind == "batch_done":
                    self._handle_batch_done(payload)
                elif kind == "login_log":
                    self._append_log(payload)
                elif kind == "login_done":
                    self._finish_login_ui(payload)
                elif kind == "proxy_status":
                    self._handle_proxy_status(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _handle_ffmpeg_status(self, ok: bool) -> None:
        self._ffmpeg_ok = ok
        self._refresh_statusbar()

    def _handle_proxy_status(self, proxy: str | None) -> None:
        self._proxy = proxy
        self._refresh_statusbar()

    def _refresh_statusbar(self) -> None:
        ff = getattr(self, "_ffmpeg_ok", None)
        parts = []
        if ff is None:
            parts.append("ffmpeg：检查中…")
        elif ff:
            parts.append("ffmpeg：已就绪")
        else:
            parts.append("ffmpeg：未安装（首次下载时自动获取）")
        if hasattr(self, "_proxy"):
            parts.append(f"代理：{self._proxy or '未检测到（下载 X 等站点需要）'}")
        self.ffmpeg_var.set("    |    ".join(parts))

    def _handle_event(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "log":
            self._append_log(event.get("msg", ""))
        elif etype == "progress":
            parts = []
            if event.get("label"):
                parts.append(event["label"])
            if event.get("percent") is not None:
                self.progress.configure(value=event["percent"])
                parts.append(f"{event['percent']:.1f}%")
            if event.get("speed"):
                parts.append(event["speed"])
            if event.get("eta"):
                parts.append("剩余 " + event["eta"])
            self.progress_var.set("   ".join(parts))
        elif etype == "state":
            state = event.get("state")
            labels = {
                downloader.STATE_EXTRACTING: "正在解析链接…",
                downloader.STATE_DOWNLOADING: "正在下载…",
                downloader.STATE_MERGING: "正在合并音视频…",
                downloader.STATE_FINISHED: "下载完成",
                downloader.STATE_CANCELLED: "已停止",
            }
            label = labels.get(state, state or "")
            i, n = self._batch_pos
            if n > 1:  # 批量时状态栏带进度计数
                label = f"第 {i}/{n} 个，{label}"
            self._set_state(label)
            if state == downloader.STATE_DOWNLOADING and not self.progress_var.get():
                self.progress_var.set("下载中…")
        elif etype == "error":
            self._set_state(f"失败：{event.get('title', '未知错误')}")
            self._append_log(f"[错误] {event.get('title', '')}\n{event.get('detail', '')}")

    def _handle_batch_pos(self, pos: tuple[int, int]) -> None:
        """新的一条开始：重置进度条，日志标记序号。"""
        i, total = pos
        self.progress.configure(value=0)
        self.progress_var.set("")
        if total > 1:
            self._append_log(f"── 第 {i}/{total} 个 ──")

    def _handle_batch_needs_login(self, payload: tuple[str, list[str]]) -> None:
        """批量中遇到"需要登录"：中断批次，打开登录窗，登录成功后续传剩余链接。"""
        site_key, remaining = payload
        self._pending_urls = remaining
        self._append_log(f"该视频需要登录，正在打开登录窗口，"
                         f"登录后将自动继续剩余 {len(remaining)} 个链接。")
        self._start_login(site_key)

    def _handle_batch_done(self, summary: dict) -> None:
        self.download_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        ok, fail, ns = summary["ok"], summary["fail"], summary["not_started"]
        if summary.get("cancelled"):
            text = f"已停止：成功 {ok}，失败 {fail}，未开始 {ns}"
        elif fail == 0 and ns == 0:
            text = "下载完成" if ok == 1 else f"全部完成（{ok} 个）"
        elif ok == 0 and ns == 0:
            text = "下载失败"
        else:
            text = f"批量结束：成功 {ok}，失败 {fail}" + (f"，未开始 {ns}" if ns else "")
        self._set_state(text)
        self._append_log(text)
        self.root.after(1200, lambda: self.progress.configure(value=0))

    # ---------- 辅助 ----------

    def _set_state(self, text: str) -> None:
        self.state_var.set(text)

    def _append_log(self, msg: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_close(self) -> None:
        if self.task_thread is not None and self.task_thread.is_alive():
            if not messagebox.askyesno(
                    APP_TITLE, "正在下载中，关闭窗口将中止下载。\n确定要关闭吗？"):
                return
            self.stop_event.set()
        if self.login_session is not None:
            self.login_stop.set()
            self.login_session.close_browser()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def _selftest() -> None:
    """打包自检：验证 exe 内 yt_dlp 及各站点解析器是否完整。

    结果写入 exe 同目录的 selftest_result.txt（--windowed 模式没有控制台输出）。
    """
    lines = []
    try:
        import yt_dlp
        from yt_dlp.extractor import gen_extractor_classes
        classes = gen_extractor_classes()
        names = {c.IE_NAME.lower() for c in classes}
        lines.append(f"yt_dlp 版本: {yt_dlp.version.__version__}")
        lines.append(f"解析器数量: {len(classes)}")
        for key in ("bilibili", "twitter", "youtube"):
            lines.append(f"  {key}: {'存在' if key in names else '缺失'}")
        lines.append("SELFTEST OK" if len(classes) > 1000 else "SELFTEST WARN: 解析器数量异常")
    except Exception as exc:
        lines.append(f"SELFTEST FAILED: {exc!r}")
    out = Path(sys.argv[0]).with_name("selftest_result.txt")
    out.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    if "--selftest" in sys.argv:
        _selftest()
        return
    App().run()


if __name__ == "__main__":
    main()
