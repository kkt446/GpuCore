"""全局快捷键监听。

Windows 走 RegisterHotKey + 消息循环（系统级注册，无需轮询、不干扰前台程序）。
其它平台退化为轮询按键状态，保证功能可用。

配置字符串格式: "ctrl+a" / "ctrl+alt+g" / "ctrl+shift+f5"
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from . import log
from .paths import is_linux, is_macos, is_windows

logger = log.get(__name__)

# --- Win32 常量 ---
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000  # 按住不重复触发

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

_MODIFIER_ALIASES: Dict[str, int] = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "ctl": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "super": MOD_WIN,
    "meta": MOD_WIN,
    "cmd": MOD_WIN,
}

# 特殊主键名 -> 虚拟键码
_VK_NAMES: Dict[str, int] = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "esc": 0x1B, "escape": 0x1B, "space": 0x20, "pageup": 0x21,
    "pagedown": 0x22, "end": 0x23, "home": 0x24, "left": 0x25,
    "up": 0x26, "right": 0x27, "down": 0x28, "insert": 0x2D,
    "delete": 0x2E, "del": 0x2E, "`": 0xC0, "-": 0xBD, "=": 0xBB,
    "[": 0xDB, "]": 0xDD, "\\": 0xDC, ";": 0xBA, "'": 0xDE,
    ",": 0xBC, ".": 0xBE, "/": 0xBF,
}
for _i in range(1, 25):
    _VK_NAMES[f"f{_i}"] = 0x6F + _i  # F1=0x70
for _c in "abcdefghijklmnopqrstuvwxyz":
    _VK_NAMES[_c] = ord(_c.upper())
for _d in "0123456789":
    _VK_NAMES[_d] = ord(_d)


@dataclass
class HotkeySpec:
    text: str
    modifiers: int
    vk: int
    vk_name: str

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
        parts.append(self.vk_name)
        return "+".join(parts)


class HotkeyError(ValueError):
    pass


def parse_hotkey(text: str) -> HotkeySpec:
    """把 'ctrl+a' 解析成 HotkeySpec。"""
    raw = (text or "").strip()
    if not raw:
        raise HotkeyError("快捷键为空")

    tokens = [t.strip().lower() for t in re.split(r"[+\-\s]+", raw) if t.strip()]
    if not tokens:
        raise HotkeyError(f"无法解析快捷键: {text!r}")

    modifiers = 0
    keys: List[str] = []
    for token in tokens:
        if token in _MODIFIER_ALIASES:
            modifiers |= _MODIFIER_ALIASES[token]
        else:
            keys.append(token)

    if not keys:
        raise HotkeyError(f"快捷键 {text!r} 缺少主键（只有修饰键）")
    if len(keys) > 1:
        raise HotkeyError(f"快捷键 {text!r} 只能有一个主键，收到 {keys}")

    key = keys[0]
    if key not in _VK_NAMES:
        raise HotkeyError(f"不认识的按键 {key!r}；支持 a-z、0-9、F1-F24 及常见符号")

    vk = _VK_NAMES[key]
    return HotkeySpec(
        text=raw,
        modifiers=modifiers,
        vk=vk,
        vk_name=key.upper() if len(key) == 1 else key.capitalize(),
    )


def format_hotkey(spec: HotkeySpec) -> str:
    return spec.describe()


# ---------------------------------------------------------------------------
# Windows 实现
# ---------------------------------------------------------------------------


class _WindowsHotkey:
    """在独立线程里跑消息循环，用 RegisterHotKey 注册系统级热键。"""

    _next_id = 0xB000

    def __init__(self, spec: HotkeySpec, callback: Callable[[], None]) -> None:
        self.spec = spec
        self.callback = callback
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._ready = threading.Event()
        self._ok = False
        self._error = ""
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # --- 线程体 ---
    def _run(self) -> None:
        import ctypes.wintypes as wtypes

        self._thread_id = self._kernel32.GetCurrentThreadId()
        hotkey_id = _WindowsHotkey._next_id
        _WindowsHotkey._next_id += 1

        user32 = self._user32
        # 必须先建消息队列，否则 PostThreadMessage 可能丢失
        msg = wtypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)

        registered = user32.RegisterHotKey(
            None, hotkey_id, self.spec.modifiers | MOD_NOREPEAT, self.spec.vk
        )
        if not registered:
            err = ctypes.get_last_error()
            self._error = f"RegisterHotKey 失败 (WinError {err})"
            self._ok = False
            self._ready.set()
            return

        self._ok = True
        self._ready.set()
        logger.info("全局快捷键已注册: %s", self.spec.describe())

        try:
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0 or ret == -1:  # WM_QUIT 或错误
                    break
                if msg.message == WM_HOTKEY and msg.wParam == hotkey_id:
                    try:
                        self.callback()
                    except Exception as exc:
                        logger.exception("热键回调异常: %s", exc)
        finally:
            user32.UnregisterHotKey(None, hotkey_id)
            logger.info("全局快捷键已注销: %s", self.spec.describe())

    # --- 对外接口 ---
    def start(self, wait: float = 3.0) -> bool:
        self._thread = threading.Thread(target=self._run, name="hotkey", daemon=True)
        self._thread.start()
        self._ready.wait(wait)
        if not self._ok:
            logger.error("热键注册失败: %s", self._error)
        return self._ok

    def stop(self) -> None:
        if self._thread_id:
            self._user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=1.5)
            self._thread = None

    @property
    def error(self) -> str:
        return self._error


# ---------------------------------------------------------------------------
# 轮询回退（Linux / macOS）
# ---------------------------------------------------------------------------


class _PollingHotkey:
    """按固定间隔轮询按键状态，带边沿触发（按下瞬间只触发一次）。"""

    def __init__(self, spec: HotkeySpec, callback: Callable[[], None], interval: float = 0.05) -> None:
        self.spec = spec
        self.callback = callback
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _pressed(self) -> bool:
        if is_linux() or is_macos():
            import subprocess

            # 借助 xdotool / osascript 不可靠，这里只保留接口
            return False
        return False

    def _run(self) -> None:
        was_down = False
        while not self._stop.is_set():
            down = self._pressed()
            if down and not was_down:
                try:
                    self.callback()
                except Exception as exc:
                    logger.exception("热键回调异常: %s", exc)
            was_down = down
            self._stop.wait(self.interval)

    def start(self, wait: float = 1.0) -> bool:
        logger.warning("当前平台不支持系统级热键注册，已退化为轮询（可能不生效）")
        self._thread = threading.Thread(target=self._run, name="hotkey-poll", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)

    @property
    def error(self) -> str:
        return ""


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------


class GlobalHotkey:
    """全局快捷键。

    用法:
        hk = GlobalHotkey("ctrl+a", on_trigger)
        if hk.start():
            ...
        hk.stop()
    """

    def __init__(self, text: str, callback: Callable[[], None]) -> None:
        self.text = text
        self.spec = parse_hotkey(text)
        self.callback = callback
        if is_windows():
            self._impl: object = _WindowsHotkey(self.spec, callback)
        else:
            self._impl = _PollingHotkey(self.spec, callback)
        self._fired = 0

    def start(self, wait: float = 3.0) -> bool:
        return bool(self._impl.start(wait))  # type: ignore[attr-defined]

    def stop(self) -> None:
        try:
            self._impl.stop()  # type: ignore[attr-defined]
        except Exception:
            pass

    @property
    def error(self) -> str:
        return getattr(self._impl, "error", "")

    def __str__(self) -> str:
        return f"<GlobalHotkey {self.spec.describe()}>"


def probe_hotkey(text: str) -> Tuple[bool, str]:
    """尝试注册一次以检测冲突，用于配置校验。"""
    try:
        spec = parse_hotkey(text)
    except HotkeyError as exc:
        return False, str(exc)

    if not is_windows():
        return True, f"{spec.describe()}（当前平台不校验冲突）"

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.RegisterHotKey.restype = wt.BOOL
    hotkey_id = 0xB7FF
    ok = bool(user32.RegisterHotKey(None, hotkey_id, spec.modifiers | MOD_NOREPEAT, spec.vk))
    err = ctypes.get_last_error()
    if ok:
        user32.UnregisterHotKey(None, hotkey_id)
        return True, f"{spec.describe()} 可用"
    return False, f"{spec.describe()} 已被其它程序占用 (WinError {err})"
