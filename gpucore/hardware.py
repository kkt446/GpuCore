
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

ATKACPI_PATH = r"\\.\ATKACPI"
CONTROL_CODE = 0x0022240C
DSTS = 0x53545344
DEVS = 0x53564544
INIT = 0x54494E49
WDOG = 0x474F4457

ECO_CANDIDATES: List[Tuple[int, str]] = [
    (0x00090020, "ROG / TUF Gaming"),
    (0x00090120, "VivoBook / Zenbook / 部分 TUF"),
]

MUX_CANDIDATES: List[Tuple[int, str]] = [
    (0x00090016, "ROG / TUF Gaming"),
    (0x00090026, "VivoBook / Zenbook / 部分 TUF"),
]

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

ECO_ON = 0
ECO_OFF = 1

MUX_IGPU = 0
MUX_STANDARD = 1
MUX_ULTIMATE = 2

UNSUPPORTED = -65536

class AtkAcpi:

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
        out = self._call(DSTS, dev.to_bytes(4, "little") + bytes(4))
        if not out or len(out) < 4:
            return None
        raw = int.from_bytes(out[0:4], "little")
        return None if raw == 0 else raw

    def get(self, dev: int) -> Optional[int]:
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

@dataclass
class DeviceMap:

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
    if dev is None:
        return None
    return acpi.get(dev)


def write_eco(acpi: AtkAcpi, dev: Optional[int], off: bool) -> Optional[int]:
    if dev is None:
        return None
    return acpi.set(dev, ECO_OFF if off else ECO_ON)


def write_mux(acpi: AtkAcpi, dev: Optional[int], value: int) -> Optional[int]:
    if dev is None:
        return None
    return acpi.set(dev, value)


def read_mux(acpi: AtkAcpi, dev: Optional[int]) -> Optional[int]:
    if dev is None:
        return None
    return acpi.get(dev)


def mode_name(eco: Optional[int], mux: Optional[int]) -> str:
    if mux == MUX_ULTIMATE:
        return "独显直连"
    if mux == MUX_IGPU:
        return "集显模式"
    if eco == ECO_OFF:
        return "集显模式"
    if mux == MUX_STANDARD:
        return "标准模式"
    return "标准模式" if eco is None else "未知"


def supported_summary(dm: DeviceMap) -> List[str]:
    rows = []
    rows.append(f"ATKACPI 通道：{'可打开' if dm.acpi_ok else '不可用'}")
    rows.append(f"独显供电：{'0x%08X（%s）' % (dm.eco, dm.eco_line) if dm.can_eco else '不支持'}")
    rows.append(f"显示通路：{'0x%08X（%s）' % (dm.mux, dm.mux_line) if dm.can_mux else '不支持'}")
    for key, dev in dm.sensors.items():
        rows.append(f"{SENSOR_DEVICES[key][1]}：0x{dev:08X}")
    rows.extend(dm.notes)
    return rows
