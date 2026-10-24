
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import log
from .paths import is_windows

logger = log.get(__name__)

ATKACPI_PATH = r"\\.\ATKACPI"
CONTROL_CODE = 0x0022240C
DSTS = 0x53545344
DEVS = 0x53564544

DEV_GPU_ECO = 0x00090020
DEV_GPU_ECO_VIVO = 0x00090120
DEV_GPU_MUX = 0x00090016
DEV_GPU_MUX_VIVO = 0x00090026
DEV_GPU_DISPLAY_CONNECT = 0x00090030
DEV_GPU_XG_CONNECTED = 0x00090018

ECO_ON = 0
ECO_OFF = 1

MUX_IGPU = 0
MUX_STANDARD = 1
MUX_ULTIMATE = 2


@dataclass
class HardwareMode:

    mode_id: str
    label: str
    description: str
    needs_reboot: bool = False
    may_blackout: bool = False
    requires_dgpu: bool = True
    eco: Optional[int] = None
    mux: Optional[int] = None

    def __str__(self) -> str:
        return f"{self.label}(eco={self.eco}, mux={self.mux})"


@dataclass
class HardwareState:
    available: bool = False
    vendor: str = ""
    eco: Optional[int] = None
    mux: Optional[int] = None
    mode_id: str = ""
    detail: str = ""
    raw: Dict[str, int] = field(default_factory=dict)

    @property
    def dgpu_enabled(self) -> Optional[bool]:
        if self.eco is None:
            return None
        return self.eco == ECO_ON

    @property
    def mux_label(self) -> str:
        return {
            MUX_IGPU: "集显模式",
            MUX_STANDARD: "标准模式（混合输出）",
            MUX_ULTIMATE: "独显直连",
        }.get(self.mux, "未知" if self.mux is None else str(self.mux))


@dataclass
class HardwareResult:
    ok: bool
    message: str
    detail: str = ""
    changed: bool = False
    needs_reboot: bool = False
    may_blackout: bool = False

    def __bool__(self) -> bool:
        return self.ok

class HardwareBackend:
    name = "none"
    vendor = ""
    description = ""

    def available(self) -> bool:
        return False

    def read_state(self) -> HardwareState:
        return HardwareState(available=False, vendor=self.vendor, detail="无可用硬件通道")

    def modes(self) -> List[HardwareMode]:
        return []

    def apply_mode(self, mode: HardwareMode) -> HardwareResult:
        return HardwareResult(False, "当前机型没有可用的硬件切换通道")

    def restore(self, state: HardwareState) -> HardwareResult:
        return HardwareResult(False, "无法还原：没有可用的硬件通道")

class AsusAtkAcpi:

    def __init__(self) -> None:
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._handle: Optional[int] = None
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
            GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
            FILE_SHARE_READ, FILE_SHARE_WRITE = 0x1, 0x2
            OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL = 3, 0x80
            handle = self._kernel32.CreateFileW(
                ATKACPI_PATH, GENERIC_READ | GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None,
            )
            if handle == wt.HANDLE(-1).value or not handle:
                logger.debug("打开 %s 失败 (WinError %d)", ATKACPI_PATH, ctypes.get_last_error())
                self._handle = None
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
                logger.debug("DeviceIoControl 失败 (WinError %d)", ctypes.get_last_error())
                return None
            return bytes(out_buf)

    def get(self, device_id: int) -> Optional[int]:
        args = device_id.to_bytes(4, "little") + (0).to_bytes(4, "little")
        out = self._call(DSTS, args)
        if not out or len(out) < 4:
            return None
        raw = int.from_bytes(out[0:4], "little", signed=False)
        value = raw - 0x10000
        if raw == 0 or value == -65536:
            return None
        return value

    def get_buffer(self, device_id: int, status: int = 0, out_size: int = 64) -> Optional[bytes]:
        args = device_id.to_bytes(4, "little") + status.to_bytes(4, "little")
        return self._call(DSTS, args, out_size)

    def set(self, device_id: int, status: int) -> Optional[int]:
        args = device_id.to_bytes(4, "little") + (status & 0xFFFFFFFF).to_bytes(4, "little")
        out = self._call(DEVS, args)
        if not out or len(out) < 4:
            return None
        return int.from_bytes(out[0:4], "little", signed=False)


class AsusHardware(HardwareBackend):
    name = "asus-atkacpi"
    vendor = "ASUS"
    description = "通过 \\\\.\\ATKACPI 直接控制独显供电与显示通路（与奥创/G-Helper 同源）"

    def __init__(self) -> None:
        self._acpi = AsusAtkAcpi()
        self._probed: Optional[bool] = None
        self._eco_id = DEV_GPU_ECO
        self._mux_id = DEV_GPU_MUX


    def available(self) -> bool:
        if self._probed is not None:
            return self._probed
        if not is_windows():
            self._probed = False
            return False
        if not self._acpi.open():
            self._probed = False
            return False
        eco = self._acpi.get(DEV_GPU_ECO)
        if eco is not None:
            self._eco_id, self._mux_id = DEV_GPU_ECO, DEV_GPU_MUX
            self._probed = True
            return True
        eco = self._acpi.get(DEV_GPU_ECO_VIVO)
        if eco is not None:
            self._eco_id, self._mux_id = DEV_GPU_ECO_VIVO, DEV_GPU_MUX_VIVO
            self._probed = True
            return True
        self._probed = False
        return False


    def read_state(self) -> HardwareState:
        if not self.available():
            return HardwareState(available=False, vendor=self.vendor, detail="ATKACPI 不可用")

        raw_eco = self._acpi.get(self._eco_id)
        mux = self._acpi.get(self._mux_id)

        try:
            from .asusprobe import nvidia_present

            present = nvidia_present()
        except Exception:
            present = None

        if present is True:
            eco = ECO_ON
        elif present is False:
            eco = ECO_OFF
        else:
            eco = raw_eco

        state = HardwareState(
            available=True,
            vendor=self.vendor,
            eco=eco,
            mux=mux,
            raw={
                "eco_register": -1 if raw_eco is None else raw_eco,
                "mux": -1 if mux is None else mux,
                "dgpu_present": -1 if present is None else int(present),
            },
        )
        state.mode_id = self._derive_mode(eco, mux)
        state.detail = self.describe(state)
        return state

    @staticmethod
    def _derive_mode(eco: Optional[int], mux: Optional[int]) -> str:
        if eco == ECO_OFF:
            return "igpu_only"
        if mux == MUX_ULTIMATE:
            return "ultimate"
        if mux == MUX_IGPU:
            return "igpu_display"
        return "standard"

    def describe(self, state: HardwareState) -> str:
        if not state.available:
            return state.detail
        dgpu = {ECO_ON: "独显已开启", ECO_OFF: "独显已关闭（省电）", None: "独显状态未知"}.get(
            state.eco, "?"
        )
        return f"{dgpu}；显示通路：{state.mux_label}"


    def modes(self) -> List[HardwareMode]:
        state = self.read_state()
        if not state.available:
            return []
        modes = [
            HardwareMode(
                "dgpu_on",
                "独显常驻",
                "保持独显通电（标准混合模式），退出游戏后也不会被切回核显",
                needs_reboot=False,
                eco=ECO_ON,
            ),
            HardwareMode(
                "dgpu_off",
                "关闭独显",
                "切断独显供电，最省电、发热最低；需要独显时会自动开启",
                needs_reboot=False,
                eco=ECO_OFF,
            ),
        ]
        if state.mux is not None:
            modes.append(
                HardwareMode(
                    "ultimate",
                    "独显直连",
                    "独显直接驱动屏幕，性能最高；切换会黑屏数秒并需要重启",
                    needs_reboot=True,
                    may_blackout=True,
                    eco=ECO_ON,
                    mux=MUX_ULTIMATE,
                )
            )
            modes.append(
                HardwareMode(
                    "standard",
                    "标准混合",
                    "核显输出、独显渲染（出厂默认）；从独显直连切回时需要重启",
                    needs_reboot=state.mux == MUX_ULTIMATE,
                    eco=ECO_ON,
                    mux=MUX_STANDARD,
                )
            )
        return modes


    def apply_mode(self, mode: HardwareMode) -> HardwareResult:
        if not self.available():
            return HardwareResult(False, "ATKACPI 不可用，无法做硬件切换")

        state = self.read_state()
        if not state.available:
            return HardwareResult(False, "无法读取当前显卡硬件状态")

        blackout = False
        reboot = False

        if mode.mux is not None and state.mux is not None and mode.mux != state.mux:
            if mode.eco == ECO_OFF and state.mux == MUX_ULTIMATE:
                return HardwareResult(
                    False,
                    "当前是「独显直连」，不能关闭独显",
                    detail="必须先把显示通路切回标准模式并重启，否则屏幕会黑掉",
                )
            logger.warning("切换 MUX: %s -> %s（可能黑屏数秒）", state.mux, mode.mux)
            result = self._acpi.set(self._mux_id, mode.mux)
            if result != 1:
                return HardwareResult(
                    False, "MUX 切换被固件拒绝", detail=f"ACPI result={result}"
                )
            blackout = True
            reboot = True
            time.sleep(1.5)
            state = self.read_state()

        if mode.eco is not None and state.eco is not None and mode.eco != state.eco:
            if mode.eco == ECO_OFF and state.mux == MUX_ULTIMATE:
                return HardwareResult(
                    False,
                    "当前是「独显直连」，不能关闭独显",
                    detail="屏幕正由独显驱动，关闭独显会导致黑屏",
                )
            logger.info("切换独显供电: eco %s -> %s", state.eco, mode.eco)
            result = self._acpi.set(self._eco_id, mode.eco)
            if result != 1:
                return HardwareResult(
                    False, "独显供电切换被固件拒绝", detail=f"ACPI result={result}"
                )
            time.sleep(1.0 if mode.eco == ECO_OFF else 0.6)

        after = self.read_state()
        changed = after.mode_id != state.mode_id or after.eco != state.eco
        detail = f"当前：{self.describe(after)}"
        if changed and mode.eco == ECO_ON:
            message = "已把独显切回常驻（标准混合模式）"
        elif changed:
            message = f"已切换到「{mode.label}」"
        else:
            message = f"硬件状态本来就是「{mode.label}」，无需切换"

        return HardwareResult(
            True, message, detail=detail,
            changed=changed, needs_reboot=reboot, may_blackout=blackout,
        )

    def restore(self, state: HardwareState) -> HardwareResult:
        if not self.available() or not state.available:
            return HardwareResult(False, "无法还原：硬件通道不可用")
        current = self.read_state()
        if current.eco == state.eco and current.mux == state.mux:
            return HardwareResult(True, "无需还原", changed=False)

        if state.eco is not None and current.eco != state.eco:
            if state.eco == ECO_OFF and (current.mux == MUX_ULTIMATE):
                return HardwareResult(False, "还原被阻止", detail="独显直连下不能关独显")
            if self._acpi.set(self._eco_id, state.eco) != 1:
                return HardwareResult(False, "还原独显供电失败")
            time.sleep(0.6)

        if state.mux is not None:
            current = self.read_state()
            if current.mux != state.mux:
                if self._acpi.set(self._mux_id, state.mux) != 1:
                    return HardwareResult(False, "还原显示通路失败", needs_reboot=True)
                return HardwareResult(
                    True, "已还原显示通路", changed=True, needs_reboot=True, may_blackout=True
                )

        after = self.read_state()
        return HardwareResult(True, f"已还原硬件状态（{self.describe(after)}）", changed=True)

    def estimate_power_state(self) -> str:
        state = self.read_state()
        if not state.available:
            return "硬件通道不可用"
        if state.eco == ECO_OFF:
            return "独显已关闭 · 省电中"
        if state.mux == MUX_ULTIMATE:
            return "独显直连 · 性能最强"
        return "独显已开启 · 标准混合"

class NullHardware(HardwareBackend):
    name = "generic"
    vendor = ""
    description = "本机没有可用的厂商硬件通道，将使用「注册表偏好 + 守护」方案"

    def available(self) -> bool:
        return False

    def read_state(self) -> HardwareState:
        return HardwareState(
            available=False, vendor="", detail="本机没有厂商 ACPI 显卡通道（非 ASUS 机型属于正常情况）"
        )

def detect_hardware() -> HardwareBackend:
    if not is_windows():
        return NullHardware()

    asus = AsusHardware()
    try:
        if asus.available():
            state = asus.read_state()
            logger.info("检测到 ASUS 硬件通道：%s", state.detail)
            return asus
    except Exception as exc:
        logger.debug("ASUS 硬件探测异常: %s", exc)

    logger.info("未检测到厂商硬件通道，使用注册表偏好方案")
    return NullHardware()


def nvidia_power_state() -> Dict[str, object]:
    if not is_windows():
        return {}
    try:
        from .paths import run_capture

        out = run_capture(
            ["nvidia-smi", "--query-gpu=pstate,power.draw,utilization.gpu",
             "--format=csv,noheader,nounits"],
            timeout=3,
        )
        if out.returncode != 0:
            return {}
        parts = [p.strip() for p in out.stdout.strip().splitlines()[0].split(",")]
        return {
            "pstate": parts[0],
            "power_w": float(parts[1]) if len(parts) > 1 else None,
            "util": float(parts[2]) if len(parts) > 2 else None,
        }
    except Exception:
        return {}
