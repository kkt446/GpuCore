
from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass, field
from typing import List, Optional

from . import branding

OK, WARN, FAIL, INFO = "OK", "WARN", "FAIL", "INFO"
_MARK = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", INFO: "[ -- ]"}


@dataclass
class Item:
    name: str
    level: str
    detail: str = ""


@dataclass
class Report:
    items: List[Item] = field(default_factory=list)

    def add(self, name: str, level: str, detail: str = "") -> Item:
        it = Item(name, level, detail)
        self.items.append(it)
        return it

    @property
    def failed(self) -> List[Item]:
        return [i for i in self.items if i.level == FAIL]

    @property
    def warned(self) -> List[Item]:
        return [i for i in self.items if i.level == WARN]

def _check_environment(rep: Report) -> None:
    v = sys.version_info
    rep.add("Python 版本", OK if v >= (3, 9) else FAIL,
            f"{v.major}.{v.minor}.{v.micro} @ {sys.executable}")

    try:
        import tkinter
        rep.add("tkinter", OK, "可用")
    except Exception as e:
        rep.add("tkinter", FAIL, f"不可用（装 Python 时要勾 tcl/tk）：{e}")

    rep.add("平台", OK if sys.platform.startswith("win") else FAIL, sys.platform)

    try:
        is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        is_admin = False
    rep.add("管理员权限", INFO,
            "是" if is_admin else "否（本程序不需要管理员）")


def _check_acpi(rep: Report) -> Optional[object]:
    from .app import AtkAcpi
    from .hardware import ECO_CANDIDATES, MUX_CANDIDATES, SENSOR_DEVICES, probe_devices

    acpi = AtkAcpi()
    if not acpi.open():
        rep.add("\\\\.\\ATKACPI 通道", WARN,
                "打不开 —— 这台机器不是华硕，或缺少 ASUS 驱动（装 G-Helper 或 MyASUS 会带上）。"
                "此版本固件相关功能在本机不可用。")
        return None
    rep.add("\\\\.\\ATKACPI 通道", OK, "可打开（普通用户权限即可）")

    dm = probe_devices(acpi)

    for label, cands in (("独显供电", ECO_CANDIDATES), ("显示通路", MUX_CANDIDATES)):
        for dev, line in cands:
            val = acpi.get_raw(dev)
            if val is None:
                rep.add(f"  {label}候选 0x{dev:08X}", INFO, f"不支持（{line}）")
            else:
                rep.add(f"  {label}候选 0x{dev:08X}", OK, f"支持（{line}）raw=0x{val:X}")

    if dm.can_eco:
        rep.add("独显供电切换", OK, f"0x{dm.eco:08X}（{dm.eco_line}）")
    else:
        rep.add("独显供电切换", WARN, "本机没有可用的 GPUEco 设备号")
    if dm.can_mux:
        rep.add("显示通路切换", OK, f"0x{dm.mux:08X}（{dm.mux_line}）")
    else:
        rep.add("显示通路切换", INFO, "本机没有 MUX 开关（很多轻薄本确实没有，属正常）")

    for key, dev in dm.sensors.items():
        try:
            val = acpi.get(dev)
        except Exception as e:
            rep.add(f"  传感器 {SENSOR_DEVICES[key][1]}", WARN,
                    f"0x{dev:08X} 读不到（{type(e).__name__}: {e}）")
            continue
        rep.add(f"  传感器 {SENSOR_DEVICES[key][1]}", OK, f"0x{dev:08X} = {val}")

    for note in dm.notes:
        rep.add("  机型适配", INFO, note)

    return acpi


def _check_gpus(rep: Report) -> None:
    try:
        from .app import nvidia_available
        ok = nvidia_available()
        rep.add("NVIDIA 独显", OK if ok else WARN,
                "nvidia-smi 可读，独显已通电" if ok
                else "读不到（独显可能正处于断电省电状态；用「标准模式」可唤醒）")
    except Exception as e:
        rep.add("NVIDIA 独显", FAIL, f"检测异常: {e}")

    try:
        from .app import detect_igpu_luid
        luid = detect_igpu_luid()
        rep.add("核显 LUID", OK if luid else WARN, luid or "没找到 Intel/AMD 核显")
    except Exception as e:
        rep.add("核显 LUID", FAIL, f"DXGI 枚举失败: {e}")

    try:
        from .app import FastSystem
        fs = FastSystem()
        fs.cpu_percent()
        import time
        time.sleep(0.35)
        cpu = fs.cpu_percent()
        used, total = fs.memory()
        rep.add("CPU 占用读数", OK if cpu >= 0 else FAIL, f"{cpu:.0f}%")
        rep.add("内存读数", OK if total else FAIL,
                f"{used / 1024**3:.1f} GB / {total / 1024**3:.1f} GB")
    except Exception as e:
        rep.add("系统指标读数", FAIL, f"{e}")


def _check_hotkey(rep: Report) -> None:
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
        VK_G = 0x47
        hk_id = 0xC0DE
        ok = user32.RegisterHotKey(
            None, hk_id, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_G)
        if ok:
            user32.UnregisterHotKey(None, hk_id)
            rep.add("全局热键通路", OK, "RegisterHotKey 可用（试注册 Ctrl+Alt+G 成功）")
        else:
            err = ctypes.get_last_error()
            rep.add("全局热键通路", WARN,
                    f"Ctrl+Alt+G 注册失败 (WinError {err})，可能被别的程序占用；程序会自动换组合键")
    except Exception as e:
        rep.add("全局热键通路", FAIL, f"{e}")


def _run_group(rep: Report, name: str, fn) -> None:
    try:
        fn(rep)
    except Exception as e:
        import traceback
        rep.add(f"{name}（这一组崩了）", FAIL, f"{type(e).__name__}: {e}")
        for line in traceback.format_exc().splitlines()[-4:]:
            rep.add("", INFO, line)


def run_self_test() -> Report:
    rep = Report()
    rep.add("GpuCore 自检", INFO, f"{branding.APP_NAME} v{branding.APP_VERSION}")
    for name, fn in (("运行环境", _check_environment),
                     ("华硕固件通道", _check_acpi),
                     ("显卡与读数", _check_gpus),
                     ("全局热键", _check_hotkey)):
        _run_group(rep, name, fn)
    return rep


def print_report(rep: Report) -> int:
    print()
    print("=" * 66)
    print(f"  {branding.APP_NAME} ({branding.APP_NAME_CN}) 自检  v{branding.APP_VERSION}")
    print("=" * 66)
    for it in rep.items:
        mark = _MARK[it.level]
        print(f"  {mark} {it.name}")
        if it.detail:
            print(f"         {it.detail}")
    print("-" * 66)
    if rep.failed:
        print(f"  结果：{len(rep.failed)} 项失败，{len(rep.warned)} 项警告")
        print("  → [FAIL] 是本程序在这台机器上真的没能工作；[WARN] 多数是平台不适用或缺驱动。")
        print("     请把上面的全部内容贴到 issue 里。")
    elif rep.warned:
        print(f"  结果：全部通过，{len(rep.warned)} 项警告（一般不影响使用）")
    else:
        print("  结果：全部通过 ✓")
    print("=" * 66)
    print()
    return 1 if rep.failed else 0

_REPORT_WMI_FIELDS = [
    ("厂商", "Win32_ComputerSystem", "Manufacturer"),
    ("型号", "Win32_ComputerSystem", "Model"),
    ("系统型号", "Win32_ComputerSystemProduct", "Name"),
    ("BIOS", "Win32_BIOS", "SMBIOSBIOSVersion"),
    ("主板", "Win32_BaseBoard", "Product"),
    ("操作系统", "Win32_OperatingSystem", "Caption"),
    ("系统版本", "Win32_OperatingSystem", "Version"),
    ("显卡", "Win32_VideoController",
     ("Name", "DriverVersion", "PNPDeviceID")),
]

_REPORT_PNP_CLASS = "Display"


def _wmi_probe() -> dict:
    import subprocess

    lines = ['Write-Output "=== GpuCore 报告 ==="']
    for key, classexpr, what in _REPORT_WMI_FIELDS:
        if isinstance(what, tuple):
            props = ", ".join(f'$_.{p}' for p in what)
            lines.append(f'Get-CimInstance {classexpr} -ErrorAction SilentlyContinue | '
                         f'ForEach-Object {{ Write-Output ("[{key}] " + '
                         f'(@({props}) -join " | ")) }}')
        else:
            lines.append(f'Write-Output ("[{key}] " + (((Get-CimInstance {classexpr} '
                         f'-ErrorAction SilentlyContinue).{what}) -join " | "))')
    lines.append('Write-Output "=== 显示设备 ==="')
    lines.append(f'Get-CimInstance Win32_PnPEntity -Filter "PNPClass=\'{_REPORT_PNP_CLASS}\'" '
                 '-ErrorAction SilentlyContinue | ForEach-Object { Write-Output '
                 '("{0} | {1} | {2}" -f $_.Status, $_.ConfigManagerErrorCode, $_.Name) }')
    script = "; ".join(lines)

    res: dict = {}
    pnp: List[str] = []
    try:
        cp = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=40,
        )
        out = [x.rstrip() for x in (cp.stdout or "").splitlines()]
    except Exception:
        res["显示设备"] = pnp
        return res

    in_pnp = False
    for raw in out:
        line = raw.strip()
        if not line or line == "=== GpuCore 报告 ===":
            continue
        if line == "=== 显示设备 ===":
            in_pnp = True
            continue
        if in_pnp:
            pnp.append(line)
            continue
        if line.startswith("[") and "] " in line:
            label, value = line[1:].split("] ", 1)
            value = value.strip()
            if not value:
                continue
            res.setdefault(label, []).append(value)
    res["显示设备"] = pnp
    return res


def _one(wmi: dict, key: str, default: str = "读不到") -> str:
    vals = wmi.get(key) or []
    return " | ".join(vals) if vals else default


def _scan_acpi(acpi, base: int, span: int = 0x20) -> List[Tuple[int, int]]:
    hits: List[Tuple[int, int]] = []
    for off in range(span):
        dev = base + off
        try:
            raw = acpi.get_raw(dev)
        except Exception:
            continue
        if raw:
            hits.append((dev, raw))
    return hits


def build_machine_report(acpi_factory=None) -> str:
    from .app import AtkAcpi
    from .hardware import ECO_CANDIDATES, MUX_CANDIDATES, SENSOR_DEVICES, probe_devices

    make_acpi = acpi_factory or AtkAcpi

    L: List[str] = []
    L.append(f"# {branding.APP_NAME} 机型报告 v{branding.APP_VERSION}")
    L.append("")

    wmi = _wmi_probe()
    L.append("## 机器")
    L.append("")
    L.append(f"- 厂商型号   : {_one(wmi, '厂商')} / {_one(wmi, '型号')}")
    L.append(f"- 系统型号   : {_one(wmi, '系统型号')}")
    L.append(f"- BIOS       : {_one(wmi, 'BIOS')}")
    L.append(f"- 主板       : {_one(wmi, '主板')}")
    L.append(f"- 操作系统   : {_one(wmi, '操作系统')} {_one(wmi, '系统版本')}")
    L.append("")

    L.append("## 显卡")
    L.append("")
    vcs = wmi.get("显卡") or []
    if vcs:
        for line in vcs:
            L.append(f"- {line}")
    else:
        L.append("- WMI 没报出显卡")
    pnp = wmi.get("显示设备") or []
    if pnp:
        L.append("- 显示类设备状态（Status | Problem | 名称）:")
        for line in pnp:
            L.append(f"    - {line}")
    else:
        L.append("- 显示类设备状态: 读不到（Get-PnpDevice 没有输出）")
    try:
        from .app import nvidia_available
        ok = nvidia_available()
        L.append(f"- nvidia-smi 可读: **{ok}**")
        if not ok:
            L.append("  （读不到独显时先看上面那行设备状态：设备不在列表里 = 只是断电省电，属正常；")
            L.append("    出现但状态是 `Error`、且第二个字段是问题码（43 等）= 驱动/设备问题，"
                     "重开机或重启该设备后再看。）")
    except Exception as exc:
        L.append(f"- nvidia-smi 探测失败: {exc}")
    L.append("")

    acpi = make_acpi()
    opened = False
    exc_msg = ""
    try:
        opened = acpi.open()
    except Exception as exc:
        exc_msg = f"{type(exc).__name__}: {exc}"
        opened = False

    if not opened:
        L.append("## ATKACPI")
        L.append("")
        if exc_msg:
            L.append(f"- **打开 `\\\\.\\ATKACPI` 时抛异常**：{exc_msg}")
        L.append("- **打不开 `\\\\.\\ATKACPI`** —— 这台机器不是华硕，或没装 ASUS 驱动。")
        L.append("  装一次 G-Helper 或 MyASUS 会带上这个驱动，然后重跑。")
        L.append("")
        L.append("## 机型自适应结果")
        L.append("")
        L.append("- 独显供电切换 : 不可用（ATKACPI 通道打不开）")
        L.append("- 显示通路切换 : 不可用（ATKACPI 通道打不开）")
        L.append("")
        L.append("## 候选设备号探测")
        L.append("")
        L.append("用**读**的方式试每一个候选号，不写任何东西。")
        L.append("")
        L.append("| 用途 | 设备号 | 产品线 | 结果 | raw |")
        L.append("|---|---|---|---|---|")
        for label, cands in (("独显供电", ECO_CANDIDATES), ("显示通路", MUX_CANDIDATES)):
            for dev, line in cands:
                L.append(f"| {label} | `0x{dev:08X}` | {line} | 打不开通道 | - |")
        L.append("")
        L.append("## 设备号全扫（关键，请勿删）")
        L.append("")
        L.append("（ATKACPI 通道打不开，这一段没法扫。装好驱动后重跑就有内容了。）")
        L.append("")
        L.append("---")
        L.append("")
        L.append(f"把上面全部内容贴到 {branding.APP_REPO}/issues 即可。")
        return "\n".join(L)

    dm = probe_devices(acpi)

    L.append("## 机型自适应结果")
    L.append("")
    L.append(f"- 独显供电切换 : {('0x%08X' % dm.eco) if dm.can_eco else '不可用'}")
    L.append(f"- 显示通路切换 : {('0x%08X' % dm.mux) if dm.can_mux else '不可用'}")
    for note in dm.notes:
        L.append(f"- 备注         : {note}")
    L.append("")

    L.append("## 候选设备号探测")
    L.append("")
    L.append("| 用途 | 设备号 | 产品线 | 结果 | raw |")
    L.append("|---|---|---|---|---|")
    for label, cands in (("独显供电", ECO_CANDIDATES), ("显示通路", MUX_CANDIDATES)):
        for dev, line in cands:
            raw = acpi.get_raw(dev)
            if raw is None:
                L.append(f"| {label} | `0x{dev:08X}` | {line} | 不支持 | - |")
            else:
                L.append(f"| {label} | `0x{dev:08X}` | {line} | **支持** | `0x{raw:X}` |")
    L.append("")

    L.append("## 传感器")
    L.append("")
    for key, dev in dm.sensors.items():
        try:
            val = acpi.get(dev)
        except Exception:
            val = "读不到"
        L.append(f"- 0x{dev:08X} = {val}   （{SENSOR_DEVICES[key][1]}）")
    L.append("")

    L.append("## 设备号全扫（关键，请勿删）")
    L.append("")
    L.append("ATKACPI 上「读得到非零」的号就是这台机器真正存在的设备。")
    L.append("")
    for label, base, span in (("0x00090000 段（显卡/显示相关）", 0x00090000, 0x40),
                              ("0x00120000 段（传感器相关）", 0x00120000, 0x80)):
        L.append(f"### {label}")
        L.append("")
        hits = _scan_acpi(acpi, base, span)
        if not hits:
            L.append("（一个都没读到）")
        else:
            for dev, raw in hits:
                L.append(f"- `0x{dev:08X}` = `0x{raw:X}`")
        L.append("")

    L.append("---")
    L.append("")
    L.append("把上面全部内容贴到 " + branding.APP_REPO + "/issues 即可。")
    return "\n".join(L)


def print_machine_report() -> int:
    print()
    print(build_machine_report())
    print()
    return 0
