@echo off
rem Double-click launcher for check-slow.ps1 (bypasses ExecutionPolicy for this run only)
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0check-slow.ps1" %*
echo.
pause