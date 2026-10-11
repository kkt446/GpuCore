"""便携包冒烟测试：在**包内**解释器里真正把主窗口建出来，然后立刻关掉。

为什么值得单独测：`--check` 只验证采集通路，**不建窗口**。窗口建立过程里
（DPI 设置、字体、Canvas、图标、置顶、托盘收进后台）任何一个环节在别的机器上
炸掉，用户看到的就是"双击没反应"——最难排查的那种故障。

用法（在便携包目录里）：
    python\python.exe packaging\smoke-gui.py
"""

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
    # 走一遍模拟刷新：这一步会把所有标签/进度条都更新一遍，最容易暴露 KeyError
    app._tick()
    # 试一下隐藏/显示（热键回调走的是这两条路径）
    app._hide()
    app.root.update()
    app._show()
    app.root.update()
    # 设置窗口也建一遍（含快捷键录制框）
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
