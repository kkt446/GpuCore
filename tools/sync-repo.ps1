<#
GpuCore 仓库同步 —— 让 GitHub 上的结构 == 你本地 git 仓库的结构。

为什么需要它：网页上传会把整层目录结构压平（根目录多出一层 GpuCore\），
还会把 __pycache__ 一并传上去，并且吞掉 .github/workflows。这个脚本走
GitHub Git Data API，一次提交建好整棵树，路径分毫不差。

用法：
  pwsh -File tools/sync-repo.ps1 -Token ghp_xxx              # 预演，只出计划
  pwsh -File tools/sync-repo.ps1 -Token ghp_xxx -Apply       # 真改
  pwsh -File tools/sync-repo.ps1 -Token ghp_xxx -Apply -FixEncodedPaths   # 见下

【踩过的坑，别再犯】
  Contents API 的 {path} 要「按段编码」，斜杠必须原样保留。如果把整条路径丢进
  [uri]::EscapeDataString，斜杠会变成 %2F，GitHub 就把 "gpucore%2Fapp.py" 当成
  一个**文件名**，真正的目录一个都不建 —— 表面 67 个文件全 OK，实际仓库是坏的。
  所以传文件走 Git Data API（路径是 JSON 字符串，不经过 URL），
  只有「读单个文件」这种非它不可的地方才用 Contents API，且必须按段编码。
  万一已经传坏了，用 -FixEncodedPaths 把带 %2F 的假文件清掉再重传。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Token,
    [string]$Repo = "KKT446/GpuCore",
    [string]$Branch = "main",
    [string]$LocalDir = '',
    [switch]$Apply,
    [switch]$FixEncodedPaths
)
$ErrorActionPreference = 'Stop'
# PowerShell 5.1 在 param() 默认值求值时 $PSScriptRoot 还是空的，只能等进入脚本体再算
if (-not $LocalDir) { $LocalDir = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path }
$LocalDir = (Resolve-Path $LocalDir).Path
$Api  = "https://api.github.com"
$Hdrs = @{ Authorization = "Bearer $Token"; 'User-Agent' = 'GpuCore-sync'; Accept = 'application/vnd.github+json' }

function Get-Json($p) { Invoke-RestMethod -Uri "$Api$p" -Headers $Hdrs -TimeoutSec 60 }
function Send-Json($m, $p, $b) {
    $json = [System.Text.Encoding]::UTF8.GetBytes(($b | ConvertTo-Json -Depth 6 -Compress))
    Invoke-RestMethod -Uri "$Api$p" -Method $m -Headers $Hdrs -Body $json -ContentType 'application/json; charset=utf-8' -TimeoutSec 180
}

# git 默认把非 ASCII 路径转义成 \345\220\257，PowerShell 处理会报「非法字符」
$savedEnc = [Console]::OutputEncoding
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
try {
    $tracked = @(& git -C $LocalDir -c core.quotePath=false ls-files)
    if ($LASTEXITCODE -ne 0) { throw "$LocalDir 不是 git 仓库" }
} finally { [Console]::OutputEncoding = $savedEnc }
# __pycache__ / *.pyc 永远不该进仓库
$tracked = @($tracked | Where-Object { $_ -ne '' -and $_ -notmatch '__pycache__|\.pyc$' })

# 本地 blob sha 用 git hash-object 算 —— 它走的是仓库里的规范化内容（LF），
# 跟远端 tree 里的 sha 才是同一套算法。Get-FileHash 算的是磁盘上的 CRLF 字节，必然全不一致。
$localSha = @{}
foreach ($f in $tracked) {
    $localSha[$f] = (& git -C $LocalDir hash-object -- $f)
}

$ref    = Get-Json "/repos/$Repo/git/ref/heads/$Branch"
$commit = Get-Json "/repos/$Repo/git/commits/$($ref.object.sha)"
$tree   = Get-Json "/repos/$Repo/git/trees/$($commit.tree.sha)?recursive=1"
$remote = @{}
foreach ($e in $tree.tree) { if ($e.type -eq 'blob') { $remote[$e.path] = $e.sha } }

$broken  = @($remote.Keys | Where-Object { $_ -like '*%2F*' })
$toDel   = @($remote.Keys | Where-Object { -not $localSha.ContainsKey($_) -and $_ -notlike '*%2F*' })
$toPut   = @($localSha.Keys | Where-Object { -not $remote.ContainsKey($_) -or $remote[$_] -ne $localSha[$_] })

Write-Host "本地（git 跟踪，已滤掉 __pycache__）: $($localSha.Count) 个" -ForegroundColor Cyan
Write-Host "远端: $($remote.Count) 个   当前分支提交 $($commit.sha.Substring(0,7))" -ForegroundColor Cyan
Write-Host ""
if ($broken.Count) {
    Write-Host "!! 发现 $($broken.Count) 个路径被 URL 编码弄坏的文件（名字里带 %2F）" -ForegroundColor Magenta
    if ($FixEncodedPaths) { Write-Host "   已加 -FixEncodedPaths，这次会一并清除" -ForegroundColor Magenta }
    else { Write-Host "   要清掉请加 -FixEncodedPaths" -ForegroundColor Magenta }
    Write-Host ""
}
Write-Host "要删除 $($toDel.Count) 个:" -ForegroundColor Yellow
$toDel | Sort-Object | ForEach-Object { Write-Host "   - $_" }
Write-Host "要上传 $($toPut.Count) 个:" -ForegroundColor Green
$toPut | Sort-Object | ForEach-Object { Write-Host "   + $_" }

if (-not $Apply) { Write-Host "`n这是预演。加 -Apply 才会真改。" -ForegroundColor Magenta; return }
if (@($toDel).Count + @($toPut).Count + $broken.Count -eq 0) { Write-Host "`n已经一致，无需改动 ✓" -ForegroundColor Green; return }

# ── 第一步：一次提交删掉多余文件（含坏路径）──────────────────────────
$kill = @($toDel)
if ($FixEncodedPaths) { $kill += $broken }
if ($kill.Count) {
    $entries = @()
    foreach ($d in $kill) { $entries += @{ path = $d; mode = '100644'; type = 'blob'; sha = $null } }
    $t1 = Send-Json 'POST' "/repos/$Repo/git/trees" @{ base_tree = $commit.tree.sha; tree = $entries }
    $c1 = Send-Json 'POST' "/repos/$Repo/git/commits" @{
        message = "清理：删掉 $($kill.Count) 个不该在仓库里的文件"; tree = $t1.sha; parents = @($commit.sha)
    }
    Send-Json 'PATCH' "/repos/$Repo/git/refs/heads/$Branch" @{ sha = $c1.sha } | Out-Null
    Write-Host "`n已删除 $($kill.Count) 个（提交 $($c1.sha.Substring(0,7))）" -ForegroundColor Yellow
    Start-Sleep -Seconds 2
    $commit = Get-Json "/repos/$Repo/git/commits/$($c1.sha)"
}

# ── 第二步：blob → 一棵树 → 一个提交 ─────────────────────────────────
$entries = @()
$n = 0
foreach ($f in ($toPut | Sort-Object)) {
    $n++
    $bytes = [System.IO.File]::ReadAllBytes((Join-Path $LocalDir $f))
    $blob  = Send-Json 'POST' "/repos/$Repo/git/blobs" @{ content = [Convert]::ToBase64String($bytes); encoding = 'base64' }
    $entries += @{ path = $f; mode = '100644'; type = 'blob'; sha = $blob.sha }
    Write-Host ("  [{0}/{1}] {2}" -f $n, $toPut.Count, $f) -ForegroundColor DarkGray
}
if (-not $entries.Count) { Write-Host "`n已一致 ✓" -ForegroundColor Green; return }

$t2 = Send-Json 'POST' "/repos/$Repo/git/trees" @{ base_tree = $commit.tree.sha; tree = $entries }
$c2 = Send-Json 'POST' "/repos/$Repo/git/commits" @{
    message = "同步 $($entries.Count) 个文件"; tree = $t2.sha; parents = @($commit.sha)
}
Send-Json 'PATCH' "/repos/$Repo/git/refs/heads/$Branch" @{ sha = $c2.sha } | Out-Null
Write-Host "`n完成。提交 $($c2.sha.Substring(0,7))，$($entries.Count) 个文件" -ForegroundColor Green
