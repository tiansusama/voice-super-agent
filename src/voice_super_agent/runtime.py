import asyncio
import logging
from threading import Thread, current_thread


logger = logging.getLogger(__name__)


class Runtime:
    """在独立线程运行后台事件循环，供桌面启动及后续 Agent 使用。"""

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = Thread(target=self._loop.run_forever, name="agent-runtime")

    def start(self) -> None:
        self._thread.start()
        # 等待后台实际处理一次请求，再向桌面报告启动完成。
        self.get_startup_status()
        logger.info("后台事件循环已启动")

    def get_startup_status(self) -> dict[str, str]:
        return asyncio.run_coroutine_threadsafe(self._startup_status(), self._loop).result()

    async def _startup_status(self) -> dict[str, str]:
        logger.info("后台启动状态已确认，线程：%s", current_thread().name)
        return {"status": "ready"}

    def stop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
        self._loop.close()
        logger.info("后台事件循环已停止")
