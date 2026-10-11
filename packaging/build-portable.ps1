# GpuCore 免安装包构建脚本
#
# 目标：打出一个 **目标机不需要装 Python** 的目录，解压双击即用。
#
# 做法：借用本机已有的 Python 运行时（默认用 DSH 自带的 3.12 完整运行时），
#       裁剪掉用不到的模块，再把 gpucore 包、GpuCore.exe 启动器、说明文档放进去，
#       最后压成 zip。
#
# 用法：
#     powershell -ExecutionPolicy Bypass -File packaging\build-portable.ps1
#     powershell -ExecutionPolicy Bypass -File packaging\build-portable.ps1 -NoZip -KeepPruned
#
# 参数：
#     -RuntimeDir <路径>  指定 Python 运行时目录（含 python.exe / Lib / DLLs / tcl）
#     -OutDir <路径>      输出目录（默认 dist）
#     -Version <字符串>   版本号（默认从 gpucore\branding.py 读）
#     -NoZip              不压缩，只留目录（调试用）
#     -KeepPruned         保留被裁掉的模块到 _pruned 目录（排查"裁多了"用）

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

# ---------------------------------------------------------------------------
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

# -OutDir 传绝对路径时 Join-Path 会把它当子路径拼，拼出 "C:\...\GpuCore\C:\...\dist"
# 这种非法路径（New-Item 报「不支持给定路径的格式」）。所以这里统一换算成相对项目根的路径。
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

# ---------------------------------------------------------------------------
Step 2 "准备输出目录"
if (Test-Path $target) { Remove-Item $target -Recurse -Force }
New-Item -ItemType Directory -Path $target -Force | Out-Null
Ok $target

# ---------------------------------------------------------------------------
Step 3 "复制 Python 运行时"
$pyDst = Join-Path $target 'python'
Copy-Item $RuntimeDir $pyDst -Recurse -Force
Ok ("python\ 已复制（{0:N1} MB）" -f ((Get-ChildItem $pyDst -Recurse -File | Measure-Object Length -Sum).Sum / 1MB))

# ---------------------------------------------------------------------------

# $pruned 必须在 Step 3.5 之前定义：那一段就开始往 _pruned 里搬东西了，
# 定义晚了在 -KeepPruned 下会因为 $pruned 是 null 直接报错。

# $pruned 必须在 Step 3.5 之前定义：那一段就开始往 _pruned 里搬东西了，
# 定义晚了在 -KeepPruned 下会因为 $pruned 是 null 直接报错（Join-Path 收到 null）.
$pruned = Join-Path $dist '_pruned'
if ($KeepPruned) { New-Item -ItemType Directory -Path $pruned -Force | Out-Null }

Step 3.5 "删掉运行时自带的安装器和文档"
# 借来的运行时不一定是"解压好的干净目录"：
#   * python.org 的安装包目录旁边常常躺着 python-3.12.10-amd64.exe（约 27 MB），
#     CI 上 actions/setup-python 给的就是这种 → 整个复制进来会白胖 27 MB。
#   * 官方发行包还带 Doc（HTML 文档，体积可观）和 NEWS.txt / LICENSE.txt。
# 这些都是给人看的，程序一个字节都用不到，而且**从没用过它们的人不会想到要删**。
# 实测：合成一份和 CI 同构的运行时（192.3 MB，含 26.7 MB 安装器）用旧脚本打出来
# 是 67.2 MB 的包且安装器还在里面；下面这段会把它们全部拿走。
$pyJunk = @{
    # 按名字挑：安装器、文档、发行说明
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

# ---------------------------------------------------------------------------
Step 4 "裁剪用不到的模块"
# 只保留本程序真的会 import 的东西。裁多了会导致启动即崩，所以：
#   * DLLs 里的 .pyd/.dll 全部保留（体积小、依赖关系复杂，不值得冒险）
#   * tcl 只留运行时脚本，删掉 demos/msgs/encodings 等
#   * Lib 按下面的白名单删，白名单之外一律移除

# 保留（以及它们的依赖会被隐式保留，因为整目录保留）
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
    # `from __future__ import annotations` 靠这个模块，少它一个字节都启动不了
    '__future__.py'
)
$libDst = Join-Path $pyDst 'Lib'
$removed = 0
$removedBytes = 0

# site-packages 一律删掉，不做白名单判断：GpuCore 只用标准库 + tkinter + ctypes，
# 里面全是第三方包，一个都不需要。这条实测有价值 —— 借用的运行时素材
# （DSH 那份 Python 3.12.14）自带 numpy/pandas/lxml/Pillow/python-docx 等，
# 不删的话包会白胖 24 MB（151.6 MB vs CI 干净 CPython 的 127.8 MB）。
# 顺带把 pip 也带走：免安装包不需要装任何东西。
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

# tcl 只留运行必需的脚本目录
$tclDst = Join-Path $pyDst 'tcl'
foreach ($sub in Get-ChildItem $tclDst -Directory) {
    $drop = @('demos', 'msgs', 'tzdata', 'opt0.4', 'http1.0')
    if ($sub.Name -in $drop) {
        Remove-Item $sub.FullName -Recurse -Force
        Ok "tcl 删除 $($sub.Name)"
    }
}

# 裁剪后立刻验证解释器本身还能起来。曾因为把 __future__.py 一起裁掉，
# 导致包里每个 `from __future__ import annotations` 都 ModuleNotFoundError，
# 到第 8 步才发现 —— 代价是一次完整的拷贝+压缩。这里几百毫秒就挡掉。
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

# ---------------------------------------------------------------------------
Step 5 "复制 GpuCore 本体"
Copy-Item $PackageDir (Join-Path $target 'gpucore') -Recurse -Force
Get-ChildItem (Join-Path $target 'gpucore') -Recurse -Directory -Filter '__pycache__' |
    Remove-Item -Recurse -Force
Copy-Item (Join-Path $Packaging 'main.py') (Join-Path $target 'main.py') -Force
# 便携标记：让程序把 config.json 写在包目录内，做到「删文件夹即卸载干净」
@(
    "GpuCore portable bundle v$Version",
    "This marker makes GpuCore keep its config.json next to the exe."
) -join "`r`n" | Set-Content -Path (Join-Path $target 'PORTABLE') -Encoding ASCII
# GUI 冒烟脚本也放进包里：它必须在包目录内运行（要给 sys.path 找到 gpucore），
# 而且留着对用户有用 —— 怀疑界面坏了时可以直接跑它。
Copy-Item (Join-Path $Packaging 'smoke-gui.py') (Join-Path $target 'smoke-gui.py') -Force
Ok "gpucore\ + main.py + PORTABLE 标记 + smoke-gui.py"

# ---------------------------------------------------------------------------
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
# 编译成控制台程序（/target:exe）而不是 /target:winexe —— 理由见 launcher.cs 顶部：
# winexe 没有控制台，`GpuCore.exe --check` 的输出会全部丢掉，而自检输出是用户
# 排障的唯一手段。双击时程序自己会把控制台藏起来，体验一样。
& $csc /nologo /target:exe /optimize+ /out:"$exePath" `
    /reference:System.Windows.Forms.dll /reference:System.dll `
    (Join-Path $Packaging 'launcher.cs') 2>&1 | ForEach-Object { Ok $_ }
if (-not (Test-Path $exePath)) { Die "启动器编译失败" }
Ok ("GpuCore.exe 已生成（{0:N0} KB）" -f ((Get-Item $exePath).Length / 1KB))

# ---------------------------------------------------------------------------
Step 7 "写入 ._pth（让运行时只认包内的库）"
# 有这个文件时 Python 进入隔离模式：忽略 PYTHONPATH / 注册表里的 site 配置，
# 避免目标机上装了别的 Python 时被串味。
#
# DLLs 必须显式列进来！隔离模式会关掉默认 DLL 搜索路径，而 _ctypes.pyd 依赖的
# libffi-8.dll、_tkinter.pyd 依赖的 tcl86t.dll 都躺在 DLLs\ 里 —— 漏掉这一行的
# 表现就是 `ModuleNotFoundError: No module named '_ctypes'`，包内解释器一起来就死。
$pth = Join-Path $pyDst 'python312._pth'
@('Lib', 'DLLs', '.', 'import site') -join "`r`n" | Set-Content -Path $pth -Encoding ASCII
Ok "python\python312._pth"

# ---------------------------------------------------------------------------
Step 8 "在干净环境里自检"
# 关键验证：清空 PYTHONPATH/PYTHONHOME 并把 PATH 收窄，确认用的是包内解释器，
# 而不是目标机上碰巧存在的 Python。
$env:PYTHONPATH = ''
$env:PYTHONHOME = ''
$env:PYTHONIOENCODING = 'utf-8'   # 报告里有中文和 ✓，别让子进程退回 GBK
$savedPath = $env:PATH
$env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
$checkFile = Join-Path $env:TEMP 'gpucore-portable-check.txt'
# 必须重定向到文件：python 的 traceback 走 stderr，而 $ErrorActionPreference='Stop'
# 会把任何 stderr 输出当成终止错误，导致自检逻辑根本执行不到。
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

# ---------------------------------------------------------------------------
# Step 8.2 "验证 GpuCore.exe 真的能把自检输出交出来"
#
# 上面那一步是直接调包内 python.exe 的，因此**绕过了启动器**。而用户排障时敲的是
# `GpuCore.exe --check`，走的是 launcher.cs 那条路：它必须认出这是个"要看输出"
# 的参数，用带控制台的 python.exe 并且不建新窗口（CreateNoWindow=false），
# 否则 stdout 是无效句柄，报告一个字都留不下。
#
# 这不是假设的故障 —— `--report` 当初就是这么静默失败的：退出码 0，输出 0 字节。
# 单测 `packaging/launcher-console-flags` 只能静态核对白名单，真正跑一遍还是得靠这里。
$exeCheck = Join-Path $env:TEMP 'gpucore-exe-check.txt'
$ErrorActionPreference = 'Continue'
& $exePath --check *> $exeCheck
$exeCode = $LASTEXITCODE
$ErrorActionPreference = $savedEap
# 注意：这里**不**断言退出码。跑这个包的机器可能本来就没有华硕通道，
# 退出码非 0 是正常结果；我们要证的是"输出有没有传出来"，这才是用户看不见的故障。
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

# ---------------------------------------------------------------------------
Step 8.5 "界面冒烟测试（真建一个窗口）"
# --check 只验证采集通路，不建窗口。窗口建不起来 / 建出来是空的这类故障
# （DPI、字体、Canvas、图标）只有在真建一次窗口时才会现形，而那正是用户
# 双击后看到"没反应"的场景，所以这一步必须做。
#
# 例外：无桌面会话的构建机（比如 CI）建不了窗口，这时跳过而不是失败。
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

# ---------------------------------------------------------------------------
Step 9 "清理不该进包的东西"
# 说明书不再塞进包里 —— 说明文档直接放在仓库里维护，包里重复一份就是两处要同步。
# 这两个名字是历史产物，删掉以防从旧包里继承过来。
foreach ($legacy in @('使用说明.txt', 'README-portable.txt')) {
    $p = Join-Path $target $legacy
    if (Test-Path $p) { Remove-Item $p -Force; Ok "删掉旧的 $legacy" }
}
# __pycache__：前面那几步自检会在包内生成 .pyc。留着不但让包变大，更麻烦的是
# 以后改了 .py 却因为 .pyc 时间戳比源码新而没被重新编译，跑的还是旧代码 ——
# 这种"我明明改了啊"的诡异现象很难查，所以一律清掉。
$caches = @(Get-ChildItem $target -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue)
foreach ($c in $caches) { Remove-Item $c.FullName -Recurse -Force -ErrorAction SilentlyContinue }
Ok "清掉 $($caches.Count) 个 __pycache__ 目录（避免带着自检产生的 .pyc 发布）"
Ok "说明书请从仓库根目录的 使用说明.txt 获取，包里不再重复带一份"

# ---------------------------------------------------------------------------
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
