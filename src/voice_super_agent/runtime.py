import asyncio
from contextlib import suppress
from copy import deepcopy
import logging
import os
from pathlib import Path
from queue import Queue
import sqlite3
from threading import Thread
from typing import Any
from datetime import datetime, timezone
from uuid import uuid4
from websockets.exceptions import WebSocketException

from voice_super_agent.conversation import Conversation
from voice_super_agent.memory.store import ConversationStore
from voice_super_agent.voice.lifecycle import VoiceLifecycle, WakeSettings
from voice_super_agent.desktop.hotkey import HoldHotkey
from voice_super_agent.voice.wake import KeywordDetector
from voice_super_agent.audio.devices import read_devices


logger = logging.getLogger(__name__)
DESKTOP_COMMANDS = {"show_main", "hide_main", "quit", "preview_floating", "hide_floating", "toggle_preview_card"}


class Runtime:
    """后台独占应用状态，通过队列向桌面提交状态和窗口命令。"""

    def __init__(self, desktop_check: bool = False, database_path: str | Path = ":memory:",
                 wake_model_dir: Path | None = None):
        self._loop = asyncio.new_event_loop()
        self._thread = Thread(target=self._loop.run_forever, name="agent-runtime")
        self.events: Queue[dict[str, Any] | None] = Queue()
        self._views: set[str] = set()
        self._sequence = 0
        self._activity: asyncio.Task | None = None
        self._conversation: Conversation | None = None
        self._voice_task: asyncio.Task | None = None
        self._wake_model_dir = wake_model_dir
        self._wake_settings = WakeSettings()
        self._lifecycle = VoiceLifecycle()
        self._detector: KeywordDetector | None = None
        self._hotkey_task: asyncio.Task | None = None
        self._idle_task: asyncio.Task | None = None
        self._device_task: asyncio.Task | None = None
        self._execution_lock = asyncio.Lock()
        self._inputs: asyncio.Queue[tuple[str, str, int | None]] = asyncio.Queue()
        self._input_task: asyncio.Task | None = None
        self._api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        self._base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
        self._messages: dict[str, dict[str, Any]] = {}
        self._user_ids: dict[str, str] = {}
        self._model_records: list[dict] = []
        self._window = {"display_start_position": 0, "model_start_position": 0}
        self._store = ConversationStore(database_path)
        self._storage_ready = False
        self._revision = 0
        self._saved_revision = 0
        self._save_urgent = asyncio.Event()
        self._save_task: asyncio.Task | None = None
        self._exit_prepared = False
        self._state = {
            "status": "ready",
            "main_window": "visible",
            "floating_visible": False,
            "preview_expanded": True,
            "desktop_check": desktop_check,
            "activity_running": False,
            "activity_tick": 0,
            "model_available": bool(self._api_key),
            "model_error": "" if self._api_key else "缺少 DEEPSEEK_API_KEY，请在项目 .env 中填写后重启应用。",
            "model": os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
            "active_reply_id": None,
            "storage_status": "saved",
            "storage_error": "",
            "exiting": False,
            "voice_status": "off",
            "voice_activity": "listening",
            "voice_error": "",
            "wake_word": "你好小博",
            "wake_hotkey": "Ctrl+\\",
            "voice_idle_seconds": 60,
            "microphone_level": 0.0,
            "playback_level": 0.0,
            "microphone_device": "",
            "playback_device": "",
            "audio_device_pending": False,
            "playback_stop_ms": 0.0,
            "last_error": None,
            "voice_recovery": "idle",
        }

    def start(self) -> None:
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._initialize(), self._loop).result()
        logger.info("后台事件循环已启动")

    async def _initialize(self) -> None:
        try:
            saved = await self._store.open()
        except (sqlite3.Error, OSError) as exc:
            logger.exception("读取对话失败")
            self._state.update(storage_status="error", storage_error=f"读取对话失败：{exc}。本次未恢复记录，已暂停新消息。")
            return
        self._storage_ready = True
        self._messages = {message["id"]: message for message in saved["messages"]}
        self._user_ids = {message["reply_id"]: message["id"] for message in saved["messages"]
                          if message["role"] == "user" and message["status"] != "backchannel"}
        self._model_records = saved["model_records"]
        self._window = saved["window"]
        unfinished = [message for message in self._messages.values() if message["status"] in {"queued", "thinking", "streaming"}]
        for message in unfinished:
            message["status"] = "unfinished"
        if unfinished:
            self._request_save(immediate=True)
        logger.info("已恢复 %s 条展示消息、%s 条原生模型消息", len(self._messages), len(self._model_records))
        self._idle_task = asyncio.create_task(self._watch_idle(), name="voice-idle")
        if self._wake_model_dir is not None:
            self._update({"voice_status": "starting"})
            self._voice_task = asyncio.create_task(self._set_voice(False), name="prepare-wake-host")
            self._device_task = asyncio.create_task(self._watch_audio_devices(), name="audio-devices")

    def connect(self, view: str) -> dict[str, Any]:
        return asyncio.run_coroutine_threadsafe(self._connect(view), self._loop).result()

    async def _connect(self, view: str) -> dict[str, Any]:
        # 订阅和取快照在同一次调度完成，后续事件序号严格晚于该快照。
        self._views.add(view)
        logger.info("页面订阅状态：%s，序号：%s", view, self._sequence)
        return self._snapshot()

    def disconnect(self, view: str) -> None:
        self._loop.call_soon_threadsafe(self._views.discard, view)

    def update_windows(self, **changes: Any) -> None:
        self._loop.call_soon_threadsafe(self._update, changes)

    def _update(self, changes: dict[str, Any]) -> None:
        self._state.update(changes)
        self._sequence += 1
        self.events.put({"kind": "state", "views": tuple(self._views), "snapshot": self._snapshot()})

    def _snapshot(self) -> dict[str, Any]:
        return {"sequence": self._sequence, "state": dict(self._state),
                "messages": [dict(message) for message in self._messages.values()][self._window["display_start_position"]:]}

    def send_text(self, text: str) -> dict[str, str]:
        return asyncio.run_coroutine_threadsafe(self._send_text(text), self._loop).result()

    def resend_text(self, user_message_id: str) -> dict[str, str]:
        return asyncio.run_coroutine_threadsafe(self._resend_text(user_message_id), self._loop).result()

    async def _resend_text(self, user_message_id: str) -> dict[str, str]:
        message = self._messages[user_message_id]
        if (message["role"] != "user" or message["source"] != "text"
                or self._messages[message["reply_id"]]["status"] != "failed"):
            raise ValueError("只能重新发送回答失败的文字消息。")
        # 从后台原消息读取正文，沿普通发送路径追加新输入并执行接管。
        return await self._send_text(message["text"])

    async def _send_text(self, text: str) -> dict[str, str]:
        text = text.strip()
        if not text:
            raise ValueError("请输入消息内容。")
        if self._state["storage_error"]:
            raise ValueError(self._state["storage_error"])
        if self._state["exiting"]:
            raise ValueError("正在保存并退出，请等待。")
        if not self._state["model_available"]:
            raise ValueError(self._state["model_error"])
        if self._state["voice_status"] in {"starting", "stopping"}:
            raise ValueError("语音正在切换，请稍后发送文字。")
        epoch = None
        if self._state["voice_status"] == "active":
            self._lifecycle.new_input()
            epoch = self._conversation.begin_text_takeover()
        user_id, reply_id = uuid4().hex, uuid4().hex
        timestamp = datetime.now(timezone.utc).isoformat()
        self._messages[user_id] = {
            "id": user_id, "role": "user", "source": "text", "reply_id": reply_id,
            "text": text, "status": "completed", "created_at": timestamp, "error": "",
        }
        self._messages[reply_id] = {
            "id": reply_id, "role": "assistant", "source": "text", "reply_id": reply_id,
            "text": "", "status": "queued", "created_at": timestamp, "error": "",
        }
        self._user_ids[reply_id] = user_id
        self._inputs.put_nowait((reply_id, text, epoch))
        self._request_save(immediate=True)
        self._update({})
        if self._input_task is None:
            self._input_task = asyncio.create_task(self._run_inputs(), name="text-inputs")
        return {"user_message_id": user_id, "reply_id": reply_id}

    async def _run_inputs(self) -> None:
        """串行提交输入，模型生成继续由原生流水线异步执行。"""
        try:
            while True:
                reply_id, text, epoch = await self._inputs.get()
                if self._messages[reply_id]["status"] == "failed":
                    continue
                async with self._execution_lock:
                    try:
                        if self._conversation is not None and (self._conversation._runner_task.done()
                                or (self._conversation.voice_enabled and self._state["voice_status"] == "error")):
                            await self._conversation.close()
                            self._conversation = None
                        if self._conversation is None:
                            self._conversation = self._create_conversation(voice=False)
                            await self._conversation.start()
                        await self._conversation.send(reply_id, text, voice_epoch=epoch)
                    except (RuntimeError, TimeoutError, ValueError) as exc:
                        logger.exception("文字输入处理失败")
                        if isinstance(exc, ValueError):
                            self._update({"model_available": False, "model_error": str(exc) + "。请检查模型配置后重启应用。"})
                        self._conversation_event({"type": "unavailable", "error": str(exc), "reply_ids": (reply_id,),
                                                  "voice": self._conversation is not None and self._conversation.voice_enabled})
                        if self._conversation is not None:
                            await self._conversation.close()
                            self._conversation = None
                        # 清理期间已接收的新输入继续由本消费者处理。
        finally:
            self._input_task = None

    def _create_conversation(self, voice: bool) -> Conversation:
        def emit(event):
            # 关闭已等待旧执行对象清理；回调的归属仍以实际对象为准。
            if self._conversation is conversation:
                self._conversation_event(event)

        conversation = Conversation(
            self._api_key, self._state["model"], self._base_url, emit,
            messages=deepcopy([record["message"] for record in self._model_records[self._window["model_start_position"]:]]),
            voice=voice,
        )
        return conversation

    async def _close_conversation(self, *, interrupted: bool = False) -> None:
        if self._input_task is not None:
            self._input_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._input_task
            self._input_task = None
        if self._conversation is not None:
            if interrupted:
                await self._conversation.close(interrupted=True)
            else:
                await self._conversation.close()
            self._conversation = None
        while not self._inputs.empty():
            self._inputs.get_nowait()
        for message in self._messages.values():
            if message["status"] in {"queued", "thinking", "streaming"}:
                message["status"] = "interrupted" if interrupted and message["role"] == "assistant" else "unfinished"
        self._update({"active_reply_id": None})
        self._request_save(immediate=True)

    async def _set_voice(self, enabled: bool, *, recovering: bool = False) -> None:
        async with self._execution_lock:
            try:
                if recovering:
                    # 用户恢复先提交文字回复的已有片段，旧对象释放后再建立待唤醒宿主。
                    await self._close_conversation(interrupted=True)
                    self._lifecycle.deactivate()
                if self._wake_model_dir is not None:
                    if not self._state["storage_error"] and (
                            self._conversation is None or not self._conversation.voice_enabled
                            or self._conversation._runner_task.done()):
                        self._require_voice_configuration()
                        self._wake_settings = WakeSettings.from_environment()
                        hotkey = HoldHotkey(self._wake_settings.hotkey)
                        self._lifecycle.idle_seconds = self._wake_settings.idle_seconds
                        await self._close_conversation()
                        await self._prepare_voice_conversation()
                        if self._hotkey_task is None or self._hotkey_task.done():
                            self._hotkey_task = asyncio.create_task(self._watch_hotkey(hotkey), name="wake-hotkey")
                        self._update({"wake_word": self._wake_settings.word, "wake_hotkey": self._wake_settings.hotkey,
                                      "voice_idle_seconds": self._wake_settings.idle_seconds})
                    if enabled:
                        await self._use_default_audio()
                        await self._conversation.activate_voice()
                        if self._state["storage_error"]:
                            raise RuntimeError(self._state["storage_error"])
                        self._lifecycle.activate(self._loop.time())
                        self._update({"voice_status": "active", "voice_activity": "listening"})
                    else:
                        if self._state["storage_error"]:
                            await self._close_conversation()
                        elif self._conversation._voice.gate.mode != "standby":
                            await self._conversation.standby_voice()
                        if not self._state["storage_error"]:
                            await self._use_default_audio()
                        self._lifecycle.deactivate()
                        for message in self._messages.values():
                            if message["status"] in {"queued", "thinking", "streaming"}:
                                message["status"] = "unfinished"
                        self._request_save(immediate=True)
                        self._update({"voice_status": "error" if self._state["storage_error"] else "standby",
                                      "voice_error": self._state["storage_error"], "voice_activity": "listening", "active_reply_id": None})
                    if recovering:
                        self._update({"voice_recovery": "failed" if self._state["storage_error"] else "succeeded"})
                    return
                if not recovering:
                    await self._close_conversation()
                if enabled or recovering:
                    # 切换执行对象前完成已有提交的事务，保存失败时保留文字与窗口。
                    if not await self._flush():
                        self._update({"voice_status": "error", "voice_error": self._state["storage_error"],
                                      "voice_recovery": "failed" if recovering else self._state["voice_recovery"]})
                        return
                    self._conversation = self._create_conversation(voice=True)
                    await self._conversation.start()
                    if self._state["storage_error"]:
                        await self._close_conversation()
                        self._update({"voice_status": "error", "voice_error": self._state["storage_error"],
                                      "voice_recovery": "failed" if recovering else self._state["voice_recovery"]})
                        return
                    if enabled:
                        await self._conversation.activate_voice()
                        self._lifecycle.activate(self._loop.time())
                    else:
                        self._conversation._voice.gate.mode = "standby"
                else:
                    self._lifecycle.deactivate()
                self._update({"voice_status": "standby" if recovering else "active" if enabled else "off", "voice_activity": "listening",
                              "voice_error": "", "voice_recovery": "succeeded" if recovering else self._state["voice_recovery"]})
            except asyncio.CancelledError:
                if self._conversation is not None:
                    await self._conversation.close()
                    self._conversation = None
                raise
            except (RuntimeError, TimeoutError, ValueError, OSError, WebSocketException) as exc:
                logger.exception("切换语音失败")
                self._lifecycle.deactivate()
                if self._conversation is not None:
                    await self._conversation.close()
                    self._conversation = None
                self._update({"voice_status": "error", "voice_error": f"语音启动失败：{exc}",
                              "voice_recovery": "failed" if recovering else self._state["voice_recovery"]})

    async def _prepare_voice_conversation(self):
        if self._detector is None:
            self._detector = await asyncio.to_thread(KeywordDetector, self._wake_model_dir, self._wake_settings.word)
        if not await self._flush():
            raise RuntimeError(self._state["storage_error"])
        self._conversation = self._create_conversation(voice=True)
        self._conversation._voice.gate.detector = self._detector
        await self._conversation.start()
        if self._state["storage_error"]:
            raise RuntimeError(self._state["storage_error"])
        self._conversation._voice.gate.mode = "standby"

    def _require_voice_configuration(self):
        missing = [name for name in ("DEEPSEEK_API_KEY", "VOLCENGINE_API_KEY", "ARK_API_KEY")
                   if not os.getenv(name, "").strip()]
        if missing:
            raise ValueError("缺少语音配置：" + "、".join(missing))

    async def _watch_hotkey(self, hotkey):
        while True:
            if hotkey.sample(hotkey.pressed(), self._loop.time()) and self._state["voice_status"] == "standby":
                await self._command("wake_voice")
            await asyncio.sleep(0.02)

    async def _watch_idle(self):
        while True:
            if self._lifecycle.should_end(self._loop.time()) and self._state["voice_status"] == "active":
                await self._command("end_voice")
            await asyncio.sleep(0.05)

    async def _use_default_audio(self):
        transport = self._conversation._voice.transport
        devices = await asyncio.to_thread(read_devices)
        if not devices.contains(transport.devices):
            raise OSError("当前录音或播放设备已失效，请恢复语音以打开 Windows 当前默认设备。")
        if devices.defaults != transport.devices.defaults:
            await self._conversation.reopen_audio()
        self._update({"audio_device_pending": False})

    async def _watch_audio_devices(self):
        while True:
            await asyncio.sleep(0.5)
            try:
                async with self._execution_lock:
                    if self._state["voice_status"] not in {"standby", "active"}:
                        continue
                    transport = self._conversation._voice.transport
                    devices = await asyncio.to_thread(read_devices)
                    active = await asyncio.to_thread(transport.is_active)
                    if not devices.contains(transport.devices) or not active:
                        raise OSError("当前录音或播放设备已失效，请恢复语音以打开 Windows 当前默认设备。")
                    changed = devices.defaults != transport.devices.defaults
                    if self._state["voice_status"] == "standby" and changed:
                        await self._conversation.reopen_audio()
                        self._update({"audio_device_pending": False})
                    elif changed != self._state["audio_device_pending"]:
                        self._update({"audio_device_pending": changed})
            except (OSError, RuntimeError, ValueError) as exc:
                logger.exception("音频设备不可用")
                if self._voice_task is not None and not self._voice_task.done():
                    self._voice_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await self._voice_task
                async with self._execution_lock:
                    self._conversation_event({"type": "error", "reply_id": self._state["active_reply_id"],
                                              "error": str(exc), "service": "audio", "category": "connectivity",
                                              "permanent": True, "voice": True})
                    await self._close_conversation()
                    self._lifecycle.deactivate()
                    self._update({"voice_status": "error", "voice_error": str(exc), "active_reply_id": None,
                                  "microphone_level": 0.0, "playback_level": 0.0, "audio_device_pending": False})

    def _conversation_event(self, event: dict[str, Any]) -> None:
        if event["type"] == "voice_input_reset":
            for activity in ("input", "capture", "recognition", "user_turn", "input_tail"):
                self._lifecycle.activity(activity, False, self._loop.time())
            return
        if event["type"] == "voice_superseded":
            self._messages[event["user_id"]].update(text=event["text"], status="unfinished")
            self._messages[event["reply_id"]]["status"] = "unfinished"
            self._lifecycle.activity(f"reply:{event['reply_id']}", False, self._loop.time())
            self._request_save(immediate=True)
            self._update({})
            return
        if event["type"] == "audio_levels":
            self._update({"microphone_level": event["microphone"], "playback_level": event["playback"]})
            return
        if event["type"] == "audio_devices":
            self._update({"microphone_device": event["microphone"], "playback_device": event["speaker"]})
            return
        if event["type"] == "audio_stop":
            self._update({"playback_stop_ms": event["elapsed_ms"]})
            return
        if event["type"] == "wake":
            asyncio.create_task(self._command("wake_voice"), name="keyword-wake")
            return
        if event["type"] == "voice_busy":
            self._lifecycle.activity(event["activity"], event["active"], self._loop.time())
            return
        if event["type"] == "end_requested":
            self._lifecycle.ending_reply = event["reply_id"]
            return
        if event["type"] == "voice_activity":
            self._update({"voice_activity": event["activity"]})
            return
        if event["type"] == "voice_error":
            self._update({"voice_error": event["error"]})
            return
        if event["type"] == "error":
            reply_id = event["reply_id"]
            message = self._messages[reply_id] if reply_id is not None else None
            failed_reply = message is not None and message["status"] in {"queued", "thinking", "streaming"}
            if failed_reply:
                message.update(status="failed", error=event["error"])
                self._lifecycle.activity(f"reply:{reply_id}", False, self._loop.time())
            changes = {"last_error": {"service": event["service"], "category": event["category"],
                                      "permanent": event["permanent"], "message": event["error"],
                                      "reply_id": reply_id if failed_reply else None,
                                      "source": message["source"] if failed_reply else None}}
            if event["service"] == "model" and event["permanent"]:
                changes.update(model_available=False, model_error=event["error"] + "。请检查模型配置后重启应用。")
            elif event["voice"] and event["service"] != "model":
                changes["voice_error"] = event["error"]
            if self._state["active_reply_id"] == reply_id:
                changes["active_reply_id"] = None
            self._request_save(immediate=True)
            self._update(changes)
            return
        if event["type"] in {"voice_input", "voice_committed", "voice_unfinished", "backchannel"}:
            reply_id = event["reply_id"]
            backchannel = event["type"] == "backchannel"
            status = {"voice_input": "streaming", "voice_committed": "completed",
                      "voice_unfinished": "unfinished", "backchannel": "backchannel"}[event["type"]]
            self._messages[event["user_id"]] = {
                "id": event["user_id"], "role": "user", "source": "voice", "reply_id": reply_id,
                "text": event["text"], "status": status,
                "created_at": event["created_at"], "error": "",
            }
            if not backchannel:
                if event["type"] == "voice_input":
                    self._lifecycle.new_input()
                    self._lifecycle.activity("input", True, self._loop.time())
                self._user_ids[reply_id] = event["user_id"]
                if reply_id not in self._messages:
                    self._messages[reply_id] = {"id": reply_id, "role": "assistant", "source": "voice", "reply_id": reply_id,
                                                "text": "", "status": "queued", "created_at": event["created_at"], "error": ""}
                if event["type"] in {"voice_committed", "voice_unfinished"}:
                    self._lifecycle.activity("input", False, self._loop.time())
                    self._lifecycle.activity(f"reply:{reply_id}", True, self._loop.time())
                    self._messages[reply_id]["status"] = "thinking"
                    self._capture_context(event["messages"], reply_id)
                    self._update({"active_reply_id": reply_id, "voice_activity": "thinking"})
            self._request_save(immediate=event["type"] != "voice_input")
            self._update({})
            return
        if event["type"] == "unavailable":
            for message in self._messages.values():
                if (message["reply_id"] in event["reply_ids"] or not self._state["model_available"]) and message["status"] in {"queued", "thinking", "streaming"}:
                    message.update(status="failed", error=event["error"])
            self._request_save(immediate=True)
            if event.get("voice", False):
                self._lifecycle.deactivate()
                self._update({"voice_status": "error", "voice_error": self._state["voice_error"] or event["error"], "active_reply_id": None})
            else:
                self._update({"active_reply_id": None})
            return
        if event["type"] == "context":
            self._capture_context(event["messages"], event["reply_id"])
            self._request_save(immediate=True)
            self._update({})
            return
        message = self._messages[event["reply_id"]]
        changes = {}
        if event["type"] == "started":
            self._lifecycle.new_input()
            self._lifecycle.activity(f"reply:{event['reply_id']}", True, self._loop.time())
            message["status"] = "thinking"
            changes["active_reply_id"] = message["id"]
        elif event["type"] == "text":
            if message["status"] in {"failed", "interrupted", "unfinished", "completed"}:
                return
            message["text"] += event["text"]
            message["status"] = "streaming"
        elif event["type"] == "finished":
            self._lifecycle.activity(f"reply:{event['reply_id']}", False, self._loop.time())
            self._lifecycle.finished_reply = event["reply_id"]
            self._capture_context(event["messages"], event["reply_id"])
            if message["status"] != "failed":
                message["status"] = event["status"]
            if self._state["active_reply_id"] == message["id"]:
                changes["active_reply_id"] = None
        self._request_save(immediate=event["type"] != "text")
        self._update(changes)

    def _capture_context(self, messages: list[dict], reply_id: str) -> None:
        """记录原生提交顺序，新增消息的关联取自本轮输入和回复标识。"""
        start = self._window["model_start_position"]
        records = self._model_records[:start]
        for position, message in enumerate(messages, start=start):
            if position < len(self._model_records):
                display_id = self._model_records[position]["display_message_id"]
            else:
                display_id = self._user_ids[reply_id] if message["role"] == "user" else reply_id
            records.append({"display_message_id": display_id, "message": message})
        self._model_records = records

    def _request_save(self, immediate: bool = False) -> None:
        self._revision += 1
        if self._state["storage_error"]:
            return
        self._state["storage_status"] = "saving"
        if immediate:
            self._save_urgent.set()
        if self._save_task is None or self._save_task.done():
            self._save_task = asyncio.create_task(self._save_pending(), name="save-conversation")

    async def _save_pending(self) -> bool:
        while self._saved_revision < self._revision:
            # 流式片段按短批次合并；用户提交、原生提交和退出可提前唤醒。
            try:
                await asyncio.wait_for(self._save_urgent.wait(), timeout=0.25)
            except TimeoutError:
                pass
            self._save_urgent.clear()
            revision = self._revision
            messages = [dict(message) for message in self._messages.values()]
            records = deepcopy(self._model_records)
            try:
                await self._store.save(messages, records, dict(self._window))
            except (sqlite3.Error, OSError) as exc:
                logger.exception("保存对话失败")
                self._update({"storage_status": "error", "storage_error": f"保存对话失败：{exc}。已暂停新消息；请处理原因后再次点击退出应用以保存。"})
                if self._state["voice_status"] == "active":
                    # 保存失败后停止语音输入，保留内存中的原生提交供退出时重试。
                    self._update({"voice_status": "stopping"})
                    self._voice_task = asyncio.create_task(self._set_voice(False), name="stop-voice-on-storage-error")
                return False
            self._saved_revision = revision
            self._update({"storage_status": "saved" if revision == self._revision else "saving", "storage_error": ""})
        return True

    async def _flush(self) -> bool:
        self._save_urgent.set()
        if self._save_task is not None and not self._save_task.done():
            return await self._save_task
        if self._saved_revision < self._revision:
            self._save_task = asyncio.create_task(self._save_pending(), name="save-conversation")
            return await self._save_task
        return True

    def prepare_exit(self) -> bool:
        return asyncio.run_coroutine_threadsafe(self._prepare_exit(), self._loop).result()

    async def _prepare_exit(self) -> bool:
        """先完成原生取消与最终事务，成功后桌面才关闭窗口。"""
        if self._exit_prepared:
            return True
        self._update({"exiting": True})
        self._lifecycle.deactivate()
        for task in (self._hotkey_task, self._idle_task, self._device_task):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        await self._stop_activity()
        if self._voice_task is not None:
            self._voice_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._voice_task
        await self._close_conversation()
        self._update({"active_reply_id": None})
        if self._storage_ready:
            self._request_save(immediate=True)
            if not await self._flush():
                self._update({"exiting": False})
                return False
        self._exit_prepared = True
        return True

    def command(self, command: str) -> None:
        asyncio.run_coroutine_threadsafe(self._command(command), self._loop).result()

    async def _command(self, command: str) -> None:
        if command in DESKTOP_COMMANDS:
            self.events.put({"kind": "command", "command": command})
        elif command == "start_activity":
            if self._activity is None:
                self._update({"activity_running": True})
                self._activity = asyncio.create_task(self._run_activity())
        elif command == "stop_activity":
            await self._stop_activity()
        elif command in {"start_voice", "stop_voice", "wake_voice", "end_voice", "recover_voice"}:
            enabled = command in {"start_voice", "wake_voice"}
            recovering = command == "recover_voice"
            if ((enabled or recovering) and self._state["storage_error"]) or self._state["exiting"]:
                raise ValueError(self._state["storage_error"] or "正在保存并退出，请等待。")
            if (enabled or recovering) and self._state["voice_status"] in {"starting", "active", "stopping"}:
                return
            if enabled and self._state["voice_status"] == "error":
                return
            if not enabled and self._voice_task is not None and not self._voice_task.done():
                self._voice_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._voice_task
            if enabled or recovering:
                if not self._state["model_available"]:
                    self._update({"voice_status": "error", "voice_error": self._state["model_error"],
                                  "voice_recovery": "failed" if recovering else self._state["voice_recovery"]})
                    return
                try:
                    self._require_voice_configuration()
                except ValueError as exc:
                    self._update({"voice_status": "error", "voice_error": str(exc),
                                  "voice_recovery": "failed" if recovering else self._state["voice_recovery"]})
                    return
            self._update({"voice_status": "starting" if enabled or recovering else "stopping", "voice_error": "",
                          "voice_recovery": "running" if recovering else self._state["voice_recovery"]})
            self._voice_task = asyncio.create_task(self._set_voice(enabled, recovering=recovering), name="switch-voice")
        else:
            raise ValueError(f"未知桌面命令：{command}")

    async def _run_activity(self) -> None:
        """联调活动以真实异步任务持续产生状态，验证 UI 推送和控制并行。"""
        while True:
            await asyncio.sleep(0.05)
            self._update({"activity_tick": self._state["activity_tick"] + 1})

    async def _stop_activity(self) -> None:
        if self._activity is not None:
            self._activity.cancel()
            with suppress(asyncio.CancelledError):
                await self._activity
            self._activity = None
        self._update({"activity_running": False})

    def stop(self) -> None:
        try:
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result()
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join()
            self._loop.close()
            logger.info("后台事件循环已停止")

    async def _shutdown(self) -> None:
        try:
            if not await self._prepare_exit():
                raise RuntimeError(self._state["storage_error"])
        finally:
            await self._store.close()
            self._views.clear()
            await self._loop.shutdown_asyncgens()
            await self._loop.shutdown_default_executor()
