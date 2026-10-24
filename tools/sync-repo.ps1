
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

if (-not $LocalDir) { $LocalDir = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path }
$LocalDir = (Resolve-Path $LocalDir).Path
$Api  = "https://api.github.com"
$Hdrs = @{ Authorization = "Bearer $Token"; 'User-Agent' = 'GpuCore-sync'; Accept = 'application/vnd.github+json' }

function Get-Json($p) { Invoke-RestMethod -Uri "$Api$p" -Headers $Hdrs -TimeoutSec 60 }
function Send-Json($m, $p, $b) {
    $json = [System.Text.Encoding]::UTF8.GetBytes(($b | ConvertTo-Json -Depth 6 -Compress))
    Invoke-RestMethod -Uri "$Api$p" -Method $m -Headers $Hdrs -Body $json -ContentType 'application/json; charset=utf-8' -TimeoutSec 180
}

$savedEnc = [Console]::OutputEncoding
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
try {
    $tracked = @(& git -C $LocalDir -c core.quotePath=false ls-files)
    if ($LASTEXITCODE -ne 0) { throw "$LocalDir 不是 git 仓库" }
} finally { [Console]::OutputEncoding = $savedEnc }

$tracked = @($tracked | Where-Object { $_ -ne '' -and $_ -notmatch '__pycache__|\.pyc$' })

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
