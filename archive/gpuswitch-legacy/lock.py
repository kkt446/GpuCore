
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import log
from .hardware import (
    ECO_OFF,
    ECO_ON,
    MUX_ULTIMATE,
    HardwareBackend,
    HardwareMode,
    HardwareResult,
    HardwareState,
)

logger = log.get(__name__)

CONFIG_KEY = "lock"


@dataclass
class LockRecord:

    enabled: bool = False
    mode_id: str = ""
    eco: Optional[int] = None
    mux: Optional[int] = None
    applied_at: float = 0.0
    source: str = ""

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "mode_id": self.mode_id,
            "eco": self.eco,
            "mux": self.mux,
            "applied_at": self.applied_at,
            "source": self.source,
        }

    @staticmethod
    def from_dict(data: Optional[dict]) -> "LockRecord":
        if not isinstance(data, dict):
            return LockRecord()
        return LockRecord(
            enabled=bool(data.get("enabled", False)),
            mode_id=str(data.get("mode_id", "") or ""),
            eco=data.get("eco") if isinstance(data.get("eco"), int) else None,
            mux=data.get("mux") if isinstance(data.get("mux"), int) else None,
            applied_at=float(data.get("applied_at", 0) or 0),
            source=str(data.get("source", "") or ""),
        )


@dataclass
class LockEvent:

    kind: str
    message: str
    detail: str = ""
    at: float = field(default_factory=time.time)


class GpuLock:

    def __init__(
        self,
        backend: HardwareBackend,
        on_event: Optional[Callable[[LockEvent], None]] = None,
        record: Optional[LockRecord] = None,
    ) -> None:
        self.backend = backend
        self.on_event = on_event
        self.record = record or LockRecord()
        self.interval = 4.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.drift_count = 0
        self.restore_count = 0
        self.last_event: Optional[LockEvent] = None
        self.last_state: Optional[HardwareState] = None
        self._consecutive_failures = 0


    def start(self, interval: float = 4.0) -> bool:
        if not self.backend.available():
            logger.info("硬件通道不可用，锁定守护不启动（将只用注册表偏好）")
            return False
        if not self.record.enabled or not self.record.mode_id:
            return False
        self.interval = max(1.0, float(interval))
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="gpu-lock", daemon=True)
        self._thread.start()
        logger.info("锁定守护已启动：模式 %s，间隔 %.1fs", self.record.mode_id, self.interval)
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())


    def find_mode(self, mode_id: str) -> Optional[HardwareMode]:
        for mode in self.backend.modes():
            if mode.mode_id == mode_id:
                return mode
        return None

    @staticmethod
    def _satisfied(mode: HardwareMode, state: HardwareState) -> bool:
        pairs = [(mode.eco, state.eco), (mode.mux, state.mux)]
        checked = [(want, now) for want, now in pairs if want is not None]
        if not checked:
            return state.mode_id == mode.mode_id
        return all(want == now for want, now in checked)


    def adopt(self, mode: HardwareMode, source: str = "external") -> HardwareResult:
        state = self.backend.read_state()
        satisfied = self._satisfied(mode, state)

        with self._lock:
            self.record = LockRecord(
                enabled=True,
                mode_id=mode.mode_id,
                eco=state.eco,
                mux=state.mux,
                applied_at=time.time(),
                source=source,
            )
            self.drift_count = 0
            self.restore_count = 0
            self.last_state = state

        self._emit(
            "applied",
            f"已按「{mode.label}」锁定",
            f"当前：{state.detail}",
        )
        return HardwareResult(
            True,
            f"已锁定「{mode.label}」",
            detail=state.detail,
            changed=False,
            needs_reboot=not satisfied,
        )

    def enable(self, mode: HardwareMode, source: str = "ui") -> HardwareResult:
        result = self.backend.apply_mode(mode)
        if not result.ok:
            self._emit("failed", f"锁定失败：{result.message}", result.detail)
            return result

        state = self.backend.read_state()
        if not self._satisfied(mode, state):
            self._emit(
                "failed",
                f"写入后校验未通过：{state.detail}",
                "固件可能拒绝了这次切换，或需要重启才生效",
            )
            return HardwareResult(
                False,
                "切换未生效",
                detail=f"当前仍是 {state.mode_id}（{state.detail}）",
                needs_reboot=mode.needs_reboot,
            )

        with self._lock:
            self.record = LockRecord(
                enabled=True,
                mode_id=mode.mode_id,
                eco=state.eco,
                mux=state.mux,
                applied_at=time.time(),
                source=source,
            )
            self.drift_count = 0
            self.restore_count = 0
            self.last_state = state

        self._emit(
            "applied",
            f"已锁定「{mode.label}」：{result.message}",
            result.detail,
        )
        return result

    def disable(self) -> None:
        with self._lock:
            self.record.enabled = False
        self.stop()
        self._emit("applied", "已解除锁定，不再监视显卡状态", "")

    def expect_change(self) -> bool:
        was_running = self.running
        self.stop()
        logger.info("准备执行显式切换，守护已让路（原状态 %s）", self.record.mode_id or "未锁定")
        return was_running

    def release(self) -> None:
        if not self.record.enabled:
            return
        if self.record.mode_id and not self.running:
            self.start()

    def cancel_change(self) -> None:
        if self.record.enabled and not self.running:
            self.start()

    def reapply(self) -> HardwareResult:
        mode = self.find_mode(self.record.mode_id)
        if mode is None:
            return HardwareResult(False, f"锁定记录里的模式 {self.record.mode_id!r} 在当前机型上不存在")
        return self.backend.apply_mode(mode)


    def _loop(self) -> None:
        self._stop.wait(2.0)
        while not self._stop.is_set():
            try:
                self._check_once()
            except Exception as exc:
                logger.debug("锁定守护异常: %s", exc)
            self._stop.wait(self.interval)

    def _check_once(self) -> None:
        with self._lock:
            if not self.record.enabled:
                return
            record = LockRecord(**self.record.to_dict())

        state = self.backend.read_state()
        if not state.available:
            return
        with self._lock:
            self.last_state = state

        mode = self.find_mode(record.mode_id)
        if mode is None:
            with self._lock:
                self._consecutive_failures = 0
            return

        if self._satisfied(mode, state):
            with self._lock:
                self._consecutive_failures = 0
            return

        with self._lock:
            self.drift_count += 1
        self._emit(
            "drift",
            f"检测到显卡状态被改动：{record.mode_id} -> {state.mode_id}，正在抢回…",
            state.detail,
        )

        result = self.backend.apply_mode(mode)
        if result.ok:
            with self._lock:
                self.restore_count += 1
                self._consecutive_failures = 0
            self._emit(
                "restored",
                f"已抢回「{mode.label}」（第 {self.restore_count} 次）",
                result.detail,
            )
        else:
            with self._lock:
                self._consecutive_failures += 1
                failures = self._consecutive_failures
            self._emit(
                "failed",
                f"抢回失败：{result.message}",
                f"{result.detail}（连续失败 {failures} 次）",
            )
            if failures >= 3:
                logger.warning("连续 3 次抢回失败，暂停守护 60 秒以免反复触发固件动作")
                self._stop.wait(60.0)


    def _emit(self, kind: str, message: str, detail: str = "") -> None:
        event = LockEvent(kind=kind, message=message, detail=detail)
        with self._lock:
            self.last_event = event
        logger.info("[锁定] %s %s", kind, message)
        if self.on_event:
            try:
                self.on_event(event)
            except Exception as exc:
                logger.debug("锁定事件回调异常: %s", exc)


    def status(self) -> Dict[str, object]:
        with self._lock:
            return {
                "enabled": self.record.enabled,
                "mode_id": self.record.mode_id,
                "running": self.running,
                "drift_count": self.drift_count,
                "restore_count": self.restore_count,
                "last_event": self.last_event,
                "state": self.last_state,
            }

    def status_text(self) -> str:
        info = self.status()
        if not info["enabled"]:
            return "未锁定"
        if not self.backend.available():
            return "已锁定（无硬件通道，仅注册表偏好）"
        base = f"已锁定「{info['mode_id']}」"
        if info["restore_count"]:
            base += f" · 已抢回 {info['restore_count']} 次"
        return base
