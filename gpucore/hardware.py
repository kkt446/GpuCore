"""华硕机型自适应层。

背景：GpuCore 早期版本把设备号写死在代码里（GPUEco=0x00090020、GPUMux=0x00090016），
这两个号只有 ROG / TUF 的一部分机型认。华硕各产品线用的号并不统一：

    独显供电(eco)：ROG 0x00090020 / Vivo、部分 TUF 0x00090120 / 部分 Intel 机型 0x00090120
    显示通路(mux)：ROG 0x00090016 / Vivo、部分 TUF 0x00090026

所以启动时**逐个探测**：EC 对不支持的设备号会返回 0（DS 的 `raw - 0x10000` 之后得到
None），而支持的设备号**一定会回一个非零原始值**——即使它的值恰好是 0（比如独显正通电时
GPUEco 读出来就是 0）。这个区别就是探测依据，不必猜。

顺带说明为什么不能"读一次判断好坏"：读 0 有两种含义（不支持 / 值为 0），
而回 None 只有一种含义（不支持），所以判据用"是否返回 None"。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# --- ATKACPI 通道 ---
ATKACPI_PATH = r"\\.\ATKACPI"
CONTROL_CODE = 0x0022240C
DSTS = 0x53545344  # 读
DEVS = 0x53564544  # 写
INIT = 0x54494E49
WDOG = 0x474F4457

# --- 设备号候选表：功能 -> [(设备号, 适用产品线说明), ...] 按优先级排列 ---
ECO_CANDIDATES: List[Tuple[int, str]] = [
    (0x00090020, "ROG / TUF Gaming"),
    (0x00090120, "VivoBook / Zenbook / 部分 TUF"),
]

MUX_CANDIDATES: List[Tuple[int, str]] = [
    (0x00090016, "ROG / TUF Gaming"),
    (0x00090026, "VivoBook / Zenbook / 部分 TUF"),
]

# --- 其余设备号（各机型一致，探测失败即视为不支持）---
SENSOR_DEVICES: Dict[str, Tuple[int, str]] = {
    "temp_cpu": (0x00120094, "CPU 温度"),
    "temp_gpu": (0x00120097, "独显温度"),
    "perf_mode": (0x00120075, "性能模式"),
    "gpu_base": (0x00120099, "独显基础功耗"),
    "gpu_power": (0x00120098, "独显附加功耗"),
    "battery_limit": (0x00120057, "电池充电上限"),
    "display_connect": (0x00090030, "外接显示器状态"),
    "xg_connected": (0x00090018, "外接显卡坞"),
}

#: 独显供电取值
ECO_ON = 0   # 通电，独显可用
ECO_OFF = 1  # 断电，省电

#: 显示通路取值
MUX_IGPU = 0      # 只用核显输出（最省电，独显不参与显示）
MUX_STANDARD = 1  # 混合：核显输出 + 独显渲染，新程序默认走独显
MUX_ULTIMATE = 2  # 独显直连（性能最高，必须重启生效）

#: EC 对不支持的设备号返回这个值（raw - 0x10000 之后）
UNSUPPORTED = -65536


# ---------------------------------------------------------------------------
# 原始 ACPI 通道
# ---------------------------------------------------------------------------


class AtkAcpi:
    """`\\\\.\\ATKACPI` 的最小封装：句柄缓存 + 可重连。

    注意必须用 RLock：公开方法会加锁，而 `_call` 在句柄缺失时也会去 `open()`，
    `open()` 自己又加锁——用普通 Lock 会死锁（本项目真的发生过，表现为界面卡住）。
    """

    def __init__(self) -> None:
        import threading

        try:
            k = ctypes.WinDLL("kernel32", use_last_error=True)
        except OSError:
            k = None
        self._k = k
        self._h = None
        self._failed = False
        self._lock = threading.RLock()
        if k is not None:
            self._configure()

    def _configure(self) -> None:
        k = self._k
        k.CreateFileW.restype = wt.HANDLE
        k.CreateFileW.argtypes = [
            wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE
        ]
        k.DeviceIoControl.restype = wt.BOOL
        k.DeviceIoControl.argtypes = [
            wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD,
            ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p,
        ]
        k.CloseHandle.restype = wt.BOOL
        k.CloseHandle.argtypes = [wt.HANDLE]

    @property
    def available(self) -> bool:
        return self.open()

    def open(self) -> bool:
        with self._lock:
            if self._h:
                return True
            if self._failed or self._k is None:
                return False
            h = self._k.CreateFileW(
                ATKACPI_PATH, 0x80000000 | 0x40000000, 1 | 2, None, 3, 0x80, None
            )
            if h in (None, 0, wt.HANDLE(-1).value):
                self._failed = True
                return False
            self._h = h
            return True

    def close(self) -> None:
        with self._lock:
            if self._h and self._k is not None:
                try:
                    self._k.CloseHandle(wt.HANDLE(self._h))
                except Exception:
                    pass
            self._h = None

    def _call(self, method: int, args: bytes, out: int = 16) -> Optional[bytes]:
        with self._lock:
            if not self._h and not self.open():
                return None
            buf = bytearray(8 + len(args))
            buf[0:4] = method.to_bytes(4, "little")
            buf[4:8] = len(args).to_bytes(4, "little")
            buf[8 : 8 + len(args)] = args
            ib = (ctypes.c_char * len(buf)).from_buffer(buf)
            ob = (ctypes.c_char * out)()
            ret = wt.DWORD(0)
            try:
                ok = self._k.DeviceIoControl(
                    wt.HANDLE(self._h), CONTROL_CODE,
                    ctypes.cast(ib, ctypes.c_void_p), len(buf),
                    ctypes.cast(ob, ctypes.c_void_p), out, ctypes.byref(ret), None,
                )
            except Exception:
                return None
            return bytes(ob) if ok else None

    def get_raw(self, dev: int) -> Optional[int]:
        """返回原始值；None 表示设备号不被支持。"""
        out = self._call(DSTS, dev.to_bytes(4, "little") + bytes(4))
        if not out or len(out) < 4:
            return None
        raw = int.from_bytes(out[0:4], "little")
        return None if raw == 0 else raw

    def get(self, dev: int) -> Optional[int]:
        """返回规范化值（raw - 0x10000）；None 表示不支持。"""
        raw = self.get_raw(dev)
        if raw is None:
            return None
        return raw - 0x10000

    def set(self, dev: int, value: int) -> Optional[int]:
        out = self._call(
            DEVS, dev.to_bytes(4, "little") + (value & 0xFFFFFFFF).to_bytes(4, "little")
        )
        if not out or len(out) < 4:
            return None
        return int.from_bytes(out[0:4], "little")


# ---------------------------------------------------------------------------
# 机型自适应：把"功能"解析成"这台机器上真正可用的设备号"
# ---------------------------------------------------------------------------


@dataclass
class DeviceMap:
    """本机实际可用的设备号。

    eco / mux 为 None 表示这台机器没有该功能（无独显、无 MUX 开关，或不是华硕）。
    """

    acpi_ok: bool = False
    eco: Optional[int] = None
    eco_line: str = ""
    mux: Optional[int] = None
    mux_line: str = ""
    sensors: Dict[str, int] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    @property
    def can_eco(self) -> bool:
        return self.acpi_ok and self.eco is not None

    @property
    def can_mux(self) -> bool:
        return self.acpi_ok and self.mux is not None

    def describe(self) -> str:
        if not self.acpi_ok:
            return "无华硕固件通道（非华硕机型或 ATKACPI 驱动未安装）"
        bits = []
        bits.append(f"独显供电 {'✓ ' + self.eco_line if self.can_eco else '✗ 不支持'}")
        bits.append(f"显示通路 {'✓ ' + self.mux_line if self.can_mux else '✗ 不支持'}")
        return "；".join(bits)

    def temp_cpu(self) -> Optional[int]:
        dev = self.sensors.get("temp_cpu")
        return dev

    def temp_gpu(self) -> Optional[int]:
        return self.sensors.get("temp_gpu")


def probe_devices(acpi: AtkAcpi) -> DeviceMap:
    """逐个探测设备号，返回本机可用的功能表。

    判据：`get_raw()` 返回 None = 不支持；返回任何整数（包括 0）= 支持。
    """
    dm = DeviceMap()
    if not acpi.open():
        return dm
    dm.acpi_ok = True

    for dev, line in ECO_CANDIDATES:
        if acpi.get_raw(dev) is not None:
            dm.eco, dm.eco_line = dev, line
            if dev != ECO_CANDIDATES[0][0]:
                dm.notes.append(f"独显供电使用备用设备号 0x{dev:08X}（{line}）")
            break

    for dev, line in MUX_CANDIDATES:
        if acpi.get_raw(dev) is not None:
            dm.mux, dm.mux_line = dev, line
            if dev != MUX_CANDIDATES[0][0]:
                dm.notes.append(f"显示通路使用备用设备号 0x{dev:08X}（{line}）")
            break

    for key, (dev, _desc) in SENSOR_DEVICES.items():
        if acpi.get_raw(dev) is not None:
            dm.sensors[key] = dev

    if dm.eco is None and dm.mux is None:
        dm.notes.append("固件通道可打开，但没找到任何独显供电/显示通路设备号")
    return dm


def read_eco(acpi: AtkAcpi, dev: Optional[int]) -> Optional[int]:
    """读独显供电状态。

    ⚠️ 实测提醒：本机（FX608LM）GPUEco **回读不可信**——写入 1 之后 nvidia-smi
    确实报设备消失，但这里读回来仍是 0。所以调用方**不要**用这个值当权威判据，
    真正的权威信号是「nvidia-smi 能否看到独显」（见 app.nvidia_available）。
    这个函数保留给能正确回读的机型，以及写之前的"当前值"记录。
    """
    if dev is None:
        return None
    return acpi.get(dev)


def write_eco(acpi: AtkAcpi, dev: Optional[int], off: bool) -> Optional[int]:
    """写独显供电（off=True 断电）。返回 DEVS 的 result，1 表示固件接受。

    这只是"最后一步"——写之前该做的准备（结束占用独显的进程、释放句柄、停 NV 服务）
    由 app.py 的切换流程负责，因为那些步骤需要用到 metrics / 进程枚举。
    """
    if dev is None:
        return None
    return acpi.set(dev, ECO_OFF if off else ECO_ON)


def write_mux(acpi: AtkAcpi, dev: Optional[int], value: int) -> Optional[int]:
    """写显示通路。0=集显 1=混合 2=独显直连。**重启后才生效。**"""
    if dev is None:
        return None
    return acpi.set(dev, value)


def read_mux(acpi: AtkAcpi, dev: Optional[int]) -> Optional[int]:
    if dev is None:
        return None
    return acpi.get(dev)


def mode_name(eco: Optional[int], mux: Optional[int]) -> str:
    """把 (eco, mux) 翻译成人话，判定顺序与 G-Helper 一致。"""
    if mux == MUX_ULTIMATE:
        return "独显直连"
    if mux == MUX_IGPU:
        return "集显模式"
    if eco == ECO_OFF:
        return "集显模式"  # 没有 MUX 的机型靠断电独显达到同样效果
    if mux == MUX_STANDARD:
        return "标准模式"
    return "标准模式" if eco is None else "未知"


def supported_summary(dm: DeviceMap) -> List[str]:
    """给自检/界面用的一行行说明。"""
    rows = []
    rows.append(f"ATKACPI 通道：{'可打开' if dm.acpi_ok else '不可用'}")
    rows.append(f"独显供电：{'0x%08X（%s）' % (dm.eco, dm.eco_line) if dm.can_eco else '不支持'}")
    rows.append(f"显示通路：{'0x%08X（%s）' % (dm.mux, dm.mux_line) if dm.can_mux else '不支持'}")
    for key, dev in dm.sensors.items():
        rows.append(f"{SENSOR_DEVICES[key][1]}：0x{dev:08X}")
    rows.extend(dm.notes)
    return rows
