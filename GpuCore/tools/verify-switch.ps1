#Requires -Version 5.1
<#
    Switch-feature verification (run OUTSIDE the restricted sandbox).

    ASCII-only on purpose: Windows PowerShell 5.1 reads .ps1 files as ANSI/GBK
    unless they carry a UTF-8 BOM, which corrupts non-ASCII literals and can
    break parsing outright.

    It performs three checks and RESTORES the original state afterwards:
      1. Read current global GPU preference
      2. Write "high performance", read it back, then restore
      3. Exercise the app's own switcher code path

    Usage:
        powershell -ExecutionPolicy Bypass -File tools\verify-switch.ps1
#>

[CmdletBinding()]
param(
    [switch]$Keep,
    [string]$PythonPath = ''
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Key = 'HKCU:\Software\Microsoft\DirectX\UserGpuPreferences'
$Name = 'GpuPreference'

function Line($t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "  [OK] $t" -ForegroundColor Green }
function Bad($t)  { Write-Host "  [X]  $t" -ForegroundColor Red }
function Info($t) { Write-Host "  $t" -ForegroundColor Gray }

Line "0. Environment"
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
Info "user: $($id.Name)"
Info "elevated: $(([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))"
Info "restricted token S-1-5-12: $((($id.Groups | ForEach-Object { $_.Value }) -contains 'S-1-5-12'))"

Line "1. Current state"
if (-not (Test-Path $Key)) {
    Bad "registry key missing: $Key"
    exit 1
}
$original = $null
try { $original = (Get-ItemProperty -Path $Key -Name $Name -ErrorAction Stop).$Name } catch { }
if ($null -eq $original) {
    Info "global value '$Name' is currently ABSENT"
} else {
    Info "global value '$Name' = '$original'"
}

$entries = (Get-Item $Key).Property
Info "per-app entries in this key: $($entries.Count)"
foreach ($e in ($entries | Select-Object -First 5)) {
    Info "   $e  =>  $((Get-ItemProperty -Path $Key -Name $e).$e)"
}

Line "2. Write test (set high-performance, then read back)"
$testValue = 'GpuPreference=2;'
try {
    New-ItemProperty -Path $Key -Name $Name -Value $testValue -PropertyType String -Force | Out-Null
    Ok "write succeeded: $Name = '$testValue'"
} catch {
    Bad "write failed: $($_.Exception.Message)"
    Write-Host ""
    Write-Host "  'Access is denied' here means this session also runs with a restricted" -ForegroundColor Yellow
    Write-Host "  token (e.g. launched from DSH). Re-run this script in a normal" -ForegroundColor Yellow
    Write-Host "  VS Code terminal or PowerShell window." -ForegroundColor Yellow
    exit 2
}

$readBack = (Get-ItemProperty -Path $Key -Name $Name).$Name
if ($readBack -eq $testValue) {
    Ok "read-back verified: '$readBack'"
} else {
    Bad "read-back mismatch: '$readBack'"
}

Line "3. Exercise the app's own switcher path"
$py = $PythonPath
if (-not $py) { $py = Join-Path $Root '.venv\Scripts\python.exe' }
if (-not (Test-Path $py)) { $py = 'python' }
Info "python: $py"
$snippet = @'
import sys
sys.path.insert(0, sys.argv[1])
from gpuswitch.switcher import get_backend, is_elevated
from gpuswitch.gpu import enumerate_adapters

backend = get_backend()
print("  backend   :", backend.name)
print("  elevated  :", is_elevated())
print("  global now:", backend.current_global())

for a in enumerate_adapters(use_cache=False):
    if a.kind in ("integrated", "discrete"):
        print("   ", a.name, "| kind =", a.kind, "| value to write =", backend.build_value(a))

print("  capabilities:")
for k, v in backend.capabilities().items():
    print("    -", k, ":", v)
'@
Push-Location $Root
try {
    $snippet | & $py - $Root
    if ($LASTEXITCODE -eq 0) { Ok "switcher interface OK" } else { Bad "switcher exited $LASTEXITCODE" }
} catch {
    Bad "invocation failed: $($_.Exception.Message)"
} finally {
    Pop-Location
}

Line "4. Restore"
if ($Keep) {
    Write-Host "  -Keep given: leaving the test value in place (now high-performance)" -ForegroundColor Yellow
} else {
    if ($null -eq $original) {
        Remove-ItemProperty -Path $Key -Name $Name -ErrorAction SilentlyContinue
        Ok "removed test value (back to absent)"
    } else {
        New-ItemProperty -Path $Key -Name $Name -Value $original -PropertyType String -Force | Out-Null
        Ok "restored to '$original'"
    }
    $final = $null
    try { $final = (Get-ItemProperty -Path $Key -Name $Name -ErrorAction Stop).$Name } catch { }
    if ($null -eq $final) {
        Info "current value: <absent>"
    } else {
        Info "current value: '$final'"
    }
}

Line "5. Hotkey availability"
Push-Location $Root
try {
    & $py -m gpuswitch --test-hotkey ctrl+a
    & $py -m gpuswitch --test-hotkey ctrl+alt+g
} finally { Pop-Location }

Write-Host "`nVerification finished." -ForegroundColor Green
