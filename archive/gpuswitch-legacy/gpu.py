
from __future__ import annotations

import ctypes
import re
import subprocess
import sys
from typing import Dict, List, Optional

from . import log
from .adapter import (
    PREF_HIGH_PERFORMANCE,
    PREF_POWER_SAVING,
    TYPE_DISCRETE,
    TYPE_INTEGRATED,
    TYPE_UNKNOWN,
    TYPE_VIRTUAL,
    GpuAdapter,
    classify,
    sort_adapters,
)
from .paths import is_linux, is_macos, is_windows

logger = log.get(__name__)


DXGI_ERROR_NOT_FOUND = 0x887A0002
DXGI_ADAPTER_FLAG_SOFTWARE = 0x2
DXGI_ADAPTER_FLAG_REMOTE = 0x4

USER_GPU_PREFS_KEY = r"Software\Microsoft\DirectX\UserGpuPreferences"


def _guid(text: str):
    import uuid

    u = uuid.UUID(text.strip("{}"))
    return (ctypes.c_byte * 16).from_buffer_copy(u.bytes_le)

class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]


class _DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [
        ("Description", ctypes.c_wchar * 128),
        ("VendorId", ctypes.c_uint32),
        ("DeviceId", ctypes.c_uint32),
        ("SubSysId", ctypes.c_uint32),
        ("Revision", ctypes.c_uint32),
        ("DedicatedVideoMemory", ctypes.c_size_t),
        ("DedicatedSystemMemory", ctypes.c_size_t),
        ("SharedSystemMemory", ctypes.c_size_t),
        ("AdapterLuid", _LUID),
        ("Flags", ctypes.c_uint32),
    ]


def _vtbl_call(ptr, index: int, restype, *argtypes):
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(vtable[index])


def _release(ptr) -> None:
    if not ptr:
        return
    try:
        _vtbl_call(ptr, 2, ctypes.c_ulong)(ptr)
    except Exception:
        pass


def enumerate_dxgi() -> List[GpuAdapter]:
    if not is_windows():
        return []

    adapters: List[GpuAdapter] = []
    factory = ctypes.c_void_p()
    try:
        dxgi = ctypes.WinDLL("dxgi")
    except OSError as exc:
        logger.debug("dxgi.dll 加载失败: %s", exc)
        return []

    try:
        create = dxgi.CreateDXGIFactory1
        create.restype = ctypes.c_long
        create.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        iid = _guid("{770AAE78-F26F-4DBA-A829-253C83D1B387}")
        hr = create(ctypes.byref(iid), ctypes.byref(factory))
        if hr < 0 or not factory:
            logger.debug("CreateDXGIFactory1 失败 hr=0x%08X", hr & 0xFFFFFFFF)
            return []
    except Exception as exc:
        logger.debug("CreateDXGIFactory1 异常: %s", exc)
        return []

    try:
        enum_adapters1 = _vtbl_call(
            factory, 12, ctypes.c_long, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)
        )
        index = 0
        while True:
            adapter = ctypes.c_void_p()
            hr = enum_adapters1(factory, index, ctypes.byref(adapter))
            if hr < 0 or not adapter:
                break
            try:
                desc = _DXGI_ADAPTER_DESC1()
                get_desc1 = _vtbl_call(
                    adapter, 10, ctypes.c_long, ctypes.POINTER(_DXGI_ADAPTER_DESC1)
                )
                if get_desc1(adapter, ctypes.byref(desc)) < 0:
                    index += 1
                    continue

                name = (desc.Description or "").strip()
                vendor_id = int(desc.VendorId)
                dedicated = int(desc.DedicatedVideoMemory)
                luid = (int(desc.AdapterLuid.HighPart & 0xFFFFFFFF) << 32) | int(
                    desc.AdapterLuid.LowPart
                )
                kind = classify(vendor_id, name, dedicated)
                if desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE:
                    kind = TYPE_VIRTUAL
                if desc.Flags & DXGI_ADAPTER_FLAG_REMOTE:
                    kind = TYPE_VIRTUAL

                adapters.append(
                    GpuAdapter(
                        name=name,
                        vendor_id=vendor_id,
                        device_id=int(desc.DeviceId),
                        luid=luid,
                        kind=kind,
                        dedicated_vram=dedicated,
                        shared_memory=int(desc.SharedSystemMemory),
                        source="dxgi",
                        extra={
                            "subsys_id": int(desc.SubSysId),
                            "revision": int(desc.Revision),
                            "flags": int(desc.Flags),
                            "dedicated_system_memory": int(desc.DedicatedSystemMemory),
                        },
                    )
                )
            finally:
                _release(adapter)
            index += 1
    except Exception as exc:
        logger.debug("DXGI 枚举异常: %s", exc)
    finally:
        _release(factory)

    return adapters

_VIDEO_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"


def enumerate_registry() -> List[GpuAdapter]:
    if not is_windows():
        return []
    import winreg

    adapters: List[GpuAdapter] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _VIDEO_CLASS) as root:
            index = 0
            while True:
                try:
                    sub_name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                if not re.fullmatch(r"\d{4}", sub_name):
                    continue
                try:
                    with winreg.OpenKey(root, sub_name) as sub:
                        def val(name, default=None):
                            try:
                                return winreg.QueryValueEx(sub, name)[0]
                            except OSError:
                                return default

                        desc = val("DriverDesc") or val("DeviceDesc") or ""
                        if not desc:
                            continue
                        vendor = val("VendorId", 0xFFFF)
                        device = val("DeviceId", 0xFFFF)
                        adapter = GpuAdapter(
                            name=str(desc).strip(),
                            vendor_id=int(vendor) & 0xFFFF,
                            device_id=int(device) & 0xFFFF,
                            kind=TYPE_UNKNOWN,
                            source="registry",
                            extra={"inf": val("InfSection"), "provider": val("ProviderName")},
                        )
                        adapter.dedicated_vram = int(val("HardwareInformation.qwMemorySize", 0) or 0)
                        adapter.kind = classify(
                            adapter.vendor_id, adapter.name, adapter.dedicated_vram
                        )
                        adapters.append(adapter)
                except OSError:
                    continue
    except OSError as exc:
        logger.debug("注册表枚举失败: %s", exc)
    return adapters

def display_adapter_names() -> List[str]:
    names: List[str] = []
    if is_windows():
        try:
            user32 = ctypes.WinDLL("user32")
            gdi32 = ctypes.WinDLL("gdi32")

            class _DISPLAY_DEVICEW(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("DeviceName", ctypes.c_wchar * 32),
                    ("DeviceString", ctypes.c_wchar * 128),
                    ("StateFlags", ctypes.c_ulong),
                    ("DeviceID", ctypes.c_wchar * 128),
                    ("DeviceKey", ctypes.c_wchar * 128),
                ]

            DD_DEVICE_NAME = 0x1
            index = 0
            while True:
                dev = _DISPLAY_DEVICEW()
                dev.cb = ctypes.sizeof(_DISPLAY_DEVICEW)
                if not user32.EnumDisplayDevicesW(None, index, ctypes.byref(dev), DD_DEVICE_NAME):
                    break
                index += 1
                monitor = _DISPLAY_DEVICEW()
                monitor.cb = ctypes.sizeof(_DISPLAY_DEVICEW)
                attached = False
                m_index = 0
                while user32.EnumDisplayDevicesW(
                    dev.DeviceName, m_index, ctypes.byref(monitor), 0
                ):
                    m_index += 1
                    if monitor.StateFlags & 0x1:
                        attached = True
                        break
                if attached and dev.DeviceString:
                    names.append(dev.DeviceString.strip())
            gdi32
        except Exception as exc:
            logger.debug("EnumDisplayDevices 失败: %s", exc)
    return names


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def mark_display_adapters(adapters: List[GpuAdapter]) -> None:
    names = display_adapter_names()
    if not names:
        return
    normalized = {_normalize(n) for n in names}
    for adapter in adapters:
        key = _normalize(adapter.name)
        if not key:
            continue
        if key in normalized:
            adapter.drives_display = True
            continue
        for candidate in normalized:
            if len(key) >= 8 and (key in candidate or candidate in key):
                adapter.drives_display = True
                break

def nvidia_smi_adapters() -> List[Dict[str, object]]:
    if not is_windows() and not is_linux():
        return []
    try:
        from .paths import run_capture

        out = run_capture(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.total,memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            timeout=4,
        )
    except Exception as exc:
        logger.debug("nvidia-smi 不可用: %s", exc)
        return []
    if out.returncode != 0:
        return []

    items: List[Dict[str, object]] = []
    for line in out.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            items.append(
                {
                    "index": int(parts[0]),
                    "name": parts[1],
                    "memory_total_mib": float(parts[2]),
                    "memory_used_mib": float(parts[3]),
                    "util_percent": float(parts[4]),
                }
            )
        except ValueError:
            continue
    return items

def _enumerate_linux() -> List[GpuAdapter]:
    adapters: List[GpuAdapter] = []
    try:
        from .paths import run_capture

        out = run_capture(["lspci", "-nn"], timeout=5)
    except Exception as exc:
        logger.debug("lspci 不可用: %s", exc)
        return adapters

    for line in out.stdout.splitlines():
        low = line.lower()
        if not any(key in low for key in ("vga compatible controller", "3d controller", "display controller")):
            continue
        match = re.search(r"\[([0-9a-f]{4}):([0-9a-f]{4})\]", line, re.I)
        vendor_id = int(match.group(1), 16) if match else 0
        device_id = int(match.group(2), 16) if match else 0
        name = line.split(":", 2)[-1].strip()
        name = re.sub(r"\s*\[[0-9a-f]{4}:[0-9a-f]{4}\].*$", "", name, flags=re.I)
        adapters.append(
            GpuAdapter(
                name=name,
                vendor_id=vendor_id,
                device_id=device_id,
                kind=classify(vendor_id, name, 0),
                source="lspci",
            )
        )
    return adapters


def _enumerate_macos() -> List[GpuAdapter]:
    adapters: List[GpuAdapter] = []
    try:
        from .paths import run_capture

        out = run_capture(
            ["system_profiler", "SPDisplaysDataType", "-json"], timeout=12
        )
        import json

        data = json.loads(out.stdout or "{}")
        for item in data.get("SPDisplaysDataType", []):
            name = item.get("sppci_model") or item.get("_name") or "Unknown GPU"
            vendor = (item.get("spdisplays_vendor") or "").lower()
            vendor_id = 0x8086 if "intel" in vendor else 0x1002 if "amd" in vendor else 0
            adapters.append(
                GpuAdapter(
                    name=name,
                    vendor_id=vendor_id,
                    kind=classify(vendor_id, name, 0),
                    source="system_profiler",
                    drives_display=bool(item.get("spdisplays_ndrvs")),
                )
            )
    except Exception as exc:
        logger.debug("system_profiler 失败: %s", exc)
    return adapters


def _read_user_preferences() -> Dict[str, str]:
    if not is_windows():
        return {}
    import winreg

    result: Dict[str, str] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, USER_GPU_PREFS_KEY) as key:
            index = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                if isinstance(value, str):
                    result[name] = value
    except OSError:
        pass
    return result


def parse_preference(value: str) -> Optional[int]:
    if not value:
        return None
    match = re.search(r"GpuPreference\s*=\s*(\d+)", value, re.I)
    if not match:
        return None
    return int(match.group(1))


def dedupe_adapters(adapters: List[GpuAdapter]) -> List[GpuAdapter]:
    groups: Dict[tuple, List[GpuAdapter]] = {}
    order: List[tuple] = []
    passthrough: List[GpuAdapter] = []

    for adapter in adapters:
        if not adapter.vendor_id or not adapter.device_id:
            passthrough.append(adapter)
            continue
        key = (adapter.vendor_id, adapter.device_id)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(adapter)

    merged: List[GpuAdapter] = []
    for key in order:
        items = groups[key]
        if len(items) == 1:
            merged.append(items[0])
            continue
        items.sort(
            key=lambda a: (
                0 if a.drives_display else 1,
                -a.dedicated_vram,
                -a.util_percent,
                len(a.name),
            )
        )
        keep = items[0]
        keep.extra["duplicate_luids"] = [f"{a.luid:#x}" for a in items[1:]]
        keep.extra["duplicate_count"] = len(items)
        merged.append(keep)

    merged.extend(passthrough)
    return merged


def enumerate_adapters(use_cache: bool = True, cache_ms: int = 5000) -> List[GpuAdapter]:
    global _cache, _cache_time
    import time

    now = time.monotonic() * 1000
    if use_cache and _cache is not None and (now - _cache_time) < cache_ms:
        return list(_cache)

    adapters: List[GpuAdapter] = []
    if is_windows():
        adapters = enumerate_dxgi()
        if not adapters:
            adapters = enumerate_registry()
        if adapters:
            reg = {a.key: a for a in enumerate_registry()}
            for adapter in adapters:
                fallback = reg.get(adapter.key)
                if fallback and adapter.dedicated_vram <= 0 and fallback.dedicated_vram > 0:
                    adapter.dedicated_vram = fallback.dedicated_vram
        mark_display_adapters(adapters)
        for smi in nvidia_smi_adapters():
            for adapter in adapters:
                if adapter.vendor_id == 0x10DE and _normalize(str(smi["name"])) in _normalize(adapter.name) or (
                    adapter.vendor_id == 0x10DE and _normalize(adapter.name) in _normalize(str(smi["name"]))
                ):
                    adapter.extra["nvidia_smi_index"] = smi["index"]
                    if adapter.dedicated_vram <= 0 and smi["memory_total_mib"]:
                        adapter.dedicated_vram = int(float(smi["memory_total_mib"]) * 1024 * 1024)
                    break
    elif is_linux():
        adapters = _enumerate_linux()
    elif is_macos():
        adapters = _enumerate_macos()

    if not adapters:
        logger.warning("未枚举到任何显卡（平台 %s）", sys.platform)

    if adapters:
        for adapter in adapters:
            adapter.preference = (
                PREF_POWER_SAVING if adapter.kind == TYPE_INTEGRATED
                else PREF_HIGH_PERFORMANCE if adapter.kind == TYPE_DISCRETE
                else None
            )

    adapters = sort_adapters(adapters)
    adapters = dedupe_adapters(adapters)
    _cache = list(adapters)
    _cache_time = now
    return adapters


_cache: Optional[List[GpuAdapter]] = None
_cache_time: float = 0.0


def invalidate_cache() -> None:
    global _cache, _cache_time
    _cache = None
    _cache_time = 0.0


def describe_renderer(adapters: List[GpuAdapter]) -> Optional[GpuAdapter]:
    real = [a for a in adapters if a.kind != TYPE_VIRTUAL]
    if not real:
        return None

    busy = [a for a in real if a.util_percent >= 1.0]
    if busy:
        return max(busy, key=lambda a: a.util_percent)

    displaying = [a for a in real if a.drives_display]
    discrete_display = [a for a in displaying if a.kind == TYPE_DISCRETE]
    if discrete_display:
        return discrete_display[0]
    with_vram = [a for a in real if a.dedicated_vram >= 512 * 1024 * 1024]
    if with_vram:
        return with_vram[0]
    return real[0]


def describe_current(adapters: List[GpuAdapter]) -> Optional[GpuAdapter]:
    if not adapters:
        return None

    displaying = [a for a in adapters if a.drives_display and a.kind != TYPE_VIRTUAL]
    if displaying:
        return max(displaying, key=lambda a: a.util_percent)

    real = [a for a in adapters if a.kind != TYPE_VIRTUAL]
    pool = real or adapters
    busy = [a for a in pool if a.util_percent > 0.5]
    if busy:
        return max(busy, key=lambda a: a.util_percent)
    return max(pool, key=lambda a: a.dedicated_vram)
