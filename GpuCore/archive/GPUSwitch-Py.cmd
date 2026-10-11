@echo off
rem GPUSwitch 启动器（Python 版）
setlocal
set "PYW=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw.exe"
cd /d "%~dp0"
start "" "%PYW%" -m gpupanel
exit /b 0
