# VideoFactory - one-command setup for Windows 10/11 (not yet tested on a real Windows PC).
#
#   powershell -ExecutionPolicy Bypass -File setup\windows-setup.ps1     (run from the VideoFactory folder)
#
# Safe to run again: every step checks first and skips what is already done.
# It never uploads or prints your keys or password.

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
$Dir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Dir

function Say($t)  { Write-Host ""; Write-Host "== $t" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "   OK: $t" -ForegroundColor Green }
function Fail($t) { Write-Host ""; Write-Host "   PROBLEM: $t" -ForegroundColor Red; Write-Host "   Copy this screen and send it to your AI co-founder."; exit 1 }
function HasKey($k) { (Test-Path ".env") -and ((Get-Content ".env") -match "^$k=.") }

Say "VideoFactory setup in: $Dir"
if (-not (Test-Path "$Dir\app")) { Fail "This does not look like the VideoFactory folder (no 'app' folder here)." }

# ------------------------------------------------------------------ 1. Python
Say "1/7  Python 3.13"
$py = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3.13 --version *> $null
    if ($LASTEXITCODE -eq 0) { $py = "py" }
}
if (-not $py) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Fail "winget is not available. Install Python 3.13 from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH'), then run this script again." }
    Write-Host "   Installing Python 3.13 with winget..."
    winget install --id Python.Python.3.13 -e --accept-package-agreements --accept-source-agreements
    Fail "Python was just installed (or the install needs attention). Close this window, open a NEW PowerShell window, and run this script again."
}
Ok (& py -3.13 --version)

# ------------------------------------------------------------------ 2. FFmpeg
Say "2/7  FFmpeg and FFprobe (video building, with caption support)"
New-Item -ItemType Directory -Force "$Dir\bin" | Out-Null
function FFmpegReady { (Test-Path "$Dir\bin\ffmpeg.exe") -and (Test-Path "$Dir\bin\ffprobe.exe") -and ((& "$Dir\bin\ffmpeg.exe" -hide_banner -filters 2>$null | Out-String) -match " subtitles ") }
if (FFmpegReady) { Ok "already installed" } else {
    foreach ($url in @("https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
                       "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip")) {
        try {
            Write-Host "   Downloading $url"
            Invoke-WebRequest -Uri $url -OutFile "$env:TEMP\vf-ffmpeg.zip" -ErrorAction Stop
            if (Test-Path "$env:TEMP\vf-ffmpeg") { Remove-Item "$env:TEMP\vf-ffmpeg" -Recurse -Force }
            Expand-Archive -Path "$env:TEMP\vf-ffmpeg.zip" -DestinationPath "$env:TEMP\vf-ffmpeg" -Force -ErrorAction Stop
            Get-ChildItem "$env:TEMP\vf-ffmpeg" -Recurse -Include ffmpeg.exe, ffprobe.exe | Copy-Item -Destination "$Dir\bin" -Force
            if (FFmpegReady) { break }
        } catch { Write-Host "   That download did not work, trying the next one..." }
    }
    if (-not (FFmpegReady)) { Fail "Could not install an FFmpeg that can draw captions." }
    Ok "installed"
}

# ------------------------------------------------------------------ 3. Toolbox
Say "3/7  Private Python toolbox and add-ons"
if (-not (Test-Path "$Dir\venv\Scripts\python.exe")) { & py -3.13 -m venv venv; if ($LASTEXITCODE -ne 0) { Fail "Could not create the toolbox." } }
& "$Dir\venv\Scripts\python.exe" -m pip install --quiet --upgrade pip 2>&1 | Out-Null
& "$Dir\venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { Fail "Could not install the add-ons (check the internet connection)." }
Ok "ready"

# ------------------------------------------------------------------ 4. Keys
Say "4/7  Free AI key (Google Gemini)"
if (HasKey "GEMINI_API_KEY") { Ok "already saved" } else {
    Write-Host "   Create a free key at https://aistudio.google.com/apikey (no billing), copy it, then paste it here."
    & "$Dir\venv\Scripts\python.exe" -m app.set_key GEMINI_API_KEY
    if ($LASTEXITCODE -ne 0) { Fail "The Gemini key was not saved." }
}
Say "     Optional: free Pixabay key for more pictures"
if (HasKey "PIXABAY_API_KEY") { Ok "already saved" } else {
    $answer = Read-Host "   Do you have a Pixabay key to add now? (y/N)"
    if ($answer -eq "y" -or $answer -eq "Y") { & "$Dir\venv\Scripts\python.exe" -m app.set_key PIXABAY_API_KEY }
    else { Ok "skipped (Wikimedia Commons will be used)" }
}

# ------------------------------------------------------------------ 5. Password
Say "5/7  Dashboard password"
if (HasKey "DASHBOARD_PASSWORD_HASH") { Ok "already set" } else {
    & "$Dir\venv\Scripts\python.exe" -m app.set_password
    if ($LASTEXITCODE -ne 0) { Fail "The password was not saved." }
}

# ------------------------------------------------------------------ 6. Check
Say "6/7  Quick check of the code"
& "$Dir\venv\Scripts\python.exe" -m compileall -q app
if ($LASTEXITCODE -ne 0) { Fail "The app code has an error." }
Ok "code is fine"

# ------------------------------------------------------------------ 7. Start
Say "7/7  Start automatically at every login"
$bat = "$Dir\start-windows.bat"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c start `"VideoFactory`" /min `"$bat`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
try { Register-ScheduledTask -TaskName "VideoFactory" -Action $action -Trigger $trigger -Settings $settings -Force -ErrorAction Stop | Out-Null }
catch { Fail "Windows refused to create the automatic start. Right-click PowerShell, choose 'Run as administrator', and run this script again." }
Start-ScheduledTask -TaskName "VideoFactory"
& powercfg /change standby-timeout-ac 0
Start-Sleep -Seconds 8
try { $code = (Invoke-WebRequest http://127.0.0.1:8000/login -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop).StatusCode } catch { $code = 0 }
if ($code -eq 200) { Ok "RUNNING (a minimized 'VideoFactory' window is in the taskbar: do not close it)" }
else { Write-Host "   Not answering yet. Wait 30 seconds, then open http://127.0.0.1:8000" }

Say "Done. Next steps (see docs\VideoFactory-Setup-Guide.pdf):"
Write-Host "   - If Windows asks whether Python may use networks: tick Private networks, click Allow."
Write-Host "   - Open http://127.0.0.1:8000, log in, open System check, tap Test AI connection."
Write-Host "   - Permanent phone link: install Tailscale (guide step W10)."
Write-Host "   - YouTube: guide Part C, then step W12."
