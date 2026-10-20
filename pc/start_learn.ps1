# Learning + party-invite decline, in one command.
#   - mirror to ESP32 TFT, recognize, log map/monsters to the DB (map viewer opens automatically)
#   - save frames and retrain repeatedly
#   - party invite: Cancel + chat "I NO need PT" (while the game is in front)
#
#   powershell -ExecutionPolicy Bypass -File D:\hobby\AutoKeyMouseAI\pc\start_learn.ps1
#   powershell -ExecutionPolicy Bypass -File D:\hobby\AutoKeyMouseAI\pc\start_learn.ps1 -Pull        (git pull first)
#   powershell -ExecutionPolicy Bypass -File D:\hobby\AutoKeyMouseAI\pc\start_learn.ps1 -NoDecline   (learning only)
# Or double-click start_learn.bat.  Stop with Ctrl+C.
param(
    [switch]$Pull,          # git pull before starting (close the map viewer first)
    [switch]$NoDecline,     # do not decline party invites
    [int]$OcrEvery = 3      # train OCR every N rounds
)

$ErrorActionPreference = "Continue"
$pc = $PSScriptRoot
$root = Split-Path $pc -Parent
Set-Location $pc

if ($Pull) {
    Write-Host "[start] git pull"
    git -C $root pull
}

$activate = Join-Path $pc ".venv\Scripts\Activate.ps1"
if (-not (Test-Path $activate)) { $activate = Join-Path $root ".venv\Scripts\Activate.ps1" }
if (Test-Path $activate) { . $activate } else { Write-Host "[start] .venv not found. Using the current python." }

$loopArgs = @("-OcrEvery", $OcrEvery)
if ($NoDecline) { $loopArgs += "-NoDecline" }
Write-Host ("[start] learning + map logging" + $(if ($NoDecline) { "" } else { " + party invite decline" }))
& (Join-Path $pc "learn_loop.ps1") @loopArgs
