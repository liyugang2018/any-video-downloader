# 截取程序主窗口截图 -> docs/screenshot.png
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win {
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    public struct RECT { public int Left, Top, Right, Bottom; }
}
"@
[Win]::SetProcessDPIAware() | Out-Null

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
New-Item -ItemType Directory -Force -Path "$root\docs" | Out-Null

# pythonw 启动避免多余控制台窗口
$proc = Start-Process -FilePath "$root\venv\Scripts\pythonw.exe" -ArgumentList "app.py", "--demo" -PassThru

try {
    $launchTime = (Get-Date).AddSeconds(-2)
    $h = [IntPtr]::Zero
    $winProc = $null
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 250
        if ($proc.HasExited) { throw "app.py --demo exited early" }
        # venv 的 pythonw 是启动器，Tk 窗口属于其子进程：按启动时间找新起的 pythonw
        $cand = Get-Process pythonw -ErrorAction SilentlyContinue | Where-Object {
            $_.MainWindowHandle -ne 0 -and $_.StartTime -gt $launchTime
        }
        if ($cand) { $winProc = @($cand)[0]; $h = $winProc.MainWindowHandle; break }
    }
    if ($h -eq [IntPtr]::Zero) { throw "window not found" }
    Start-Sleep -Milliseconds 1500   # 等 ffmpeg 检查/界面稳定

    $r = New-Object Win+RECT
    [Win]::GetWindowRect($h, [ref]$r) | Out-Null
    $w = $r.Right - $r.Left
    $ht = $r.Bottom - $r.Top
    if ($w -le 0 -or $ht -le 0) { throw "bad window rect: $w x $ht" }

    $bmp = New-Object System.Drawing.Bitmap $w, $ht
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen($r.Left, $r.Top, 0, 0, $bmp.Size)
    $bmp.Save("$root\docs\screenshot.png", [System.Drawing.Imaging.ImageFormat]::Png)
    $g.Dispose(); $bmp.Dispose()
    Write-Output "SCREENSHOT_OK ${w}x${ht}"
} finally {
    if ($winProc -and -not $winProc.HasExited) { Stop-Process -Id $winProc.Id -Force }
    if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
}
