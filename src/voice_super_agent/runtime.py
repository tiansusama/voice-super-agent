import asyncio
from contextlib import suppress
import logging
import os
from queue import Queue
from threading import Thread
from typing import Any
from datetime import datetime, timezone
from uuid import uuid4

from voice_super_agent.conversation import TextConversation


logger = logging.getLogger(__name__)
DESKTOP_COMMANDS = {"show_main", "hide_main", "quit", "preview_floating", "hide_floating", "toggle_preview_card"}


class Runtime:
    """后台独占应用状态，通过队列向桌面提交状态和窗口命令。"""

    def __init__(self, desktop_check: bool = False):
        self._loop = asyncio.new_event_loop()
        self._thread = Thread(target=self._loop.run_forever, name="agent-runtime")
        self.events: Queue[dict[str, Any] | None] = Queue()
        self._views: set[str] = set()
        self._sequence = 0
        self._activity: asyncio.Task | None = None
        self._conversation: TextConversation | None = None
        self._inputs: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._input_task: asyncio.Task | None = None
        self._api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        self._base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
        self._messages: dict[str, dict[str, Any]] = {}
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
        }

    def start(self) -> None:
        self._thread.start()
        logger.info("后台事件循环已启动")

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
                "messages": [dict(message) for message in self._messages.values()]}

    def send_text(self, text: str) -> dict[str, str]:
        return asyncio.run_coroutine_threadsafe(self._send_text(text), self._loop).result()

    async def _send_text(self, text: str) -> dict[str, str]:
        text = text.strip()
        if not text:
            raise ValueError("请输入消息内容。")
        if not self._state["model_available"]:
            raise ValueError(self._state["model_error"])
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
        self._inputs.put_nowait((reply_id, text))
        self._update({})
        if self._input_task is None:
            self._input_task = asyncio.create_task(self._run_inputs(), name="text-inputs")
        return {"user_message_id": user_id, "reply_id": reply_id}

    async def _run_inputs(self) -> None:
        """串行提交输入，模型生成继续由原生流水线异步执行。"""
        try:
            self._conversation = TextConversation(
                self._api_key, self._state["model"],
                self._base_url,
                self._conversation_event,
            )
            await self._conversation.start()
            while True:
                reply_id, text = await self._inputs.get()
                await self._conversation.send(reply_id, text)
        except (RuntimeError, TimeoutError, ValueError) as exc:
            logger.exception("文字输入处理失败")
            self._conversation_event({"type": "unavailable", "error": str(exc)})
            if self._conversation is not None:
                await self._conversation.close()

    def _conversation_event(self, event: dict[str, Any]) -> None:
        if event["type"] == "unavailable":
            for message in self._messages.values():
                if message["status"] in {"queued", "thinking", "streaming"}:
                    message.update(status="failed", error=event["error"])
            self._update({"model_available": False, "model_error": event["error"], "active_reply_id": None})
            return
        message = self._messages[event["reply_id"]]
        changes = {}
        if event["type"] == "started":
            message["status"] = "thinking"
            changes["active_reply_id"] = message["id"]
        elif event["type"] == "text":
            message["text"] += event["text"]
            message["status"] = "streaming"
        elif event["type"] == "error":
            message.update(status="failed", error=event["error"])
        elif event["type"] == "finished":
            if message["status"] != "failed":
                message["status"] = event["status"]
            if self._state["active_reply_id"] == message["id"]:
                changes["active_reply_id"] = None
        self._update(changes)

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
        asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
        self._loop.close()
        logger.info("后台事件循环已停止")

    async def _shutdown(self) -> None:
        await self._stop_activity()
        if self._input_task is not None:
            self._input_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._input_task
        if self._conversation is not None:
            await self._conversation.close()
        self._views.clear()
