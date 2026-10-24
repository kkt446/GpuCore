
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict

from . import log
from .paths import config_path

DEFAULTS: Dict[str, Any] = {
    "hotkey": "ctrl+a",
    "hotkey_fallback": True,
    "poll_ms": 1000,
    "enumerate_cache_ms": 5000,
    "window_width": 470,
    "window_height": 0,
    "always_on_top": True,
    "window_position": "center",
    "toggle_on_hotkey": True,
    "show_total_usage": True,
    "verbose": False,
    "confirm_switch": True,
    "switch_scope": "self",
    "auto_lock_on_switch": False,
    "lock": {
        "enabled": False,
        "mode_id": "",
        "eco": None,
        "mux": None,
        "applied_at": 0,
        "source": "",
    },
    "lock_interval": 15.0,
    "lock_restore_on_start": True,
    "apps": [],
    "watch_processes": False,
    "watch_cooldown": 10.0,
}


def load() -> Dict[str, Any]:
    cfg = copy.deepcopy(DEFAULTS)
    path = config_path()
    if not path.exists():
        return cfg
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for key, value in raw.items():
                if key in cfg:
                    cfg[key] = value
                else:
                    cfg[key] = value
    except Exception as exc:
        log.get(__name__).warning("配置文件解析失败，使用默认值: %s", exc)
    return cfg


def save(cfg: Dict[str, Any]) -> Path:
    path = config_path()
    merged = copy.deepcopy(DEFAULTS)
    merged.update(cfg or {})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        log.get(__name__).warning("配置写入失败: %s", exc)
    return path


def ensure_exists() -> Path:
    path = config_path()
    if not path.exists():
        save(DEFAULTS)
    return path
