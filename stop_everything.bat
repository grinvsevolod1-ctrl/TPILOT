@echo off
chcp 65001 >nul
set "BASE=%~dp0"
set "BASE=%BASE:~0,-1%"

echo STOP PARTNER STAT BOT...
call "%BASE%\stop_partner_stat_bot.bat" >nul 2>&1

echo STOP CORE SYSTEM...
powershell -NoProfile -ExecutionPolicy Bypass -File "%BASE%\stop_everything.ps1"
set "CORE_CODE=%ERRORLEVEL%"

echo STOP EVERYTHING OK
exit /b %CORE_CODE%
