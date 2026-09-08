# 万能视频下载

粘贴视频链接，自动下载视频。基于 [yt-dlp](https://github.com/yt-dlp/yt-dlp)，
支持上千个站点。

## 国内站点支持情况（2026-09 实测）

| 站点 | 状态 | 说明 |
|------|------|------|
| B站 | 直接可用 | 免登录可下 480P；登录后更高清 |
| 微博 / AcFun | 直接可用 | 游客即可下载 |
| 西瓜视频 | 需验证 | 首次下载自动弹窗获取验证信息（不必登录）|
| 抖音 | 需登录 | 首次下载自动弹登录窗口，扫码登录一次即可；直接粘贴 App 分享文案也行 |
| 知乎 | 部分支持 | 部分视频可下，视具体内容而定 |
| 快手 | 不支持 | yt-dlp 官方已移除其解析器 |
| 小红书 | 不支持 | 无公开解析途径 |

> 海外站（X/Twitter、YouTube、TikTok 等）需开启代理软件，程序自动探测常见端口。
> 其他 yt-dlp 支持的站点（[完整列表](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)）同样可下。

## 使用方法

双击 `dist\万能视频下载.exe`（无需安装 Python），粘贴视频链接，点"下载"。

- **保存位置**：默认 `我的文档\Videos`，可自行更换
- **登录方式**：默认"自动"。下载需要登录的内容（X 视频、B站高清/会员）时，
  程序会自动弹出登录窗口——在窗口里登录一次，之后所有下载自动携带登录状态。
  也可以在"登录方式"下拉里手动触发"登录 X (Twitter)…"、"登录 B站…"
- **内置登录窗口原理**：程序用系统 Chrome/Edge 内核打开一个独立登录窗口，
  登录信息保存在程序自己的目录（不读取日常浏览器的加密 cookies，
  不受新版 Chrome App-Bound Encryption 影响）
- **ffmpeg**：合并高清音视频所需。首次下载时自动获取（约 160MB，只需一次，
  存放在 `%LOCALAPPDATA%\VideoDownloader\ffmpeg`），之后所有下载均为高清

## 常见问题

| 问题 | 说明 |
|------|------|
| X（Twitter）/ YouTube 下载需要什么 | 两件事：1) **开代理软件**（程序自动探测 Clash 7890 / v2rayN 10809 等常见端口，无需配置）；2) 部分推文需要登录（会自动弹登录窗口）。公开推文开代理即可直接下载 |
| 提示"无法连接到该网站" | 被墙站点直连被阻断。开启代理软件后重试；若代理端口不在常见列表，请在代理软件里开启"系统代理"或改为常见端口 |
| 弹出登录窗口 | 这是正常流程：视频需要登录才能下载，在弹出的窗口里登录，登录成功后程序自动继续下载 |
| B站只有 480P | 未登录状态的上限。用"登录 B站…"弹窗登录后可拿更高清晰度（会员清晰度需大会员账号） |
| 提示"请求过于频繁" | 站点风控（短时间内请求过多），等几分钟再试 |
| 提示"解析失败（站点改版）" | 视频网站更新了反爬机制，需要升级 yt-dlp 后重新打包（见下方开发说明） |
| exe 被杀毒软件误报 | PyInstaller 单文件 exe 的常见误报。添加信任即可；介意的话可改用 `--onedir` 模式打包 |
| 下载速度慢 | 程序默认 4 线程下载分片；网络原因的慢速无法从程序侧解决 |

## 开发说明

环境：Python 3.10+

```bash
python -m venv venv
venv\Scripts\python -m pip install yt-dlp pyinstaller

# 运行界面
venv\Scripts\python app.py

# 重新打包（修改代码后）
build.bat

# 打包自检（验证 exe 内 yt-dlp 完整性，结果写入 selftest_result.txt）
dist\VideoDownloader.exe --selftest
```

### 文件结构

| 文件 | 职责 |
|------|------|
| `app.py` | GUI 入口（tkinter）：输入、进度、日志、线程调度、登录向导交互 |
| `downloader.py` | 核心下载：封装 yt-dlp，进度回调与错误翻译（含"登录可解决"判定） |
| `login_browser.py` | 站点登录向导：CDP 弹窗登录、cookies 存档、UA 记录 |
| `proxy_manager.py` | 本地代理探测（被墙站点自动走 Clash/v2rayN 等常见端口） |
| `ffmpeg_manager.py` | ffmpeg 检测与自动下载（npmmirror / gyan.dev / GitHub 三源容错） |
| `build.bat` | 一键重新打包 |
