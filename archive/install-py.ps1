

[CmdletBinding()]
param([switch]$NoShortcut, [switch]$Uninstall)

$ErrorActionPreference = 'Stop'
$SourceDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallDir = Join-Path $env:LOCALAPPDATA 'GPUSwitch'
$AppDir = Join-Path $InstallDir 'app'

function Line($t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "  [OK] $t" -ForegroundColor Green }
function Warn2($t){ Write-Host "  [!]  $t" -ForegroundColor Yellow }
function Err($t)  { Write-Host "  [X]  $t" -ForegroundColor Red }

if ($Uninstall) {
    Line "卸载"
    foreach ($dir in @(
        [Environment]::GetFolderPath('Desktop'),
        (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs')
    )) {
        $lnk = Join-Path $dir 'GPUSwitch.lnk'
        if (Test-Path $lnk) { Remove-Item $lnk -Force; Ok "已删除 $lnk" }
    }
    if (Test-Path $InstallDir) { Remove-Item $InstallDir -Recurse -Force; Ok "已删除 $InstallDir" }
    Write-Host "`n卸载完成（Python 本身没动）`n" -ForegroundColor Green
    exit 0
}

Write-Host ""
Write-Host "  GPUSwitch (Python 版) - 安装" -ForegroundColor White
Write-Host "  ----------------------------------------" -ForegroundColor DarkGray

Line "1/4 查找 Python"
$python = $null
$candidates = @()
$launcher = Get-Command py -ErrorAction SilentlyContinue
if ($launcher) {
    try {
        $p = & $launcher.Source -3 -c "import sys; print(sys.executable)" 2>$null
        if ($p) { $candidates += $p.Trim() }
    } catch { }
}
foreach ($base in @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles")) {
    if (Test-Path $base) {
        $candidates += Get-ChildItem $base -Filter 'python.exe' -Recurse -Depth 3 -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty FullName
    }
}
foreach ($c in ($candidates | Select-Object -Unique)) {
    if ($c -match 'WindowsApps') { continue }
    try {
        $v = & $c -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and [version]$v -ge [version]'3.9') {
            $tk = & $c -c "import tkinter" 2>&1
            if ($LASTEXITCODE -eq 0) { $python = $c; break }
        }
    } catch { }
}

if (-not $python) {
    Warn2 "没找到带 tkinter 的 Python，尝试用 winget 安装"
    & winget install --id Python.Python.3.12 --exact --scope user `
        --accept-package-agreements --accept-source-agreements --silent 2>&1 | Out-Null
    Start-Sleep -Seconds 3
    $guess = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    if (Test-Path $guess) { $python = $guess }
}
if (-not $python) {
    Err "装不上 Python。请手动安装（勾选 tcl/tk）：https://www.python.org/downloads/windows/"
    exit 1
}
Ok "Python: $python  ($(& $python --version))"

Line "2/4 安装到 $AppDir"
if (Test-Path $AppDir) { Remove-Item $AppDir -Recurse -Force }
New-Item -ItemType Directory -Path $AppDir -Force | Out-Null
Copy-Item (Join-Path $SourceDir 'gpupanel') $AppDir -Recurse -Force
Ok "已复制 gpupanel"

Line "3/4 生成启动器"
$pythonw = Join-Path (Split-Path $python -Parent) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }

$launcherCmd = Join-Path $InstallDir 'GPUSwitch.cmd'
$body = @"
@echo off
chcp 65001 >nul
cd /d "$AppDir"
start "" "$pythonw" -m gpupanel
exit /b 0
"@
Set-Content -Path $launcherCmd -Value $body -Encoding ASCII
Ok "启动器: $launcherCmd"

$consoleCmd = Join-Path $InstallDir 'GPUSwitch-console.cmd'
$body2 = @"
@echo off
chcp 65001 >nul
cd /d "$AppDir"
"$python" -m gpupanel
pause
"@
Set-Content -Path $consoleCmd -Value $body2 -Encoding ASCII
Ok "调试启动器（带控制台）: $consoleCmd"

if (-not $NoShortcut) {
    try {
        $shell = New-Object -ComObject WScript.Shell
        foreach ($dir in @(
            [Environment]::GetFolderPath('Desktop'),
            (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs')
        )) {
            $lnkPath = Join-Path $dir 'GPUSwitch.lnk'
            $lnk = $shell.CreateShortcut($lnkPath)
            $lnk.TargetPath = $launcherCmd
            $lnk.WorkingDirectory = $InstallDir
            $lnk.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
            $lnk.Description = 'GPUSwitch 显卡热切换 + 资源监视'
            $lnk.Save()
            Ok "快捷方式: $lnkPath"
        }
    } catch { Warn2 "创建快捷方式失败: $($_.Exception.Message)" }
}

Line "4/4 验证"
Push-Location $AppDir
try {
    $check = & $python -c "import gpupanel.app; print('import OK')" 2>&1
    if ($check -match 'import OK') { Ok "模块导入正常" } else { Err "导入失败: $check" }
} finally { Pop-Location }

Write-Host ""
Write-Host "  安装完成" -ForegroundColor Green
Write-Host "  ----------------------------------------" -ForegroundColor DarkGray
Write-Host "  桌面双击 GPUSwitch 即可启动。" -ForegroundColor White
Write-Host ""
Write-Host "  特点：" -ForegroundColor Gray
Write-Host "    * 监视跑在后台线程，界面不卡、拖动流畅" -ForegroundColor Gray
Write-Host "    * CPU/内存走 Win32 原生调用，核显占用走 PDH 直调" -ForegroundColor Gray
Write-Host "    * nvidia-smi 结果做缓存，不阻塞界面" -ForegroundColor Gray
Write-Host ""
Write-Host "  出问题看日志：`"$consoleCmd`"" -ForegroundColor DarkGray
Write-Host ""
