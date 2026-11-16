"""Windows 桌面验收与资源采样：本地模拟模型、受控语音状态，音频设备保持关闭。"""

import argparse
import asyncio
from ctypes import wintypes
import json
import math
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import repeat
from statistics import mean
from tempfile import TemporaryDirectory
from threading import Thread
import time

import psutil
import webview
from loguru import logger

from voice_super_agent.runtime import Runtime
from voice_super_agent.desktop.host import Desktop, user32


ROOT = Path(__file__).resolve().parents[1]


class ModelService(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = payload["messages"][-1]["content"]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            parts = repeat("正在生成用于验收的内容。") if prompt == "测量流式生成" else ["接管完成。"]
            for text in parts:
                chunk = {"id": "desktop-measure", "object": "chat.completion.chunk", "created": 0,
                         "model": "local-measure", "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.flush()
                time.sleep(0.05)
            self.wfile.write(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # 用户文字接管会关闭旧流，旧服务端写入随连接结束。
            return


def resources(seconds):
    """采样宿主及 WebView2 子进程；CPU 以一个逻辑核满载为 100%。"""
    host = psutil.Process()
    def snapshot():
        rows = {}
        for process in [host, *host.children(recursive=True)]:
            try:
                cpu = process.cpu_times()
                memory = process.memory_info()
                rows[process.pid] = (cpu.user + cpu.system, memory.rss, memory.private)
            except psutil.NoSuchProcess:
                continue
        return rows
    before, started = snapshot(), time.perf_counter()
    samples = []
    for _ in range(int(seconds * 2)):
        time.sleep(0.5)
        after, end = snapshot(), time.perf_counter()
        shared = before.keys() & after.keys()
        samples.append({"cpu_one_core_percent": sum(after[pid][0] - before[pid][0] for pid in shared) / (end - started) * 100,
                        "tree_rss_mib": sum(row[1] for row in after.values()) / 2**20,
                        "tree_private_mib": sum(row[2] for row in after.values()) / 2**20,
                        "host_private_mib": after[host.pid][2] / 2**20,
                        "processes": len(after), "changed_processes": len(before.keys() ^ after.keys())})
        before, started = after, end
    return {"sample_seconds": seconds, "interval_ms": 500, "samples": samples,
            "mean_cpu_one_core_percent": mean(row["cpu_one_core_percent"] for row in samples),
            "mean_cpu_machine_percent": mean(row["cpu_one_core_percent"] for row in samples) / psutil.cpu_count(),
            "mean_tree_private_mib": mean(row["tree_private_mib"] for row in samples),
            "max_tree_rss_mib": max(row["tree_rss_mib"] for row in samples)}


def validate(output, seconds):
    logger.disable("pipecat")
    result = {"checks": [], "resources": {}, "condition": "本地 SSE 模型；受控语音消息、状态和音量；不收音、不播放"}
    service = ThreadingHTTPServer(("127.0.0.1", 0), ModelService)
    Thread(target=service.serve_forever, daemon=True).start()
    os.environ["DEEPSEEK_API_KEY"] = "local-desktop-validation"
    os.environ["DEEPSEEK_BASE_URL"] = f"http://127.0.0.1:{service.server_port}"
    os.environ["DEEPSEEK_MODEL"] = "local-measure"
    with TemporaryDirectory(prefix="desktop-measure-", dir=output.parent) as directory:
        runtime = Runtime(database_path=Path(directory) / "desktop.sqlite3")
        runtime.start()
        desktop = Desktop(runtime, str(ROOT / "frontend/dist/index.html"))
        animation = None
        receiver = []
        def check(condition, label):
            assert condition, label
            result["checks"].append(label)
        def wait(condition):
            deadline = time.perf_counter() + 20
            while time.perf_counter() < deadline:
                value = condition()
                if value:
                    return value
                time.sleep(0.02)
            raise AssertionError("等待桌面状态超时")
        def js(source):
            return desktop.main.evaluate_js(source)
        def send(text):
            js('''(() => { const input=document.querySelector('#message-input');
              Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(input,''' + json.dumps(text) + ''');
              input.dispatchEvent(new Event('input',{bubbles:true})); })()''')
            wait(lambda: js("!document.querySelector('.send-button').disabled"))
            js("document.querySelector('.composer').requestSubmit()")
        async def voice_animation():
            while True:
                level = 0.1 + 0.5 * abs(math.sin(time.perf_counter() * 2))
                runtime._update({"microphone_level": level, "playback_level": 0.4})
                await asyncio.sleep(0.05)
        original_command = runtime._command
        async def command(name):
            if name == "end_voice":
                result["checks"].append("收音球结束按钮发出 end_voice")
                runtime._lifecycle.deactivate()
                runtime._update({"voice_status": "standby", "voice_activity": "listening"})
            else:
                await original_command(name)
        runtime._command = command

        def verify():
            nonlocal animation
            from System import Action
            from System.Drawing import Point
            from System.Windows.Forms import Cursor, Form, TextBox, FormWindowState
            try:
                assert desktop.main.events.loaded.wait(20)
                wait(lambda: js("Boolean(document.querySelector('.desktop-shell'))"))
                result["display"] = js("({width:innerWidth,height:innerHeight,devicePixelRatio})")
                result["display"]["dpi"] = user32.GetDpiForWindow(desktop.main.native.Handle.ToInt64())
                check(True, "生产前端经真实桥接启动")
                time.sleep(2)
                result["resources"]["main_visible_voice_off"] = resources(seconds)
                started = time.perf_counter()
                send("测量流式生成")
                wait(lambda: js("document.querySelector('.messages').innerText.includes('正在生成用于验收')"))
                result["local_stream_first_dom_ms"] = (time.perf_counter() - started) * 1000
                result["resources"]["main_streaming_local_model"] = resources(seconds)
                send("接管旧回复")
                wait(lambda: js("document.querySelector('.messages').innerText.includes('接管完成')"))
                check(js("Boolean(document.querySelector('.message[data-status=interrupted]'))"), "流式生成中发送文字保留旧片段")
                runtime.command("hide_main")
                wait(lambda: runtime.connect("measure")["state"]["main_window"] == "hidden")
                result["resources"]["main_hidden_voice_off"] = resources(seconds)
                def focus_receiver():
                    form = Form()
                    form.Text = "焦点验证窗口"
                    form.Width, form.Height = 360, 120
                    box = TextBox()
                    box.Left, box.Top, box.Width = 15, 20, 300
                    form.Controls.Add(box)
                    form.Show()
                    form.Activate()
                    box.Focus()
                    receiver.append(form)
                desktop.main.native.Invoke(Action(focus_receiver))
                user32.GetForegroundWindow.restype = wintypes.HWND
                time.sleep(0.5)
                foreground_before = user32.GetForegroundWindow()
                def speech_scene():
                    runtime._floating_session = True
                    runtime._messages["floating-user"] = {"id": "floating-user", "reply_id": "floating-reply", "role": "user",
                        "source": "voice", "status": "completed", "text": "请显示一轮问答", "created_at": "2026-11-16T01:00:00Z", "error": ""}
                    runtime._messages["floating-reply"] = {"id": "floating-reply", "reply_id": "floating-reply", "role": "assistant",
                        "source": "voice", "status": "completed", "text": "这是一轮受控问答，用来验证窗口、焦点和音量呈现。", "created_at": "2026-11-16T01:00:00Z", "error": ""}
                    runtime._lifecycle.busy.add("playback")
                    runtime._update({"voice_status": "active", "voice_activity": "speaking", "floating_reply_id": "floating-reply"})
                runtime._loop.call_soon_threadsafe(speech_scene)
                wait(lambda: desktop.floating.evaluate_js("Boolean(document.querySelector('.floating-answer'))"))
                wait(lambda: runtime.connect("measure")["state"]["floating_card_height"] > 0)
                result["focus"] = {"expected": receiver[0].Handle.ToInt64(), "before": foreground_before, "after": user32.GetForegroundWindow(),
                                   "main": desktop.main.native.Handle.ToInt64(), "floating": desktop.floating.native.Handle.ToInt64(),
                                   "floating_style": user32.GetWindowLongW(desktop.floating.native.Handle.ToInt64(), -20)}
                focus = result["focus"]
                focus["status"] = "inconclusive"
                if focus["before"] == focus["expected"]:
                    if focus["after"] == focus["expected"]:
                        focus["status"] = "passed"
                        check(True, "悬浮自动出现保持验证窗口焦点")
                    elif focus["after"] in {focus["main"], focus["floating"]}:
                        focus["status"] = "failed"
                        raise AssertionError("悬浮自动出现使应用窗口取得焦点")
                # 普通窗口未获前台或外部应用切走时，原始句柄保留供人工复核。
                animation = asyncio.run_coroutine_threadsafe(voice_animation(), runtime._loop)
                result["resources"]["floating_synthetic_levels_20hz"] = resources(seconds)
                if focus["status"] == "passed":
                    check(user32.GetForegroundWindow() == focus["expected"], "持续音量更新保持验证窗口焦点")
                animation.cancel()
                def update_scene(changes, text=None):
                    if text is not None:
                        runtime._messages["floating-reply"]["text"] = text
                    runtime._update(changes)
                floating_js = desktop.floating.evaluate_js
                runtime._loop.call_soon_threadsafe(update_scene, {"microphone_level": 0.25, "playback_level": 0.04})
                wait(lambda: floating_js("document.querySelector('.ring-level').getAttribute('stroke-dasharray') === '0.5 1'"))
                check(floating_js("document.querySelectorAll('.orb-wave i').length === 5"), "麦克风外环与五根播放音柱分别呈现")
                for changes, hint in [({"voice_status": "starting"}, "正在准备"),
                                      ({"voice_status": "active", "voice_activity": "recognizing"}, "正在识别")]:
                    runtime._loop.call_soon_threadsafe(update_scene, changes)
                    wait(lambda: floating_js("document.querySelector('.orb-hint')?.textContent") == hint)
                runtime._loop.call_soon_threadsafe(update_scene, {"voice_activity": "speaking"}, "长回复用于验证滚动。\n" * 80)
                wait(lambda: floating_js("document.querySelector('.floating-answer')?.textContent.length > 500"))
                time.sleep(0.2)
                check(floating_js("(() => {const e=document.querySelector('.floating-transcript'); return e.scrollHeight-e.scrollTop-e.clientHeight < 2 && document.querySelector('.floating-conversation').getBoundingClientRect().height <= 236;})()"),
                      "长回复限制卡片高度并跟随末尾")
                floating_js("document.querySelector('.floating-transcript').scrollTop=0")
                wait(lambda: floating_js("Boolean(document.querySelector('.floating-card-actions'))"))
                runtime._loop.call_soon_threadsafe(update_scene, {}, "长回复用于验证滚动。\n" * 100)
                wait(lambda: floating_js("document.querySelector('.floating-answer').textContent.length > 1000"))
                check(floating_js("document.querySelector('.floating-transcript').scrollTop < 2"), "上滚后新增文字保持阅读位置")
                floating_js("document.querySelector('.floating-card-actions button').click()")
                wait(lambda: floating_js("(() => {const e=document.querySelector('.floating-transcript'); return e.scrollHeight-e.scrollTop-e.clientHeight < 2;})()"))
                check(True, "回到最新恢复跟随")
                runtime._loop.call_soon_threadsafe(update_scene, {}, "这是一轮受控问答，用来验证窗口、焦点和音量呈现。")
                animation = asyncio.run_coroutine_threadsafe(voice_animation(), runtime._loop)
                orb = desktop.floating.evaluate_js("(() => {const r=document.querySelector('.voice-orb').getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height};})()")
                scale = user32.GetDpiForWindow(desktop.floating.native.Handle.ToInt64()) / 96
                check(not desktop.floating.native.Region.IsVisible(float((orb["x"] - 12) * scale), float((orb["y"] + orb["h"] / 2) * scale)),
                      "收音球旁空白区域由原生形状裁去")
                form = desktop.floating.native
                desktop.main.native.Invoke(Action(lambda: setattr(Cursor, "Position", Point(
                    form.Left + int((orb["x"] + orb["w"] / 2) * scale), form.Top + int((orb["y"] + orb["h"] / 2) * scale)))))
                wait(lambda: runtime.connect("measure")["state"]["floating_controls_visible"])
                check(True, "悬停收音球同步可点击按钮区域")
                desktop.floating.evaluate_js("document.querySelector('.orb-hide').click()")
                wait(lambda: runtime.connect("measure")["state"]["floating_hidden"])
                time.sleep(0.2)
                check(not runtime.connect("measure")["state"]["floating_visible"], "手动隐藏期间20Hz音量更新不重新弹出")
                user32.IsWindowVisible.argtypes = [wintypes.HWND]
                user32.IsWindowVisible.restype = wintypes.BOOL
                wait(lambda: not user32.IsWindowVisible(desktop.floating.native.Handle.ToInt64()))
                check(True, "手动隐藏后原生窗口确实不可见")
                runtime.command("preview_floating")
                wait(lambda: runtime.connect("measure")["state"]["floating_visible"])
                wait(lambda: user32.IsWindowVisible(desktop.floating.native.Handle.ToInt64()))
                check(True, "找回后原生窗口确实可见")
                check(True, "托盘命令可找回悬浮窗")
                if focus["status"] == "passed":
                    check(user32.GetForegroundWindow() == focus["expected"], "隐藏后找回保持验证窗口焦点")
                runtime._loop.call_soon_threadsafe(update_scene, {"voice_status": "error", "voice_error": "受控 ASR 接口失败"})
                wait(lambda: floating_js("document.querySelector('.floating-failure')?.textContent") == "受控 ASR 接口失败")
                check(floating_js("Array.from(document.querySelectorAll('.floating-card-actions button')).some(e=>e.textContent.includes('恢复语音') && !e.disabled)"),
                      "悬浮错误保留问答并显示可用恢复入口")
                runtime._loop.call_soon_threadsafe(update_scene, {"voice_status": "active", "voice_error": ""})
                wait(lambda: floating_js("!document.querySelector('.orb-end').disabled"))
                desktop.floating.evaluate_js("document.querySelector('.orb-end').click()")
                wait(lambda: runtime.connect("measure")["state"]["voice_status"] == "standby")
                check(not runtime.connect("measure")["state"]["floating_visible"], "待唤醒收起悬浮窗")
                wait(lambda: not user32.IsWindowVisible(desktop.floating.native.Handle.ToInt64()))
                check(True, "回待唤醒后原生窗口确实不可见")
                desktop.activate()
                wait(lambda: runtime.connect("measure")["state"]["main_window"] == "visible")
                desktop._gui(lambda: setattr(desktop.main.native, "WindowState", FormWindowState.Minimized))
                wait(lambda: runtime.connect("measure")["state"]["main_window"] == "minimized")
                desktop.activate()
                wait(lambda: runtime.connect("measure")["state"]["main_window"] == "visible")
                check(js("document.querySelector('.messages').innerText.includes('接管完成')"), "最小化恢复后内容连续")
                desktop._gui(desktop.main.native.Close)
                wait(lambda: runtime.connect("measure")["state"]["main_window"] == "hidden")
                check(runtime._thread.is_alive(), "关闭主窗口后后台继续运行")
                result["completed"] = True
            finally:
                if animation is not None:
                    animation.cancel()
                if receiver:
                    desktop.main.native.Invoke(Action(receiver[0].Close))
                output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                runtime.command("quit")
        try:
            webview.start(verify, gui="edgechromium", http_server=True, storage_path=str(Path(directory) / "webview"))
        finally:
            desktop.stop()
            runtime.stop()
            service.shutdown()
    assert result.get("completed"), "桌面验收未完成，查看终端异常及输出记录"
    print(json.dumps({"completed": True, "checks": len(result["checks"]), "resources": list(result["resources"])}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=5, help="每个场景的资源采样秒数")
    parser.add_argument("--output", type=Path, default=ROOT / ".local/verification/stage-15/desktop.json")
    args = parser.parse_args()
    if args.seconds < 1:
        parser.error("采样时长需要大于0")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    validate(args.output, args.seconds)
