"""极简日志：同时写文件与 stderr，避免依赖。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .paths import log_path

_configured = False


def setup(level: int = logging.INFO, to_file: bool = True) -> logging.Logger:
    global _configured
    logger = logging.getLogger("gpuswitch")
    if _configured:
        return logger

    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")

    # Windows 控制台默认是 GBK，中文日志会乱码甚至抛 UnicodeEncodeError。
    # 这里显式用 UTF-8 并忽略无法编码的字符，保证任何终端都不会崩。
    try:
        stream = logging.StreamHandler(
            open(sys.stderr.fileno(), "w", encoding="utf-8", errors="replace", closefd=False)
        )
    except Exception:
        stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    if to_file:
        try:
            handler = RotatingFileHandler(
                log_path(), maxBytes=512 * 1024, backupCount=2, encoding="utf-8"
            )
            handler.setFormatter(fmt)
            logger.addHandler(handler)
        except Exception:
            # 日志文件不可写不应影响主流程
            pass

    _configured = True
    return logger


def get(name: str = "gpuswitch") -> logging.Logger:
    return logging.getLogger(name)
