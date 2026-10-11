"""极简热切换：只做一件事——把独显开或关。

这个文件刻意保持最小：**没有守护线程、没有锁定、没有自动重试**。
之前那些"自动抢回"的逻辑会在用户点按钮后把状态改回去，
表现成"按哪个都不管用"，所以这一版全部删掉。

切换顺序完全照 G-Helper 的 GPUModeControl.SetGPUEco：

    eco=1（关独显）:  KillGPUApps -> DisposeGpuControl -> StopNVService -> ACPI
    eco=0（开独显）:  ACPI -> StartNVService

用法：
    python -m gpuswitch.hotswitch off     # 关独显（集显模式）
    python -m gpuswitch.hotswitch on      # 开独显（标准模式）
    python -m gpuswitch.hotswitch status  # 看当前状态
    python -m gpuswitch.hotswitch gui     # 就两个按钮的窗口
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import subprocess
import sys
import time
from pathlib import Path

ATKACPI = r"\\.\ATKACPI"
CONTROL = 0x0022240C
DSTS = 0x53545344
DEVS = 0x53564544
DEV_ECO = 0x00090020
DEV_MUX = 0x00090016

NV_SERVICES = ("NVDisplay.ContainerLocalSystem", "NvContainerLocalSystem")

_PROTECTED = {
    "system", "registry", "memory compression", "csrss.exe", "wininit.exe",
    "winlogon.exe", "services.exe", "lsass.exe", "smss.exe", "svchost.exe",
    "dwm.exe", "explorer.exe", "fontdrvhost.exe", "sihost.exe", "ctfmon.exe",
    "taskhostw.exe", "runtimebroker.exe", "shellexperiencehost.exe",
    "startmenuexperiencehost.exe", "searchhost.exe", "textinputhost.exe",
    "msmpeng.exe", "securityhealthservice.exe", "ghelper.exe",
    "nvcontainer.exe", "nvdisplay.container.exe",
}


class Acpi:
    def __init__(self) -> None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.restype = wt.HANDLE
        k.CreateFileW.argtypes = [
            wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE
        ]
        k.DeviceIoControl.restype = wt.BOOL
        k.DeviceIoControl.argtypes = [
            wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD,
            ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p,
        ]
        k.CloseHandle.argtypes = [wt.HANDLE]
        self._k = k
        self._h = k.CreateFileW(ATKACPI, 0x80000000 | 0x40000000, 1 | 2, None, 3, 0x80, None)
        if self._h in (None, 0, wt.HANDLE(-1).value):
            raise RuntimeError("打不开 \\\\.\\ATKACPI —— 需要 ASUS System Control Interface 驱动")

    def _call(self, method: int, args: bytes, out: int = 16) -> bytes:
        buf = bytearray(8 + len(args))
        buf[0:4] = method.to_bytes(4, "little")
        buf[4:8] = len(args).to_bytes(4, "little")
        buf[8 : 8 + len(args)] = args
        ib = (ctypes.c_char * len(buf)).from_buffer(buf)
        ob = (ctypes.c_char * out)()
        ret = wt.DWORD(0)
        self._k.DeviceIoControl(
            self._h, CONTROL,
            ctypes.cast(ib, ctypes.c_void_p), len(buf),
            ctypes.cast(ob, ctypes.c_void_p), out, ctypes.byref(ret), None,
        )
        return bytes(ob)

    def get(self, dev: int):
        args = dev.to_bytes(4, "little") + bytes(4)
        raw = int.from_bytes(self._call(DSTS, args)[0:4], "little")
        return None if raw == 0 else raw - 0x10000

    def set(self, dev: int, value: int) -> int:
        args = dev.to_bytes(4, "little") + (value & 0xFFFFFFFF).to_bytes(4, "little")
        return int.from_bytes(self._call(DEVS, args)[0:4], "little")

    def close(self) -> None:
        try:
            self._k.CloseHandle(self._h)
        except Exception:
            pass


def run(cmd, timeout=20):
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)


def dgpu_works() -> bool:
    """独显现在能不能用。只在用户主动查看/切换后调用一次。"""
    try:
        r = run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], timeout=6)
    except Exception:
        return False
    text = ((r.stdout or "") + (r.stderr or "")).lower()
    return r.returncode == 0 and "nvidia" in text


def gpu_processes():
    """正在占用独显的进程（pid, 名字）。"""
    found = []
    try:
        r = run(["nvidia-smi", "--query-compute-apps=pid,process_name",
                 "--format=csv,noheader"], timeout=6)
        for line in (r.stdout or "").strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2 and parts[0].isdigit():
                found.append((int(parts[0]), Path(parts[1]).name))
    except Exception:
        pass
    return [(p, n) for p, n in found if n.lower() not in _PROTECTED and p not in (0, 4)]


def kill(procs) -> list:
    done = []
    for pid, name in procs:
        try:
            if run(["taskkill", "/F", "/PID", str(pid)], timeout=10).returncode == 0:
                done.append(name)
        except Exception:
            pass
    return done


def svc(action: str) -> list:
    done = []
    for name in NV_SERVICES:
        try:
            if run(["net", action, name], timeout=30).returncode == 0:
                done.append(name)
        except Exception:
            pass
    return done


def switch(off: bool, log=print) -> bool:
    """off=True 关独显（集显模式），off=False 开独显（标准模式）。"""
    acpi = Acpi()
    try:
        if off:
            procs = gpu_processes()
            if procs:
                log(f"正在结束占用独显的程序：{'、'.join(n for _, n in procs[:5])}")
                killed = kill(procs)
                if killed:
                    log(f"已结束：{'、'.join(killed)}")
                time.sleep(1.5)
            else:
                log("没有程序占用独显")

            log("停止 NVIDIA 服务")
            stopped = svc("stop")
            if stopped:
                log(f"已停止：{'、'.join(stopped)}")
            time.sleep(1.0)

        log(f"写入 ACPI：GPUEco = {1 if off else 0}")
        result = acpi.set(DEV_ECO, 1 if off else 0)
        if result != 1:
            log(f"✗ 固件拒绝了写入（result={result}）")
            if not off:
                svc("start")
            return False

        if not off:
            log("启动 NVIDIA 服务")
            started = svc("start")
            if started:
                log(f"已启动：{'、'.join(started)}")

        # 等设备重新枚举，然后只探测一次（探测本身会唤醒独显，所以不做轮询）
        wait = 10 if off else 14
        log(f"等待设备重新枚举（{wait} 秒）…")
        time.sleep(wait)

        ok = dgpu_works()
        if off:
            if not ok:
                log("✓ 独显已关闭（集显模式）")
                return True
            log("⚠ 独显仍在工作，可能有程序正占用它（关掉游戏/录屏后再试）")
            return False
        if ok:
            log("✓ 独显已开启（标准模式）")
            return True
        log("⚠ 独显还没被驱动接管，可能需要在设备管理器里重启一次，或重启电脑")
        return False
    finally:
        acpi.close()


def status() -> str:
    acpi = Acpi()
    try:
        eco = acpi.get(DEV_ECO)
        mux = acpi.get(DEV_MUX)
    finally:
        acpi.close()
    works = dgpu_works()
    mux_text = {0: "集显模式", 1: "标准混合", 2: "独显直连"}.get(mux, f"未知({mux})")
    return (
        f"独显可用   : {'是' if works else '否'}\n"
        f"GPUEco 寄存器: {eco}（注意：本机型这个值不可信，写 1 后仍可能读回 0）\n"
        f"显示通路 MUX : {mux}（{mux_text}）"
    )


def gui() -> int:
    import tkinter as tk

    root = tk.Tk()
    root.title("显卡热切换")
    root.configure(bg="#1b1b1b")
    root.resizable(False, False)
    root.geometry("360x260")

    tk.Label(root, text="显卡热切换", bg="#1b1b1b", fg="#f0f0f0",
             font=("Microsoft YaHei UI", 14, "bold")).pack(pady=(16, 4))
    state = tk.Label(root, text="点下面的按钮切换", bg="#1b1b1b", fg="#9a9a9a",
                     font=("Microsoft YaHei UI", 9), wraplength=320, justify="center")
    state.pack(pady=(0, 12))

    buttons = []

    def do(off: bool) -> None:
        for b in buttons:
            b.configure(state="disabled")
        state.configure(text="切换中，请稍等 10~15 秒…", fg="#e0a32e")
        root.update()

        def worker() -> None:
            try:
                ok = switch(off, log=lambda t: (state.configure(text=t), root.update()))
            except Exception as exc:
                ok = False
                state.configure(text=f"失败：{exc}", fg="#e5534b")
                root.update()
            if ok:
                state.configure(text="完成，可以关掉这个开关了" if off else "完成", fg="#38b26b")
            for b in buttons:
                b.configure(state="normal")
            root.update()

        import threading

        threading.Thread(target=worker, daemon=True).start()

    def mk(parent, text, off, color):
        b = tk.Label(parent, text=text, bg=color, fg="white", cursor="hand2",
                     font=("Microsoft YaHei UI", 11, "bold"), pady=12)
        b.pack(fill="x", padx=24, pady=5)
        b.bind("<Button-1>", lambda _e: do(off))
        buttons.append(b)
        return b

    mk(root, "用集显（关掉独显，省电凉快）", True, "#2f6f9f")
    mk(root, "用独显（开启独显，性能模式）", False, "#3f8f3f")

    tk.Label(root, text='命令行也能用：python -m gpuswitch.hotswitch off / on',
             bg="#1b1b1b", fg="#6a6a6a", font=("Microsoft YaHei UI", 8)).pack(pady=(10, 0))

    root.mainloop()
    return 0


def main() -> int:
    arg = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    if arg in ("off", "eco", "igpu", "集显"):
        return 0 if switch(True) else 1
    if arg in ("on", "standard", "dgpu", "独显"):
        return 0 if switch(False) else 1
    if arg == "gui":
        return gui()
    print(status())
    return 0


if __name__ == "__main__":
    sys.exit(main())
