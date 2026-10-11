"""首选显卡切换。

各平台能做到的"切换"并不相同，本模块把它们统一成同一套接口：

* Windows：写入 GPU 首选项（Windows 图形设置同源）。
      - 全局默认：HKCU\\...\\UserGpuPreferences 下名为 "GpuPreference" 的值，
        需要管理员权限（提权后自动写入）。
      - 指定程序：同名键下以 exe 全路径为名，无需管理员。
  注意：Windows 的显卡归属由 GPU 驱动在**程序创建 D3D 设备时**决定，
  已运行的程序不会被改变，需要重启该程序才生效——这是系统限制，
  包括"设置 > 系统 > 屏幕 > 显示卡"也是同样行为。

* Linux：优先 nvidia-prime / prime-select，其次 DRI_PRIME 环境变量。
* macOS：受系统限制，只能打开"节能"设置面板由用户手动切换。
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import log
from .adapter import (
    TYPE_DISCRETE,
    TYPE_INTEGRATED,
    GpuAdapter,
    PREF_HIGH_PERFORMANCE,
    PREF_POWER_SAVING,
)
from .paths import is_linux, is_macos, is_windows

logger = log.get(__name__)

USER_PREFS_SUBKEY = r"Software\Microsoft\DirectX\UserGpuPreferences"
GLOBAL_VALUE_NAME = "GpuPreference"


# ---------------------------------------------------------------------------
# 结果与权限
# ---------------------------------------------------------------------------


@dataclass
class SwitchResult:
    ok: bool
    message: str
    detail: str = ""
    needs_restart: bool = True
    needs_elevation: bool = False
    scope: str = ""

    def __bool__(self) -> bool:
        return self.ok


def is_elevated() -> bool:
    """当前进程是否以管理员身份运行。"""
    if not is_windows():
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_elevated(argv: Optional[Sequence[str]] = None) -> bool:
    """以管理员身份重新启动自身（弹一次 UAC）。"""
    if not is_windows():
        return False
    try:
        params = " ".join(f'"{a}"' for a in (argv or sys.argv))
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, str(Path.cwd()), 1
        )
        return int(result) > 32
    except Exception as exc:
        logger.warning("提权失败: %s", exc)
        return False


# ---------------------------------------------------------------------------
# 后端基类
# ---------------------------------------------------------------------------


class SwitchBackend:
    name = "none"
    description = ""

    def capabilities(self) -> Dict[str, object]:
        return {}

    #: 该后端是否支持"全局默认显卡"这一档
    supports_global: bool = False
    #: 该后端是否支持"指定程序"
    supports_per_app: bool = False

    def preference_value(self, adapter: GpuAdapter) -> int:
        if adapter.kind == TYPE_INTEGRATED:
            return PREF_POWER_SAVING
        if adapter.kind == TYPE_DISCRETE:
            return PREF_HIGH_PERFORMANCE
        # 未知类型：有独立显存的按高性能处理
        return PREF_HIGH_PERFORMANCE if adapter.dedicated_vram > 512 * 1024 * 1024 else PREF_POWER_SAVING

    def current_global(self) -> Optional[int]:
        return None

    def set_global(self, adapter: GpuAdapter, elevate: bool = False) -> SwitchResult:
        raise NotImplementedError

    def set_for_app(self, adapter: GpuAdapter, exe_path: str) -> SwitchResult:
        raise NotImplementedError

    def app_preference(self, exe_path: str) -> Optional[int]:
        return None

    def clear_global(self) -> SwitchResult:
        return SwitchResult(False, "当前后端不支持清除全局偏好")


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


class WindowsBackend(SwitchBackend):
    name = "windows-gpu-preference"
    description = "写入 Windows 图形首选项（与「设置 > 系统 > 屏幕 > 显示卡」同源）"
    supports_global = True
    supports_per_app = True

    def __init__(self) -> None:
        self._dxgi = None
        try:
            self._dxgi = ctypes.WinDLL("dxgi")
        except OSError:
            self._dxgi = None
        self._set_default = None
        if self._dxgi is not None:
            try:
                self._set_default = self._dxgi.SetProcessDefaultGraphicsAdapter
            except AttributeError:
                self._set_default = None

    def capabilities(self) -> Dict[str, object]:
        return {
            "全局默认显卡": "支持（需要管理员权限，会弹一次 UAC）",
            "指定程序显卡": "支持（无需管理员）",
            "已运行程序热切换": "不支持（Windows 限制，需重启该程序）",
            "DXGI 创建期指定": "可用" if self._set_default else "不可用",
            "当前是否管理员": "是" if is_elevated() else "否",
        }

    # ---- 注册表读写 ----

    def _open_key(self, write: bool):
        import winreg

        access = winreg.KEY_READ | (winreg.KEY_WRITE if write else 0)
        try:
            return winreg.OpenKey(winreg.HKEY_CURRENT_USER, USER_PREFS_SUBKEY, 0, access)
        except FileNotFoundError:
            if not write:
                return None
            # 键不存在时逐级创建
            parent = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, r"Software\Microsoft\DirectX", 0, winreg.KEY_WRITE
            )
            try:
                return winreg.CreateKeyEx(parent, "UserGpuPreferences", 0, access)
            finally:
                parent.Close()

    def read_all(self) -> Dict[str, str]:
        import winreg

        result: Dict[str, str] = {}
        key = self._open_key(write=False)
        if key is None:
            return result
        try:
            index = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                if isinstance(value, str):
                    result[name] = value
        finally:
            key.Close()
        return result

    def build_value(self, adapter: GpuAdapter, include_device: bool = True) -> str:
        """构造 GpuPreference 字符串。

        带厂商/设备号时只对那块卡生效，这也是 Windows 自己写入的格式。
        """
        pref = self.preference_value(adapter)
        if include_device and adapter.vendor_id and adapter.device_id:
            return f"GpuPreference={pref};{adapter.vendor_id:04X}:{adapter.device_id:04X};"
        return f"GpuPreference={pref};"

    def current_global(self) -> Optional[int]:
        raw = self.read_all().get(GLOBAL_VALUE_NAME)
        if not raw:
            return None
        match = re.search(r"GpuPreference\s*=\s*(\d+)", raw)
        return int(match.group(1)) if match else None

    def app_preference(self, exe_path: str) -> Optional[int]:
        raw = self.read_all().get(str(Path(exe_path)))
        if not raw:
            return None
        match = re.search(r"GpuPreference\s*=\s*(\d+)", raw)
        return int(match.group(1)) if match else None

    def _write_values(self, values: Dict[str, str]) -> Tuple[bool, str]:
        import winreg

        try:
            key = self._open_key(write=True)
            if key is None:
                return False, "无法打开注册表键（可能被安全软件拦截）"
            try:
                for name, value in values.items():
                    winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
            finally:
                key.Close()
            return True, ""
        except PermissionError as exc:
            return False, f"注册表写入被拒绝：{exc}（可能需要管理员权限）"
        except OSError as exc:
            return False, f"注册表写入失败：{exc}"

    # ---- 对外动作 ----

    def set_global(self, adapter: GpuAdapter, elevate: bool = False) -> SwitchResult:
        pref = self.preference_value(adapter)
        current = self.current_global()
        if current == pref:
            label = "集显（省电）" if pref == PREF_POWER_SAVING else "独显（高性能）"
            return SwitchResult(
                True,
                f"全局默认已经是「{label}」，无需切换",
                detail=f"{GLOBAL_VALUE_NAME} 当前值 = {pref}",
                needs_restart=False,
                scope="global",
            )

        ok, error = self._write_values({GLOBAL_VALUE_NAME: self.build_value(adapter)})
        if not ok:
            if elevate and not is_elevated():
                # 当前不是管理员，尝试提权重启
                if relaunch_elevated():
                    return SwitchResult(
                        True,
                        "已请求管理员权限，请在 UAC 弹窗中确认后重新操作",
                        detail=error,
                        needs_restart=True,
                        needs_elevation=True,
                        scope="global",
                    )
            return SwitchResult(
                False,
                "写入全局显卡首选项失败",
                detail=error,
                needs_elevation=not is_elevated(),
                scope="global",
            )

        # 同时给已知的"正在用 GPU"的程序写一份，让影响更明显
        applied = self._apply_to_current_gpu_apps(adapter)
        label = "集显（省电）" if pref == PREF_POWER_SAVING else "独显（高性能）"
        detail = f"已写入 {self.build_value(adapter)}"
        if applied:
            detail += f"；并同步了 {applied} 个正在使用 GPU 的程序"
        return SwitchResult(
            True,
            f"全局默认已切换为「{label}」",
            detail=detail,
            needs_restart=True,
            scope="global",
        )

    def set_for_app(self, adapter: GpuAdapter, exe_path: str) -> SwitchResult:
        target = str(Path(exe_path))
        if not target.lower().endswith(".exe"):
            return SwitchResult(False, f"{target} 不是可执行文件", scope="app")
        pref = self.preference_value(adapter)
        if self.app_preference(target) == pref:
            label = "集显" if pref == PREF_POWER_SAVING else "独显"
            return SwitchResult(
                True, f"「{Path(target).name}」已经设定为{label}", needs_restart=False, scope="app"
            )
        ok, error = self._write_values({target: self.build_value(adapter)})
        if not ok:
            return SwitchResult(False, "写入程序显卡首选项失败", detail=error, scope="app")
        label = "集显（省电）" if pref == PREF_POWER_SAVING else "独显（高性能）"
        return SwitchResult(
            True,
            f"「{Path(target).name}」已设定为{label}",
            detail="下次启动该程序时生效",
            needs_restart=True,
            scope="app",
        )

    def clear_global(self) -> SwitchResult:
        import winreg

        try:
            key = self._open_key(write=True)
            if key is None:
                return SwitchResult(False, "注册表键不存在")
            try:
                winreg.DeleteValue(key, GLOBAL_VALUE_NAME)
            except FileNotFoundError:
                return SwitchResult(True, "全局偏好本来就是空的", needs_restart=False)
            finally:
                key.Close()
            return SwitchResult(True, "已清除全局显卡偏好，恢复由 Windows 自动决定")
        except OSError as exc:
            return SwitchResult(False, "清除失败", detail=str(exc))

    # ---- 辅助 ----

    def _apply_to_current_gpu_apps(self, adapter: GpuAdapter) -> int:
        """给当前正在使用 GPU 的程序写上同样的偏好，扩大生效范围。"""
        pids = _gpu_pids()
        if not pids:
            return 0
        values: Dict[str, str] = {}
        for pid in pids:
            exe = _pid_exe(pid)
            if not exe:
                continue
            # 自己不写，避免把本程序也限制住
            if exe.lower() == sys.executable.lower():
                continue
            values[exe] = self.build_value(adapter)
        if not values:
            return 0
        ok, _ = self._write_values(values)
        return len(values) if ok else 0


def _gpu_pids() -> List[int]:
    """从 PDH 计数器里取当前有 GPU 活动的进程号。"""
    try:
        from .metrics import PdhQuery, _GPU_ENGINE_PATH, _instance_of

        query = PdhQuery()
        if not query.open():
            return []
        try:
            query.sync([(_GPU_ENGINE_PATH, "engine")])
            query.collect()
            pids: List[int] = []
            for instance, _kind, value in query.collect():
                if value <= 0:
                    continue
                match = re.match(r"pid_(\d+)_", _instance_of(instance))
                if match and match.group(1) not in pids:
                    pids.append(match.group(1))
            return pids
        finally:
            query.close()
    except Exception as exc:
        logger.debug("获取 GPU 进程列表失败: %s", exc)
        return []


def _pid_exe(pid: str) -> Optional[str]:
    """取进程的可执行文件全路径。"""
    import ctypes.wintypes as wt

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        size = wt.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value or None
        return None
    finally:
        kernel32.CloseHandle(handle)


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------


class LinuxBackend(SwitchBackend):
    name = "linux-prime"
    description = "使用 nvidia-prime / PRIME 渲染分流"
    supports_global = True
    supports_per_app = True

    def capabilities(self) -> Dict[str, object]:
        caps: Dict[str, object] = {}
        caps["prime-select"] = shutil.which("prime-select") or "未安装"
        caps["nvidia-settings"] = shutil.which("nvidia-settings") or "未安装"
        caps["DRI_PRIME"] = "可用"
        caps["当前用户"] = "root" if is_elevated() else "普通用户（全局切换需要 sudo）"
        return caps

    def current_global(self) -> Optional[int]:
        try:
            from .paths import run_capture

            out = run_capture(["prime-select", "query"], timeout=5).stdout.lower()
        except Exception:
            return None
        if "nvidia" in out:
            return PREF_HIGH_PERFORMANCE
        if "intel" in out or "amd" in out or "on-demand" in out:
            return PREF_POWER_SAVING
        return None

    def set_global(self, adapter: GpuAdapter, elevate: bool = False) -> SwitchResult:
        target = "nvidia" if adapter.kind == TYPE_DISCRETE else "intel"
        if shutil.which("prime-select") is None:
            return SwitchResult(
                False,
                "未找到 prime-select",
                detail="请先安装 nvidia-prime，或用 DRI_PRIME 按程序切换",
            )
        cmd = ["prime-select", target]
        if os.geteuid() != 0:
            if shutil.which("pkexec") is None:
                return SwitchResult(
                    False, "需要 root 权限", detail="请手动执行: sudo " + " ".join(cmd),
                    needs_elevation=True,
                )
            cmd = ["pkexec"] + cmd
        try:
            from .paths import run_capture

            out = run_capture(cmd, timeout=60)
        except Exception as exc:
            return SwitchResult(False, "切换失败", detail=str(exc), needs_elevation=True)
        if out.returncode != 0:
            return SwitchResult(False, "切换失败", detail=(out.stderr or out.stdout).strip())
        return SwitchResult(
            True,
            f"已切换到 {target}",
            detail="需要重新登录或重启 X 会话才会生效",
            needs_restart=True,
        )

    def set_for_app(self, adapter: GpuAdapter, exe_path: str) -> SwitchResult:
        if adapter.kind == TYPE_DISCRETE:
            return SwitchResult(
                True,
                "按程序使用独显：启动时加环境变量 DRI_PRIME=1",
                detail=f"DRI_PRIME=1 {exe_path}",
                needs_restart=False,
                scope="app",
            )
        return SwitchResult(
            True,
            "按程序使用核显：启动时不要设置 DRI_PRIME",
            detail=f"env -u DRI_PRIME {exe_path}",
            needs_restart=False,
            scope="app",
        )


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------


class MacBackend(SwitchBackend):
    name = "macos-manual"
    description = "macOS 不提供程序化显卡切换，只能打开设置面板手动切换"
    supports_global = False
    supports_per_app = False

    def capabilities(self) -> Dict[str, object]:
        return {
            "程序化切换": "不支持（系统未开放 API）",
            "手动切换": "「系统设置 > 电池 > 自动图形切换」或断开外接显卡",
            "自动切换": "系统按负载与电源状态自动选择",
        }

    def set_global(self, adapter: GpuAdapter, elevate: bool = False) -> SwitchResult:
        try:
            subprocess.Popen(
                ["open", "x-apple.systempreferences:com.apple.Battery-Settings.extension"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            return SwitchResult(False, "无法打开设置面板", detail=str(exc), needs_restart=False)
        return SwitchResult(
            True,
            "已打开电池设置，请手动关闭「自动图形切换」以固定使用独显",
            needs_restart=False,
        )

    def set_for_app(self, adapter: GpuAdapter, exe_path: str) -> SwitchResult:
        return SwitchResult(False, "macOS 不支持按程序指定显卡", needs_restart=False)


# ---------------------------------------------------------------------------
# DXGI 创建期指定（给子进程用）
# ---------------------------------------------------------------------------


def set_process_default_adapter(vendor_id: int, device_id: int) -> bool:
    """在创建 D3D 设备前调用，强制本进程使用指定显卡。

    这是 DXGI 的未公开接口，仅在部分系统/驱动上有效，因此只作为加分项：
    成功时可以让新启动的子进程直接用上目标显卡，失败也不影响注册表方案。
    """
    if not is_windows():
        return False
    try:
        dxgi = ctypes.WinDLL("dxgi")
        func = dxgi.SetProcessDefaultGraphicsAdapter
        func.argtypes = [ctypes.c_uint16, ctypes.c_uint16]
        func.restype = ctypes.c_long
        hr = func(ctypes.c_uint16(vendor_id), ctypes.c_uint16(device_id))
        return hr >= 0
    except Exception as exc:
        logger.debug("SetProcessDefaultGraphicsAdapter 不可用: %s", exc)
        return False


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def get_backend() -> SwitchBackend:
    if is_windows():
        return WindowsBackend()
    if is_linux():
        return LinuxBackend()
    if is_macos():
        return MacBackend()
    return SwitchBackend()


def describe_switch_capability(adapter: GpuAdapter) -> str:
    """给界面用的一句人话说明。"""
    if adapter.kind == TYPE_INTEGRATED:
        return "切到核显：省电、发热低，适合办公与看视频"
    if adapter.kind == TYPE_DISCRETE:
        return "切到独显：性能强，适合游戏与渲染，耗电更高"
    return "该设备不参与显卡切换"
