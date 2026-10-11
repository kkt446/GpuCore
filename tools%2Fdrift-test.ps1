#Requires -Version 5.1
<#
    Real-hardware drift test.

    Plays the role of Armoury Crate / G-Helper: flips the dGPU power state via
    the ASUS ACPI interface, then watches whether the running GPUSwitch guard
    notices and restores the locked state.

    ASUS device ids:
        GPUEco 0x00090020 : 0 = dGPU on   1 = dGPU off
#>

[CmdletBinding()]
param(
    [int]$TimeoutSec = 15,
    [int]$TargetEco = 1
)

$ErrorActionPreference = 'Continue'

$code = @'
using System;
using System.Runtime.InteropServices;
public static class Atk {
    [DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
    static extern IntPtr CreateFile(string n, uint a, uint s, IntPtr sec, uint d, uint f, IntPtr t);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool DeviceIoControl(IntPtr h, uint c, byte[] i, uint isz, byte[] o, uint osz, ref uint r, IntPtr ov);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
    const uint CONTROL = 0x0022240C, DSTS = 0x53545344, DEVS = 0x53564544;
    static byte[] Call(IntPtr h, uint m, byte[] args) {
        byte[] b = new byte[8 + args.Length];
        BitConverter.GetBytes(m).CopyTo(b, 0);
        BitConverter.GetBytes((uint)args.Length).CopyTo(b, 4);
        Array.Copy(args, 0, b, 8, args.Length);
        byte[] o = new byte[16]; uint r = 0;
        DeviceIoControl(h, CONTROL, b, (uint)b.Length, o, (uint)o.Length, ref r, IntPtr.Zero);
        return o;
    }
    public static int Get(uint id) {
        IntPtr h = CreateFile(@"\\.\ATKACPI", 0x80000000u | 0x40000000u, 3u, IntPtr.Zero, 3u, 0x80u, IntPtr.Zero);
        if (h == new IntPtr(-1)) return -999;
        try { byte[] a = new byte[8]; BitConverter.GetBytes(id).CopyTo(a, 0);
              return BitConverter.ToInt32(Call(h, DSTS, a), 0) - 65536; }
        finally { CloseHandle(h); }
    }
    public static int Set(uint id, int v) {
        IntPtr h = CreateFile(@"\\.\ATKACPI", 0x80000000u | 0x40000000u, 3u, IntPtr.Zero, 3u, 0x80u, IntPtr.Zero);
        if (h == new IntPtr(-1)) return -999;
        try { byte[] a = new byte[8]; BitConverter.GetBytes(id).CopyTo(a, 0); BitConverter.GetBytes((uint)v).CopyTo(a, 4);
              return BitConverter.ToInt32(Call(h, DEVS, a), 0); }
        finally { CloseHandle(h); }
    }
}
'@

Add-Type -TypeDefinition $code -ErrorAction Stop
$ECO = [uint32]0x00090020

function Line($t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "  [OK] $t" -ForegroundColor Green }
function Bad($t)  { Write-Host "  [X]  $t" -ForegroundColor Red }
function Info($t) { Write-Host "  $t" -ForegroundColor Gray }

Line "1. Before sabotage"
$before = [Atk]::Get($ECO)
Info "GPUEco = $before  (0 = dGPU on, 1 = dGPU off)"
if ($before -ne 0) {
    Bad "dGPU is not currently ON; the guard may not be running. Aborting."
    exit 1
}

Line "2. Sabotaging: switching dGPU OFF (pretending to be Armoury Crate)"
$logPath = Join-Path $env:APPDATA 'GPUSwitch\gpuswitch.log'
$logBefore = 0
if (Test-Path $logPath) {
    $logBefore = @(Select-String -Path $logPath -Pattern 'restored ' -Encoding UTF8 -ErrorAction SilentlyContinue).Count
}
Info "guard restore events before sabotage: $logBefore"

$rc = [Atk]::Set($ECO, $TargetEco)
Info "DEVS result = $rc (1 = accepted)"
Ok "Sent 'dGPU off' - this is exactly the state the user complains about"

Line "3. Sampling GPUEco for up to $TimeoutSec s"
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$sawTarget = $false
while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) {
    $v = [Atk]::Get($ECO)
    if ($v -eq $TargetEco) { $sawTarget = $true }
    Write-Host ("    +{0,5:N1} s  eco={1}" -f $sw.Elapsed.TotalSeconds, $v) -ForegroundColor DarkGray
    Start-Sleep -Milliseconds 250
}

$logAfter = 0
if (Test-Path $logPath) {
    $logAfter = @(Select-String -Path $logPath -Pattern 'restored ' -Encoding UTF8 -ErrorAction SilentlyContinue).Count
}
$newRestores = $logAfter - $logBefore

Line "4. Result"
Info "observed the sabotaged value during sampling : $sawTarget"
Info "guard restore events recorded in log         : +$newRestores"
Info "final GPUEco                                 : $([Atk]::Get($ECO))  (0 = dGPU on, as locked)"

if ($newRestores -ge 1) {
    Ok "REAL HARDWARE TEST PASSED: external change detected and locked state restored"
    Write-Host ""
    Write-Host "  Guard log entries produced by this test:" -ForegroundColor Gray
    Select-String -Path $logPath -Pattern '\[锁定\]' -Encoding UTF8 -ErrorAction SilentlyContinue |
        Select-Object -Last 4 |
        ForEach-Object { Write-Host "    $($_.Line)" -ForegroundColor DarkGray }
    exit 0
} else {
    Bad "No restore event recorded - the guard is not enforcing the lock"
    exit 2
}
