

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

        
        
        
        
        bool consoleMode = false;
        foreach (string a in args)
        {
            string s = a.ToLowerInvariant();
            if (s == "--check" || s == "--selftest" || s == "--version" || s == "-v"
                || s == "--help" || s == "-h" || s == "--console" || s == "--check-gui"
                || s == "--report")
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
        
        
        psi.CreateNoWindow = !consoleMode;
        
        
        psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";

        
        
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
