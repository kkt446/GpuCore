@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ============================================================
rem  华硕显卡切换工具 - 启动器
rem
rem  每次启动前先把配置里的「锁定」关掉。
rem  原因：锁定守护会在后台反复写 ACPI 改独显供电，如果配置里
rem  残留着开启的锁定，用户点按钮的切换会被守护立刻抢回，
rem  表现成"按哪个都不管用"。
rem ============================================================

set "CFG=%APPDATA%\GPUSwitch\config.json"

if exist "%CFG%" (
  echo 正在重置锁定状态...
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
