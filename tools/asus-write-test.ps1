

[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'

$code = @'
using System;
using System.Runtime.InteropServices;
public static class Atk {
    [DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
    static extern IntPtr CreateFile(string name, uint access, uint share, IntPtr sec, uint disp, uint flags, IntPtr tmpl);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool DeviceIoControl(IntPtr h, uint code, byte[] inBuf, uint inSize, byte[] outBuf, uint outSize, ref uint ret, IntPtr ov);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool CloseHandle(IntPtr h);

    const uint CONTROL = 0x0022240C;
    const uint DSTS = 0x53545344;
    const uint DEVS = 0x53564544;
    static IntPtr h = IntPtr.Zero;

    public static string Open() {
        h = CreateFile(@"\\.\ATKACPI", 0x80000000u | 0x40000000u, 1u | 2u, IntPtr.Zero, 3u, 0x80u, IntPtr.Zero);
        if (h == new IntPtr(-1)) return "CreateFile failed winerr=" + Marshal.GetLastWin32Error();
        return "ok";
    }
    public static void Close() { if (h != IntPtr.Zero && h != new IntPtr(-1)) CloseHandle(h); }

    static byte[] Call(uint method, byte[] args) {
        byte[] buf = new byte[8 + args.Length];
        BitConverter.GetBytes(method).CopyTo(buf, 0);
        BitConverter.GetBytes((uint)args.Length).CopyTo(buf, 4);
        Array.Copy(args, 0, buf, 8, args.Length);
        byte[] outBuf = new byte[16];
        uint ret = 0;
        DeviceIoControl(h, CONTROL, buf, (uint)buf.Length, outBuf, (uint)outBuf.Length, ref ret, IntPtr.Zero);
        return outBuf;
    }

    public static int Get(uint deviceId) {
        byte[] args = new byte[8];
        BitConverter.GetBytes(deviceId).CopyTo(args, 0);
        byte[] o = Call(DSTS, args);
        return BitConverter.ToInt32(o, 0) - 65536;
    }

    public static string Set(uint deviceId, int status) {
        byte[] args = new byte[8];
        BitConverter.GetBytes(deviceId).CopyTo(args, 0);
        BitConverter.GetBytes((uint)status).CopyTo(args, 4);
        byte[] o = Call(DEVS, args);
        int result = BitConverter.ToInt32(o, 0);
        return "result=" + result + " raw=" + BitConverter.ToString(o) + " winerr=" + Marshal.GetLastWin32Error();
    }
}
'@

function Line($t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "  [OK] $t" -ForegroundColor Green }
function Bad($t)  { Write-Host "  [X]  $t" -ForegroundColor Red }
function Info($t) { Write-Host "  $t" -ForegroundColor Gray }

Add-Type -TypeDefinition $code -ErrorAction Stop

Line "0. 打开设备"
$open = [Atk]::Open()
if ($open -ne 'ok') { Bad $open; exit 1 }
Ok "已连接 \\.\ATKACPI"

$GPUEco = [uint32]0x00090020

Line "1. 读取当前状态"
$before = [Atk]::Get($GPUEco)
Info "GPUEco = $before  ($(if ($before -eq 0) { '独显开启 / 标准模式' } else { '独显已关闭 / 集显模式' }))"

Line "2. 写回同一个值（测试写权限，不改变状态）"
$res = [Atk]::Set($GPUEco, $before)
Info "DEVS result: $res"

$after = [Atk]::Get($GPUEco)
Info "写回后 GPUEco = $after"

if ($res -match 'result=1') {
    Ok "写入权限确认：result=1 表示 ACPI 接受了写入"
} else {
    Bad "写入未被接受（result != 1），这条路需要管理员权限或驱动不支持"
}

Line "3. 状态校验"
if ($after -eq $before) {
    Ok "状态未改变（$before -> $after），完全无损"
} else {
    Bad "状态被改动了！$before -> $after，正在还原"
    [void][Atk]::Set($GPUEco, $before)
    Start-Sleep -Seconds 2
    $back = [Atk]::Get($GPUEco)
    if ($back -eq $before) { Ok "已还原为 $back" } else { Bad "还原失败，当前 $back，请手动在 G-Helper 里恢复" }
}

Line "4. 顺带读取 MUX（只读）"
$mux = [Atk]::Get([uint32]0x00090016)
$muxName = switch ($mux) { 0 { '集显模式' } 1 { '标准模式(混合)' } 2 { '独显直连' } default { "未知($mux)" } }
Info "GPUMux = $mux  ($muxName)"

[Atk]::Close()
Write-Host "`n测试结束。" -ForegroundColor Green
