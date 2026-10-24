@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"


set "CFG=%APPDATA%\GPUSwitch\config.json"

if exist "%CFG%" (
  echo 姝ｅ湪閲嶇疆閿佸畾鐘舵€?..
  powershell -NoProfile -Command ^
    "$p='%CFG%'; $c=Get-Content $p -Raw -Encoding UTF8 | ConvertFrom-Json;" ^
    "$c.lock.enabled=$false; $c.lock.mode_id=''; $c.auto_lock_on_switch=$false; $c.lock_restore_on_start=$false;" ^
    "$c | ConvertTo-Json -Depth 10 | Set-Content $p -Encoding UTF8" 2>nul
)

if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" -m gpuswitch --show
) else (
  start "" pythonw -m gpuswitch --show
)

exit /b 0
