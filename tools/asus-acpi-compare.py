
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import time

CONTROL = 0x0022240C
DSTS = 0x53545344
DEVS = 0x53564544
INIT = 0x54494E49
WDOG = 0x474F4457

ECO_ROG = 0x00090020
ECO_VIVO = 0x00090120
MUX_ROG = 0x00090016
MUX_VIVO = 0x00090026


def open_device():
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
    handle = k.CreateFileW(
        r"\\.\ATKACPI", 0x80000000 | 0x40000000, 1 | 2, None, 3, 0x80, None
    )
    if handle in (None, 0, wt.HANDLE(-1).value):
        return None, None
    return k, handle


def call(k, handle, method: int, args: bytes, out_size: int):
    buf = bytearray(8 + len(args))
    buf[0:4] = method.to_bytes(4, "little")
    buf[4:8] = len(args).to_bytes(4, "little")
    buf[8 : 8 + len(args)] = args
    in_buf = (ctypes.c_char * len(buf)).from_buffer(buf)
    out_buf = (ctypes.c_char * out_size)()
    returned = wt.DWORD(0)
    ok = k.DeviceIoControl(
        handle, CONTROL,
        ctypes.cast(in_buf, ctypes.c_void_p), len(buf),
        ctypes.cast(out_buf, ctypes.c_void_p), out_size,
        ctypes.byref(returned), None,
    )
    return bool(ok), bytes(out_buf), int(returned.value)


def read_short(k, handle, dev: int):
    args = dev.to_bytes(4, "little") + (0).to_bytes(4, "little")
    ok, out, ret = call(k, handle, DSTS, args, 16)
    raw = int.from_bytes(out[0:4], "little")
    return ok, raw, raw - 0x10000


def read_large(k, handle, dev: int, extra: int = 8, out_size: int = 64):
    args = dev.to_bytes(4, "little") + (0).to_bytes(4, "little") + bytes(extra)
    ok, out, ret = call(k, handle, DSTS, args, out_size)
    head = int.from_bytes(out[0:4], "little")
    return ok, head, head - 0x10000


def write_dev(k, handle, dev: int, value: int):
    args = dev.to_bytes(4, "little") + (value & 0xFFFFFFFF).to_bytes(4, "little")
    ok, out, ret = call(k, handle, DEVS, args, 16)
    return ok, int.from_bytes(out[0:4], "little")


def main() -> int:
    k, handle = open_device()
    if not k:
        print("打不开 \\\\.\\ATKACPI")
        return 1
    print("设备已打开")

    print("\n--- A. 短读（本项目原方式：输入8 输出16）---")
    for name, dev in (("GPUEco", ECO_ROG), ("GPUMux", MUX_ROG), ("ECO_VIVO", ECO_VIVO)):
        ok, raw, value = read_short(k, handle, dev)
        print(f"  {name:<10} ok={ok}  raw=0x{raw:08X}  value={value}")

    print("\n--- B. 大读（G-Helper DeviceGetLarge：输入12 输出64）---")
    for name, dev in (("GPUEco", ECO_ROG), ("GPUMux", MUX_ROG)):
        ok, head, value = read_large(k, handle, dev)
        print(f"  {name:<10} ok={ok}  head=0x{head:08X}  value={value}")

    print("\n--- C. INIT / WDOG 握手 ---")
    ok, out, ret = call(k, handle, INIT, bytes(8), 16)
    print(f"  INIT  ok={ok} ret={ret} out={out[:8].hex(' ')}")
    ok, out, ret = call(k, handle, WDOG, bytes(8), 16)
    print(f"  WDOG  ok={ok} ret={ret} out={out[:8].hex(' ')}")
    for name, dev in (("GPUEco", ECO_ROG), ("GPUMux", MUX_ROG)):
        ok, head, value = read_large(k, handle, dev)
        print(f"  握手后 {name:<10} head=0x{head:08X} value={value}")

    print("\n--- D. 写 eco=1 后逐次回读（对比两种读法）---")
    base_short = read_short(k, handle, ECO_ROG)[2]
    base_large = read_large(k, handle, ECO_ROG)[2]
    print(f"  写前： 短读={base_short}  大读={base_large}")
    ok, result = write_dev(k, handle, ECO_ROG, 1)
    print(f"  DEVS(eco=1) ok={ok} result={result}")
    for i in range(8):
        time.sleep(0.4)
        s = read_short(k, handle, ECO_ROG)[2]
        lg = read_large(k, handle, ECO_ROG)[2]
        print(f"    +{0.4*(i+1):.1f}s  短读={s}  大读={lg}")

    print("\n--- 恢复 eco=0 ---")
    ok, result = write_dev(k, handle, ECO_ROG, 0)
    print(f"  DEVS(eco=0) ok={ok} result={result}")
    time.sleep(2)
    print(f"  回读：短读={read_short(k, handle, ECO_ROG)[2]}  大读={read_large(k, handle, ECO_ROG)[2]}")

    ctypes.WinDLL("kernel32").CloseHandle(handle)
    return 0


if __name__ == "__main__":
    sys.exit(main())
