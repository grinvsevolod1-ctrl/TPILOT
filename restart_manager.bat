@echo off
set "KEY=%~1"
if "%KEY%"=="" (
  echo Usage: restart_manager.bat manager_key
  exit /b 1
)
set "BASE=%~dp0"
set "BASE=%BASE:~0,-1%"
call "%BASE%\stop_manager.bat" "%KEY%"
timeout /t 2 /nobreak >nul
call "%BASE%\start_manager.bat" "%KEY%"
