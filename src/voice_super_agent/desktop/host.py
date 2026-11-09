from collections import deque
import ctypes
from ctypes import wintypes
import json
import logging
from queue import Empty
from threading import Thread

import webview

from voice_super_agent.runtime import Runtime


logger = logging.getLogger(__name__)
user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = wintypes.LONG
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
user32.SetWindowLongW.restype = wintypes.LONG
user32.GetDpiForWindow.argtypes = [wintypes.HWND]
user32.GetDpiForWindow.restype = wintypes.UINT


class DesktopApi:
    """页面命令和订阅均交给同一后台循环处理。"""

    def __init__(self, runtime: Runtime, view: str):
        self._runtime = runtime
        self._view = view

    def connect(self):
        return {"view": self._view, **self._runtime.connect(self._view)}

    def command(self, name: str) -> None:
        self._runtime.command(name)

    def send_text(self, text: str):
        return self._runtime.send_text(text)

    def resend_text(self, user_message_id: str):
        return self._runtime.resend_text(user_message_id)


class Desktop:
    """管理窗口与托盘；独立发送线程承接阻塞的 WebView 脚本调用。"""

    def __init__(self, runtime: Runtime, page: str):
        self.runtime = runtime
        self._quitting = False
        self._expanded = True
        self.main = webview.create_window(
            "小博 · Voice Super Agent", url=page,
            js_api=DesktopApi(runtime, "main"), width=960, height=680,
            min_size=(640, 480), background_color="#FBFAF7",
        )
        self.floating = webview.create_window(
            "小博 · 收音窗", url=page,
            js_api=DesktopApi(runtime, "floating"), width=320, height=312,
            min_size=(320, 312), resizable=False, frameless=True,
            easy_drag=False, hidden=True, focus=False, on_top=True,
            shadow=False, background_color="#FBFAF7",
        )
        self.main.events.before_show += self._initialize_main
        self.floating.events.before_show += self._initialize_floating
        self.main.events.before_load += lambda: runtime.disconnect("main")
        self.floating.events.before_load += lambda: runtime.disconnect("floating")
        self._worker = Thread(target=self._pump, name="desktop-events")

    def stop(self) -> None:
        if self._worker.ident is not None:
            self.runtime.events.put(None)
            self._worker.join()

    def activate(self) -> None:
        self.runtime.command("show_main")

    def _initialize_main(self, window) -> None:
        from System.Windows.Forms import ContextMenuStrip, NotifyIcon, ToolStripMenuItem

        form = window.native
        form.FormClosing += self._main_closing
        form.VisibleChanged += self._main_visibility_changed
        form.Resize += self._main_visibility_changed
        self._tray = NotifyIcon()
        self._tray.Icon = form.Icon
        self._tray.Text = "小博 · 后台运行"
        self._tray_status = ("小博 · 后台运行", False)
        menu = ContextMenuStrip()
        show = ToolStripMenuItem("显示主窗口")
        show.Click += lambda *_: self.activate()
        self._show_floating = ToolStripMenuItem("显示收音窗")
        self._show_floating.Enabled = False
        self._show_floating.Click += lambda *_: self.runtime.command("preview_floating")
        self._recover_voice = ToolStripMenuItem("恢复语音")
        self._recover_voice.Enabled = False
        self._recover_voice.Click += lambda *_: self.runtime.command("recover_voice")
        quit_item = ToolStripMenuItem("退出应用")
        quit_item.Click += lambda *_: self.runtime.command("quit")
        for item in (show, self._show_floating, self._recover_voice, quit_item):
            menu.Items.Add(item)
        self._tray.ContextMenuStrip = menu
        self._tray.DoubleClick += lambda *_: self.activate()
        self._tray.Visible = True
        self._worker.start()
        logger.info("主窗口与托盘已准备")

    def _initialize_floating(self, window) -> None:
        form = window.native
        # 用 WS_EX_TOOLWINDOW 替代 WS_EX_APPWINDOW，保留正在初始化 WebView2 的 HWND。
        handle = form.Handle.ToInt64()
        style = user32.GetWindowLongW(handle, -20)
        user32.SetWindowLongW(handle, -20, (style | 0x80) & ~0x40000)
        form.FormClosing += self._floating_closing
        form.DpiChanged += lambda *_: self._shape_floating()
        self._shape_floating()

    def _shape_floating(self) -> None:
        from System.Drawing import Point, Region
        from System.Drawing.Drawing2D import GraphicsPath
        from System.Windows.Forms import Screen

        form = self.floating.native
        scale = user32.GetDpiForWindow(form.Handle.ToInt64()) / 96
        path = GraphicsPath()
        if self._expanded:
            # 物理区域只覆盖圆角卡片与圆球，间隙及外角由系统裁去。
            radius = 18 * scale
            width, height = 320 * scale, 224 * scale
            diameter = radius * 2
            for x, y, angle in ((0, 0, 180), (width - diameter, 0, 270),
                                (width - diameter, height - diameter, 0),
                                (0, height - diameter, 90)):
                path.AddArc(float(x), float(y), float(diameter), float(diameter), float(angle), 90.0)
            path.CloseFigure()
        path.AddEllipse(float(248 * scale), float(244 * scale), float(64 * scale), float(64 * scale))
        previous = form.Region
        form.Region = Region(path)
        path.Dispose()
        if previous is not None:
            previous.Dispose()
        work_area = Screen.FromHandle(self.main.native.Handle).WorkingArea
        form.Location = Point(work_area.Right - form.Width - int(20 * scale),
                              work_area.Bottom - form.Height - int(20 * scale))

    def _main_closing(self, sender, args) -> None:
        from System.Windows.Forms import CloseReason

        if self._quitting:
            return
        args.Cancel = True
        if args.CloseReason == CloseReason.ApplicationExitCall:
            self.runtime.command("quit")
        else:
            sender.Hide()
            logger.info("主窗口关闭后隐藏，应用继续常驻")

    def _floating_closing(self, sender, args) -> None:
        if not self._quitting:
            args.Cancel = True
            sender.Hide()
            self.runtime.update_windows(floating_visible=False)

    def _main_visibility_changed(self, *_args) -> None:
        from System.Windows.Forms import FormWindowState

        form = self.main.native
        if not form.Visible:
            state = "hidden"
        elif form.WindowState == FormWindowState.Minimized:
            state = "minimized"
        else:
            state = "visible"
        self.runtime.update_windows(main_window=state)
        if state == "visible" and self.floating.native is not None:
            self.floating.native.Hide()
            self.runtime.update_windows(floating_visible=False)

    def _gui(self, action) -> None:
        from System import Action

        self.main.native.BeginInvoke(Action(action))

    def _handle_command(self, command: str) -> bool:
        from System.Windows.Forms import FormWindowState

        if command == "quit":
            if not self.runtime.prepare_exit():
                self.activate()
                return True
            self._quitting = True
            self.runtime.disconnect("main")
            self.runtime.disconnect("floating")
            self._gui(self._close_windows)
            return False
        if command == "show_main":
            def show():
                self.main.native.Show()
                self.main.native.WindowState = FormWindowState.Normal
                self.main.native.Activate()
                logger.info("显示已有主窗口")
            self._gui(show)
        elif command == "hide_main":
            self._gui(self.main.native.Hide)
        elif command == "preview_floating":
            self._gui(self._show_preview)
        elif command == "hide_floating":
            def hide():
                self.floating.native.Hide()
                self.runtime.update_windows(floating_visible=False)
            self._gui(hide)
        elif command == "toggle_preview_card":
            def toggle():
                self._expanded = not self._expanded
                self._shape_floating()
                self.runtime.update_windows(preview_expanded=self._expanded)
            self._gui(toggle)
        return True

    def _show_preview(self) -> None:
        self.main.native.Hide()
        self._shape_floating()
        # 避开 pywebview.show() 的 Activate 路径，保留 focus=False 的原生样式。
        self.floating.native.Show()
        self._show_floating.Enabled = True
        self.runtime.update_windows(floating_visible=True, preview_expanded=self._expanded)
        logger.info("显示悬浮宿主，DPI：%s", user32.GetDpiForWindow(self.floating.native.Handle.ToInt64()))

    def _close_windows(self) -> None:
        self._tray.Visible = False
        self._tray.Dispose()
        self.floating.native.Close()
        self.main.native.Close()

    def _pump(self) -> None:
        pending = deque()
        while True:
            event = pending.popleft() if pending else self.runtime.events.get()
            if event is None:
                return
            if event["kind"] == "command":
                if not self._handle_command(event["command"]):
                    return
                continue
            # 状态为完整快照，连续状态合并为最新一份；窗口命令保留先后顺序。
            while True:
                try:
                    following = self.runtime.events.get_nowait()
                except Empty:
                    break
                if following is None or following["kind"] == "command":
                    pending.append(following)
                    break
                event = following
            state = event["snapshot"]["state"]
            tray_status = ("小博 · 模型不可用" if not state["model_available"] else
                           "小博 · 正在恢复语音" if state["voice_recovery"] == "running" else
                           "小博 · 语音不可用" if state["voice_status"] == "error" else "小博 · 后台运行")
            can_recover = (state["voice_status"] == "error" and state["model_available"]
                           and not state["storage_error"] and not state["exiting"])
            if (tray_status, can_recover) != self._tray_status:
                self._tray_status = (tray_status, can_recover)
                def update_tray(status=tray_status, can_recover=can_recover):
                    self._tray.Text = status
                    self._recover_voice.Enabled = can_recover
                self._gui(update_tray)
            script = "window.dispatchEvent(new CustomEvent('agent-state', {detail: " + json.dumps(event["snapshot"]) + "}))"
            for view in event["views"]:
                window = self.main if view == "main" else self.floating
                window.evaluate_js(script)
