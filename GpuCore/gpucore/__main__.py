"""支持 `python -m gpucore` 直接启动。"""

import sys

from .app import main

if __name__ == "__main__":
    sys.exit(main())
