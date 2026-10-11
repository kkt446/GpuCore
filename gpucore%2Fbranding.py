"""GpuCore 的身份信息 —— 名字、版本、路径集中在这里，改名只改这一个文件。

注意：APP_DIR_NAME 决定安装目录与配置目录。改它会**丢掉已有配置**，
      所以它跟着品牌走，不跟着代码版本走。
"""

from __future__ import annotations

import os
from pathlib import Path

# --- 品牌 ---
APP_NAME = "GpuCore"
APP_NAME_CN = "双芯"
APP_TAGLINE = "显卡热切换 + 资源监视"
APP_VERSION = "1.0.0"
APP_REPO = "https://github.com/KKT446/GpuCore"

# --- 路径 ---
# %LOCALAPPDATA%\GpuCore\          安装目录 / 配置目录
# %LOCALAPPDATA%\GpuCore\app\      Python 包所在
APP_DIR_NAME = "GpuCore"

# 旧名字，仅用于升级时清理残留（新装可忽略）
LEGACY_DIR_NAMES = ("GPUSwitch",)
LEGACY_RUN_NAMES = ("GPUSwitch",)


def bundle_dir() -> Path:
    """便携包根目录（含 GpuCore.exe / main.py / gpucore\\ 的那一层）。"""
    return Path(__file__).resolve().parent.parent


def portable_mode() -> bool:
    """是否运行在免安装便携包里。

    判据：便携包根目录有 GpuCore.exe 或 PORTABLE 标记文件。
    便携模式把配置/日志放在包目录内，做到「解压即用、删文件夹即卸载」。
    """
    base = bundle_dir()
    try:
        if (base / "GpuCore.exe").exists() or (base / "PORTABLE").exists():
            return True
    except OSError:
        pass
    return False


def _writable(path: Path) -> bool:
    """真测一次写入。目录存在但只读（U 盘写保护、Program Files）时返回 False。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def install_dir() -> Path:
    """数据目录（配置、日志、缓存放这里）。

    依次尝试：便携包目录 → %LOCALAPPDATA%\\GpuCore → 用户主目录 → 临时目录。
    任何一步失败都往下退 —— 装在 Program Files、只读 U 盘、或者跑在受限令牌
    下的进程里时，都能找到**真的能写**的地方，而不是启动就崩。
    """
    if portable_mode():
        base = bundle_dir()
        if _writable(base):
            return base

    import tempfile

    root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
    candidates = []
    if root:
        candidates.append(Path(root) / APP_DIR_NAME)
    candidates.append(Path.home() / APP_DIR_NAME)
    candidates.append(Path(tempfile.gettempdir()) / APP_DIR_NAME)
    for c in candidates:
        if _writable(c):
            return c
    return candidates[0]


def app_dir() -> Path:
    """Python 包所在的目录（安装版是 %LOCALAPPDATA%\\GpuCore\\app）。"""
    return install_dir() / "app"


def config_path() -> Path:
    """配置文件全路径。"""
    return install_dir() / "config.json"


def log_path() -> Path:
    """日志文件全路径。"""
    return install_dir() / "gpucore.log"
