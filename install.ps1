#Requires -Version 5.1
<#
    GpuCore（双芯）—— 一键安装

    做什么：
      1. 找到（必要时用 winget 装）带 tkinter 的 Python 3.9+
      2. 把 gpucore 包复制到 %LOCALAPPDATA%\GpuCore\app
      3. 生成启动器 + 桌面/开始菜单快捷方式
      4. 跑一次自检，确认这台机器能用

    用法：
        powershell -ExecutionPolicy Bypass -File install.ps1
        powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
        powershell -ExecutionPolicy Bypass -File install.ps1 -NoShortcut

    不需要管理员权限。不会安装任何驱动。

    注意：本文件必须保存为 UTF-8 with BOM。
          Windows PowerShell 5.1 在没有 BOM 时会按系统 ANSI 代码页读取，
          中文注释会被解码坏掉并直接引发语法错误。
#>

[CmdletBinding()]
param(
    [switch]$NoShortcut,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

$SourceDir   = Split-Path -Parent $MyInvocation.MyCommand.Path

# --------------------------------------------------------------------------
# 0. 从一个来源读品牌信息
#    gpucore/branding.py 是唯一权威。以前这里把 GpuCore / 双芯 各硬写了一遍，
#    哪天改了 branding.py 的 APP_NAME 而忘了改这里，安装目录和程序读配置的
#    目录就会分叉（装到 A、去 B 找 config.json）。所以现读，不再抄。
# --------------------------------------------------------------------------
function Read-Branding {
    param([string]$Root)
    $path = Join-Path $Root 'gpucore\branding.py'
    if (-not (Test-Path $path)) { throw "找不到 $path，install.ps1 必须和 gpucore\ 放在一起" }
    $text = Get-Content -Path $path -Raw -Encoding UTF8
    $get = {
        param([string]$Key)
        $m = [regex]::Match($text, "(?m)^\s*$Key\s*=\s*[`"']([^`"']+)[`"']")
        if (-not $m.Success) { throw "branding.py 里读不到 $Key" }
        $m.Groups[1].Value
    }
    [pscustomobject]@{
        Name     = (& $get 'APP_NAME')
        NameCn   = (& $get 'APP_NAME_CN')
        Version  = (& $get 'APP_VERSION')
        DirName  = (& $get 'APP_DIR_NAME')
        Tagline  = (& $get 'APP_TAGLINE')
        Repo     = (& $get 'APP_REPO')
    }
}

$B            = Read-Branding -Root $SourceDir
$AppName      = $B.Name
$AppNameCn    = $B.NameCn
$AppVersion   = $B.Version
$LegacyNames  = @('GPUSwitch')
$InstallDir   = Join-Path $env:LOCALAPPDATA $B.DirName
$AppDir       = Join-Path $InstallDir 'app'
$LauncherCmd  = Join-Path $InstallDir "$AppName.cmd"
$ConsoleCmd   = Join-Path $InstallDir "$AppName-console.cmd"

function Line($t)  { Write-Host "`n=== $t ===" -ForegroundColor Cyan }
function Ok($t)    { Write-Host "  [OK] $t" -ForegroundColor Green }
function Warn2($t) { Write-Host "  [!]  $t" -ForegroundColor Yellow }
function Err($t)   { Write-Host "  [X]  $t" -ForegroundColor Red }

function Get-ShortcutDirs {
    @(
        [Environment]::GetFolderPath('Desktop'),
        (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs')
    ) | Where-Object { $_ -and (Test-Path $_) }
}

function Stop-RunningInstances {
    Get-Process pythonw -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -like "$env:LOCALAPPDATA\Programs\Python\*" } |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 400
}

# --------------------------------------------------------------------------
# 卸载
# --------------------------------------------------------------------------
if ($Uninstall) {
    Line "卸载 $AppName"

    foreach ($dir in (Get-ShortcutDirs)) {
        foreach ($n in @($AppName) + $LegacyNames) {
            $lnk = Join-Path $dir "$n.lnk"
            if (Test-Path $lnk) { Remove-Item $lnk -Force; Ok "已删除 $lnk" }
        }
    }

    $runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    foreach ($n in @($AppName) + $LegacyNames) {
        try {
            if (Get-ItemProperty -Path $runKey -Name $n -ErrorAction SilentlyContinue) {
                Remove-ItemProperty -Path $runKey -Name $n -Force
                Ok "已移除开机自启项 $n"
            }
        } catch { }
    }

    Stop-RunningInstances

    if (Test-Path $InstallDir) { Remove-Item $InstallDir -Recurse -Force; Ok "已删除 $InstallDir" }
    foreach ($n in $LegacyNames) {
        $old = Join-Path $env:LOCALAPPDATA $n
        if (Test-Path $old) { Remove-Item $old -Recurse -Force; Ok "已清理旧目录 $old" }
    }

    Write-Host "`n卸载完成。Python 本身没动，nvidia-smi 等系统组件也没动。`n" -ForegroundColor Green
    exit 0
}

Write-Host ""
Write-Host "  $AppName ($AppNameCn) $AppVersion - 安装" -ForegroundColor White
Write-Host "  ----------------------------------------" -ForegroundColor DarkGray

# --------------------------------------------------------------------------
# 1. 找 Python
# --------------------------------------------------------------------------
Line "1/5 查找 Python"

$python = $null
$candidates = @()

$launcher = Get-Command py -ErrorAction SilentlyContinue
if ($launcher) {
    try {
        $p = & $launcher.Source -3 -c "import sys; print(sys.executable)" 2>$null
        if ($p) { $candidates += $p.Trim() }
    } catch { }
}

foreach ($base in @("$env:LOCALAPPDATA\Programs\Python", $env:ProgramFiles)) {
    if (Test-Path $base) {
        $candidates += Get-ChildItem $base -Filter 'python.exe' -Recurse -Depth 3 `
            -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName
    }
}

foreach ($c in ($candidates | Select-Object -Unique)) {
    if ($c -match 'WindowsApps') { continue }
    try {
        $v = & $c -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -ne 0 -or [version]$v -lt [version]'3.9') { continue }
        & $c -c "import tkinter" 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { $python = $c; break }
        Warn2 "$c 没有 tkinter，跳过"
    } catch { }
}

if (-not $python) {
    Warn2 "没找到可用的 Python，尝试用 winget 装 Python 3.12"
    try {
        & winget install --id Python.Python.3.12 --exact --scope user `
            --accept-package-agreements --accept-source-agreements --silent 2>&1 | Out-Null
        Start-Sleep -Seconds 3
    } catch { }
    $guess = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    if (Test-Path $guess) { $python = $guess }
}

if (-not $python) {
    Err "没找到带 tkinter 的 Python。"
    Write-Host "     手动装一次即可（安装时务必勾选 tcl/tk）：" -ForegroundColor Gray
    Write-Host "       https://www.python.org/downloads/windows/" -ForegroundColor Gray
    exit 1
}
Ok "Python: $python"
Ok "版本: $(& $python --version 2>&1)"

# --------------------------------------------------------------------------
# 2. 复制文件
# --------------------------------------------------------------------------
Line "2/5 安装到 $AppDir"

Stop-RunningInstances

if (Test-Path $AppDir) { Remove-Item $AppDir -Recurse -Force }
New-Item -ItemType Directory -Path $AppDir -Force | Out-Null
Copy-Item (Join-Path $SourceDir 'gpucore') $AppDir -Recurse -Force
Get-ChildItem $AppDir -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Ok "已复制 gpucore"

# --------------------------------------------------------------------------
# 3. 启动器
# --------------------------------------------------------------------------
Line "3/5 生成启动器"

$pythonw = Join-Path (Split-Path $python -Parent) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }

$launcherBody = @(
    '@echo off',
    "rem $AppName ($AppNameCn) - 无窗口启动",
    'chcp 65001 >nul',
    "cd /d `"$AppDir`"",
    "start `"`" `"$pythonw`" -m gpucore",
    'exit /b 0'
) -join "`r`n"
Set-Content -Path $LauncherCmd -Value $launcherBody -Encoding ASCII
Ok "启动器: $LauncherCmd"

$consoleBody = @(
    '@echo off',
    "rem $AppName ($AppNameCn) - 带控制台启动（出问题时看这里的报错）",
    'chcp 65001 >nul',
    "cd /d `"$AppDir`"",
    "`"$python`" -m gpucore",
    'echo.',
    'pause'
) -join "`r`n"
Set-Content -Path $ConsoleCmd -Value $consoleBody -Encoding ASCII
Ok "调试启动器: $ConsoleCmd"

# --------------------------------------------------------------------------
# 4. 快捷方式
# --------------------------------------------------------------------------
if (-not $NoShortcut) {
    Line "4/5 创建快捷方式"
    try {
        $shell = New-Object -ComObject WScript.Shell
        foreach ($dir in (Get-ShortcutDirs)) {
            $lnkPath = Join-Path $dir "$AppName.lnk"
            $lnk = $shell.CreateShortcut($lnkPath)
            $lnk.TargetPath       = $LauncherCmd
            $lnk.WorkingDirectory = $InstallDir
            $lnk.IconLocation     = "$env:SystemRoot\System32\shell32.dll,13"
            $lnk.Description      = "$AppName - $($B.Tagline)"
            $lnk.Save()
            Ok "快捷方式: $lnkPath"
        }
        foreach ($dir in (Get-ShortcutDirs)) {
            foreach ($n in $LegacyNames) {
                $old = Join-Path $dir "$n.lnk"
                if (Test-Path $old) { Remove-Item $old -Force; Ok "已删除旧快捷方式 $old" }
            }
        }
    } catch { Warn2 "创建快捷方式失败: $($_.Exception.Message)" }
} else {
    Line "4/5 跳过快捷方式"
}

# --------------------------------------------------------------------------
# 5. 自检
# --------------------------------------------------------------------------
Line "5/5 自检"
Push-Location $AppDir
try {
    & $python -m gpucore --check
    if ($LASTEXITCODE -ne 0) { Warn2 "自检有项目没通过，看上面 [FAIL] 的行" }
} catch {
    Err "自检没跑起来: $($_.Exception.Message)"
} finally { Pop-Location }

foreach ($n in $LegacyNames) {
    $old = Join-Path $env:LOCALAPPDATA $n
    if (Test-Path $old) {
        Warn2 "发现旧版本目录 $old"
        Write-Host "     想清理就执行: Remove-Item '$old' -Recurse -Force" -ForegroundColor Gray
    }
}

Write-Host ""
Write-Host "  安装完成" -ForegroundColor Green
Write-Host "  ----------------------------------------" -ForegroundColor DarkGray
Write-Host "  桌面双击 $AppName 启动（或开始菜单搜 $AppName）" -ForegroundColor White
Write-Host ""
Write-Host "  用法：" -ForegroundColor Gray
Write-Host "    * 启动后常驻后台，按 Ctrl+Alt+G 呼出/收起面板" -ForegroundColor Gray
Write-Host "    * 面板上点「集显模式 / 标准模式 / 独显直连 / 自动切换」即刻切换" -ForegroundColor Gray
Write-Host "    *「独显直连」需要重启才生效（硬件限制，G-Helper 也一样）" -ForegroundColor Gray
Write-Host "    * 关窗口 = 收进后台，不会退出" -ForegroundColor Gray
Write-Host ""
Write-Host "  出问题看报错：$ConsoleCmd" -ForegroundColor DarkGray
Write-Host ""
