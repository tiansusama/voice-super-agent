import asyncio
from collections import deque
from concurrent.futures import Future
import ctypes
from ctypes import wintypes
import json
import logging
from math import ceil
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
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
dwmapi = ctypes.WinDLL("dwmapi")
dwmapi.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
dwmapi.DwmSetWindowAttribute.restype = wintypes.LONG


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

    def save_setting(self, name: str, value):
        return self._runtime.save_setting(name, value)

    def list_history(self, before_position: int | None = None):
        return self._runtime.list_history(before_position)

    def read_history(self, message_id: str, offset: int = 0):
        return self._runtime.read_history(message_id, offset)

    def list_tasks(self, cursor: int | None = None):
        return self._runtime.list_tasks(cursor)

    def get_task(self, task_id: str):
        return self._runtime.get_task(task_id)

    def task_command(self, task_id: str, action: str, payload: dict | None = None):
        return self._runtime.task_command(task_id, action, payload)

    def save_task_concurrency(self, limit: int):
        return self._runtime.save_task_concurrency(limit)

    def mark_task_read(self, notification_id: str):
        self._runtime.mark_task_read(notification_id)

    def floating_layout(self, card_height: float, controls_visible: bool, hint_visible: bool) -> None:
        self._runtime.update_windows(floating_card_height=ceil(card_height),
                                     floating_controls_visible=controls_visible, floating_hint_visible=hint_visible)


class Desktop:
    """管理窗口与托盘；独立发送线程承接阻塞的 WebView 脚本调用。"""

    def __init__(self, runtime: Runtime, page: str):
        self.runtime = runtime
        self._quitting = False
        self._main_ready: Future[None] = Future()
        self._floating_layout = (False, True, 0, False, False, False)
        self._floating_target = self._floating_layout
        self.main = webview.create_window(
            "小博 · Voice Super Agent", url=page,
            js_api=DesktopApi(runtime, "main"), width=960, height=680,
            min_size=(640, 480), background_color="#FBFAF7",
        )
        self.floating = webview.create_window(
            "小博 · 收音窗", url=page,
            js_api=DesktopApi(runtime, "floating"), width=320, height=368,
            min_size=(320, 368), resizable=False, frameless=True,
            easy_drag=False, hidden=True, focus=False, on_top=True,
            shadow=False, background_color="#FBFAF7",
        )
        self.main.events.before_show += self._initialize_main
        self.main.events.shown += self._signal_main_ready
        self.floating.events.before_show += self._initialize_floating
        self.main.events.before_load += lambda: runtime.disconnect("main")
        self.floating.events.before_load += lambda: runtime.disconnect("floating")
        self._worker = Thread(target=self._pump, name="desktop-events")

    def stop(self) -> None:
        if not self._main_ready.done():
            self._main_ready.set_exception(RuntimeError("主窗口已关闭。"))
        if self._worker.ident is not None:
            self.runtime.events.put(None)
            self._worker.join()

    def activate(self) -> None:
        self.runtime.command("show_main")

    def _signal_main_ready(self) -> None:
        self._main_ready.set_result(None)

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
        from Microsoft.Win32 import SystemEvents

        form = window.native
        form.FormClosing += self._floating_closing
        form.DpiChanged += lambda *_: self._shape_floating()
        SystemEvents.UserPreferenceChanged += lambda *_: self._gui(self._shape_floating)
        self._sync_floating(self._floating_target)

    def _sync_floating(self, layout: tuple) -> None:
        self._floating_layout = layout
        self._show_floating.Enabled = layout[5]
        if self.floating.native is None:
            return
        self._shape_floating()
        if layout[0]:
            # 通过原生免激活显示路径保留前台窗口，避免 Form.Show 主动选择悬浮窗。
            user32.ShowWindow(self.floating.native.Handle.ToInt64(), 4)
        else:
            self.floating.native.Hide()

    def _shape_floating(self) -> None:
        from System.Drawing import Point, Region
        from System.Drawing.Drawing2D import FillMode, GraphicsPath
        from System.Windows.Forms import Screen

        form = self.floating.native
        handle = form.Handle.ToInt64()
        # 隐藏窗口初始化会重写扩展样式；同步布局时同时恢复免激活的工具窗口样式。
        style = user32.GetWindowLongW(handle, -20)
        user32.SetWindowLongW(handle, -20, (style | 0x8000080) & ~0x40000)
        # Mica 会铺满矩形 HWND；关闭系统背景后，原生区域之外才会透出桌面。
        backdrop = ctypes.c_int(1)
        dwmapi.DwmSetWindowAttribute(handle, 38, ctypes.byref(backdrop), ctypes.sizeof(backdrop))
        scale = user32.GetDpiForWindow(handle) / 96
        path = GraphicsPath(FillMode.Winding)
        _, expanded, card_height, controls, hint, _ = self._floating_layout
        rectangles = []
        if expanded and card_height:
            rectangles.append((4, 252 - card_height, 312, card_height, 18))
        if hint:
            rectangles.append((116, 344, 88, 20, 10))
        # 原生区域跟随实际卡片高度与悬停按钮；卡片和圆球之间的空隙可穿透。
        for left, top, width, height, radius in rectangles:
            left, top, width, height, radius = (value * scale for value in (left, top, width, height, radius))
            diameter = radius * 2
            for x, y, angle in ((left, top, 180), (left + width - diameter, top, 270),
                                (left + width - diameter, top + height - diameter, 0),
                                (left, top + height - diameter, 90)):
                path.AddArc(float(x), float(y), float(diameter), float(diameter), float(angle), 90.0)
            path.CloseFigure()
        path.AddEllipse(float(128 * scale), float(272 * scale), float(64 * scale), float(64 * scale))
        if controls:
            for left in (122, 172):
                path.AddEllipse(float(left * scale), float(256 * scale), float(26 * scale), float(26 * scale))
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
            self.runtime.command("hide_floating")

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

    def _gui(self, action) -> None:
        from System import Action

        self.main.native.BeginInvoke(Action(action))

    async def get_main_visibility(self) -> bool:
        await asyncio.shield(asyncio.wrap_future(self._main_ready))
        return await asyncio.to_thread(self._run_main_on_gui, self._main_is_visible)

    async def set_main_visibility(self, visible: bool) -> bool:
        await asyncio.shield(asyncio.wrap_future(self._main_ready))
        return await asyncio.to_thread(self._run_main_on_gui, lambda: self._set_main_visibility(visible))

    def _run_main_on_gui(self, operation) -> bool:
        from System import Boolean, Func

        return bool(self.main.native.Invoke(Func[Boolean](operation)))

    def _main_is_visible(self) -> bool:
        from System.Windows.Forms import FormWindowState

        form = self.main.native
        return form.Visible and form.WindowState != FormWindowState.Minimized

    def _set_main_visibility(self, visible: bool) -> bool:
        from System.Windows.Forms import FormWindowState

        form = self.main.native
        if visible:
            form.Show()
            form.WindowState = FormWindowState.Normal
            form.Activate()
            logger.info("显示已有主窗口")
        else:
            form.Hide()
        return self._main_is_visible()

    def _handle_command(self, command: str) -> bool:
        if command in {"quit", "quit_ready"}:
            if command == "quit" and not self.runtime.prepare_exit():
                self.activate()
                return True
            self._quitting = True
            self.runtime.disconnect("main")
            self.runtime.disconnect("floating")
            self._gui(self._close_windows)
            return False
        if command == "show_main":
            self._gui(lambda: self._set_main_visibility(True))
        elif command == "hide_main":
            self._gui(lambda: self._set_main_visibility(False))
        elif command == "preview_floating":
            self._gui(self._show_preview)
        return True

    def _show_preview(self) -> None:
        self.main.native.Hide()

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
            layout = (state["floating_visible"], state["preview_expanded"], state["floating_card_height"],
                      state["floating_controls_visible"], state["floating_hint_visible"], state["floating_available"])
            if layout != self._floating_target:
                self._floating_target = layout
                self._gui(lambda layout=layout: self._sync_floating(layout))
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
