
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

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gpucore import branding, hardware
from gpucore import app as gapp

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
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


WRITE_FLAG = "--write"
_ENFORCE_WRITE = False
_ACTIVE_KEYWORD = ""


def _looks_like_write_only() -> bool:
    kw = _ACTIVE_KEYWORD
    if not kw:
        return False
    sel = [n for n, _ in CHECKS if kw in n]
    return bool(sel) and all(n.startswith("write/") for n in sel)


def _write_guard_ok() -> bool:
    if _ENFORCE_WRITE:
        return True
    if _looks_like_write_only():
        raise AssertionError(
            f"-k {_ACTIVE_KEYWORD!r} 只选中了写操作用例，却没给 {WRITE_FLAG}："
            f"这些用例会全部跳过，而报告仍会说「全部通过」——"
            f"那份「通过」什么都没验证。要真跑请加 {WRITE_FLAG}（会切显卡），"
            f"否则请换一个筛选词。")
    return False


def _write_allowed() -> bool:
    return _write_guard_ok()


def _set_run_flags(enforce_write: bool = False, keyword: str = "") -> None:
    global _ENFORCE_WRITE, _ACTIVE_KEYWORD
    _ENFORCE_WRITE = enforce_write
    _ACTIVE_KEYWORD = keyword

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
    p = branding.bundle_dir() / "install.ps1"
    _assert(p.exists(), f"找不到 {p}")
    src = p.read_text(encoding="utf-8")

    _assert("Read-Branding" in src, "install.ps1 没有从 branding.py 读品牌信息")

    bad = re.search(r"\$(?:AppName|AppNameCn|InstallDir)\s*=\s*['\"]", src)
    _assert(not bad, f"install.ps1 又把品牌名硬写回去了: {bad.group(0) if bad else ''}")

    for key in ("APP_NAME", "APP_NAME_CN", "APP_DIR_NAME"):
        _assert(key in src, f"install.ps1 没读 {key}")


@check("suite/no-orphan-tests")
def test_no_orphan_tests() -> None:
    me = Path(__file__).resolve()
    lines = me.read_text(encoding="utf-8").splitlines()
    orphans = []
    for i, line in enumerate(lines):
        m = re.match(r"^def (test_\w+)\s*\(", line)
        if not m:
            continue
        j = i - 1
        while j >= 0 and not lines[j].strip():
            j -= 1
        if j < 0 or not lines[j].lstrip().startswith("@check("):
            orphans.append(f"第 {i + 1} 行 {m.group(1)}")
    _assert(not orphans,
            "这些用例函数没有 @check 装饰器，永远不会执行：" + "、".join(orphans))

    names = re.findall(r'^@check\("([^"]+)"\)', me.read_text(encoding="utf-8"), re.M)
    dupes = sorted({n for n in names if names.count(n) > 1})
    _assert(not dupes, f"用例名字重复，报告里分不清是哪条：{dupes}")
    _assert(len(names) >= 30, f"只注册到 {len(names)} 条用例，套件是不是被误删了")


@check("ps1/utf8-bom")
def test_ps1_has_bom() -> None:
    root = branding.bundle_dir()
    files = subprocess.run(["git", "-c", "core.quotePath=false", "-C", str(root), "ls-files", "*.ps1"],
                           capture_output=True, text=True, encoding="utf-8", timeout=30)
    _assert(files.returncode == 0, f"git ls-files 失败：{files.stderr.strip()[:200]}")
    scripts = [r.strip() for r in files.stdout.splitlines() if r.strip() and not r.startswith("archive/")]
    _assert(scripts, "一个 .ps1 都没找到，路径或 git 参数有问题")

    missing = []
    for rel in scripts:
        p = root / rel
        if not p.is_file():
            continue
        head = p.read_bytes()[:3]
        if head != b"\xef\xbb\xbf":
            missing.append(rel)
    _assert(not missing,
            "这些 .ps1 没有 UTF-8 BOM，中文 Windows 上 PowerShell 5.1 会按 GBK 解码、"
            "报出位置完全不对的语法错误：" + "、".join(missing))


@check("selftest/keyword-must-match")
def test_keyword_fail_closed() -> None:
    root = branding.bundle_dir()
    cp = subprocess.run([sys.executable, "-m", "gpucore.tests.test_gpucore", "-k", "不会存在的用例名"],
                        cwd=str(root), capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=120)
    _assert(cp.returncode != 0,
            f"筛选词没命中却返回成功（退出码 {cp.returncode}）：{cp.stdout.strip()[:300]}")
    _assert("没有匹配" in cp.stdout, f"没说清为什么没跑任何用例：{cp.stdout.strip()[:300]}")
    _assert("全部通过" not in cp.stdout, "零用例却印出了「全部通过」")


@check("selftest/no-sibling-device-table")
def test_no_stale_device_table() -> None:
    src = (branding.bundle_dir() / "gpucore" / "selftest.py").read_text(encoding="utf-8")
    _assert("DEVICES =" not in src,
            "selftest.py 里又出现了并列的设备号表（DEVICES = ...）；"
            "设备号只该定义在 gpucore/hardware.py，自检请直接遍历它")


@check("branding/portable-mode")
def test_portable_mode() -> None:
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


@check("branding/version-single-source")
def test_version_single_source() -> None:
    root = branding.bundle_dir()
    files = subprocess.run(["git", "-c", "core.quotePath=false", "-C", str(root), "ls-files"],
                           capture_output=True, text=True, encoding="utf-8", timeout=30)
    _assert(files.returncode == 0, f"git ls-files 失败：{files.stderr.strip()[:200]}")

    wanted = branding.APP_VERSION
    brand = re.compile(branding.APP_NAME)
    ver_re = re.compile(r"\bv?(\d+\.\d+\.\d+)\b")

    stale: list[str] = []
    seen_with_version: set[str] = set()
    for rel in files.stdout.splitlines():
        rel = rel.strip()
        if not rel or rel.startswith("archive/"):
            continue
        p = root / rel
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for raw in text.splitlines():
            if "git tag" in raw or not brand.search(raw):
                continue
            m = ver_re.search(raw)
            if not m:
                continue
            if m.group(1) == wanted:
                seen_with_version.add(rel)
                continue
            stale.append(f"{rel} 这一行写着 {m.group(1)}（当前是 {wanted}）：{raw.strip()[:70]}")

    _assert(not stale,
            "版本号漂了，改 branding.APP_VERSION 时要一起改：\n      "
            + "\n      ".join(sorted(stale)))

    for req in ("README.md", "使用说明.txt"):
        _assert(req in seen_with_version,
                f"{req} 里找不到写着当前版本 {wanted} 的 GpuCore 那一行"
                f" —— 用户拿到包不知道装的是哪版")

    notes = root / "RELEASE-NOTES.md"
    if notes.exists():
        head = notes.read_text(encoding="utf-8").strip().splitlines()[0]
        _assert(wanted in head,
                f"RELEASE-NOTES.md 第一行是 {head[:60]!r}，没写当前版本 {wanted}"
                f" —— 发布说明过期了，GitHub Release 上会贴错内容")

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
    _assert(0x00090020 in eco, "缺少 ROG/TUF 的独显供电设备号")
    _assert(0x00090120 in eco, "缺少 VivoBook/Zenbook 的独显供电设备号")
    _assert(0x00090016 in mux, "缺少 ROG/TUF 的显示通路设备号")
    _assert(0x00090026 in mux, "缺少 VivoBook/Zenbook 的显示通路设备号")
    for key in ("temp_cpu", "temp_gpu", "perf_mode"):
        _assert(key in hardware.SENSOR_DEVICES, f"传感器缺少 {key}")


@check("hardware/candidate-order-is-stable")
def test_candidate_order() -> None:
    _assert(hardware.ECO_CANDIDATES[0][0] == 0x00090020,
            f"独显供电首选号被改动了：{hardware.ECO_CANDIDATES[0][0]:#010x}"
            f" —— 新机型请追加到列表末尾")
    _assert(hardware.MUX_CANDIDATES[0][0] == 0x00090016,
            f"显示通路首选号被改动了：{hardware.MUX_CANDIDATES[0][0]:#010x}"
            f" —— 新机型请追加到列表末尾")
    for label, cands in (("ECO", hardware.ECO_CANDIDATES), ("MUX", hardware.MUX_CANDIDATES)):
        devs = [d for d, _ in cands]
        _assert(len(devs) == len(set(devs)), f"{label}_CANDIDATES 里有重复的设备号：{devs}")
        _assert(len(cands) >= 2, f"{label}_CANDIDATES 至少要有两个候选（ROG/TUF 与 VivoBook 各一）")
        for dev, line in cands:
            _assert(isinstance(line, str) and line.strip(),
                    f"{label} 候选 {dev:#010x} 没有产品线说明，报告里就说不清它来自哪个机型")


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
    import inspect
    import re

    write_checks = [(n, f) for n, f in CHECKS if n.startswith("write/")]
    _assert(write_checks, "一条 write/ 用例都没有？开关的语义没人守了")
    for name, fn in write_checks:
        src = inspect.getsource(fn)
        _assert("_write_allowed()" in src,
                f"{name} 没有用 _write_allowed() 把关 —— 它会无视有没有 {WRITE_FLAG} 就写 ACPI")
        _assert(re.search(r"\bsys\.argv\b\s*\[", src) is None,
                f"{name} 直接读了 sys.argv —— 程序化调用 main([...]) 时开关会失效，"
                f"应当改用 _write_allowed()")

    for name, fn in CHECKS:
        if name.startswith("write/"):
            continue
        src = inspect.getsource(fn)
        for bad in (r"\bswitch_eco\s*\(", r"\bswitch_mux\s*\(",
                    r"(?m)^\s*def\s+write_eco\s*\(", r"(?m)^\s*def\s+write_mux\s*\("):
            _assert(re.search(bad, src) is None,
                    f"只读用例 {name} 里出现了 {bad}，会真的改显卡状态")

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

@check("hotkey/parse")
def test_hotkey_parse() -> None:
    spec = gapp.parse_hotkey("ctrl+alt+g")
    _assert(spec.vk == ord("G"), f"主键解析错: {spec.vk}")
    _assert(spec.modifiers == (gapp.MOD_CONTROL | gapp.MOD_ALT), f"修饰键解析错: {spec.modifiers}")
    _assert(spec.describe() == "Ctrl+Alt+G", f"显示文本错: {spec.describe()}")
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

@check("config/roundtrip")
def test_config_roundtrip() -> None:
    original = gapp.CONFIG_PATH
    tmp = Path(tempfile.mkdtemp(prefix="gpucore-cfg-"))
    if not _dir_writable(tmp):
        print("      （当前环境不允许写临时目录：跳过）")
        return
    try:
        gapp.CONFIG_PATH = tmp / "config.json"
        _assert(gapp.load_config()["hotkey"] == "ctrl+alt+g", "默认热键不对")
        gapp.save_config({"hotkey": "ctrl+shift+k", "always_on_top": False})
        cfg = gapp.load_config()
        _assert(cfg["hotkey"] == "ctrl+shift+k", "保存后读回不一致")
        _assert(cfg["always_on_top"] is False, "布尔值没保存")
        _assert(not (tmp / "config.json.tmp").exists(), "原子写的临时文件没清掉")
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
    got = gapp.get_autostart()
    _assert(isinstance(got, bool), "get_autostart 必须返回 bool")
    print(f"      当前自启状态: {got}")

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
        btn._click()
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
    import inspect

    src = inspect.getsource(gapp.App._tick)
    _assert("can_eco" in src and "can_mux" in src, "界面没有按硬件能力置灰按钮")

@check("packaging/launcher-console-flags")
def test_launcher_console_flags() -> None:
    root = branding.bundle_dir()
    cs = root / "packaging" / "launcher.cs"
    if not cs.exists():
        return
    src = cs.read_text(encoding="utf-8", errors="replace")

    flags = set(re.findall(r'\bs\s*==\s*"(--[a-z][a-z\-]*)"', src))
    _assert(flags, "launcher.cs 里一个 CLI 开关都没读到，正则可能失效了")

    block = re.search(r"bool consoleMode = false;(.*?)\n        \}", src, re.S)
    _assert(block, "launcher.cs 里找不到 consoleMode 判定块")
    body = block.group(1)
    whitelisted = set(re.findall('"(--[a-z][a-z\\-]*)"', body))
    _assert(whitelisted, "consoleMode 白名单是空的")

    missing = sorted(f for f in flags if f not in whitelisted)
    _assert(not missing,
            f"launcher.cs 判定到的开关 {missing} 不在 consoleMode 白名单里，"
            f"这些参数从 GpuCore.exe 跑会静默无输出。白名单现有：{sorted(whitelisted)}")

    app_src = (root / "gpucore" / "app.py").read_text(encoding="utf-8", errors="replace")
    km = re.search(r"known\s*=\s*\{([^}]*)\}", app_src)
    _assert(km, "app.py 里找不到 known 集合")
    known = set(re.findall(r'"(--[a-z][a-z\-]*)"', km.group(1)))
    _assert(known, "app.py 的 known 集合解析为空")
    not_in_launcher = sorted(known - flags)
    _assert(not not_in_launcher,
            f"app.py 认识但 launcher.cs 没判定到的开关：{not_in_launcher} —— "
            f"这些参数从 GpuCore.exe 调用会静默无输出")


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
    out = cp.stdout
    _assert("自检" in out or "GpuCore" in out, f"--check 没输出报告: {out[:200]}")
    _assert("结果：" in out, f"--check 报告没走到结论（可能被中途的异常带走了）:\n{out[-500:]}")
    _assert("（这一组崩了）" not in out,
            f"--check 有检查组整体崩溃，报告是残的:\n{out[-1500:]}")
    _assert("[FAIL]" not in out,
            f"自检把平台不适用的情况记成了失败（应当记 WARN）:\n{out[-1500:]}")
    _assert(cp.returncode == 0, f"--check 未通过：\n{out[-1500:]}\n{cp.stderr[-500:]}")


@check("selftest/group-crash-is-contained")
def test_selftest_resilience() -> None:
    from gpucore.selftest import INFO, _run_group, print_report, run_self_test
    from io import StringIO
    import contextlib

    rep = run_self_test()
    _assert(rep.items, "自检一项都没产出")
    _assert(any(i.name.startswith("GpuCore 自检") for i in rep.items), "缺少抬头项")

    def boom(_rep):
        raise RuntimeError("模拟某组检查崩了")

    _run_group(rep, "模拟组", boom)
    names = [i.name for i in rep.items]
    _assert("模拟组（这一组崩了）" in names, f"崩溃的组没有被记成一项: {names[-4:]}")
    detail = " ".join(i.detail or "" for i in rep.items)
    _assert("模拟某组检查崩了" in detail, "崩溃原因没写进报告")

    buf = StringIO()
    with contextlib.redirect_stdout(buf):
        code = print_report(rep)
    text = buf.getvalue()
    _assert("结果：" in text, "崩溃之后报告没走到结论")
    _assert(code == 1, "有失败项时 print_report 应当返回 1")


@check("cli/report")
def test_cli_report() -> None:
    cp = subprocess.run([sys.executable, "-m", "gpucore", "--report"],
                        capture_output=True, encoding="utf-8", errors="replace",
                        timeout=300, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    out = cp.stdout
    _assert(cp.returncode == 0, f"--report 退出码 {cp.returncode}:\n{cp.stderr[-500:]}")
    for section in ("## 机器", "## 显卡", "## 机型自适应结果", "## 候选设备号探测"):
        _assert(section in out, f"报告缺少「{section}」段:\n{out[:600]}")

    _assert("支持" in out or "打不开" in out, "候选表既没说命中也说打不开，看不出探测结果")
    if "打不开" not in out:
        _assert("不支持" in out, "候选表只列了命中的号，没报出试过但未命中的候选")

    if "打不开" in out:
        _assert("G-Helper" in out or "MyASUS" in out,
                "ATKACPI 打不开时没有告诉用户怎么补驱动")

    for section in ("## 机器", "## 显卡", "## 机型自适应结果",
                    "## 候选设备号探测", "## ATKACPI"):
        got = out.count(section)
        _assert(got <= 1, f"「{section}」在报告里出现了 {got} 次（每个小节只能有一次）")

    footer = [x for x in out.splitlines() if "issues 即可" in x]
    _assert(len(footer) == 1, f"收尾指引有 {len(footer)} 条（只该有一条）：{footer}")
    _assert(branding.APP_REPO in footer[0],
            f"收尾指引里的仓库地址不对（应来自 branding.APP_REPO）：{footer[0]}")

    _assert("显示类设备状态" in out, "报告没有显示类设备状态那一行")


@check("selftest/report-acpi-unavailable")
def test_report_without_acpi() -> None:

    class _DeadAcpi:
        def open(self) -> bool:
            return False

    from gpucore import selftest

    rep = selftest.build_machine_report(acpi_factory=_DeadAcpi)
    for section in ("## 机器", "## 显卡", "## 机型自适应结果",
                    "## 候选设备号探测", "## 设备号全扫（关键，请勿删）"):
        _assert(section in rep, f"打不开通道时报告缺少「{section}」段")

    _assert(rep.count("## ATKACPI") == 1,
            f"「## ATKACPI」出现了 {rep.count('## ATKACPI')} 次（只该有一次）")

    from gpucore.hardware import ECO_CANDIDATES, MUX_CANDIDATES
    for dev, _line in list(ECO_CANDIDATES) + list(MUX_CANDIDATES):
        _assert(f"0x{dev:08X}" in rep, f"打不开通道时漏掉了候选号 0x{dev:08X}")
    _assert("打不开通道" in rep, "候选表没说清这些号是没能探测，还是真的不支持")

    _assert("通道打不开，这一段没法扫" in rep,
            "打不开通道时没有说明「设备号全扫」为什么是空的")


@check("cli/unknown-arg")
def test_cli_unknown() -> None:
    cp = subprocess.run([sys.executable, "-m", "gpucore", "--definitely-not-a-flag"],
                        capture_output=True, encoding="utf-8", errors="replace", timeout=30,
                        env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    _assert(cp.returncode != 0, "未知参数应当报错退出")
    _assert("未知参数" in (cp.stdout + cp.stderr), f"未知参数没有明确报错: {cp.stdout!r} {cp.stderr!r}")

@check("write/flag-is-fail-closed")
def test_write_flag_fail_closed() -> None:
    kw = "switch-eco"
    assert callable(main)
    _set_run_flags()

    _set_run_flags(False, kw)
    _assert(_looks_like_write_only(), f"-k {kw!r} 应当被认成「只跑了写用例」")
    try:
        _write_guard_ok()
    except AssertionError as exc:
        _assert(WRITE_FLAG in str(exc), f"报错里没说清该加哪个开关：{exc}")
    else:
        raise AssertionError(f"-k {kw} 没给 {WRITE_FLAG}，却静默通过了")

    _set_run_flags(True, kw)
    _assert(_write_allowed(), f"给了 {WRITE_FLAG} 却没把开关打开")

    _set_run_flags(False, "hardware")
    _assert(not _looks_like_write_only(), "-k hardware 被误判成只跑了写用例")
    _assert(not _write_allowed(), "-k hardware 竟然被允许写 ACPI")

    _set_run_flags(False, "write")
    _assert(not _looks_like_write_only(),
            "-k write 被误判成只跑了写用例 —— 它明明还选中了 hardware/no-write-by-default")
    _assert(not _write_allowed(), "-k write 竟然被允许写 ACPI")

    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(main)))
    assigns = [n for n in ast.walk(tree)
               if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "_ENFORCE_WRITE" for t in n.targets)]
    _assert(assigns, "main() 里找不到对 _ENFORCE_WRITE 的赋值 —— 开关没人设了")
    _assert(any(isinstance(sub, ast.Name) and sub.id == "argv"
                for n in assigns for sub in ast.walk(n.value)),
            "main() 设开关时没用自己的 argv 参数（又回去读进程参数了？）")

    real_argv = list(sys.argv)
    try:
        sys.argv = ["test_gpucore"]
        _set_run_flags(False, kw)
        try:
            _write_allowed()
        except AssertionError:
            pass
        else:
            raise AssertionError(
                "_write_allowed() 的结果受 sys.argv 影响了 —— 它本该只看 _ACTIVE_KEYWORD")
    finally:
        sys.argv = real_argv

    _set_run_flags()
    _assert(not _write_allowed(), "运行态没还回去：复位之后仍被认为允许写 ACPI")


@check("write/switch-eco")
def test_write_eco() -> None:
    if not _write_allowed():
        print(f"      （跳过：加 {WRITE_FLAG} 才会真的写 ACPI）")
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
        ok = mon.switch_eco(True, logs.append)
        _assert(ok, f"关独显失败：{logs[-3:]}")
        time.sleep(2)
        print(f"      关独显后 nvidia-smi 可用: {gapp.nvidia_available()}（预期 False）")
        ok = mon.switch_eco(False, logs.append)
        _assert(ok, f"开独显失败：{logs[-3:]}")
        time.sleep(3)
        _assert(gapp.nvidia_available(), "开独显后 nvidia-smi 仍不可用，独显没回来")
        print("      双向切换成功")
    finally:
        print("      日志:", *logs[-6:], sep="\n        ")

def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    keyword = ""
    if "-k" in argv:
        i = argv.index("-k")
        if i + 1 < len(argv):
            keyword = argv[i + 1]

    global _ENFORCE_WRITE, _ACTIVE_KEYWORD
    _ENFORCE_WRITE = WRITE_FLAG in argv
    _ACTIVE_KEYWORD = keyword

    os.environ.setdefault("GPUCORE_NO_GUI", "1")

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    selected = [(n, f) for n, f in CHECKS if not keyword or keyword in n]

    if keyword and not selected:
        print(f"筛选词 {keyword!r} 没有匹配到任何用例：{len(CHECKS)} 项里的名字都不会被它选中。")
        print("可用名字（也可只写其中一段，例如 hardware）：")
        print("  " + "\n  ".join(n for n, _ in CHECKS))
        return 2

    print(f"GpuCore 自测：{len(selected)} 项"
          + (f"（筛选 {keyword!r}）" if keyword else "")
          + ("  [含写 ACPI 用例]" if "--write" in argv else ""))
    print("=" * 72)

    if not keyword:
        _assert(len(selected) == len(CHECKS),
                f"没给筛选词，却只选中 {len(selected)}/{len(CHECKS)} 项")

    failed: List[Tuple[str, str]] = []
    t0 = time.time()
    for name, fn in selected:
        t = time.time()
        try:
            fn()
        except Exception as exc:
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
