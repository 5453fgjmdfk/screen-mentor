@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0codex-plugin\install.ps1" -DesktopOnly
set "mentor_exit=%errorlevel%"
pause
exit /b %mentor_exit%
