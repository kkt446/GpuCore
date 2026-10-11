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
    [ValidateSet('on', 'off', 'status', 'gui', 'fix', 'direct', 'standard')]
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

function Set-GpuPreference {
    <#
        写全局显卡首选项，决定新启动的程序默认用哪块卡。

        为什么必须做这一步：把独显"通电"只代表它可用，程序默认还是走
        核显渲染。Windows 在程序启动时读
        HKCU\...\UserGpuPreferences 里的 "GpuPreference" 值
        （不带 exe 路径的那条 = 全局默认）：
            1 = 省电（核显）   2 = 高性能（独显）
        这与「设置 > 系统 > 屏幕 > 显示卡」写的是同一个位置。

        注意：已经在运行的程序不会被改变，要重启该程序才生效。
    #>
    param([int]$Preference)

    $key = 'HKCU:\Software\Microsoft\DirectX\UserGpuPreferences'
    try {
        if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }
        Set-ItemProperty -Path $key -Name 'GpuPreference' `
            -Value "GpuPreference=$Preference;" -Type String -Force
        $after = (Get-ItemProperty -Path $key -Name 'GpuPreference').GpuPreference
        if ($after -eq "GpuPreference=$Preference;") {
            $label = if ($Preference -eq 2) { '独显（高性能）' } else { '核显（省电）' }
            Write-Ok "全局显卡首选项已设为 $label"
            return $true
        }
        Write-Warn2 "写入后读回不一致：$after"
        return $false
    } catch {
        Write-Warn2 "写显卡首选项失败：$($_.Exception.Message)"
        Write-Step "可手动去「设置 > 系统 > 屏幕 > 显示卡」设为 高性能"
        return $false
    }
}

function Get-GpuPreference {
    $key = 'HKCU:\Software\Microsoft\DirectX\UserGpuPreferences'
    try {
        $v = (Get-ItemProperty -Path $key -Name 'GpuPreference' -ErrorAction Stop).GpuPreference
        if ($v -match '(\d+)') { return [int]$Matches[1] }
    } catch { }
    return $null
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

        # ---- 关键一步：设置全局显卡首选项 ----
        # 独显通电只代表"可用"，程序默认仍走核显渲染。要让程序真的用独显，
        # 必须把全局首选项写成 高性能（2）；切集显时写回省电（1）。
        if ($Off) {
            Write-Step "设置全局显卡首选项为 核显（省电）"
            [void](Set-GpuPreference -Preference 1)
        } elseif ($works) {
            Write-Step "设置全局显卡首选项为 独显（高性能）"
            [void](Set-GpuPreference -Preference 2)
        }

        if ($Off) {
            if (-not $works) { Write-Ok "独显已关闭（集显模式）"; return $true }
            Write-Warn2 "独显仍在工作——可能有程序正占用它（关掉游戏/录屏再试）"
            return $false
        } else {
            if ($works) {
                Write-Ok "独显已开启（标准模式）"
                Write-Step "新启动的程序会用独显渲染；已在运行的程序要重启才生效"
                return $true
            }
            Write-Warn2 "独显还没被驱动接管。试试：gpuswitch.cmd fix（需管理员）"
            return $false
        }
    } finally {
        [Atk]::Close()
    }
}

function Set-Mux {
    <#
        切换显示通路（MUX）：
            0 = 集显模式（屏幕由核显输出）
            1 = 标准混合（核显输出 + 独显渲染）
            2 = 独显直连（屏幕直接由独显驱动，性能最高）

        独显直连会把屏幕的物理输出切到独显上，切换过程黑屏数秒，
        并且通常需要重启才能完全生效。这是硬件行为，不是缺陷。
    #>
    param([int]$Value)

    if (-not [Atk]::Open()) { Write-Err "打不开 \\.\ATKACPI"; return $false }
    try {
        $current = [Atk]::Get($DEV_MUX)
        if ($current -eq $Value) {
            Write-Step "显示通路已经是 $Value，无需切换"
            return $true
        }

        # 安全闸：屏幕由独显驱动时绝不能关独显
        if ($Value -eq 0 -and (Test-DgpuWorks)) {
            Write-Warn2 "独显当前在工作，不能直接切到集显输出"
            Write-Step "请先执行 'off' 关闭独显，再切集显模式"
            return $false
        }

        Write-Step "写入 ACPI：GPUMux = $Value（可能黑屏数秒）"
        $result = [Atk]::Set($DEV_MUX, $Value)
        if ($result -ne 1) {
            Write-Err "固件拒绝了 MUX 写入（result=$result）"
            return $false
        }
        Write-Ok "固件已接受"

        Write-Step "等待显示通路重新枚举（12 秒，屏幕可能闪黑）…"
        Start-Sleep -Seconds 12
        Write-Step "读回 GPUMux = $([Atk]::Get($DEV_MUX))"
        return $true
    } finally {
        [Atk]::Close()
    }
}

function Invoke-SwitchToDirect {
    <#
        切到独显直连：确保独显通电后把显示通路切到独显。
        性能最高的一档，但会黑屏并需要重启。
    #>
    Write-Host ""
    Write-Host "  即将切到「独显直连」" -ForegroundColor Yellow
    Write-Host "  ----------------------------------------" -ForegroundColor DarkGray
    Write-Host "  * 屏幕会黑屏数秒后恢复" -ForegroundColor Yellow
    Write-Host "  * 需要重启电脑才能完全生效" -ForegroundColor Yellow
    Write-Host "  * 重启后整机由独显驱动，最费电" -ForegroundColor Yellow
    Write-Host ""

    if (-not (Test-DgpuWorks)) {
        Write-Step "独显未通电，先开启它"
        if (-not (Invoke-Switch -Off $false)) {
            Write-Err "独显没能开启，无法切独显直连"
            return $false
        }
    }

    if (-not (Set-Mux -Value 2)) { return $false }

    Write-Step "设置全局显卡首选项为 独显（高性能）"
    [void](Set-GpuPreference -Preference 2)

    Write-Ok "已切到独显直连"
    Write-Host ""
    Write-Host "  >>> 现在请重启电脑，重启后整机由独显驱动 <<<" -ForegroundColor Cyan
    Write-Host "  想切回混合模式：重启后执行 gpuswitch.cmd standard" -ForegroundColor Gray
    Write-Host ""
    return $true
}

function Invoke-SwitchToStandard {
    # 从独显直连切回标准混合：写 MUX=1，需要重启
    Write-Host ""
    Write-Host "  即将切回「标准混合」" -ForegroundColor Yellow
    Write-Host "  * 需要重启电脑才能完全生效" -ForegroundColor Yellow
    Write-Host ""

    if (-not [Atk]::Open()) { Write-Err "打不开 \\.\ATKACPI"; return $false }
    try {
        $mux = [Atk]::Get($DEV_MUX)
        if ($mux -eq 1) {
            Write-Step "显示通路已经是标准混合"
        } else {
            # 切回前必须确保独显通电，否则没有渲染设备
            if (-not (Test-DgpuWorks)) {
                Write-Step "先开启独显"
                [void]([Atk]::Set($DEV_ECO, 0))
                foreach ($s in $NV_SERVICES) { & net start $s 2>&1 | Out-Null }
                Start-Sleep -Seconds 6
            }
            if (-not (Set-Mux -Value 1)) { return $false }
        }
    } finally {
        [Atk]::Close()
    }

    Write-Step "设置全局显卡首选项为 独显（高性能）"
    [void](Set-GpuPreference -Preference 2)

    Write-Ok "已切回标准混合"
    Write-Host ""
    Write-Host "  >>> 建议重启电脑让显示通路完全生效 <<<" -ForegroundColor Cyan
    Write-Host ""
    return $true
}

function Show-Status {
    if (-not [Atk]::Open()) { Write-Err "打不开 \\.\ATKACPI ($([Atk]::LastError))"; return }
    try {
        $eco = [Atk]::Get($DEV_ECO)
        $mux = [Atk]::Get($DEV_MUX)
    } finally { [Atk]::Close() }

    $muxText = switch ($mux) { 0 { '集显模式' } 1 { '标准混合' } 2 { '独显直连' } default { "未知($mux)" } }
    $works = Test-DgpuWorks
    $pref = Get-GpuPreference
    $prefText = switch ($pref) {
        1 { '核显（省电）' }
        2 { '独显（高性能）' }
        0 { '系统自动' }
        default { '未设置（系统自动）' }
    }

    Write-Host ""
    Write-Host "  独显可用      : $(if ($works) { '是' } else { '否' })" -ForegroundColor $(if ($works) { 'Green' } else { 'Yellow' })
    Write-Host "  显卡首选项    : $prefText   <- 决定新启动的程序用哪块卡" -ForegroundColor $(if ($pref -eq 2) { 'Green' } else { 'Gray' })
    Write-Host "  GPUEco 寄存器 : $eco   （本机型这个值不可信，写 1 后仍可能读回 0）" -ForegroundColor Gray
    Write-Host "  显示通路 MUX  : $mux（$muxText）" -ForegroundColor Gray
    Write-Host ""
    if ($works -and $pref -eq 2) {
        Write-Host "  独显可用，且新程序默认用独显 —— 已经在跑的程序需要重启才会换卡" -ForegroundColor Green
    } elseif ($works -and $pref -ne 2) {
        Write-Host "  独显可用，但首选项还是省电 —— 程序仍会用核显渲染" -ForegroundColor Yellow
        Write-Host "  执行 'on' 会把首选项改成独显" -ForegroundColor Gray
    } else {
        Write-Host "  当前独显不可用：要么在集显模式，要么设备处于异常状态" -ForegroundColor Yellow
        Write-Host "  如果刚切过集显模式，用 'on' 切回来；切不回来用 'fix'（需管理员）" -ForegroundColor Gray
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
    $form.Size = New-Object System.Drawing.Size(420, 450)
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
    $logBox.Location = New-Object System.Drawing.Point(24, 268)
    $logBox.Size = New-Object System.Drawing.Size(356, 110)
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
        $btnOff.Enabled = $false; $btnOn.Enabled = $false; $btnDirect.Enabled = $false; $btnStd.Enabled = $false
        $status.Text = '正在切换到集显模式…'
        $form.Refresh()
        $ok = Invoke-Switch -Off $true  *>&1 | ForEach-Object { $logBox.AppendText("$_`r`n"); $form.Refresh() }
        $status.Text = if ($ok) { '已切到集显模式' } else { '切换未完全成功，看下面日志' }
        $btnOff.Enabled = $true; $btnOn.Enabled = $true; $btnDirect.Enabled = $true; $btnStd.Enabled = $true
    }

    $btnOn = New-Button '用独显（开启独显，性能）' 148 ([System.Drawing.Color]::FromArgb(63, 143, 63)) {
        $btnOff.Enabled = $false; $btnOn.Enabled = $false; $btnDirect.Enabled = $false; $btnStd.Enabled = $false
        $status.Text = '正在切换到标准模式…'
        $form.Refresh()
        $ok = Invoke-Switch -Off $false *>&1 | ForEach-Object { $logBox.AppendText("$_`r`n"); $form.Refresh() }
        $status.Text = if ($ok) { '已切到标准模式（新程序会用独显）' } else { '切换未完全成功，看下面日志' }
        $btnOff.Enabled = $true; $btnOn.Enabled = $true; $btnDirect.Enabled = $true; $btnStd.Enabled = $true
    }

    $btnDirect = New-Button '独显直连（性能最高，黑屏+需重启）' 196 ([System.Drawing.Color]::FromArgb(150, 90, 40)) {
        $answer = [System.Windows.Forms.MessageBox]::Show(
            "独显直连会把屏幕的物理输出切到独显，性能最高。`n`n" +
            "· 屏幕会黑屏数秒后恢复`n" +
            "· 需要重启电脑才能完全生效`n" +
            "· 重启后整机由独显驱动，最费电`n`n" +
            "确定继续吗？",
            '确认切到独显直连',
            [System.Windows.Forms.MessageBoxButtons]::YesNo,
            [System.Windows.Forms.MessageBoxIcon]::Warning)
        if ($answer -ne 'Yes') { $status.Text = '已取消'; return }

        $btnOff.Enabled = $false; $btnOn.Enabled = $false; $btnDirect.Enabled = $false; $btnStd.Enabled = $false
        $status.Text = '正在切到独显直连（可能黑屏）…'
        $form.Refresh()
        $ok = Invoke-SwitchToDirect *>&1 | ForEach-Object { $logBox.AppendText("$_`r`n"); $form.Refresh() }
        $status.Text = if ($ok) { '已切到独显直连 —— 请重启电脑' } else { '切换未完全成功，看下面日志' }
        $btnOff.Enabled = $true; $btnOn.Enabled = $true; $btnDirect.Enabled = $true; $btnStd.Enabled = $true
    }

    $btnStd = New-Button '切回标准混合（从独显直连回来）' 244 ([System.Drawing.Color]::FromArgb(80, 80, 100)) {
        $answer = [System.Windows.Forms.MessageBox]::Show(
            "从独显直连切回标准混合，需要重启电脑才能完全生效。`n`n确定继续吗？",
            '确认切回标准混合',
            [System.Windows.Forms.MessageBoxButtons]::YesNo,
            [System.Windows.Forms.MessageBoxIcon]::Warning)
        if ($answer -ne 'Yes') { $status.Text = '已取消'; return }

        $btnOff.Enabled = $false; $btnOn.Enabled = $false; $btnDirect.Enabled = $false; $btnStd.Enabled = $false
        $status.Text = '正在切回标准混合…'
        $form.Refresh()
        $ok = Invoke-SwitchToStandard *>&1 | ForEach-Object { $logBox.AppendText("$_`r`n"); $form.Refresh() }
        $status.Text = if ($ok) { '已切回标准混合 —— 建议重启' } else { '切换未完全成功，看下面日志' }
        $btnOff.Enabled = $true; $btnOn.Enabled = $true; $btnDirect.Enabled = $true; $btnStd.Enabled = $true
    }

    $fix = New-Object System.Windows.Forms.LinkLabel
    $fix.Text = '切不回来？点这里修复设备'
    $fix.Location = New-Object System.Drawing.Point(24, 386)
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
    'on'       { $ok = Invoke-Switch -Off $false; exit $(if ($ok) { 0 } else { 1 }) }
    'off'      { $ok = Invoke-Switch -Off $true;  exit $(if ($ok) { 0 } else { 1 }) }
    'direct'   { $ok = Invoke-SwitchToDirect;     exit $(if ($ok) { 0 } else { 1 }) }
    'standard' { $ok = Invoke-SwitchToStandard;   exit $(if ($ok) { 0 } else { 1 }) }
    'fix'      { Repair-Device }
    'gui'      { Show-Gui }
    default    { Show-Status }
}
