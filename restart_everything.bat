@echo off
chcp 65001 >nul
set "BASE=%~dp0"
set "BASE=%BASE:~0,-1%"

echo STOPPING...
call "%BASE%\stop_everything.bat"
rem Stop failures are intentionally ignored: processes may already be down.

timeout /t 2 /nobreak >nul

echo STARTING...
call "%BASE%\start_everything.bat"
set "START_CODE=%ERRORLEVEL%"

if not "%START_CODE%"=="0" (
    echo RESTART EVERYTHING FAILED: start_everything.bat exited with code %START_CODE%
    exit /b %START_CODE%
)

echo RESTART EVERYTHING OK
exit /b 0
