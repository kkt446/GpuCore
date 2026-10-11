"""华硕专用面板。

设计目标：不要再让"哪块卡在用"产生歧义。

关键改动：**独显和核显的占用分开显示**。
混合模式下两块卡同时在位，只报一个"GPU 占用"必然误导人——
之前就是把核显的 29% 显示在独显的名字旁边，看起来像读错了。

界面分四块：
    1. 顶部：机型 + 当前模式（集显/标准/独显直连）+ 独显供电与显示通路
    2. 显卡占用：独显、核显、显存、温度，各自独立成行
    3. 显卡模式：集显模式 / 标准模式 / 独显直连 —— 点一下就是硬件切换
    4. 系统与状态：CPU、内存、最近一次操作结果
"""

from __future__ import annotations

import time
import tkinter as tk
from typing import Callable, Dict, List, Optional

from . import log
from .adapter import TYPE_DISCRETE, TYPE_INTEGRATED, TYPE_VIRTUAL, GpuAdapter, format_bytes
from .asusprobe import (
    ASUS_MODES,
    ECO_OFF,
    ECO_ON,
    MUX_ULTIMATE,
    AsusMode,
    AtkAcpi,
    detect_current_mode,
)
from .metrics import MetricsCollector
from .ui import (
    ACCENT,
    BAR_CPU,
    BAR_GPU,
    BAR_RAM,
    BG,
    BTN_BG,
    CARD,
    ERROR,
    OK,
    TEXT,
    TEXT_DIM,
    TEXT_FAINT,
    WARN,
    Bar,
    Card,
    FlatButton,
    ToggleButton,
    font,
    px,
)

logger = log.get(__name__)

BAR_DGPU = "#76B900"   # NVIDIA 绿
BAR_IGPU = "#00A3EE"   # Intel 蓝
BAR_TEMP = "#E5534B"


class AsusPanel:
    """华硕专用主面板。"""

    def __init__(
        self,
        root: tk.Tk,
        collector: MetricsCollector,
        adapters_provider: Callable[[], List[GpuAdapter]],
        on_mode: Callable[[AsusMode], None],
        config: Optional[dict] = None,
        on_refresh: Optional[Callable[[], None]] = None,
        lock=None,
        on_lock_toggle: Optional[Callable[[object], None]] = None,
    ) -> None:
        self.root = root
        self.collector = collector
        self.adapters_provider = adapters_provider
        self.on_mode = on_mode
        self.config = config or {}
        self.on_refresh = on_refresh
        self.lock = lock
        self.on_lock_toggle = on_lock_toggle

        self.width = px(int(self.config.get("window_width", 500)))
        self.height = px(int(self.config.get("window_height", 0) or 0))
        self._mode_buttons: Dict[str, ToggleButton] = {}
        self._message = ""
        self._message_kind = "info"
        self._pending: List[tuple] = []
        self._acpi = AtkAcpi()
        self._last_hw: dict = {}
        self._hw_tick = 0

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------

    def build(self) -> None:
        root = self.root
        root.title("华硕显卡切换")
        root.configure(bg=BG)
        root.resizable(False, False)
        try:
            root.attributes("-topmost", bool(self.config.get("always_on_top", True)))
        except Exception:
            pass
        root.bind("<Escape>", lambda _e: self.hide())
        root.protocol("WM_DELETE_WINDOW", self.hide)

        wrap = tk.Frame(root, bg=BG)
        wrap.pack(fill="both", expand=True, padx=px(8), pady=px(8))
        self._wrap = wrap

        self._build_header(wrap)
        self._build_usage(wrap)
        self._build_modes(wrap)
        self._build_footer(wrap)

        root.update_idletasks()
        natural = wrap.winfo_reqheight() + px(16)
        configured = px(int(self.config.get("window_height", 0) or 0))
        self.height = max(px(320), min(natural, configured) if configured else natural)
        root.geometry(f"{self.width}x{self.height}")
        self._center()

        self.refresh_static()
        self.tick()

    def _center(self) -> None:
        self.root.update_idletasks()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        position = str(self.config.get("window_position", "center"))
        if position == "cursor":
            x = self.root.winfo_pointerx() - self.width // 2
            y = self.root.winfo_pointery() - px(30)
        elif position == "top-right":
            x, y = screen_w - self.width - px(24), px(24)
        else:
            x = (screen_w - self.width) // 2
            y = max(px(20), (screen_h - self.height) // 2 - px(40))
        x = max(0, min(x, screen_w - self.width))
        y = max(0, min(y, screen_h - self.height))
        self.root.geometry(f"{self.width}x{self.height}+{x}+{y}")

    # ---- 1. 顶部：机型 + 当前模式 ----

    def _build_header(self, parent: tk.Frame) -> None:
        card = Card(parent)
        card.pack(fill="x", pady=(0, px(8)))
        self.header_card = card

        row = tk.Frame(card, bg=CARD)
        row.pack(fill="x", padx=px(11), pady=(px(9), px(6)))

        left = tk.Frame(row, bg=CARD)
        left.pack(side="left", fill="x", expand=True)
        self.model_label = tk.Label(
            left, text="读取中…", bg=CARD, fg=TEXT_FAINT, font=font(8), anchor="w"
        )
        self.model_label.pack(anchor="w")
        self.mode_big_label = tk.Label(
            left, text="—", bg=CARD, fg=ACCENT, font=font(15, True), anchor="w"
        )
        self.mode_big_label.pack(anchor="w", pady=(px(1), 0))

        self.power_label = tk.Label(
            card, text="", bg=CARD, fg=TEXT, font=font(10), anchor="w",
            justify="left", wraplength=self.width - px(40),
        )
        self.power_label.pack(fill="x", padx=px(11))

        self.mux_label = tk.Label(
            card, text="", bg=CARD, fg=TEXT_DIM, font=font(9), anchor="w",
            justify="left", wraplength=self.width - px(40),
        )
        self.mux_label.pack(fill="x", padx=px(11), pady=(px(2), px(9)))

    # ---- 2. 显卡占用：独显 / 核显 分开 ----

    def _build_usage(self, parent: tk.Frame) -> None:
        card = Card(parent, "显卡占用")
        card.pack(fill="x", pady=(0, px(8)))
        self.usage_card = card

        body = tk.Frame(card, bg=CARD)
        body.pack(fill="x", padx=px(11), pady=(px(5), px(11)))

        self.dgpu_bar = Bar(body, "独立显卡", BAR_DGPU, width=300)
        self.dgpu_bar.pack(fill="x", pady=(px(4), px(6)))
        self.igpu_bar = Bar(body, "核心显卡", BAR_IGPU, width=300)
        self.igpu_bar.pack(fill="x", pady=(0, px(6)))
        self.vram_bar = Bar(body, "显存占用", "#9AA0A6", width=300)
        self.vram_bar.pack(fill="x", pady=(0, px(6)))
        self.temp_bar = Bar(body, "温度 / CPU", BAR_CPU, width=300)
        self.temp_bar.pack(fill="x", pady=(0, px(6)))
        self.ram_bar = Bar(body, "内存占用", BAR_RAM, width=300)
        self.ram_bar.pack(fill="x")

    # ---- 3. 模式按钮 ----

    def _build_modes(self, parent: tk.Frame) -> None:
        card = Card(parent, "显卡模式")
        card.pack(fill="x", pady=(0, px(8)))
        self.modes_card = card

        body = tk.Frame(card, bg=CARD)
        body.pack(fill="x", padx=px(11), pady=(px(7), px(10)))
        grid = tk.Frame(body, bg=CARD)
        grid.pack(fill="x")

        for index, mode in enumerate(ASUS_MODES):
            row, column = divmod(index, 2)
            hints = []
            if mode.needs_reboot:
                hints.append("需重启")
            if mode.may_blackout:
                hints.append("会黑屏")
            button = ToggleButton(
                grid,
                text=mode.label,
                subtext=" · ".join(hints) if hints else "点击切换",
                command=lambda m=mode: self.on_mode(m),
                height=54,
            )
            button.grid(row=row, column=column, sticky="nsew", padx=px(3), pady=px(3))
            self._mode_buttons[mode.mode_id] = button
        for column in range(2):
            grid.grid_columnconfigure(column, weight=1, uniform="mode")

        self.mode_hint = tk.Label(
            body, text="", bg=CARD, fg=TEXT_FAINT, font=font(8), anchor="w",
            justify="left", wraplength=self.width - px(46),
        )
        self.mode_hint.pack(fill="x", pady=(px(6), 0))

    # ---- 4. 状态与操作 ----

    def _build_footer(self, parent: tk.Frame) -> None:
        card = Card(parent)
        card.pack(fill="x")
        self.footer_card = card

        self.message_label = tk.Label(
            card, text="", bg=CARD, fg=TEXT_DIM, font=font(9), anchor="w",
            justify="left", wraplength=self.width - px(36),
        )
        self.message_label.pack(fill="x", padx=px(11), pady=(px(8), px(4)))

        row = tk.Frame(card, bg=CARD)
        row.pack(fill="x", padx=px(8), pady=(0, px(8)))
        FlatButton(row, "重新检测", command=self._refresh).pack(side="left", padx=px(3))
        FlatButton(row, "接口自检", command=self.show_self_test).pack(side="left", padx=px(3))
        if self.lock is not None:
            FlatButton(row, "锁定: 开" if self._locked() else "锁定: 关",
                       command=self._toggle_lock).pack(side="left", padx=px(3))
        FlatButton(row, "隐藏", command=self.hide).pack(side="right", padx=px(3))

    # ------------------------------------------------------------------
    # 数据刷新
    # ------------------------------------------------------------------

    def _read_hw(self) -> dict:
        """读硬件状态。

        关键：**独显在不在系统里**才是状态的权威判据。
        GPUEco 寄存器回读在本机型不可信（写 1 后读回仍是 0，但独显确实断电了），
        早期版本就是因为信了它，界面状态全是错的。
        """
        try:
            from .asusprobe import dgpu_device_present, nvidia_present

            present = nvidia_present()
            # 设备是否报错（被断电后驱动没接管时会变成 Problem 状态）
            device_ok = dgpu_device_present()
            mux = self._acpi.get(0x00090016)
            eco_reg = self._acpi.get(0x00090020)
            mode = self._detect_mode(present, mux, eco_reg)
            return {
                "available": eco_reg is not None,
                "eco": eco_reg,
                "mux": mux,
                "present": present,
                "device_problem": device_ok is False,
                "mode": mode,
            }
        except Exception as exc:
            logger.debug("读取硬件状态失败: %s", exc)
            return {
                "available": False, "eco": None, "mux": None,
                "present": None, "device_problem": False, "mode": None,
            }

    @staticmethod
    def _detect_mode(present: Optional[bool], mux: Optional[int], eco_reg: Optional[int]):
        """用"独显在不在"判断模式，寄存器只作辅助。"""
        from .asusprobe import MUX_ULTIMATE, find_mode

        if present is False:
            return find_mode("eco")
        if present is True:
            return find_mode("ultimate") if mux == MUX_ULTIMATE else find_mode("standard")
        # 拿不到硬件信号时退回寄存器语义
        if eco_reg == 1:
            return find_mode("eco")
        if eco_reg is None:
            return None
        return find_mode("ultimate") if mux == MUX_ULTIMATE else find_mode("standard")

    def refresh_static(self) -> None:
        """刷新不随每秒变化的部分：机型、模式高亮。"""
        hw = self._read_hw()
        self._last_hw = hw

        try:
            from .asusprobe import run_self_test  # noqa: F401  仅为确保模块可用
        except Exception:
            pass

        if not self.model_label.cget("text") or self.model_label.cget("text") == "读取中…":
            self.model_label.configure(text=self._model_text())

        mode = hw.get("mode")
        self.mode_big_label.configure(text=mode.label if mode else "接口不可用")
        self.mode_big_label.configure(fg=ACCENT if mode else ERROR)

        # 独显供电 / 显示通路：这两行是"哪块卡在用"的关键信息，必须写出来
        present = hw.get("present")
        problem = hw.get("device_problem")
        mux = hw.get("mux")
        if problem:
            self.power_label.configure(
                text="⚠ 独立显卡：驱动未接管（设备报错，需重启电脑恢复）", fg=ERROR
            )
        elif present is True:
            self.power_label.configure(
                text="● 独立显卡：已通电，随时可用", fg="#76B900"
            )
        elif present is False:
            self.power_label.configure(
                text="○ 独立显卡：已断电（集显模式，最省电）", fg=TEXT_FAINT
            )
        else:
            self.power_label.configure(text="独立显卡：状态未知", fg=WARN)

        mux_text = {
            0: "显示输出：核心显卡",
            1: "显示输出：核显输出 + 独显渲染（标准混合）",
            2: "显示输出：独显直连（性能最高）",
        }.get(mux, "显示输出：未知")
        self.mux_label.configure(text=mux_text)

        for mode_id, button in self._mode_buttons.items():
            button.set_selected(mode is not None and mode.mode_id == mode_id)
            button.set_enabled(hw.get("available", False))

        hint = "点一下即硬件切换，立刻生效、不用重启程序"
        if not hw.get("available"):
            hint = "华硕 ACPI 接口不可用：需要 ASUS System Control Interface 驱动"
        elif mode is not None and mode.needs_reboot:
            hint = "当前是独显直连：切回其它模式需要重启才能完全生效"
        self.mode_hint.configure(text=hint)

        self._render_message()

    def _model_text(self) -> str:
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS"
            ) as key:
                model = str(winreg.QueryValueEx(key, "SystemProductName")[0])
            # 厂商常把型号写成 "TX Gaming FX608LM_FX608LM"，去掉重复段
            model = model.replace("_", " ").strip()
            words = model.split()
            deduped: List[str] = []
            for word in words:
                if word not in deduped:
                    deduped.append(word)
            model = " ".join(deduped)
            if model.upper().startswith("ASUS"):
                return model
            return f"ASUS {model}"
        except Exception:
            return "ASUS 笔记本"

    def tick(self) -> None:
        """每秒刷新占用数据。"""
        self.drain_messages()

        adapters = self.adapters_provider() or []
        try:
            self.collector.apply_to_adapters(adapters)
        except Exception as exc:
            logger.debug("指标刷新失败: %s", exc)

        system = self.collector.system()
        nvidia = self.collector.nvidia_sample()

        # --- 独显：优先用 nvidia-smi（设备级占用，最贴近任务管理器）---
        dgpu = next((a for a in adapters if a.kind == TYPE_DISCRETE), None)
        if nvidia:
            sample = next(iter(nvidia.values()))
            d_util = sample.util_percent
            vram_used = sample.vram_used
            vram_total = sample.vram_total
        elif dgpu is not None:
            d_util = dgpu.util_percent
            vram_used = dgpu.vram_used
            vram_total = dgpu.dedicated_vram
        else:
            d_util, vram_used, vram_total = 0.0, 0, 0

        # --- 核显：PDH 里属于核显 LUID 的最大引擎占用 ---
        igpu = next((a for a in adapters if a.kind == TYPE_INTEGRATED), None)
        i_util = igpu.util_percent if igpu is not None else 0.0

        hw = self._last_hw or {}
        powered = hw.get("present")
        if powered is None:
            powered = hw.get("eco") == ECO_ON
        dgpu_state = "已通电" if powered else "已断电"
        self.dgpu_bar.set(
            d_util if powered else 0.0,
            f"{d_util:.0f}% · {dgpu_state}" + ("" if powered else "（现在无法使用）"),
        )
        self.igpu_bar.set(i_util, f"{i_util:.0f}% · 负责输出画面")

        if vram_total:
            self.vram_bar.set(
                vram_used * 100.0 / vram_total,
                f"{format_bytes(vram_used)} / {format_bytes(vram_total)}",
            )
        else:
            self.vram_bar.set(0.0, format_bytes(vram_used) if vram_used else "—")

        # 温度：CPU 温度走华硕 ACPI，独显温度多数机型不支持
        cpu_temp = self._acpi.get(0x00120094)
        gpu_temp = self._acpi.get(0x00120097)
        temp_text = []
        if cpu_temp:
            temp_text.append(f"CPU {cpu_temp}℃")
        if gpu_temp:
            temp_text.append(f"GPU {gpu_temp}℃")
        temp_text.append(f"CPU 负载 {system.cpu_percent:.0f}%")
        self.temp_bar.set(
            min(100.0, (cpu_temp or 0) * 100.0 / 105.0),
            " · ".join(temp_text),
        )

        self.ram_bar.set(system.ram_percent, system.ram_label)

        # 硬件状态每 3 秒复查一次（外部工具可能改了模式）
        self._hw_tick += 1
        if self._hw_tick % 3 == 0:
            previous = (self._last_hw or {}).get("mode")
            self.refresh_static()
            current = (self._last_hw or {}).get("mode")
            if previous is not None and current is not None and previous.mode_id != current.mode_id:
                self.set_message(
                    f"检测到显卡模式被其它程序改成了「{current.label}」"
                    + ("（锁定已开启，会自动抢回）" if self._locked() else ""),
                    "warn",
                )

    # ------------------------------------------------------------------
    # 消息 / 自检 / 显隐
    # ------------------------------------------------------------------

    def set_message(self, text: str, kind: str = "info") -> None:
        self._message = text
        self._message_kind = kind
        self._render_message()

    def queue_message(self, text: str, kind: str = "info") -> None:
        self._pending.append((text, kind))

    def drain_messages(self) -> None:
        while self._pending:
            text, kind = self._pending.pop(0)
            self.set_message(text, kind)

    def _render_message(self) -> None:
        colors = {"info": TEXT_DIM, "ok": OK, "warn": WARN, "error": ERROR}
        if not self._message:
            hw = self._last_hw or {}
            eco = hw.get("eco")
            if eco == ECO_ON:
                text = "独显已通电可用。需要独显渲染的程序现在就能用上它"
            elif eco == ECO_OFF:
                text = "独显已断电省电。想用独显请点「标准模式」"
            else:
                text = "按快捷键可随时呼出/收起本面板"
            self.message_label.configure(text=text, fg=TEXT_FAINT)
            return
        self.message_label.configure(
            text=self._message, fg=colors.get(self._message_kind, TEXT_DIM)
        )

    def show_self_test(self) -> None:
        """在界面上展示接口自检结果（不写任何状态）。"""
        try:
            from .asusprobe import run_self_test

            report = run_self_test()
        except Exception as exc:
            self.set_message(f"自检失败：{exc}", "error")
            return

        lines = []
        for check in report.checks:
            if check.value == "不支持":
                continue
            lines.append(f"{check.mark} {check.name}: {check.value}")
        summary = "；".join(lines[:8])
        if len(lines) > 8:
            summary += f" …共 {len(lines)} 项"
        self.set_message(
            f"接口自检{'通过' if report.passed else '有异常'} → {summary}", 
            "ok" if report.passed else "warn",
        )
        logger.info("界面自检:\n%s", report.text())

    def _locked(self) -> bool:
        try:
            return bool(self.lock and self.lock.record.enabled)
        except Exception:
            return False

    def _toggle_lock(self) -> None:
        if self.lock is None:
            return
        try:
            if self._locked():
                self.lock.disable()
                self.set_message("已解除锁定", "info")
            else:
                hw = self._last_hw or {}
                mode = hw.get("mode")
                if mode is None:
                    self.set_message("无法锁定：读不到当前模式", "warn")
                    return
                result = self.lock.enable(mode, source="panel-lock")
                if result.ok:
                    self.lock.start(float(self.config.get("lock_interval", 4.0) or 4.0))
                    self.set_message(
                        f"已锁定「{mode.label}」：被其它工具改回去会自动抢回", "ok"
                    )
                else:
                    self.set_message(f"锁定失败：{result.message}", "error")
        except Exception as exc:
            self.set_message(f"锁定操作失败：{exc}", "error")
        # 刷新按钮文字
        for child in self.footer_card.winfo_children():
            for button in child.winfo_children():
                if isinstance(button, FlatButton) and str(button.cget("text")).startswith("锁定:"):
                    button.configure(text="锁定: 开" if self._locked() else "锁定: 关")
        self.refresh_static()

    def _refresh(self) -> None:
        try:
            if self.on_refresh:
                self.on_refresh()
            self.refresh_static()
            self.set_message("已重新检测", "ok")
        except Exception as exc:
            self.set_message(f"重新检测失败：{exc}", "error")

    def show(self) -> None:
        self.refresh_static()
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.attributes("-topmost", True)
            self.root.focus_force()
        except Exception:
            pass
        self.tick()

    def hide(self) -> None:
        self.root.withdraw()

    def toggle(self) -> None:
        if self.root.state() == "normal":
            self.hide()
        else:
            self.show()

    # 兼容旧接口命名
    @property
    def adapters(self) -> List[GpuAdapter]:
        return self.adapters_provider() or []

    def refresh_hardware(self) -> None:
        self.refresh_static()
