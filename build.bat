@echo off
rem 重新打包脚本：修改代码后运行本脚本即可重新生成 exe
chcp 65001 >nul
cd /d "%~dp0"

if not exist venv\Scripts\pyinstaller.exe (
    echo [错误] 未找到 venv，请先执行:
    echo    python -m venv venv
    echo    venv\Scripts\python -m pip install yt-dlp pyinstaller
    pause
    exit /b 1
)

rem --collect-all yt_dlp_ejs: YouTube JS 挑战求解脚本（yt-dlp 官方伴生包，版本需与 yt-dlp 配套）
venv\Scripts\pyinstaller.exe --noconfirm --onefile --windowed --collect-all yt_dlp --collect-all yt_dlp_ejs --name VideoDownloader app.py
if errorlevel 1 (
    echo [错误] 打包失败
    pause
    exit /b 1
)

copy /y dist\VideoDownloader.exe "dist\万能视频下载.exe" >nul
echo.
echo 打包完成: dist\万能视频下载.exe
pause
