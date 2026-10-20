@echo off
rem Double-click: learning + party-invite decline (Ctrl+C or close the window to stop)
cd /d "%~dp0"
powershell -NoExit -ExecutionPolicy Bypass -File "%~dp0start_learn.ps1" %*
