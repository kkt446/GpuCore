
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .branding import (
    APP_NAME, APP_NAME_CN, APP_TAGLINE, APP_VERSION, LEGACY_RUN_NAMES, config_path, log_path,
)
from .hardware import (
    ATKACPI_PATH as ATKACPI,
    AtkAcpi,
    DeviceMap,
    ECO_OFF,
    ECO_ON,
    MUX_IGPU,
    MUX_STANDARD,
    MUX_ULTIMATE,
    SENSOR_DEVICES,
    probe_devices,
    read_eco,
    read_mux,
    write_eco,
    write_mux,
)


CONTROL_CODE = 0x0022240C
DSTS = 0x53545344
DEVS = 0x53564544

NV_SERVICES = ("NVDisplay.ContainerLocalSystem", "NvContainerLocalSystem")

BG = "#F3F3F3"
CARD = "#FFFFFF"
BTN = "#FFFFFF"
BTN_HOVER = "#EDEDED"
BORDER = "#E2E2E2"
TEXT = "#1B1B1B"
DIM = "#5A5A5A"
FAINT = "#8A8A8A"
ACCENT = "#00A3EE"
GREEN = "#38A169"
ORANGE = "#DD6B20"
PURPLE = "#805AD5"
RED = "#E53E3E"
BAR_TRACK = "#E6E6E6"
CARD_BORDER = "#E4E4E4"

FONT = "Microsoft YaHei UI"

_scale = 1.0


def px(v: float) -> int:
    return int(round(v * _scale))


FONT_BOOST = 1.25


def font(size: int, bold: bool = False) -> tuple:
    return (FONT, -px(size * FONT_BOOST), "bold" if bold else "normal")


def prepare_dpi() -> float:
    global _scale
    try:
        user32 = ctypes.WinDLL("user32")
        try:
            user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass
        dpi = user32.GetDpiForSystem()
        if dpi:
            _scale = dpi / 96.0
    except Exception:
        _scale = 1.0
    return _scale

Acpi = AtkAcpi

class _FILETIME(ctypes.Structure):
    _fields_ = [("lo", wt.DWORD), ("hi", wt.DWORD)]

    @property
    def value(self) -> int:
        return (self.hi << 32) | self.lo


class _MEMSTATUS(ctypes.Structure):
    _fields_ = [
        ("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class FastSystem:

    def __init__(self) -> None:
        self.k = ctypes.WinDLL("kernel32")
        self._prev = None

    def cpu_percent(self) -> float:
        idle, kern, user = _FILETIME(), _FILETIME(), _FILETIME()
        if not self.k.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)
        ):
            return 0.0
        cur = (idle.value, kern.value, user.value)
        if self._prev is None:
            self._prev = cur
            return 0.0
        d_idle = cur[0] - self._prev[0]
        d_kern = cur[1] - self._prev[1]
        d_user = cur[2] - self._prev[2]
        self._prev = cur
        total = d_kern + d_user
        if total <= 0:
            return 0.0
        return max(0.0, min(100.0, (total - d_idle) * 100.0 / total))

    def memory(self) -> tuple:
        st = _MEMSTATUS()
        st.dwLength = ctypes.sizeof(_MEMSTATUS)
        if not self.k.GlobalMemoryStatusEx(ctypes.byref(st)):
            return 0, 0
        total = int(st.ullTotalPhys)
        return total - int(st.ullAvailPhys), total

class _PdhValue(ctypes.Structure):

    class _U(ctypes.Union):
        _fields_ = [("longValue", ctypes.c_long), ("doubleValue", ctypes.c_double),
                    ("largeValue", ctypes.c_longlong),
                    ("AnsiStringValue", ctypes.c_char_p),
                    ("WideStringValue", ctypes.c_wchar_p)]

    _fields_ = [("CStatus", wt.DWORD), ("value", _U)]


@dataclass
class Snapshot:
    cpu: float = 0.0
    ram_used: int = 0
    ram_total: int = 0
    dgpu: Optional[float] = None
    dgpu_vram: int = 0
    dgpu_vram_total: int = 0
    igpu: float = 0.0
    temp: Optional[int] = None
    mux: Optional[int] = None
    eco: Optional[int] = None
    dgpu_works: bool = False
    igpu_luid: str = ""


_MACHINE_MODEL: Optional[str] = None


def machine_model() -> str:
    global _MACHINE_MODEL
    if _MACHINE_MODEL is not None:
        return _MACHINE_MODEL
    name = ""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\BIOS",
            0, winreg.KEY_READ,
        ) as k:
            for field in ("SystemProductName", "BaseBoardProduct"):
                try:
                    v = winreg.QueryValueEx(k, field)[0]
                except OSError:
                    continue
                v = str(v).strip().strip("\x00")
                if v and v.lower() not in ("system product name", "to be filled by o.e.m.", "default string"):
                    name = v
                    break
    except OSError:
        pass
    if not name:
        try:
            name = os.environ.get("COMPUTERNAME", "") or "本机"
        except Exception:
            name = "本机"
    _MACHINE_MODEL = name
    return name


def child_env() -> dict:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_quiet(cmd, timeout=6) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, env=child_env(),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def nvidia_available() -> bool:
    try:
        r = run_quiet(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
    except Exception:
        return False
    text = ((r.stdout or "") + (r.stderr or "")).lower()
    return r.returncode == 0 and "nvidia" in text


def detect_igpu_luid() -> str:
    import re

    try:
        dxgi = ctypes.WinDLL("dxgi")

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]

        class DESC1(ctypes.Structure):
            _fields_ = [
                ("Description", ctypes.c_wchar * 128),
                ("VendorId", ctypes.c_uint32),
                ("DeviceId", ctypes.c_uint32),
                ("SubSysId", ctypes.c_uint32),
                ("Revision", ctypes.c_uint32),
                ("DedicatedVideoMemory", ctypes.c_size_t),
                ("DedicatedSystemMemory", ctypes.c_size_t),
                ("SharedSystemMemory", ctypes.c_size_t),
                ("AdapterLuid", LUID),
                ("Flags", ctypes.c_uint32),
            ]

        factory = ctypes.c_void_p()
        iid = (ctypes.c_byte * 16).from_buffer_copy(
            __import__("uuid").UUID("{770AAE78-F26F-4DBA-A829-253C83D1B387}").bytes_le
        )
        if dxgi.CreateDXGIFactory1(ctypes.byref(iid), ctypes.byref(factory)) != 0:
            raise OSError("CreateDXGIFactory1 失败")

        vt = ctypes.cast(factory, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        enum_fn = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)
        )(vt[12])

        idx = 0
        integrated = None
        while True:
            ad = ctypes.c_void_p()
            if enum_fn(factory, idx, ctypes.byref(ad)) < 0 or not ad:
                break
            idx += 1
            try:
                avt = ctypes.cast(ad, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                get_desc = ctypes.WINFUNCTYPE(
                    ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(DESC1)
                )(avt[10])
                d = DESC1()
                if get_desc(ad, ctypes.byref(d)) < 0:
                    continue
                name = (d.Description or "").lower()
                video_mem = int(d.DedicatedVideoMemory)
                luid = ((int(d.AdapterLuid.HighPart) & 0xFFFFFFFF) << 32) | int(d.AdapterLuid.LowPart)
                inst = f"0x{luid >> 32:08x}_0x{luid & 0xFFFFFFFF:08x}"

                is_igpu = any(k in name for k in (
                    "intel", "uhd", "iris", "radeon graphics", "vega", "adreno"))
                if not is_igpu and video_mem < 512 * 1024 * 1024 and "nvidia" not in name:
                    is_igpu = True
                if is_igpu:
                    integrated = inst
                    break
            finally:
                try:
                    cvt = ctypes.cast(ad, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                    ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(cvt[2])(ad)
                except Exception:
                    pass
        try:
            cvt = ctypes.cast(factory, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(cvt[2])(factory)
        except Exception:
            pass

        if integrated:
            return integrated
    except Exception:
        pass

    try:
        r = run_quiet([
            "powershell", "-NoProfile", "-Command",
            "(Get-Counter '\\GPU Engine(*)\\Utilization Percentage' -ErrorAction SilentlyContinue)"
            ".CounterSamples | Select-Object -ExpandProperty InstanceName"
        ], timeout=20)
        instances = set()
        for line in (r.stdout or "").splitlines():
            m = re.search(r"luid_(0x[0-9a-fA-F]+)_(0x[0-9a-fA-F]+)", line)
            if m:
                instances.add("%s_%s" % (
                    "0x%08x" % int(m.group(1), 16),
                    "0x%08x" % int(m.group(2), 16),
                ))
        if instances:
            return sorted(instances)[0]
    except Exception:
        pass

    return ""


class Monitor(threading.Thread):

    def __init__(self, interval: float = 1.0, on_log: Optional[Callable[[str], None]] = None) -> None:
        super().__init__(name="monitor", daemon=True)
        self.interval = interval
        self.on_log = on_log
        self.snapshot = Snapshot()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._acpi = AtkAcpi()
        self._dev = DeviceMap()
        self._fast = FastSystem()
        try:
            self._dev = probe_devices(self._acpi)
        except Exception as exc:
            self._dev.notes.append(f"设备号探测异常: {exc}")
        self._igpu_luid = ""
        self._dgpu_checked_at = 0.0
        self._dgpu_works = False
        self._dgpu_cache_seconds = 3.0
        self._dgpu_val: Optional[float] = None
        self._dgpu_vram = 0
        self._dgpu_vram_total = 0
        self._dgpu_fail_streak = 0
        self._igpu_checked_at = 0.0
        self._igpu_cache_seconds = 2.0
        self._igpu_cached = 0.0
        self._pdh_handle = None
        self._pdh_query = None
        self._pdh_counters: Dict[str, object] = {}
        self._sensor_cache: Dict[str, tuple] = {}
        self._sensor_cache_seconds = 2.0

    @property
    def devices(self) -> DeviceMap:
        return self._dev

    def _sensor(self, key: str) -> Optional[int]:
        dev = self._dev.sensors.get(key)
        if dev is None:
            return None
        now = time.monotonic()
        hit = self._sensor_cache.get(key)
        if hit and now - hit[0] < self._sensor_cache_seconds:
            return hit[1]
        val = self._acpi.get(dev)
        self._sensor_cache[key] = (now, val)
        return val

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        try:
            self._igpu_luid = detect_igpu_luid()
            if self.on_log:
                self.on_log(f"核显 LUID: {self._igpu_luid}")
        except Exception:
            self._igpu_luid = "0x00000000_0x0001111f"

        self._fast.cpu_percent()
        self._fast.memory()
        self._sample()
        self._refresh_dgpu()
        self._dgpu_checked_at = time.monotonic()
        self._igpu_checked_at = 0.0
        self._dgpu_fail_streak = 0 if self._dgpu_works else 0

        while not self._stop_event.is_set():
            try:
                self._sample()
            except Exception as exc:
                if self.on_log:
                    self.on_log(f"采集异常: {exc}")
            self._stop_event.wait(self.interval)

    def _sample(self) -> None:
        snap = Snapshot()
        snap.igpu_luid = self._igpu_luid

        snap.cpu = self._fast.cpu_percent()
        snap.ram_used, snap.ram_total = self._fast.memory()
        snap.mux = read_mux(self._acpi, self._dev.mux)
        snap.eco = read_eco(self._acpi, self._dev.eco)
        t = self._sensor("temp_cpu")
        if t is not None and 0 < t < 120:
            snap.temp = t
        snap.dgpu_works = self._dgpu_works
        snap.igpu = self._igpu_cached

        with self._lock:
            self.snapshot = snap

        now = time.monotonic()
        if now - self._dgpu_checked_at > self._dgpu_cache_seconds:
            self._dgpu_checked_at = now
            threading.Thread(target=self._refresh_dgpu, daemon=True).start()
        if now - self._igpu_checked_at > self._igpu_cache_seconds:
            self._igpu_checked_at = now
            threading.Thread(target=self._refresh_igpu, daemon=True).start()

    def _refresh_dgpu(self) -> None:
        works = nvidia_available()
        dgpu = None
        vram = vram_total = 0
        if works:
            try:
                r = run_quiet([
                    "nvidia-smi",
                    "--query-gpu=utilization.gpu,memory.used,memory.total",
                    "--format=csv,noheader,nounits",
                ])
                parts = [p.strip() for p in (r.stdout or "").strip().splitlines()[0].split(",")]
                if len(parts) >= 3:
                    dgpu = float(parts[0])
                    vram = int(float(parts[1]) * 1024 * 1024)
                    vram_total = int(float(parts[2]) * 1024 * 1024)
            except Exception:
                with self._lock:
                    dgpu = self._dgpu_val if self._dgpu_val is not None else 0.0
                    vram = self._dgpu_vram
                    vram_total = self._dgpu_vram_total

        with self._lock:
            if works:
                self._dgpu_fail_streak = 0
                self._dgpu_works = True
            else:
                self._dgpu_fail_streak += 1
                if self._dgpu_fail_streak >= 3:
                    self._dgpu_works = False
                    dgpu = None
                else:
                    dgpu = self._dgpu_val
                    vram = self._dgpu_vram
                    vram_total = self._dgpu_vram_total

            if dgpu is not None:
                self._dgpu_val = dgpu
                self._dgpu_vram = vram
                self._dgpu_vram_total = vram_total

            self.snapshot.dgpu_works = self._dgpu_works
            self.snapshot.dgpu = dgpu if self._dgpu_works else None
            self.snapshot.dgpu_vram = vram
            self.snapshot.dgpu_vram_total = vram_total

    def _refresh_igpu(self) -> None:
        value = 0.0
        try:
            value = self._pdh_igpu()
        except Exception:
            pass
        with self._lock:
            self._igpu_cached = value
            self.snapshot.igpu = value

    def _pdh_igpu(self) -> float:
        try:
            pdh = ctypes.WinDLL("pdh")
        except OSError:
            return 0.0

        PDH_FMT_DOUBLE = 0x00000200
        PDH_FMT_NOCAP100 = 0x00008000
        VAL = _PdhValue

        pdh.PdhOpenQueryW.argtypes = [wt.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wt.HANDLE)]
        pdh.PdhOpenQueryW.restype = ctypes.c_long
        pdh.PdhAddEnglishCounterW.argtypes = [
            wt.HANDLE, wt.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wt.HANDLE)
        ]
        pdh.PdhAddEnglishCounterW.restype = ctypes.c_long
        pdh.PdhCollectQueryData.argtypes = [wt.HANDLE]
        pdh.PdhCollectQueryData.restype = ctypes.c_long
        pdh.PdhGetFormattedCounterValue.argtypes = [
            wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.POINTER(VAL)
        ]
        pdh.PdhGetFormattedCounterValue.restype = ctypes.c_long
        pdh.PdhExpandWildCardPathW.argtypes = [
            wt.LPCWSTR, wt.LPCWSTR, wt.LPWSTR, ctypes.POINTER(wt.DWORD), wt.DWORD
        ]
        pdh.PdhExpandWildCardPathW.restype = ctypes.c_long
        pdh.PdhRemoveCounter.argtypes = [wt.HANDLE]
        pdh.PdhRemoveCounter.restype = ctypes.c_long

        if self._pdh_handle is None:
            handle = wt.HANDLE()
            if pdh.PdhOpenQueryW(None, 0, ctypes.byref(handle)) != 0:
                return 0.0
            self._pdh_handle = handle
            self._pdh_query = pdh
            self._pdh_counters = {}
        query = self._pdh_query
        size = wt.DWORD(0)
        pdh.PdhExpandWildCardPathW(None, r"\GPU Engine(*)\Utilization Percentage",
                                   None, ctypes.byref(size), 0)
        if size.value == 0:
            time.sleep(0.2)
            pdh.PdhExpandWildCardPathW(None, r"\GPU Engine(*)\Utilization Percentage",
                                       None, ctypes.byref(size), 0)
            if size.value == 0:
                return 0.0
        buf = ctypes.create_unicode_buffer(size.value)
        pdh.PdhExpandWildCardPathW(None, r"\GPU Engine(*)\Utilization Percentage",
                                   buf, ctypes.byref(size), 0)
        paths = [p for p in buf[: size.value].split("\0") if p]

        key = (self._igpu_luid or "").lower()
        wanted = [p for p in paths if key and key in p.lower()] if key else []
        for p in list(self._pdh_counters):
            if p not in wanted:
                query.PdhRemoveCounter(self._pdh_counters.pop(p))
        for p in wanted:
            if p in self._pdh_counters:
                continue
            h = ctypes.c_void_p()
            if query.PdhAddEnglishCounterW(self._pdh_handle, p, 0, ctypes.byref(h)) == 0:
                self._pdh_counters[p] = h

        if not self._pdh_counters:
            return 0.0

        if query.PdhCollectQueryData(self._pdh_handle) != 0:
            return 0.0

        best = 0.0
        for h in self._pdh_counters.values():
            v = VAL()
            fmt = wt.DWORD(0)
            if query.PdhGetFormattedCounterValue(
                h, PDH_FMT_DOUBLE | PDH_FMT_NOCAP100, ctypes.byref(fmt), ctypes.byref(v)
            ) == 0:
                engine = v.value.doubleValue
                best = max(best, min(100.0, engine))
        return round(best, 0)

    def read(self) -> Snapshot:
        with self._lock:
            s = self.snapshot
            return Snapshot(**vars(s))

    def switch_eco(self, off: bool, log: Callable[[str], None]) -> bool:
        if self._dev.eco is None:
            log("这台机器没有可用的独显供电设备号，无法切换")
            return False

        if off:
            names = self._kill_gpu_apps(log)
            if names:
                time.sleep(1.0)
            log("释放显卡句柄")
            self._release_gpu_handles()
            stopped = stop_nv_services()
            if stopped:
                log("已停止 NVIDIA 服务：" + "、".join(stopped))
            else:
                log("NVIDIA 服务未停（无管理员权限时属正常）")

        log(f"写入 ACPI：GPUEco(0x{self._dev.eco:08X}) = {ECO_OFF if off else ECO_ON}")
        res = write_eco(self._acpi, self._dev.eco, off)
        if res != 1:
            log(f"固件拒绝写入（result={res}）")
            if not off:
                start_nv_services()
            return False

        if off:
            set_gpu_preference(1)
            log("已切到集显模式（独显断电）")
        else:
            log("恢复 NVIDIA 服务")
            started = start_nv_services()
            if started:
                log("已启动 NVIDIA 服务：" + "、".join(started))
            time.sleep(2.0)
            set_gpu_preference(2)
            log("已切到标准模式（独显通电，新程序用独显）")
        self._sensor_cache.clear()
        return True

    def _kill_gpu_apps(self, log: Callable[[str], None]) -> List[str]:
        protected = {
            "system", "registry", "memory compression", "csrss.exe", "wininit.exe",
            "winlogon.exe", "services.exe", "lsass.exe", "smss.exe", "svchost.exe",
            "dwm.exe", "explorer.exe", "fontdrvhost.exe", "sihost.exe", "ctfmon.exe",
            "taskhostw.exe", "runtimebroker.exe", "shellexperiencehost.exe",
            "startmenuexperiencehost.exe", "searchhost.exe", "textinputhost.exe",
            "msmpeng.exe", "securityhealthservice.exe", "nvcontainer.exe",
            "nvdisplay.container.exe", "python.exe", "pythonw.exe", "gpucore.exe",
        }
        me = os.getpid()
        targets: List[tuple] = []

        try:
            r = run_quiet([
                "nvidia-smi",
                "--query-compute-apps=pid,process_name,used_memory",
                "--format=csv,noheader,nounits",
            ])
            for line in (r.stdout or "").strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 2:
                    continue
                try:
                    pid = int(parts[0])
                except ValueError:
                    continue
                name = Path(parts[1]).name if parts[1] else f"pid-{pid}"
                targets.append((pid, name))
        except Exception:
            pass

        keep = []
        for pid, name in targets:
            low = name.lower()
            if pid in (0, 4, me) or low in protected:
                continue
            if any(low == n for n, _ in keep):
                continue
            keep.append((pid, name))

        if not keep:
            log("没有程序占用独显")
            return []

        log("正在结束占用独显的程序：" + "、".join(n for _, n in keep[:5]))
        killed = []
        for pid, name in keep:
            try:
                if run_quiet(["taskkill", "/F", "/PID", str(pid)], timeout=10).returncode == 0:
                    killed.append(name)
            except Exception:
                pass
        return killed

    def _release_gpu_handles(self) -> None:
        try:
            if self._pdh_query is not None:
                self._pdh_query.close()
        except Exception:
            pass
        self._pdh_query = None
        self._pdh_handle = None
        self._pdh_counters.clear()
        try:
            ctypes.WinDLL("kernel32").SetProcessWorkingSetSize(
                ctypes.WinDLL("kernel32").GetCurrentProcess(),
                ctypes.c_size_t(-1), ctypes.c_size_t(-1),
            )
        except Exception:
            pass
        time.sleep(0.5)

    def switch_mux(self, value: int, log: Callable[[str], None]) -> bool:
        if self._dev.mux is None:
            log("这台机器没有可用的显示通路（MUX）设备号，无法切换")
            return False
        cur = read_mux(self._acpi, self._dev.mux)
        if cur == value:
            log(f"显示通路已经是 {value}")
            return True
        if value == MUX_IGPU and self._dgpu_works:
            log("独显在工作，不能切集显输出")
            return False
        log(f"写入 GPUMux(0x{self._dev.mux:08X}) = {value}（可能黑屏数秒）")
        if write_mux(self._acpi, self._dev.mux, value) != 1:
            log("固件拒绝 MUX 写入")
            return False
        log("已接受（MUX 切换需要重启才生效）")
        time.sleep(3)
        return True


def set_gpu_preference(pref: int) -> bool:
    import winreg

    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\DirectX\UserGpuPreferences",
            0, winreg.KEY_WRITE,
        ) as key:
            winreg.SetValueEx(key, "GpuPreference", 0, winreg.REG_SZ, f"GpuPreference={pref};")
        return True
    except OSError:
        return False


def stop_nv_services() -> List[str]:
    stopped = []
    for name in NV_SERVICES:
        try:
            if run_quiet(["net", "stop", name], timeout=20).returncode == 0:
                stopped.append(name)
        except Exception:
            pass
    return stopped


def start_nv_services() -> List[str]:
    started = []
    for name in NV_SERVICES:
        try:
            if run_quiet(["net", "start", name], timeout=30).returncode == 0:
                started.append(name)
        except Exception:
            pass
    return started

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012

_VK: Dict[str, int] = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "esc": 0x1B, "space": 0x20,
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
}
for _i in range(1, 25):
    _VK[f"f{_i}"] = 0x6F + _i
for _c in "abcdefghijklmnopqrstuvwxyz":
    _VK[_c] = ord(_c.upper())
for _d in "0123456789":
    _VK[_d] = ord(_d)

_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT,
         "shift": MOD_SHIFT, "win": MOD_WIN, "super": MOD_WIN}


@dataclass
class HotkeySpec:
    text: str
    modifiers: int
    vk: int
    key_name: str

    def describe(self) -> str:
        parts = []
        if self.modifiers & MOD_CONTROL:
            parts.append("Ctrl")
        if self.modifiers & MOD_ALT:
            parts.append("Alt")
        if self.modifiers & MOD_SHIFT:
            parts.append("Shift")
        if self.modifiers & MOD_WIN:
            parts.append("Win")
        parts.append(self.key_name.upper() if len(self.key_name) == 1 else self.key_name.capitalize())
        return "+".join(parts)


def parse_hotkey(text: str) -> HotkeySpec:
    import re

    tokens = [t for t in re.split(r"[+\-\s]+", (text or "").strip().lower()) if t]
    if not tokens:
        raise ValueError("快捷键为空")
    mods = 0
    keys = []
    for t in tokens:
        if t in _MODS:
            mods |= _MODS[t]
        else:
            keys.append(t)
    if len(keys) != 1:
        raise ValueError("需要一个主键，例如 ctrl+alt+g")
    if keys[0] not in _VK:
        raise ValueError(f"不认识的按键 {keys[0]!r}")
    if mods == 0:
        raise ValueError("全局热键必须包含修饰键（Ctrl / Alt / Shift / Win）")
    return HotkeySpec(text.strip(), mods, _VK[keys[0]], keys[0])


class HotkeyListener:

    def __init__(self, text: str, callback: Callable[[], None]) -> None:
        self.spec = parse_hotkey(text)
        self.callback = callback
        self.error = ""
        self._thread: Optional[threading.Thread] = None
        self._tid = 0
        self._ready = threading.Event()
        self._ok = False
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def start(self, wait: float = 3.0) -> bool:
        self._thread = threading.Thread(target=self._run, daemon=True, name="hotkey")
        self._thread.start()
        self._ready.wait(wait)
        return self._ok

    def _run(self) -> None:
        self._tid = self._kernel32.GetCurrentThreadId()
        msg = wt.MSG()
        self._user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        hk_id = 0xC0DE
        if not self._user32.RegisterHotKey(
            None, hk_id, self.spec.modifiers | MOD_NOREPEAT, self.spec.vk
        ):
            self.error = f"注册失败 WinError={ctypes.get_last_error()}"
            self._ready.set()
            return
        self._ok = True
        self._ready.set()
        try:
            while True:
                r = self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r in (0, -1):
                    break
                if msg.message == WM_HOTKEY and msg.wParam == hk_id:
                    try:
                        self.callback()
                    except Exception:
                        pass
        finally:
            self._user32.UnregisterHotKey(None, hk_id)

    def stop(self) -> None:
        if self._tid:
            self._user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=1.5)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = APP_NAME


def on_ac_power() -> bool:
    try:
        class SPS(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_byte),
                        ("BatteryFlag", ctypes.c_byte),
                        ("BatteryLifePercent", ctypes.c_byte),
                        ("Reserved1", ctypes.c_byte),
                        ("BatteryLifeTime", wt.DWORD),
                        ("BatteryFullLifeTime", wt.DWORD)]

        st = SPS()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
            return st.ACLineStatus == 1
    except Exception:
        pass
    return True


def reboot_now(delay: int = 3) -> bool:
    try:
        subprocess.Popen(
            ["shutdown", "/r", "/t", str(delay), "/c", f"{APP_NAME}: 显卡模式切换，需要重启生效"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True
    except Exception:
        return False


def autostart_command() -> str:
    from .branding import bundle_dir, portable_mode

    if portable_mode():
        exe = bundle_dir() / "GpuCore.exe"
        if exe.exists():
            return f'"{exe}"'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        pythonw = Path(sys.executable)
    return f'"{pythonw}" -m {APP_NAME.lower()}'


def get_autostart() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
        return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> bool:
    import winreg

    cmd = autostart_command()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_WRITE) as k:
            if enabled:
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, cmd)
                for legacy in LEGACY_RUN_NAMES:
                    if legacy != RUN_NAME:
                        try:
                            winreg.DeleteValue(k, legacy)
                        except FileNotFoundError:
                            pass
            else:
                try:
                    winreg.DeleteValue(k, RUN_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False

CONFIG_PATH = config_path()


def load_config() -> dict:
    import json

    defaults = {"hotkey": "ctrl+alt+g", "hotkey_enabled": True, "always_on_top": True}
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                defaults.update(data)
    except Exception:
        pass
    return defaults


def save_config(cfg: dict) -> None:
    import json

    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        pass

class Card(tk.Frame):

    def __init__(self, master, title: str = "", **kw):
        super().__init__(master, bg=CARD, highlightbackground=CARD_BORDER,
                         highlightthickness=px(1), bd=0, **kw)
        self.header = None
        self.right_label = None
        if title:
            self.header = tk.Frame(self, bg=CARD)
            self.header.pack(fill="x", padx=px(12), pady=(px(9), px(2)))
            tk.Label(self.header, text=title, bg=CARD, fg=TEXT, font=font(10, True),
                     anchor="w").pack(side="left")
            self.right_label = tk.Label(self.header, text="", bg=CARD, fg=DIM,
                                        font=font(9), anchor="e")
            self.right_label.pack(side="right")

    def set_right(self, text: str) -> None:
        if self.right_label is not None:
            self.right_label.configure(text=text)


class StatBar(tk.Frame):

    def __init__(self, master, label: str, color: str, width: int = 344):
        super().__init__(master, bg=CARD)
        self.color = color
        self.bar_w = px(width)
        self.bar_h = px(5)

        top = tk.Frame(self, bg=CARD)
        top.pack(fill="x")
        tk.Label(top, text=label, bg=CARD, fg=DIM, font=font(8), anchor="w").pack(side="left")
        self.value = tk.Label(top, text="--", bg=CARD, fg=TEXT, font=font(8), anchor="e")
        self.value.pack(side="right")

        self.canvas = tk.Canvas(self, width=self.bar_w, height=self.bar_h, bg=CARD,
                                highlightthickness=0, bd=0)
        self.canvas.pack(fill="x", pady=(px(3), 0))
        self._pct = 0.0
        self._draw()

    def _rr(self, x1, y1, x2, y2, r, **kw):
        r = max(1, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return self.canvas.create_polygon(pts, smooth=True, **kw)

    def _draw(self):
        self.canvas.delete("all")
        w = max(self.canvas.winfo_width(), self.bar_w)
        h = self.bar_h
        self._rr(0, 0, w, h, h / 2, fill=BAR_TRACK, outline="")
        fw = max(0.0, min(1.0, self._pct / 100.0)) * w
        if fw >= 1:
            self._rr(0, 0, max(fw, h), h, h / 2, fill=self.color, outline="")

    def set(self, pct: float, text: str = "") -> None:
        self._pct = max(0.0, min(100.0, float(pct)))
        if text:
            self.value.configure(text=text)
        self._draw()


class ModeButton(tk.Frame):

    def __init__(self, master, text: str, color: str, command=None, width=110, height=44):
        super().__init__(master, bg=BORDER, bd=0, highlightthickness=0, cursor="hand2")
        self.command = command
        self.color = color
        self._selected = False
        self._disabled = False
        self._inner = tk.Frame(self, bg=BTN, bd=0, highlightthickness=0)
        self._inner.pack(fill="both", expand=True, padx=px(2), pady=px(2))
        self.label = tk.Label(self._inner, text=text, bg=BTN, fg=TEXT, font=font(8, True),
                              wraplength=px(width - 12), justify="center")
        self.label.pack(expand=True, fill="both")
        for w in (self, self._inner, self.label):
            w.bind("<Button-1>", self._click)
            w.bind("<Enter>", self._enter)
            w.bind("<Leave>", self._leave)
        self.configure(width=px(width), height=px(height))
        self.pack_propagate(False)

    def _click(self, _e=None):
        if self._disabled:
            return
        if self.command:
            self.command()

    def _paint(self, bg, border, fg=TEXT):
        self.configure(bg=border)
        self._inner.configure(bg=bg)
        self.label.configure(bg=bg, fg=fg)

    def _enter(self, _e=None):
        if self._disabled or self._selected:
            return
        self._paint(BTN_HOVER, BORDER)

    def _leave(self, _e=None):
        if self._disabled or self._selected:
            return
        self._paint(BTN, BORDER)

    def set_selected(self, on: bool) -> None:
        self._selected = on
        if on:
            self._paint(BTN_HOVER, self.color, TEXT)
        else:
            self._paint(BTN, BORDER, DIM)

    def set_disabled(self, on: bool) -> None:
        self._disabled = on
        self.configure(cursor="arrow" if on else "hand2")
        if on:
            self._paint(BG, BORDER, FAINT)
        elif not self._selected:
            self._paint(BTN, BORDER, DIM)


class App:
    def __init__(self) -> None:
        prepare_dpi()
        self.config = load_config()
        self.hotkey: Optional[HotkeyListener] = None
        self._hotkey_fired = 0

        self.monitor = Monitor(interval=1.0, on_log=self._log_from_thread)
        self._log_queue: List[str] = []
        self._busy = False

        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} · {APP_NAME_CN}")
        w, h = px(400), px(660)
        self.root.geometry(f"{w}x{h}")
        self.root.resizable(False, False)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self._hide)

        self.root.attributes("-topmost", bool(self.config.get("always_on_top", True)))

        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{sw - w - px(8)}+{sh - h - px(48)}")

        self._build()
        self.monitor.start()
        self._setup_hotkey()
        self._tick()

    def _setup_hotkey(self) -> None:
        if self.hotkey:
            self.hotkey.stop()
            self.hotkey = None
        if not self.config.get("hotkey_enabled", True):
            self._log("快捷键已关闭")
            return

        text = str(self.config.get("hotkey", "ctrl+alt+g"))
        try:
            spec = parse_hotkey(text)
        except ValueError as exc:
            self._log(f"快捷键无效（{text}）：{exc}")
            return

        listener = HotkeyListener(text, lambda: setattr(self, "_hotkey_fired", self._hotkey_fired + 1))
        if listener.start():
            self.hotkey = listener
            self._log(f"快捷键已启用：{spec.describe()}（呼出 / 收起）")
        else:
            self._log(f"快捷键 {spec.describe()} 注册失败：{listener.error}（可能被占用）")

    def _drain_hotkey(self) -> None:
        if self._hotkey_fired:
            self._hotkey_fired = 0
            if self.root.state() == "normal" and self.root.winfo_viewable():
                self._hide()
            else:
                self._show()

    def _show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.attributes("-topmost", bool(self.config.get("always_on_top", True)))
            self.root.focus_force()
        except Exception:
            pass

    def _hide(self) -> None:
        self.root.withdraw()

    def _open_settings(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("设置")
        win.configure(bg=BG)
        win.resizable(False, False)
        win.attributes("-topmost", True)
        w, h = px(380), px(330)
        win.geometry(f"{w}x{h}+{self.root.winfo_x() - w - px(10)}+{self.root.winfo_y()}")

        tk.Label(win, text="设置", bg=BG, fg=TEXT, font=font(12, True)).pack(
            anchor="w", padx=px(14), pady=(px(12), px(8)))

        card = tk.Frame(win, bg=CARD, highlightbackground="#2C2C2C", highlightthickness=px(1))
        card.pack(fill="x", padx=px(12), pady=(0, px(8)))

        var_enabled = tk.BooleanVar(value=bool(self.config.get("hotkey_enabled", True)))
        row = tk.Frame(card, bg=CARD)
        row.pack(fill="x", padx=px(10), pady=(px(9), px(3)))
        tk.Label(row, text="呼出快捷键", bg=CARD, fg=TEXT, font=font(9, True)).pack(side="left")

        chk = tk.Checkbutton(
            row, text="启用", variable=var_enabled, bg=CARD, fg=DIM, font=font(8),
            selectcolor=CARD, activebackground=CARD, activeforeground=TEXT,
            highlightthickness=0, bd=0)
        chk.pack(side="right")

        entry = tk.Entry(card, bg="#2A2A2A", fg=TEXT, font=font(9), bd=0,
                         insertbackground=TEXT, justify="center", state="readonly",
                         readonlybackground="#2A2A2A")
        entry.configure(state="normal")
        entry.insert(0, str(self.config.get("hotkey", "ctrl+alt+g")))
        entry.configure(state="readonly")
        entry.pack(fill="x", padx=px(10), pady=(px(3), px(2)), ipady=px(5))

        hint = tk.Label(card, text="点上面的框，然后直接按你想用的组合键", bg=CARD, fg=FAINT,
                        font=font(7), anchor="w")
        hint.pack(fill="x", padx=px(10), pady=(0, px(9)))

        current: Dict[str, str] = {"text": str(self.config.get("hotkey", "ctrl+alt+g"))}
        recording = {"on": False}

        def set_entry(text: str) -> None:
            entry.configure(state="normal")
            entry.delete(0, "end")
            entry.insert(0, text)
            entry.configure(state="readonly")
            current["text"] = text

        def on_focus_in(_e=None) -> None:
            recording["on"] = True
            hint.configure(text="● 正在录制…请按组合键（Esc 取消）", fg=ACCENT)

        def on_focus_out(_e=None) -> None:
            recording["on"] = False
            hint.configure(text="点上面的框，然后直接按你想用的组合键", fg=FAINT)

        def on_key(event) -> str:
            if not recording["on"]:
                return ""
            keysym = (event.keysym or "").lower()

            if keysym in ("control_l", "control_r", "alt_l", "alt_r",
                          "shift_l", "shift_r", "super_l", "super_r", "win_l", "win_r"):
                mods = []
                if event.state & 0x4:
                    mods.append("ctrl")
                if event.state & 0x8 or event.state & 0x20000:
                    mods.append("alt")
                if event.state & 0x1:
                    mods.append("shift")
                if event.state & 0x40:
                    mods.append("win")
                hint.configure(text="● " + ("+".join(mods) + "+…" if mods else "正在录制…"), fg=ACCENT)
                return "break"

            if keysym == "escape":
                recording["on"] = False
                hint.configure(text="已取消", fg=ORANGE)
                return "break"

            name = keysym
            if len(keysym) == 1:
                name = keysym
            elif keysym.startswith("f") and keysym[1:].isdigit():
                name = keysym
            else:
                alias = {
                    "space": "space", "return": "enter", "tab": "tab",
                    "backspace": "backspace", "delete": "delete",
                    "home": "home", "end": "end", "prior": "pageup",
                    "next": "pagedown", "insert": "insert",
                    "minus": "-", "equal": "=", "bracketleft": "[",
                    "bracketright": "]", "backslash": "\\", "semicolon": ";",
                    "apostrophe": "'", "comma": ",", "period": ".", "slash": "/",
                    "grave": "`",
                }.get(keysym, keysym)

            mods = []
            if event.state & 0x4:
                mods.append("ctrl")
            if event.state & 0x8 or event.state & 0x20000:
                mods.append("alt")
            if event.state & 0x1:
                mods.append("shift")
            if event.state & 0x40:
                mods.append("win")

            combo = "+".join(mods + [name]) if mods else name
            try:
                spec = parse_hotkey(combo)
            except ValueError as exc:
                hint.configure(text=f"✗ {exc}", fg=RED)
                return "break"

            set_entry(combo)
            hint.configure(text=f"已录制：{spec.describe()}", fg=GREEN)
            recording["on"] = False
            win.focus_set()
            return "break"

        entry.bind("<Button-1>", lambda _e: (entry.focus_set(), on_focus_in()))
        entry.bind("<FocusIn>", on_focus_in)
        entry.bind("<FocusOut>", on_focus_out)
        entry.bind("<KeyPress>", on_key)

        card2 = tk.Frame(win, bg=CARD, highlightbackground="#2C2C2C", highlightthickness=px(1))
        card2.pack(fill="x", padx=px(12), pady=(0, px(8)))
        var_auto = tk.BooleanVar(value=get_autostart())
        tk.Checkbutton(
            card2, text="开机自动启动（常驻后台）", variable=var_auto, bg=CARD, fg=TEXT,
            font=font(9), selectcolor=CARD, activebackground=CARD, activeforeground=TEXT,
            highlightthickness=0, bd=0, anchor="w").pack(
            fill="x", padx=px(10), pady=px(10))

        card3 = tk.Frame(win, bg=CARD, highlightbackground="#2C2C2C", highlightthickness=px(1))
        card3.pack(fill="x", padx=px(12), pady=(0, px(8)))
        var_top = tk.BooleanVar(value=bool(self.config.get("always_on_top", True)))
        tk.Checkbutton(
            card3, text="窗口始终置顶", variable=var_top, bg=CARD, fg=TEXT, font=font(9),
            selectcolor=CARD, activebackground=CARD, activeforeground=TEXT,
            highlightthickness=0, bd=0, anchor="w").pack(
            fill="x", padx=px(10), pady=px(10))

        status = tk.Label(win, text="", bg=BG, fg=GREEN, font=font(8), anchor="w",
                          wraplength=px(350), justify="left")
        status.pack(fill="x", padx=px(14))

        def apply_and_close() -> None:
            self.config["hotkey_enabled"] = bool(var_enabled.get())
            self.config["hotkey"] = current["text"] or "ctrl+alt+g"
            self.config["always_on_top"] = bool(var_top.get())
            save_config(self.config)

            ok_auto = set_autostart(bool(var_auto.get()))
            self.root.attributes("-topmost", self.config["always_on_top"])
            self._setup_hotkey()
            self._log(
                f"设置已保存：快捷键 {self.config['hotkey']}"
                f"（{'启用' if self.config['hotkey_enabled'] else '关闭'}），"
                f"开机自启 {'开' if var_auto.get() else '关'}"
                + ("" if ok_auto else "（自启写入失败）")
            )
            win.destroy()

        btns = tk.Frame(win, bg=BG)
        btns.pack(fill="x", padx=px(12), pady=(px(4), px(12)))
        for text, cmd, color in (
            ("保存", apply_and_close, ACCENT),
            ("取消", win.destroy, BTN),
        ):
            b = tk.Label(btns, text=text, bg=color, fg=TEXT, font=font(9, True),
                         padx=px(18), pady=px(6), cursor="hand2")
            b.pack(side="right", padx=(px(6), 0))
            b.bind("<Button-1>", lambda _e, c=cmd: c())

    def _build(self) -> None:
        pad = px(8)

        c1 = Card(self.root)
        c1.pack(fill="x", padx=pad, pady=(pad, px(6)))
        self.model_lbl = tk.Label(c1, text="读取中…", bg=CARD, fg=FAINT, font=font(8), anchor="w")
        self.model_lbl.pack(fill="x", padx=px(9), pady=(px(7), 0))
        self.mode_lbl = tk.Label(c1, text="—", bg=CARD, fg=ACCENT, font=font(16, True), anchor="w")
        self.mode_lbl.pack(fill="x", padx=px(9))
        self.info_lbl = tk.Label(c1, text="", bg=CARD, fg=DIM, font=font(9), anchor="w")
        self.info_lbl.pack(fill="x", padx=px(9), pady=(0, px(9)))

        c2 = Card(self.root, "资源监视")
        c2.pack(fill="x", padx=pad, pady=(0, px(6)))
        body2 = tk.Frame(c2, bg=CARD)
        body2.pack(fill="x", padx=px(9), pady=(px(5), px(9)))
        self.bar_dgpu = StatBar(body2, "独显 GPU", GREEN, width=364)
        self.bar_dgpu.pack(fill="x", pady=(px(3), px(6)))
        self.bar_igpu = StatBar(body2, "核显 GPU", ACCENT, width=364)
        self.bar_igpu.pack(fill="x", pady=(0, px(6)))
        self.bar_ram = StatBar(body2, "内存", "#E0A32E", width=364)
        self.bar_ram.pack(fill="x", pady=(0, px(6)))
        self.bar_cpu = StatBar(body2, "CPU", "#38B26B", width=364)
        self.bar_cpu.pack(fill="x")

        c3 = Card(self.root, "显卡模式")
        c3.pack(fill="x", padx=pad, pady=(0, px(6)))
        self.mode_card = c3
        body3 = tk.Frame(c3, bg=CARD)
        body3.pack(fill="x", padx=px(10), pady=(px(6), px(10)))
        grid = tk.Frame(body3, bg=CARD)
        grid.pack(fill="x")
        bw, bh = 92, 46

        self.btn_eco = ModeButton(grid, "集显模式", ACCENT, lambda: self._do_eco(True), bw, bh)
        self.btn_eco.grid(row=0, column=0, padx=px(2), pady=px(2))
        self.btn_std = ModeButton(grid, "标准模式", GREEN, lambda: self._do_eco(False), bw, bh)
        self.btn_std.grid(row=0, column=1, padx=px(2), pady=px(2))
        self.btn_ult = ModeButton(grid, "独显直连", ORANGE, self._do_ultimate, bw, bh)
        self.btn_ult.grid(row=0, column=2, padx=px(2), pady=px(2))
        self.btn_auto = ModeButton(grid, "自动切换", ACCENT, self._do_auto, bw, bh)
        self.btn_auto.grid(row=0, column=3, padx=px(2), pady=px(2))
        for col in range(4):
            grid.grid_columnconfigure(col, weight=1, uniform="m")

        self.mode_buttons = [self.btn_eco, self.btn_std, self.btn_ult, self.btn_auto]

        row_btn = tk.Frame(c3, bg=CARD)
        row_btn.pack(fill="x", padx=px(10), pady=(0, px(10)))
        for text, cmd in (("标准混合", self._do_standard), ("设置", self._open_settings)):
            b = tk.Label(row_btn, text=text, bg=BTN, fg=DIM, font=font(8),
                         padx=px(14), pady=px(4), cursor="hand2",
                         highlightbackground=BORDER, highlightthickness=px(1))
            b.pack(side="left", padx=(0, px(6)))
            b.bind("<Button-1>", lambda _e, c=cmd: c())
            b.bind("<Enter>", lambda _e, x=b: x.configure(bg=BTN_HOVER))
            b.bind("<Leave>", lambda _e, x=b: x.configure(bg=BTN))

        c4 = Card(self.root)
        c4.pack(fill="both", expand=True, padx=pad, pady=(0, pad))
        self.log = tk.Text(c4, height=7, bg="#1C1C1C", fg="#B4B4B4", bd=0,
                           font=(FONT, -px(8 * FONT_BOOST)), wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True, padx=px(6), pady=(px(6), px(4)))

        row = tk.Frame(c4, bg=CARD)
        row.pack(fill="x", padx=px(6), pady=(0, px(6)))
        hint = tk.Label(row, text="", bg=CARD, fg=FAINT, font=font(7), anchor="w")
        hint.pack(side="left")
        self.hotkey_hint = hint

        btn_repair = tk.Label(row, text="修复显示设备", bg=BTN, fg=DIM, font=font(8),
                              padx=px(10), pady=px(3), cursor="hand2")
        btn_repair.pack(side="right")
        btn_repair.bind("<Button-1>", lambda _e: self._repair())

    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _log_from_thread(self, text: str) -> None:
        self._log_queue.append(text)

    def _tick(self) -> None:
        self._drain_hotkey()

        while self._log_queue:
            self._log(self._log_queue.pop(0))

        if not self._busy:
            s = self.monitor.read()
            dev = self.monitor.devices

            if s.mux == MUX_ULTIMATE and s.dgpu_works:
                mode, idx = "独显直连", 2
            elif s.mux == MUX_IGPU:
                mode, idx = "集显模式", 0
            elif dev.can_eco and not s.dgpu_works:
                mode, idx = "集显模式", 0
            else:
                mode, idx = "标准模式", 1

            self.mode_lbl.configure(text=mode)
            for i, b in enumerate(self.mode_buttons):
                b.set_selected(i == idx)
            for b in (self.btn_eco, self.btn_std, self.btn_auto):
                b.set_disabled(not dev.can_eco)
            self.btn_ult.set_disabled(not dev.can_mux)

            if not dev.acpi_ok:
                self.info_lbl.configure(
                    text="⚠ 无华硕固件通道，只能监视、不能切换", fg=RED)
            elif not dev.can_eco:
                self.info_lbl.configure(
                    text="⚠ 本机固件未提供独显供电开关，只能监视", fg=ORANGE)
            elif s.dgpu_works:
                mux_txt = f"    ·    MUX={s.mux}" if dev.can_mux else ""
                self.info_lbl.configure(text=f"● 独显已通电可用{mux_txt}", fg=GREEN)
            else:
                mux_txt = f"    ·    MUX={s.mux}" if dev.can_mux else ""
                self.info_lbl.configure(text=f"○ 独显已断电（省电）{mux_txt}", fg=ORANGE)

            if s.dgpu is None:
                self.bar_dgpu.set(0, "已断电")
            else:
                vram = ""
                if s.dgpu_vram_total:
                    vram = f"  {s.dgpu_vram/1024**3:.1f}/{s.dgpu_vram_total/1024**3:.1f}G"
                self.bar_dgpu.set(s.dgpu, f"{s.dgpu:.0f}%{vram}")

            self.bar_igpu.set(s.igpu, f"{s.igpu:.0f}%")

            ram_pct = s.ram_used * 100 / s.ram_total if s.ram_total else 0
            self.bar_ram.set(ram_pct, f"{s.ram_used/1024**3:.1f}/{s.ram_total/1024**3:.1f} GB")

            cpu_text = f"{s.cpu:.0f}%"
            if s.temp:
                cpu_text += f"    {s.temp}℃"
            self.bar_cpu.set(s.cpu, cpu_text)

            if not self.model_lbl.cget("text") or self.model_lbl.cget("text") == "读取中…":
                self.model_lbl.configure(text=f"{machine_model()}    ·    {dev.describe()}")

            if hasattr(self, "hotkey_hint"):
                if self.config.get("hotkey_enabled", True) and self.hotkey:
                    self.hotkey_hint.configure(text=f"按 {self.hotkey.spec.describe()} 呼出/收起")
                elif not self.config.get("hotkey_enabled", True):
                    self.hotkey_hint.configure(text="快捷键已关闭（设置里可开）")

        self.root.after(1000, self._tick)

    def _force_refresh(self) -> None:
        self.monitor._dgpu_checked_at = 0.0
        self._log("已强制刷新")

    def _run_async(self, fn: Callable[[], None]) -> None:
        def worker():
            try:
                fn()
            except Exception as exc:
                self._log_from_thread(f"失败: {exc}")
            finally:
                self._busy = False
        self._busy = True
        threading.Thread(target=worker, daemon=True).start()

    def _do_eco(self, off: bool) -> None:
        if self._busy:
            return
        label = "集显模式" if off else "标准模式"
        self._log(f"--- 切到 {label} ---")
        self._run_async(lambda: self.monitor.switch_eco(
            off, lambda t: self._log_from_thread(t)))

    def _do_ultimate(self) -> None:
        if self._busy:
            return
        from tkinter import messagebox

        if not messagebox.askyesno(
            "切到独显直连",
            "独显直连让独显直接驱动屏幕，性能最高。\n\n"
            "· 屏幕会黑屏数秒\n"
            "· **切换后需要重启电脑才生效**（这是硬件要求，G-Helper 也一样）\n"
            "· 之后整机由独显驱动，比较费电\n\n"
            "确定切换吗？"
        ):
            return

        self._log("--- 切到独显直连（切换后会提示重启）---")

        def job():
            if not self.monitor._dgpu_works:
                self.monitor.switch_eco(False, lambda t: self._log_from_thread(t))
            ok = self.monitor.switch_mux(MUX_ULTIMATE, lambda t: self._log_from_thread(t))
            set_gpu_preference(2)
            if not ok:
                self._log_from_thread("MUX 写入失败，已放弃")
                return
            self._log_from_thread("独显直连已写入，正在重启电脑（3 秒后）…")
            reboot_now(3)

        self._run_async(job)

    def _do_standard(self) -> None:
        if self._busy:
            return
        from tkinter import messagebox

        if not messagebox.askyesno(
            "切回标准混合",
            "从独显直连切回标准混合（核显输出 + 独显渲染）。\n\n"
            "· 需要重启电脑才完全生效\n\n确定继续吗？"
        ):
            return
        self._log("--- 切回标准混合 ---")

        def job():
            if not self.monitor._dgpu_works:
                self.monitor.switch_eco(False, lambda t: self._log_from_thread(t))
            ok = self.monitor.switch_mux(MUX_STANDARD, lambda t: self._log_from_thread(t))
            set_gpu_preference(2)
            if not ok:
                self._log_from_thread("MUX 写入失败")
                return
            self._log_from_thread("已切回标准混合，正在重启电脑（3 秒后）…")
            reboot_now(3)

        self._run_async(job)

    def _do_auto(self) -> None:
        if self._busy:
            return
        want_dgpu = on_ac_power()
        self.config["auto_mode"] = True
        save_config(self.config)
        self._log(
            f"--- 自动切换：当前{'插电' if want_dgpu else '用电池'}，"
            f"目标={'开启独显' if want_dgpu else '关闭独显'} ---"
        )
        self._run_async(lambda: self.monitor.switch_eco(
            not want_dgpu, lambda t: self._log_from_thread(t)))

    def _repair(self) -> None:
        self._log("--- 修复显示设备 ---")

        def job():
            try:
                r = run_quiet(["pnputil", "/enum-devices", "/class", "Display"], timeout=20)
                import re

                m = re.search(r"PCI\\VEN_10DE\S+", r.stdout or "")
                if not m:
                    self._log_from_thread("没找到 NVIDIA 显示设备")
                    return
                out = run_quiet(["pnputil", "/restart-device", m.group(0)], timeout=60)
                text = (out.stdout or "") + (out.stderr or "")
                ok = "success" in text.lower() or "成功" in text
                self._log_from_thread("设备已重启" if ok else f"重启返回: {text.strip()[:120]}")
            except Exception as exc:
                self._log_from_thread(f"修复失败: {exc}")

        self._run_async(job)

    def _toggle_top(self) -> None:
        self.root.attributes("-topmost", not self.root.attributes("-topmost"))

    def _on_close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
        self.monitor.stop()
        if self.hotkey:
            self.hotkey.stop()
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self) -> int:
        self._log(f"{APP_NAME} {APP_VERSION} 就绪（监视在后台线程，界面不卡）")
        if self.config.get("always_on_top", True):
            self._log("窗口已置顶；关闭窗口会收进后台，按快捷键再呼出")
        self.root.mainloop()
        self._on_close()
        return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    known = {"--version", "-V", "--check", "--selftest", "--report", "--help", "-h", "--console"}
    unknown = [a for a in argv if a.startswith("-") and a not in known]
    if unknown:
        print(f"未知参数: {' '.join(unknown)}（用 --help 看用法）", file=sys.stderr)
        return 2

    if "--version" in argv or "-V" in argv:
        print(f"{APP_NAME} {APP_VERSION}")
        return 0

    if "--check" in argv or "--selftest" in argv:
        from .selftest import print_report, run_self_test

        return print_report(run_self_test())

    if "--report" in argv:
        from .selftest import print_machine_report

        return print_machine_report()

    if "--help" in argv or "-h" in argv:
        print(f"""{APP_NAME} ({APP_NAME_CN}) {APP_VERSION} —— {APP_TAGLINE}

用法:
  python -m gpucore            启动面板（常驻后台，按热键呼出）
  python -m gpucore --check    自检：检查这台机器能不能用、缺什么
  python -m gpucore --report   机型报告：贴到 issue 里，用来补全其他华硕机型的设备号
  python -m gpucore --version  看版本
""")
        return 0

    if not sys.platform.startswith("win"):
        print("仅支持 Windows")
        return 1
    return App().run()


if __name__ == "__main__":
    sys.exit(main())
