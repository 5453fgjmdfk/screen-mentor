@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0codex-plugin\install.ps1"
set "mentor_exit=%errorlevel%"
pause
exit /b %mentor_exit%
