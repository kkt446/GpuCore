@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m gpuswitch.hotswitch gui
) else (
  python -m gpuswitch.hotswitch gui
)
