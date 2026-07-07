@echo off
setlocal
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0restart_windows.ps1" -OpenBrowser %*
exit /b %ERRORLEVEL%
