
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from typing import List, Optional

from . import APP_NAME, __version__, config as config_module, log
from .adapter import TYPE_VIRTUAL, GpuAdapter
from .gpu import describe_current, enumerate_adapters, invalidate_cache
from .hardware import detect_hardware
from .hotkey import GlobalHotkey, HotkeyError, probe_hotkey
from .lock import GpuLock, LockEvent, LockRecord
from .metrics import MetricsCollector
from .switcher import (
    SwitchBackend,
    SwitchResult,
    get_backend,
    is_elevated,
    set_process_default_adapter,
)
from .ui import GpuPanel, prepare_dpi
from .paths import is_windows
from .watcher import LaunchRule, ProcessWatcher, launch_with_gpu, terminate_and_relaunch

logger = log.get(__name__)


def _asus_available() -> bool:
    try:
        from .asusprobe import AtkAcpi

        acpi = AtkAcpi()
        if not acpi.open():
            return False
        try:
            return acpi.get(0x00090020) is not None
        finally:
            acpi.close()
    except Exception as exc:
        logger.debug("华硕通道探测失败: %s", exc)
        return False


class GpuSwitchApp:
    def __init__(self, config: dict, start_hidden: bool = True) -> None:
        self.config = config
        self.start_hidden = start_hidden
        self.pending_hotkey = 0
        self.watcher: Optional[ProcessWatcher] = None

        self.scale = prepare_dpi()
        self.root = tk.Tk()
        self.root.withdraw()

        self.collector = MetricsCollector(interval_ms=int(config.get("poll_ms", 1000)))
        self.adapters: List[GpuAdapter] = enumerate_adapters(use_cache=False)
        self.backend: SwitchBackend = get_backend()

        self.hardware = detect_hardware()
        self.lock = GpuLock(
            self.hardware,
            on_event=self._on_lock_event,
            record=LockRecord.from_dict(config.get("lock")),
        )

        self._apply_dxgiself_hint()

        self._asus = _asus_available()
        if self._asus:
            from .asus_ui import AsusPanel

            self.panel = AsusPanel(
                self.root,
                self.collector,
                adapters_provider=self._current_adapters,
                on_mode=self.request_hardware_mode,
                config=config,
                on_refresh=self.reload_adapters,
                lock=self.lock,
            )
            logger.info("检测到华硕机型，使用华硕专用面板")
        else:
            self.panel = GpuPanel(
                self.root,
                self.adapters,
                self.collector,
                on_switch=self.request_switch,
                config=config,
                on_refresh=self.reload_adapters,
                on_open_settings=self.open_graphics_settings,
                lock=self.lock,
                on_lock_toggle=self.toggle_lock,
            )
        self.panel.build()

        self.hotkey: Optional[GlobalHotkey] = None
        self._setup_hotkey()
        self.root.after(120, self._drain_hotkey)

        self._restore_lock()
        self._setup_watcher()

        logger.info(
            "%s %s 启动完成：%d 块显卡，采集后端 %s，切换后端 %s，硬件通道 %s，进程监听 %s",
            APP_NAME, __version__, len(self.adapters), self.collector.backend,
            self.backend.name, self.hardware.name, self.watcher.mode or "未启用",
        )


    def _app_rules(self) -> List[LaunchRule]:
        rules: List[LaunchRule] = []
        for item in self.config.get("apps", []) or []:
            rule = LaunchRule.from_dict(item) if isinstance(item, dict) else None
            if rule is not None:
                rules.append(rule)
        return rules

    def _setup_watcher(self) -> None:
        rules = self._app_rules()
        self.watcher = ProcessWatcher(
            rules_provider=self._app_rules,
            on_detected=self._on_process_detected,
            cooldown=float(self.config.get("watch_cooldown", 10.0) or 10.0),
        )
        if not rules:
            logger.info("没有配置按程序指定显卡的规则，进程监听待命")
            return
        if not self.config.get("watch_processes", False):
            logger.info("已配置 %d 条规则，但 watch_processes 未开启", len(rules))
            return
        if self.watcher.start():
            logger.info("进程监听已启动，共 %d 条规则", len(rules))

    def _on_process_detected(self, rule: LaunchRule, pid: int, cmdline: List[str]) -> None:
        text = f"检测到「{rule.name}」启动，已写入显卡偏好：{rule.preference_label}"
        if rule.ask:
            text += "（已按询问设置处理）"
        if rule.restart:
            text += "；正在重启该进程让偏好生效"
            self.panel.queue_message(text, "warn")
            threading.Thread(
                target=lambda: terminate_and_relaunch(rule.path, cmdline or None),
                name="relaunch",
                daemon=True,
            ).start()
            return
        text += "；若它已经开始渲染，重启该程序后偏好才会生效"
        self.panel.queue_message(text, "info")


    def _restore_lock(self) -> None:
        if not self.config.get("lock_restore_on_start", True):
            return
        if not self.lock.record.enabled or not self.lock.record.mode_id:
            return
        if not self.hardware.available():
            logger.info("上次的锁定记录存在，但本机没有硬件通道，跳过恢复")
            return

        result = self.lock.reapply()
        if result.ok:
            self.lock.start(float(self.config.get("lock_interval", 4.0) or 4.0))
            self.panel.queue_message(f"已恢复上次的显卡锁定：{result.message}", "ok")
            logger.info("已恢复锁定: %s", result.message)
        else:
            self.panel.queue_message(
                f"恢复上次锁定失败：{result.message} {result.detail}", "warn"
            )

    def _on_lock_event(self, event: LockEvent) -> None:
        kind = "ok" if event.kind in ("restored", "applied") else "warn"
        text = event.message
        if event.detail:
            text = f"{text}（{event.detail}）"
        self.panel.queue_message(text, kind)

    def toggle_lock(self, mode) -> None:
        if not self.hardware.available():
            self.panel.set_message(
                "本机没有厂商硬件通道，无法做硬件级锁定；已改用 Windows 图形首选项守护",
                "warn",
            )
            return

        status = self.lock.status()
        if status.get("enabled") and status.get("mode_id") == mode.mode_id:
            self.lock.disable()
            self._save_lock()
            self.panel.set_message(f"已解除锁定「{mode.label}」", "info")
            self.panel.refresh_hardware()
            return

        if mode.needs_reboot or mode.may_blackout:
            warning = (
                f"「{mode.label}」会重新配置显示通路，屏幕可能黑屏数秒"
                + ("，并且需要重启才能完全生效" if mode.needs_reboot else "")
                + "。确定继续吗？"
            )
            if not self._confirm(warning):
                self.panel.set_message("已取消切换", "info")
                return

        self.panel.set_message(f"正在锁定「{mode.label}」…", "info")
        self.root.update_idletasks()
        try:
            result = self.lock.enable(mode, source="ui")
        except Exception as exc:
            logger.exception("锁定失败")
            self.panel.set_message(f"锁定失败：{exc}", "error")
            return

        if result.ok:
            self.lock.start(float(self.config.get("lock_interval", 4.0) or 4.0))
            self._save_lock()
            parts = [result.message]
            if result.detail:
                parts.append(result.detail)
            parts.append("其它工具再改回去，本程序会自动抢回来")
            self.panel.set_message("；".join(parts), "ok")
        else:
            self.panel.set_message(f"锁定失败：{result.message} {result.detail}", "error")
        self.panel.refresh_hardware()

    def _save_lock(self) -> None:
        self.config["lock"] = self.lock.record.to_dict()
        config_module.save(self.config)

    def _confirm(self, message: str) -> bool:
        try:
            from tkinter import messagebox

            return bool(
                messagebox.askyesno("确认切换", message, parent=self.root, default="no")
            )
        except Exception:
            return False


    def _apply_dxgiself_hint(self) -> None:
        try:
            current = describe_current(self.adapters)
            if current and current.vendor_id:
                set_process_default_adapter(current.vendor_id, current.device_id)
        except Exception:
            pass

    FALLBACK_HOTKEYS = ("ctrl+alt+g", "ctrl+shift+g", "ctrl+alt+f12", "ctrl+shift+f12")

    def _setup_hotkey(self) -> None:
        configured = str(
            os.environ.get("GPUSWITCH_HOTKEY") or self.config.get("hotkey", "ctrl+a")
        )

        candidates = [configured]
        if self.config.get("hotkey_fallback", True):
            for item in self.FALLBACK_HOTKEYS:
                if item not in candidates:
                    candidates.append(item)

        errors = []
        for index, text in enumerate(candidates):
            try:
                candidate = GlobalHotkey(text, self._on_hotkey)
            except HotkeyError as exc:
                errors.append(f"{text}: {exc}")
                continue
            if candidate.start():
                self.hotkey = candidate
                if text != configured:
                    self.config["hotkey"] = text
                    config_module.save(self.config)
                    logger.warning("快捷键 %s 不可用，已自动改用 %s", configured, text)
                    self.panel.set_message(
                        f"快捷键 {configured} 被占用，已自动改用 {text}（已写入配置）",
                        "warn",
                    )
                else:
                    logger.info("快捷键 %s 注册成功", text)
                return
            errors.append(f"{text}: {candidate.error or '被占用'}")

        detail = "；".join(errors)
        logger.error("所有候选快捷键都注册失败: %s", detail)
        self.panel.set_message(
            "全局快捷键注册失败（候选都被占用），可在配置里换一个后重启：\n" + detail,
            "error",
        )


    def _on_hotkey(self) -> None:
        self.pending_hotkey += 1

    def _drain_hotkey(self) -> None:
        if self.pending_hotkey:
            self.pending_hotkey = 0
            if self.config.get("toggle_on_hotkey", True):
                self.panel.toggle()
            else:
                self.panel.show()
        self.root.after(120, self._drain_hotkey)


    def _current_adapters(self) -> List[GpuAdapter]:
        adapters = enumerate_adapters(use_cache=True, cache_ms=4000)
        self.adapters = adapters
        return adapters

    def request_hardware_mode(self, mode) -> None:
        name = getattr(mode, "label", str(mode))

        if getattr(mode, "may_blackout", False) or getattr(mode, "needs_reboot", False):
            if not self._confirm(
                f"切换到「{name}」会重新配置显示通路，屏幕可能黑屏数秒"
                + ("，并且需要重启才能完全生效" if mode.needs_reboot else "")
                + "。\n\n确定继续吗？"
            ):
                self.panel.set_message("已取消切换", "info")
                return

        self.panel.set_message(f"正在切换到「{name}」…", "info")
        self.root.update_idletasks()

        self.lock.expect_change()

        try:
            result = self._apply_asus_mode(mode)
        except Exception as exc:
            logger.exception("硬件模式切换失败")
            self.lock.cancel_change()
            self.panel.set_message(f"切换失败：{exc}", "error")
            return

        if not result.ok:
            self.lock.cancel_change()
            self.panel.set_message(f"切换失败：{result.message} {result.detail}", "error")
            self.panel.refresh_static()
            return

        lock_note = ""
        if self.config.get("auto_lock_on_switch", True):
            try:
                from .hardware import HardwareMode

                lock_result = self.lock.adopt(
                    HardwareMode(
                        mode_id=mode.mode_id,
                        label=mode.label,
                        description=mode.description,
                        needs_reboot=mode.needs_reboot,
                        may_blackout=mode.may_blackout,
                        eco=mode.eco,
                        mux=mode.mux,
                    ),
                    source="mode-switch",
                )
                if lock_result.ok:
                    self.lock.start(float(self.config.get("lock_interval", 4.0) or 4.0))
                    self._save_lock()
                    lock_note = "已按新模式锁定，外部改动会自动抢回"
                else:
                    lock_note = f"锁定未生效：{lock_result.message}"
            except Exception as exc:
                logger.debug("重新锁定失败: %s", exc)
                lock_note = f"重新锁定失败：{exc}"
        else:
            self.lock.cancel_change()

        parts = [result.message]
        if result.detail:
            parts.append(result.detail)
        if result.changed:
            parts.append("硬件已切换完成，正在运行的程序无需重启")
        if result.needs_reboot:
            parts.append("显示通路变更需要重启才能完全生效")
        if lock_note:
            parts.append(lock_note)

        self.panel.set_message("\n".join(parts), "ok")
        logger.info("硬件模式切换成功: %s | %s", name, result.message)

        invalidate_cache()
        self.panel.refresh_static()

    def _apply_asus_mode(self, mode):
        from .hardware import ECO_OFF, ECO_ON, MUX_ULTIMATE, HardwareResult

        state = self.hardware.read_state()
        if not state.available:
            return HardwareResult(False, "华硕 ACPI 接口不可用")

        needs_reboot = False
        may_blackout = False

        if mode.mux is not None and state.mux is not None and mode.mux != state.mux:
            if mode.eco == ECO_OFF and state.mux == MUX_ULTIMATE:
                return HardwareResult(
                    False, "当前是「独显直连」，不能关闭独显",
                    detail="请先切回标准模式并重启，否则屏幕会黑",
                )
            logger.warning("切换 MUX: %s -> %s（可能黑屏）", state.mux, mode.mux)
            if self.hardware._acpi.set(self.hardware._mux_id, mode.mux) != 1:
                return HardwareResult(False, "MUX 切换被固件拒绝")
            may_blackout = True
            needs_reboot = True
            time.sleep(1.5)
            state = self.hardware.read_state()

        changed = False
        note = ""
        if mode.eco is not None and state.eco is not None and mode.eco != state.eco:
            if mode.eco == ECO_OFF and state.mux == MUX_ULTIMATE:
                return HardwareResult(False, "独显直连下不能关闭独显")

            from .gpu_power import set_gpu_eco

            eco_result = set_gpu_eco(
                mode.eco,
                self.hardware._acpi,
                self.hardware._eco_id,
                on_progress=lambda text: self.panel.set_message(f"{mode.label}：{text}", "info"),
            )
            if not eco_result.ok:
                return HardwareResult(
                    False, eco_result.message, detail=eco_result.detail
                )
            changed = True
            note = eco_result.detail

        after = self.hardware.read_state()

        message = (
            f"已切换到「{mode.label}」"
            if (changed or may_blackout)
            else f"硬件状态本来就是「{mode.label}」，无需切换"
        )
        detail = f"显示通路 MUX={after.mux}"
        if note:
            detail += f"；{note}"
        return HardwareResult(
            True, message,
            detail=detail,
            changed=changed or may_blackout,
            needs_reboot=needs_reboot,
            may_blackout=may_blackout,
        )


    def request_switch(self, adapter: GpuAdapter) -> None:
        if adapter.kind == TYPE_VIRTUAL:
            self.panel.set_message(f"{adapter.name} 是虚拟显卡，不参与切换", "warn")
            return

        mode = self._hardware_mode_for(adapter)
        hardware_ready = bool(mode) and self.hardware.available()

        if hardware_ready:
            state = self.hardware.read_state()
            if mode is not None and self.lock._satisfied(mode, state):
                self.panel.set_message(
                    f"当前硬件状态已经满足「{mode.label}」，无需切换", "info"
                )
                return

        self.panel.set_message(f"正在切换到 {adapter.name} …", "info")
        self.root.update_idletasks()

        hardware_note = ""
        if hardware_ready and mode is not None:
            if mode.needs_reboot or mode.may_blackout:
                if not self._confirm(
                    f"切换到「{mode.label}」会重新配置显示通路，"
                    "屏幕可能黑屏数秒，并且需要重启才能完全生效。继续吗？"
                ):
                    self.panel.set_message("已取消切换", "info")
                    return
            try:
                result = self.lock.enable(mode, source="switch")
            except Exception as exc:
                logger.exception("硬件切换失败")
                self.panel.set_message(f"硬件切换失败：{exc}", "error")
                return

            if not result.ok:
                self.panel.set_message(
                    f"硬件切换失败：{result.message} {result.detail}", "error"
                )
                return

            hardware_note = result.message
            if result.changed and self.config.get("auto_lock_on_switch", True):
                self.lock.start(float(self.config.get("lock_interval", 4.0) or 4.0))
                self._save_lock()
                hardware_note += "，并已锁定（被其它工具改回去会自动抢回）"
            elif result.changed:
                hardware_note += "（未开启锁定）"
            self.panel.refresh_hardware()

        pref_note = ""
        try:
            scope = str(self.config.get("switch_scope", "self")).lower()
            if scope == "global":
                pref = self.backend.set_global(adapter, elevate=True)
            else:
                pref = self.backend.set_for_app(adapter, sys.executable)
            pref_note = pref.message
            if not pref.ok and pref.detail:
                pref_note += f"（{pref.detail}）"
        except Exception as exc:
            logger.exception("写入显卡偏好失败")
            pref_note = f"写入显卡偏好失败：{exc}"

        parts = []
        if hardware_note:
            parts.append(f"硬件切换：{hardware_note}")
        parts.append(f"显卡偏好：{pref_note}")
        if not hardware_ready:
            parts.append(
                "本机没有厂商硬件通道，只能改偏好——"
                "它在该程序下次启动时生效，已在运行的程序不会改变"
            )
        elif hardware_note:
            parts.append("已运行的程序无需重启")

        kind = "ok" if hardware_ready or "已" in pref_note else "warn"
        self.panel.set_message("\n".join(parts), kind)
        logger.info(
            "切换[%s] 硬件=%s 偏好=%s", adapter.name, hardware_note or "未做", pref_note
        )

        invalidate_cache()
        self.panel.adapters = self.reload_adapters()
        self.panel.refresh_static()

    def _hardware_mode_for(self, adapter: GpuAdapter):
        if not self.hardware.available():
            return None
        want = "dgpu_on" if adapter.vendor_id != 0x8086 else "dgpu_off"
        if adapter.kind == TYPE_INTEGRATED:
            want = "dgpu_off"
        elif adapter.kind == TYPE_DISCRETE:
            want = "dgpu_on"
        return self.lock.find_mode(want)

    def _report(self, result: SwitchResult, adapter: GpuAdapter) -> None:
        parts = [result.message]
        if result.detail:
            parts.append(result.detail)
        if result.ok and result.needs_restart:
            parts.append("Windows 的显卡归属在程序启动时决定，新启动的程序会使用新设置")
        if not result.ok and result.needs_elevation and not is_elevated():
            parts.append("需要管理员权限：请以管理员身份重新运行本程序")
        text = "\n".join(parts)
        kind = "ok" if result.ok else ("warn" if result.needs_elevation else "error")
        self.panel.set_message(text, kind)
        logger.info("切换结果[%s]: %s | %s", adapter.name, result.ok, result.message)


    def reload_adapters(self) -> List[GpuAdapter]:
        invalidate_cache()
        self.adapters = enumerate_adapters(use_cache=False)
        self.panel.adapters = self.adapters
        return self.adapters

    def open_graphics_settings(self) -> None:
        if not is_windows():
            self.panel.set_message("当前平台没有等价的图形设置面板", "warn")
            return
        try:
            os.startfile("ms-settings:display-advancedgraphics")
            self.panel.set_message("已打开 Windows 图形设置", "ok")
        except Exception:
            try:
                os.startfile("ms-settings:display")
            except Exception as exc:
                self.panel.set_message(f"无法打开设置：{exc}", "error")


    def run(self) -> int:
        self.collector.start()
        if not self.start_hidden:
            self.panel.show()
        else:
            text = str(self.config.get("hotkey", "ctrl+a"))
            logger.info("面板已就绪，按 %s 呼出", text)
            print(f"[{APP_NAME}] 已就绪，按 {text} 呼出面板（Ctrl+C 退出）")
        try:
            self.root.mainloop()
        finally:
            self.shutdown()
        return 0

    def shutdown(self) -> None:
        logger.info("正在退出…")
        if self.hotkey:
            self.hotkey.stop()
        try:
            self.lock.stop()
        except Exception:
            pass
        try:
            if getattr(self, "watcher", None) is not None:
                self.watcher.stop()
        except Exception:
            pass
        self.collector.stop()

def run_apps_cli(args) -> int:
    from .watcher import (
        LaunchRule,
        delete_preference,
        launch_with_gpu,
        read_all_preferences,
        write_preference,
    )

    config_module.ensure_exists()
    cfg = config_module.load()
    rules: List[dict] = [r for r in (cfg.get("apps") or []) if isinstance(r, dict)]

    def save() -> None:
        cfg["apps"] = rules
        if args.watch_apps:
            cfg["watch_processes"] = True
        config_module.save(cfg)

    if args.app_list:
        print(f"进程拦截：{'已开启' if cfg.get('watch_processes') else '未开启'}")
        print(f"规则数量：{len(rules)}")
        if rules:
            print()
            for item in rules:
                rule = LaunchRule.from_dict(item)
                if rule is None:
                    continue
                flags = []
                if rule.ask:
                    flags.append("启动时询问")
                if rule.restart:
                    flags.append("自动重启")
                suffix = f"  [{', '.join(flags)}]" if flags else ""
                print(f"  {rule.name:<28} {rule.preference_label}{suffix}")
                print(f"      {rule.path}")
        prefs = read_all_preferences()
        if prefs:
            print(f"\n注册表 HKCU\\...\\UserGpuPreferences 共 {len(prefs)} 条：")
            for path, value in list(prefs.items())[:12]:
                from .watcher import parse_preference

                pref, ask = parse_preference(value)
                label = {0: "系统默认", 1: "集显", 2: "独显"}.get(pref, "?")
                print(f"  [{label}{' · 询问' if ask else ''}] {path}")
        return 0

    if args.app_add:
        path = str(Path(args.app_add))
        if not path.lower().endswith(".exe"):
            print(f"请指定 .exe 文件：{path}", file=sys.stderr)
            return 2
        if not os.path.exists(path):
            print(f"文件不存在：{path}", file=sys.stderr)
            return 2

        rules = [r for r in rules if str(Path(str(r.get("path", "")))).lower() != path.lower()]
        rules.append(
            LaunchRule(
                path=path, preference=args.pref, ask=args.ask, restart=args.restart
            ).to_dict()
        )
        save()
        ok = write_preference(path, args.pref, ask=args.ask)
        label = {0: "系统默认", 1: "省电（集显）", 2: "高性能（独显）"}[args.pref]
        print(f"已添加规则：{Path(path).name} -> {label}")
        print(f"注册表偏好写入：{'成功' if ok else '失败'}")
        if args.ask:
            print("该程序启动时会弹窗询问用哪块卡（需要在程序里开启进程拦截）")
        if args.restart:
            print("注意：自动重启会结束该程序，未保存的数据会丢失")
        print(f"\n配置已保存。开启进程拦截：--app-add ... --watch-apps")
        return 0

    if args.app_remove:
        path = str(Path(args.app_remove)).lower()
        before = len(rules)
        rules = [r for r in rules if str(Path(str(r.get("path", "")))).lower() != path]
        save()
        removed = delete_preference(args.app_remove)
        print(f"已删除 {before - len(rules)} 条规则，注册表清理：{'成功' if removed else '无该项'}")
        return 0

    if args.app_launch:
        path = str(Path(args.app_launch))
        rule = None
        for item in rules:
            candidate = LaunchRule.from_dict(item)
            if candidate and str(Path(candidate.path)).lower() == path.lower():
                rule = candidate
                break
        if rule is None:
            rule = LaunchRule(path=path, preference=args.pref or 2)
            print(f"未找到规则，临时按「{rule.preference_label}」启动一次")
        if launch_with_gpu(rule):
            print(f"已用「{rule.preference_label}」启动 {Path(path).name}")
            return 0
        print("启动失败", file=sys.stderr)
        return 1

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gpuswitch",
        description="全局快捷键呼出的显卡监控与切换面板",
    )
    parser.add_argument("--show", action="store_true", help="启动后直接显示面板")
    parser.add_argument("--check", action="store_true", help="运行环境自检后退出")
    parser.add_argument("--json", action="store_true", help="打印显卡信息(JSON)后退出")
    parser.add_argument("--test-hotkey", metavar="KEY", help="检测某个快捷键是否可用")
    parser.add_argument("--reset-config", action="store_true", help="恢复默认配置后退出")
    parser.add_argument(
        "--lock", metavar="MODE",
        help="直接锁定某个硬件模式：dgpu_on / dgpu_off / ultimate / standard",
    )
    parser.add_argument("--unlock", action="store_true", help="解除显卡锁定")
    parser.add_argument("--yes", action="store_true", help="跳过危险操作确认（脚本化用）")
    parser.add_argument("--hw-status", action="store_true", help="打印显卡硬件状态后退出")
    parser.add_argument("--asus-test", action="store_true",
                        help="华硕接口逐项自检（只读，不改变任何状态）")
    parser.add_argument("--watch-lock", action="store_true",
                        help="在终端里前台运行锁定守护（Ctrl+C 退出）")
    parser.add_argument("--app-list", action="store_true", help="列出按程序指定显卡的规则")
    parser.add_argument("--app-add", metavar="EXE", help="添加一条规则（配合 --pref / --ask / --restart）")
    parser.add_argument("--app-remove", metavar="EXE", help="删除一条规则")
    parser.add_argument("--app-launch", metavar="EXE",
                        help="按规则启动该程序（先写偏好再启动，最可靠）")
    parser.add_argument("--pref", type=int, choices=[0, 1, 2], default=2,
                        help="0 系统默认 / 1 省电(集显) / 2 高性能(独显)，默认 2")
    parser.add_argument("--ask", action="store_true", help="该程序启动时询问用哪块卡")
    parser.add_argument("--restart", action="store_true",
                        help="拦截到启动后自动重启该进程（会丢未保存数据）")
    parser.add_argument("--watch-apps", action="store_true",
                        help="开启进程拦截（与 --app-add 一起用可持久化）")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return parser


def run_lock_cli(args) -> int:
    from .hardware import nvidia_power_state

    config_module.ensure_exists()
    cfg = config_module.load()
    hardware = detect_hardware()
    events: List[str] = []

    def collect(event: LockEvent) -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] {event.kind}: {event.message}"
        if event.detail:
            line += f"  ({event.detail})"
        events.append(line)
        print(line, flush=True)

    lock = GpuLock(hardware, on_event=collect, record=LockRecord.from_dict(cfg.get("lock")))

    if args.hw_status:
        print(f"硬件通道: {hardware.name} — {hardware.description}")
        state = hardware.read_state()
        print(f"可用: {'是' if state.available else '否'}")
        if state.available:
            print(f"独显(eco) = {state.eco}   显示通路(mux) = {state.mux}")
            print(f"状态: {state.detail}")
            print(f"模式标识: {state.mode_id}")
            print("\n可锁定模式:")
            for mode in hardware.modes():
                marks = []
                if mode.needs_reboot:
                    marks.append("需重启")
                if mode.may_blackout:
                    marks.append("会黑屏")
                suffix = f"  [{', '.join(marks)}]" if marks else ""
                print(f"  {mode.mode_id:<10} {mode.label:<8} {mode.description}{suffix}")
        power = nvidia_power_state()
        if power:
            print(f"\nnvidia-smi: {power}")
        record = lock.record
        print(f"\n当前锁定: {'开启' if record.enabled else '关闭'}"
              + (f"（模式 {record.mode_id}，已抢回 {lock.restore_count} 次）" if record.enabled else ""))
        return 0

    if args.unlock:
        lock.disable()
        cfg["lock"] = lock.record.to_dict()
        config_module.save(cfg)
        print("已解除锁定")
        return 0

    if args.lock:
        mode_id = args.lock.strip()
        mode = lock.find_mode(mode_id)
        if mode is None:
            available = ", ".join(m.mode_id for m in hardware.modes()) or "（无）"
            print(f"未知模式 {mode_id!r}；可用: {available}", file=sys.stderr)
            return 2
        if (mode.needs_reboot or mode.may_blackout) and not args.yes:
            print(
                f"「{mode.label}」会重新配置显示通路，可能黑屏数秒"
                + ("并需要重启" if mode.needs_reboot else "")
                + "。确认请加 --yes",
                file=sys.stderr,
            )
            return 3
        result = lock.enable(mode, source="cli")
        if not result.ok:
            print(f"锁定失败: {result.message} {result.detail}", file=sys.stderr)
            return 1
        cfg["lock"] = lock.record.to_dict()
        config_module.save(cfg)
        print(f"已锁定: {result.message}  {result.detail}")

        if args.watch_lock:
            print("\n前台守护中（每 4 秒检查一次，试着用奥创/G-Helper 改一下看会不会被抢回）。")
            print("按 Ctrl+C 退出。\n")
            lock.start(4.0)
            try:
                while True:
                    time.sleep(1.0)
            except KeyboardInterrupt:
                print("\n退出守护")
            finally:
                lock.stop()
        return 0

    if args.watch_lock:
        if not lock.record.enabled:
            print("当前没有锁定任何模式，先执行 --lock <模式>", file=sys.stderr)
            return 2
        print("前台守护中，按 Ctrl+C 退出。")
        lock.start(4.0)
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            lock.stop()
        return 0

    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    log.setup(level=10 if os.environ.get("GPUSWITCH_DEBUG") else 20)

    if args.check:
        from .selfcheck import run_check

        return run_check()
    if args.json:
        from .selfcheck import run_json

        return run_json()
    if args.test_hotkey:
        ok, message = probe_hotkey(args.test_hotkey)
        print(("✓ " if ok else "✗ ") + message)
        return 0 if ok else 1
    if args.reset_config:
        path = config_module.save(config_module.DEFAULTS)
        print(f"已恢复默认配置：{path}")
        return 0
    if args.asus_test:
        from .asusprobe import run_self_test

        report = run_self_test()
        print(report.text())
        print()
        print(f"结论：{'全部通过' if report.passed else '存在异常，见上面 ✗ 项'}")
        return 0 if report.passed else 1
    if args.lock or args.unlock or args.hw_status or args.watch_lock:
        return run_lock_cli(args)
    if args.app_list or args.app_add or args.app_remove or args.app_launch:
        return run_apps_cli(args)

    config_module.ensure_exists()
    cfg = config_module.load()
    try:
        app = GpuSwitchApp(cfg, start_hidden=not args.show)
    except tk.TclError as exc:
        print(f"无法创建界面（缺少显示环境？）：{exc}", file=sys.stderr)
        return 2
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
