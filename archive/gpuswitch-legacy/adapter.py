
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

TYPE_INTEGRATED = "integrated"
TYPE_DISCRETE = "discrete"
TYPE_VIRTUAL = "virtual"
TYPE_UNKNOWN = "unknown"

TYPE_LABELS = {
    TYPE_INTEGRATED: "集显（核显）",
    TYPE_DISCRETE: "独显",
    TYPE_VIRTUAL: "虚拟显卡",
    TYPE_UNKNOWN: "未知",
}

PREF_POWER_SAVING = 1
PREF_HIGH_PERFORMANCE = 2


@dataclass
class GpuAdapter:

    name: str
    vendor_id: int = 0
    device_id: int = 0
    luid: int = 0
    kind: str = TYPE_UNKNOWN
    dedicated_vram: int = 0
    shared_memory: int = 0
    drives_display: bool = False
    source: str = ""
    preference: Optional[int] = None
    util_percent: float = 0.0
    vram_used: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def kind_label(self) -> str:
        return TYPE_LABELS.get(self.kind, TYPE_LABELS[TYPE_UNKNOWN])

    @property
    def vendor_name(self) -> str:
        return {
            0x10DE: "NVIDIA",
            0x8086: "Intel",
            0x1002: "AMD",
            0x1022: "AMD",
            0x5143: "Qualcomm",
            0x1AE0: "Google",
            0x1414: "Microsoft",
        }.get(self.vendor_id, f"厂商 0x{self.vendor_id:04X}" if self.vendor_id else "未知厂商")

    @property
    def vram_label(self) -> str:
        if self.dedicated_vram > 0:
            return _fmt_bytes(self.dedicated_vram)
        return "共享内存"

    @property
    def key(self) -> str:
        return f"{self.vendor_id:04X}:{self.device_id:04X}:{self.luid:#x}"

    @property
    def is_switchable(self) -> bool:
        return self.kind in (TYPE_INTEGRATED, TYPE_DISCRETE, TYPE_UNKNOWN)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "vendor_id": self.vendor_id,
            "device_id": self.device_id,
            "luid": self.luid,
            "kind": self.kind,
            "kind_label": self.kind_label,
            "vendor": self.vendor_name,
            "dedicated_vram": self.dedicated_vram,
            "shared_memory": self.shared_memory,
            "drives_display": self.drives_display,
            "source": self.source,
            "preference": self.preference,
            "extra": self.extra,
        }

    def __str__(self) -> str:
        return f"<GpuAdapter {self.name} [{self.kind_label}] luid={self.luid:#x}>"


def _fmt_bytes(value: int) -> str:
    step = 1024.0
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < step:
            if unit in ("B", "KB"):
                return f"{size:.0f} {unit}"
            return f"{size:.1f} {unit}"
        size /= step
    return f"{size:.1f} PB"


def format_bytes(value: int) -> str:
    return _fmt_bytes(value)


def classify(vendor_id: int, name: str, dedicated_vram: int) -> str:
    lowered = (name or "").lower()

    virtual_hints = (
        "virtual", "remote display", "basic display", "basic render",
        "indirect display", "idd ", "parsec", "sunshine", "usb display",
        "displaylink", "mumu", "meta virtual", "oray", "todesk", "gameloop",
        "warp", "software adapter", "llvmpipe", "swiftshader",
    )
    if any(hint in lowered for hint in virtual_hints):
        return TYPE_VIRTUAL
    if vendor_id == 0x1414:
        return TYPE_VIRTUAL

    integrated_hints = (
        "iris", "uhd graphics", "hd graphics", "graphics 610", "graphics 620",
        "graphics 630", "integrated", "vega", "radeon graphics", "apu",
        "apple m1", "apple m2", "apple m3", "apple m4", "adreno", "mali",
        "qualcomm", "snapdragon",
    )
    if any(hint in lowered for hint in integrated_hints):
        if not any(hint in lowered for hint in ("rx ", "pro w", "pro v", "instinct")):
            return TYPE_INTEGRATED

    discrete_hints = (
        "geforce", "quadro", "rtx", "gtx", "radeon rx", "radeon pro",
        "arc a", "arc b", "tesla", "instinct", "nvidia",
    )
    if any(hint in lowered for hint in discrete_hints):
        return TYPE_DISCRETE

    if vendor_id == 0x8086 and "arc" not in lowered:
        return TYPE_INTEGRATED

    if dedicated_vram >= 512 * 1024 * 1024:
        return TYPE_DISCRETE
    if vendor_id in (0x10DE, 0x1002) and dedicated_vram > 0:
        return TYPE_DISCRETE

    return TYPE_UNKNOWN


def sort_adapters(adapters: List[GpuAdapter]) -> List[GpuAdapter]:
    order = {TYPE_DISCRETE: 0, TYPE_INTEGRATED: 1, TYPE_UNKNOWN: 2, TYPE_VIRTUAL: 3}
    return sorted(
        adapters,
        key=lambda a: (
            order.get(a.kind, 9),
            0 if a.drives_display else 1,
            -a.dedicated_vram,
            a.name,
        ),
    )
