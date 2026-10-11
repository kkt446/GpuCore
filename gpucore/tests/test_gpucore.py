"""GpuCore 自测套件 —— 只跑不碰硬件、不改变你显卡状态的检查。

用法：
    python -m gpucore.tests.test_gpucore              # 全部（安全的）用例
    python -m gpucore.tests.test_gpucore -k hotkey    # 只跑名字里带 hotkey 的
    python -m gpucore.tests.test_gpucore --write      # 额外跑会真的写 ACPI 的用例（会切显卡！）

设计原则：
    * 默认**绝不**写 ACPI —— 只有 --write 才跑 switch_eco/switch_mux。
    * 每条用例独立 try/except，互不影响；失败会打印异常与堆栈尾部。
    * 退出码 0 = 全过，1 = 有失败。可直接用于 CI 或发布前把关。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import dataclasses
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Callable, List, Optional, Tuple

# 允许直接 `python gpucore/tests/test_gpucore.py` 运行
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gpucore import branding, hardware  # noqa: E402
from gpucore import app as gapp  # noqa: E402

CHECKS: List[Tuple[str, Callable[[], None]]] = []


def check(name: str):
    def deco(fn: Callable[[], None]):
        CHECKS.append((name, fn))
        return fn

    return deco


def _assert(cond, msg: str = "") -> None:
    if not cond:
        raise AssertionError(msg or "断言失败")


def _dir_writable(d: Path) -> bool:
    """真测一次写入；受限令牌/只读位置下会返回 False（用来跳过而不是误报失败）。"""
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 身份 / 路径
# ---------------------------------------------------------------------------

@check("branding/identity")
def test_branding_identity() -> None:
    _assert(branding.APP_NAME == "GpuCore", f"APP_NAME={branding.APP_NAME!r}")
    _assert(branding.APP_NAME_CN, "缺少中文名")
    _assert(branding.APP_REPO.startswith("https://github.com/"), "仓库地址不对")
    parts = branding.APP_VERSION.split(".")
    _assert(len(parts) >= 2 and all(p.isdigit() for p in parts), f"版本号格式异常 {branding.APP_VERSION!r}")


@check("branding/bundle-dir")
def test_bundle_dir() -> None:
    b = branding.bundle_dir()
    _assert(b.is_dir(), f"bundle_dir 不是目录: {b}")
    _assert((b / "gpucore" / "app.py").exists(), f"bundle_dir 里找不到 gpucore/app.py: {b}")


@check("branding/install-ps1-single-source")
def test_install_ps1_reads_branding() -> None:
    """install.ps1 必须从 branding.py 现读品牌名，不能自己再抄一份。

    它拿这个值算安装目录 %LOCALAPPDATA%\\<APP_DIR_NAME>。如果它硬写 'GpuCore'
    而 branding.py 已经改名，安装目录和程序读配置的目录就会分叉 —— 装到 A 目录、
    程序去 B 目录找 config.json，用户看到的是"配置莫名其妙丢了"。
    """
    p = branding.bundle_dir() / "install.ps1"
    _assert(p.exists(), f"找不到 {p}")
    src = p.read_text(encoding="utf-8")

    _assert("Read-Branding" in src, "install.ps1 没有从 branding.py 读品牌信息")

    # 旧写法：$AppName = 'GpuCore' / $AppNameCn = '双芯'
    bad = re.search(r"\$(?:AppName|AppNameCn|InstallDir)\s*=\s*['\"]", src)
    _assert(not bad, f"install.ps1 又把品牌名硬写回去了: {bad.group(0) if bad else ''}")

    for key in ("APP_NAME", "APP_NAME_CN", "APP_DIR_NAME"):
        _assert(key in src, f"install.ps1 没读 {key}")



def test_portable_mode() -> None:
    # 源码树里没有 GpuCore.exe / PORTABLE，应当判为非便携
    _assert(branding.portable_mode() is False, "源码树被误判为便携包")
    got = branding.install_dir()
    _assert(isinstance(got, Path), "install_dir 必须返回 Path")
    _assert(got.name == branding.APP_DIR_NAME or got == branding.bundle_dir(),
            f"install_dir 落到意外位置: {got}")


@check("branding/config-writable")
def test_config_path() -> None:
    p = branding.config_path()
    d = p.parent
    d.mkdir(parents=True, exist_ok=True)
    probe = d / ".gpucore_write_probe"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    _assert(p.name == "config.json", f"配置文件名不对: {p.name}")
    _assert(branding.log_path().parent == d, "日志与配置不在同一目录")


# ---------------------------------------------------------------------------
# 硬件自适应层（只读）
# ---------------------------------------------------------------------------

@check("hardware/constants")
def test_hardware_constants() -> None:
    _assert(hardware.ATKACPI_PATH == r"\\.\ATKACPI", "设备路径不对")
    _assert(hardware.CONTROL_CODE == 0x0022240C, "控制码不对")
    _assert((hardware.DSTS, hardware.DEVS) == (0x53545344, 0x53564544), "方法号不对")
    _assert((hardware.ECO_ON, hardware.ECO_OFF) == (0, 1), "eco 语义不对")
    _assert((hardware.MUX_IGPU, hardware.MUX_STANDARD, hardware.MUX_ULTIMATE) == (0, 1, 2), "mux 语义不对")


@check("hardware/candidates")
def test_hardware_candidates() -> None:
    eco = dict(hardware.ECO_CANDIDATES)
    mux = dict(hardware.MUX_CANDIDATES)
    # ROG/TUF 与 VivoBook/Zenbook 两套都要在候选里，否则只有一种机型能用
    _assert(0x00090020 in eco, "缺少 ROG/TUF 的独显供电设备号")
    _assert(0x00090120 in eco, "缺少 VivoBook/Zenbook 的独显供电设备号")
    _assert(0x00090016 in mux, "缺少 ROG/TUF 的显示通路设备号")
    _assert(0x00090026 in mux, "缺少 VivoBook/Zenbook 的显示通路设备号")
    for key in ("temp_cpu", "temp_gpu", "perf_mode"):
        _assert(key in hardware.SENSOR_DEVICES, f"传感器缺少 {key}")


@check("hardware/acpi-open")
def test_acpi_open() -> None:
    acpi = hardware.AtkAcpi()
    opened = acpi.open()
    acpi.close()
    if not opened:
        print("      （非华硕或未装 ATK 驱动：跳过，不算失败）")
        return
    print("      ATKACPI 可打开")


@check("hardware/probe")
def test_probe() -> None:
    acpi = hardware.AtkAcpi()
    if not acpi.open():
        print("      （无 ACPI 通道：跳过）")
        return
    try:
        dm = hardware.probe_devices(acpi)
    finally:
        acpi.close()
    _assert(isinstance(dm, hardware.DeviceMap), "probe_devices 没返回 DeviceMap")
    _assert(isinstance(dm.acpi_ok, bool), "acpi_ok 必须是 bool")
    if dm.eco is not None:
        _assert(dm.eco in (0x00090020, 0x00090120), f"选中的 eco 设备号意外: {hex(dm.eco)}")
    if dm.mux is not None:
        _assert(dm.mux in (0x00090016, 0x00090026), f"选中的 mux 设备号意外: {hex(dm.mux)}")
    print(f"      {dm.describe()}")
    for line in hardware.supported_summary(dm):
        print(f"      {line}")


@check("hardware/device-map-gates")
def test_device_map_gates() -> None:
    # 没探测到就不该声称能切 —— 这是"机型自适应"的安全底线
    empty = hardware.DeviceMap()
    _assert(empty.can_eco is False, "空 DeviceMap 不该声称能切独显供电")
    _assert(empty.can_mux is False, "空 DeviceMap 不该声称能切显示通路")
    full = hardware.DeviceMap(acpi_ok=True, eco=0x00090020, mux=0x00090016)
    _assert(full.can_eco is True and full.can_mux is True, "完整 DeviceMap 应当都能切")


@check("hardware/read-eco-readonly")
def test_read_eco() -> None:
    acpi = hardware.AtkAcpi()
    if not acpi.open():
        print("      （无 ACPI 通道：跳过）")
        return
    try:
        dm = hardware.probe_devices(acpi)
        if dm.eco is None:
            print("      （本机无 eco 设备号：跳过）")
            return
        raw = hardware.read_eco(acpi, dm.eco)
        _assert(raw is None or raw in (0, 1), f"read_eco 返回值意外: {raw}")
        mux = hardware.read_mux(acpi, dm.mux) if dm.mux else None
        print(f"      {hardware.mode_name(raw, mux)}")
    finally:
        acpi.close()


@check("hardware/no-write-by-default")
def test_no_write_by_default() -> None:
    # 防回归：这个测试套件本身绝不能默认写 ACPI —— 会切用户的显卡。
    # 断言「所有写操作都只出现在 test_write_* 函数里」。
    import inspect

    for fn in (test_write_eco,):
        _assert("--write" in inspect.getsource(fn), f"{fn.__name__} 没有 --write 保护")
    for name, fn in CHECKS:
        if name.startswith(("hardware/", "metrics/")) and not name.endswith("no-write-by-default"):
            src = inspect.getsource(fn)
            for bad in ("write_eco(", "write_mux(", "switch_eco(", "switch_mux("):
                _assert(bad not in src, f"只读用例 {name} 里出现了 {bad}，会真的改显卡状态")


# ---------------------------------------------------------------------------
# 采集：CPU / 内存 / 显卡
# ---------------------------------------------------------------------------

@check("metrics/fast-system")
def test_fast_system() -> None:
    fs = gapp.FastSystem()
    time.sleep(0.35)
    cpu = fs.cpu_percent()
    _assert(0.0 <= cpu <= 100.0, f"CPU 占用超出范围: {cpu}")
    used, total = fs.memory()
    _assert(total > 0, "内存总量为 0")
    _assert(0 < used <= total, f"内存用量异常: {used}/{total}")
    print(f"      CPU {cpu:.0f}%  内存 {used / 2**30:.1f}/{total / 2**30:.1f} GB")


@check("metrics/machine-model")
def test_machine_model() -> None:
    name = gapp.machine_model()
    _assert(isinstance(name, str), "machine_model 必须返回字符串")
    _assert(name.strip() != "", "读不到机型名（界面顶部会空着）")
    print(f"      {name}")


@check("metrics/igpu-luid")
def test_igpu_luid() -> None:
    luid = gapp.detect_igpu_luid()
    if luid in (None, ""):
        print("      （未找到核显：跳过）")
        return
    # 格式必须是 0x%08x_0x%08x（19 字符）—— 大小写必须统一成小写，
    # 否则和 PDH 实例名（大写）比较时偶发匹配不上，核显读数会恒为 0
    _assert(re.fullmatch(r"0x[0-9a-f]{8}_0x[0-9a-f]{8}", luid) is not None,
            f"LUID 格式不对（应为 0x%08x_0x%08x 小写）: {luid!r}")


@check("metrics/nvidia-smi")
def test_nvidia_smi() -> None:
    ok = gapp.nvidia_available()
    print(f"      nvidia-smi 可用: {ok}")
    if ok:
        cp = gapp.run_quiet(["nvidia-smi", "--query-gpu=name,utilization.gpu", "--format=csv,noheader,nounits"])
        _assert(cp.returncode == 0, f"nvidia-smi 查询失败: {cp.stderr}")
        _assert("NVIDIA" in cp.stdout, f"nvidia-smi 输出异常: {cp.stdout!r}")
        print(f"      {cp.stdout.strip()}")


@check("metrics/run-quiet-utf8")
def test_run_quiet_utf8() -> None:
    # 中文 Windows 上 subprocess 默认 GBK，遇到 UTF-8 子进程输出会直接崩。
    cp = gapp.run_quiet([sys.executable, "-c", "print('中文输出 测试')"])
    _assert(cp.returncode == 0, "run_quiet 调用失败")
    _assert("中文输出" in cp.stdout, f"UTF-8 输出被解坏: {cp.stdout!r}")


@check("metrics/monitor-thread")
def test_monitor_thread() -> None:
    logs: List[str] = []
    mon = gapp.Monitor(interval=0.3, on_log=logs.append)
    mon.start()
    try:
        deadline = time.time() + 15
        snap = None
        while time.time() < deadline:
            snap = mon.read()
            # 采到「有意义的一帧」才算就绪：CPU 和内存总量都非零
            if snap is not None and snap.cpu > 0 and snap.ram_total > 0:
                break
            time.sleep(0.2)
        _assert(snap is not None, "Monitor.read() 一直是 None")
        _assert(snap.ram_total > 0, f"15 秒内没采到内存数据，日志：{logs[-3:]}")
        _assert(0 <= snap.cpu <= 100, f"CPU 占用异常 {snap.cpu}")
        _assert(snap.eco in (None, 0, 1), f"eco 值异常 {snap.eco}")
        _assert(snap.mux in (None, 0, 1, 2), f"mux 值异常 {snap.mux}")
        if snap.temp is not None:
            _assert(-40 < snap.temp < 130, f"CPU 温度离谱: {snap.temp}")
        print(f"      CPU {snap.cpu:.0f}%  内存 {snap.ram_used / 2**30:.1f}/{snap.ram_total / 2**30:.1f} GB"
              f"  独显 {'断电' if snap.dgpu is None else f'{snap.dgpu:.0f}%'}"
              f"  核显 {snap.igpu:.0f}%")
        # 快照必须能被反复读且线程安全（无锁撕裂）
        for _ in range(20):
            mon.read()
    finally:
        mon.stop()
        mon.join(timeout=5)
    _assert(not mon.is_alive(), "Monitor.stop() 之后线程还在跑")


@check("metrics/snapshot-frozen")
def test_snapshot_frozen() -> None:
    fields = {f.name for f in dataclasses.fields(gapp.Snapshot)}
    for need in ("cpu", "ram_used", "ram_total", "dgpu", "igpu", "eco", "mux", "temp"):
        _assert(need in fields, f"Snapshot 缺少字段 {need}")


# ---------------------------------------------------------------------------
# 热键
# ---------------------------------------------------------------------------

@check("hotkey/parse")
def test_hotkey_parse() -> None:
    spec = gapp.parse_hotkey("ctrl+alt+g")
    _assert(spec.vk == ord("G"), f"主键解析错: {spec.vk}")
    _assert(spec.modifiers == (gapp.MOD_CONTROL | gapp.MOD_ALT), f"修饰键解析错: {spec.modifiers}")
    _assert(spec.describe() == "Ctrl+Alt+G", f"显示文本错: {spec.describe()}")
    # 大小写 / 空格 / 连字符 / 顺序都应能容忍
    _assert(gapp.parse_hotkey("CTRL + SHIFT + F9").describe() == "Ctrl+Shift+F9")
    _assert(gapp.parse_hotkey("alt-ctrl-g").modifiers == (gapp.MOD_CONTROL | gapp.MOD_ALT))
    for bad in ("", "g", "ctrl+alt", "ctrl+alt+nosuchkey"):
        try:
            gapp.parse_hotkey(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} 应该被拒绝")


@check("hotkey/register")
def test_hotkey_register() -> None:
    spec = gapp.parse_hotkey("ctrl+alt+shift+f24")
    user32 = ctypes.windll.user32
    ok = user32.RegisterHotKey(None, 1, spec.modifiers | gapp.MOD_NOREPEAT, spec.vk)
    _assert(ok, "RegisterHotKey 失败（组合键可能已被占用）")
    user32.UnregisterHotKey(None, 1)
    print(f"      {spec.describe()} 试注册成功")


@check("hotkey/listener-lifecycle")
def test_hotkey_listener() -> None:
    fired: List[int] = []
    lis = gapp.HotkeyListener("ctrl+alt+shift+f23", lambda: fired.append(1))
    started = lis.start(wait=3.0)
    _assert(started, f"热键监听线程没起来: {lis.error}")
    time.sleep(0.3)
    lis.stop()
    _assert(not lis.error, f"监听线程报错: {lis.error}")


# ---------------------------------------------------------------------------
# 配置 / 自启
# ---------------------------------------------------------------------------

@check("config/roundtrip")
def test_config_roundtrip() -> None:
    original = gapp.CONFIG_PATH
    tmp = Path(tempfile.mkdtemp(prefix="gpucore-cfg-"))
    if not _dir_writable(tmp):
        # 沙箱/受限令牌下临时目录也可能不可写，此时跳过而不是误报失败
        print("      （当前环境不允许写临时目录：跳过）")
        return
    try:
        gapp.CONFIG_PATH = tmp / "config.json"
        _assert(gapp.load_config()["hotkey"] == "ctrl+alt+g", "默认热键不对")
        gapp.save_config({"hotkey": "ctrl+shift+k", "always_on_top": False})
        cfg = gapp.load_config()
        _assert(cfg["hotkey"] == "ctrl+shift+k", "保存后读回不一致")
        _assert(cfg["always_on_top"] is False, "布尔值没保存")
        # 落盘的文件必须是合法 JSON，且不能留下 .tmp 残渣
        _assert(not (tmp / "config.json.tmp").exists(), "原子写的临时文件没清掉")
        # 配置文件损坏时必须退回默认值，而不是崩掉
        gapp.CONFIG_PATH.write_text("{ 这不是 json", encoding="utf-8")
        _assert(gapp.load_config()["hotkey"] == "ctrl+alt+g", "损坏配置没有回退默认值")
        print(f"      配置目录: {tmp}")
    finally:
        gapp.CONFIG_PATH = original


@check("config/save-is-atomic")
def test_config_atomic() -> None:
    src = Path(gapp.__file__).read_text(encoding="utf-8")
    body = src.split("def save_config")[1].split("\ndef ")[0]
    _assert("replace" in body or "os.replace" in body, "save_config 不是原子写，断电可能写坏配置")


@check("autostart/command-shape")
def test_autostart_command() -> None:
    cmd = gapp.autostart_command()
    _assert(cmd.startswith('"'), f"命令必须以带引号的路径开头: {cmd}")
    _assert(cmd.count('"') % 2 == 0, f"引号不成对: {cmd}")
    if branding.portable_mode():
        _assert("GpuCore.exe" in cmd, f"便携包的启动命令应指向 GpuCore.exe: {cmd}")
    else:
        _assert(cmd.endswith("-m gpucore"), f"安装版启动命令不对: {cmd}")
        exe = Path(cmd.strip('"').split('"')[0])
        _assert(exe.exists(), f"自启命令里的解释器不存在: {exe}")
    print(f"      {cmd}")


@check("autostart/read-only-check")
def test_autostart_read() -> None:
    # 只读检查，不写注册表（避免污染用户的自启项）
    got = gapp.get_autostart()
    _assert(isinstance(got, bool), "get_autostart 必须返回 bool")
    print(f"      当前自启状态: {got}")


# ---------------------------------------------------------------------------
# 界面组件（不启动主窗口）
# ---------------------------------------------------------------------------

@check("ui/widgets")
def test_ui_widgets() -> None:
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    try:
        card = gapp.Card(root, "标题")
        card.pack()
        card.set_right("右侧状态")
        bar = gapp.StatBar(root, "占用", gapp.ACCENT)
        bar.pack()
        bar.set(42.0, "42%")
        bar.set(0.0)
        bar.set(100.0)
        btn = gapp.ModeButton(root, "按钮", gapp.ACCENT, command=lambda: None)
        btn.pack()
        btn.set_selected(True)
        btn.set_selected(False)
        btn.set_disabled(True)
        btn._click()  # 置灰后点击不该触发
        btn.set_disabled(False)
        root.update_idletasks()
    finally:
        root.destroy()


@check("ui/palette")
def test_ui_palette() -> None:
    for name in ("BG", "CARD", "BORDER", "TEXT", "DIM", "ACCENT", "GREEN", "ORANGE", "BAR_TRACK"):
        val = getattr(gapp, name, None)
        _assert(isinstance(val, str) and val.startswith("#") and len(val) == 7,
                f"颜色常量 {name}={val!r} 不是 #RRGGBB")


@check("ui/disabled-modes")
def test_ui_mode_gating() -> None:
    # 界面的置灰判定必须与硬件能力一致：没有固件通道就不能声称能切
    import inspect

    src = inspect.getsource(gapp.App._tick)
    _assert("can_eco" in src and "can_mux" in src, "界面没有按硬件能力置灰按钮")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@check("cli/version-and-help")
def test_cli() -> None:
    py = sys.executable
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    for args in (["--version"], ["--help"]):
        cp = subprocess.run([py, "-m", "gpucore", *args], capture_output=True,
                            encoding="utf-8", errors="replace", timeout=30, env=env)
        _assert(cp.returncode == 0, f"`gpucore {' '.join(args)}` 退出码 {cp.returncode}: {cp.stderr[-300:]}")
    cp = subprocess.run([py, "-m", "gpucore", "--check"], capture_output=True,
                        encoding="utf-8", errors="replace", timeout=120, env=env)
    _assert("自检" in cp.stdout or "GpuCore" in cp.stdout, f"--check 没输出报告: {cp.stdout[:200]}")
    _assert("✓" in cp.stdout or "全部通过" in cp.stdout, f"--check 报告没走到结论（可能崩在中途）:\n{cp.stdout[-400:]}")
    _assert(cp.returncode == 0, f"--check 未通过：\n{cp.stdout[-1500:]}\n{cp.stderr[-500:]}")


@check("cli/report")
def test_cli_report() -> None:
    """--report 是给别的华硕机型owner用的，必须在任何机器上都能跑出一份完整报告。

    关键在于「ATKACPI 打不开时也不能崩」：非华硕机器、或没装 ASUS 驱动的机器上，
    这份报告正是用户拿来提问的那份东西，崩了就等于没有反馈通道。
    """
    cp = subprocess.run([sys.executable, "-m", "gpucore", "--report"],
                        capture_output=True, encoding="utf-8", errors="replace",
                        timeout=300, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    out = cp.stdout
    _assert(cp.returncode == 0, f"--report 退出码 {cp.returncode}:\n{cp.stderr[-500:]}")
    for section in ("## 机器", "## 显卡", "## 机型自适应结果", "## 候选设备号探测"):
        _assert(section in out, f"报告缺少「{section}」段:\n{out[:600]}")

    # 候选表必须两种结果都在：支持的报 raw，不支持的也要列出来。
    # 只列命中的号，看的人就不知道「哪些候选被试过但没中」了。
    _assert("支持" in out, "候选表里一个命中的候选都没有（本机至少应命中一个）")
    _assert("不支持" in out or "打不开" in out, "候选表没有报出未命中的候选")

    # 打不开通道时给出明确的下一步，而不是只报错
    if "打不开" in out:
        _assert("G-Helper" in out or "MyASUS" in out,
                "ATKACPI 打不开时没有告诉用户怎么补驱动")


@check("cli/unknown-arg")
def test_cli_unknown() -> None:
    cp = subprocess.run([sys.executable, "-m", "gpucore", "--definitely-not-a-flag"],
                        capture_output=True, encoding="utf-8", errors="replace", timeout=30,
                        env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    _assert(cp.returncode != 0, "未知参数应当报错退出")
    _assert("未知参数" in (cp.stdout + cp.stderr), f"未知参数没有明确报错: {cp.stdout!r} {cp.stderr!r}")


# ---------------------------------------------------------------------------
# 写操作（默认跳过，只有 --write 才跑，会真的切显卡！）
# ---------------------------------------------------------------------------

@check("write/switch-eco")
def test_write_eco() -> None:
    if "--write" not in sys.argv:
        print("      （跳过：加 --write 才会真的写 ACPI）")
        return
    acpi = hardware.AtkAcpi()
    if not acpi.open():
        print("      （无 ACPI 通道：跳过）")
        return
    dm = hardware.probe_devices(acpi)
    acpi.close()
    if dm.eco is None:
        print("      （本机无 eco 设备号：跳过）")
        return
    mon = gapp.Monitor()
    logs: List[str] = []
    try:
        before = gapp.nvidia_available()
        print(f"      切换前 nvidia-smi 可用: {before}")
        ok = mon.switch_eco(True, logs.append)   # 关独显
        _assert(ok, f"关独显失败：{logs[-3:]}")
        time.sleep(2)
        print(f"      关独显后 nvidia-smi 可用: {gapp.nvidia_available()}（预期 False）")
        ok = mon.switch_eco(False, logs.append)  # 开独显
        _assert(ok, f"开独显失败：{logs[-3:]}")
        time.sleep(3)
        _assert(gapp.nvidia_available(), "开独显后 nvidia-smi 仍不可用，独显没回来")
        print("      双向切换成功")
    finally:
        print("      日志:", *logs[-6:], sep="\n        ")


# ---------------------------------------------------------------------------
# 跑
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    keyword = ""
    if "-k" in argv:
        i = argv.index("-k")
        if i + 1 < len(argv):
            keyword = argv[i + 1]

    # 主窗口不要弹出来
    os.environ.setdefault("GPUCORE_NO_GUI", "1")

    # 本套件会打印被测代码的原样输出（比如 hardware.DeviceMap.describe() 里的 ✓/✗）。
    # 中文 Windows 的控制台默认是 GBK，碰上 ✓ 会直接抛
    #   UnicodeEncodeError: 'gbk' codec can't encode character '\u2713'
    # 把一条用例判成失败 —— 纯属误报。先切 UTF-8，切不动就退化成替代字符。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    selected = [(n, f) for n, f in CHECKS if not keyword or keyword in n]
    print(f"GpuCore 自测：{len(selected)} 项"
          + (f"（筛选 {keyword!r}）" if keyword else "")
          + ("  [含写 ACPI 用例]" if "--write" in argv else ""))
    print("=" * 72)

    failed: List[Tuple[str, str]] = []
    t0 = time.time()
    for name, fn in selected:
        t = time.time()
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"  [失败] {name}  ({time.time() - t:.2f}s)")
            print("         " + "\n         ".join(traceback.format_exc().strip().splitlines()[-4:]))
        else:
            print(f"  [通过] {name}  ({time.time() - t:.2f}s)")

    print("=" * 72)
    print(f"{len(selected) - len(failed)}/{len(selected)} 通过，用时 {time.time() - t0:.1f}s")
    if failed:
        print("\n失败清单：")
        for name, msg in failed:
            print(f"  * {name}: {msg}")
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
