
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import log
from .paths import is_windows, run_capture

logger = log.get(__name__)


ATKACPI_PATH = r"\\.\ATKACPI"
CONTROL_CODE = 0x0022240C
DSTS = 0x53545344
DEVS = 0x53564544

DEVICES: Dict[int, Tuple[str, str]] = {
    0x00090020: ("GPUEco", "独显供电：0=通电可用 1=断电省电"),
    0x00090016: ("GPUMux", "显示通路：0=集显 1=标准混合 2=独显直连"),
    0x00090030: ("GPUDisplayConnect", "外接显示器连接状态"),
    0x00120099: ("GPU_BASE", "独显 TGP 基础功耗 (W)"),
    0x00120098: ("GPU_POWER", "独显 TGP 附加功耗 (W)"),
    0x001200C0: ("PPT_GPUC0", "NVIDIA 动态加速 (W)"),
    0x001200C2: ("PPT_GPUC2", "独显温度目标 (℃)"),
    0x00120097: ("Temp_GPU", "独显温度"),
    0x00120094: ("Temp_CPU", "CPU 温度"),
    0x00120075: ("PerformanceMode", "性能模式"),
    0x00090031: ("StatusMode", "状态位"),
    0x00120057: ("BatteryLimit", "电池充电上限"),
    0x001200D2: ("CORES_CPU", "当前 CPU 核心配置"),
    0x00050020: ("ScreenOverdriveSupport", "屏幕超频支持"),
    0x00090018: ("GPUXGConnected", "外接显卡坞"),
    0x000600C4: ("VRAM_MEM", "显存分配选项"),
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

        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._handle: Optional[int] = None
        self._open_failed = False
        self._lock = threading.RLock()
        self._configure()

    def _configure(self) -> None:
        k = self._kernel32
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

    def open(self) -> bool:
        with self._lock:
            if self._handle:
                return True
            if self._open_failed:
                return False
            handle = self._kernel32.CreateFileW(
                ATKACPI_PATH, 0x80000000 | 0x40000000, 0x1 | 0x2, None, 3, 0x80, None
            )
            if handle in (None, 0, wt.HANDLE(-1).value):
                logger.warning(
                    "打开 %s 失败 WinError=%d（华硕 ACPI 驱动不可用）",
                    ATKACPI_PATH, ctypes.get_last_error(),
                )
                self._handle = None
                self._open_failed = True
                return False
            self._handle = handle
            return True

    def close(self) -> None:
        with self._lock:
            if self._handle:
                try:
                    self._kernel32.CloseHandle(wt.HANDLE(self._handle))
                except Exception:
                    pass
                self._handle = None

    def _call(self, method: int, args: bytes, out_size: int = 16) -> Optional[bytes]:
        with self._lock:
            if not self._handle and not self.open():
                return None
            buffer = bytearray(8 + len(args))
            buffer[0:4] = method.to_bytes(4, "little")
            buffer[4:8] = len(args).to_bytes(4, "little")
            buffer[8 : 8 + len(args)] = args
            in_buf = (ctypes.c_char * len(buffer)).from_buffer(buffer)
            out_buf = (ctypes.c_char * out_size)()
            returned = wt.DWORD(0)
            ok = self._kernel32.DeviceIoControl(
                wt.HANDLE(self._handle), CONTROL_CODE,
                ctypes.cast(in_buf, ctypes.c_void_p), len(buffer),
                ctypes.cast(out_buf, ctypes.c_void_p), out_size,
                ctypes.byref(returned), None,
            )
            if not ok:
                logger.debug("DeviceIoControl 失败 WinError=%d", ctypes.get_last_error())
                return None
            return bytes(out_buf)

    def get_raw(self, device_id: int, out_size: int = 16) -> Optional[bytes]:
        args = device_id.to_bytes(4, "little") + (0).to_bytes(4, "little")
        return self._call(DSTS, args, out_size)

    def get(self, device_id: int) -> Optional[int]:
        out = self.get_raw(device_id)
        if not out or len(out) < 4:
            return None
        raw = int.from_bytes(out[0:4], "little", signed=False)
        if raw == 0:
            return None
        return raw - 0x10000

    def set(self, device_id: int, value: int) -> Optional[int]:
        args = device_id.to_bytes(4, "little") + (value & 0xFFFFFFFF).to_bytes(4, "little")
        out = self._call(DEVS, args)
        if not out or len(out) < 4:
            return None
        return int.from_bytes(out[0:4], "little", signed=False)


_present_cache: Dict[str, object] = {"value": None, "at": 0.0}


def dgpu_device_present() -> Optional[bool]:
    if not is_windows():
        return None
    try:
        setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
        cfgmgr32 = ctypes.WinDLL("cfgmgr32", use_last_error=True)
    except OSError as exc:
        logger.debug("无法加载 setupapi: %s", exc)
        return None

    class SP_DEVINFO_DATA(ctypes.Structure):
        _fields_ = [
            ("cbSize", wt.DWORD),
            ("ClassGuid", ctypes.c_byte * 16),
            ("DevInst", wt.DWORD),
            ("Reserved", ctypes.POINTER(ctypes.c_ulong)),
        ]

    DIGCF_PRESENT = 0x2
    DIGCF_ALLCLASSES = 0x4
    SPDRP_HARDWAREID = 0x00000001
    MAX_DEVICE_ID_LEN = 200

    setupapi.SetupDiGetClassDevsW.restype = wt.HANDLE
    setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.c_void_p, wt.LPCWSTR, wt.HANDLE, wt.DWORD
    ]
    setupapi.SetupDiEnumDeviceInfo.argtypes = [
        wt.HANDLE, wt.DWORD, ctypes.POINTER(SP_DEVINFO_DATA)
    ]
    setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
        wt.HANDLE, ctypes.POINTER(SP_DEVINFO_DATA), wt.DWORD,
        ctypes.POINTER(wt.DWORD), ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)
    ]
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wt.HANDLE]
    cfgmgr32.CM_Get_DevNode_Status.argtypes = [
        ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD), wt.DWORD, ctypes.c_ulong
    ]

    handle = setupapi.SetupDiGetClassDevsW(None, None, None, DIGCF_PRESENT | DIGCF_ALLCLASSES)
    if handle in (None, 0, wt.HANDLE(-1).value):
        return None
    try:
        found = False
        index = 0
        while True:
            info = SP_DEVINFO_DATA()
            info.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)
            if not setupapi.SetupDiEnumDeviceInfo(handle, index, ctypes.byref(info)):
                break
            index += 1

            buffer = ctypes.create_unicode_buffer(MAX_DEVICE_ID_LEN)
            needed = wt.DWORD(0)
            if not setupapi.SetupDiGetDeviceRegistryPropertyW(
                handle, ctypes.byref(info), SPDRP_HARDWAREID, None,
                buffer, ctypes.sizeof(buffer), ctypes.byref(needed)
            ):
                continue
            if "VEN_10DE" not in (buffer.value or "").upper():
                continue

            found = True
            status = wt.DWORD(0)
            problem = wt.DWORD(0)
            if cfgmgr32.CM_Get_DevNode_Status(
                ctypes.byref(status), ctypes.byref(problem), info.DevInst, 0
            ) == 0:
                return problem.value == 0
            return True
        if not found:
            return False
        return None
    except Exception as exc:
        logger.debug("SetupAPI 查询失败: %s", exc)
        return None
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(handle)


def nvidia_present(cache_seconds: float = 10.0) -> Optional[bool]:
    if not is_windows():
        return None

    now = time.monotonic()
    cached_at = float(_present_cache.get("at") or 0.0)
    if cache_seconds > 0 and now - cached_at < cache_seconds:
        cached = _present_cache.get("value")
        return cached if cached is None else bool(cached)

    present = dgpu_device_present()
    if present is None or present is False:
        try:
            out = run_capture(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], timeout=4
            )
            text = ((out.stdout or "") + (out.stderr or "")).lower()
            by_driver = bool(out.returncode == 0 and "nvidia" in text)
            present = by_driver if present is None else (present and by_driver)
        except Exception:
            if present is None:
                present = False

    _present_cache["value"] = present
    _present_cache["at"] = time.monotonic()
    return present


def invalidate_presence_cache() -> None:
    _present_cache["value"] = None
    _present_cache["at"] = 0.0


def asus_services_state() -> Dict[str, str]:
    result: Dict[str, str] = {}
    if not is_windows():
        return result
    try:
        out = run_capture(
            ["powershell", "-NoProfile", "-Command",
             "Get-Service | Where-Object { $_.Name -match 'Asus|Armoury|ATK' } | "
             "ForEach-Object { \"$($_.Name)=$($_.Status)\" }"],
            timeout=15,
        )
        for line in (out.stdout or "").splitlines():
            line = line.strip()
            if "=" in line:
                name, _, status = line.partition("=")
                result[name.strip()] = status.strip()
    except Exception as exc:
        logger.debug("查询华硕服务失败: %s", exc)
    return result

@dataclass
class Check:
    name: str
    ok: bool
    value: str = ""
    note: str = ""

    @property
    def mark(self) -> str:
        return "✓" if self.ok else "✗"


@dataclass
class SelfTestReport:
    checks: List[Check] = field(default_factory=list)
    eco: Optional[int] = None
    mux: Optional[int] = None
    model: str = ""
    dgpu_present: Optional[bool] = None

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.checks if c.value != "不支持")

    @property
    def power_state_text(self) -> str:
        if self.dgpu_present is True:
            return "独显已通电可用"
        if self.dgpu_present is False:
            return "独显已断电（集显模式）"
        return "独显状态未知"

    def add(self, name: str, ok: bool, value: str = "", note: str = "") -> None:
        self.checks.append(Check(name, ok, value, note))

    def text(self) -> str:
        lines = [f"华硕接口自检  {self.model}", "-" * 60]
        for check in self.checks:
            line = f"  {check.mark} {check.name:<22} {check.value}"
            if check.note:
                line += f"   {check.note}"
            lines.append(line)
        return "\n".join(lines)


def run_self_test() -> SelfTestReport:
    report = SelfTestReport()

    if not is_windows():
        report.add("平台", False, "非 Windows", "华硕通道只在 Windows 下可用")
        return report

    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS"
        ) as key:
            report.model = str(winreg.QueryValueEx(key, "SystemProductName")[0])
    except Exception:
        report.model = "未知机型"
    report.add("机型", True, report.model)

    acpi = AtkAcpi()
    if not acpi.open():
        report.add("\\\\.\\ATKACPI", False, "打不开", "华硕 ACPI 驱动不可用")
        return report
    report.add("\\\\.\\ATKACPI", True, "已连接")

    supported = 0
    for device_id, (name, meaning) in DEVICES.items():
        value = acpi.get(device_id)
        if value is None:
            report.add(name, True, "不支持", meaning)
        else:
            supported += 1
            report.add(name, True, str(value), meaning)

    report.eco = acpi.get(0x00090020)
    report.mux = acpi.get(0x00090016)
    report.add("支持的设备号", supported > 0, f"{supported}/{len(DEVICES)}")

    report.dgpu_present = nvidia_present()
    report.add(
        "独显是否在系统里（权威判据）",
        True,
        "在（已通电）" if report.dgpu_present else "不在（已断电）",
        "用 nvidia-smi 能否读到判断；GPUEco 寄存器回读在本机型不可信",
    )

    services = asus_services_state()
    if services:
        running = [n for n, s in services.items() if s.lower() == "running"]
        stopped = [n for n, s in services.items() if s.lower() != "running"]
        report.add(
            "华硕服务",
            len(running) > 0,
            f"{len(running)} 运行 / {len(stopped)} 停止",
            "；".join(f"{n}={s}" for n, s in services.items()),
        )
        if "AsusAppService" in services and services["AsusAppService"].lower() != "running":
            report.add(
                "AsusAppService",
                False,
                services["AsusAppService"],
                "被禁用时 ACPI 写入可能只短暂生效、状态难以维持；"
                "需要以管理员运行并启用该服务",
            )

    if report.eco is not None:
        result = acpi.set(0x00090020, report.eco)
        report.add(
            "写入通路（值保持）",
            result == 1,
            f"DEVS result={result}",
            "1 = 固件接受了写入" if result == 1 else "写入被拒绝",
        )

    acpi.close()
    return report

@dataclass
class AsusMode:
    mode_id: str
    label: str
    description: str
    eco: int
    mux: Optional[int]
    needs_reboot: bool = False
    may_blackout: bool = False

    def matches(self, eco: Optional[int], mux: Optional[int]) -> bool:
        if eco is not None and eco != self.eco:
            return False
        if self.mux is not None and mux is not None and mux != self.mux:
            return False
        return True


ASUS_MODES: List[AsusMode] = [
    AsusMode(
        "eco", "集显模式",
        "关闭独显，只用核显。最省电、最凉快，独显相关功能不可用",
        eco=ECO_OFF, mux=None,
    ),
    AsusMode(
        "standard", "标准模式",
        "出厂默认：核显输出画面、独显负责渲染。两块卡都可用",
        eco=ECO_ON, mux=MUX_STANDARD,
    ),
    AsusMode(
        "ultimate", "独显直连",
        "独显直接驱动屏幕，性能最高。切换会黑屏数秒，通常需要重启",
        eco=ECO_ON, mux=MUX_ULTIMATE, needs_reboot=True, may_blackout=True,
    ),
]


def find_mode(mode_id: str) -> Optional[AsusMode]:
    for mode in ASUS_MODES:
        if mode.mode_id == mode_id:
            return mode
    return None


def detect_current_mode(eco: Optional[int], mux: Optional[int]) -> Optional[AsusMode]:
    present = nvidia_present()
    if present is False:
        return find_mode("eco")
    if present is True:
        if mux == MUX_ULTIMATE:
            return find_mode("ultimate")
        return find_mode("standard")
    if eco is None:
        return None
    if eco == ECO_OFF:
        return find_mode("eco")
    if mux == MUX_ULTIMATE:
        return find_mode("ultimate")
    if mux == MUX_IGPU:
        return find_mode("eco")
    return find_mode("standard")
