# Mirror to ESP32 + recognize + DB logging + repeated learning, all in one.
#   OBS -> full-size frame -> recognize (windows/icons, monster name, map/coords) -> DB (viewer) + ESP32 overlay
#                         -> save frames -> train (objects every round, OCR every N rounds, record filter) -> reload
# It restarts automatically if mirror_only stops with an error (OBS / Wi-Fi trouble etc.).
# Stop with Ctrl+C.
#
#   cd D:\hobby\AutoKeyMouseAI\pc
#   powershell -ExecutionPolicy Bypass -File .\learn_loop.ps1
#   powershell -ExecutionPolicy Bypass -File .\learn_loop.ps1 -TrainEvery 40 -OcrEvery 2 -NoViewer
param(
    [double]$Fps = 10,            # frames per second sent to ESP32
    [double]$RecognizeEvery = 1,  # seconds between recognitions (full-size frame)
    [double]$LearnEvery = 30,     # seconds between saved frames (dataset/frames)
    [int]$TrainEvery = 60,        # train after this many new frames (60 x 30 s = about 30 min)
    [int]$OcrEvery = 3,           # train OCR every N rounds (slow)
    [int]$RestartWait = 10,       # seconds to wait before restarting after an error
    [switch]$NoViewer             # do not open the map viewer
)

$ErrorActionPreference = "Continue"
$pc = $PSScriptRoot
$root = Split-Path $pc -Parent
Set-Location $pc

$activate = Join-Path $root ".venv\Scripts\Activate.ps1"
if (-not (Test-Path $activate)) { $activate = Join-Path $pc ".venv\Scripts\Activate.ps1" }
if (Test-Path $activate) { . $activate } else { Write-Host "[loop] .venv not found. Using the current python." }

if (-not (Test-Path (Join-Path $pc "config.yaml"))) {
    Write-Host "[loop] config.yaml not found. Copy config.example.yaml to config.yaml first."
    exit 1
}

# The record filter AI must exist before logging to the DB (it is retrained every round afterwards).
if (-not (Test-Path (Join-Path $pc "models\record_filter.npz"))) {
    Write-Host "[loop] training the record filter AI first..."
    python tools\record_filter.py train
}

# Map viewer (reads the same DB). Started once; skip if a viewer is already running.
$jar = Join-Path $root "viewer\mapviewer.jar"
$db = Join-Path $pc "data\mu_map.db"
if (-not $NoViewer -and (Test-Path $jar)) {
    $running = Get-CimInstance Win32_Process -Filter "Name='javaw.exe' OR Name='java.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*mapviewer.jar*" }
    if (-not $running) {
        Write-Host "[loop] opening the map viewer"
        Start-Process javaw -ArgumentList @("-jar", "`"$jar`"", "`"$db`"") -WorkingDirectory $root
    }
}

$run = 0
$quickFails = 0
while ($true) {
    $run++
    $started = Get-Date
    Write-Host ("[loop] start #{0}  {1}" -f $run, (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
    # 1st start asks the OBS password (Enter = keep saved one). Restarts use the saved password without asking.
    $ask = @()
    if ($run -gt 1) { $ask = @("--no-ask") }
    python tools\mirror_only.py --auto-train @ask --fps $Fps --recognize-every $RecognizeEvery `
        --learn-every $LearnEvery --train-every $TrainEvery --ocr-every $OcrEvery
    $code = $LASTEXITCODE
    if ($code -eq 0) {
        Write-Host "[loop] stopped (Ctrl+C). Bye."
        break
    }
    Add-Content -Path (Join-Path $pc "models\learn_log.txt") -Value (
        "{0} loop: mirror_only exited with code {1}, restarting" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $code)
    # Crashing again and again right after start means a setting / code problem: restarting will not help.
    if (((Get-Date) - $started).TotalSeconds -lt 90) { $quickFails++ } else { $quickFails = 0 }
    if ($quickFails -ge 3) {
        Write-Host "[loop] mirror_only failed 3 times right after start. Stopped. Check the error above."
        exit $code
    }
    Write-Host "[loop] mirror_only stopped with code $code. Restarting in $RestartWait s (Ctrl+C to quit)"
    Start-Sleep -Seconds $RestartWait
}
