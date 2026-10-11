// GpuCore.exe —— 免安装版启动器
//
// 为什么需要它：便携包里带的是 Python 的 pythonw.exe，直接双击 pythonw.exe
// 不会启动本程序（它只是个解释器）。这个原生小程序负责：
//   1. 找到同级目录 python\pythonw.exe
//   2. 把工作目录设成自己所在目录（这样相对路径都稳定）
//   3. 无窗口启动去跑 main.py（不开控制台，避免弹黑框）
//
// 用 .NET Framework 自带的 csc.exe 编译，目标机无需任何运行时。
// 目标机不必装 Python：解释器就在包里。
//
// 【为什么是"控制台程序"而不是 winexe】
// 第一版编译成 /target:winexe（纯 GUI 程序，没有控制台），结果
// `GpuCore.exe --check` 在任何终端里都**一个字都不打印**：winexe 进程自己没有
// 控制台，python 子进程继承到的标准句柄是无效的，输出全部丢进黑洞。
// 自检报告是这个项目给用户排障的主要手段，输出打不出来等于没用。
// 所以改成 /target:exe（控制台程序）：
//   * 从 cmd / PowerShell 里跑 → 复用父进程控制台，--check 的输出正常显示；
//   * 双击启动 → 立刻把自己那个控制台窗口藏掉（见 HideOwnConsole），
//     用户只会看到一瞬间的闪动，程序本身照常后台常驻。
// 实测：cmd 里 `GpuCore.exe --check > out.txt` 能拿到完整报告且退出码为 0。

using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows.Forms;

static class GpuCoreLauncher
{
    [DllImport("kernel32.dll")]
    private static extern IntPtr GetConsoleWindow();

    [DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    private const int SW_HIDE = 0;

    // 双击启动时用：把本进程的控制台窗口藏起来，避免弹黑框。
    // 从已有终端启动时这里也没有新窗口可藏，是安全的空操作。
    static void HideOwnConsole()
    {
        try
        {
            IntPtr h = GetConsoleWindow();
            if (h != IntPtr.Zero) ShowWindow(h, SW_HIDE);
        }
        catch
        {
        }
    }

    static string ExeDir()
    {
        try
        {
            return Path.GetDirectoryName(Application.ExecutablePath);
        }
        catch
        {
            return Directory.GetCurrentDirectory();
        }
    }

    static void Fail(string message)
    {
        MessageBox.Show(
            message,
            "GpuCore 启动失败",
            MessageBoxButtons.OK,
            MessageBoxIcon.Error);
    }

    [STAThread]
    static int Main(string[] args)
    {
        string root = ExeDir();
        string pythonw = Path.Combine(root, "python", "pythonw.exe");
        string python = Path.Combine(root, "python", "python.exe");
        string mainPy = Path.Combine(root, "main.py");

        // 先判断是不是"要看输出"的模式：这类模式必须保留控制台。
        bool consoleMode = false;
        foreach (string a in args)
        {
            string s = a.ToLowerInvariant();
            if (s == "--check" || s == "--selftest" || s == "--version" || s == "-v"
                || s == "--help" || s == "-h" || s == "--console" || s == "--check-gui")
            {
                consoleMode = true;
                break;
            }
        }
        if (!consoleMode) HideOwnConsole();

        if (!File.Exists(pythonw))
        {
            Fail("找不到内置的 Python 运行时：\n" + pythonw +
                 "\n\n压缩包可能没完整解压。请把整个 GpuCore 文件夹解压出来再运行，" +
                 "不要在压缩包里直接双击。");
            return 2;
        }
        if (!File.Exists(mainPy))
        {
            Fail("找不到 main.py：\n" + mainPy + "\n\n这是一个损坏的副本，请重新解压。");
            return 3;
        }

        ProcessStartInfo psi = new ProcessStartInfo();
        psi.FileName = pythonw;
        psi.WorkingDirectory = root;
        psi.UseShellExecute = false;
        // 看输出的模式必须让子进程继承本进程的控制台，否则 stdout 是无效句柄，
        // 报告照样一个字都留不下（第一版就栽在 CreateNoWindow=true 上）。
        psi.CreateNoWindow = !consoleMode;
        // 强制子进程用 UTF-8 输出：中文 Windows 上 Python 从 GUI 进程启动时
        // 会根据代码页选 GBK，自检报告里的中文/✓ 就变成乱码甚至直接崩。
        psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";

        // --check / --selftest 这类要看输出的模式：用带控制台的 python.exe
        // （pythonw.exe 的 stdout 是无效句柄，print 会丢），并等它跑完把退出码传出去。
        if (consoleMode && File.Exists(python))
        {
            psi.FileName = python;
        }

        psi.Arguments = Quote(mainPy) + " " + string.Join(" ", args);

        try
        {
            Process p = Process.Start(psi);
            if (consoleMode)
            {
                p.WaitForExit();
                return p.ExitCode;
            }
            return 0;
        }
        catch (Exception ex)
        {
            Fail("启动失败：\n" + ex.Message);
            return 4;
        }
    }

    static string Quote(string value)
    {
        return "\"" + value.Replace("\"", "\\\"") + "\"";
    }
}
