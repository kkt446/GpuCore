# 检查所有 .ps1 是否带 UTF-8 BOM，并逐个体检语法。
#
# 存在的理由：Windows PowerShell 5.1 读**不带 BOM** 的 .ps1 时，会按当前 ANSI 代码页
# （中文系统上是 GBK）解码。脚本里那些中文注释会被解成一堆乱码，然后以
# 「Unexpected token '鍐欏叆'」这种面目全非的语法错误炸掉——报错信息完全指不到
# 真正的原因上，第一次踩会浪费很久。
#
# 更阴的是：很多编辑器/工具（包括本项目的 AI 编辑工具）写回文件时会把 BOM 掉。
# 所以每次改完 .ps1 都跑一遍这个。
#
# 用法：
#     powershell -ExecutionPolicy Bypass -File tools\check-ps1.ps1

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

$files = Get-ChildItem -Path $root -Recurse -Filter *.ps1 -File |
    Where-Object { $_.FullName -notmatch '\\archive\\|\\.local\\|\\dist\\' }

$badBom = @()
$badParse = @()

foreach ($f in $files) {
    $bytes = [System.IO.File]::ReadAllBytes($f.FullName)
    $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)

    if (-not $hasBom) {
        $badBom += $f
        Write-Host ("  [缺少 BOM] {0}" -f $f.FullName.Replace($root, '.')) -ForegroundColor Red
    }

    # 即使没有 BOM 也解析一遍：万一文件是纯 ASCII 的，缺 BOM 其实不影响运行。
    $err = $null
    [System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$null, [ref]$err) | Out-Null
    if ($err -and $err.Count -gt 0) {
        $badParse += $f
        Write-Host ("  [语法错误 x{0}] {1}" -f $err.Count, $f.FullName.Replace($root, '.')) -ForegroundColor Red
        $err | Select-Object -First 5 | ForEach-Object {
            Write-Host ("      {0}: {1}" -f $_.Extent.StartLineNumber, $_.Message) -ForegroundColor DarkRed
        }
    } else {
        Write-Host ("  [OK] {0}" -f $f.FullName.Replace($root, '.')) -ForegroundColor DarkGray
    }
}

Write-Host ""
if ($badBom.Count -eq 0 -and $badParse.Count -eq 0) {
    Write-Host ("全部通过：{0} 个 .ps1 文件，BOM 和语法都正常 ✓" -f $files.Count) -ForegroundColor Green
    exit 0
}

if ($badBom.Count -gt 0) {
    Write-Host "修 BOM（用 write 工具或下面这行都能修）：" -ForegroundColor Yellow
    Write-Host '  [System.IO.File]::WriteAllText($p, [System.IO.File]::ReadAllText($p), (New-Object System.Text.UTF8Encoding($true)))' -ForegroundColor DarkGray
}
exit 1
