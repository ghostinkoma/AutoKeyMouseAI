# Keep everything running in one command (CPU only; the GTX 660 is not used - see below):
#   1. mirror the game to the ESP32 TFT
#   2. decline party invites (Cancel + chat "I NO need PT") while the game is in front
#   3. map learning (location / monsters -> DB, map viewer opens automatically)
#   4. window learning (window types for the ESP32): starts right away in the background,
#      then again every learning round (about 30 min), and the model is sent to the ESP32 over Wi-Fi
# If mirror_only stops with an error it is restarted automatically (learn_loop.ps1). Stop with Ctrl+C.
#
#   powershell -ExecutionPolicy Bypass -File D:\hobby\AutoKeyMouseAI\pc\start_all.ps1
#   powershell -ExecutionPolicy Bypass -File D:\hobby\AutoKeyMouseAI\pc\start_all.ps1 -Pull        (git pull first)
#   powershell -ExecutionPolicy Bypass -File D:\hobby\AutoKeyMouseAI\pc\start_all.ps1 -NoKickoff   (no window learning at start)
# Or double-click start_all.bat.
#
# Why not the GPU: OpenCL on the GTX 660 measured 0.72x (slower) for window/icon search (tools\perf_check.py),
# CUDA tools no longer support it, and the GPU is busy with the game and OBS. Instead the search is narrowed
# (performance.full_search_every, about 2.8x faster) and training uses at most half of the CPU threads.
param(
    [switch]$Pull,          # git pull before starting (close the map viewer first)
    [switch]$NoDecline,     # do not decline party invites
    [switch]$NoKickoff,     # do not run window learning once at start
    [int]$OcrEvery = 3      # train OCR every N rounds
)

$ErrorActionPreference = "Continue"
$pc = $PSScriptRoot
$root = Split-Path $pc -Parent
Set-Location $pc

if ($Pull) {
    Write-Host "[all] git pull"
    git -C $root pull
}

$activate = Join-Path $pc ".venv\Scripts\Activate.ps1"
if (-not (Test-Path $activate)) { $activate = Join-Path $root ".venv\Scripts\Activate.ps1" }
if (Test-Path $activate) { . $activate } else { Write-Host "[all] .venv not found. Using the current python." }

# Training uses at most half of the CPU threads (the game, recognition and the TFT come first)
$half = [Math]::Max(1, [int]([Environment]::ProcessorCount / 2))
foreach ($k in @("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "AKM_CV_THREADS")) {
    if (-not [Environment]::GetEnvironmentVariable($k)) { [Environment]::SetEnvironmentVariable($k, "$half", "Process") }
}

# 4. Window learning once now, in the background at low priority (log: models\window_kickoff.log,
#    results also appear in the map viewer). learn_loop repeats it every round afterwards.
if (-not $NoKickoff) {
    $log = Join-Path $pc "models\window_kickoff.log"
    New-Item -ItemType Directory -Force -Path (Join-Path $pc "models") | Out-Null
    $py = (Get-Command python).Source
    # start /belownormal: python itself runs at low priority (the game and the TFT mirror come first)
    $low = "start `"`" /b /wait /belownormal `"$py`""
    $cmd = "$low tools\window_tour.py auto --n 300 && $low tools\window_train.py train --keep-better --push auto"
    Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", "($cmd) > `"$log`" 2>&1") `
        -WorkingDirectory $pc -WindowStyle Hidden | Out-Null
    Write-Host "[all] window learning started in the background (log: models\window_kickoff.log)"
}

# 1-3 (+4 every round): TFT mirror, party invite decline, map learning, automatic training. Restarts on errors.
$loopArgs = @{ OcrEvery = $OcrEvery }
if ($NoDecline) { $loopArgs["NoDecline"] = $true }
Write-Host ("[all] TFT mirror + map learning + window learning" + $(if ($NoDecline) { "" } else { " + party invite decline" }))
& (Join-Path $pc "learn_loop.ps1") @loopArgs
