## v1.1.0 —— 新增机型报告，方便把新机型补进兼容表

### 新增 `--report`：换台华硕机器先跑这个

`--check` 只告诉你「成不成」，`--report` 告诉你「这台机器到底有哪些设备号」。

```powershell
GpuCore.exe --report        # 免安装版
python -m gpucore --report  # 源码方式
```

输出是一段 markdown，跑完**整段贴到 issue 里**即可：

- 机器型号 / BIOS / 主板 / 操作系统
- 显卡清单（走 WMI，在别的机器上更可靠）
- 候选设备号探测表（命中的报 raw，没命中的也列出来）
- 传感器读数
- **两个设备号段的完整扫描**（`0x00090000` 段 + `0x00120000` 段里凡是读得到非零的号）

最后那一段是全扫结果，也就是这台机器真正存在的设备。**它是补全兼容性的唯一凭据，请勿删减。**

ATKACPI 通道打不开时，报告会告诉你装一次 G-Helper 或 MyASUS 就能带上这个驱动，而不是直接崩掉。

### 修复

- **`GpuCore.exe --report` 静默无输出**（退出码 0，一个字都不打印）
  `packaging/launcher.cs` 里决定「哪些参数要保留控制台」的白名单漏了 `--report`，
  于是走了 `pythonw.exe` + `CreateNoWindow`，stdout 是无效句柄，print 全丢。
  这种失败不报错，只能靠测试守。现在新增了 `packaging/launcher-console-flags` 用例，
  **双向**核对：launcher 判定到的开关都在白名单里，且 `app.py` 认识的开关 launcher 都认识。
- **打包脚本 `-OutDir` 传绝对路径会拼错**
  `Join-Path` 会拼成 `C:\...\GpuCore\C:\...\dist` 这种非法路径，
  `New-Item` 报「不支持给定路径的格式」。现在先换算成相对项目根的路径，落在项目外则明确报错。

### 免安装包

`GpuCore-1.1.0-portable.zip` —— 解压双击 `GpuCore.exe` 即可，目标机不需要装 Python、不需要联网。

自测 33/33 通过（默认路径绝不写 ACPI）。
