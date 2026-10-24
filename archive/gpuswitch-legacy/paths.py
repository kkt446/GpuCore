
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
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def app_data_dir() -> Path:
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
    return candidates[0]


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def config_path() -> Path:
    return app_data_dir() / "config.json"


def log_path() -> Path:
    return app_data_dir() / "gpuswitch.log"


def asset_path(name: str) -> Path:
    return project_root() / "assets" / name


def run_capture(args, timeout: float = 5.0, **kwargs):
    import subprocess

    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")
    kwargs.setdefault("timeout", timeout)
    if is_windows():
        kwargs.setdefault("creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return subprocess.run(args, **kwargs)
