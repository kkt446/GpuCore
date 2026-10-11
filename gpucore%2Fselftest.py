"""GpuCore 自检 —— 在新机器上排查"为什么不能用/读数不对"。

跑法：
    python -m gpucore --check

逐项检查：Python 环境 → 平台 → ASUS ACPI 通道 → 各设备号可读性 →
独显是否可用 → DXGI/PDH 占用读数 → 全局热键是否注册得上。

输出是给人看的，不抛异常：某一项失败就标红继续。
"""

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


# 设备号表（华硕 ACPI）。值是给自检时逐个读的。
DEVICES = {
    "GPUEcoROG (独显供电)": 0x00090020,
    "GPUMuxROG (显示通路)": 0x00090016,
    "GPUEcoVivo": 0x00090120,
    "GPUMuxVivo": 0x00090026,
    "Temp_CPU": 0x00120094,
    "Temp_GPU": 0x00120097,
    "PerformanceMode": 0x00120075,
}


def _check_environment(rep: Report) -> None:
    v = sys.version_info
    rep.add("Python 版本", OK if v >= (3, 9) else FAIL,
            f"{v.major}.{v.minor}.{v.micro} @ {sys.executable}")

    try:
        import tkinter  # noqa: F401
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
    """探测固件通道 + 机型自适应地找出可用设备号。

    不写死设备号：不同华硕产品线的号不一样，所以这里逐个候选探测，
    并把**实际选中的**号报出来（用户拿去反馈时这个信息最有用）。
    """
    from .app import AtkAcpi
    from .hardware import ECO_CANDIDATES, MUX_CANDIDATES, SENSOR_DEVICES, probe_devices

    acpi = AtkAcpi()
    if not acpi.open():
        rep.add("\\\\.\\ATKACPI 通道", FAIL,
                "打不开。这台机器可能不是华硕，或缺少 ASUS 驱动（装 G-Helper 或 MyASUS 会带上）")
        return None
    rep.add("\\\\.\\ATKACPI 通道", OK, "可打开（普通用户权限即可）")

    dm = probe_devices(acpi)

    # 逐个候选报出探测结果，方便看清为什么选了某个号
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
        val = acpi.get(dev)
        rep.add(f"  传感器 {SENSOR_DEVICES[key][1]}", OK, f"0x{dev:08X} = {val}")

    for note in dm.notes:
        rep.add("  机型适配", INFO, note)

    return acpi


def _check_gpus(rep: Report) -> None:
    # 独显
    try:
        from .app import nvidia_available
        ok = nvidia_available()
        rep.add("NVIDIA 独显", OK if ok else WARN,
                "nvidia-smi 可读，独显已通电" if ok
                else "读不到（独显可能正处于断电省电状态；用「标准模式」可唤醒）")
    except Exception as e:
        rep.add("NVIDIA 独显", FAIL, f"检测异常: {e}")

    # 核显 LUID
    try:
        from .app import detect_igpu_luid
        luid = detect_igpu_luid()
        rep.add("核显 LUID", OK if luid else WARN, luid or "没找到 Intel/AMD 核显")
    except Exception as e:
        rep.add("核显 LUID", FAIL, f"DXGI 枚举失败: {e}")

    # 占用读数
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
    """试着注册一个热键再释放，确认 RegisterHotKey 通路可用。"""
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


def run_self_test() -> Report:
    rep = Report()
    rep.add("GpuCore 自检", INFO, f"{branding.APP_NAME} v{branding.APP_VERSION}")
    _check_environment(rep)
    _check_acpi(rep)
    _check_gpus(rep)
    _check_hotkey(rep)
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
        print("  → 上面标 [FAIL] 的就是这台机器缺的东西。")
    elif rep.warned:
        print(f"  结果：全部通过，{len(rep.warned)} 项警告（一般不影响使用）")
    else:
        print("  结果：全部通过 ✓")
    print("=" * 66)
    print()
    return 1 if rep.failed else 0
