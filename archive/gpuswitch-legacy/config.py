"""配置读写：JSON 文件，带默认值与宽松校验。

配置位置见 paths.config_path()，默认：
    Windows  %APPDATA%\\GPUSwitch\\config.json
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict

from . import log
from .paths import config_path

DEFAULTS: Dict[str, Any] = {
    # 全局快捷键：修饰键用 + 连接，最后一个是主键。例如 "ctrl+a"、"ctrl+alt+g"
    "hotkey": "ctrl+a",
    # 主热键被别的程序占用时，是否自动改用候选键（结果会写回这里）
    "hotkey_fallback": True,
    # 指标采样间隔（毫秒）
    "poll_ms": 1000,
    # 硬件枚举缓存时间（毫秒），避免频繁枚举
    "enumerate_cache_ms": 5000,
    # 面板宽度（按 96 DPI 的逻辑像素，实际会按系统缩放自动换算）
    "window_width": 470,
    # 面板高度：0 表示按内容自适应（推荐）
    "window_height": 0,
    # 面板是否置顶
    "always_on_top": True,
    # 面板出现位置：center / cursor / top-right
    "window_position": "center",
    # 再按一次快捷键是否收起面板
    "toggle_on_hotkey": True,
    # 是否显示"总占用"（所有进程合计，任务管理器口径）
    "show_total_usage": True,
    # 是否常显日志窗口（False 时只在出错时提示）
    "verbose": False,
    # 切换后是否自动把注册表写入结果提示出来
    "confirm_switch": True,
    # 切换目标：self=只对当前程序生效（无需管理员）；global=写入全局默认（需要管理员）
    "switch_scope": "self",
    # 点显卡按钮做硬件切换时，是否自动开启锁定（防止被其它工具切回去）
    # 默认关闭：实测这台机器上"探测独显"这个动作本身就会把它唤醒，
    # 自动守护会变成自己反复把独显打开。需要对抗外部工具时再手动开。
    "auto_lock_on_switch": False,
    # --- 显卡选择锁定（防其它工具改回去）---
    "lock": {
        # 是否开启锁定；开启后守护线程会把被改动的状态抢回来
        "enabled": False,
        # 锁定的硬件模式 id：dgpu_on / dgpu_off / ultimate / standard
        "mode_id": "",
        # 应用时记录的硬件基线，用于还原
        "eco": None,
        "mux": None,
        "applied_at": 0,
        "source": "",
    },
    # 守护线程检查间隔（秒）。默认 15 秒而不是 4 秒：
    # 每次检查都要探测独显是否在位，而探测动作本身可能把独显唤醒，
    # 所以间隔拉开、减少无谓打扰；需要更快抢回时再调小。
    "lock_interval": 15.0,
    # 是否在启动时自动恢复上次的锁定
    "lock_restore_on_start": True,
    # --- 按程序指定显卡（通用方案，任何 Windows 笔电都能用）---
    # 每项: {"path": "C:\\game\\game.exe", "preference": 2, "ask": false, "restart": false}
    #   preference: 0 系统默认 / 1 省电(集显) / 2 高性能(独显)
    #   ask:        该程序启动时弹窗询问用哪块卡
    #   restart:    拦截到启动后自动重启该进程让偏好生效（会丢未保存数据，默认关）
    "apps": [],
    # 是否启用进程拦截
    "watch_processes": False,
    # 同一程序两次拦截的冷却时间（秒），防止重启后自己又触发一次
    "watch_cooldown": 10.0,
}


def load() -> Dict[str, Any]:
    """读取配置；文件缺失或损坏时回落到默认值并补全缺失项。"""
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
                # 未知键保留，方便用户写注释性字段
                else:
                    cfg[key] = value
    except Exception as exc:  # 损坏的配置不应导致启动失败
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
    """首次运行时落一份带默认值的配置文件，方便用户手改。"""
    path = config_path()
    if not path.exists():
        save(DEFAULTS)
    return path
