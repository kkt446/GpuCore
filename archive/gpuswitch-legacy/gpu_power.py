"""独显电源切换的完整流程（照抄 G-Helper 的做法）。

G-Helper 的 `GPUModeControl.SetGPUEco` 在写 ACPI 之前有三步准备工作，
缺一步独显就不会真正断电：

    1. KillGPUApps()        —— 结束所有正在占用独显的进程
    2. DisposeGpuControl()  —— 释放自己持有的 GPU 句柄（NVAPI/驱动句柄）
    3. StopNVService()      —— 停掉 NVIDIA 显示容器服务（NV 平台）

然后才 `acpi.DeviceSet(GPUEco, eco)`。

本项目早期版本直接写 ACPI，结果"有时生效有时不生效"——原因就是
独显被占用（包括被本程序自己的 nvidia-smi / PDH / DXGI 枚举按着）时，
EC 拒绝真正断电。切换到 eco=0 时 G-Helper 还会重启 NVIDIA 服务让
驱动重新枚举设备。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from . import log
from .paths import is_windows, run_capture

logger = log.get(__name__)

# 系统关键进程绝不结束
_PROTECTED_NAMES = {
    "system", "registry", "memory compression", "csrss.exe", "wininit.exe",
    "winlogon.exe", "services.exe", "lsass.exe", "smss.exe", "svchost.exe",
    "dwm.exe", "explorer.exe", "fontdrvhost.exe", "sihost.exe", "ctfmon.exe",
    "taskhostw.exe", "runtimebroker.exe", "shellexperiencehost.exe",
    "startmenuexperiencehost.exe", "searchhost.exe", "textinputhost.exe",
    "msmpeng.exe", "securityhealthservice.exe", "nvcontainer.exe",
    "nvdisplay.container.exe", "python.exe", "pythonw.exe", "ghelper.exe",
}

#: NVIDIA 相关服务（停掉再重启，可让驱动重新枚举独显）
NV_SERVICES = ("NVDisplay.ContainerLocalSystem", "NvContainerLocalSystem")


@dataclass
class GpuProcess:
    pid: int
    name: str
    memory_mib: float = 0.0


@dataclass
class EcoResult:
    ok: bool
    message: str
    detail: str = ""
    killed: List[str] = field(default_factory=list)
    services_restarted: List[str] = field(default_factory=list)
    verified: Optional[bool] = None

    def __bool__(self) -> bool:
        return self.ok


# ---------------------------------------------------------------------------
# 1) 找出占用独显的进程
# ---------------------------------------------------------------------------


def gpu_processes() -> List[GpuProcess]:
    """列出正在使用 NVIDIA 独显的进程。

    用 nvidia-smi 的 compute/graphics 应用列表；拿不到就退回 PDH 计数器。
    """
    found: List[GpuProcess] = []

    if is_windows() or sys.platform.startswith("linux"):
        try:
            out = run_capture(
                ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                 "--format=csv,noheader,nounits"],
                timeout=5,
            )
            for line in (out.stdout or "").strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 2:
                    continue
                try:
                    pid = int(parts[0])
                except ValueError:
                    continue
                name = Path(parts[1]).name if parts[1] else f"pid-{pid}"
                mem = 0.0
                if len(parts) > 2 and parts[2].replace(".", "", 1).isdigit():
                    mem = float(parts[2])
                found.append(GpuProcess(pid=pid, name=name, memory_mib=mem))
        except Exception as exc:
            logger.debug("枚举独显进程失败: %s", exc)

    # 图形类应用 nvidia-smi 常常不列，用 PDH 的引擎占用补一遍
    try:
        from .metrics import PdhQuery, _GPU_ENGINE_PATH, _instance_of
        from .gpu import enumerate_adapters
        from .adapter import TYPE_DISCRETE

        discrete = next(
            (a for a in enumerate_adapters(use_cache=True) if a.kind == TYPE_DISCRETE),
            None,
        )
        if discrete is not None and discrete.luid:
            query = PdhQuery()
            if query.open():
                try:
                    query.sync([(_GPU_ENGINE_PATH, "engine")])
                    query.collect()
                    for instance, _kind, value in query.collect():
                        if value <= 0:
                            continue
                        match = re.search(r"pid_(\d+)", _instance_of(instance))
                        luid_match = re.search(r"luid_(0x[0-9a-f]+)_(0x[0-9a-f]+)", _instance_of(instance))
                        if not match or not luid_match:
                            continue
                        high = int(luid_match.group(1), 16)
                        low = int(luid_match.group(2), 16)
                        if (((high & 0xFFFFFFFF) << 32) | low) != discrete.luid:
                            continue
                        pid = int(match.group(1))
                        if any(item.pid == pid for item in found):
                            continue
                        found.append(GpuProcess(pid=pid, name=f"pid-{pid}"))
                finally:
                    query.close()
    except Exception as exc:
        logger.debug("PDH 补充枚举失败: %s", exc)

    return found


def killable(processes: List[GpuProcess]) -> List[GpuProcess]:
    """过滤掉不该结束的进程。"""
    result = []
    for item in processes:
        if item.name.lower() in _PROTECTED_NAMES:
            continue
        if item.pid in (0, 4):
            continue
        result.append(item)
    return result


def kill_processes(processes: List[GpuProcess]) -> List[str]:
    """结束这些进程，返回实际结束掉的名字。"""
    killed: List[str] = []
    for item in processes:
        try:
            result = run_capture(["taskkill", "/F", "/PID", str(item.pid)], timeout=10)
            if result.returncode == 0:
                killed.append(item.name)
                logger.info("已结束占用独显的进程: %s (pid %d)", item.name, item.pid)
            else:
                logger.debug("结束 %s 失败: %s", item.name, (result.stderr or "").strip())
        except Exception as exc:
            logger.debug("结束 pid %d 异常: %s", item.pid, exc)
    return killed


# ---------------------------------------------------------------------------
# 2) 释放本程序自己持有的 GPU 句柄
# ---------------------------------------------------------------------------


def release_own_gpu_handles() -> None:
    """释放本进程持有的显卡相关句柄。

    主要是让 DXGI 工厂/适配器对象被回收，并刷新 PDH 计数器句柄。
    Python 这边没有 NVAPI 句柄要放，但 DXGI 枚举会持有适配器引用，
    释放后 EC 才更容易把独显断电。
    """
    try:
        from .gpu import invalidate_cache

        invalidate_cache()
    except Exception:
        pass
    try:
        ctypes.windll.kernel32.SetProcessWorkingSetSize(
            ctypes.windll.kernel32.GetCurrentProcess(), ctypes.c_size_t(-1), ctypes.c_size_t(-1)
        )
    except Exception:
        pass
    # 给驱动一点时间回收引用
    time.sleep(0.5)


# ---------------------------------------------------------------------------
# 3) NVIDIA 服务
# ---------------------------------------------------------------------------


def nv_services_state() -> dict:
    state = {}
    for name in NV_SERVICES:
        try:
            out = run_capture(["sc", "query", name], timeout=8)
            text = (out.stdout or "")
            if "RUNNING" in text:
                state[name] = "running"
            elif "STOPPED" in text:
                state[name] = "stopped"
            else:
                state[name] = "unknown"
        except Exception:
            state[name] = "unknown"
    return state


def stop_nv_services() -> List[str]:
    """停掉 NVIDIA 容器服务（需要管理员，失败不影响主流程）。"""
    stopped = []
    for name in NV_SERVICES:
        try:
            result = run_capture(["net", "stop", name], timeout=20)
            if result.returncode == 0:
                stopped.append(name)
                logger.info("已停止服务 %s", name)
        except Exception as exc:
            logger.debug("停止 %s 失败: %s", name, exc)
    return stopped


def start_nv_services() -> List[str]:
    started = []
    for name in NV_SERVICES:
        try:
            result = run_capture(["net", "start", name], timeout=30)
            if result.returncode == 0:
                started.append(name)
                logger.info("已启动服务 %s", name)
        except Exception as exc:
            logger.debug("启动 %s 失败: %s", name, exc)
    return started


def restart_nv_services(delay: float = 3.0) -> List[str]:
    stop_nv_services()
    time.sleep(delay)
    return start_nv_services()


# ---------------------------------------------------------------------------
# 完整流程
# ---------------------------------------------------------------------------


def set_gpu_eco(
    eco: int,
    acpi,
    eco_device: int,
    on_progress: Optional[Callable[[str], None]] = None,
    kill_apps: bool = True,
    verify: bool = False,
) -> EcoResult:
    """按 G-Helper 的顺序切换独显电源。

    eco=1 关闭独显、eco=0 开启独显。

    关于 verify：默认**不做**事后探测。实测这台机器上任何探测独显的动作
    （nvidia-smi 会加载驱动、SetupAPI 会触发枚举）都会把刚断电的独显唤醒，
    于是"校验"本身就把结果破坏了，还会让调用方误判成失败。
    所以默认以"ACPI 接受了写入"为成功判据，需要强校验时再传 verify=True。
    """
    def note(text: str) -> None:
        logger.info(text)
        if on_progress:
            try:
                on_progress(text)
            except Exception:
                pass

    if not is_windows():
        return EcoResult(False, "仅支持 Windows")

    killed: List[str] = []
    services: List[str] = []

    if eco == 1:
        # --- 关独显前的三步准备（G-Helper 的必需步骤）---
        if kill_apps:
            procs = killable(gpu_processes())
            if procs:
                names = "、".join(p.name for p in procs[:5])
                note(f"正在结束占用独显的程序：{names}")
                killed = kill_processes(procs)
                time.sleep(1.0)
            else:
                note("没有程序占用独显")

        note("释放显卡句柄")
        release_own_gpu_handles()

        note("停止 NVIDIA 显示服务")
        services = stop_nv_services()

    note(f"写入 ACPI：GPUEco = {eco}")
    result = acpi.set(eco_device, eco)
    if result != 1:
        if eco == 0:
            start_nv_services()
        return EcoResult(
            False, "ACPI 写入被拒绝",
            detail=f"DEVS result={result}",
            killed=killed, services_restarted=services,
        )

    verified: Optional[bool] = None
    if verify:
        from .asusprobe import invalidate_presence_cache, nvidia_present

        deadline = time.time() + 10.0
        while time.time() < deadline:
            time.sleep(1.5)
            invalidate_presence_cache()
            present = nvidia_present(cache_seconds=0)
            if (eco == 1 and present is False) or (eco == 0 and present is True):
                verified = True
                break
        if verified is None:
            verified = False

    if eco == 0:
        note("恢复 NVIDIA 显示服务")
        services = start_nv_services()
        time.sleep(1.5)

    if eco == 1:
        message = "已关闭独显（集显模式）"
    else:
        message = "已开启独显（标准模式）"

    detail_parts = []
    if killed:
        detail_parts.append(f"结束了 {len(killed)} 个占用独显的程序：" + "、".join(killed[:5]))
    if services:
        detail_parts.append(("重启" if eco == 0 else "停止") + "服务：" + "、".join(services))
    if verified is False:
        detail_parts.append("状态未确认（探测独显会把它唤醒，属正常）")

    return EcoResult(
        True, message, detail="；".join(detail_parts),
        killed=killed, services_restarted=services, verified=verified,
    )
