
from __future__ import annotations

import json
import sys
import time

from . import log
from .adapter import format_bytes
from .gpu import describe_current, enumerate_adapters
from .metrics import MetricsCollector


def _section(title: str) -> None:
    print()
    print("=" * 68)
    print(title)
    print("=" * 68)


def run_check() -> int:
    logger = log.setup()
    problems: list[str] = []

    _section("1. 显卡枚举")
    adapters = enumerate_adapters(use_cache=False)
    if not adapters:
        problems.append("没有枚举到任何显卡")
        print("  ✗ 未枚举到显卡")
    for adapter in adapters:
        flag = "★在用" if adapter.drives_display else "     "
        print(
            f"  {flag} {adapter.name}\n"
            f"        类型: {adapter.kind_label} | 厂商: {adapter.vendor_name}\n"
            f"        显存: {adapter.vram_label} | LUID: {adapter.luid:#018x}\n"
            f"        来源: {adapter.source} | 切换候选: {'是' if adapter.is_switchable else '否'}"
        )

    current = describe_current(adapters)
    print(f"\n  判定当前使用: {current.name if current else '未知'}")

    _section("2. 指标采集")
    collector = MetricsCollector(interval_ms=500)
    print(f"  采集后端: {collector.backend}")
    collector.start()
    try:
        for round_index in range(3):
            time.sleep(0.8)
            system = collector.system()
            collector.apply_to_adapters(adapters)
            print(
                f"  第{round_index + 1}次: CPU {system.cpu_percent:5.1f}%  "
                f"内存 {system.ram_label} ({system.ram_percent:.1f}%)"
            )
            for adapter in adapters:
                if adapter.kind == "virtual":
                    continue
                print(
                    f"          {adapter.name[:34]:<34} GPU {adapter.util_percent:5.1f}%  "
                    f"显存 {format_bytes(adapter.vram_used)}"
                )
        if all(collector.system().cpu_percent == 0.0 for _ in range(1)):
            problems.append("CPU 采样始终为 0")
    finally:
        collector.stop()

    _section("3. 全局热键")
    try:
        import os

        from .config import load
        from .hotkey import GlobalHotkey, parse_hotkey

        hotkey_text = os.environ.get("GPUSWITCH_CHECK_HOTKEY") or str(load().get("hotkey", "ctrl+a"))
        spec = parse_hotkey(hotkey_text)
        print(f"  配置: {hotkey_text} -> 修饰键 0x{spec.modifiers:04X} 主键 0x{spec.vk:02X} ({spec.vk_name})")
        fired = {"count": 0}
        listener = GlobalHotkey(hotkey_text, lambda: fired.__setitem__("count", fired["count"] + 1))
        if listener.start():
            print("  ✓ RegisterHotKey 成功，已在消息线程里等待按键")
            print("    （自检不做真实按键注入；热键注册成功即代表可被全局捕获）")
            listener.stop()
        else:
            problems.append(f"热键 {hotkey_text} 注册失败（可能被其它程序占用）")
            print("  ✗ 注册失败")
    except Exception as exc:
        problems.append(f"热键模块异常: {exc}")
        print(f"  ✗ 异常: {exc}")

    _section("4. 切换后端")
    try:
        from .switcher import get_backend

        backend = get_backend()
        caps = backend.capabilities()
        print(f"  后端: {backend.name}")
        print(f"  说明: {backend.description}")
        for key, value in caps.items():
            print(f"    - {key}: {value}")
    except Exception as exc:
        problems.append(f"切换后端异常: {exc}")
        print(f"  ✗ 异常: {exc}")

    _section("5. 界面构建")
    try:
        import tkinter as tk

        from .config import DEFAULTS

        root = tk.Tk()
        root.withdraw()
        from .ui import GpuPanel, prepare_dpi

        prepare_dpi()
        panel = GpuPanel(
            root,
            adapters,
            collector,
            on_switch=lambda adapter: None,
            config=dict(DEFAULTS),
        )
        panel.build()
        root.update_idletasks()
        print(
            f"  ✓ Tkinter 面板构建成功（{panel.width}x{panel.height} 实际像素，"
            f"缩放 {prepare_dpi():.2f}x）"
        )
        root.destroy()
    except Exception as exc:
        problems.append(f"界面构建失败: {exc}")
        print(f"  ✗ 异常: {exc}")

    _section("结论")
    if problems:
        for item in problems:
            print(f"  ✗ {item}")
        return 1
    print("  ✓ 全部自检通过")
    return 0


def run_json() -> int:
    log.setup()
    adapters = enumerate_adapters(use_cache=False)
    collector = MetricsCollector()
    collector.start()
    time.sleep(1.2)
    collector.apply_to_adapters(adapters)
    collector.stop()
    current = describe_current(adapters)
    print(
        json.dumps(
            {
                "current": current.name if current else None,
                "adapters": [a.to_dict() for a in adapters],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(run_check())
