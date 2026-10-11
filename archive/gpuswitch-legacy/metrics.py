"""指标采集：GPU 占用 / 显存、CPU、内存。

采集策略（按可用性依次降级）：
1. Windows 性能计数器（PDH）—— 任务管理器同源，无需管理员、无需第三方库
2. psutil —— 可选依赖，装了就用（CPU/内存更省事）
3. nvidia-smi —— NVIDIA 显卡的占用与显存
4. /proc、vm_stat 等系统文件 —— Linux / macOS 回退
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import log
from .adapter import GpuAdapter, TYPE_VIRTUAL, format_bytes
from .paths import is_linux, is_macos, is_windows

logger = log.get(__name__)

try:  # 可选依赖
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None


# ---------------------------------------------------------------------------
# 采样结果
# ---------------------------------------------------------------------------


@dataclass
class SystemMetrics:
    cpu_percent: float = 0.0
    ram_used: int = 0
    ram_total: int = 0
    swap_percent: float = 0.0
    cpu_freq_mhz: float = 0.0
    cpu_name: str = ""
    cpu_cores: int = 0
    timestamp: float = field(default_factory=time.time)

    @property
    def ram_percent(self) -> float:
        if self.ram_total <= 0:
            return 0.0
        return self.ram_used * 100.0 / self.ram_total

    @property
    def ram_label(self) -> str:
        return f"{format_bytes(self.ram_used)} / {format_bytes(self.ram_total)}"


@dataclass
class GpuSample:
    """单块显卡的一次采样。"""

    util_percent: float = 0.0           # 最忙引擎的占用（任务管理器口径）
    util_total_percent: float = 0.0     # 所有引擎累加
    vram_used: int = 0
    vram_total: int = 0
    per_engine: Dict[str, float] = field(default_factory=dict)

    @property
    def vram_percent(self) -> float:
        if self.vram_total <= 0:
            return 0.0
        return min(100.0, self.vram_used * 100.0 / self.vram_total)


# ---------------------------------------------------------------------------
# PDH（Windows 性能计数器）
# ---------------------------------------------------------------------------

PDH_FMT_DOUBLE = 0x00000200
PDH_FMT_NOCAP100 = 0x00008000
PDH_MORE_DATA = 0x800007D2
PDH_NO_DATA = 0x800007D5
ERROR_SUCCESS = 0

_GPU_ENGINE_PATH = r"\GPU Engine(*)\Utilization Percentage"
_GPU_MEM_PATH = r"\GPU Adapter Memory(*)\Dedicated Usage"
#: (通配路径, 类别) 列表，交给 PdhQuery.sync
_PDH_WILDCARDS: List[Tuple[str, str]] = [
    (_GPU_ENGINE_PATH, "engine"),
    (_GPU_MEM_PATH, "memory"),
]


class _PDH_COUNTER_PATH_ELEMENTS_W(ctypes.Structure):
    _fields_ = [
        ("szMachineName", wt.LPWSTR),
        ("szObjectName", wt.LPWSTR),
        ("szInstanceName", wt.LPWSTR),
        ("szParentInstance", wt.LPWSTR),
        ("dwInstanceIndex", wt.DWORD),
        ("szCounterName", wt.LPWSTR),
    ]


class _PDH_FMT_COUNTERVALUE(ctypes.Structure):
    class _Union(ctypes.Union):
        _fields_ = [
            ("longValue", ctypes.c_long),
            ("doubleValue", ctypes.c_double),
            ("largeValue", ctypes.c_longlong),
            ("AnsiStringValue", ctypes.c_char_p),
            ("WideStringValue", ctypes.c_wchar_p),
        ]

    _fields_ = [("CStatus", wt.DWORD), ("value", _Union)]


class _PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
    _fields_ = [("szName", wt.LPWSTR), ("FmtValue", _PDH_FMT_COUNTERVALUE)]


_LUID_RE = re.compile(
    r"luid_(0x[0-9a-fA-F]+)_(0x[0-9a-fA-F]+)_phys_(\d+)(?:_eng_(\d+)_engtype_(\w+))?"
)


def parse_luid(instance_name: str) -> Optional[int]:
    """从计数器实例名里取出 64 位 LUID。"""
    match = _LUID_RE.search(instance_name or "")
    if not match:
        return None
    high = int(match.group(1), 16)
    low = int(match.group(2), 16)
    return ((high & 0xFFFFFFFF) << 32) | (low & 0xFFFFFFFF)


def parse_engine_type(instance_name: str) -> str:
    match = _LUID_RE.search(instance_name or "")
    if match and match.group(5):
        return match.group(5).lower()
    return "unknown"


class PdhQuery:
    """一个 PDH 查询句柄，可反复 AddEnglishCounter / Collect。"""

    def __init__(self) -> None:
        self._pdh = ctypes.WinDLL("pdh")
        self._handle = wt.HANDLE()
        # 路径 -> (计数器句柄, 类别)；类别在注册时就确定，避免运行时按实例名猜
        self._counters: Dict[str, Tuple[ctypes.c_void_p, str]] = {}
        self._lock = threading.Lock()
        self._configure()

    def _configure(self) -> None:
        pdh = self._pdh
        pdh.PdhOpenQueryW.argtypes = [wt.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wt.HANDLE)]
        pdh.PdhOpenQueryW.restype = ctypes.c_long
        pdh.PdhAddEnglishCounterW.argtypes = [wt.HANDLE, wt.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
        pdh.PdhAddEnglishCounterW.restype = ctypes.c_long
        pdh.PdhRemoveCounter.argtypes = [ctypes.c_void_p]
        pdh.PdhRemoveCounter.restype = ctypes.c_long
        pdh.PdhCollectQueryData.argtypes = [wt.HANDLE]
        pdh.PdhCollectQueryData.restype = ctypes.c_long
        pdh.PdhGetFormattedCounterValue.argtypes = [
            ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.POINTER(_PDH_FMT_COUNTERVALUE)
        ]
        pdh.PdhGetFormattedCounterValue.restype = ctypes.c_long
        pdh.PdhExpandWildCardPathW.argtypes = [wt.LPCWSTR, wt.LPCWSTR, wt.LPWSTR, ctypes.POINTER(wt.DWORD), wt.DWORD]
        pdh.PdhExpandWildCardPathW.restype = ctypes.c_long

    @property
    def valid(self) -> bool:
        return bool(self._handle)

    def open(self) -> bool:
        status = self._pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._handle))
        if status != ERROR_SUCCESS:
            logger.warning("PdhOpenQuery 失败: 0x%08X", status & 0xFFFFFFFF)
            self._handle = wt.HANDLE()
            return False
        return True

    def expand(self, wildcard: str) -> List[str]:
        """把通配路径展开成具体计数器路径列表。"""
        size = wt.DWORD(0)
        status = self._pdh.PdhExpandWildCardPathW(None, wildcard, None, ctypes.byref(size), 0)
        if size.value == 0:
            return []
        buf = ctypes.create_unicode_buffer(size.value)
        status = self._pdh.PdhExpandWildCardPathW(None, wildcard, buf, ctypes.byref(size), 0)
        if status != ERROR_SUCCESS and status != PDH_MORE_DATA:
            logger.debug("PdhExpandWildCardPath(%s) = 0x%08X", wildcard, status & 0xFFFFFFFF)
            return []
        # 双 \0 结尾的多字符串
        raw = buf[: size.value]
        return [item for item in raw.split("\0") if item]

    def sync(self, wildcards: List[Tuple[str, str]]) -> int:
        """重建计数器集合。

        wildcards 为 [(通配路径, 类别)]，类别取 'engine' 或 'memory'。
        返回注册成功的数量。
        """
        with self._lock:
            wanted: Dict[str, str] = {}
            for wildcard, kind in wildcards:
                for path in self.expand(wildcard):
                    wanted[path] = kind

            # 移除已经消失的计数器（进程退出后实例会消失）
            for path in list(self._counters):
                if path not in wanted:
                    self._pdh.PdhRemoveCounter(self._counters.pop(path)[0])

            added = 0
            for path, kind in wanted.items():
                if path in self._counters:
                    added += 1
                    continue
                handle = ctypes.c_void_p()
                if self._pdh.PdhAddEnglishCounterW(self._handle, path, 0, ctypes.byref(handle)) == ERROR_SUCCESS:
                    self._counters[path] = (handle, kind)
                    added += 1
            return added

    def collect(self) -> List[Tuple[str, str, float]]:
        """采集并返回 [(实例名, 类别, 数值)]；首次调用通常无数据（需两次采样）。"""
        with self._lock:
            status = self._pdh.PdhCollectQueryData(self._handle)
            if status != ERROR_SUCCESS:
                logger.debug("PdhCollectQueryData = 0x%08X", status & 0xFFFFFFFF)
                return []
            results: List[Tuple[str, str, float]] = []
            for path, (handle, kind) in self._counters.items():
                value = _PDH_FMT_COUNTERVALUE()
                fmt_type = wt.DWORD(0)
                status = self._pdh.PdhGetFormattedCounterValue(
                    handle, PDH_FMT_DOUBLE | PDH_FMT_NOCAP100, ctypes.byref(fmt_type), ctypes.byref(value)
                )
                if status != ERROR_SUCCESS:
                    continue
                results.append((_instance_of(path), kind, float(value.value.doubleValue)))
            return results

    def close(self) -> None:
        with self._lock:
            if self._handle:
                try:
                    self._pdh.PdhCloseQuery(self._handle)
                except Exception:
                    pass
                self._handle = wt.HANDLE()


def _instance_of(path: str) -> str:
    """从 '\\\\Machine\\Object(Instance)\\Counter' 提取 Instance。"""
    try:
        after_object = path.split("\\", 3)[3] if path.count("\\") >= 3 else path
        if "(" in after_object and ")" in after_object:
            return after_object[after_object.index("(") + 1 : after_object.rindex(")")]
        return after_object.split("\\")[0]
    except Exception:
        return path


# ---------------------------------------------------------------------------
# Windows CPU / 内存（不依赖 psutil）
# ---------------------------------------------------------------------------


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wt.DWORD),
        ("dwMemoryLoad", wt.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wt.DWORD), ("dwHighDateTime", wt.DWORD)]

    @property
    def value(self) -> int:
        return (self.dwHighDateTime << 32) | self.dwLowDateTime


class Win32Sampler:
    """用 Win32 API 采集 CPU 与内存，避免依赖 psutil。"""

    def __init__(self) -> None:
        self.kernel32 = ctypes.WinDLL("kernel32")
        self._prev: Optional[Tuple[int, int, int]] = None
        self._cpu_name = ""

    def cpu_percent(self) -> float:
        idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
        if not self.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return 0.0
        cur = (idle.value, kernel.value, user.value)
        if self._prev is None:
            self._prev = cur
            return 0.0
        d_idle = cur[0] - self._prev[0]
        d_kernel = cur[1] - self._prev[1]
        d_user = cur[2] - self._prev[2]
        self._prev = cur
        total = d_kernel + d_user  # kernel 已含 idle
        if total <= 0:
            return 0.0
        busy = total - d_idle
        return max(0.0, min(100.0, busy * 100.0 / total))

    def memory(self) -> Tuple[int, int]:
        status = _MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not self.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return 0, 0
        total = int(status.ullTotalPhys)
        used = total - int(status.ullAvailPhys)
        return used, total

    def cpu_name(self) -> str:
        if self._cpu_name:
            return self._cpu_name
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as key:
                self._cpu_name = str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except Exception:
            self._cpu_name = os.environ.get("PROCESSOR_IDENTIFIER", "CPU")
        return self._cpu_name

    def cpu_cores(self) -> int:
        try:
            return int(os.cpu_count() or 0)
        except Exception:
            return 0


# ---------------------------------------------------------------------------
# 统一采集器
# ---------------------------------------------------------------------------


class MetricsCollector:
    """线程安全的指标采集器。

    start() 后由后台线程按间隔采样，snapshot() 取最近一次结果。
    """

    def __init__(self, interval_ms: int = 1000) -> None:
        self.interval = max(200, int(interval_ms)) / 1000.0
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        self._system = SystemMetrics()
        self._gpu: Dict[int, GpuSample] = {}
        self._nvidia: Dict[str, GpuSample] = {}
        self._last_gpu_sync = 0.0
        self._last_smi = 0.0

        self._pdh: Optional[PdhQuery] = None
        self._win32: Optional[Win32Sampler] = None
        self._pdh_ready = False
        self._pdh_fail_count = 0

        if is_windows():
            self._win32 = Win32Sampler()
            try:
                self._pdh = PdhQuery()
                self._pdh_ready = self._pdh.open()
                if self._pdh_ready:
                    # 首次采样建立基线：PDH 的速率型计数器需要两次采集
                    self._pdh.sync(_PDH_WILDCARDS)
                    self._pdh.collect()
                    logger.info("已启用 PDH 性能计数器采集")
            except Exception as exc:
                logger.info("PDH 不可用，回退其它采集方式: %s", exc)
                self._pdh = None
                self._pdh_ready = False

    # ---- 生命周期 ----

    @property
    def backend(self) -> str:
        parts = []
        if self._pdh_ready:
            parts.append("PDH")
        if psutil is not None:
            parts.append("psutil")
        if self._win32 is not None and not parts:
            parts.append("Win32")
        if not parts:
            parts.append("系统文件")
        return "+".join(parts)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        # 先同步采一轮，避免界面首帧全是 0
        self._sample()
        time.sleep(0.25)
        self._thread = threading.Thread(target=self._loop, name="metrics", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._pdh:
            self._pdh.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._sample()
            except Exception as exc:  # 采样异常不应终止线程
                logger.debug("采样异常: %s", exc)
            self._stop.wait(self.interval)

    # ---- 采样 ----

    def _sample(self) -> None:
        system = self._sample_system()
        gpu = self._sample_gpu()

        with self._lock:
            self._system = system
            self._gpu = gpu

    def _sample_system(self) -> SystemMetrics:
        metrics = SystemMetrics(timestamp=time.time())

        if psutil is not None:
            try:
                metrics.cpu_percent = float(psutil.cpu_percent(interval=None))
                mem = psutil.virtual_memory()
                metrics.ram_used = int(mem.used)
                metrics.ram_total = int(mem.total)
                metrics.cpu_cores = int(psutil.cpu_count(logical=True) or 0)
                try:
                    freq = psutil.cpu_freq()
                    metrics.cpu_freq_mhz = float(freq.current) if freq else 0.0
                except Exception:
                    pass
                try:
                    metrics.swap_percent = float(psutil.swap_memory().percent)
                except Exception:
                    pass
            except Exception as exc:
                logger.debug("psutil 采样失败: %s", exc)

        if self._win32 is not None:
            if metrics.cpu_percent <= 0.0:
                metrics.cpu_percent = self._win32.cpu_percent()
            if metrics.ram_total <= 0:
                metrics.ram_used, metrics.ram_total = self._win32.memory()
            metrics.cpu_name = metrics.cpu_name or self._win32.cpu_name()
            metrics.cpu_cores = metrics.cpu_cores or self._win32.cpu_cores()

        if is_linux() and metrics.ram_total <= 0:
            metrics.ram_used, metrics.ram_total = _linux_memory()
            metrics.cpu_percent = metrics.cpu_percent or _linux_cpu_percent()

        if is_macos() and metrics.ram_total <= 0:
            metrics.ram_used, metrics.ram_total = _macos_memory()

        return metrics

    def _sample_gpu(self) -> Dict[int, GpuSample]:
        samples: Dict[int, GpuSample] = {}
        now = time.monotonic()

        # --- PDH：按 LUID 聚合引擎占用，按 LUID 取显存 ---
        if self._pdh_ready and self._pdh is not None:
            try:
                # 进程会不断新建/退出，定期重建计数器集合
                if now - self._last_gpu_sync > 5.0:
                    self._pdh.sync(_PDH_WILDCARDS)
                    self._last_gpu_sync = now

                for instance, kind, value in self._pdh.collect():
                    luid = parse_luid(instance)
                    if luid is None:
                        continue
                    sample = samples.setdefault(luid, GpuSample())
                    if kind == "engine":
                        # 同一引擎类型会出现在多个进程上，需先累加；
                        # 再取"最忙的那类引擎"作为代表值，与任务管理器口径一致
                        engine = parse_engine_type(instance)
                        sample.per_engine[engine] = sample.per_engine.get(engine, 0.0) + value
                    elif kind == "memory":
                        sample.vram_used = max(sample.vram_used, int(value))

                for sample in samples.values():
                    sample.util_total_percent = sum(sample.per_engine.values())
                    sample.util_percent = (
                        max(sample.per_engine.values()) if sample.per_engine else 0.0
                    )
                self._pdh_fail_count = 0
            except Exception as exc:
                self._pdh_fail_count += 1
                logger.debug("PDH 采样失败(%d): %s", self._pdh_fail_count, exc)
                if self._pdh_fail_count > 5:
                    logger.warning("PDH 连续失败，停用性能计数器")
                    self._pdh_ready = False

        # --- nvidia-smi：补显存总量（PDH 拿不到总量），限频调用 ---
        if now - self._last_smi > 3.0:
            self._last_smi = now
            smi = _nvidia_smi_sample()
            if smi:
                self._nvidia = smi

        return samples

    # ---- 读取 ----

    def snapshot(self) -> Tuple[SystemMetrics, Dict[int, GpuSample]]:
        with self._lock:
            return self._system, dict(self._gpu)

    def nvidia_sample(self) -> Dict[str, GpuSample]:
        return dict(self._nvidia)

    def apply_to_adapters(self, adapters: List[GpuAdapter]) -> None:
        """把最新采样写回显卡对象，供界面直接读取。"""
        _system, samples = self.snapshot()
        nvidia = self.nvidia_sample()

        for adapter in adapters:
            if adapter.kind == TYPE_VIRTUAL:
                adapter.util_percent = 0.0
                adapter.vram_used = 0
                continue

            # 1) 按 LUID 精确匹配 PDH 采样
            sample = samples.get(adapter.luid) if adapter.luid else None
            if sample is None and adapter.luid:
                # LUID 高位在少数驱动上不一致，按低 32 位再试
                low = adapter.luid & 0xFFFFFFFF
                for luid, candidate in samples.items():
                    if (luid & 0xFFFFFFFF) == low:
                        sample = candidate
                        break

            if sample is not None:
                adapter.util_percent = round(sample.util_percent, 1)
                adapter.vram_used = sample.vram_used
                adapter.extra["engines"] = {
                    k: round(v, 1)
                    for k, v in sorted(sample.per_engine.items())
                    if v > 0
                }
                adapter.extra["source_util"] = "PDH"

            # 2) NVIDIA 卡用 nvidia-smi 的结果校准：
            #    显存总量（PDH 拿不到）一定取用；PDH 无数据时占用也回退到它
            if adapter.vendor_id == 0x10DE and nvidia:
                match = _match_nvidia(nvidia, adapter.name)
                if match is not None:
                    if match.vram_total:
                        adapter.dedicated_vram = match.vram_total
                    if sample is None:
                        adapter.util_percent = round(match.util_percent, 1)
                        if match.vram_used:
                            adapter.vram_used = match.vram_used
                        adapter.extra["source_util"] = "nvidia-smi"

    def system(self) -> SystemMetrics:
        with self._lock:
            return self._system


# ---------------------------------------------------------------------------
# nvidia-smi
# ---------------------------------------------------------------------------


def _match_nvidia(samples: Dict[str, GpuSample], adapter_name: str):
    """把 nvidia-smi 的结果匹配到某块卡：先按名字包含，再退化到唯一一块。"""
    key = (adapter_name or "").lower()
    for name, sample in samples.items():
        if name and (name in key or key in name):
            return sample
    # 名称差异过大时只在只有一块 NVIDIA 卡的情况下兜底
    if len(samples) == 1:
        return next(iter(samples.values()))
    return None


def _nvidia_smi_sample() -> Dict[str, GpuSample]:
    if not (is_windows() or is_linux()):
        return {}
    try:
        from .paths import run_capture

        out = run_capture(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            timeout=3,
        )
    except Exception:
        return {}
    if out.returncode != 0:
        return {}

    samples: Dict[str, GpuSample] = {}
    for line in out.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            samples[parts[0].lower()] = GpuSample(
                util_percent=float(parts[1]),
                util_total_percent=float(parts[1]),
                vram_used=int(float(parts[2]) * 1024 * 1024),
                vram_total=int(float(parts[3]) * 1024 * 1024),
            )
        except ValueError:
            continue
    return samples


# ---------------------------------------------------------------------------
# Linux / macOS 回退
# ---------------------------------------------------------------------------


def _linux_memory() -> Tuple[int, int]:
    try:
        info: Dict[str, int] = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                value = rest.strip().split()
                if value:
                    info[key.strip()] = int(value[0]) * 1024
        total = info.get("MemTotal", 0)
        available = info.get("MemAvailable", info.get("MemFree", 0))
        return total - available, total
    except Exception:
        return 0, 0


_linux_cpu_prev: Optional[Tuple[int, int]] = None


def _linux_cpu_percent() -> float:
    global _linux_cpu_prev
    try:
        with open("/proc/stat", "r", encoding="utf-8") as handle:
            first = handle.readline().split()
        values = [int(v) for v in first[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        if _linux_cpu_prev is None:
            _linux_cpu_prev = (idle, total)
            return 0.0
        d_idle = idle - _linux_cpu_prev[0]
        d_total = total - _linux_cpu_prev[1]
        _linux_cpu_prev = (idle, total)
        if d_total <= 0:
            return 0.0
        return max(0.0, min(100.0, (d_total - d_idle) * 100.0 / d_total))
    except Exception:
        return 0.0


def _macos_memory() -> Tuple[int, int]:
    try:
        from .paths import run_capture

        total = int(run_capture(["sysctl", "-n", "hw.memsize"], timeout=3).stdout.strip())
        vm = run_capture(["vm_stat"], timeout=3).stdout
        page_size = 4096
        match = re.search(r"page size of (\d+) bytes", vm)
        if match:
            page_size = int(match.group(1))
        pages = {}
        for line in vm.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                digits = re.sub(r"[^0-9]", "", value)
                if digits:
                    pages[key.strip()] = int(digits)
        active = pages.get("Pages active", 0)
        wired = pages.get("Pages wired down", 0)
        compressed = pages.get("Pages occupied by compressor", 0)
        used = (active + wired + compressed) * page_size
        return used, total
    except Exception:
        return 0, 0
