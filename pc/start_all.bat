@echo off
rem Double-click: TFT mirror + party-invite decline + map learning + window learning (Ctrl+C or close the window to stop)
cd /d "%~dp0"
powershell -NoExit -ExecutionPolicy Bypass -File "%~dp0start_all.ps1" %*
