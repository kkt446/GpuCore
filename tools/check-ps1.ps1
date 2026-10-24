

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
