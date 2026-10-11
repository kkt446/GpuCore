"""开发辅助：把面板渲染成 PNG，便于在没有人工点按的情况下检查界面。

用法:  python tools/screenshot.py [输出路径]
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gpuswitch import config as config_module
from gpuswitch.gpu import enumerate_adapters
from gpuswitch.metrics import MetricsCollector
from gpuswitch.ui import GpuPanel, prepare_dpi


def hwnd_of(widget) -> int:
    """取顶层窗口句柄。"""
    widget.update_idletasks()
    user32 = ctypes.WinDLL("user32")
    hwnd = user32.GetParent(widget.winfo_id())
    return int(hwnd or widget.winfo_id())


def capture(hwnd: int, path: Path) -> bool:
    """用 PrintWindow 抓取窗口位图并保存为 PNG。"""
    import tkinter as tk  # noqa: F401

    user32 = ctypes.WinDLL("user32")
    gdi32 = ctypes.WinDLL("gdi32")

    rect = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        print("GetWindowRect 失败")
        return False
    width = rect.right - rect.left
    height = rect.bottom - rect.top

    window_dc = user32.GetWindowDC(hwnd)
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    gdi32.SelectObject(memory_dc, bitmap)

    # PW_RENDERFULLCONTENT = 2，能抓到分层/合成内容
    ok = user32.PrintWindow(hwnd, memory_dc, 2)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
            ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
            ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
            ("biClrImportant", wt.DWORD),
        ]

    header = BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    header.biWidth = width
    header.biHeight = -height  # 自顶向下
    header.biPlanes = 1
    header.biBitCount = 32
    header.biCompression = 0  # BI_RGB

    buffer_size = width * height * 4
    buffer = ctypes.create_string_buffer(buffer_size)
    gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0)

    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(memory_dc)
    user32.ReleaseDC(hwnd, window_dc)

    try:
        from PIL import Image

        image = Image.frombuffer("RGBA", (width, height), buffer, "raw", "BGRA", 0, 1)
        image.convert("RGB").save(path)
        print(f"PrintWindow={'OK' if ok else 'FAIL'} 已保存 {path} ({width}x{height})")
        return True
    except Exception as exc:
        print(f"PIL 保存失败: {exc}")
        return False


def main() -> int:
    import tkinter as tk

    prepare_dpi()
    cfg = config_module.DEFAULTS.copy()

    root = tk.Tk()
    collector = MetricsCollector(interval_ms=300)
    adapters = enumerate_adapters(use_cache=False)
    root.deiconify()

    # 有硬件通道时，顺便把锁定卡片渲染成"已锁定"的样子，方便视觉验收
    from gpuswitch.hardware import detect_hardware
    from gpuswitch.lock import GpuLock, LockRecord

    hardware = detect_hardware()
    lock = GpuLock(hardware, record=LockRecord())
    if hardware.available():
        modes = hardware.modes()
        if modes:
            state = hardware.read_state()
            lock.record = LockRecord(
                enabled=True, mode_id=modes[0].mode_id, eco=state.eco, mux=state.mux
            )

    panel = GpuPanel(
        root, adapters, collector, on_switch=lambda a: None, config=cfg,
        on_refresh=lambda: enumerate_adapters(use_cache=False),
        on_open_settings=lambda: None,
        lock=lock,
        on_lock_toggle=lambda m: None,
    )
    panel.build()
    panel.set_message(
        "硬件切换：已把独显切回常驻（标准混合模式）；显卡偏好：已写入 GpuPreference=2",
        "ok",
    )
    collector.start()
    panel.show()

    # 等采集器出数据
    for _ in range(12):
        root.update()
        time.sleep(0.25)
    panel.tick()
    panel.refresh_hardware()
    root.update()
    time.sleep(0.3)
    root.update()

    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("panel-preview.png")
    capture(hwnd_of(root), out)
    collector.stop()
    root.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
