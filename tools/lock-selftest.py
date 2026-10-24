
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gpuswitch.hardware import (
    ECO_OFF,
    ECO_ON,
    HardwareBackend,
    HardwareMode,
    HardwareResult,
    HardwareState,
)
from gpuswitch.lock import GpuLock

MUX_IGPU, MUX_STANDARD, MUX_ULTIMATE = 0, 1, 2


class FakeHardware(HardwareBackend):

    name = "fake"
    description = "测试用"

    def __init__(self) -> None:
        self.eco = ECO_ON
        self.mux = MUX_STANDARD
        self.writes: List[tuple] = []
        self.lock = threading.Lock()

    def available(self) -> bool:
        return True

    def read_state(self) -> HardwareState:
        with self.lock:
            eco, mux = self.eco, self.mux
        return HardwareState(
            available=True, vendor="FAKE", eco=eco, mux=mux,
            mode_id=self._mode(eco, mux), detail=f"eco={eco} mux={mux}",
        )

    @staticmethod
    def _mode(eco: int, mux: int) -> str:
        if eco == ECO_OFF:
            return "igpu_only"
        if mux == MUX_ULTIMATE:
            return "ultimate"
        if mux == MUX_IGPU:
            return "igpu_display"
        return "standard"

    def modes(self) -> List[HardwareMode]:
        return [
            HardwareMode("dgpu_on", "独显常驻", "测试", eco=ECO_ON),
            HardwareMode("dgpu_off", "关闭独显", "测试", eco=ECO_OFF),
            HardwareMode("ultimate", "独显直连", "测试", eco=ECO_ON, mux=MUX_ULTIMATE,
                         needs_reboot=True, may_blackout=True),
            HardwareMode("standard", "标准混合", "测试", eco=ECO_ON, mux=MUX_STANDARD),
        ]

    def apply_mode(self, mode: HardwareMode) -> HardwareResult:
        with self.lock:
            changed = False
            if mode.eco is not None and mode.eco != self.eco:
                self.eco = mode.eco
                self.writes.append(("eco", mode.eco))
                changed = True
            if mode.mux is not None and mode.mux != self.mux:
                self.mux = mode.mux
                self.writes.append(("mux", mode.mux))
                changed = True
        return HardwareResult(True, f"fake applied {mode.label}", changed=changed)

    def sabotage(self, eco: Optional[int] = None, mux: Optional[int] = None) -> None:
        with self.lock:
            if eco is not None:
                self.eco = eco
            if mux is not None:
                self.mux = mux


class Case:
    def __init__(self, title: str) -> None:
        self.title = title
        self.failures: List[str] = []

    def check(self, condition: bool, message: str) -> None:
        print(f"   {'✓' if condition else '✗'} {message}")
        if not condition:
            self.failures.append(message)

    def section(self, text: str) -> None:
        print(f"\n{text}")


def wait_until(predicate, timeout: float = 6.0, interval: float = 0.1) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def main() -> int:
    hw = FakeHardware()
    events: List[str] = []
    lock = GpuLock(hw, on_event=lambda e: events.append(f"{e.kind}: {e.message}"))
    case = Case("锁定守护逻辑自检")

    print("=" * 70)
    print("锁定守护逻辑自检（假后端，不碰真实硬件）")
    print("=" * 70)

    case.section("1. 锁定「独显常驻」后，别的程序把独显关掉（切回核显）")
    result = lock.enable(lock.find_mode("dgpu_on"))
    case.check(result.ok, f"锁定建立成功（{result.message}）")
    lock.start(0.3)
    case.check(lock.running, "守护线程已启动")

    hw.sabotage(eco=ECO_OFF)
    ok = wait_until(lambda: hw.read_state().eco == ECO_ON)
    case.check(ok, f"被切回核显后自动抢回独显（当前 eco={hw.read_state().eco}）")
    case.check(lock.restore_count >= 1, f"抢回计数 +1（当前 {lock.restore_count}）")

    case.section("2. 用户手动把显示通路切到独显直连（不是被篡改）")
    hw.sabotage(mux=MUX_ULTIMATE)
    time.sleep(1.2)
    mux_now = hw.read_state().mux
    case.check(
        mux_now == MUX_ULTIMATE,
        f"「独显常驻」只管供电，不越权纠正显示通路（mux={mux_now}，应保持 2）",
    )
    case.check(
        hw.read_state().eco == ECO_ON, "独显仍然保持通电"
    )

    case.section("3. 改成锁定「标准混合」（eco+mux 都管），再篡改 MUX")
    lock.disable()
    result = lock.enable(lock.find_mode("standard"))
    case.check(result.ok, f"切换到锁定「标准混合」（{result.message}）")
    lock.start(0.3)

    hw.sabotage(mux=MUX_ULTIMATE, eco=ECO_OFF)
    ok = wait_until(
        lambda: hw.read_state().mux == MUX_STANDARD and hw.read_state().eco == ECO_ON
    )
    state = hw.read_state()
    case.check(ok, f"供电与显示通路都被抢回（eco={state.eco} mux={state.mux}）")

    case.section("4. 反复篡改 5 次，每次都应在 3 秒内被抢回")
    caught = 0
    for index in range(5):
        hw.sabotage(eco=ECO_OFF)
        if wait_until(lambda: hw.read_state().eco == ECO_ON, timeout=3.0):
            caught += 1
    state = hw.read_state()
    case.check(caught == 5, f"5 次篡改全部被抢回（实际 {caught} 次）")
    case.check(state.eco == ECO_ON, f"最终仍是锁定状态（eco={state.eco}）")
    case.check(
        lock.restore_count >= 5, f"抢回计数累计正确（{lock.restore_count} 次）"
    )

    case.section("5. 解锁后篡改，守护必须停止干预")
    lock.disable()
    count_before = lock.restore_count
    hw.sabotage(eco=ECO_OFF)
    time.sleep(1.2)
    case.check(
        hw.read_state().eco == ECO_OFF,
        "解锁后不再干预，用户的手动选择得到尊重",
    )
    case.check(
        lock.restore_count == count_before, "解锁后抢回计数不再增长"
    )

    case.section("6. 固件拒绝写入时应报错而不是假装成功")


    class StubbornHardware(FakeHardware):
        def apply_mode(self, mode: HardwareMode) -> HardwareResult:
            return HardwareResult(True, "假装成功但什么都没做")

    stubborn = StubbornHardware()
    stubborn_lock = GpuLock(stubborn)
    stubborn_lock._satisfied = GpuLock._satisfied
    res = stubborn_lock.enable(stubborn_lock.find_mode("dgpu_off"))
    case.check(not res.ok, f"校验不通过时返回失败（{res.message}）")

    lock.stop()

    print("\n" + "=" * 70)
    print("事件流水（最近 8 条）:")
    for line in events[-8:]:
        print(f"  - {line}")

    print("=" * 70)
    if case.failures:
        print("失败项:")
        for item in case.failures:
            print(f"  ✗ {item}")
        return 1
    print("✓ 锁定守护全部逻辑验证通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
