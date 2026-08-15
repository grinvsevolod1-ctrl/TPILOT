@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$root=(Resolve-Path '.').Path.ToLower(); if(-not $root.EndsWith('\')){$root=$root+'\'}; Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -and $_.CommandLine.ToLower().Contains($root) -and $_.CommandLine -match 'panel_bot\.py' } | ForEach-Object { try { Invoke-CimMethod -InputObject $_ -MethodName Terminate | Out-Null } catch {} }; Write-Output 'STOP PANEL BOT OK'"
exit /b %ERRORLEVEL%
