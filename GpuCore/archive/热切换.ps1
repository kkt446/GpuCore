#Requires -Version 5.1
<#
    华硕显卡热切换 —— 零依赖版（只用 Windows 自带的 PowerShell）

    不需要 Python、不需要任何库。双击 热切换-GUI.cmd 出窗口，
    或在命令行里用：
        powershell -ExecutionPolicy Bypass -File 热切换.ps1 on      # 开独显（标准模式）
        powershell -ExecutionPolicy Bypass -File 热切换.ps1 off     # 关独显（集显模式）
        powershell -ExecutionPolicy Bypass -File 热切换.ps1 status  # 看状态

    切换顺序照 G-Helper 的 GPUModeControl.SetGPUEco：
        关独显：杀占用独显的进程 -> 停 NVIDIA 服务 -> 写 ACPI(GPUEco=1)
        开独显：写 ACPI(GPUEco=0) -> 启 NVIDIA 服务
    独显被占用时 EC 拒绝断电，所以"先杀进程"这步不能省。
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('on', 'off', 'status', 'gui', 'fix')]
    [string]$Action = 'status'
)

$ErrorActionPreference = 'Continue'

# ---------------------------------------------------------------------------
# ACPI 通道（\\.\ATKACPI）
# ---------------------------------------------------------------------------

$atkSource = @'
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
    public static string LastError = "";

    public static bool Open() {
        h = CreateFile(@"\\.\ATKACPI", 0x80000000u | 0x40000000u, 1u | 2u, IntPtr.Zero, 3u, 0x80u, IntPtr.Zero);
        if (h == new IntPtr(-1)) {
            LastError = "winerr=" + Marshal.GetLastWin32Error();
            return false;
        }
        return true;
    }
    public static void Close() { if (h != IntPtr.Zero && h != new IntPtr(-1)) CloseHandle(h); h = IntPtr.Zero; }

    static byte[] Call(uint method, byte[] args) {
        byte[] buf = new byte[8 + args.Length];
        BitConverter.GetBytes(method).CopyTo(buf, 0);
        BitConverter.GetBytes((uint)args.Length).CopyTo(buf, 4);
        Array.Copy(args, 0, buf, 8, args.Length);
        byte[] o = new byte[16]; uint r = 0;
        DeviceIoControl(h, CONTROL, buf, (uint)buf.Length, o, (uint)o.Length, ref r, IntPtr.Zero);
        return o;
    }
    public static int Get(uint id) {
        byte[] a = new byte[8];
        BitConverter.GetBytes(id).CopyTo(a, 0);
        int raw = BitConverter.ToInt32(Call(DSTS, a), 0);
        return raw == 0 ? -65536 : raw - 65536;
    }
    public static int Set(uint id, int v) {
        byte[] a = new byte[8];
        BitConverter.GetBytes(id).CopyTo(a, 0);
        BitConverter.GetBytes((uint)v).CopyTo(a, 4);
        return BitConverter.ToInt32(Call(DEVS, a), 0);
    }
}
'@

if (-not ('Atk' -as [type])) {
    Add-Type -TypeDefinition $atkSource -ErrorAction Stop
}

$DEV_ECO = [uint32]0x00090020
$DEV_MUX = [uint32]0x00090016
$NV_SERVICES = @('NVDisplay.ContainerLocalSystem', 'NvContainerLocalSystem')
$PROTECTED = @(
    'system', 'registry', 'memory compression', 'csrss.exe', 'wininit.exe',
    'winlogon.exe', 'services.exe', 'lsass.exe', 'smss.exe', 'svchost.exe',
    'dwm.exe', 'explorer.exe', 'fontdrvhost.exe', 'sihost.exe', 'ctfmon.exe',
    'taskhostw.exe', 'runtimebroker.exe', 'shellexperiencehost.exe',
    'startmenuexperiencehost.exe', 'searchhost.exe', 'textinputhost.exe',
    'msmpeng.exe', 'securityhealthservice.exe', 'ghelper.exe',
    'nvcontainer.exe', 'nvdisplay.container.exe'
)

function Write-Step($t) { Write-Host "  $t" -ForegroundColor Gray }
function Write-Ok($t)   { Write-Host "  [OK] $t" -ForegroundColor Green }
function Write-Warn2($t){ Write-Host "  [!]  $t" -ForegroundColor Yellow }
function Write-Err($t)  { Write-Host "  [X]  $t" -ForegroundColor Red }

function Test-DgpuWorks {
    # 只在需要时调用一次：探测独显会唤醒它，不能轮询
    $r = & nvidia-smi --query-gpu=name --format=csv,noheader 2>&1 | Out-String
    return ($LASTEXITCODE -eq 0 -and $r -match 'NVIDIA')
}

function Get-GpuProcesses {
    $list = @()
    $out = & nvidia-smi --query-compute-apps=pid,process_name --format=csv,noheader 2>&1
    foreach ($line in $out) {
        $parts = $line -split ','
        if ($parts.Count -ge 2) {
            $pidText = $parts[0].Trim()
            $name = Split-Path $parts[1].Trim() -Leaf
            if ($pidText -match '^\d+$' -and $PROTECTED -notcontains $name.ToLower()) {
                $list += [pscustomobject]@{ Pid = [int]$pidText; Name = $name }
            }
        }
    }
    return $list
}

function Invoke-Switch {
    param([bool]$Off)

    if (-not [Atk]::Open()) {
        Write-Err "打不开 \\.\ATKACPI ($([Atk]::LastError))"
        Write-Host "      需要 ASUS System Control Interface 驱动。" -ForegroundColor Yellow
        return $false
    }

    try {
        if ($Off) {
            # ---- 关独显前的准备（G-Helper 的必需步骤）----
            $procs = Get-GpuProcesses
            if ($procs.Count -gt 0) {
                Write-Step "结束占用独显的程序：$(($procs | ForEach-Object { $_.Name }) -join '、')"
                foreach ($p in $procs) {
                    & taskkill /F /PID $p.Pid 2>&1 | Out-Null
                }
                Start-Sleep -Milliseconds 1500
            } else {
                Write-Step "没有程序占用独显"
            }

            Write-Step "停止 NVIDIA 服务"
            foreach ($s in $NV_SERVICES) {
                & net stop $s 2>&1 | Out-Null
            }
            Start-Sleep -Milliseconds 1000
        }

        Write-Step "写入 ACPI：GPUEco = $(if ($Off) { 1 } else { 0 })"
        $result = [Atk]::Set($DEV_ECO, $(if ($Off) { 1 } else { 0 }))
        if ($result -ne 1) {
            Write-Err "固件拒绝了写入（result=$result）"
            if (-not $Off) { foreach ($s in $NV_SERVICES) { & net start $s 2>&1 | Out-Null } }
            return $false
        }
        Write-Ok "固件已接受"

        if (-not $Off) {
            Write-Step "启动 NVIDIA 服务"
            foreach ($s in $NV_SERVICES) { & net start $s 2>&1 | Out-Null }
        }

        $waitSec = if ($Off) { 10 } else { 14 }
        Write-Step "等待设备重新枚举（$waitSec 秒）…"
        Start-Sleep -Seconds $waitSec

        $works = Test-DgpuWorks
        if ($Off) {
            if (-not $works) { Write-Ok "独显已关闭（集显模式）"; return $true }
            Write-Warn2 "独显仍在工作——可能有程序正占用它（关掉游戏/录屏再试）"
            return $false
        } else {
            if ($works) { Write-Ok "独显已开启（标准模式）"; return $true }
            Write-Warn2 "独显还没被驱动接管。试试：热切换.ps1 fix（重启设备，需要管理员）"
            return $false
        }
    } finally {
        [Atk]::Close()
    }
}

function Show-Status {
    if (-not [Atk]::Open()) { Write-Err "打不开 \\.\ATKACPI ($([Atk]::LastError))"; return }
    try {
        $eco = [Atk]::Get($DEV_ECO)
        $mux = [Atk]::Get($DEV_MUX)
    } finally { [Atk]::Close() }

    $muxText = switch ($mux) { 0 { '集显模式' } 1 { '标准混合' } 2 { '独显直连' } default { "未知($mux)" } }
    $works = Test-DgpuWorks

    Write-Host ""
    Write-Host "  独显可用      : $(if ($works) { '是' } else { '否' })" -ForegroundColor $(if ($works) { 'Green' } else { 'Yellow' })
    Write-Host "  GPUEco 寄存器 : $eco   （本机型这个值不可信，写 1 后仍可能读回 0）" -ForegroundColor Gray
    Write-Host "  显示通路 MUX  : $mux（$muxText）" -ForegroundColor Gray
    Write-Host ""
    if ($works) {
        Write-Host "  当前是标准模式（独显可用）" -ForegroundColor Green
    } else {
        Write-Host "  当前独显不可用：要么在集显模式，要么设备处于异常状态" -ForegroundColor Yellow
        Write-Host "  如果刚切过集显模式，用 'on' 切回来；如果切不回来用 'fix'" -ForegroundColor Gray
    }
    Write-Host ""
}

function Repair-Device {
    # 重启显卡设备，让驱动重新接管（需要管理员）
    $id = (& pnputil /enum-devices /class Display 2>&1 |
        Select-String -Pattern 'PCI\\VEN_10DE\S+' -AllMatches |
        ForEach-Object { $_.Matches.Value } | Select-Object -First 1)

    if (-not $id) { Write-Err "没找到 NVIDIA 显示设备"; return }

    Write-Host ""
    Write-Host "  正在重启设备：$id" -ForegroundColor Cyan
    $out = & pnputil /restart-device "$id" 2>&1 | Out-String
    if ($out -match 'success|成功') {
        Write-Ok "设备已重启"
    } else {
        Write-Warn2 "重启设备返回：$($out.Trim())"
        Write-Host "      如果不是管理员，请用管理员身份重开这个窗口" -ForegroundColor Yellow
    }
    Start-Sleep -Seconds 5
    if (Test-DgpuWorks) { Write-Ok "独显已恢复可用" } else { Write-Warn2 "仍不可用，可能真的需要重启电脑" }
}

function Show-Gui {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing

    $form = New-Object System.Windows.Forms.Form
    $form.Text = '显卡热切换'
    $form.Size = New-Object System.Drawing.Size(420, 320)
    $form.StartPosition = 'CenterScreen'
    $form.BackColor = [System.Drawing.Color]::FromArgb(27, 27, 27)
    $form.ForeColor = [System.Drawing.Color]::White

    $title = New-Object System.Windows.Forms.Label
    $title.Text = '显卡热切换'
    $title.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 15, [System.Drawing.FontStyle]::Bold)
    $title.ForeColor = [System.Drawing.Color]::White
    $title.Location = New-Object System.Drawing.Point(24, 18)
    $title.Size = New-Object System.Drawing.Size(360, 34)
    $form.Controls.Add($title)

    $status = New-Object System.Windows.Forms.Label
    $status.Text = '选择要用哪块显卡'
    $status.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9)
    $status.ForeColor = [System.Drawing.Color]::FromArgb(160, 160, 160)
    $status.Location = New-Object System.Drawing.Point(24, 56)
    $status.Size = New-Object System.Drawing.Size(360, 44)
    $form.Controls.Add($status)

    $logBox = New-Object System.Windows.Forms.TextBox
    $logBox.Multiline = $true
    $logBox.ScrollBars = 'Vertical'
    $logBox.ReadOnly = $true
    $logBox.BackColor = [System.Drawing.Color]::FromArgb(20, 20, 20)
    $logBox.ForeColor = [System.Drawing.Color]::FromArgb(200, 200, 200)
    $logBox.Font = New-Object System.Drawing.Font('Consolas', 8)
    $logBox.Location = New-Object System.Drawing.Point(24, 176)
    $logBox.Size = New-Object System.Drawing.Size(356, 96)
    $form.Controls.Add($logBox)

    function New-Button($text, $y, $color, $script) {
        $b = New-Object System.Windows.Forms.Button
        $b.Text = $text
        $b.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 10, [System.Drawing.FontStyle]::Bold)
        $b.Location = New-Object System.Drawing.Point(24, $y)
        $b.Size = New-Object System.Drawing.Size(356, 44)
        $b.FlatStyle = 'Flat'
        $b.BackColor = $color
        $b.ForeColor = [System.Drawing.Color]::White
        $b.Add_Click($script)
        $form.Controls.Add($b)
        return $b
    }

    $btnOff = New-Button '用集显（关掉独显，省电）' 100 ([System.Drawing.Color]::FromArgb(47, 111, 159)) {
        $btnOff.Enabled = $false; $btnOn.Enabled = $false
        $status.Text = '正在切换到集显模式…'
        $form.Refresh()
        $ok = Invoke-Switch -Off $true  *>&1 | ForEach-Object { $logBox.AppendText("$_`r`n"); $form.Refresh() }
        $status.Text = if ($ok) { '已切到集显模式' } else { '切换未完全成功，看下面日志' }
        $btnOff.Enabled = $true; $btnOn.Enabled = $true
    }

    $btnOn = New-Button '用独显（开启独显，性能）' 148 ([System.Drawing.Color]::FromArgb(63, 143, 63)) {
        $btnOff.Enabled = $false; $btnOn.Enabled = $false
        $status.Text = '正在切换到标准模式…'
        $form.Refresh()
        $ok = Invoke-Switch -Off $false *>&1 | ForEach-Object { $logBox.AppendText("$_`r`n"); $form.Refresh() }
        $status.Text = if ($ok) { '已切到标准模式' } else { '切换未完全成功，看下面日志' }
        $btnOff.Enabled = $true; $btnOn.Enabled = $true
    }

    $fix = New-Object System.Windows.Forms.LinkLabel
    $fix.Text = '切不回来？点这里修复设备'
    $fix.Location = New-Object System.Drawing.Point(24, 280)
    $fix.Size = New-Object System.Drawing.Size(200, 20)
    $fix.LinkColor = [System.Drawing.Color]::FromArgb(120, 180, 220)
    $fix.Add_Click({
        $fix.Enabled = $false
        Repair-Device *>&1 | ForEach-Object { $logBox.AppendText("$_`r`n"); $form.Refresh() }
        $fix.Enabled = $true
    })
    $form.Controls.Add($fix)

    $form.TopMost = $true
    [void]$form.ShowDialog()
}

# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "  华硕显卡热切换" -ForegroundColor White
Write-Host "  ----------------------------------------" -ForegroundColor DarkGray

switch ($Action) {
    'on'     { $ok = Invoke-Switch -Off $false; exit $(if ($ok) { 0 } else { 1 }) }
    'off'    { $ok = Invoke-Switch -Off $true;  exit $(if ($ok) { 0 } else { 1 }) }
    'fix'    { Repair-Device }
    'gui'    { Show-Gui }
    default  { Show-Status }
}
