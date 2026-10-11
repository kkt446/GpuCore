"""跨平台路径解析：配置文件、日志、缓存目录。"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def _writable(path: Path) -> bool:
    """探测目录是否真的可写（%APPDATA% 在被沙箱/受限令牌约束时不可写）。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def app_data_dir() -> Path:
    """返回可写的应用数据目录。

    优先系统标准位置，不可写时依次回退到用户主目录、脚本所在目录。
    """
    candidates = []
    if is_windows():
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if base:
            candidates.append(Path(base) / "GPUSwitch")
    elif is_macos():
        candidates.append(Path.home() / "Library" / "Application Support" / "GPUSwitch")
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
        candidates.append((Path(base) if base else Path.home() / ".config") / "gpuswitch")

    candidates.append(Path.home() / ".gpuswitch")
    candidates.append(project_root() / ".local")

    for candidate in candidates:
        if _writable(candidate):
            return candidate
    # 全部失败时返回第一个，让调用方报出明确错误
    return candidates[0]


def project_root() -> Path:
    """项目根目录（含 README 的那一层）。"""
    return Path(__file__).resolve().parent.parent


def config_path() -> Path:
    return app_data_dir() / "config.json"


def log_path() -> Path:
    return app_data_dir() / "gpuswitch.log"


def asset_path(name: str) -> Path:
    return project_root() / "assets" / name


def run_capture(args, timeout: float = 5.0, **kwargs):
    """运行外部命令并捕获输出。

    中文 Windows 上 subprocess 默认用 GBK 解码，遇到 UTF-8 中文输出会直接抛
    UnicodeDecodeError（连 text=True 都救不了）。这里统一按 UTF-8 解码并对
    无法解码的字节做替换，保证任何区域设置下都不会崩。
    """
    import subprocess

    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")
    kwargs.setdefault("timeout", timeout)
    if is_windows():
        kwargs.setdefault("creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return subprocess.run(args, **kwargs)
