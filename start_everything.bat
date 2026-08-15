@echo off
chcp 65001 >nul
set "BASE=%~dp0"
set "BASE=%BASE:~0,-1%"

echo STOP OLD PARTNER STAT BOT...
call "%BASE%\stop_partner_stat_bot.bat" >nul 2>&1

echo START CORE SYSTEM...
powershell -NoProfile -ExecutionPolicy Bypass -File "%BASE%\start_everything.ps1"
set "CORE_CODE=%ERRORLEVEL%"

if not "%CORE_CODE%"=="0" (
    echo START CORE SYSTEM FAILED CODE %CORE_CODE%
    exit /b %CORE_CODE%
)

echo START PARTNER STAT BOT...
call "%BASE%\start_partner_stat_bot.bat"
set "PARTNER_CODE=%ERRORLEVEL%"

if not "%PARTNER_CODE%"=="0" (
    echo START PARTNER STAT BOT FAILED CODE %PARTNER_CODE%
    exit /b %PARTNER_CODE%
)

echo START EVERYTHING OK
exit /b 0
