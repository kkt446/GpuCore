"""GpuCore（双芯）—— 华硕笔记本显卡热切换 + 资源监视。"""

from .branding import APP_NAME, APP_VERSION
from .app import main

__all__ = ["APP_NAME", "APP_VERSION", "main"]
