"""GPUSwitch - 全局快捷键唤出的显卡监控与切换面板。

模块结构：
    config      配置读写
    gpu         显卡枚举（DXGI / 注册表 / LSPCI 回退）
    metrics     指标采集（GPU 占用、显存、CPU、内存）
    hotkey      全局快捷键监听
    switcher    首选显卡切换（多平台后端）
    ui          Tkinter 面板
    app         程序装配与主循环
"""

__version__ = "1.0.0"
APP_NAME = "GPUSwitch"
