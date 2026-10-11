"""GpuCore 自检 —— 在新机器上排查"为什么不能用/读数不对"。

跑法：
    python -m gpucore --check

逐项检查：Python 环境 → 平台 → ASUS ACPI 通道 → 各设备号可读性 →
独显是否可用 → DXGI/PDH 占用读数 → 全局热键是否注册得上。

输出是给人看的，不抛异常：某一项失败就标红继续。
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass, field
from typing import List, Optional

from . import branding

OK, WARN, FAIL, INFO = "OK", "WARN", "FAIL", "INFO"
_MARK = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", INFO: "[ -- ]"}


@dataclass
class Item:
    name: str
    level: str
    detail: str = ""


@dataclass
class Report:
    items: List[Item] = field(default_factory=list)

    def add(self, name: str, level: str, detail: str = "") -> Item:
        it = Item(name, level, detail)
        self.items.append(it)
        return it

    @property
    def failed(self) -> List[Item]:
        return [i for i in self.items if i.level == FAIL]

    @property
    def warned(self) -> List[Item]:
        return [i for i in self.items if i.level == WARN]


# 设备号表**只此一份**，在 gpucore/hardware.py：
#   * 独显供电 / 显示通路 —— ECO_CANDIDATES、MUX_CANDIDATES（按产品线候选，逐个探测）
#   * 温度 / 功耗 / 电池 / 外接 —— SENSOR_DEVICES
# 这里以前还抄过一份 DEVICES 表，但它从头到尾没有任何地方引用过，
# 作用只有一个：让人以为改了它就能加设备号（其实改了不会生效），
# 而且会和 hardware.py 里的真表越来越不一致。已删除，别再抄回来。
#
# 「设备号全扫」那一段（print_machine_report 里）覆盖的是全 0x0009/0x0012 段，
# 比任何手抄表都全，也不需要维护。


def _check_environment(rep: Report) -> None:
    v = sys.version_info
    rep.add("Python 版本", OK if v >= (3, 9) else FAIL,
            f"{v.major}.{v.minor}.{v.micro} @ {sys.executable}")

    try:
        import tkinter  # noqa: F401
        rep.add("tkinter", OK, "可用")
    except Exception as e:
        rep.add("tkinter", FAIL, f"不可用（装 Python 时要勾 tcl/tk）：{e}")

    rep.add("平台", OK if sys.platform.startswith("win") else FAIL, sys.platform)

    try:
        is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        is_admin = False
    rep.add("管理员权限", INFO,
            "是" if is_admin else "否（本程序不需要管理员）")


def _check_acpi(rep: Report) -> Optional[object]:
    """探测固件通道 + 机型自适应地找出可用设备号。

    不写死设备号：不同华硕产品线的号不一样，所以这里逐个候选探测，
    并把**实际选中的**号报出来（用户拿去反馈时这个信息最有用）。
    """
    from .app import AtkAcpi
    from .hardware import ECO_CANDIDATES, MUX_CANDIDATES, SENSOR_DEVICES, probe_devices

    acpi = AtkAcpi()
    if not acpi.open():
        # 这里必须是 WARN 而不是 FAIL：自检的 FAIL 含义是"这台机器上 GpuCore 坏了"，
        # 而"不是华硕/没装 ASUS 驱动"是**平台不适用**，不是缺陷。
        # 记成 FAIL 的后果实测过两次：非华硕机器上 --check 返回 1，
        # GitHub Actions 的 windows runner 因此永远红（虚机里当然没有华硕固件通道）。
        rep.add("\\\\.\\ATKACPI 通道", WARN,
                "打不开 —— 这台机器不是华硕，或缺少 ASUS 驱动（装 G-Helper 或 MyASUS 会带上）。"
                "此版本固件相关功能在本机不可用。")
        return None
    rep.add("\\\\.\\ATKACPI 通道", OK, "可打开（普通用户权限即可）")

    dm = probe_devices(acpi)

    # 逐个候选报出探测结果，方便看清为什么选了某个号
    for label, cands in (("独显供电", ECO_CANDIDATES), ("显示通路", MUX_CANDIDATES)):
        for dev, line in cands:
            val = acpi.get_raw(dev)
            if val is None:
                rep.add(f"  {label}候选 0x{dev:08X}", INFO, f"不支持（{line}）")
            else:
                rep.add(f"  {label}候选 0x{dev:08X}", OK, f"支持（{line}）raw=0x{val:X}")

    if dm.can_eco:
        rep.add("独显供电切换", OK, f"0x{dm.eco:08X}（{dm.eco_line}）")
    else:
        rep.add("独显供电切换", WARN, "本机没有可用的 GPUEco 设备号")
    if dm.can_mux:
        rep.add("显示通路切换", OK, f"0x{dm.mux:08X}（{dm.mux_line}）")
    else:
        rep.add("显示通路切换", INFO, "本机没有 MUX 开关（很多轻薄本确实没有，属正常）")

    for key, dev in dm.sensors.items():
        try:
            val = acpi.get(dev)
        except Exception as e:
            # 传感器读失败只该是这一项的问题，不该影响后面的检查
            rep.add(f"  传感器 {SENSOR_DEVICES[key][1]}", WARN,
                    f"0x{dev:08X} 读不到（{type(e).__name__}: {e}）")
            continue
        rep.add(f"  传感器 {SENSOR_DEVICES[key][1]}", OK, f"0x{dev:08X} = {val}")

    for note in dm.notes:
        rep.add("  机型适配", INFO, note)

    return acpi


def _check_gpus(rep: Report) -> None:
    # 独显
    try:
        from .app import nvidia_available
        ok = nvidia_available()
        rep.add("NVIDIA 独显", OK if ok else WARN,
                "nvidia-smi 可读，独显已通电" if ok
                else "读不到（独显可能正处于断电省电状态；用「标准模式」可唤醒）")
    except Exception as e:
        rep.add("NVIDIA 独显", FAIL, f"检测异常: {e}")

    # 核显 LUID
    try:
        from .app import detect_igpu_luid
        luid = detect_igpu_luid()
        rep.add("核显 LUID", OK if luid else WARN, luid or "没找到 Intel/AMD 核显")
    except Exception as e:
        rep.add("核显 LUID", FAIL, f"DXGI 枚举失败: {e}")

    # 占用读数
    try:
        from .app import FastSystem
        fs = FastSystem()
        fs.cpu_percent()
        import time
        time.sleep(0.35)
        cpu = fs.cpu_percent()
        used, total = fs.memory()
        rep.add("CPU 占用读数", OK if cpu >= 0 else FAIL, f"{cpu:.0f}%")
        rep.add("内存读数", OK if total else FAIL,
                f"{used / 1024**3:.1f} GB / {total / 1024**3:.1f} GB")
    except Exception as e:
        rep.add("系统指标读数", FAIL, f"{e}")


def _check_hotkey(rep: Report) -> None:
    """试着注册一个热键再释放，确认 RegisterHotKey 通路可用。"""
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
        VK_G = 0x47
        hk_id = 0xC0DE
        ok = user32.RegisterHotKey(
            None, hk_id, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_G)
        if ok:
            user32.UnregisterHotKey(None, hk_id)
            rep.add("全局热键通路", OK, "RegisterHotKey 可用（试注册 Ctrl+Alt+G 成功）")
        else:
            err = ctypes.get_last_error()
            rep.add("全局热键通路", WARN,
                    f"Ctrl+Alt+G 注册失败 (WinError {err})，可能被别的程序占用；程序会自动换组合键")
    except Exception as e:
        rep.add("全局热键通路", FAIL, f"{e}")


def _run_group(rep: Report, name: str, fn) -> None:
    """跑一组检查，保证它**不会把整份报告带走**。

    这条是 GitHub Actions 上真踩出来的：
    windows runner（Hyper-V 虚机、没有 ATKACPI）上 `_check_acpi` 中途抛异常，
    run_self_test 直接把异常抛穿，print_report 根本没被调用 ——
    用户看到的就是一份**从中间截断、没有结论**的报告，等于反馈通道没了。
    自检报告唯一的用处就是"出问题时给人看"，所以它自己绝不允许因为一处异常而变残。
    """
    try:
        fn(rep)
    except Exception as e:
        import traceback
        rep.add(f"{name}（这一组崩了）", FAIL, f"{type(e).__name__}: {e}")
        for line in traceback.format_exc().splitlines()[-4:]:
            rep.add("", INFO, line)


def run_self_test() -> Report:
    rep = Report()
    rep.add("GpuCore 自检", INFO, f"{branding.APP_NAME} v{branding.APP_VERSION}")
    for name, fn in (("运行环境", _check_environment),
                     ("华硕固件通道", _check_acpi),
                     ("显卡与读数", _check_gpus),
                     ("全局热键", _check_hotkey)):
        _run_group(rep, name, fn)
    return rep


def print_report(rep: Report) -> int:
    print()
    print("=" * 66)
    print(f"  {branding.APP_NAME} ({branding.APP_NAME_CN}) 自检  v{branding.APP_VERSION}")
    print("=" * 66)
    for it in rep.items:
        mark = _MARK[it.level]
        print(f"  {mark} {it.name}")
        if it.detail:
            print(f"         {it.detail}")
    print("-" * 66)
    if rep.failed:
        print(f"  结果：{len(rep.failed)} 项失败，{len(rep.warned)} 项警告")
        print("  → [FAIL] 是本程序在这台机器上真的没能工作；[WARN] 多数是平台不适用或缺驱动。")
        print("     请把上面的全部内容贴到 issue 里。")
    elif rep.warned:
        print(f"  结果：全部通过，{len(rep.warned)} 项警告（一般不影响使用）")
    else:
        print("  结果：全部通过 ✓")
    print("=" * 66)
    print()
    return 1 if rep.failed else 0


# ---------------------------------------------------------------------------
# 机型反馈报告
#
# 「换一台华硕就不一定能用」是这类工具的根本短板：设备号是按产品线分的，
# 我手上只有一台 TX Gaming FX608LM。要让别人帮我补全，就得让他们能一键产出一份
# **我能直接拿去改代码**的东西 —— 所以这份报告的重点不是"能不能用"，
# 而是「这台机器的 ATKACPI 上到底有哪些设备号是真的」。
# ---------------------------------------------------------------------------

# 报告里要取的每一项。全部塞进**一次** PowerShell 调用里取（原因见 _wmi_probe 的注释）。
#   单值型：(键, "CLASSEXPR", "字段")  —— CLASSEXPR 会被原样拼进 Get-CimInstance
#   实例型：(键, "CLASSEXPR", ("字段1", "字段2", ...))  —— 每个实例出一行 `[键] a | b`
_REPORT_WMI_FIELDS = [
    ("厂商", "Win32_ComputerSystem", "Manufacturer"),
    ("型号", "Win32_ComputerSystem", "Model"),
    ("系统型号", "Win32_ComputerSystemProduct", "Name"),
    ("BIOS", "Win32_BIOS", "SMBIOSBIOSVersion"),
    ("主板", "Win32_BaseBoard", "Product"),
    ("操作系统", "Win32_OperatingSystem", "Caption"),
    ("系统版本", "Win32_OperatingSystem", "Version"),
    ("显卡", "Win32_VideoController",
     ("Name", "DriverVersion", "PNPDeviceID")),
]

# 显示类设备的 PnP 状态。
#
# 这一项是用来分开「独显断电省电」和「独显驱动坏了」的：断电时设备根本不出现在
# 设备列表里，驱动坏了才会出现并带一个状态码（Code 43 = CM_PROB_FAILED_POST_START）。
# 只看 nvidia-smi 分不出这两者 —— 两种情况下它都读不到。
#
# 用 Win32_PnPEntity（CIM 查询）而不是 Get-PnpDevice（cmdlet）：实测本机上
# `Get-PnpDevice -Class Display` 要 5.7 秒，`Get-CimInstance Win32_PnPEntity
# -Filter "PNPClass='Display'"` 约 1 秒，拿到的 Status/ConfigManagerErrorCode
# 是同一份数据（Error + 43 也读得到）。
_REPORT_PNP_CLASS = "Display"


def _wmi_probe() -> dict:
    """一次 PowerShell 调用拿回报告要用的所有机器信息。

    返回 `{"字段": 压平后的字符串, "显示设备": [每行一个设备]}`。
    读不到就是空串/空列表 —— 调用方负责在报告里写"读不到"。

    为什么非要合成一次调用：每次起 PowerShell 在本机要 0.5 秒起、每个 CIM 查询还要
    再花时间，逐字段各调一次会拖到二十几秒，用户会以为程序卡死了。现在整份报告
    信息大约 8 秒（其中大部分是 CIM 冷启动），所以超时给了 40 秒。
    """
    import subprocess

    lines = ['Write-Output "=== GpuCore 报告 ==="']
    for key, classexpr, what in _REPORT_WMI_FIELDS:
        if isinstance(what, tuple):
            props = ", ".join(f'$_.{p}' for p in what)
            lines.append(f'Get-CimInstance {classexpr} -ErrorAction SilentlyContinue | '
                         f'ForEach-Object {{ Write-Output ("[{key}] " + '
                         f'(@({props}) -join " | ")) }}')
        else:
            lines.append(f'Write-Output ("[{key}] " + (((Get-CimInstance {classexpr} '
                         f'-ErrorAction SilentlyContinue).{what}) -join " | "))')
    lines.append('Write-Output "=== 显示设备 ==="')
    lines.append(f'Get-CimInstance Win32_PnPEntity -Filter "PNPClass=\'{_REPORT_PNP_CLASS}\'" '
                 '-ErrorAction SilentlyContinue | ForEach-Object { Write-Output '
                 '("{0} | {1} | {2}" -f $_.Status, $_.ConfigManagerErrorCode, $_.Name) }')
    script = "; ".join(lines)

    res: dict = {}
    pnp: List[str] = []
    try:
        cp = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=40,
        )
        out = [x.rstrip() for x in (cp.stdout or "").splitlines()]
    except Exception:
        res["显示设备"] = pnp
        return res

    in_pnp = False
    for raw in out:
        line = raw.strip()
        if not line or line == "=== GpuCore 报告 ===":
            continue
        if line == "=== 显示设备 ===":
            in_pnp = True
            continue
        if in_pnp:
            pnp.append(line)
            continue
        if line.startswith("[") and "] " in line:
            label, value = line[1:].split("] ", 1)
            value = value.strip()
            if not value:
                continue
            # 值**按原样**存成一条，不做任何分隔符拆合：虚拟机显示适配器的
            # PNPDeviceID 本身就含 `&`、驱动版本里本来就有多个号，
            # 拿一个字符去"压平再拆开"必然把它们切碎（第一版就是这么错的）。
            # 同一个标签出现多次（多块显卡）就多条。
            res.setdefault(label, []).append(value)
    res["显示设备"] = pnp
    return res


def _one(wmi: dict, key: str, default: str = "读不到") -> str:
    """取一个单值字段；重复出现时用 ` | ` 接起来（只用在确实是一个值的地方）。"""
    vals = wmi.get(key) or []
    return " | ".join(vals) if vals else default


def _scan_acpi(acpi, base: int, span: int = 0x20) -> List[Tuple[int, int]]:
    """扫一段设备号，返回所有「读得到非零 raw」的号。

    ATKACPI 的行为：**不支持的号返回 0，支持的号一定回非零**
    （见 docs/asus-gpu-switching.md）。所以非零就是"这个号在这台机器上存在"。
    反过来，"值为 0"既可能是没有这个号、也可能是这个号的值恰好是 0 ——
    所以报告里只列非零的，不做反向断言。
    """
    hits: List[Tuple[int, int]] = []
    for off in range(span):
        dev = base + off
        try:
            raw = acpi.get_raw(dev)
        except Exception:
            continue
        if raw:  # None（不支持）和 0 都跳过
            hits.append((dev, raw))
    return hits


def build_machine_report(acpi_factory=None) -> str:
    """产出一份可以直接贴到 issue 里的纯文本报告。

    `acpi_factory` 只为测试留的口子：默认真的去开 `\\\\.\\ATKACPI`。
    但"打不开通道"那条分支（非华硕机器）在开发机上是跑不到的，
    而它恰恰是最容易出问题的一段 —— 所以让用例能塞一个假的进来。
    """
    from .app import AtkAcpi
    from .hardware import ECO_CANDIDATES, MUX_CANDIDATES, SENSOR_DEVICES, probe_devices

    make_acpi = acpi_factory or AtkAcpi

    L: List[str] = []
    L.append(f"# {branding.APP_NAME} 机型报告 v{branding.APP_VERSION}")
    L.append("")

    # 一次 PowerShell 调用取回全部机器信息（每个字段各起一个进程要二十几秒）
    wmi = _wmi_probe()
    L.append("## 机器")
    L.append("")
    L.append(f"- 厂商型号   : {_one(wmi, '厂商')} / {_one(wmi, '型号')}")
    L.append(f"- 系统型号   : {_one(wmi, '系统型号')}")
    L.append(f"- BIOS       : {_one(wmi, 'BIOS')}")
    L.append(f"- 主板       : {_one(wmi, '主板')}")
    L.append(f"- 操作系统   : {_one(wmi, '操作系统')} {_one(wmi, '系统版本')}")
    L.append("")

    L.append("## 显卡")
    L.append("")
    # 用 WMI 而不是 DXGI 枚举：这份报告是在**别人的机器**上跑的，
    # WMI 不依赖我们那套手写的 ctypes COM 虚表，出错概率低得多。
    # 显卡可能有几块（核显/独显/虚拟显示器），一块一行，型号和它自己的
    # 驱动版本、设备 ID 对起来看 —— 所以每个实例出一行、字段用 ` | ` 分隔。
    vcs = wmi.get("显卡") or []
    if vcs:
        for line in vcs:
            L.append(f"- {line}")
    else:
        L.append("- WMI 没报出显卡")
    # 各显示设备在 Windows 这边的状态 + 问题码。这一行是用来分开
    # 「独显断电省电」和「独显驱动坏了」的：断电时设备根本不出现，
    # 驱动坏了才会出现并带一个 Error + 问题码（Code 43 就是 CM_PROB_FAILED_POST_START）。
    # 光看 nvidia-smi 是分不出这两者的 —— 两种情况下它都读不到。
    pnp = wmi.get("显示设备") or []
    if pnp:
        L.append("- 显示类设备状态（Status | Problem | 名称）:")
        for line in pnp:
            L.append(f"    - {line}")
    else:
        L.append("- 显示类设备状态: 读不到（Get-PnpDevice 没有输出）")
    try:
        from .app import nvidia_available
        ok = nvidia_available()
        L.append(f"- nvidia-smi 可读: **{ok}**")
        if not ok:
            # 这里不许替用户下结论：False 既可能是"独显正断电省电"（正常），
            # 也可能是"驱动没起来 / 设备故障"。真正的区别就在上面那行设备状态里。
            L.append("  （读不到独显时先看上面那行设备状态：设备不在列表里 = 只是断电省电，属正常；")
            L.append("    出现但状态是 `Error`、且第二个字段是问题码（43 等）= 驱动/设备问题，"
                     "重开机或重启该设备后再看。）")
    except Exception as exc:
        L.append(f"- nvidia-smi 探测失败: {exc}")
    L.append("")

    acpi = make_acpi()
    opened = False
    exc_msg = ""
    try:
        opened = acpi.open()
    except Exception as exc:
        exc_msg = f"{type(exc).__name__}: {exc}"
        opened = False

    if not opened:
        # 这个标题只能出现一次：原来在异常分支里先 append 一个 "## ATKACPI"，
        # 紧接着这里又 append 一个，报告里会出现两个同名小节。
        L.append("## ATKACPI")
        L.append("")
        if exc_msg:
            L.append(f"- **打开 `\\\\.\\ATKACPI` 时抛异常**：{exc_msg}")
        L.append("- **打不开 `\\\\.\\ATKACPI`** —— 这台机器不是华硕，或没装 ASUS 驱动。")
        L.append("  装一次 G-Helper 或 MyASUS 会带上这个驱动，然后重跑。")
        L.append("")
        # 通道不可用时这里**不能提前返回**：环境部分（机型、显卡）和下面这几段
        # 才是这份报告真正要交回来的东西。原来直接 return，非华硕机器上
        # 连「候选设备号探测」都看不到 —— 而那一节正是用来判断"缺少哪个号"的。
        L.append("## 机型自适应结果")
        L.append("")
        L.append("- 独显供电切换 : 不可用（ATKACPI 通道打不开）")
        L.append("- 显示通路切换 : 不可用（ATKACPI 通道打不开）")
        L.append("")
        L.append("## 候选设备号探测")
        L.append("")
        L.append("用**读**的方式试每一个候选号，不写任何东西。")
        L.append("")
        L.append("| 用途 | 设备号 | 产品线 | 结果 | raw |")
        L.append("|---|---|---|---|---|")
        for label, cands in (("独显供电", ECO_CANDIDATES), ("显示通路", MUX_CANDIDATES)):
            for dev, line in cands:
                L.append(f"| {label} | `0x{dev:08X}` | {line} | 打不开通道 | - |")
        L.append("")
        # 标题和正常分支保持一致：报告的小节目录不该因为机器不同而不同。
        # 「设备号全扫」这一段正是补设备号的关键，打不开通道时也要有这一节，
        # 只是说明这次没能扫。
        L.append("## 设备号全扫（关键，请勿删）")
        L.append("")
        L.append("（ATKACPI 通道打不开，这一段没法扫。装好驱动后重跑就有内容了。）")
        L.append("")
        L.append("---")
        L.append("")
        L.append(f"把上面全部内容贴到 {branding.APP_REPO}/issues 即可。")
        return "\n".join(L)

    dm = probe_devices(acpi)

    L.append("## 机型自适应结果")
    L.append("")
    L.append(f"- 独显供电切换 : {('0x%08X' % dm.eco) if dm.can_eco else '不可用'}")
    L.append(f"- 显示通路切换 : {('0x%08X' % dm.mux) if dm.can_mux else '不可用'}")
    for note in dm.notes:
        L.append(f"- 备注         : {note}")
    L.append("")

    L.append("## 候选设备号探测")
    L.append("")
    L.append("| 用途 | 设备号 | 产品线 | 结果 | raw |")
    L.append("|---|---|---|---|---|")
    for label, cands in (("独显供电", ECO_CANDIDATES), ("显示通路", MUX_CANDIDATES)):
        for dev, line in cands:
            raw = acpi.get_raw(dev)
            if raw is None:
                L.append(f"| {label} | `0x{dev:08X}` | {line} | 不支持 | - |")
            else:
                L.append(f"| {label} | `0x{dev:08X}` | {line} | **支持** | `0x{raw:X}` |")
    L.append("")

    L.append("## 传感器")
    L.append("")
    for key, dev in dm.sensors.items():
        try:
            val = acpi.get(dev)
        except Exception:
            val = "读不到"
        L.append(f"- 0x{dev:08X} = {val}   （{SENSOR_DEVICES[key][1]}）")
    L.append("")

    # 这一段是补设备号的关键：把这两段扫全，我就能看出这台机器用的是哪套号
    L.append("## 设备号全扫（关键，请勿删）")
    L.append("")
    L.append("ATKACPI 上「读得到非零」的号就是这台机器真正存在的设备。")
    L.append("")
    for label, base, span in (("0x00090000 段（显卡/显示相关）", 0x00090000, 0x40),
                              ("0x00120000 段（传感器相关）", 0x00120000, 0x80)):
        L.append(f"### {label}")
        L.append("")
        hits = _scan_acpi(acpi, base, span)
        if not hits:
            L.append("（一个都没读到）")
        else:
            for dev, raw in hits:
                L.append(f"- `0x{dev:08X}` = `0x{raw:X}`")
        L.append("")

    L.append("---")
    L.append("")
    L.append("把上面全部内容贴到 " + branding.APP_REPO + "/issues 即可。")
    return "\n".join(L)


def print_machine_report() -> int:
    print()
    print(build_machine_report())
    print()
    return 0
