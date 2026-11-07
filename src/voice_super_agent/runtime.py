import asyncio
from contextlib import suppress
import logging
from queue import Queue
from threading import Thread
from typing import Any


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
        self._state = {
            "status": "ready",
            "main_window": "visible",
            "floating_visible": False,
            "preview_expanded": True,
            "desktop_check": desktop_check,
            "activity_running": False,
            "activity_tick": 0,
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
        return {"sequence": self._sequence, "state": dict(self._state)}

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
        self._views.clear()
