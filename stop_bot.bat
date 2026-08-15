@echo off
chcp 65001 >nul
set "BASE=%~dp0"
set "BASE=%BASE:~0,-1%"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$base='%BASE%'.ToLower(); $main=(Join-Path $base 'main.py').ToLower(); $envp=(Join-Path $base '.env.TPilot').ToLower(); $procs=Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and ($_.Name -in @('python.exe','pythonw.exe')) -and $_.CommandLine.ToLower().Contains($main) -and $_.CommandLine.ToLower().Contains($envp) -and -not $_.CommandLine.ToLower().Contains('--manager') }; foreach($p in $procs){ try { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } catch {} }"

echo STOP TPILOT OK
