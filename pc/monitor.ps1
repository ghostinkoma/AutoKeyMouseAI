# Watch learn_loop.ps1 from a second window (read only; safe to run while learning).
# Every -Every minutes shows: is learn_loop running, latest learn_log lines, DB progress, recent registrations.
# With -Eval, also measures OCR accuracy every -EvalEvery rounds (takes a while).
# Stop with Ctrl+C.
#
#   cd D:\hobby\AutoKeyMouseAI\pc
#   powershell -ExecutionPolicy Bypass -File .\monitor.ps1
#   powershell -ExecutionPolicy Bypass -File .\monitor.ps1 -Every 10 -Eval -EvalEvery 6
param(
    [int]$Every = 5,          # minutes between checks
    [int]$LogLines = 8,       # learn_log lines to show
    [int]$RegLog = 10,        # recent DB registrations to show
    [switch]$Eval,            # also run the OCR accuracy check
    [int]$EvalEvery = 6,      # run the OCR check every N checks (6 x 5 min = 30 min)
    [switch]$Once             # check once and exit
)

$ErrorActionPreference = "Continue"
$pc = $PSScriptRoot
Set-Location $pc
$activate = Join-Path $pc ".venv\Scripts\Activate.ps1"
if (Test-Path $activate) { . $activate } else { Write-Host "[monitor] .venv not found. Using the current python." }
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$log = Join-Path $pc "models\learn_log.txt"
$frames = Join-Path $pc "dataset\frames"
$n = 0
while ($true) {
    $n++
    Write-Host ""
    Write-Host ("===== check #{0}  {1} =====" -f $n, (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))

    $proc = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*mirror_only.py*" }
    if ($proc) { Write-Host "[monitor] mirror_only: running (pid $($proc.ProcessId -join ','))" }
    else { Write-Host "[monitor] mirror_only: NOT running  -> start learn_loop.ps1 in the other window" -ForegroundColor Red }

    if (Test-Path $frames) {
        $all = Get-ChildItem $frames -Filter *.png
        $recent = $all | Where-Object { $_.LastWriteTime -gt (Get-Date).AddMinutes(-$Every) }
        Write-Host ("[monitor] saved frames: {0} (new in last {1} min: {2})" -f $all.Count, $Every, $recent.Count)
    }

    if (Test-Path $log) {
        Write-Host "[monitor] learn_log (latest):"
        Get-Content $log -Encoding UTF8 -Tail $LogLines | ForEach-Object { "  $_" }
        $last = (Get-Item $log).LastWriteTime
        if ($proc -and ((Get-Date) - $last).TotalHours -gt 2) {
            Write-Host ("[monitor] learn_log not updated since {0} (over 2 h). Learning may be stuck." -f $last) -ForegroundColor Yellow
        }
    }

    Write-Host "[monitor] DB progress:"
    python tools\db_status.py
    if ($RegLog -gt 0) { python tools\db_status.py --log $RegLog }

    if ($Eval -and (($n - 1) % $EvalEvery -eq 0)) {
        Write-Host "[monitor] OCR accuracy (this takes a while):"
        python tools\edge_train.py eval --show 5
    }

    if ($Once) { break }
    Start-Sleep -Seconds ($Every * 60)
}
