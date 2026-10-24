
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gpuswitch.asusprobe import ECO_ON, AtkAcpi, nvidia_present


def smi_state() -> str:
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=pstate,power.draw", "--format=csv,noheader"],
        capture_output=True, text=True, errors="replace",
    )
    text = ((result.stdout or "") + (result.stderr or "")).strip()
    return text.splitlines()[0][:60] if text else "(无输出)"


def main() -> int:
    acpi = AtkAcpi()
    print(f"当前 GPUEco 寄存器 = {acpi.get(0x00090020)}")
    print(f"独显是否在系统里   = {nvidia_present(cache_seconds=0)}")
    print(f"nvidia-smi         = {smi_state()}")

    if nvidia_present(cache_seconds=0) is True:
        print("\n独显已经是通电状态，无需恢复。")
        return 0

    print("\n正在写 eco=0 恢复独显通电…")
    result = acpi.set(0x00090020, ECO_ON)
    print(f"DEVS result = {result}")

    for i in range(10):
        time.sleep(1)
        present = nvidia_present(cache_seconds=0)
        print(f"  +{i + 1}s  独显在系统里 = {present}  nvidia-smi = {smi_state()}")
        if present:
            print("\n✓ 独显已恢复。")
            return 0

    print("\n✗ 10 秒内没有恢复。建议重启一次，或在 G-Helper 里手动切回标准模式。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
