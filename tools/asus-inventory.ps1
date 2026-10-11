#Requires -Version 5.1
<#
    ASUS hardware inventory - READ ONLY.

    Enumerates every vendor interface available on THIS machine and reports the
    live values, so we can see exactly what is usable and what is not.
    Performs NO writes.
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'

function Line($t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "  [OK] $t" -ForegroundColor Green }
function Bad($t)  { Write-Host "  [X]  $t" -ForegroundColor Red }
function Info($t) { Write-Host "  $t" -ForegroundColor Gray }

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
    static IntPtr h = IntPtr.Zero;
    public static int LastError = 0;

    public static bool Open() {
        h = CreateFile(@"\\.\ATKACPI", 0x80000000u | 0x40000000u, 1u | 2u, IntPtr.Zero, 3u, 0x80u, IntPtr.Zero);
        LastError = Marshal.GetLastWin32Error();
        return h != new IntPtr(-1);
    }
    public static void Close() { if (h != IntPtr.Zero && h != new IntPtr(-1)) CloseHandle(h); h = IntPtr.Zero; }

    static byte[] Call(uint method, byte[] args, int outSize) {
        byte[] buf = new byte[8 + args.Length];
        BitConverter.GetBytes(method).CopyTo(buf, 0);
        BitConverter.GetBytes((uint)args.Length).CopyTo(buf, 4);
        Array.Copy(args, 0, buf, 8, args.Length);
        byte[] o = new byte[outSize]; uint r = 0;
        DeviceIoControl(h, CONTROL, buf, (uint)buf.Length, o, (uint)o.Length, ref r, IntPtr.Zero);
        return o;
    }

    // DSTS: returns raw 16 bytes; -65536 means unsupported
    public static string Raw(uint id) {
        byte[] a = new byte[8];
        BitConverter.GetBytes(id).CopyTo(a, 0);
        return BitConverter.ToString(Call(DSTS, a, 16));
    }
    public static int Get(uint id) {
        byte[] a = new byte[8];
        BitConverter.GetBytes(id).CopyTo(a, 0);
        return BitConverter.ToInt32(Call(DSTS, a, 16), 0) - 65536;
    }
    // large read for buffer-style devices
    public static string GetLarge(uint id) {
        byte[] a = new byte[12];
        BitConverter.GetBytes(id).CopyTo(a, 0);
        return BitConverter.ToString(Call(DSTS, a, 64));
    }
}
'@

Add-Type -TypeDefinition $code -ErrorAction Stop

Line "0. Machine"
$bios = Get-ItemProperty 'HKLM:\HARDWARE\DESCRIPTION\System\BIOS' -ErrorAction SilentlyContinue
Info "model : $($bios.SystemProductName)"
Info "bios  : $($bios.BIOSVersion)"

Line "1. ATKACPI device"
if ([Atk]::Open()) {
    Ok "\\.\ATKACPI 已打开（读写句柄）"
} else {
    Bad "打不开 \\.\ATKACPI，WinError=$([Atk]::LastError)"
    exit 1
}

# G-Helper 的完整设备号表，逐个读值，看哪些真正可用
$devices = [ordered]@{
    'GPUEcoROG (独显节能)'      = 0x00090020
    'GPUEcoVivo'                = 0x00090120
    'GPUMuxROG (显示通路)'      = 0x00090016
    'GPUMuxVivo'                = 0x00090026
    'GPUXGConnected (外接显卡)' = 0x00090018
    'GPUXG'                     = 0x00090019
    'GPUDisplayConnect'         = 0x00090030
    'GPU_BASE (TGP基础)'        = 0x00120099
    'GPU_POWER (TGP附加)'       = 0x00120098
    'PPT_GPUC0 (NVIDIA Boost)'  = 0x001200C0
    'PPT_GPUC2 (GPU温度目标)'   = 0x001200C2
    'Temp_GPU'                  = 0x00120097
    'Temp_CPU'                  = 0x00120094
    'PerformanceMode'           = 0x00120075
    'StatusMode'                = 0x00090031
    'PowerSavingMode'           = 0x00090032
    'BatteryLimit'              = 0x00120057
    'BatteryDischarge'          = 0x0012005A
    'VRAM_MEM'                  = 0x000600C4
    'APU_MEM'                   = 0x000600C1
    'CORES_CPU'                 = 0x001200D2
    'CORES_MAX'                 = 0x001200D3
    'ScreenOverdriveSupport'    = 0x00050020
    'ScreenHDRControl'          = 0x00050071
}

Line "2. Device values (DSTS read)"
Write-Host ("  {0,-28} {1,-12} {2}" -f 'name', 'id', 'value') -ForegroundColor Gray
$supported = @()
foreach ($kv in $devices.GetEnumerator()) {
    $id = [uint32]$kv.Value
    $v = [Atk]::Get($id)
    $mark = if ($v -eq -65536 -or $v -lt -1000) { '不支持' } else { "$v" }
    if ($mark -ne '不支持') { $supported += $kv.Key }
    $color = if ($mark -eq '不支持') { 'DarkGray' } else { 'White' }
    Write-Host ("  {0,-28} 0x{1:X8}  {2}" -f $kv.Key, $id, $mark) -ForegroundColor $color
}
Info "本机支持 $($supported.Count) / $($devices.Count) 个设备号"

Line "3. GPU Eco state decoded"
$eco = [Atk]::Get(0x00090020)
$mux = [Atk]::Get(0x00090016)
Info "GPUEco = $eco  -> $(if ($eco -eq 0) { '独显已通电（可以使用）' } elseif ($eco -eq 1) { '独显已断电（集显模式）' } else { "未知 ($eco)" })"
Info "GPUMux = $mux  -> $(switch ($mux) { 0 { '集显模式' } 1 { '标准混合（核显输出+独显渲染）' } 2 { '独显直连' } default { "未知 ($mux)" } })"

Line "4. What is actually rendering right now"
$smi = & nvidia-smi --query-gpu=name,utilization.gpu,memory.used,pstate,power.draw --format=csv,noheader 2>&1
Info "nvidia-smi: $smi"
$gpuEngines = (Get-Counter '\GPU Engine(*)\Utilization Percentage' -ErrorAction SilentlyContinue).CounterSamples |
    Where-Object { $_.CookedValue -gt 0.5 } |
    Sort-Object CookedValue -Descending | Select-Object -First 6
Info "PDH 引擎占用（>0.5%）："
foreach ($s in $gpuEngines) { Write-Host ("    {0,-70} {1,6:N1}%" -f $s.InstanceName, $s.CookedValue) -ForegroundColor DarkGray }

Line "5. Adapter LUIDs (to map PDH counters to physical GPUs)"
$mem = (Get-Counter '\GPU Adapter Memory(*)\Dedicated Usage' -ErrorAction SilentlyContinue).CounterSamples
foreach ($s in $mem) {
    Write-Host ("    {0,-42} 显存占用 {1,12:N0} 字节" -f $s.InstanceName, $s.CookedValue) -ForegroundColor DarkGray
}

Line "6. Display topology"
$dd = Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue |
    Select-Object Name, PNPDeviceID, CurrentHorizontalResolution, CurrentVerticalResolution, Status
if ($dd) { $dd | Format-Table -AutoSize | Out-String | Write-Host } else { Info "WMI 不可用，改用注册表" }

[Atk]::Close()
Write-Host "`n盘点结束（未做任何写入）。" -ForegroundColor Green
