# 华硕显卡切换：踩坑记录（改代码前先看这份）

这份笔记记录的是**实测结论**，不是猜测。每条都有对应的现象日志。
改 `gpucore/app.py` 里跟 ACPI 有关的部分之前，务必先读完。

---

## 一、设备接口

设备路径 `\\.\ATKACPI`，控制码 `0x0022240C`。用 `DeviceIoControl` 调用，普通用户权限就能打开，不需要管理员。

输入缓冲区格式：`[方法名(4字节 LE)][参数长度(4字节 LE)][参数...]`

| 方法 | 值（ASCII） | 用途 |
|---|---|---|
| `DSTS` | `0x53545344` | 读设备号 |
| `DEVS` | `0x53564544` | 写设备号 |
| `INIT` | `0x54494E49` | 初始化 |
| `WDOG` | `0x474F4457` | 看门狗 |

读值约定：返回值 **减去 `0x10000`** 才是真实值；`0` 表示设备号不支持。

### 关键设备号

| 设备 | 编号 | 语义 |
|---|---|---|
| `GPUEcoROG` | `0x00090020` | 独显供电：**0 = 通电，1 = 断电** |
| `GPUMuxROG` | `0x00090016` | 显示通路：**0 = 集显，1 = 标准，2 = 独显直连** |
| `GPUEcoVivo` | `0x00090120` | Vivo 机型（本机不支持） |
| `GPUMuxVivo` | `0x00090026` | Vivo 机型（本机不支持） |
| `Temp_CPU` | `0x00120094` | CPU 温度 |
| `Temp_GPU` | `0x00120097` | GPU 温度（本机读回 0） |
| `PerformanceMode` | `0x00120075` | 性能模式 |

本机（FX608LM / BIOS 315）实测 **7 个里支持 5 个**：Eco、Mux、Temp_CPU、Temp_GPU、PerformanceMode 可读；两个 Vivo 号不支持。

---

## 二、切换前必须先做三件事

G-Helper 的 `GPUModeControl.SetGPUEco`：

```csharp
if (eco == 1) {
    HardwareControl.KillGPUApps();          // ① 结束占用独显的进程
    HardwareControl.DisposeGpuControl();    // ② 释放自己持有的 GPU 句柄
    if (IsNVPlatform()) NvidiaGpuControl.StopNVService();  // ③ 停 NVIDIA 容器服务
}
status = Program.acpi.SetGPUEco(eco);       // ④ 最后才写 ACPI
```

**独显被占用时，EC 不会真正断它的电。** 这就是早期版本"有时生效有时不生效"的根本原因——
占用者包括本程序自己的 `nvidia-smi`、PDH 计数器查询和 DXGI 枚举。

`eco=0`（开回来）时 G-Helper 还会 `RestartNVService()` 并重新枚举设备。

---

## 三、两个"读数骗局"（最容易踩）

### 骗局一：`GPUEco` 寄存器回读不可信

本机实测：写入 `eco=1`，`nvidia-smi` 立刻报 `Failed to initialize NVML: Not Found`（设备真的没了），
但 `DSTS` 读回来**始终是 0**。

排查过：短读（输入 8 / 输出 16）和 G-Helper 的 `DeviceGetLarge`（输入 12 / 输出 64）**结果完全一样**，
所以不是读法的问题，是这个寄存器在本机上回读就不准。

**后果**：早期版本信了它 → 程序认为"独显还开着" → 守护线程反复写 `eco=0` 把独显又打开
（日志里实测误抢回 14~19 次）。

**处置**：不要把 Eco 回读当作独显开关的判据。

### 骗局二：任何"探测独显"的动作都会把它唤醒

- `nvidia-smi` 会加载 NVIDIA 驱动 → 独显被唤醒
- SetupAPI 查设备问题码 → 触发设备枚举 → 独显被唤醒

于是"校验独显是否断电"这个动作**本身就把独显开回来了**，现象变成：

```
写完 eco=1 → 1 秒后 nvidia-smi 消失 → 3 秒后又出现
```

看起来像"切换失败"，其实是自己把自己弄醒的。

**处置**：
- 校验默认**不做**事后探测，以 "ACPI 接受写入（`DEVS` 返回值 == 1）" 为成功判据
- 用 `nvidia-smi` 判断通电/断电时，必须**限频**（本程序是连续失败 3 次、约 9 秒才改判）
- 探测频率不要高于几秒一次

### 唯一有效的两个观测信号

1. `nvidia-smi` 能不能读到独显 → 判断通电/断电（**有副作用，要限频**）
2. Windows 设备树的 problem code → **不敏感**，实测 `eco=1` 之后仍返回 True，基本没用

---

## 四、MUX 切换必须重启

G-Helper 的 `SetGPUMode` 切完 MUX 后：

```csharp
status = Program.acpi.DeviceSet(AsusACPI.GPUMux, 0, "GPUMux");
restart = true;
...
if (restart) { Process.Start("shutdown", "/r /t 1"); }
```

**它切完直接重启电脑。** 因为 MUX 改的是显示通路，纯写 ACPI 不会让屏幕立刻改由独显驱动——
不重启的话现象就是"点了没反应"，很容易被误判成功能坏了。

本程序：写完 MUX 弹确认框，然后 `shutdown /r /t 3`。

---

## 五、模式判定（与 G-Helper 一致）

```
mux == 0                        → Ultimate（独显直连）
否则 eco == 1                   → Eco（集显模式）
否则                            → Standard（标准模式）
```

本程序不跟 MUX 抢方向：守护/锁定只强制它**声明要管**的部分，
用户手动把显示通路切成独显直连，不会被顶回去。

---

## 六、安全闸

`MUX = 独显直连` 时屏幕由独显直接驱动，**此时关闭独显会直接黑屏**。

程序在写入前会先读状态并拒绝这种组合，同时把"独显直连 / 标准混合"这类会黑屏的操作放在确认弹窗之后。

另外，独显被强制断电后，如果驱动没能重新接管，设备会停在 `Status: Problem`，
而 `pnputil /restart-device` 需要管理员权限。这种情况下**重启电脑**是最可靠的恢复手段。

---

## 七、多机型适配（别再把设备号写死）

第一版把设备号写成模块常量（`DEV_ECO = 0x00090020` 等），只有 ROG / 部分 TUF 认，
换台 VivoBook 就整套功能失效。现在都收进 `gpucore/hardware.py` 的候选表：

| 用途 | 候选 | 机型 |
|---|---|---|
| 独显供电 | `0x00090020` / `0x00090120` | ROG、TUF Gaming / VivoBook、Zenbook、部分 TUF |
| 显示通路（MUX） | `0x00090016` / `0x00090026` | 同上顺序 |

探测判据很关键：**EC 对不支持的设备号返回 `0`，而支持的设备号一定回一个非零 raw**
（即使它的值本身是 0，读回来也是 `0x10000`）。所以 `get_raw()` 返回 `None` 才代表"不支持"，
返回 `0` 是"支持且值为 0"。写反了会把所有设备都判成不支持，或者更糟——去写一个意义不明的号。

探不到就不写：`DeviceMap.can_eco` / `can_mux` 为假时界面上对应按钮**置灰**，
而不是试着写一下看看。宁可少个功能，也不要在一台没验证过的机器上写未知的设备号。

---

## 八、打包免安装版踩过的坑

给别人用的便携包由 `packaging/build-portable.ps1` 生成，**目标机不需要装 Python**
（包里带完整 CPython 运行时）。下面几条都是真踩过、且报错信息完全指不到病根的：

**① `.ps1` 必须带 UTF-8 BOM。**
Windows PowerShell 5.1 读不带 BOM 的脚本时按当前 ANSI 代码页（中文系统是 GBK）解码，
中文注释被解成乱码后满屏 `Unexpected token '鍐欏叆...'`——看起来像语法错误，实际是编码问题。
很多编辑器（包括本项目用的编辑工具）写回文件时会掉 BOM。改完跑 `tools/check-ps1.ps1` 确认。

**② `python312._pth` 里必须列上 `DLLs`。**
有这个文件时解释器进入隔离模式，**默认的 DLL 搜索路径被关掉**。而 `_ctypes.pyd` 依赖的
`libffi-8.dll`、`_tkinter.pyd` 依赖的 `tcl86t.dll` 都躺在 `DLLs\` 目录里 —— 漏了这一行，
包内解释器一起来就死：`ModuleNotFoundError: No module named '_ctypes'`。

**③ 裁剪白名单不能漏 `Lib\__future__.py`。**
几乎所有模块第一行都是 `from __future__ import annotations`，删掉它整个包都起不来。

**④ 启动器必须是控制台程序（`/target:exe`），不能是 `/target:winexe`。**
winexe 进程自己没有控制台，被它拉起的 python 子进程继承到的是无效标准句柄，
于是 `GpuCore.exe --check` 在任何终端里**一个字都不打印**——而自检报告正是用户
反馈问题时唯一能给的东西。同理在看输出的模式里 `CreateNoWindow` 必须为 `false`。
双击启动时启动器会立刻用 `GetConsoleWindow` + `ShowWindow(SW_HIDE)` 把自己的控制台藏掉。

**⑤ 两道自动关卡都真抓到过会发出去的坏包，别省。**
`--check` 在**清空 `PYTHONPATH`/`PYTHONHOME`、`PATH` 只剩 `System32`** 的干净环境里跑，
抓到了 ② 和 ③；`smoke-gui.py` 真建一个窗口，补上 `--check` 覆盖不到的"双击没反应"。

**⑥ 子进程输出统一按 UTF-8 处理。**
`subprocess` 默认用系统首选编码解码子进程输出（中文系统是 GBK），而子 Python 的 stdout
是管道时也用 GBK 编码。因为 nvidia-smi / pnputil 的输出实测是纯 ASCII，统一
`encoding="utf-8"` + `PYTHONIOENCODING=utf-8`（`child_env()`）对本机原生工具无害。

---

## 九、相关文件

| 文件 | 内容 |
|---|---|
| `gpucore/app.py` | 界面、采集、切换流程、安全闸 |
| `gpucore/hardware.py` | 机型自适应层：ACPI 读写 + 设备号候选探测 |
| `gpucore/selftest.py` | 逐设备号自检（`--check`）+ 机型报告（`--report`） |
| `gpucore/tests/test_gpucore.py` | 自测套件，默认绝不写 ACPI |
| `packaging/build-portable.ps1` | 免安装包构建（含两道自动关卡） |
| `packaging/launcher.cs` | `GpuCore.exe` 启动器源码（控制台子系统） |
| `tools/check-ps1.ps1` | 体检所有 `.ps1` 的 BOM 与语法 |
| `archive/gpuswitch-legacy/` | 早期实现（含锁定守护、进程拦截等），仅作参考 |
