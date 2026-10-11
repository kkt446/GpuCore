"""按程序指定显卡（通用方案，任何 Windows 笔电都能用）。

参考思路来自 JasonHu051219/GPUSwitch：Windows 的显卡归属在**进程创建时**由
系统读取 `HKCU\\...\\UserGpuPreferences` 决定，所以想"指定某个程序用哪块卡"，
必须在它初始化图形之前把偏好写好。两种做法：

  1. 由本程序启动目标程序（最可靠，零风险）：先写偏好，再 CreateProcess
  2. 拦截系统里新出现的进程（通用，但要重启该进程才生效）：
     用 WMI `Win32_Process` 的 `Creation` 事件感知启动 -> 写偏好 ->
     结束并重启该进程（可选，会丢失未保存数据，默认关闭）

本模块提供这两条路，并维护一份"程序 -> 期望显卡"的名单。
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import log
from .adapter import PREF_HIGH_PERFORMANCE, PREF_POWER_SAVING
from .paths import is_windows

try:  # 可选依赖：装了更省事，没装走 Win32 原生实现
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None

logger = log.get(__name__)

USER_PREFS_SUBKEY = r"Software\Microsoft\DirectX\UserGpuPreferences"
ASK_MARK = "ASK"

# 不希望被拦截/重启的系统关键进程
_PROTECTED = {
    "system", "system idle process", "registry", "memory compression",
    "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe",
    "smss.exe", "svchost.exe", "dwm.exe", "explorer.exe", "fontdrvhost.exe",
    "sihost.exe", "ctfmon.exe", "taskhostw.exe", "runtimebroker.exe",
    "shellexperiencehost.exe", "startmenuexperiencehost.exe",
    "searchhost.exe", "textinputhost.exe", "securityhealthservice.exe",
    "msmpeng.exe", "python.exe", "pythonw.exe", "gpuswitch.exe",
}


@dataclass
class LaunchRule:
    """一条"按程序指定显卡"的规则。"""

    path: str
    preference: int = 0          # 0 系统默认 / 1 省电(集显) / 2 高性能(独显)
    ask: bool = False            # 启动时询问
    restart: bool = False        # 拦截到启动后是否自动重启该进程

    @property
    def name(self) -> str:
        return Path(self.path).name

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "preference": self.preference,
            "ask": self.ask,
            "restart": self.restart,
        }

    @staticmethod
    def from_dict(data: dict) -> Optional["LaunchRule"]:
        path = str((data or {}).get("path", "") or "").strip()
        if not path:
            return None
        return LaunchRule(
            path=path,
            preference=int(data.get("preference", 0) or 0),
            ask=bool(data.get("ask", False)),
            restart=bool(data.get("restart", False)),
        )

    @property
    def preference_label(self) -> str:
        return {
            0: "系统默认",
            PREF_POWER_SAVING: "省电（集显）",
            PREF_HIGH_PERFORMANCE: "高性能（独显）",
        }.get(self.preference, "系统默认")


# ---------------------------------------------------------------------------
# 注册表读写
# ---------------------------------------------------------------------------


def _open_prefs(write: bool):
    """打开 UserGpuPreferences 键。

    读不到/没权限时返回 None，绝不抛异常——调用方需要区分
    "没有这条偏好"和"读不了注册表"，而不是崩掉。
    """
    import winreg

    access = winreg.KEY_READ | (winreg.KEY_WRITE if write else 0)
    try:
        return winreg.OpenKey(winreg.HKEY_CURRENT_USER, USER_PREFS_SUBKEY, 0, access)
    except FileNotFoundError:
        pass
    except PermissionError as exc:
        logger.debug("注册表访问被拒（%s）: %s", "写" if write else "读", exc)
        return None
    except OSError as exc:
        logger.debug("打开注册表键失败: %s", exc)
        return None

    if not write:
        return None

    # 键不存在时逐级创建
    try:
        parent = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\DirectX", 0, winreg.KEY_WRITE
        )
    except OSError as exc:
        logger.debug("打开 DirectX 键失败: %s", exc)
        return None
    try:
        return winreg.CreateKeyEx(parent, "UserGpuPreferences", 0, access)
    except OSError as exc:
        logger.debug("创建 UserGpuPreferences 失败: %s", exc)
        return None
    finally:
        parent.Close()


def registry_writable() -> bool:
    """当前环境能否写注册表偏好。受限环境（沙箱/只读令牌）会返回 False。"""
    if not is_windows():
        return False
    import winreg

    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, USER_PREFS_SUBKEY, 0, winreg.KEY_WRITE
        ) as key:
            winreg.SetValueEx(key, "__gpuswitch_probe__", 0, winreg.REG_SZ, "probe")
            winreg.DeleteValue(key, "__gpuswitch_probe__")
        return True
    except OSError:
        return False


def read_all_preferences() -> Dict[str, str]:
    """读出注册表里所有程序级显卡偏好。"""
    if not is_windows():
        return {}
    import winreg

    result: Dict[str, str] = {}
    try:
        # 用 with 管理句柄：winreg.PyHKEY 只有 Close()，写 .close() 会 AttributeError
        with _open_prefs(write=False) as key:
            if key is None:
                return result
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


def write_preference(path: str, preference: int, ask: bool = False) -> bool:
    """写入某个程序的显卡偏好。返回是否成功。"""
    if not is_windows():
        return False
    import winreg

    value = f"GpuPreference={int(preference)};"
    if ask:
        value += ASK_MARK
    try:
        key = _open_prefs(write=True)
        if key is None:
            return False
        try:
            winreg.SetValueEx(key, str(Path(path)), 0, winreg.REG_SZ, value)
        finally:
            key.Close()
        return True
    except OSError as exc:
        logger.warning("写入显卡偏好失败 %s: %s", path, exc)
        return False


def delete_preference(path: str) -> bool:
    if not is_windows():
        return False
    import winreg

    try:
        key = _open_prefs(write=True)
        if key is None:
            return False
        try:
            winreg.DeleteValue(key, str(Path(path)))
        except FileNotFoundError:
            pass
        finally:
            key.Close()
        return True
    except OSError:
        return False


def parse_preference(value: str) -> tuple:
    """解析 'GpuPreference=2;ASK' -> (2, True)。"""
    if not value:
        return (0, False)
    match = re.search(r"GpuPreference\s*=\s*(\d+)", value, re.I)
    pref = int(match.group(1)) if match else 0
    return (pref, ASK_MARK in value.upper())


# ---------------------------------------------------------------------------
# 启动器：最可靠的按程序指定显卡方式
# ---------------------------------------------------------------------------


def launch_with_gpu(rule: LaunchRule, args: Optional[List[str]] = None) -> bool:
    """先写好偏好再启动程序，确保它一开始就用对显卡。"""
    if not write_preference(rule.path, rule.preference, ask=False):
        logger.warning("偏好写入失败，仍尝试启动: %s", rule.path)
    try:
        cmd = [rule.path] + list(args or [])
        subprocess.Popen(
            cmd,
            cwd=str(Path(rule.path).parent),
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0),
        )
        logger.info("已用「%s」启动 %s", rule.preference_label, rule.name)
        return True
    except Exception as exc:
        logger.exception("启动失败 %s: %s", rule.path, exc)
        return False


# ---------------------------------------------------------------------------
# 进程拦截
# ---------------------------------------------------------------------------


class ProcessWatcher:
    """监听新进程，在它加载图形之前写好显卡偏好。

    WMI 的 `Win32_Process` Creation 事件是秒级的，比轮询省 CPU；
    拿不到 WMI 时自动退化为轮询快照。
    """

    def __init__(
        self,
        rules_provider: Callable[[], List[LaunchRule]],
        on_detected: Optional[Callable[[LaunchRule, int, List[str]], None]] = None,
        cooldown: float = 10.0,
    ) -> None:
        self._rules_provider = rules_provider
        self.on_detected = on_detected
        self.cooldown = cooldown
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._cooldowns: Dict[str, float] = {}
        self._known: Dict[str, float] = {}
        self.mode = ""          # wmi / poll
        self.caught = 0

    # ---- 生命周期 ----

    def start(self) -> bool:
        if not is_windows():
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="proc-watch", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---- 主循环 ----

    def _run(self) -> None:
        """先试 WMI 事件监听；不可用就退化为轮询。

        注意：WMI 线程"启动成功"不等于"能收到事件"——watch_for 本身就可能抛
        异常。所以这里要求它先成功建立监听（state='ok'）才算数，否则老实轮询。
        """
        if self._run_wmi():
            return
        logger.info("WMI 进程监听不可用，退化为轮询（每 1.5 秒）")
        self._run_poll()

    def _run_wmi(self) -> bool:
        try:
            import pythoncom  # type: ignore
            import wmi  # type: ignore
        except Exception as exc:
            logger.debug("缺少 WMI 模块: %s", exc)
            return False

        state: Dict[str, object] = {"mode": "starting", "error": ""}

        def loop() -> None:
            pythoncom.CoInitialize()
            try:
                watcher = wmi.WMI().watch_for(
                    notification_type="Creation", wmi_class="Win32_Process"
                )
                state["mode"] = "ok"
                logger.info("WMI 进程监听已建立")
                while not self._stop.is_set():
                    try:
                        proc = watcher(timeout_ms=1000)
                    except Exception as exc:
                        if "timed out" in str(exc).lower():
                            continue
                        logger.debug("WMI 监听异常: %s", exc)
                        continue
                    try:
                        self._handle(proc.ExecutablePath, int(proc.ProcessId))
                    except Exception:
                        continue
            except Exception as exc:
                state["mode"] = "failed"
                state["error"] = str(exc)
                logger.debug("WMI 监听建立失败: %s", exc)
            finally:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass

        worker = threading.Thread(target=loop, name="proc-watch-wmi", daemon=True)
        worker.start()

        # 等它真的建立起监听，而不是只看线程有没有活着
        deadline = time.time() + 8.0
        while time.time() < deadline:
            if state["mode"] == "ok":
                break
            if state["mode"] == "failed" or self._stop.is_set():
                return False
            time.sleep(0.1)

        if state["mode"] != "ok":
            logger.debug("WMI 监听未能建立（%s）", state.get("error") or "超时")
            return False

        self.mode = "wmi"
        self._wmi_thread = worker
        # 监听生效期间主线程在这里等待；一旦监听线程死了就返回失败并退化
        while not self._stop.is_set() and worker.is_alive():
            time.sleep(0.5)
        if not self._stop.is_set() and not worker.is_alive():
            logger.warning("WMI 监听线程意外退出，退化为轮询")
            self.mode = ""
            return False
        return True

    def _run_poll(self) -> None:
        self.mode = "poll"
        # 先建立基线，避免把已有进程当成"新启动"
        self._baseline()
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception as exc:
                logger.debug("轮询异常: %s", exc)
            self._stop.wait(1.5)

    def _baseline(self) -> None:
        for _pid, path, name in _iter_processes():
            if path:
                self._known[path.lower()] = time.time()
            elif name:
                self._known[name.lower()] = time.time()

    def _poll_once(self) -> None:
        for pid, path, name in _iter_processes():
            if path or name:
                self._handle(path, pid, name)

    # ---- 命中处理 ----

    def _handle(self, exe_path: Optional[str], pid: int, name_hint: str = "") -> None:
        """处理一个进程：能拿到全路径就按路径匹配，拿不到就按进程名兜底。

        有些进程（尤其是 System32 里的）不允许查询镜像路径，如果直接跳过，
        受监控的程序就永远不会被拦截。
        """
        raw = (exe_path or "").strip()
        if raw:
            path = str(Path(raw))
        elif name_hint:
            path = ""          # 只有名字，无法写注册表偏好，但还能匹配规则
        else:
            return

        key = (path or name_hint).lower()
        if Path(name_hint or path).name.lower() in _PROTECTED:
            return
        rule = None if path else self._match_by_name(name_hint)
        if rule is None:
            rule = self._match(path) if path else None
        if rule is None:
            return

        now = time.time()
        if now - self._cooldowns.get(rule.path.lower(), 0.0) <= self.cooldown:
            return
        self._cooldowns[rule.path.lower()] = now
        self.caught += 1

        # 关键动作：在进程初始化图形之前把偏好写进去（写规则里记录的完整路径）
        write_preference(rule.path, rule.preference, ask=False)
        logger.info(
            "拦截到 %s（pid %d，%s），已写入偏好「%s」",
            rule.name, pid, path or f"仅识别到进程名 {name_hint}",
            rule.preference_label,
        )

        if self.on_detected:
            try:
                cmdline = _cmdline(pid)
            except Exception:
                cmdline = []
            try:
                self.on_detected(rule, pid, cmdline)
            except Exception as exc:
                logger.debug("拦截回调异常: %s", exc)

    def _match(self, path: str) -> Optional[LaunchRule]:
        if not path:
            return None
        try:
            rules = self._rules_provider() or []
        except Exception:
            return None
        target = str(Path(path)).lower()
        for rule in rules:
            if str(Path(rule.path)).lower() == target:
                return rule
        return None

    def _match_by_name(self, name: str) -> Optional[LaunchRule]:
        """按可执行文件名匹配（拿不到全路径时的兜底）。"""
        if not name:
            return None
        try:
            rules = self._rules_provider() or []
        except Exception:
            return None
        wanted = name.lower()
        for rule in rules:
            if Path(rule.path).name.lower() == wanted:
                return rule
        return None


def _iter_processes_win32():
    """不依赖 psutil 的进程枚举（CreateToolhelp32Snapshot + QueryFullProcessImageNameW）。

    psutil 是可选的，但这个功能不能因此失效，所以内置一份原生实现。
    """
    import ctypes
    import ctypes.wintypes as wt

    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    MAX_PATH_LONG = 32768

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD),
            ("cntUsage", wt.DWORD),
            ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wt.DWORD),
            ("cntThreads", wt.DWORD),
            ("th32ParentProcessID", wt.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wt.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    kernel32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)
    ]
    kernel32.CloseHandle.argtypes = [wt.HANDLE]

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        return
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return
        while True:
            pid = int(entry.th32ProcessID)
            name = entry.szExeFile or ""
            path = None
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                try:
                    size = wt.DWORD(MAX_PATH_LONG)
                    buffer = ctypes.create_unicode_buffer(size.value)
                    if kernel32.QueryFullProcessImageNameW(
                        handle, 0, buffer, ctypes.byref(size)
                    ):
                        path = buffer.value or None
                finally:
                    kernel32.CloseHandle(handle)
            yield pid, path, name
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)


def _iter_processes():
    """枚举 (pid, exe 全路径, 进程名)。优先 psutil，缺失时用 Win32 原生实现。"""
    if psutil is not None:
        try:
            for proc in psutil.process_iter(["pid", "name", "exe"]):
                try:
                    yield (
                        int(proc.info["pid"]),
                        proc.info.get("exe"),
                        proc.info.get("name") or "",
                    )
                except Exception:
                    continue
            return
        except Exception:
            pass
    if is_windows():
        try:
            yield from _iter_processes_win32()
        except Exception as exc:
            logger.debug("Win32 进程枚举失败: %s", exc)


def _cmdline(pid: int) -> List[str]:
    """取进程命令行（用于重启时保留参数）。没有 psutil 时返回空，退化为按路径启动。"""
    if psutil is None:
        return []
    try:
        return list(psutil.Process(pid).cmdline())
    except Exception:
        return []


def is_running(path: str) -> bool:
    """目标程序当前是否在运行。"""
    target = str(Path(path)).lower()
    wanted_name = Path(path).name.lower()
    for _pid, exe, name in _iter_processes():
        if exe and str(Path(exe)).lower() == target:
            return True
        if not exe and name and name.lower() == wanted_name:
            return True
    return False


def terminate_and_relaunch(path: str, cmdline: Optional[List[str]] = None) -> bool:
    """结束所有同名进程并重新启动（偏好需在调用前写好）。

    这是参考实现用的手段：进程启动后偏好来不及生效，只能重启一次。
    会丢失未保存的数据，因此必须由用户显式确认。
    优先用 psutil 优雅结束，没有 psutil 时退化为 taskkill。
    """
    target = str(Path(path)).lower()
    name = Path(path).name
    terminated = 0

    if psutil is not None:
        victims = []
        for proc in psutil.process_iter(["pid", "exe"]):
            try:
                exe = proc.info.get("exe")
                if exe and str(Path(exe)).lower() == target:
                    victims.append(proc)
            except Exception:
                continue
        for proc in victims:
            try:
                proc.terminate()
                terminated += 1
            except Exception:
                continue
        _, alive = psutil.wait_procs(victims, timeout=3)
        for proc in alive:
            try:
                proc.kill()
            except Exception:
                continue
        if alive:
            psutil.wait_procs(alive, timeout=2)
    else:
        # 没有 psutil 时用系统自带的 taskkill 兜底
        try:
            from .paths import run_capture

            result = run_capture(["taskkill", "/IM", name, "/F"], timeout=10)
            terminated = 1 if result.returncode == 0 else 0
        except Exception as exc:
            logger.warning("taskkill 失败: %s", exc)

    if terminated == 0:
        logger.info("没有找到正在运行的 %s，直接启动", name)

    time.sleep(1.0)  # 给系统时间释放图形句柄
    try:
        if cmdline:
            subprocess.Popen(
                cmdline,
                cwd=str(Path(path).parent),
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        else:
            os.startfile(path)  # type: ignore[attr-defined]
        logger.info("已重启 %s", name)
        return True
    except Exception as exc:
        logger.exception("重启失败: %s", exc)
        return False
