# 显卡切换工具（GPUSwitch）

按一个全局快捷键弹出面板：**上面告诉你现在用的是哪块显卡**，**中间是 GPU / CPU / 内存三路实时占用**，
**下面点另一块显卡就切过去**——在支持的机型上是**硬件级热切换，立刻生效，不用重启任何程序**。

面板长这样（真实渲染截图，非设计稿）：

![面板预览](assets/panel-preview.png)

---

## 一、先说清楚：Windows 上"切换显卡"到底有哪几种，本程序用的是哪种

这是整个项目最容易被误解的地方，所以放在最前面。

| 方式 | 点下去立刻生效？ | 需要重启程序？ | 适用机器 | 本程序 |
| --- | --- | --- | --- | --- |
| **A. 厂商硬件通道**（改显卡供电 / 显示通路） | ✅ 是，1 秒内 | ❌ 不用 | ASUS 等有厂商 ACPI 接口的机型 | ✅ **首选路径** |
| **B. 显卡偏好**（写注册表 `UserGpuPreferences`） | ❌ 否 | ✅ 需要 | 任何 Windows 10 1803+ 双显卡机器 | ✅ 作为补充 |
| C. 杀进程 + 重启 | 间接生效 | ✅ 需要（丢未保存数据） | 任何机器 | ✅ 可选开启 |
| D. MUX 硬切换（独显直连 / 集显模式） | 会黑屏数秒 | 通常需要 | 有 MUX 开关的机型 | ✅ 支持，带安全闸 |

**本程序在这台机器（ASUS FX608LM）上走的是 A**：直接通过 `\\.\ATKACPI` 改显卡供电与显示通路，
和奥创（Armoury Crate）、G-Helper 是**同一个接口**，所以是真正意义上的即时热切换。

在没有厂商通道的机器上自动退化为 **B**，并且界面会**明确写出**"对被点的程序在下次启动时生效"，
不会让你以为点了没反应。

### 为什么 B 做不到即时生效

Windows 在**进程创建、初始化图形设备时**读一次显卡偏好，之后就固定在那个 GPU 上，
没有任何公开接口能把一个已经在运行的程序迁移到另一块显卡。所以：

* 想让**已经在跑**的程序换卡 → 只能重启它（方式 C，会丢数据）
* 想让**以后启动**的程序用某块卡 → 写偏好即可（方式 B）

NVIDIA 控制面板里的"首选图形处理器"也是同样的机制，同样需要重启程序。

---

## 二、核心功能：锁定显卡选择

这是本程序最实用的部分，解决的是**"打完游戏退出后，独显被别的工具强制切回核显"**。

1. 在「锁定显卡选择」卡片里点一个模式（例如**独显常驻**）→ 立刻应用并**记住**
2. 守护线程每 4 秒检查一次硬件状态
3. 发现被别的程序（奥创、G-Helper、厂商服务、Windows 电源策略）改动了 → **立刻改回来**，
   并在面板上告诉你"刚被改了，我已经抢回"
4. 重启后自动恢复上次的锁定

真机实测日志（由 `tools/drift-test.ps1` 主动扮演"奥创"篡改 4 次）：

```
[锁定] drift   检测到显卡状态被改动：dgpu_on -> igpu_only，正在抢回…
[锁定] restored 已抢回「独显常驻」（第 1 次）
[锁定] restored 已抢回「独显常驻」（第 2 次）
[锁定] restored 已抢回「独显常驻」（第 3 次）
[锁定] restored 已抢回「独显常驻」（第 4 次）
```

**锁定只强制它声明要管的部分**：「独显常驻」只保证独显通电，你自己手动把显示通路切成
独显直连不会被顶回去——守护不会跟你抢方向盘。

### 安全闸

`MUX=独显直连` 时屏幕由独显直接驱动，此时关闭独显会**直接黑屏**。
程序在写入前会先读取状态并拒绝这种组合，同时把"独显直连/标准混合"这类会黑屏的操作
放在确认弹窗之后。

---

## 三、安装

把整个文件夹拷到目标机器，**双击 `安装.cmd`**。脚本会自动：

1. 找可用的 Python（3.9+）；找不到就用 `winget` 静默装 Python 3.12
2. 校验 tkinter（界面必需），缺失时给出明确指引
3. 建虚拟环境并尝试装可选依赖（`psutil` / `pystray` / `Pillow`，装不上不影响功能）
4. 生成启动器 + 桌面快捷方式 + 开机自启
5. 跑一遍自检并打印结果

```powershell
# 跳过某些步骤
powershell -ExecutionPolicy Bypass -File install.ps1 -NoStartup -NoShortcut -SkipDeps
```

### 开发环境（VS Code）

```powershell
code .                                     # 打开项目
# Ctrl+Shift+P → Python: Select Interpreter → 选 .venv\Scripts\python.exe
# F5 → 选「运行：显示面板」或「全功能测试套件」
```

`.vscode/launch.json` 已备好 6 个调试配置，含一键跑测试套件与一键渲染界面截图。

---

## 四、用法

| 操作 | 效果 |
| --- | --- |
| **Ctrl+A** | 呼出 / 收起面板（可改，见下） |
| 点**另一块显卡**的按钮 | 硬件热切换（有厂商通道时），立刻生效 |
| 点**当前显卡**的按钮 | 提示已在用，不做无用动作 |
| 「锁定显卡选择」里的模式 | 锁定该硬件模式，被改动会自动抢回 |
| 「重新检测显卡」 | 重新枚举（外接显卡坞、显卡热插拔后） |
| `Esc` / 右上角 × | 收起（不退出程序） |

配置文件：`%APPDATA%\GPUSwitch\config.json`

> 该目录不可写时会依次回退到 `%USERPROFILE%\.gpuswitch\` 和项目内 `.local\`，
> 实际位置写在启动日志里。

```jsonc
{
  "hotkey": "ctrl+a",          // ctrl/alt/shift/win + a-z / 0-9 / F1-F24
  "poll_ms": 1000,             // 占用刷新间隔
  "switch_scope": "self",      // self 免管理员 / global 改全局默认（弹一次 UAC）
  "auto_lock_on_switch": true, // 点显卡按钮切换后自动锁定
  "lock": { "enabled": false, "mode_id": "" },  // 锁定状态（程序自动维护）
  "lock_interval": 4.0,        // 守护检查间隔（秒）
  "apps": [],                  // 按程序指定显卡的规则
  "watch_processes": false     // 是否启用进程拦截
}
```

### 命令行

```powershell
python -m gpuswitch --show                    # 启动并显示面板
python -m gpuswitch --check                   # 环境自检
python -m gpuswitch --hw-status               # 显卡硬件状态 + 可锁定模式
python -m gpuswitch --lock dgpu_on            # 锁定「独显常驻」
python -m gpuswitch --lock ultimate --yes     # 锁独显直连（会黑屏，需重启）
python -m gpuswitch --unlock                  # 解除锁定
python -m gpuswitch --watch-lock              # 前台跑守护，方便观察抢回过程
python -m gpuswitch --app-add "D:\game\g.exe" --pref 2 --watch-apps   # 该程序用独显
python -m gpuswitch --app-list                # 查看规则与注册表现状
python -m gpuswitch --app-launch "D:\game\g.exe"   # 先写偏好再启动（最可靠）
python -m gpuswitch --test-hotkey ctrl+alt+g  # 检测快捷键是否被占用
python -m gpuswitch --json                    # 打印显卡信息
```

---

## 五、按程序指定显卡（通用方案）

来自参考项目 [JasonHu051219/GPUSwitch](https://github.com/JasonHu051219/GPUSwitch) 的思路，
本程序以更省依赖的方式实现：

1. **最可靠**：用 `--app-launch` 或面板启动目标程序——先写偏好，再 `CreateProcess`，零竞态
2. **拦截式**：开启 `watch_processes` 后监听新进程，在它初始化图形前写入偏好
   （优先 WMI `Win32_Process` 的 `Creation` 事件；WMI 不可用时退化为 Win32 进程快照轮询）
3. **可选重启**：加 `--restart` 后，拦截到启动会结束并重启该进程让偏好生效
   ——**会丢未保存数据**，所以默认关闭，必须显式开启

系统关键进程（`svchost.exe`、`lsass.exe`、`explorer.exe` 等 27 个）在保护名单里，
永远不会被拦截或重启。

---

## 六、实现方式（零硬依赖）

| 功能 | 实现 |
| --- | --- |
| 显卡枚举（名称/厂商/显存/LUID） | `ctypes` 直调 **DXGI** `CreateDXGIFactory1` + `IDXGIFactory1.EnumAdapters1`（手写 COM 虚表调用） |
| 集显/独显/虚拟卡判定 | 设备号 + 名称关键字 + 显存特征三级判定 |
| 判断"当前在用哪块卡" | 按**渲染负载**判定（混合模式下所有画面都输出到核显，看谁驱动显示器会误判） |
| GPU 占用 / 显存 | **PDH 性能计数器**（`\GPU Engine(*)\Utilization Percentage`、`\GPU Adapter Memory(*)\Dedicated Usage`），与任务管理器同源 |
| CPU / 内存 | Win32 `GetSystemTimes` / `GlobalMemoryStatusEx`；装了 psutil 优先用 |
| 硬件热切换 | `\\.\ATKACPI` + `DeviceIoControl(0x0022240C)`，`DSTS` 读 / `DEVS` 写 |
| 进程枚举 | psutil，缺失时用 `CreateToolhelp32Snapshot` + `QueryFullProcessImageNameW` 原生实现 |
| 全局快捷键 | `user32!RegisterHotKey` + 独立消息线程 |
| 界面 | tkinter 自绘卡片 / 圆角进度条 / 描边按钮，适配高 DPI |

```
gpuswitch/
├── adapter.py    显卡数据结构 + 集显/独显/虚拟卡判定
├── gpu.py        DXGI / 注册表 / lspci 多路枚举 + 去重 + 渲染方判定
├── metrics.py    PDH + Win32 + psutil 指标采集（多级降级）
├── hardware.py   厂商硬件通道（ASUS ACPI）读写 + 安全闸
├── lock.py       显卡选择锁定与守护线程
├── watcher.py    按程序指定显卡（进程拦截 / 启动前写偏好 / 重启）
├── hotkey.py     RegisterHotKey 全局快捷键
├── switcher.py   注册表偏好后端（Windows / Linux PRIME / macOS）
├── ui.py         面板（卡片、圆角进度条、描边按钮）
├── app.py        装配、主循环、命令行
└── selfcheck.py  --check 自检
```

---

## 七、测试

```powershell
python tools/test-all.py              # 全功能测试套件（31 项）
python tools/test-all.py -k ui        # 只跑名字含 ui 的用例
python tools/test-all.py --list       # 列出全部用例
python tools/lock-selftest.py         # 锁定守护逻辑（假后端，不碰硬件）
python tools/drift-test.ps1           # 真机漂移测试：扮演奥创篡改，验证守护抢回
python tools/asus-probe.ps1           # 只读探测硬件通道
python tools/verify-switch.ps1        # 验证注册表偏好写入（自动还原）
```

`tools/drift-test.ps1` 会真的把独显关掉再验证守护抢回，属于**有意为之的破坏性测试**，
只在你自己想验证时运行。

---

## 八、和参考项目的区别

[JasonHu051219/GPUSwitch](https://github.com/JasonHu051219/GPUSwitch) 做的是**纯偏好方案**，
它的 README 明确写了"不修改 HKLM、不需要管理员"。它用 WMI 监听进程启动，
然后把程序**杀掉再重启**，逼 Windows 重新读偏好。

| | 参考项目 | 本程序 |
| --- | --- | --- |
| 切换机制 | 只写注册表偏好 | **厂商硬件通道**（真热切换）+ 偏好 |
| 点按钮后立即生效 | ❌（要重启程序） | ✅（有厂商通道时，1 秒内） |
| 对抗奥创改回设置 | ❌ 无此功能 | ✅ 锁定 + 守护抢回 |
| 硬件层 MUX / 独显供电 | ❌ 不涉及 | ✅ 支持，带黑屏安全闸 |
| 进程拦截 | ✅ WMI 事件 | ✅ WMI 事件（不可用时退化为 Win32 轮询） |
| 启动时询问用哪块卡 | ✅ | 🔶 规则里支持 `ask` 标记（弹窗交互待完善） |
| UI 风格 | PySide6 / Win11 列表 | tkinter / G-Helper 风格卡片 |
| 依赖 | PySide6 + wmi + pywin32 + psutil | **零硬依赖**（全 stdlib + ctypes） |

两者互补：**没有厂商通道的机器靠偏好方案，有的机器靠硬件方案**，本程序把两条路都放进来了。

---

## 九、常见问题

**按 Ctrl+A 会抢掉浏览器/编辑器的全选？**
会，全局热键优先级高于前台程序。改 `config.json` 里的 `hotkey` 为 `"ctrl+alt+g"` 再重启即可
（先用 `--test-hotkey` 测是否被占用）。

**配置的热键被别的程序占用了怎么办？**
程序会自动依次尝试 `ctrl+alt+g` → `ctrl+shift+g` → `ctrl+alt+f12` → `ctrl+shift+f12`，
找到一个能用的就生效，并把结果写回 `config.json`，同时在面板上告诉你换了哪个。
这样不会出现"程序在跑但按什么都没反应"。想关掉这个行为就把 `hotkey_fallback` 设为 `false`。

**点了显卡按钮没反应？**
先看卡片下面那行提示：
* 写着"点一下即热切换" → 是硬件通道，应该 1 秒内生效；若没反应看状态栏报错
* 写着"本机无厂商硬件通道" → 只改了偏好，需要重启该程序

**切换后独显占用还是 0%？**
独显通电 ≠ 有程序在用它。程序默认用哪块卡由偏好决定，新启动的程序才会用上。

**点「独显直连」黑屏了？**
这是 MUX 切换的正常现象，几秒后恢复、通常需要重启。程序在你确认前会先弹窗警告。

**集显显存显示 0 B？**
核显没有独立显存，用的是共享内存，正常。

**不想开机自启？**
删掉 `shell:startup`（Win+R 输入）里的「显卡切换工具」快捷方式。
