
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import tkinter as tk
    from gpucore.app import App, prepare_dpi
except Exception:
    traceback.print_exc()
    raise SystemExit("导入失败")

ok = True
try:
    scale = prepare_dpi()
    print(f"DPI scaling = {scale}")
except Exception:
    traceback.print_exc()
    ok = False

try:
    app = App()
    app.root.update_idletasks()
    app.root.update()
    w = app.root.winfo_width()
    h = app.root.winfo_height()
    title = app.root.title()
    print(f"窗口: {w}x{h}  标题: {title}")
    if w < 100 or h < 100:
        ok = False
        print("窗口尺寸异常（可能布局/控件构建失败）")
    app._tick()
    app._hide()
    app.root.update()
    app._show()
    app.root.update()
    app._open_settings()
    app.root.update_idletasks()
    app.root.update()
    print("设置窗口 OK")
    app.root.destroy()
except Exception:
    traceback.print_exc()
    ok = False

print("GUI 冒烟测试:", "通过 ✓" if ok else "失败 ✗")
raise SystemExit(0 if ok else 1)
