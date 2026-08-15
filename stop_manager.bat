@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
if "%~1"=="" (
  echo Usage: stop_manager.bat manager_key
  exit /b 1
)
set "KEY=%~1"
set "ROOT=%CD%\"
echo STOP MANAGER %KEY%
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$key='%KEY%'; $root='%ROOT%'; $pattern='--manager\s+' + [regex]::Escape($key) + '(\s|$)'; Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -like ('*' + $root + '*') -and $_.CommandLine -match $pattern } | ForEach-Object { Write-Host ('KILL PID ' + $_.ProcessId + ' ' + $_.CommandLine); try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }"
exit /b 0
