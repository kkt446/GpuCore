

[CmdletBinding()]
param(
    [string]$RuntimeDir = 'C:\Users\xzq\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python',
    [string]$OutDir     = 'dist',
    [string]$Version    = '',
    [switch]$NoZip,
    [switch]$KeepPruned
)

$ErrorActionPreference = 'Stop'

$Root       = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$PackageDir = Join-Path $Root 'gpucore'
$Packaging  = Join-Path $Root 'packaging'

if (-not $Version) {
    $branding = Join-Path $PackageDir 'branding.py'
    $m = Select-String -Path $branding -Pattern 'APP_VERSION\s*=\s*"([^"]+)"'
    if ($m) { $Version = $m.Matches[0].Groups[1].Value } else { $Version = '0.0.0' }
}

function Step($n, $text) { Write-Host "`n[$n] $text" -ForegroundColor Cyan }
function Ok($text)       { Write-Host "    $text" -ForegroundColor DarkGray }
function Die($text)      { Write-Host "`n错误：$text" -ForegroundColor Red; exit 1 }
function Warn($text)     { Write-Host "    $text" -ForegroundColor Yellow }

Write-Host "GpuCore 免安装包构建  v$Version" -ForegroundColor White
Write-Host "项目根: $Root"

Step 1 "检查素材"
if (-not (Test-Path (Join-Path $PackageDir 'app.py'))) { Die "找不到 gpucore\app.py，项目根识别错误：$Root" }
if (-not (Test-Path (Join-Path $RuntimeDir 'python.exe'))) {
    Die "运行时目录里没有 python.exe：$RuntimeDir`n      用 -RuntimeDir 指定一个完整的 Python 安装目录。"
}
foreach ($need in 'pythonw.exe', 'Lib', 'DLLs', 'tcl') {
    if (-not (Test-Path (Join-Path $RuntimeDir $need))) { Die "运行时缺少 $need：$RuntimeDir" }
}
Ok "gpucore 包: $PackageDir"
Ok "Python 运行时: $RuntimeDir"

if ([System.IO.Path]::IsPathRooted($OutDir)) {
    $fullDist = [System.IO.Path]::GetFullPath($OutDir)
    $fullRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    if ($fullDist.StartsWith($fullRoot, [StringComparison]::OrdinalIgnoreCase)) {
        $OutDir = $fullDist.Substring($fullRoot.Length).TrimStart('\')
    } else {
        Die "-OutDir 必须落在项目根目录内（或传相对路径）：$OutDir"
    }
}

$dist = Join-Path $Root $OutDir
$name = "GpuCore-$Version-portable"
$target = Join-Path $dist $name

Step 2 "准备输出目录"
if (Test-Path $target) { Remove-Item $target -Recurse -Force }
New-Item -ItemType Directory -Path $target -Force | Out-Null
Ok $target

Step 3 "复制 Python 运行时"
$pyDst = Join-Path $target 'python'
Copy-Item $RuntimeDir $pyDst -Recurse -Force
Ok ("python\ 已复制（{0:N1} MB）" -f ((Get-ChildItem $pyDst -Recurse -File | Measure-Object Length -Sum).Sum / 1MB))

$pruned = Join-Path $dist '_pruned'
if ($KeepPruned) { New-Item -ItemType Directory -Path $pruned -Force | Out-Null }

Step 3.5 "删掉运行时自带的安装器和文档"

$pyJunk = @{
    
    'File'  = @('python-*-amd64.exe', 'python-*-arm64.exe', 'NEWS.txt', 'LICENSE.txt')
    'Dir'   = @('Doc')
}
$junkRemoved = 0
$junkBytes = 0
foreach ($f in $pyJunk.File) {
    @(Get-ChildItem -Path $pyDst -Filter $f -File -ErrorAction SilentlyContinue) | ForEach-Object {
        $junkBytes += $_.Length
        $junkRemoved++
        if ($KeepPruned) { Move-Item $_.FullName (Join-Path $pruned $_.Name) -Force }
        else            { Remove-Item $_.FullName -Force }
    }
}
foreach ($d in $pyJunk.Dir) {
    $junkDir = Join-Path $pyDst $d
    if (Test-Path $junkDir) {
        $junkBytes += (Get-ChildItem $junkDir -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
        $junkRemoved++
        if ($KeepPruned) { Move-Item $junkDir (Join-Path $pruned $d) -Force }
        else            { Remove-Item $junkDir -Recurse -Force }
    }
}
if ($junkRemoved -gt 0) { Ok ("删掉运行时自带的安装器/文档 {0} 项，省 {1:N1} MB" -f $junkRemoved, ($junkBytes / 1MB)) }
else { Ok "运行时里没有自带的安装器/文档（干净）" }

Step 4 "裁剪用不到的模块"

$libKeep = @(
    'abc.py', 'argparse.py', 'ast.py', 'asyncio', 'base64.py', 'bisect.py', 'bz2.py',
    'calendar.py', 'codecs.py', 'codeop.py', 'collections', 'colorsys.py', 'configparser.py',
    'contextlib.py', 'contextvars.py', 'copy.py', 'copyreg.py', 'csv.py', 'ctypes',
    'dataclasses.py', 'datetime.py', 'decimal.py', 'difflib.py', 'dis.py', 'email',
    'encodings', 'enum.py', 'fnmatch.py', 'fractions.py', 'functools.py', 'genericpath.py',
    'getopt.py', 'getpass.py', 'gettext.py', 'glob.py', 'gzip.py', 'hashlib.py', 'heapq.py',
    'hmac.py', 'html', 'http', 'importlib', 'inspect.py', 'io.py', 'ipaddress.py',
    'json', 'keyword.py', 'linecache.py', 'locale.py', 'logging', 'lzma.py', 'math.py',
    'mimetypes.py', 'ntpath.py', 'nturl2path.py', 'numbers.py', 'opcode.py', 'operator.py',
    'optparse.py', 'os.py', 'pathlib.py', 'pickle.py', 'pkgutil.py', 'platform.py',
    'plistlib.py', 'posixpath.py', 'pprint.py', 'profile.py', 'pstats.py', 'queue.py',
    'quopri.py', 'random.py', 're', 're.py', 'reprlib.py', 'runpy.py', 'secrets.py',
    'selectors.py', 'shlex.py', 'shutil.py', 'signal.py', 'site.py', 'sitecustomize.py',
    'sndhdr.py', 'socket.py', 'socketserver.py', 'ssl.py', 'stat.py', 'string.py',
    'stringprep.py', 'struct.py', 'subprocess.py', 'symtable.py', 'tarfile.py',
    'tempfile.py', 'textwrap.py', 'threading.py', 'timeit.py', 'tkinter', 'token.py',
    'tokenize.py', 'traceback.py', 'tracemalloc.py', 'tty.py', 'types.py', 'typing.py',
    'unicodedata.py', 'urllib', 'uuid.py', 'warnings.py', 'wave.py', 'weakref.py',
    'webbrowser.py', 'winreg.py', 'winsound.py', 'xml', 'zipfile.py', 'zipimport.py',
    'zoneinfo', '_collections_abc.py', '_compat_pickle.py', '_compression.py',
    '_markupbase.py', '_osx_support.py', '_py_abc.py', '_pydecimal.py', '_pyio.py',
    '_sitebuiltins.py', '_strptime.py', '_threading_local.py', '_weakrefset.py',
    
    '__future__.py'
)
$libDst = Join-Path $pyDst 'Lib'
$removed = 0
$removedBytes = 0

$sp = Join-Path $libDst 'site-packages'
if (Test-Path $sp) {
    $bytes = (Get-ChildItem $sp -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
    $n = (Get-ChildItem $sp -Recurse -File -ErrorAction SilentlyContinue).Count
    $removedBytes += $bytes
    if ($KeepPruned) { Move-Item $sp (Join-Path $pruned 'site-packages') -Force }
    else            { Remove-Item $sp -Recurse -Force }
    Ok ("删掉 site-packages（{0} 个文件，省 {1:N1} MB）" -f $n, ($bytes / 1MB))
}

foreach ($item in Get-ChildItem $libDst -Force) {
    if ($item.Name -eq '__pycache__') { continue }
    if ($item.Name -in $libKeep) { continue }
    $bytes = (Get-ChildItem $item.FullName -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
    $removed++
    $removedBytes += $bytes
    if ($KeepPruned) { Move-Item $item.FullName (Join-Path $pruned $item.Name) -Force }
    else            { Remove-Item $item.FullName -Recurse -Force }
}
Ok ("Lib 裁掉 {0} 项，省 {1:N1} MB" -f $removed, ($removedBytes / 1MB))

$tclDst = Join-Path $pyDst 'tcl'
foreach ($sub in Get-ChildItem $tclDst -Directory) {
    $drop = @('demos', 'msgs', 'tzdata', 'opt0.4', 'http1.0')
    if ($sub.Name -in $drop) {
        Remove-Item $sub.FullName -Recurse -Force
        Ok "tcl 删除 $($sub.Name)"
    }
}

$savedEap0 = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& (Join-Path $pyDst 'python.exe') -c "from __future__ import annotations; import ctypes, tkinter, json, subprocess, threading, winreg, urllib.request; print('runtime ok')" *> (Join-Path $env:TEMP 'gpucore-runtime-smoke.txt')
$smoke = $LASTEXITCODE
$ErrorActionPreference = $savedEap0
if ($smoke -ne 0) {
    Get-Content (Join-Path $env:TEMP 'gpucore-runtime-smoke.txt') -Encoding UTF8 | ForEach-Object { Ok $_ }
    Die "裁剪过度：包内解释器已经跑不起来必需模块。检查 `$libKeep 白名单（尤其是 __future__.py）。"
}
Ok "裁剪后运行时自检通过"

Step 5 "复制 GpuCore 本体"
Copy-Item $PackageDir (Join-Path $target 'gpucore') -Recurse -Force
Get-ChildItem (Join-Path $target 'gpucore') -Recurse -Directory -Filter '__pycache__' |
    Remove-Item -Recurse -Force
Copy-Item (Join-Path $Packaging 'main.py') (Join-Path $target 'main.py') -Force

@(
    "GpuCore portable bundle v$Version",
    "This marker makes GpuCore keep its config.json next to the exe."
) -join "`r`n" | Set-Content -Path (Join-Path $target 'PORTABLE') -Encoding ASCII

Copy-Item (Join-Path $Packaging 'smoke-gui.py') (Join-Path $target 'smoke-gui.py') -Force
Ok "gpucore\ + main.py + PORTABLE 标记 + smoke-gui.py"

Step 6 "编译启动器 GpuCore.exe"
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path $csc)) {
    $csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
}
if (-not (Test-Path $csc)) {
    Die "找不到 csc.exe（.NET Framework 编译器）。请装 .NET Framework 4.x，或用 -NoZip 手动处理。"
}
$exePath = Join-Path $target 'GpuCore.exe'
$exePath = Join-Path $target 'GpuCore.exe'

& $csc /nologo /target:exe /optimize+ /out:"$exePath" `
    /reference:System.Windows.Forms.dll /reference:System.dll `
    (Join-Path $Packaging 'launcher.cs') 2>&1 | ForEach-Object { Ok $_ }
if (-not (Test-Path $exePath)) { Die "启动器编译失败" }
Ok ("GpuCore.exe 已生成（{0:N0} KB）" -f ((Get-Item $exePath).Length / 1KB))

Step 7 "写入 ._pth（让运行时只认包内的库）"

$pth = Join-Path $pyDst 'python312._pth'
@('Lib', 'DLLs', '.', 'import site') -join "`r`n" | Set-Content -Path $pth -Encoding ASCII
Ok "python\python312._pth"

Step 8 "在干净环境里自检"

$env:PYTHONPATH = ''
$env:PYTHONHOME = ''
$env:PYTHONIOENCODING = 'utf-8'   
$savedPath = $env:PATH
$env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
$checkFile = Join-Path $env:TEMP 'gpucore-portable-check.txt'

$savedEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& (Join-Path $pyDst 'python.exe') (Join-Path $target 'main.py') --check *> $checkFile
$code = $LASTEXITCODE
$ErrorActionPreference = $savedEap
$env:PATH = $savedPath
$check = Get-Content $checkFile -Encoding UTF8
$check | Select-Object -Last 14 | ForEach-Object { Ok $_ }
if ($code -ne 0) {
    Write-Host "`n包内自检未通过（退出码 $code）。完整输出：" -ForegroundColor Red
    Write-Host "    $checkFile" -ForegroundColor Red
    Die "不要发布这个包 —— 先修掉自检里的报错"
}
Ok "自检通过（退出码 0）"

$exeCheck = Join-Path $env:TEMP 'gpucore-exe-check.txt'
$ErrorActionPreference = 'Continue'
& $exePath --check *> $exeCheck
$exeCode = $LASTEXITCODE
$ErrorActionPreference = $savedEap

$exeBytes = if (Test-Path $exeCheck) { (Get-Item $exeCheck).Length } else { 0 }
if ($exeBytes -lt 200) {
    Write-Host "`nGpuCore.exe --check 只留下了 $exeBytes 字节输出（预期几百字节以上）。" -ForegroundColor Red
    Write-Host "说明启动器没把 stdout 交给子进程 —— 用户敲这个命令会看到一片空白。" -ForegroundColor Red
    Write-Host "检查 launcher.cs 的 consoleMode 白名单里有没有 --check / --report。" -ForegroundColor Red
    Die "启动器输出通路断了"
}
$exeHead = Get-Content $exeCheck -Encoding UTF8 -TotalCount 3
$exeHead | ForEach-Object { Ok $_ }
Ok ("GpuCore.exe --check 输出 {0:N0} 字节（退出码 {1}，非 0 属预期）" -f $exeBytes, $exeCode)

Step 8.5 "界面冒烟测试（真建一个窗口）"

$guiFile = Join-Path $env:TEMP 'gpucore-portable-gui.txt'
$ErrorActionPreference = 'Continue'
& (Join-Path $pyDst 'python.exe') (Join-Path $target 'smoke-gui.py') *> $guiFile
$guiCode = $LASTEXITCODE
$ErrorActionPreference = $savedEap
$guiOut = Get-Content $guiFile -Encoding UTF8 -ErrorAction SilentlyContinue
$guiOut | Select-Object -Last 6 | ForEach-Object { Ok $_ }
$noDesktop = ($guiOut -join "`n") -match 'no display name|couldn.t connect to display|TclError'
if ($guiCode -ne 0) {
    if ($noDesktop) {
        Warn "构建机没有桌面会话，跳过界面冒烟测试（本地打包时会真跑）"
    } else {
        Write-Host "`n界面冒烟失败。完整输出：" -ForegroundColor Red
        Write-Host "    $guiFile" -ForegroundColor Red
        Die '窗口建不起来，用户双击会看到"没反应" —— 不要发布这个包'
    }
} else {
    Ok "界面冒烟通过"
}

Step 9 "清理不该进包的东西"

foreach ($legacy in @('使用说明.txt', 'README-portable.txt')) {
    $p = Join-Path $target $legacy
    if (Test-Path $p) { Remove-Item $p -Force; Ok "删掉旧的 $legacy" }
}

$caches = @(Get-ChildItem $target -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue)
foreach ($c in $caches) { Remove-Item $c.FullName -Recurse -Force -ErrorAction SilentlyContinue }
Ok "清掉 $($caches.Count) 个 __pycache__ 目录（避免带着自检产生的 .pyc 发布）"
Ok "说明书请从仓库根目录的 使用说明.txt 获取，包里不再重复带一份"

Step 10 "统计与压缩"
$totalBytes = (Get-ChildItem $target -Recurse -File | Measure-Object Length -Sum).Sum
$files = (Get-ChildItem $target -Recurse -File | Measure-Object).Count
Write-Host ""
Write-Host ("    目录大小: {0:N1} MB / {1} 个文件" -f ($totalBytes / 1MB), $files) -ForegroundColor White

if (-not $NoZip) {
    $zip = Join-Path $dist "$name.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        $target, $zip, [System.IO.Compression.CompressionLevel]::Optimal, $false)
    $zipMB = (Get-Item $zip).Length / 1MB
    Write-Host ("    ZIP: {0}  ({1:N1} MB)" -f $zip, $zipMB) -ForegroundColor Green
}

Write-Host "`n完成。" -ForegroundColor Green
Write-Host "  发布目录: $target"
if (-not $NoZip) { Write-Host "  发布压缩包: $(Join-Path $dist "$name.zip")" }
Write-Host "`n把这个 zip 发给别人（或传到 GitHub Release），对方解压双击 GpuCore.exe 即可。" -ForegroundColor White
