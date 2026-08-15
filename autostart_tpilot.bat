@echo off
chcp 65001 >nul
cd /d "C:\ALM_TPilot"

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if not exist "C:\ALM_TPilot\logs" mkdir "C:\ALM_TPilot\logs"

echo [%date% %time%] AUTOSTART BEGIN >> "C:\ALM_TPilot\logs\autostart.log"
timeout /t 60 /nobreak >> "C:\ALM_TPilot\logs\autostart.log" 2>&1

echo [%date% %time%] RUN start_everything.bat >> "C:\ALM_TPilot\logs\autostart.log"
call "C:\ALM_TPilot\start_everything.bat" >> "C:\ALM_TPilot\logs\autostart.log" 2>&1
set EXIT_CODE=%ERRORLEVEL%

echo [%date% %time%] AUTOSTART END CODE %EXIT_CODE% >> "C:\ALM_TPilot\logs\autostart.log"
exit /b %EXIT_CODE%

