
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "GpuCore"
APP_NAME_CN = "双芯"
APP_TAGLINE = "显卡热切换 + 资源监视"
APP_VERSION = "1.1.1"
APP_REPO = "https://github.com/KKT446/GpuCore"

APP_DIR_NAME = "GpuCore"

LEGACY_DIR_NAMES = ("GPUSwitch",)
LEGACY_RUN_NAMES = ("GPUSwitch",)


def bundle_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def portable_mode() -> bool:
    base = bundle_dir()
    try:
        if (base / "GpuCore.exe").exists() or (base / "PORTABLE").exists():
            return True
    except OSError:
        pass
    return False


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def install_dir() -> Path:
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
    return install_dir() / "app"


def config_path() -> Path:
    return install_dir() / "config.json"


def log_path() -> Path:
    return install_dir() / "gpucore.log"
