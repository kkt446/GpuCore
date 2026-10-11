"""GpuCore 免安装版入口。

便携包里的 GpuCore.exe 会调用这个文件。之所以不让启动器直接 `-m gpucore`，
是因为这里显式把包目录挂进 sys.path，无论从哪个工作目录启动都能找到包，
排错时也只需看这一个文件。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gpucore.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
