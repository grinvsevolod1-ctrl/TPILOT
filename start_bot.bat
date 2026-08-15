@echo off
chcp 65001 >nul
set "BASE=%~dp0"
set "BASE=%BASE:~0,-1%"
set "PY=%BASE%\venv\Scripts\python.exe"
set "ENV=%BASE%\.env.TPilot"
set "MAIN=%BASE%\main.py"
if not exist "%BASE%\logs" mkdir "%BASE%\logs"

powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%PY%' -ArgumentList @('%MAIN%','--env','%ENV%') -WorkingDirectory '%BASE%' -WindowStyle Hidden -RedirectStandardOutput '%BASE%\logs\tpilot.log' -RedirectStandardError '%BASE%\logs\tpilot.err.log'"

echo START TPILOT OK. Log: logs\tpilot.log
