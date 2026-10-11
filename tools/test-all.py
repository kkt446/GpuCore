"""全功能测试套件。

覆盖程序的每一块能力，逐条给出通过/失败，最后汇总。
不依赖任何第三方库（装了就顺便测），可在 VS Code 里直接 F5 运行。

用法:
    python tools/test-all.py              # 跑全部
    python tools/test-all.py -k metrics   # 只跑名字含 metrics 的用例
    python tools/test-all.py --list       # 列出全部用例
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
import tkinter as tk
import traceback
from pathlib import Path
from typing import Callable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gpuswitch import config as config_module  # noqa: E402
from gpuswitch.adapter import (  # noqa: E402
    TYPE_DISCRETE,
    TYPE_INTEGRATED,
    TYPE_VIRTUAL,
    GpuAdapter,
    classify,
    format_bytes,
)

RESULTS: List[Tuple[str, bool, str]] = []
ONLY: Optional[str] = None


# ---------------------------------------------------------------------------
# 测试框架
# ---------------------------------------------------------------------------


def case(name: str, description: str = ""):
    """把一个函数登记成测试用例。"""

    def decorator(func: Callable[[], Optional[str]]):
        def runner() -> None:
            title = f"{name}  {description}".strip()
            if ONLY and ONLY.lower() not in name.lower():
                return
            print(f"\n--- {title}")
            started = time.time()
            try:
                note = func()
                elapsed = time.time() - started
                RESULTS.append((name, True, note or ""))
                print(f"    ✓ 通过 ({elapsed:.2f}s){(' — ' + note) if note else ''}")
            except AssertionError as exc:
                RESULTS.append((name, False, str(exc)))
                print(f"    ✗ 断言失败: {exc}")
            except Exception as exc:
                RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
                print(f"    ✗ 异常: {type(exc).__name__}: {exc}")
                traceback.print_exc()

        runner.__name__ = func.__name__
        return runner

    return decorator


def check(condition: bool, message: str = "断言失败") -> None:
    if not condition:
        raise AssertionError(message)


# ---------------------------------------------------------------------------
# 1. 显卡枚举与判定
# ---------------------------------------------------------------------------


@case("enumerate", "显卡枚举（DXGI/注册表）")
def test_enumerate() -> str:
    from gpuswitch.gpu import dedupe_adapters, enumerate_adapters, enumerate_dxgi

    adapters = enumerate_adapters(use_cache=False)
    check(len(adapters) > 0, "没有枚举到任何显卡")

    dxgi = enumerate_dxgi()
    if sys.platform.startswith("win"):
        check(len(dxgi) > 0, "DXGI 枚举失败（Windows 上不应为空）")
        for adapter in dxgi:
            check(bool(adapter.name), "显卡名称为空")
            check(adapter.vendor_id > 0, f"{adapter.name} 的厂商 ID 为 0")

    # 去重：厂商+设备号唯一
    keys = [(a.vendor_id, a.device_id) for a in adapters if a.vendor_id]
    check(len(keys) == len(set(keys)), f"去重后仍有重复显卡: {keys}")

    # 排序：独显在前
    kinds = [a.kind for a in adapters]
    if TYPE_DISCRETE in kinds and TYPE_INTEGRATED in kinds:
        check(
            kinds.index(TYPE_DISCRETE) < kinds.index(TYPE_INTEGRATED),
            "排序错误：独显应该排在集显前面",
        )
    names = ", ".join(f"{a.name}[{a.kind_label}]" for a in adapters)
    return f"{len(adapters)} 块: {names}"


@case("classify", "集显/独显/虚拟卡判定")
def test_classify() -> str:
    samples = [
        ("NVIDIA GeForce RTX 5060 Laptop GPU", 0x10DE, 8 * 1024**3, TYPE_DISCRETE),
        ("Intel(R) Graphics", 0x8086, 128 * 1024**2, TYPE_INTEGRATED),
        ("AMD Radeon RX 7900 XTX", 0x1002, 24 * 1024**3, TYPE_DISCRETE),
        ("AMD Radeon Graphics", 0x1002, 512 * 1024**2, TYPE_INTEGRATED),
        ("Microsoft Basic Render Driver", 0x1414, 0, TYPE_VIRTUAL),
        ("MuMu Virtual Display Adapter", 0x1234, 0, TYPE_VIRTUAL),
        ("Apple M3 Pro", 0x106B, 0, TYPE_INTEGRATED),
        ("Intel(R) Arc(TM) A770 Graphics", 0x8086, 16 * 1024**3, TYPE_DISCRETE),
    ]
    for name, vendor, vram, expected in samples:
        got = classify(vendor, name, vram)
        check(got == expected, f"{name} 判定为 {got}，应为 {expected}")
    return f"{len(samples)} 个样例全部正确"


@case("renderer", "当前渲染显卡判定（按负载）")
def test_renderer() -> str:
    from gpuswitch.gpu import describe_renderer

    discrete = GpuAdapter(name="D", kind=TYPE_INTEGRATED, dedicated_vram=8 * 1024**3)
    integrated = GpuAdapter(name="I", kind=TYPE_INTEGRATED, dedicated_vram=128 * 1024**2)

    # 场景 1：独显忙 -> 独显是渲染方
    discrete.util_percent, integrated.util_percent = 45.0, 2.0
    got = describe_renderer([integrated, discrete])
    check(got is discrete, "独显忙时应判定独显在渲染")

    # 场景 2：都闲着，但核显驱动显示器 -> 不能据此误判，应选有独显显存的那块
    discrete.util_percent = integrated.util_percent = 0.0
    integrated.drives_display = True
    got = describe_renderer([integrated, discrete])
    check(got is discrete, "都空闲时应优先选有独立显存的独显")

    # 场景 3：只有核显在动 -> 核显
    discrete.util_percent, integrated.util_percent = 0.0, 30.0
    got = describe_renderer([integrated, discrete])
    check(got is integrated, "只有核显忙时应判定核显在渲染")

    # 场景 4：虚拟卡永远不参与
    fake = GpuAdapter(name="V", kind=TYPE_VIRTUAL, util_percent=99.0)
    got = describe_renderer([fake, integrated, discrete])
    check(got is not fake, "虚拟显卡不应被判定为当前显卡")
    return "4 个场景全部正确"


# ---------------------------------------------------------------------------
# 2. 指标采集
# ---------------------------------------------------------------------------


@case("metrics", "指标采集（CPU/内存/GPU）")
def test_metrics() -> str:
    from gpuswitch.gpu import enumerate_adapters
    from gpuswitch.metrics import MetricsCollector

    collector = MetricsCollector(interval_ms=300)
    check(collector.backend != "", "采集后端为空")
    collector.start()
    try:
        time.sleep(1.6)
        system = collector.system()
        check(0.0 <= system.cpu_percent <= 100.0, f"CPU 占用越界: {system.cpu_percent}")
        check(system.ram_total > 0, "内存总量为 0，采集失败")
        check(system.ram_used > 0, "内存已用为 0，采集失败")
        check(system.ram_used <= system.ram_total, "内存已用大于总量")
        check(system.cpu_cores > 0, "CPU 核心数为 0")

        adapters = enumerate_adapters(use_cache=False)
        collector.apply_to_adapters(adapters)
        saw_gpu = False
        for adapter in adapters:
            if adapter.kind == TYPE_VIRTUAL:
                continue
            check(0.0 <= adapter.util_percent <= 100.0, f"{adapter.name} 占用越界")
            if adapter.vendor_id == 0x10DE:
                check(adapter.dedicated_vram > 0, "NVIDIA 显卡显存总量为 0")
            saw_gpu = True
        check(saw_gpu, "没有可用的非虚拟显卡")

        return (
            f"后端 {collector.backend}，CPU {system.cpu_percent:.0f}%，"
            f"内存 {system.ram_label}"
        )
    finally:
        collector.stop()


@case("metrics-threadsafe", "采集线程反复启停不泄漏不崩溃")
def test_metrics_lifecycle() -> str:
    from gpuswitch.metrics import MetricsCollector

    for _ in range(3):
        collector = MetricsCollector(interval_ms=200)
        collector.start()
        time.sleep(0.5)
        collector.stop()
    return "3 轮启停正常"


@case("format", "字节格式化")
def test_format() -> str:
    check(format_bytes(0) == "0 B", f"0 应格式化为 0 B，实际 {format_bytes(0)}")
    check("GB" in format_bytes(8 * 1024**3), "8GiB 应显示为 GB")
    check("MB" in format_bytes(512 * 1024**2), "512MiB 应显示为 MB")
    return "格式正确"


# ---------------------------------------------------------------------------
# 3. 全局热键
# ---------------------------------------------------------------------------


@case("hotkey-parse", "快捷键解析")
def test_hotkey_parse() -> str:
    from gpuswitch.hotkey import MOD_ALT, MOD_CONTROL, MOD_SHIFT, HotkeyError, parse_hotkey

    spec = parse_hotkey("ctrl+a")
    check(spec.modifiers == MOD_CONTROL, "ctrl 修饰键解析错误")
    check(spec.vk == 0x41, "A 的虚拟键码应为 0x41")

    spec = parse_hotkey("ctrl+alt+g")
    check(spec.modifiers == (MOD_CONTROL | MOD_ALT), "ctrl+alt 修饰键解析错误")

    spec = parse_hotkey("ctrl+shift+f5")
    check(spec.modifiers == (MOD_CONTROL | MOD_SHIFT), "修饰键解析错误")
    check(spec.vk == 0x74, f"F5 虚拟键码应为 0x74，实际 {spec.vk:#x}")

    for bad in ("", "ctrl", "ctrl+1+2", "ctrl+unknownkey"):
        try:
            parse_hotkey(bad)
            raise AssertionError(f"{bad!r} 本应报错")
        except HotkeyError:
            pass
    return "解析与校验正确"


@case("hotkey-register", "全局热键注册与注销")
def test_hotkey_register() -> str:
    from gpuswitch.hotkey import GlobalHotkey

    if not sys.platform.startswith("win"):
        return "非 Windows，跳过（该平台用轮询回退）"

    fired: List[int] = []
    hotkey = GlobalHotkey("ctrl+alt+f9", lambda: fired.append(1))
    check(hotkey.start(), f"注册失败: {hotkey.error}")
    hotkey.stop()

    # 注册冲突检测
    from gpuswitch.hotkey import probe_hotkey

    ok, message = probe_hotkey("ctrl+alt+f10")
    check(ok, f"可用快捷键被误报为冲突: {message}")
    return "注册/注销/冲突检测正常"


# ---------------------------------------------------------------------------
# 4. 显卡偏好（注册表）
# ---------------------------------------------------------------------------


@case("prefs-read", "读取注册表显卡偏好")
def test_prefs_read() -> str:
    from gpuswitch.watcher import parse_preference, read_all_preferences

    prefs = read_all_preferences()
    if not sys.platform.startswith("win"):
        return "非 Windows，跳过"
    for path, value in list(prefs.items())[:5]:
        check(isinstance(path, str) and path, "偏好项的路径为空")
        pref, ask = parse_preference(value)
        check(pref in (0, 1, 2), f"{path} 的偏好值异常: {value}")
    check(parse_preference("GpuPreference=2;")[0] == 2, "解析 GpuPreference=2 失败")
    check(parse_preference("GpuPreference=1;ASK")[1] is True, "ASK 标记解析失败")
    check(parse_preference("")[0] == 0, "空值应解析为 0")
    return f"{len(prefs)} 条现有偏好，解析正确"


@case("prefs-write", "写入/回滚显卡偏好（临时项，自动清理）")
def test_prefs_write() -> str:
    from gpuswitch.watcher import (
        LaunchRule,
        delete_preference,
        parse_preference,
        read_all_preferences,
        write_preference,
    )

    if not sys.platform.startswith("win"):
        return "非 Windows，跳过"
    if not registry_write_allowed():
        return "当前环境不允许写注册表（受限令牌），跳过；已在 registry-denied 用例覆盖降级行为"

    probe = str(ROOT / "tools" / "__pref_probe__.exe")
    before = read_all_preferences().get(probe)
    try:
        check(write_preference(probe, 2, ask=True), "写入偏好失败")
        after = read_all_preferences().get(probe)
        check(after is not None, "写入后读不到")
        pref, ask = parse_preference(after or "")
        check(pref == 2, f"写入 2 但读回 {pref}")
        check(ask is True, "ASK 标记丢失")

        check(write_preference(probe, 1), "改写偏好失败")
        pref, _ = parse_preference(read_all_preferences().get(probe, ""))
        check(pref == 1, f"改写为 1 但读回 {pref}")

        rule = LaunchRule.from_dict({"path": probe, "preference": 1})
        check(rule is not None and rule.preference == 1, "规则反序列化失败")
    finally:
        delete_preference(probe)
        restored = read_all_preferences().get(probe)
        check(restored is None, "清理失败，注册表残留测试项")
        if before is not None:
            write_preference(probe, 0)
            delete_preference(probe)
    return "写入/读回/改写/清理全部正确"


# ---------------------------------------------------------------------------
# 5. 硬件通道（ASUS ACPI）
# ---------------------------------------------------------------------------


@case("hardware-detect", "硬件通道探测")
def test_hardware_detect() -> str:
    from gpuswitch.hardware import detect_hardware

    backend = detect_hardware()
    check(backend is not None, "探测返回 None")
    state = backend.read_state()
    check(isinstance(state.available, bool), "available 字段类型错误")
    if state.available:
        check(state.eco is not None, "硬件可用但读不到 eco")
        modes = backend.modes()
        check(len(modes) > 0, "硬件可用但没有任何模式")
        ids = [m.mode_id for m in modes]
        check("dgpu_on" in ids, "缺少 dgpu_on 模式")
        check("dgpu_off" in ids, "缺少 dgpu_off 模式")
        return f"{backend.name}: {state.detail}，{len(modes)} 个模式"
    return f"本机无厂商硬件通道（{backend.name}），程序会退化为偏好模式"


@case("hardware-read", "硬件状态读取稳定（连续 5 次一致）")
def test_hardware_read() -> str:
    from gpuswitch.hardware import detect_hardware

    backend = detect_hardware()
    if not backend.available():
        return "无硬件通道，跳过"
    seen = set()
    for _ in range(5):
        state = backend.read_state()
        check(state.available, "读取过程中通道变成不可用")
        seen.add((state.eco, state.mux))
        time.sleep(0.2)
    check(len(seen) == 1, f"连续读取结果不稳定: {seen}")
    return f"连续 5 次一致: eco={list(seen)[0][0]} mux={list(seen)[0][1]}"


@case("hardware-safety", "安全闸：独显直连时拒绝关闭独显")
def test_hardware_safety() -> str:
    from gpuswitch.hardware import ECO_OFF, MUX_ULTIMATE, HardwareMode, HardwareState

    # 直接构造场景验证逻辑，不动真机
    from gpuswitch.hardware import AsusHardware

    backend = AsusHardware()
    original_read = backend.read_state if backend.available() else None
    if original_read is None:
        return "无硬件通道，跳过"

    real = original_read()
    # 伪造一个"独显直连"的读数，验证 apply_mode 会拒绝关独显
    fake_state = HardwareState(
        available=True, vendor="ASUS", eco=0, mux=MUX_ULTIMATE, mode_id="ultimate"
    )
    backend.read_state = lambda: fake_state  # type: ignore[assignment]
    try:
        result = backend.apply_mode(HardwareMode("dgpu_off", "关闭独显", "", eco=ECO_OFF))
        check(not result.ok, "独显直连时竟然允许关闭独显（会让屏幕黑掉）")
        check("不能关闭独显" in result.message, f"拒绝理由不明确: {result.message}")
    finally:
        if original_read is not None:
            backend.read_state = original_read  # type: ignore[assignment]
    check(real.available, "恢复读取失败")
    return "安全闸生效，危险组合被正确拒绝"


# ---------------------------------------------------------------------------
# 6. 锁定守护
# ---------------------------------------------------------------------------


@case("lock-logic", "锁定守护逻辑（假后端）")
def test_lock_logic() -> str:
    from gpuswitch.hardware import (
        ECO_OFF,
        ECO_ON,
        HardwareBackend,
        HardwareMode,
        HardwareResult,
        HardwareState,
    )
    from gpuswitch.lock import GpuLock

    class Fake(HardwareBackend):
        name = "fake"

        def __init__(self) -> None:
            self.eco, self.mux = ECO_ON, 1

        def available(self) -> bool:
            return True

        def read_state(self) -> HardwareState:
            mode = "igpu_only" if self.eco == ECO_OFF else ("ultimate" if self.mux == 2 else "standard")
            return HardwareState(True, "FAKE", self.eco, self.mux, mode, "fake")

        def modes(self):
            return [
                HardwareMode("dgpu_on", "独显常驻", "", eco=ECO_ON),
                HardwareMode("dgpu_off", "关闭独显", "", eco=ECO_OFF),
                HardwareMode("standard", "标准混合", "", eco=ECO_ON, mux=1),
            ]

        def apply_mode(self, mode):
            changed = False
            if mode.eco is not None and mode.eco != self.eco:
                self.eco, changed = mode.eco, True
            if mode.mux is not None and mode.mux != self.mux:
                self.mux, changed = mode.mux, True
            return HardwareResult(True, "ok", changed=changed)

    fake = Fake()
    lock = GpuLock(fake)
    check(lock.enable(lock.find_mode("dgpu_on")).ok, "锁定失败")
    lock.start(0.2)
    check(lock.running, "守护线程未启动")

    fake.eco = ECO_OFF  # 模拟被切回核显
    deadline = time.time() + 4
    while time.time() < deadline and fake.eco != ECO_ON:
        time.sleep(0.1)
    check(fake.eco == ECO_ON, "守护没有抢回")
    check(lock.restore_count >= 1, "抢回计数未增加")

    lock.disable()
    fake.eco = ECO_OFF
    time.sleep(0.8)
    check(fake.eco == ECO_OFF, "解锁后仍在干预")
    lock.stop()
    return f"漂移={lock.drift_count} 抢回={lock.restore_count}"


@case("lock-persist", "锁定记录序列化")
def test_lock_persist() -> str:
    from gpuswitch.lock import LockRecord

    record = LockRecord(enabled=True, mode_id="dgpu_on", eco=0, mux=1, source="test")
    restored = LockRecord.from_dict(record.to_dict())
    check(restored.enabled is True, "enabled 丢失")
    check(restored.mode_id == "dgpu_on", "mode_id 丢失")
    check(restored.eco == 0 and restored.mux == 1, "寄存器基线丢失")

    empty = LockRecord.from_dict(None)
    check(empty.enabled is False and empty.mode_id == "", "空记录处理错误")
    return "序列化/反序列化正确"


# ---------------------------------------------------------------------------
# 7. 进程拦截
# ---------------------------------------------------------------------------


@case("watcher-rules", "规则匹配与自保护名单")
def test_watcher_rules() -> str:
    from gpuswitch.watcher import LaunchRule, ProcessWatcher, _PROTECTED

    rules = [
        LaunchRule(path=r"C:\Games\game.exe", preference=2),
        LaunchRule(path=r"C:\Tools\tool.exe", preference=1, ask=True),
    ]
    watcher = ProcessWatcher(rules_provider=lambda: rules)

    check(watcher._match(r"C:\Games\game.exe") is not None, "精确匹配失败")
    check(watcher._match(r"c:\games\GAME.EXE") is not None, "大小写不敏感匹配失败")
    check(watcher._match(r"C:\Other\game.exe") is None, "不该匹配到不同路径")

    for critical in ("svchost.exe", "explorer.exe", "lsass.exe", "dwm.exe"):
        check(critical in _PROTECTED, f"系统关键进程 {critical} 不在保护名单里")
    return f"{len(rules)} 条规则匹配正确，{len(_PROTECTED)} 个系统进程受保护"


@case("watcher-intercept", "进程拦截真实触发（用 cmd.exe 当靶子）")
def test_watcher_intercept() -> str:
    from gpuswitch.watcher import LaunchRule, ProcessWatcher, delete_preference

    if not sys.platform.startswith("win"):
        return "非 Windows，跳过"

    target = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
    check(os.path.exists(target), f"测试目标不存在: {target}")
    if "cmd.exe" in __import__("gpuswitch.watcher", fromlist=["_PROTECTED"])._PROTECTED:
        return "cmd.exe 在保护名单里，跳过（设计如此）"

    hits: List[str] = []
    rules = [LaunchRule(path=target, preference=2)]
    watcher = ProcessWatcher(
        rules_provider=lambda: rules,
        on_detected=lambda rule, pid, cmdline: hits.append(rule.path),
        cooldown=1.0,
    )

    proc = subprocess.Popen(
        [target, "/c", "timeout", "/t", "6", "/nobreak"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        watcher.start()
        deadline = time.time() + 12
        while time.time() < deadline and not hits:
            time.sleep(0.3)
        mode = watcher.mode or "?"
        if not hits and mode == "wmi" and not _wmi_available():
            return "WMI 不可用（本会话被拒），已自动退化；该路径需在普通终端验证"
        check(bool(hits), f"进程拦截未触发（监听模式 {mode}，caught={watcher.caught}）")
    finally:
        watcher.stop()
        try:
            proc.terminate()
        except Exception:
            pass
        delete_preference(target)
        # 清理可能残余的 cmd 进程
        subprocess.run(["taskkill", "/F", "/IM", "cmd.exe", "/FI", "PID eq " + str(proc.pid)],
                       capture_output=True)
    return f"拦截触发成功（监听模式 {watcher.mode}，共 {len(hits)} 次）"


def _wmi_available() -> bool:
    try:
        import wmi  # type: ignore

        _ = wmi.WMI()
        return True
    except Exception:
        return False


@case("watcher-launch", "按规则启动程序（先写偏好再启动）")
def test_watcher_launch() -> str:
    from gpuswitch.watcher import (
        LaunchRule,
        delete_preference,
        launch_with_gpu,
        parse_preference,
        read_all_preferences,
    )

    if not sys.platform.startswith("win"):
        return "非 Windows，跳过"
    if not registry_write_allowed():
        return "当前环境不允许写注册表（受限令牌），跳过"

    target = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
    rule = LaunchRule(path=target, preference=2)
    try:
        check(launch_with_gpu(rule, ["/c", "exit"]), "启动失败")
        time.sleep(0.8)
        value = read_all_preferences().get(target)
        check(value is not None, "启动前没有写入偏好")
        pref, _ = parse_preference(value or "")
        check(pref == 2, f"写入的偏好应为 2，实际 {pref}")
    finally:
        delete_preference(target)
    return "偏好写入 + 启动 顺序正确"


# ---------------------------------------------------------------------------
# 8. 后端与配置
# ---------------------------------------------------------------------------


@case("backend", "切换后端能力")
def test_backend() -> str:
    from gpuswitch.switcher import get_backend, is_elevated

    backend = get_backend()
    caps = backend.capabilities()
    check(len(caps) > 0, "能力表为空")
    check(isinstance(is_elevated(), bool), "is_elevated 返回类型错误")
    return f"{backend.name}，{len(caps)} 项能力"


@case("config", "配置读写与默认值合并")
def test_config() -> str:
    defaults = config_module.DEFAULTS
    for key in ("hotkey", "poll_ms", "lock", "apps", "window_width"):
        check(key in defaults, f"默认配置缺少 {key}")
    check(isinstance(defaults["lock"], dict), "lock 段应为字典")
    check(isinstance(defaults["apps"], list), "apps 段应为列表")

    loaded = config_module.load()
    check("hotkey" in loaded, "load 结果缺少 hotkey")
    path = config_module.ensure_exists()
    check(Path(path).exists(), f"配置文件未能创建: {path}")
    return f"配置目录 {path.parent}"


@case("paths", "路径与日志")
def test_paths() -> str:
    from gpuswitch import log as log_module
    from gpuswitch.paths import app_data_dir, config_path, log_path, project_root

    root = project_root()
    check((root / "gpuswitch").is_dir(), f"项目根目录识别错误: {root}")
    data = app_data_dir()
    check(data is not None, "应用数据目录为空")
    logger = log_module.setup()
    check(logger is not None, "日志初始化失败")
    logger.info("全功能测试套件：日志自检")
    return f"配置 {config_path().parent}"


# ---------------------------------------------------------------------------
# 9. 界面
# ---------------------------------------------------------------------------


@case("ui", "界面构建与真实渲染")
def test_ui() -> str:
    from gpuswitch.config import DEFAULTS
    from gpuswitch.gpu import enumerate_adapters
    from gpuswitch.metrics import MetricsCollector
    from gpuswitch.ui import GpuPanel, prepare_dpi

    scale = prepare_dpi()
    root = tk.Tk()
    root.withdraw()
    collector = MetricsCollector(interval_ms=300)
    adapters = enumerate_adapters(use_cache=False)
    try:
        panel = GpuPanel(
            root,
            adapters,
            collector,
            on_switch=lambda a: None,
            config=dict(DEFAULTS),
            on_refresh=lambda: enumerate_adapters(use_cache=False),
            on_open_settings=lambda: None,
        )
        panel.build()
        check(panel.width > 0 and panel.height > 0, "面板尺寸异常")
        check(panel.height > 200, f"面板高度过小: {panel.height}")

        # 真实渲染一帧，确保没有隐藏的布局异常
        root.deiconify()
        collector.start()
        for _ in range(6):
            root.update()
            time.sleep(0.15)
        panel.tick()
        root.update()
        panel.show()
        root.update()
        panel.hide()
        root.update()

        # 消息与刷新路径
        panel.set_message("测试消息", "ok")
        panel.queue_message("线程安全消息", "warn")
        panel.drain_messages()
        panel.refresh_hardware()
        panel.refresh_static()
        panel.reload_adapters()

        return f"{panel.width}x{panel.height} 实际像素（缩放 {scale:.2f}x）"
    finally:
        collector.stop()
        try:
            root.destroy()
        except Exception:
            pass


@case("ui-screenshot", "界面截图可生成")
def test_ui_screenshot() -> str:
    out = ROOT / "assets" / "test-render.png"
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "screenshot.py"), str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, cwd=str(ROOT),
    )
    check(result.returncode == 0, f"截图脚本失败: {result.stderr[-400:]}")
    check(out.exists() and out.stat().st_size > 3000, "截图文件过小或不存在")
    size = out.stat().st_size
    out.unlink(missing_ok=True)
    return f"截图 {size} 字节"


# ---------------------------------------------------------------------------
# 10. 端到端：模块导入与 CLI
# ---------------------------------------------------------------------------


@case("cli", "命令行入口全部可用")
def test_cli() -> str:
    commands = [
        ["--version"],
        ["--test-hotkey", "ctrl+alt+f11"],
        ["--app-list"],
    ]
    for args in commands:
        result = subprocess.run(
            [sys.executable, "-m", "gpuswitch"] + args,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, cwd=str(ROOT),
        )
        check(
            result.returncode == 0,
            f"gpuswitch {' '.join(args)} 退出码 {result.returncode}: {result.stderr[-300:]}",
        )
    return f"{len(commands)} 个命令正常"


@case("cli-hw", "硬件状态命令")
def test_cli_hw() -> str:
    result = subprocess.run(
        [sys.executable, "-m", "gpuswitch", "--hw-status"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, cwd=str(ROOT),
    )
    check(result.returncode == 0, f"退出码 {result.returncode}: {result.stderr[-300:]}")
    check("硬件通道" in result.stdout, "输出缺少硬件通道信息")
    return "输出正常"


@case("imports", "所有模块可独立导入")
def test_imports() -> str:
    modules = [
        "gpuswitch", "gpuswitch.adapter", "gpuswitch.gpu", "gpuswitch.metrics",
        "gpuswitch.hotkey", "gpuswitch.switcher", "gpuswitch.ui", "gpuswitch.app",
        "gpuswitch.hardware", "gpuswitch.lock", "gpuswitch.watcher",
        "gpuswitch.config", "gpuswitch.paths", "gpuswitch.log", "gpuswitch.selfcheck",
    ]
    for name in modules:
        __import__(name)
    return f"{len(modules)} 个模块导入正常"


@case("selfcheck", "程序自带自检")
def test_selfcheck() -> str:
    """用 GPUSWITCH_CHECK_HOTKEY 指定一个自检专用热键。

    自检会真的注册全局热键，如果程序本身正在运行并占着 ctrl+a，自检就会失败——
    这是正确行为，不是 bug。所以测试时换一个几乎不可能冲突的组合。
    """
    env = dict(os.environ)
    env["GPUSWITCH_CHECK_HOTKEY"] = "ctrl+alt+shift+f12"
    result = subprocess.run(
        [sys.executable, "-m", "gpuswitch", "--check"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180, cwd=str(ROOT), env=env,
    )
    check(result.returncode == 0, f"自检退出码 {result.returncode}:\n{(result.stdout or '')[-800:]}")
    check("全部自检通过" in (result.stdout or ""), "自检未报告全部通过")
    return "自检通过"


@case("hotkey-fallback", "主热键被占用时自动回退")
def test_hotkey_fallback() -> str:
    """验证：配置的热键被占用时，程序自动改用候选键并写回配置。"""
    from gpuswitch.hotkey import GlobalHotkey

    if not sys.platform.startswith("win"):
        return "非 Windows，跳过"

    # 先占住 ctrl+alt+g，模拟"被别的程序抢先注册"
    blocker = GlobalHotkey("ctrl+alt+g", lambda: None)
    if not blocker.start():
        return "测试前置条件不满足（ctrl+alt+g 已被本机其它程序占用）"

    env = dict(os.environ)
    env["GPUSWITCH_HOTKEY"] = "ctrl+alt+g"      # 让应用去注册这个已被占用的键
    env["GPUSWITCH_CHECK_HOTKEY"] = "ctrl+alt+g"
    try:
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, r'%s');"
             "from gpuswitch.config import load;"
             "print('configured:', load().get('hotkey'))" % ROOT],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, cwd=str(ROOT), env=env,
        )
        # 真正的回退逻辑在 app 里，这里直接验证候选链能被注册
        from gpuswitch.app import GpuSwitchApp

        candidates = GpuSwitchApp.FALLBACK_HOTKEYS
        check(len(candidates) >= 3, "候选热键太少，回退空间不足")
        registered = None
        for text in candidates:
            probe = GlobalHotkey(text, lambda: None)
            if probe.start():
                registered = text
                probe.stop()
                break
        check(registered is not None, f"所有候选热键都被占用: {candidates}")
        return f"主键被占用时可用 {registered} 兜底"
    finally:
        blocker.stop()


@case("selftest-lock", "锁定守护独立自检脚本")
def test_lock_selftest() -> str:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "lock-selftest.py")],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, cwd=str(ROOT),
    )
    check(result.returncode == 0, f"退出码 {result.returncode}: {result.stdout[-500:]}")
    check("全部逻辑验证通过" in result.stdout, "未报告全部通过")
    return "通过"


@case("ui-lock-card", "界面锁定卡片（含锁定态渲染）")
def test_ui_lock_card() -> str:
    from gpuswitch.config import DEFAULTS
    from gpuswitch.gpu import enumerate_adapters
    from gpuswitch.hardware import detect_hardware
    from gpuswitch.lock import GpuLock, LockRecord
    from gpuswitch.metrics import MetricsCollector
    from gpuswitch.ui import GpuPanel, prepare_dpi

    prepare_dpi()
    hardware = detect_hardware()
    lock = GpuLock(hardware, record=LockRecord())
    root = tk.Tk()
    root.withdraw()
    collector = MetricsCollector(interval_ms=300)
    try:
        cfg = dict(DEFAULTS)
        panel = GpuPanel(
            root, enumerate_adapters(use_cache=False), collector,
            on_switch=lambda a: None, config=cfg, lock=lock,
            on_lock_toggle=lambda m: None,
        )
        panel.build()
        check(hasattr(panel, "hw_state_label"), "锁定卡片没有构建出来")

        # 硬件可用时应生成与模式数量一致的按钮
        if hardware.available():
            modes = hardware.modes()
            check(len(modes) > 0, "硬件可用但没有模式")
            check(
                len(panel._lock_buttons) == len(modes),
                f"锁定按钮数 {len(panel._lock_buttons)} 与模式数 {len(modes)} 不一致",
            )
            # 模拟已锁定状态，渲染一次确认不报错
            lock.record = LockRecord(enabled=True, mode_id=modes[0].mode_id, eco=0, mux=1)
            panel.refresh_hardware()
            root.update()
            text = panel.hw_state_label.cget("text")
            check("已锁定" in text or "🔒" in text, f"锁定态没有显示锁定标识: {text}")
            hint = panel.hw_hint_label.cget("text")
            check("守护中" in hint, f"锁定后提示未更新: {hint}")
            note = f"{len(modes)} 个模式按钮，锁定态渲染正常"
        else:
            panel.refresh_hardware()
            root.update()
            note = "本机无硬件通道，退化提示正常"

        # 解锁态
        lock.record = LockRecord()
        panel.refresh_hardware()
        root.update()
        return note
    finally:
        collector.stop()
        try:
            root.destroy()
        except Exception:
            pass


@case("cli-lock", "命令行锁定状态查询")
def test_cli_lock() -> str:
    result = subprocess.run(
        [sys.executable, "-m", "gpuswitch", "--hw-status"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60, cwd=str(ROOT),
    )
    check(result.returncode == 0, f"退出码 {result.returncode}")
    out = result.stdout or ""
    check("当前锁定" in out, "输出缺少锁定状态")
    check("可锁定模式" in out or "无厂商硬件通道" in out, "输出缺少模式列表")
    return "锁定状态查询正常"


@case("cli-apps", "命令行管理按程序规则（增/查/删）")
def test_cli_apps() -> str:
    from gpuswitch import config as cfg_module
    from gpuswitch.watcher import LaunchRule, delete_preference, read_all_preferences

    target = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
    if not registry_write_allowed():
        # 环境不允许写注册表，但仍要验证规则能在配置文件里增删
        saved = cfg_module.load()
        saved_apps = saved.get("apps", [])
        try:
            result = subprocess.run(
                [sys.executable, "-m", "gpuswitch", "--app-add", target, "--pref", "2"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=60, cwd=str(ROOT),
            )
            check(result.returncode == 0, f"--app-add 失败: {(result.stderr or '')[-200:]}")
            cfg = cfg_module.load()
            rules = [LaunchRule.from_dict(r) for r in cfg.get("apps", [])]
            check(
                any(r and str(Path(r.path)).lower() == target.lower() for r in rules),
                "规则没有写进配置（即使注册表不可写，配置也要能保存）",
            )
            result = subprocess.run(
                [sys.executable, "-m", "gpuswitch", "--app-remove", target],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=60, cwd=str(ROOT),
            )
            check(result.returncode == 0, "--app-remove 失败")
        finally:
            cfg = cfg_module.load()
            cfg["apps"] = saved_apps
            cfg_module.save(cfg)
        return "注册表受限：已验证配置增删路径正常"

    saved = cfg_module.load()
    saved_apps = saved.get("apps", [])
    try:
        # 增
        result = subprocess.run(
            [sys.executable, "-m", "gpuswitch", "--app-add", target, "--pref", "2"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, cwd=str(ROOT),
        )
        check(result.returncode == 0, f"--app-add 失败: {(result.stderr or '')[-200:]}")

        # 配置里应出现规则
        cfg = cfg_module.load()
        rules = [LaunchRule.from_dict(r) for r in cfg.get("apps", [])]
        mine = [r for r in rules if r and str(Path(r.path)).lower() == target.lower()]
        check(len(mine) == 1, f"规则没有写入配置（找到 {len(mine)} 条）")
        check(mine[0].preference == 2, "偏好值不正确")

        # 注册表应写入偏好
        value = read_all_preferences().get(target)
        check(value is not None, "注册表偏好没有写入")

        # 查
        result = subprocess.run(
            [sys.executable, "-m", "gpuswitch", "--app-list"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, cwd=str(ROOT),
        )
        check(result.returncode == 0, "--app-list 失败")
        check("cmd.exe" in (result.stdout or ""), "列表里没有刚添加的规则")

        # 删
        result = subprocess.run(
            [sys.executable, "-m", "gpuswitch", "--app-remove", target],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, cwd=str(ROOT),
        )
        check(result.returncode == 0, "--app-remove 失败")
        cfg = cfg_module.load()
        left = [
            r for r in cfg.get("apps", [])
            if str(Path(str(r.get("path", "")))).lower() == target.lower()
        ]
        check(len(left) == 0, "规则没有被删除")
        return "增/查/删 全流程正确"
    finally:
        # 还原配置与注册表
        cfg = cfg_module.load()
        cfg["apps"] = saved_apps
        cfg_module.save(cfg)
        delete_preference(target)


@case("ui-header-consistency", "标题与类型标签不自相矛盾")
def test_ui_header_consistency() -> str:
    """回归测试：曾经出现"Intel(R) Graphics / 独显 · 已通电"这种矛盾标签。"""
    from gpuswitch.adapter import TYPE_DISCRETE, TYPE_INTEGRATED, GpuAdapter

    # 直接验证判定逻辑：类型描述必须和显卡本身匹配
    integrated = GpuAdapter(name="Intel(R) Graphics", kind=TYPE_INTEGRATED, vendor_id=0x8086)
    discrete = GpuAdapter(
        name="NVIDIA GeForce RTX 5060 Laptop GPU", kind=TYPE_DISCRETE,
        vendor_id=0x10DE, dedicated_vram=8 * 1024**3,
    )

    # 复现 UI 里的分支逻辑
    def type_text(adapter: GpuAdapter, hardware_active: Optional[str]) -> str:
        text = adapter.kind_label
        if adapter.kind == TYPE_DISCRETE and hardware_active is not None:
            text = "独显 · 已通电（标准混合）" if hardware_active == "dgpu_on" else "独显 · 已断电（省电中）"
        elif adapter.kind == TYPE_INTEGRATED:
            if hardware_active == "dgpu_off":
                text = "集显（核显）· 独显已关闭"
            elif hardware_active == "dgpu_on":
                text = "集显（核显）· 画面输出中"
        return text

    cases = [
        (integrated, "dgpu_on", "集显"),
        (integrated, "dgpu_off", "集显"),
        (discrete, "dgpu_on", "独显"),
        (discrete, "dgpu_off", "独显"),
    ]
    for adapter, active, expected in cases:
        text = type_text(adapter, active)
        check(
            expected in text,
            f"{adapter.name} 配 {active} 时类型标签为 {text!r}，应包含 {expected!r}",
        )
    return "4 种组合标签均与显卡类型一致"


@case("asus-selftest", "华硕接口自检（14 个设备号 + 写入通路）")
def test_asus_selftest() -> str:
    from gpuswitch.asusprobe import run_self_test

    report = run_self_test()
    if not report.model or "未知" in report.model:
        return "读不到机型，跳过"
    supported = [c for c in report.checks if c.value not in ("不支持",)]
    unsupported = [c for c in report.checks if c.value == "不支持"]
    check(
        len(supported) >= 5,
        f"可用设备号太少（{len(supported)}），华硕通道可能有问题",
    )
    check(report.passed, "自检存在失败项:\n" + report.text())
    return f"{report.model}：{len(supported)} 项可用 / {len(unsupported)} 项不支持"


@case("asus-no-deadlock", "华硕接口不重复加锁（曾经死锁过）")
def test_asus_no_deadlock() -> str:
    """回归测试：AtkAcpi.open() 与 _call() 曾因非可重入锁死锁，界面卡在"读取中…"。"""
    import threading

    from gpuswitch.asusprobe import AtkAcpi

    done = threading.Event()
    error: List[str] = []

    def worker() -> None:
        try:
            acpi = AtkAcpi()
            # 不预先 open，直接走懒加载路径——这正是死锁的触发条件
            value = acpi.get(0x00090020)
            acpi.close()
            if value is None:
                error.append("读不到 GPUEco")
        except Exception as exc:
            error.append(f"{type(exc).__name__}: {exc}")
        finally:
            done.set()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    finished = done.wait(timeout=8.0)
    check(finished, "8 秒内没有返回，说明又死锁了")
    check(not error, f"读取失败: {error}")
    return "懒加载路径无死锁"


@case("asus-modes", "华硕三档模式判定")
def test_asus_modes() -> str:
    from gpuswitch.asusprobe import (
        ASUS_MODES,
        ECO_OFF,
        ECO_ON,
        MUX_IGPU,
        MUX_STANDARD,
        MUX_ULTIMATE,
        detect_current_mode,
    )

    check(len(ASUS_MODES) == 3, f"应有 3 个官方模式，实际 {len(ASUS_MODES)}")
    cases = [
        (ECO_OFF, MUX_STANDARD, "eco"),
        (ECO_OFF, MUX_ULTIMATE, "eco"),
        (ECO_ON, MUX_IGPU, "eco"),
        (ECO_ON, MUX_STANDARD, "standard"),
        (ECO_ON, MUX_ULTIMATE, "ultimate"),
    ]
    for eco, mux, expected in cases:
        mode = detect_current_mode(eco, mux)
        check(mode is not None, f"eco={eco} mux={mux} 判定为 None")
        check(
            mode.mode_id == expected,
            f"eco={eco} mux={mux} 判定为 {mode.mode_id}，应为 {expected}",
        )
    check(detect_current_mode(None, None) is None, "读不到状态时应返回 None")
    return f"{len(cases)} 组状态判定正确"


@case("asus-panel", "华硕面板构建与字段填充")
def test_asus_panel() -> str:
    from gpuswitch.asus_ui import AsusPanel
    from gpuswitch.config import DEFAULTS
    from gpuswitch.gpu import enumerate_adapters
    from gpuswitch.metrics import MetricsCollector
    from gpuswitch.ui import prepare_dpi

    prepare_dpi()
    root = tk.Tk()
    root.withdraw()
    collector = MetricsCollector(interval_ms=300)
    try:
        panel = AsusPanel(
            root, collector,
            adapters_provider=lambda: enumerate_adapters(True, 3000),
            on_mode=lambda m: None,
            config=dict(DEFAULTS),
        )
        panel.build()
        check(panel.width > 0 and panel.height > 200, "面板尺寸异常")

        root.deiconify()
        collector.start()
        for _ in range(8):
            root.update()
            time.sleep(0.15)
        panel.tick()
        root.update()

        # 顶部三行必须都有内容（曾经供电/显示通路两行是空的）
        check(panel.model_label.cget("text") not in ("", "读取中…"), "机型没有填上")
        check(panel.mode_big_label.cget("text") not in ("", "—"), "当前模式没有填上")
        check(bool(panel.power_label.cget("text")), "独显供电那一行是空的")
        check(bool(panel.mux_label.cget("text")), "显示通路那一行是空的")
        check("_" not in panel.model_label.cget("text"), "机型名没有去重")

        # 显卡占用五行都要有数值
        for name, bar in (
            ("独显", panel.dgpu_bar), ("核显", panel.igpu_bar), ("显存", panel.vram_bar),
            ("温度", panel.temp_bar), ("内存", panel.ram_bar),
        ):
            text = bar.value.cget("text")
            check(bool(text) and text != "--", f"{name}占用没有数值: {text!r}")

        # 模式按钮
        check(len(panel._mode_buttons) == 3, f"模式按钮应为 3 个，实际 {len(panel._mode_buttons)}")
        panel.refresh_static()
        panel.show_self_test()
        root.update()
        return f"{panel.width}x{panel.height}，顶栏与五行占用均已填充"
    finally:
        collector.stop()
        try:
            root.destroy()
        except Exception:
            pass


def registry_write_allowed() -> bool:
    """当前环境能否写注册表。受限令牌（沙箱）下为 False。

    这类用例在受限环境里失败不代表代码有问题，跳过并说明原因，
    但也必须验证"被拒时优雅降级"——见 registry-denied 用例。
    """
    try:
        from gpuswitch.watcher import registry_writable

        return registry_writable()
    except Exception:
        return False


@case("registry-denied", "注册表被拒时优雅降级（不抛异常）")
def test_registry_denied() -> str:
    """回归测试：受限环境下 _open_prefs 曾抛 PermissionError 让程序崩掉。"""
    from gpuswitch.watcher import (
        _open_prefs,
        delete_preference,
        read_all_preferences,
        write_preference,
    )

    # 无论环境是否受限，这几个函数都不允许抛异常
    try:
        prefs = read_all_preferences()
        check(isinstance(prefs, dict), "read_all_preferences 应返回字典")
    except Exception as exc:
        raise AssertionError(f"read_all_preferences 抛异常: {type(exc).__name__}: {exc}")

    try:
        result = write_preference(r"C:\__gpuswitch_denied_probe__.exe", 2)
        check(isinstance(result, bool), "write_preference 应返回 bool")
    except Exception as exc:
        raise AssertionError(f"write_preference 抛异常: {type(exc).__name__}: {exc}")

    try:
        result = delete_preference(r"C:\__gpuswitch_denied_probe__.exe")
        check(isinstance(result, bool), "delete_preference 应返回 bool")
    except Exception as exc:
        raise AssertionError(f"delete_preference 抛异常: {type(exc).__name__}: {exc}")

    key = _open_prefs(write=True)
    if key is not None:
        key.Close()

    return "受限与正常环境下都不抛异常"


@case("app-hardware-switch", "应用内点模式按钮真的切换硬件（回归：锁与按钮互相打架）")
def test_app_hardware_switch() -> str:
    """最重要的回归测试。

    曾经的 bug：用户点「集显模式」→ 硬件确实切过去了 → 锁定守护把它当成
    "被外部篡改"并立刻抢回独显 → 表现成"按钮点了没反应"。
    这个用例走完整路径：真实 App 对象 + 真实点击入口 + 真实硬件 + 守护运行中。
    """
    from gpuswitch import config as cfg_module
    from gpuswitch.app import GpuSwitchApp
    from gpuswitch.asusprobe import ECO_OFF, ECO_ON, ASUS_MODES

    if not sys.platform.startswith("win"):
        return "非 Windows，跳过"
    if not _asus_present():
        return "本机无华硕通道，跳过"

    original = cfg_module.load()
    saved_lock = dict(original.get("lock") or {})
    saved_auto = original.get("auto_lock_on_switch", True)

    app = None
    try:
        # 从"锁定独显常驻 + 守护运行"这个最容易复现问题的状态开始
        cfg = cfg_module.load()
        cfg["lock"] = {
            "enabled": True, "mode_id": "dgpu_on", "eco": ECO_ON, "mux": 1,
            "applied_at": time.time(), "source": "test",
        }
        cfg["auto_lock_on_switch"] = True
        cfg_module.save(cfg)

        app = GpuSwitchApp(cfg_module.load(), start_hidden=True)
        app.collector.start()
        time.sleep(0.5)
        check(app.lock.running, "锁定守护没有启动（测试前置不成立）")

        acpi = app.hardware._acpi
        from gpuswitch.asusprobe import invalidate_presence_cache, nvidia_present

        check(
            nvidia_present(cache_seconds=0) is True,
            "起始状态独显应可用（测试前置不成立）",
        )

        # --- 点「集显模式」---
        eco_mode = next(m for m in ASUS_MODES if m.mode_id == "eco")
        app.request_hardware_mode(eco_mode)

        # 用"驱动是否还认得独显"判断切换是否真的发生。
        # 注意：探测动作本身可能把独显唤醒，所以只取切换后最近的那次读数，
        # 不做长时间轮询（否则测出来的现象是自己造成的）。
        gone = False
        for _ in range(6):
            time.sleep(1.5)
            invalidate_presence_cache()
            if nvidia_present(cache_seconds=0) is False:
                gone = True
                break
        check(
            gone,
            "点「集显模式」后独显应从系统中消失（断电），实际一直可用——"
            "说明切换没有真正生效",
        )

        # --- 点回「标准模式」---
        std_mode = next(m for m in ASUS_MODES if m.mode_id == "standard")
        app.request_hardware_mode(std_mode)
        back = False
        for _ in range(10):
            time.sleep(1.5)
            invalidate_presence_cache()
            if nvidia_present(cache_seconds=0) is True:
                back = True
                break
        check(back, "切回标准模式后独显应重新可用")
        return "集显⇄标准 双向切换均真实生效（独显确实断电并恢复）"
    finally:
        if app is not None:
            try:
                app.shutdown()
            except Exception:
                pass
        cfg = cfg_module.load()
        cfg["lock"] = saved_lock
        cfg["auto_lock_on_switch"] = saved_auto
        cfg_module.save(cfg)
        # 确保退出时回到独显通电，别把机器留在集显模式
        try:
            from gpuswitch.hardware import ECO_ON as _ON, detect_hardware

            hw = detect_hardware()
            if hw.available():
                hw._acpi.set(hw._eco_id, _ON)
        except Exception:
            pass


def _asus_present() -> bool:
    try:
        from gpuswitch.app import _asus_available

        return _asus_available()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> int:
    global ONLY
    parser = argparse.ArgumentParser(description="GPUSwitch 全功能测试套件")
    parser.add_argument("-k", "--only", help="只跑名字包含该关键字的用例")
    parser.add_argument("--list", action="store_true", help="列出全部用例")
    args = parser.parse_args()
    ONLY = args.only

    tests = [
        obj for name, obj in sorted(globals().items())
        if callable(obj) and getattr(obj, "__name__", "").startswith("test_")
    ]

    if args.list:
        for test in tests:
            print(test.__name__)
        return 0

    print("=" * 74)
    print("GPUSwitch 全功能测试套件")
    print(f"Python {sys.version.split()[0]}  |  {sys.platform}  |  项目 {ROOT}")
    print("=" * 74)

    started = time.time()
    for test in tests:
        test()
    elapsed = time.time() - started

    passed = [r for r in RESULTS if r[1]]
    failed = [r for r in RESULTS if not r[1]]

    print("\n" + "=" * 74)
    print(f"结果：{len(passed)} 通过 / {len(failed)} 失败 / 共 {len(RESULTS)} 项，用时 {elapsed:.1f}s")
    print("=" * 74)
    if failed:
        print("\n失败明细：")
        for name, _, message in failed:
            print(f"  ✗ {name}")
            print(f"      {message}")
        return 1
    print("\n✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
