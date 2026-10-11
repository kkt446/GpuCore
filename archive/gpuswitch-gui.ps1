#Requires -Version 5.1
<#
    GPUSwitch —— 华硕显卡热切换 + 资源监视
    UI 与位置参照 G-Helper：窄长窗口停靠右下角，深色卡片 + 描边模式按钮。

    零依赖：只用 Windows 自带的 PowerShell + WinForms。

    关于"切集显后独显从设备管理器消失"：
    那是 EC 真的给独显断了电（省电模式的本质，G-Helper 的集显模式一样）。
    但本程序**不会**再去杀占用独显的进程、也不停 NVIDIA 服务——
    那些是早期版本的粗暴做法，会让驱动来不及优雅卸载，进而出现
    NVIDIA App 报"没有 N 卡"的状态。现在只做干净的 ACPI 切换。
#>

[CmdletBinding()]
param([switch]$Minimized)

$ErrorActionPreference = 'Continue'

# ===========================================================================
# ACPI
# ===========================================================================

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
        if (h == new IntPtr(-1)) { LastError = "winerr=" + Marshal.GetLastWin32Error(); return false; }
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
        byte[] a = new byte[8]; BitConverter.GetBytes(id).CopyTo(a, 0);
        int raw = BitConverter.ToInt32(Call(DSTS, a), 0);
        return raw == 0 ? -65536 : raw - 65536;
    }
    public static int Set(uint id, int v) {
        byte[] a = new byte[8]; BitConverter.GetBytes(id).CopyTo(a, 0); BitConverter.GetBytes((uint)v).CopyTo(a, 4);
        return BitConverter.ToInt32(Call(DEVS, a), 0);
    }
}
'@
if (-not ('Atk' -as [type])) { Add-Type -TypeDefinition $atkSource -ErrorAction Stop }

$DEV_ECO = [uint32]0x00090020
$DEV_MUX = [uint32]0x00090016

# 核显 LUID（Intel Graphics）。启动时自动探测。
$script:IgpuLuid = $null

# ===========================================================================
# 硬件
# ===========================================================================

function Invoke-Acpi {
    param([scriptblock]$Body)
    if (-not [Atk]::Open()) { return $null }
    try { return & $Body } finally { [Atk]::Close() }
}

function Test-DgpuWorks {
    $r = & nvidia-smi --query-gpu=name --format=csv,noheader 2>&1 | Out-String
    return ($LASTEXITCODE -eq 0 -and $r -match 'NVIDIA')
}

function Set-GpuPreference {
    param([int]$Preference)
    $key = 'HKCU:\Software\Microsoft\DirectX\UserGpuPreferences'
    try {
        if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }
        Set-ItemProperty -Path $key -Name 'GpuPreference' -Value "GpuPreference=$Preference;" -Type String -Force
        return ((Get-ItemProperty -Path $key -Name 'GpuPreference').GpuPreference -eq "GpuPreference=$Preference;")
    } catch { return $false }
}

function Detect-IgpuLuid {
    # 从 GPU Adapter Memory 计数器里找"没有独立显存"的那块 = 核显
    try {
        $s = (Get-Counter '\GPU Adapter Memory(*)\Dedicated Usage' -ErrorAction Stop).CounterSamples
        $zero = $s | Where-Object { $_.CookedValue -eq 0 }
        if ($zero) {
            $inst = $zero[0].InstanceName
            if ($inst -match 'luid_(0x[0-9a-f]+)_(0x[0-9a-f]+)') {
                return "$($Matches[1])_$($Matches[2])"
            }
        }
    } catch { }
    return '0x00000000_0x0001111f'
}

# ===========================================================================
# 资源监视
# ===========================================================================

function Get-Stats {
    $r = @{ Cpu = 0; RamUsed = 0; RamTotal = 0; Dgpu = $null; Igpu = 0
            VramUsed = 0; VramTotal = 0; Temp = $null }

    try {
        $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
        $r.RamTotal = [int64]$os.TotalVisibleMemorySize * 1024
        $r.RamUsed = $r.RamTotal - ([int64]$os.FreePhysicalMemory * 1024)
    } catch { }

    try {
        $r.Cpu = [math]::Round((Get-Counter '\Processor(_Total)\% Processor Time' -ErrorAction Stop).CounterSamples[0].CookedValue, 0)
    } catch { }

    # 核显：按 LUID 取最忙引擎
    try {
        if (-not $script:IgpuLuid) { $script:IgpuLuid = Detect-IgpuLuid }
        $s = (Get-Counter '\GPU Engine(*)\Utilization Percentage' -ErrorAction Stop).CounterSamples |
            Where-Object { $_.InstanceName -match [regex]::Escape($script:IgpuLuid) }
        if ($s) { $r.Igpu = [math]::Round(($s | Measure-Object CookedValue -Maximum).Maximum, 0) }
    } catch { }

    # 独显（没通电就跳过，避免唤醒它）
    if (Test-DgpuWorks) {
        $out = & nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits 2>&1
        $p = ($out | Select-Object -First 1) -split ','
        if ($p.Count -ge 3) {
            $r.Dgpu = [math]::Round([double]$p[0].Trim(), 0)
            $r.VramUsed = [int64]([double]$p[1].Trim() * 1MB)
            $r.VramTotal = [int64]([double]$p[2].Trim() * 1MB)
        }
    }

    # CPU 温度走华硕 ACPI
    $t = Invoke-Acpi { [Atk]::Get([uint32]0x00120094) }
    if ($null -ne $t -and $t -gt 0 -and $t -lt 120) { $r.Temp = $t }

    return $r
}

# ===========================================================================
# 切换（干净版：只写 ACPI + 首选项，不杀进程不停服务）
# ===========================================================================

function Switch-Eco {
    param([bool]$Off, [scriptblock]$Log)
    $say = { param($t) if ($Log) { & $Log $t } }

    & $say "写入 ACPI：GPUEco = $(if ($Off) { 1 } else { 0 })"
    $res = Invoke-Acpi { [Atk]::Set($DEV_ECO, $(if ($Off) { 1 } else { 0 })) }
    if ($res -ne 1) { & $say "固件拒绝写入（result=$res）"; return $false }
    & $say "固件已接受，等待设备重新枚举…"

    Start-Sleep -Seconds $(if ($Off) { 8 } else { 12 })
    $works = Test-DgpuWorks

    if ($Off) {
        if ($works) { & $say "独显仍在工作（可能有程序占用它）"; return $false }
        Set-GpuPreference -Preference 1 | Out-Null
        & $say "已切到集显模式"
        return $true
    } else {
        if (-not $works) { & $say "独显未被驱动接管，试试修复显示设备"; return $false }
        Set-GpuPreference -Preference 2 | Out-Null
        & $say "已切到标准模式（新程序用独显）"
        return $true
    }
}

function Switch-Mux {
    param([int]$Value, [scriptblock]$Log)
    $say = { param($t) if ($Log) { & $Log $t } }
    $cur = Invoke-Acpi { [Atk]::Get($DEV_MUX) }
    if ($cur -eq $Value) { & $say "显示通路已经是 $Value"; return $true }
    if ($Value -eq 0 -and (Test-DgpuWorks)) { & $say "独显在工作，不能切集显输出"; return $false }

    & $say "写入 GPUMux = $Value（可能黑屏数秒）"
    $res = Invoke-Acpi { [Atk]::Set($DEV_MUX, $Value) }
    if ($res -ne 1) { & $say "固件拒绝 MUX 写入"; return $false }
    & $say "已接受，等待显示通路重新枚举…"
    Start-Sleep -Seconds 12
    return $true
}

# ===========================================================================
# 界面
# ===========================================================================

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$script:Busy = $false
$C_BG     = [System.Drawing.Color]::FromArgb(25, 25, 25)
$C_CARD   = [System.Drawing.Color]::FromArgb(32, 32, 32)
$C_BTN    = [System.Drawing.Color]::FromArgb(38, 38, 38)
$C_BORDER = [System.Drawing.Color]::FromArgb(58, 58, 58)
$C_TEXT   = [System.Drawing.Color]::FromArgb(240, 240, 240)
$C_DIM    = [System.Drawing.Color]::FromArgb(165, 165, 165)
$C_ACCENT = [System.Drawing.Color]::FromArgb(0, 163, 238)
$C_GREEN  = [System.Drawing.Color]::FromArgb(118, 185, 0)
$C_CYAN   = [System.Drawing.Color]::FromArgb(0, 163, 238)
$C_ORANGE = [System.Drawing.Color]::FromArgb(230, 126, 34)
$C_PURPLE = [System.Drawing.Color]::FromArgb(130, 130, 165)

$W = 392   # 窗口宽（对齐 G-Helper 的窄长条）

function New-Card {
    param($Parent, $Title, $Y, $Height, $ShowTitle = $true)
    $panel = New-Object System.Windows.Forms.Panel
    $panel.Location = New-Object System.Drawing.Point(8, $Y)
    $panel.Size = New-Object System.Drawing.Size(($W - 24), $Height)
    $panel.BackColor = $C_CARD
    if ($ShowTitle) {
        $l = New-Object System.Windows.Forms.Label
        $l.Text = $Title
        $l.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9, [System.Drawing.FontStyle]::Bold)
        $l.ForeColor = $C_TEXT
        $l.Location = New-Object System.Drawing.Point(8, 6)
        $l.Size = New-Object System.Drawing.Size(220, 20)
        $panel.Controls.Add($l)
    }
    $Parent.Controls.Add($panel)
    return $panel
}

function New-ModeBtn {
    param($Parent, $Text, $X, $Y, $Wid, $Color, $OnClick)
    $b = New-Object System.Windows.Forms.Button
    $b.Text = $Text
    $b.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 8)
    $b.Location = New-Object System.Drawing.Point($X, $Y)
    $b.Size = New-Object System.Drawing.Size($Wid, 44)
    $b.FlatStyle = 'Flat'
    $b.FlatAppearance.BorderSize = 2
    $b.FlatAppearance.BorderColor = $C_BORDER
    $b.BackColor = $C_BTN
    $b.ForeColor = $C_TEXT
    $b.Tag = $Color
    $b.Add_Click($OnClick)
    $Parent.Controls.Add($b)
    return $b
}

function Set-Highlight {
    param($Buttons, $Index)
    for ($i = 0; $i -lt $Buttons.Count; $i++) {
        $b = $Buttons[$i]
        if ($i -eq $Index) {
            $b.FlatAppearance.BorderColor = $b.Tag
            $b.BackColor = [System.Drawing.Color]::FromArgb(48, 48, 48)
            $b.ForeColor = [System.Drawing.Color]::White
        } else {
            $b.FlatAppearance.BorderColor = $C_BORDER
            $b.BackColor = $C_BTN
            $b.ForeColor = $C_DIM
        }
    }
}

function New-Stat {
    param($Parent, $Label, $Y, $Color)
    $cap = New-Object System.Windows.Forms.Label
    $cap.Text = $Label
    $cap.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 8)
    $cap.ForeColor = $C_DIM
    $cap.Location = New-Object System.Drawing.Point(8, $Y)
    $cap.Size = New-Object System.Drawing.Size(110, 16)
    $Parent.Controls.Add($cap)

    $val = New-Object System.Windows.Forms.Label
    $val.Text = '--'
    $val.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 8)
    $val.ForeColor = $C_TEXT
    $val.TextAlign = 'MiddleRight'
    $val.Location = New-Object System.Drawing.Point(110, $Y)
    $val.Size = New-Object System.Drawing.Size(242, 16)
    $Parent.Controls.Add($val)

    $track = New-Object System.Windows.Forms.Panel
    $track.Location = New-Object System.Drawing.Point(8, ($Y + 18))
    $track.Size = New-Object System.Drawing.Size(344, 5)
    $track.BackColor = [System.Drawing.Color]::FromArgb(50, 50, 50)
    $Parent.Controls.Add($track)

    $fill = New-Object System.Windows.Forms.Panel
    $fill.Location = New-Object System.Drawing.Point(0, 0)
    $fill.Size = New-Object System.Drawing.Size(0, 5)
    $fill.BackColor = $Color
    $track.Controls.Add($fill)

    return [pscustomobject]@{ Value = $val; Fill = $fill; Max = 344 }
}

function Set-Stat {
    param($Stat, $Percent, $Text)
    if ($null -ne $Text) { $Stat.Value.Text = $Text }
    $p = [math]::Max(0, [math]::Min(100, [double]$Percent))
    $Stat.Fill.Width = [int]($Stat.Max * $p / 100)
}

# --- 窗口 ---
$form = New-Object System.Windows.Forms.Form
$form.Text = 'GPUSwitch'
$form.ClientSize = New-Object System.Drawing.Size($W, 600)
$form.FormBorderStyle = 'FixedSingle'
$form.MaximizeBox = $false
$form.BackColor = $C_BG
$form.ForeColor = $C_TEXT
$form.StartPosition = 'Manual'
$form.TopMost = $true

$wa = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
$form.Location = New-Object System.Drawing.Point(
    ($wa.Right - $form.Width - 6), ($wa.Bottom - $form.Height - 6))

# --- 卡片 1：当前状态 ---
$card1 = New-Card $form '' 6 88 $false
$lblModel = New-Object System.Windows.Forms.Label
$lblModel.Text = '读取中…'
$lblModel.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 8)
$lblModel.ForeColor = $C_DIM
$lblModel.Location = New-Object System.Drawing.Point(10, 8)
$lblModel.Size = New-Object System.Drawing.Size(344, 16)
$card1.Controls.Add($lblModel)

$lblMode = New-Object System.Windows.Forms.Label
$lblMode.Text = '—'
$lblMode.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 15, [System.Drawing.FontStyle]::Bold)
$lblMode.ForeColor = $C_ACCENT
$lblMode.Location = New-Object System.Drawing.Point(10, 26)
$lblMode.Size = New-Object System.Drawing.Size(344, 30)
$card1.Controls.Add($lblMode)

$lblInfo = New-Object System.Windows.Forms.Label
$lblInfo.Text = ''
$lblInfo.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 8)
$lblInfo.ForeColor = $C_DIM
$lblInfo.Location = New-Object System.Drawing.Point(10, 60)
$lblInfo.Size = New-Object System.Drawing.Size(344, 22)
$card1.Controls.Add($lblInfo)

# --- 卡片 2：资源监视 ---
$card2 = New-Card $form '资源监视' 100 148
$statDgpu = New-Stat $card2 '独显 GPU' 26 $C_GREEN
$statIgpu = New-Stat $card2 '核显 GPU' 56 $C_CYAN
$statRam  = New-Stat $card2 '内存'     86 ([System.Drawing.Color]::FromArgb(224, 163, 46))
$statCpu  = New-Stat $card2 'CPU'     116 ([System.Drawing.Color]::FromArgb(56, 178, 107))

# --- 卡片 3：显卡模式 ---
$card3 = New-Card $form '显卡模式' 254 132
$script:ModeBtns = @()
$bw = 112
$ModeBtns += New-ModeBtn $card3 '集显模式' 8   28 $bw $C_CYAN {
    if ($script:Busy) { return }
    $script:Busy = $true; $lblInfo.Text = '正在切换到集显模式…'; $form.Refresh()
    $r = Switch-Eco -Off $true -Log { param($t) $lblInfo.Text = $t; $form.Refresh() }
    if ($r) { $lblInfo.Text = '✓ 已切到集显模式' }
    $script:Busy = $false; Start-Sleep -Seconds 2; Update-Ui
}
$ModeBtns += New-ModeBtn $card3 '标准模式' 124 28 $bw $C_GREEN {
    if ($script:Busy) { return }
    $script:Busy = $true; $lblInfo.Text = '正在切换到标准模式…'; $form.Refresh()
    $r = Switch-Eco -Off $false -Log { param($t) $lblInfo.Text = $t; $form.Refresh() }
    if ($r) { $lblInfo.Text = '✓ 已切到标准模式' }
    $script:Busy = $false; Start-Sleep -Seconds 2; Update-Ui
}
$ModeBtns += New-ModeBtn $card3 '独显直连' 240 28 $bw $C_ORANGE {
    if ($script:Busy) { return }
    $a = [System.Windows.Forms.MessageBox]::Show(
        "独显直连会把屏幕输出切到独显，性能最高。`n`n· 屏幕会黑屏数秒`n· 需要重启才完全生效`n`n确定继续吗？",
        '确认', 'YesNo', 'Warning')
    if ($a -ne 'Yes') { return }
    $script:Busy = $true; $lblInfo.Text = '正在切到独显直连…'; $form.Refresh()
    if (-not (Test-DgpuWorks)) { Switch-Eco -Off $false -Log { param($t) $lblInfo.Text = $t; $form.Refresh() } | Out-Null }
    Switch-Mux -Value 2 -Log { param($t) $lblInfo.Text = $t; $form.Refresh() } | Out-Null
    Set-GpuPreference -Preference 2 | Out-Null
    $lblInfo.Text = '✓ 已切到独显直连 —— 请重启电脑'
    $script:Busy = $false; Start-Sleep -Seconds 2; Update-Ui
}
$ModeBtns += New-ModeBtn $card3 '标准混合' 8 80 168 $C_PURPLE {
    if ($script:Busy) { return }
    $a = [System.Windows.Forms.MessageBox]::Show(
        "从独显直连切回标准混合，需要重启才完全生效。`n`n确定继续吗？", '确认', 'YesNo', 'Warning')
    if ($a -ne 'Yes') { return }
    $script:Busy = $true; $lblInfo.Text = '正在切回标准混合…'; $form.Refresh()
    if (-not (Test-DgpuWorks)) { Switch-Eco -Off $false -Log { param($t) $lblInfo.Text = $t; $form.Refresh() } | Out-Null }
    Switch-Mux -Value 1 -Log { param($t) $lblInfo.Text = $t; $form.Refresh() } | Out-Null
    Set-GpuPreference -Preference 2 | Out-Null
    $lblInfo.Text = '✓ 已切回标准混合 —— 建议重启'
    $script:Busy = $false; Start-Sleep -Seconds 2; Update-Ui
}
$ModeBtns += New-ModeBtn $card3 '刷新状态' 184 80 168 $C_BORDER { Update-Ui }

# --- 卡片 4：日志与操作 ---
$card4 = New-Card $form '' 394 200 $false
$logBox = New-Object System.Windows.Forms.TextBox
$logBox.Multiline = $true
$logBox.ScrollBars = 'Vertical'
$logBox.ReadOnly = $true
$logBox.BackColor = [System.Drawing.Color]::FromArgb(28, 28, 28)
$logBox.ForeColor = [System.Drawing.Color]::FromArgb(180, 180, 180)
$logBox.BorderStyle = 'None'
$logBox.Font = New-Object System.Drawing.Font('Consolas', 8)
$logBox.Location = New-Object System.Drawing.Point(8, 8)
$logBox.Size = New-Object System.Drawing.Size(344, 152)
$card4.Controls.Add($logBox)

$btnFix = New-Object System.Windows.Forms.LinkLabel
$btnFix.Text = '切不回来？修复显示设备'
$btnFix.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 8)
$btnFix.Location = New-Object System.Drawing.Point(8, 168)
$btnFix.Size = New-Object System.Drawing.Size(180, 18)
$btnFix.LinkColor = [System.Drawing.Color]::FromArgb(120, 180, 220)
$btnFix.Add_Click({
    $btnFix.Enabled = $false
    $logBox.AppendText("正在重启显示设备…`r`n")
    $id = (& pnputil /enum-devices /class Display 2>&1 |
        Select-String -Pattern 'PCI\\VEN_10DE\S+' -AllMatches |
        ForEach-Object { $_.Matches.Value } | Select-Object -First 1)
    if ($id) {
        $out = & pnputil /restart-device "$id" 2>&1 | Out-String
        $logBox.AppendText("  $(if ($out -match 'success|成功') { '成功' } else { $out.Trim() })`r`n")
    } else {
        $logBox.AppendText("  没找到 NVIDIA 设备`r`n")
    }
    $btnFix.Enabled = $true
    Start-Sleep -Seconds 2
    Update-Ui
})
$card4.Controls.Add($btnFix)

$btnTop = New-Object System.Windows.Forms.Button
$btnTop.Text = '置顶'
$btnTop.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 8)
$btnTop.Location = New-Object System.Drawing.Point(264, 166)
$btnTop.Size = New-Object System.Drawing.Size(40, 22)
$btnTop.FlatStyle = 'Flat'
$btnTop.BackColor = $C_ACCENT
$btnTop.ForeColor = [System.Drawing.Color]::White
$btnTop.Add_Click({
    $form.TopMost = -not $form.TopMost
    $btnTop.BackColor = if ($form.TopMost) { $C_ACCENT } else { $C_BTN }
})
$card4.Controls.Add($btnTop)

$btnHide = New-Object System.Windows.Forms.Button
$btnHide.Text = '隐藏'
$btnHide.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 8)
$btnHide.Location = New-Object System.Drawing.Point(310, 166)
$btnHide.Size = New-Object System.Drawing.Size(40, 22)
$btnHide.FlatStyle = 'Flat'
$btnHide.BackColor = $C_BTN
$btnHide.ForeColor = $C_DIM
$btnHide.Add_Click({ $form.Hide() })
$card4.Controls.Add($btnHide)

# ===========================================================================
# 刷新
# ===========================================================================

function Update-Ui {
    try {
        $cs = Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue
        $lblModel.Text = "华硕 " + ($cs.Model -replace '_', ' ')
    } catch { $lblModel.Text = '华硕笔记本' }

    $mux = Invoke-Acpi { [Atk]::Get($DEV_MUX) }
    $works = Test-DgpuWorks

    if (-not $works) { $mode = '集显模式'; $idx = 0 }
    elseif ($mux -eq 2) { $mode = '独显直连'; $idx = 2 }
    elseif ($mux -eq 0) { $mode = '集显模式'; $idx = 0 }
    else { $mode = '标准模式'; $idx = 1 }

    $lblMode.Text = $mode
    Set-Highlight -Buttons $ModeBtns -Index $idx

    if ($works) {
        $lblInfo.Text = "● 独显已通电可用    ·    MUX=$mux"
        $lblInfo.ForeColor = $C_GREEN
    } else {
        $lblInfo.Text = "○ 独显已断电（省电）    ·    MUX=$mux"
        $lblInfo.ForeColor = $C_ORANGE
    }

    $s = Get-Stats
    if ($null -ne $s.Dgpu) {
        $vram = if ($s.VramTotal -gt 0) { "  {0:N1}/{1:N1}G" -f ($s.VramUsed / 1GB), ($s.VramTotal / 1GB) } else { '' }
        Set-Stat -Stat $statDgpu -Percent $s.Dgpu -Text "$($s.Dgpu)%$vram"
    } else {
        Set-Stat -Stat $statDgpu -Percent 0 -Text '已断电'
    }
    Set-Stat -Stat $statIgpu -Percent $s.Igpu -Text "$($s.Igpu)%"
    $ramPct = if ($s.RamTotal -gt 0) { $s.RamUsed * 100 / $s.RamTotal } else { 0 }
    Set-Stat -Stat $statRam -Percent $ramPct -Text ("{0:N1}/{1:N1} GB" -f ($s.RamUsed / 1GB), ($s.RamTotal / 1GB))
    $cpuText = "$($s.Cpu)%"
    if ($null -ne $s.Temp) { $cpuText += "    $($s.Temp)℃" }
    Set-Stat -Stat $statCpu -Percent $s.Cpu -Text $cpuText
}

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 1500
$timer.Add_Tick({ if (-not $script:Busy) { Update-Ui } })
$timer.Start()

$form.Add_Shown({
    $script:IgpuLuid = Detect-IgpuLuid
    $logBox.AppendText("GPUSwitch 就绪`r`n")
    $logBox.AppendText("核显 LUID: $script:IgpuLuid`r`n")
    $logBox.AppendText("模式按钮直接做硬件切换，切换期间别关窗口。`r`n`r`n")
    Update-Ui
})

# 托盘图标：隐藏后还能叫回来
$tray = New-Object System.Windows.Forms.NotifyIcon
try {
    $tray.Icon = [System.Drawing.SystemIcons]::Application
    $tray.Text = 'GPUSwitch 显卡热切换'
    $tray.Visible = $true
    $tray.Add_MouseDoubleClick({ $form.Show(); $form.WindowState = 'Normal'; $form.Activate() })
} catch { }

if ($Minimized) { $form.WindowState = 'Minimized' }
[void]$form.ShowDialog()
$timer.Stop()
try { $tray.Visible = $false; $tray.Dispose() } catch { }
