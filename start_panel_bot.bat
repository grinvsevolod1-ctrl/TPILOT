@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_panel_bot.ps1"
exit /b %ERRORLEVEL%
