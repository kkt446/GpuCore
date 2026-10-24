
from __future__ import annotations

import ctypes
import tkinter as tk
from typing import Callable, Dict, List, Optional

from . import log
from .adapter import (
    TYPE_DISCRETE,
    TYPE_INTEGRATED,
    TYPE_VIRTUAL,
    GpuAdapter,
    format_bytes,
)
from .metrics import MetricsCollector
from .paths import is_windows

logger = log.get(__name__)

BG = "#191919"
CARD = "#202020"
CARD_BORDER = "#2B2B2B"
TEXT = "#F2F2F2"
TEXT_DIM = "#B4B4B4"
TEXT_FAINT = "#8A8A8A"
ACCENT = "#00A3EE"
ACCENT_SOFT = "#1E6E96"
BTN_BG = "#2A2A2A"
BTN_BG_HOVER = "#343434"
BTN_BG_DISABLED = "#242424"
BTN_BORDER = "#3A3A3A"
BAR_TRACK = "#2E2E2E"
BAR_GPU = "#00A3EE"
BAR_CPU = "#38B26B"
BAR_RAM = "#E0A32E"
WARN = "#E0A32E"
ERROR = "#E5534B"
OK = "#38B26B"

FONT_FAMILY = "Microsoft YaHei UI"

_scale = 1.0


def prepare_dpi() -> float:
    global _scale
    if is_windows():
        try:
            user32 = ctypes.WinDLL("user32")
            try:
                user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            except Exception:
                try:
                    ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
                except Exception:
                    pass
            dpi = user32.GetDpiForSystem()
            if dpi:
                _scale = dpi / 96.0
        except Exception:
            _scale = 1.0
    return _scale


def px(value: float) -> int:
    return int(round(value * _scale))


def font(size: int, bold: bool = False) -> tuple:
    return (FONT_FAMILY, -px(size), "bold" if bold else "normal")

class Card(tk.Frame):

    def __init__(self, master, title: str = "", **kwargs):
        super().__init__(master, bg=CARD, highlightbackground=CARD_BORDER,
                         highlightthickness=1, bd=0, **kwargs)
        self.body = self
        self._title_row = None
        if title:
            self._title_row = tk.Frame(self, bg=CARD)
            self._title_row.pack(fill="x", padx=px(12), pady=(px(9), px(2)))
            self.title_label = tk.Label(
                self._title_row, text=title, bg=CARD, fg=TEXT, font=font(11, True),
                anchor="w",
            )
            self.title_label.pack(side="left")
            self.value_label = tk.Label(
                self._title_row, text="", bg=CARD, fg=TEXT_DIM, font=font(10), anchor="e"
            )
            self.value_label.pack(side="right")

    def set_value(self, text: str) -> None:
        if self._title_row is not None:
            self.value_label.configure(text=text)


class Bar(tk.Frame):

    def __init__(self, master, label: str, color: str, width: int = 100):
        super().__init__(master, bg=CARD)
        self.color = color
        self.bar_width = px(width)
        self.bar_height = px(7)

        top = tk.Frame(self, bg=CARD)
        top.pack(fill="x")
        tk.Label(top, text=label, bg=CARD, fg=TEXT_DIM, font=font(10, True), anchor="w").pack(
            side="left"
        )
        self.value = tk.Label(top, text="--", bg=CARD, fg=TEXT, font=font(10), anchor="e")
        self.value.pack(side="right")

        self.canvas = tk.Canvas(
            self, width=self.bar_width, height=self.bar_height, bg=CARD,
            highlightthickness=0, bd=0,
        )
        self.canvas.pack(fill="x", pady=(px(4), 0))
        self._percent = 0.0
        self._draw()

    def _round_rect(self, x1, y1, x2, y2, radius, **kwargs):
        radius = max(1, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
        points = [
            x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
            x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
            x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
        ]
        return self.canvas.create_polygon(points, smooth=True, **kwargs)

    def _draw(self) -> None:
        self.canvas.delete("all")
        width = max(self.canvas.winfo_width(), self.bar_width)
        height = self.bar_height
        self._round_rect(0, 0, width, height, height / 2, fill=BAR_TRACK, outline="")
        fill_width = max(0.0, min(1.0, self._percent / 100.0)) * width
        if fill_width >= 1:
            self._round_rect(0, 0, max(fill_width, height), height, height / 2,
                             fill=self.color, outline="")

    def set(self, percent: float, text: str = "") -> None:
        self._percent = max(0.0, min(100.0, float(percent)))
        if text:
            self.value.configure(text=text)
        self._draw()


class ToggleButton(tk.Frame):

    def __init__(
        self,
        master,
        text: str,
        subtext: str = "",
        command: Optional[Callable[[], None]] = None,
        height: int = 58,
    ):
        super().__init__(
            master, bg=BTN_BORDER, highlightthickness=0, bd=0, cursor="hand2",
        )
        self.command = command
        self._selected = False
        self._enabled = True
        self._base_subtext = subtext

        self._inner = tk.Frame(self, bg=BTN_BG, bd=0, highlightthickness=0)
        self._inner.pack(fill="both", expand=True, padx=1, pady=1)

        self.title = tk.Label(self._inner, text=text, bg=BTN_BG, fg=TEXT, font=font(10, True))
        self.title.pack(pady=(px(8), 0))
        self.sub = tk.Label(self._inner, text=subtext, bg=BTN_BG, fg=TEXT_DIM, font=font(8))
        self.sub.pack(pady=(px(1), px(7)))

        for widget in (self, self._inner, self.title, self.sub):
            widget.bind("<Button-1>", self._on_click)
            widget.bind("<Enter>", self._on_enter)
            widget.bind("<Leave>", self._on_leave)
        self.configure(height=px(height))
        self.pack_propagate(False)

    def _on_click(self, _event=None):
        if self._enabled and self.command:
            self.command()

    def _on_enter(self, _event=None):
        if self._enabled and not self._selected:
            self._paint(BTN_BG_HOVER, BTN_BORDER)

    def _on_leave(self, _event=None):
        if self._enabled and not self._selected:
            self._paint(BTN_BG, BTN_BORDER)

    def _paint(self, bg: str, border: str, fg: str = TEXT, sub_fg: str = TEXT_DIM) -> None:
        self.configure(bg=border)
        self._inner.configure(bg=bg)
        self.title.configure(bg=bg, fg=fg)
        self.sub.configure(bg=bg, fg=sub_fg)

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        if selected:
            self._paint(BTN_BG, ACCENT, TEXT, ACCENT)
        else:
            self._paint(BTN_BG, BTN_BORDER, TEXT, TEXT_DIM)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        if enabled:
            self.configure(cursor="hand2")
            self.sub.configure(text=self._base_subtext)
            if self._selected:
                self._paint(BTN_BG, ACCENT, TEXT, ACCENT)
            else:
                self._paint(BTN_BG, BTN_BORDER, TEXT, TEXT_DIM)
        else:
            self.configure(cursor="arrow")
            self._paint(BTN_BG_DISABLED, BTN_BORDER, TEXT_FAINT, "#6A6A6A")
            self.sub.configure(text="使用中")

    def set_subtext(self, text: str) -> None:
        self._base_subtext = text
        if self._enabled:
            self.sub.configure(text=text)


class FlatButton(tk.Label):

    def __init__(self, master, text: str, command=None, bg: str = BTN_BG, fg: str = TEXT_DIM):
        super().__init__(
            master, text=text, bg=bg, fg=fg, font=font(9), padx=px(10), pady=px(4),
            cursor="hand2",
        )
        self.command = command
        self._base_bg = bg
        self.bind("<Button-1>", lambda _e: command() if command else None)
        self.bind("<Enter>", lambda _e: self.configure(bg=BTN_BG_HOVER))
        self.bind("<Leave>", lambda _e: self.configure(bg=self._base_bg))

class GpuPanel:

    def __init__(
        self,
        root: tk.Tk,
        adapters: List[GpuAdapter],
        collector: MetricsCollector,
        on_switch: Callable[[GpuAdapter], None],
        config: Dict,
        on_refresh: Optional[Callable[[], List[GpuAdapter]]] = None,
        on_open_settings: Optional[Callable[[], None]] = None,
        lock=None,
        on_lock_toggle: Optional[Callable[[object], None]] = None,
    ):
        self.root = root
        self.adapters = adapters
        self.collector = collector
        self.on_switch = on_switch
        self.on_refresh = on_refresh
        self.on_open_settings = on_open_settings
        self.lock = lock
        self.on_lock_toggle = on_lock_toggle
        self.config = config or {}
        self._lock_interval = float((config or {}).get("lock_interval", 4.0) or 4.0)

        self.width = px(int(self.config.get("window_width", 470)))
        self.height = px(int(self.config.get("window_height", 660)))
        self._buttons: Dict[str, ToggleButton] = {}
        self._current_key: Optional[str] = None
        self._message = ""
        self._message_kind = "info"
        self._pending: List[tuple] = []


    def build(self) -> None:
        root = self.root
        root.title("显卡切换工具")
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
        self._build_hardware(wrap)
        self._build_footer(wrap)

        self.refresh_static()
        self.refresh_hardware()

        root.update_idletasks()
        natural = wrap.winfo_reqheight() + px(16)
        configured = px(int(self.config.get("window_height", 0) or 0))
        self.height = max(px(300), min(natural, configured) if configured else natural)
        root.geometry(f"{self.width}x{self.height}")
        self._center()

        self.tick()

    def _center(self) -> None:
        self.root.update_idletasks()
        position = str(self.config.get("window_position", "center"))
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        if position == "cursor":
            x, y = self.root.winfo_pointerx() - self.width // 2, self.root.winfo_pointery() - px(30)
        elif position == "top-right":
            x, y = screen_w - self.width - px(24), px(24)
        else:
            x, y = (screen_w - self.width) // 2, max(px(20), (screen_h - self.height) // 2 - px(30))
        x = max(0, min(x, screen_w - self.width))
        y = max(0, min(y, screen_h - self.height))
        self.root.geometry(f"{self.width}x{self.height}+{x}+{y}")


    def _build_header(self, parent: tk.Frame) -> None:
        card = Card(parent)
        card.pack(fill="x", pady=(0, px(8)))

        row = tk.Frame(card, bg=CARD)
        row.pack(fill="x", padx=px(12), pady=px(10))

        left = tk.Frame(row, bg=CARD)
        left.pack(side="left", fill="x", expand=True)

        self.title_label = tk.Label(
            left, text="当前显卡", bg=CARD, fg=TEXT_DIM, font=font(9), anchor="w"
        )
        self.title_label.pack(anchor="w")
        self.gpu_name_label = tk.Label(
            left, text="检测中…", bg=CARD, fg=TEXT, font=font(13, True), anchor="w",
            justify="left", wraplength=self.width - px(150),
        )
        self.gpu_name_label.pack(anchor="w", pady=(px(2), 0))
        self.gpu_type_label = tk.Label(
            left, text="", bg=CARD, fg=ACCENT, font=font(10), anchor="w"
        )
        self.gpu_type_label.pack(anchor="w", pady=(px(3), 0))

        self.header_right = tk.Frame(row, bg=CARD)
        self.header_right.pack(side="right", anchor="n")
        self.gpu_util_label = tk.Label(
            self.header_right, text="--", bg=CARD, fg=TEXT, font=font(10, True), anchor="e"
        )
        self.gpu_util_label.pack(anchor="e")


    def _build_usage(self, parent: tk.Frame) -> None:
        card = Card(parent, "实时占用")
        card.pack(fill="x", pady=(0, px(8)))
        self.usage_card = card

        body = tk.Frame(card, bg=CARD)
        body.pack(fill="x", padx=px(12), pady=(px(6), px(11)))

        self.gpu_bar = Bar(body, "GPU 占用", BAR_GPU, width=300)
        self.gpu_bar.pack(fill="x", pady=(px(5), px(7)))
        self.cpu_bar = Bar(body, "CPU 占用", BAR_CPU, width=300)
        self.cpu_bar.pack(fill="x", pady=(0, px(7)))
        self.ram_bar = Bar(body, "内存占用", BAR_RAM, width=300)
        self.ram_bar.pack(fill="x")


    def _build_modes(self, parent: tk.Frame) -> None:
        card = Card(parent, "显卡模式")
        card.pack(fill="x", pady=(0, px(8)))
        self.modes_card = card

        self.modes_body = tk.Frame(card, bg=CARD)
        self.modes_body.pack(fill="x", padx=px(12), pady=(px(8), px(11)))
        self._mode_grid = tk.Frame(self.modes_body, bg=CARD)
        self._mode_grid.pack(fill="x")
        self.rebuild_mode_buttons()

        self.mode_hint_label = tk.Label(
            self.modes_body, text=self._mode_hint(), bg=CARD, fg=TEXT_FAINT,
            font=font(8), anchor="w", justify="left", wraplength=self.width - px(50),
        )
        self.mode_hint_label.pack(fill="x", pady=(px(7), 0))

    def rebuild_mode_buttons(self) -> None:
        for child in self._mode_grid.winfo_children():
            child.destroy()
        self._buttons.clear()

        columns = 3
        switchable = [a for a in self.adapters if a.kind != TYPE_VIRTUAL]
        for index, adapter in enumerate(switchable):
            row, column = divmod(index, columns)
            button = ToggleButton(
                self._mode_grid,
                text=self._short_name(adapter),
                subtext=self._short_type(adapter),
                command=lambda a=adapter: self.on_switch(a),
            )
            button.grid(row=row, column=column, sticky="nsew", padx=px(3), pady=px(3))
            self._buttons[adapter.key] = button
        for column in range(columns):
            self._mode_grid.grid_columnconfigure(column, weight=1, uniform="mode")

    @staticmethod
    def _short_name(adapter: GpuAdapter) -> str:
        name = adapter.name
        for noise in (
            "NVIDIA ", "AMD ", "Intel(R) ", "Laptop GPU", "GPU", "(R)", "(TM)",
        ):
            name = name.replace(noise, "")
        name = " ".join(name.split())
        if adapter.vendor_id == 0x10DE and "RTX" in name.upper():
            parts = name.split()
            for part in parts:
                if part.upper().startswith(("RTX", "GTX")):
                    return part.upper()
        if len(name) > 16:
            name = name[:15] + "…"
        return name or adapter.vendor_name

    @staticmethod
    def _short_type(adapter: GpuAdapter) -> str:
        if adapter.kind == TYPE_INTEGRATED:
            return "集显 · 省电"
        if adapter.kind == TYPE_DISCRETE:
            return "独显 · 性能"
        return adapter.kind_label

    def _hardware_available(self) -> bool:
        try:
            return bool(self.lock is not None and self.lock.backend.available())
        except Exception:
            return False

    def _mode_hint(self) -> str:
        if self._hardware_available():
            return "点一下即热切换：硬件级切换显卡供电/显示通路，立刻生效、不用重启程序"
        return "本机无厂商硬件通道：只能改显卡偏好，对被点的程序在下次启动时生效"


    def _build_hardware(self, parent: tk.Frame) -> None:
        if self.lock is None:
            return
        card = Card(parent, "锁定显卡选择")
        card.pack(fill="x", pady=(0, px(8)))
        self.hardware_card = card

        body = tk.Frame(card, bg=CARD)
        body.pack(fill="x", padx=px(12), pady=(px(8), px(11)))

        self.hw_state_label = tk.Label(
            body, text="正在读取硬件状态…", bg=CARD, fg=TEXT_DIM, font=font(9),
            anchor="w", justify="left", wraplength=self.width - px(50),
        )
        self.hw_state_label.pack(fill="x", pady=(0, px(7)))

        self._lock_grid = tk.Frame(body, bg=CARD)
        self._lock_grid.pack(fill="x")
        self._lock_buttons: Dict[str, ToggleButton] = {}
        self.rebuild_lock_buttons()

        self.hw_hint_label = tk.Label(
            body, text="", bg=CARD, fg=TEXT_FAINT, font=font(8),
            anchor="w", justify="left", wraplength=self.width - px(50),
        )
        self.hw_hint_label.pack(fill="x", pady=(px(7), 0))

    def rebuild_lock_buttons(self) -> None:
        if self.lock is None:
            return
        for child in self._lock_grid.winfo_children():
            child.destroy()
        self._lock_buttons.clear()

        try:
            modes = self.lock.backend.modes()
        except Exception as exc:
            logger.debug("读取硬件模式失败: %s", exc)
            modes = []

        if not modes:
            tk.Label(
                self._lock_grid,
                text="本机没有可用的厂商硬件通道，已切换为「注册表偏好 + 守护」模式",
                bg=CARD, fg=TEXT_FAINT, font=font(9), anchor="w", justify="left",
                wraplength=self.width - px(50),
            ).pack(fill="x")
            return

        columns = 2
        for index, mode in enumerate(modes):
            row, column = divmod(index, columns)
            hints = []
            if mode.needs_reboot:
                hints.append("需重启")
            if mode.may_blackout:
                hints.append("会黑屏")
            button = ToggleButton(
                self._lock_grid,
                text=mode.label,
                subtext=" · ".join(hints) if hints else "点击锁定",
                command=lambda m=mode: self.on_lock_toggle(m) if self.on_lock_toggle else None,
                height=52,
            )
            button.grid(row=row, column=column, sticky="nsew", padx=px(3), pady=px(3))
            self._lock_buttons[mode.mode_id] = button
        for column in range(columns):
            self._lock_grid.grid_columnconfigure(column, weight=1, uniform="lock")

    def refresh_hardware(self) -> None:
        if self.lock is None or not hasattr(self, "hw_state_label"):
            return
        try:
            state = self.lock.backend.read_state()
        except Exception as exc:
            self.hw_state_label.configure(text=f"读取硬件状态失败：{exc}", fg=WARN)
            return

        if not state.available:
            self.hw_state_label.configure(
                text="本机没有厂商 ACPI 显卡通道（非 ASUS 机型属于正常情况）",
                fg=TEXT_FAINT,
            )
            self.hw_hint_label.configure(
                text="程序仍会锁定 Windows 图形首选项，并对新启动的程序生效"
            )
            return

        status = self.lock.status()
        locked = bool(status.get("enabled"))
        mode_id = str(status.get("mode_id") or "")

        text = state.detail
        if locked:
            text = f"🔒 已锁定 · {text}"
        self.hw_state_label.configure(text=text, fg=(ACCENT if locked else TEXT_DIM))

        highlight = mode_id if locked else state.mode_id
        for mid, button in self._lock_buttons.items():
            button.set_selected(mid == highlight)
            button.set_enabled(True)

        restores = int(status.get("restore_count") or 0)
        drifts = int(status.get("drift_count") or 0)
        if locked:
            hint = f"守护中：每 {self._lock_interval:.0f} 秒检查一次"
            if drifts:
                hint += f" · 已被改动 {drifts} 次、抢回 {restores} 次"
            else:
                hint += " · 尚未检测到被改动"
            self.hw_hint_label.configure(text=hint, fg=TEXT_DIM)
        else:
            self.hw_hint_label.configure(
                text="点上面的模式即可锁定，被其它工具改回去时会自动抢回来",
                fg=TEXT_FAINT,
            )

        last = status.get("last_event")
        if last is not None and getattr(last, "kind", "") in ("drift", "restored", "failed"):
            self.set_message(last.message, "warn" if last.kind != "restored" else "ok")


    def _build_footer(self, parent: tk.Frame) -> None:
        card = Card(parent)
        card.pack(fill="x")
        self.footer_card = card

        self.message_label = tk.Label(
            card, text="", bg=CARD, fg=TEXT_DIM, font=font(9), anchor="w",
            justify="left", wraplength=self.width - px(40),
        )
        self.message_label.pack(fill="x", padx=px(12), pady=(px(9), px(4)))

        row = tk.Frame(card, bg=CARD)
        row.pack(fill="x", padx=px(9), pady=(0, px(9)))
        FlatButton(row, "重新检测显卡", command=self.reload_adapters).pack(side="left", padx=px(3))
        if self.on_open_settings:
            FlatButton(row, "Windows 图形设置", command=self.on_open_settings).pack(
                side="left", padx=px(3)
            )
        FlatButton(row, "隐藏", command=self.hide).pack(side="right", padx=px(3))


    def reload_adapters(self) -> None:
        if not self.on_refresh:
            return
        try:
            adapters = self.on_refresh() or []
            if adapters:
                self.adapters = adapters
                self.rebuild_mode_buttons()
                self.refresh_static()
                self.set_message("已重新检测显卡", "ok")
        except Exception as exc:
            self.set_message(f"重新检测失败：{exc}", "error")

    def refresh_static(self) -> None:
        from .gpu import describe_renderer

        current = describe_renderer(self.adapters)
        self._current_key = current.key if current else None

        hardware_active: Optional[str] = None
        if self._hardware_available():
            try:
                state = self.lock.backend.read_state()
                if state.available:
                    hardware_active = "dgpu_off" if state.eco == 1 else "dgpu_on"
            except Exception:
                hardware_active = None

        if current:
            self.gpu_name_label.configure(text=current.name)
            type_text = current.kind_label
            if current.kind == TYPE_DISCRETE and hardware_active is not None:
                type_text = (
                    "独显 · 已通电（标准混合）"
                    if hardware_active == "dgpu_on"
                    else "独显 · 已断电（省电中）"
                )
            elif current.kind == TYPE_INTEGRATED:
                if hardware_active == "dgpu_off":
                    type_text = "集显（核显）· 独显已关闭"
                elif hardware_active == "dgpu_on":
                    type_text = "集显（核显）· 画面输出中"
            self.gpu_type_label.configure(text=type_text)
            self.title_label.configure(text="当前使用的显卡")
        else:
            self.gpu_name_label.configure(text="未检测到显卡")
            self.gpu_type_label.configure(text="")

        for adapter in self.adapters:
            button = self._buttons.get(adapter.key)
            if button is None:
                continue
            button.set_selected(adapter.key == self._current_key)
            button.set_enabled(True)

        if hasattr(self, "mode_hint_label"):
            self.mode_hint_label.configure(text=self._mode_hint())

        if self.config.get("show_total_usage", True):
            display = [a for a in self.adapters if a.drives_display and a.kind != TYPE_VIRTUAL]
            if display:
                names = "、".join(a.name for a in display)
                self.usage_card.set_value(f"输出到屏幕：{names}" if len(names) < 46 else "输出到屏幕")
            else:
                self.usage_card.set_value("")

        switchable = [a for a in self.adapters if a.kind != TYPE_VIRTUAL]
        if len(switchable) <= 1 and not self._message:
            self._message = (
                "只检测到一块显卡，没有可切换的其它显卡"
                if switchable
                else "没有检测到可用显卡"
            )
            self._message_kind = "warn" if switchable else "error"

        self._render_message()

    def tick(self) -> None:
        self.drain_messages()
        try:
            self.collector.apply_to_adapters(self.adapters)
            system = self.collector.system()

            current = next(
                (a for a in self.adapters if a.key == self._current_key), None
            )
            if current is None:
                from .gpu import describe_renderer

                current = describe_renderer(self.adapters)
                self._current_key = current.key if current else None

            if current is not None:
                vram_text = ""
                if current.vram_used:
                    vram_text = f" 显存 {format_bytes(current.vram_used)}"
                if current.kind == TYPE_VIRTUAL:
                    self.gpu_util_label.configure(text="")
                else:
                    self.gpu_util_label.configure(
                        text=f"GPU {current.util_percent:.0f}%{vram_text}"
                    )
                self.gpu_bar.set(
                    current.util_percent,
                    f"{current.util_percent:.0f}%" + (
                        f" · 显存 {format_bytes(current.vram_used)} / {format_bytes(current.dedicated_vram)}"
                        if current.dedicated_vram else ""
                    ),
                )
            else:
                self.gpu_bar.set(0, "--")

            self.cpu_bar.set(system.cpu_percent, f"{system.cpu_percent:.0f}%")
            self.ram_bar.set(system.ram_percent, system.ram_label)
        except Exception as exc:
            logger.debug("面板刷新异常: %s", exc)

        self._hw_tick = getattr(self, "_hw_tick", 0) + 1
        if self._hw_tick % 3 == 0:
            try:
                self.refresh_hardware()
                self.refresh_static()
            except Exception as exc:
                logger.debug("硬件状态刷新异常: %s", exc)


    def set_message(self, text: str, kind: str = "info") -> None:
        self._message = text
        self._message_kind = kind
        self._render_message()

    def queue_message(self, text: str, kind: str = "info") -> None:
        self._pending.append((text, kind))

    def drain_messages(self) -> None:
        if not self._pending:
            return
        while self._pending:
            text, kind = self._pending.pop(0)
            self.set_message(text, kind)
        try:
            self.refresh_hardware()
        except Exception:
            pass

    def _render_message(self) -> None:
        colors = {"info": TEXT_DIM, "ok": OK, "warn": WARN, "error": ERROR}
        color = colors.get(self._message_kind, TEXT_DIM)
        if not self._message:
            self.message_label.configure(
                text="按快捷键可随时呼出/收起本面板", fg=TEXT_FAINT
            )
            return
        self.message_label.configure(text=self._message, fg=color)


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

    def destroy(self) -> None:
        try:
            self.root.destroy()
        except Exception:
            pass
