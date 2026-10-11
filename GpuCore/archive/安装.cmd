@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo.
echo   显卡切换工具 - 安装
echo   ----------------------------------------
echo.

where powershell >nul 2>nul
if errorlevel 1 (
  echo   [X] 找不到 powershell，无法继续。
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set RC=%ERRORLEVEL%

echo.
if "%RC%"=="0" (
  echo   安装完成。按任意键退出。
) else (
  echo   安装未完成（错误码 %RC%）。按任意键退出。
)
pause >nul
exit /b %RC%
