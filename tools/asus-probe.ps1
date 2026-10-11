#Requires -Version 5.1
<#
    ASUS hardware probe - READ ONLY.

    Confirms whether we can read ASUS GPU state through
      1. WMI  root\wmi : AsusAtkWmi_WMNB (DEVS / DSTS methods)
      2. raw  \\.\ATKACPI DeviceIoControl  (what G-Helper uses)
    and reports the current dGPU (Eco) / MUX state.

    This script performs NO writes and changes nothing.

    Device IDs (from G-Helper's AsusACPI.cs):
        GPUEcoROG 0x00090020 : 0 = dGPU on (Standard)   1 = dGPU off (Eco)
        GPUMuxROG 0x00090016 : 0 = iGPU   1 = Standard   2 = Ultimate (dGPU direct)
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'

function Line($t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "  [OK] $t" -ForegroundColor Green }
function Bad($t)  { Write-Host "  [X]  $t" -ForegroundColor Red }
function Info($t) { Write-Host "  $t" -ForegroundColor Gray }

$GPUEco = 0x00090020
$GPUMux = 0x00090016

Line "1. WMI: root\wmi AsusAtkWmi_WMNB"

# Make sure the service that backs WMI is up (read-only probe, no system change)
foreach ($svc in 'Winmgmt') {
    $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
    if ($s) { Info "$svc : $($s.Status)" }
}

$wmiOk = $false
try {
    $instances = Get-CimInstance -Namespace 'root\wmi' -ClassName 'AsusAtkWmi_WMNB' -ErrorAction Stop
    $count = @($instances).Count
    if ($count -eq 0) {
        Bad "AsusAtkWmi_WMNB 存在但没有实例"
    } else {
        Ok "找到 AsusAtkWmi_WMNB，实例数 $count"
        $wmiOk = $true
    }
} catch {
    Bad "无法访问 AsusAtkWmi_WMNB: $($_.Exception.Message)"
}

if ($wmiOk) {
    Line "2. WMI: 用 DSTS 读取设备状态（只读）"
    $obj = @(Get-CimInstance -Namespace 'root\wmi' -ClassName 'AsusAtkWmi_WMNB')[0]
    $methods = $obj.CimClass.CimClassMethods | Select-Object -ExpandProperty Name
    Info "该类支持的方法: $($methods -join ', ')"

    foreach ($pair in @(@('GPUEco', $GPUEco), @('GPUMux', $GPUMux))) {
        $name = $pair[0]; $id = [uint32]$pair[1]
        try {
            $in = $obj.CimClass.CimClassMethods['DSTS'].Parameters
            Info "DSTS 参数: $(($in | ForEach-Object { $_.Name }) -join ', ')"
            break
        } catch { }
    }
}

Line "3. 原始设备 \\.\ATKACPI（G-Helper 走的路）"
$code = @'
using System;
using System.Runtime.InteropServices;
public static class Atk {
    [DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
    public static extern IntPtr CreateFile(string name, uint access, uint share, IntPtr sec, uint disp, uint flags, IntPtr tmpl);
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern bool DeviceIoControl(IntPtr h, uint code, byte[] inBuf, uint inSize, byte[] outBuf, uint outSize, ref uint ret, IntPtr ov);
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern bool CloseHandle(IntPtr h);
    public static string Test(uint deviceId) {
        IntPtr h = CreateFile(@"\\.\ATKACPI", 0x80000000u | 0x40000000u, 1u | 2u, IntPtr.Zero, 3u, 0x80u, IntPtr.Zero);
        if (h == new IntPtr(-1)) return "CreateFile failed, winerr=" + Marshal.GetLastWin32Error();
        try {
            byte[] args = new byte[8];
            BitConverter.GetBytes((uint)deviceId).CopyTo(args, 0);
            BitConverter.GetBytes((uint)0).CopyTo(args, 4);
            byte[] buf = new byte[8 + args.Length];
            BitConverter.GetBytes((uint)0x53545344).CopyTo(buf, 0);   // DSTS
            BitConverter.GetBytes((uint)args.Length).CopyTo(buf, 4);
            Array.Copy(args, 0, buf, 8, args.Length);
            byte[] outBuf = new byte[16];
            uint ret = 0;
            bool ok = DeviceIoControl(h, 0x0022240C, buf, (uint)buf.Length, outBuf, (uint)outBuf.Length, ref ret, IntPtr.Zero);
            int value = BitConverter.ToInt32(outBuf, 0) - 65536;
            return (ok ? "ok" : "ioctl failed, winerr=" + Marshal.GetLastWin32Error()) + " ret=" + ret + " raw=" + BitConverter.ToString(outBuf) + " value=" + value;
        } finally { CloseHandle(h); }
    }
}
'@
try {
    Add-Type -TypeDefinition $code -ErrorAction Stop
    $r1 = [Atk]::Test($GPUEco)
    Info "GPUEco (0x00090020) 读取: $r1"
    $r2 = [Atk]::Test($GPUMux)
    Info "GPUMux (0x00090016) 读取: $r2"
    if ($r1 -like 'ok*') { Ok "\\.\ATKACPI 可用，可以直接读写显卡电源状态" }
    else { Bad "\\.\ATKACPI 不可写/不可读（可能需要管理员或 ATK 驱动）" }
} catch {
    Bad "编译/调用失败: $($_.Exception.Message)"
}

Line "4. 相关驱动与服务"
Info "ATKACPI 设备对象:"
$dev = Get-CimInstance -Namespace 'root\cimv2' -ClassName 'Win32_PnPEntity' -ErrorAction SilentlyContinue |
    Where-Object { $_.DeviceID -match 'ATK|ASUS2018|ASUS9001' } |
    Select-Object Name, DeviceID, Service, Status
if ($dev) { $dev | Format-Table -AutoSize | Out-String | Write-Host } else { Info "  (未能枚举 PnP 设备)" }

Get-Service | Where-Object { $_.Name -match 'ATK|Asus|Armoury' } |
    Select-Object Name, DisplayName, Status, StartType | Format-Table -AutoSize | Out-String | Write-Host

Line "5. 当前显卡电源状态（交叉验证）"
$nvsmi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($nvsmi) {
    $d = & nvidia-smi --query-gpu=name,pstate,power.draw,utilization.gpu --format=csv,noheader 2>&1
    Info "nvidia-smi: $d"
    Info "（P8 且功耗很低 = 独显在待机/被关；P0-P3 = 独显活跃）"
} else { Info "nvidia-smi 不可用" }

Write-Host "`n探针结束（未做任何写入）。" -ForegroundColor Green
