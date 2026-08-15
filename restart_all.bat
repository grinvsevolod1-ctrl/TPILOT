@echo off
set "BASE=%~dp0"
set "BASE=%BASE:~0,-1%"
call "%BASE%\stop_all.bat"
timeout /t 2 /nobreak >nul
call "%BASE%\start_all.bat"
