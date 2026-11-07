import asyncio
from collections.abc import Callable

from httpx2 import InvalidURL
from openai import AsyncOpenAI
from pipecat.frames.frames import (
    CancelFrame, InterruptionFrame, LLMFullResponseEndFrame,
    LLMFullResponseStartFrame, LLMTextFrame, LLMMessagesAppendFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.deepseek.llm import DeepSeekLLMService
from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies
from pipecat.workers.runner import WorkerRunner


class TextDeepSeekService(DeepSeekLLMService):
    """文字请求失败直接反馈，由用户决定后续输入。"""

    def create_client(self, api_key=None, base_url=None, **kwargs):
        try:
            return AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0, timeout=60)
        except InvalidURL as exc:
            raise ValueError(f"DEEPSEEK_BASE_URL 配置无效：{exc}") from exc

    async def cleanup(self):
        await super().cleanup()
        await self._client.close()


class TextConversation:
    """原生聚合器独占模型上下文，展示事件绑定当前回复。"""

    def __init__(self, api_key: str, model: str, base_url: str, emit: Callable[[dict], None]):
        self.context = LLMContext()
        self._emit = emit
        self._reply_id: str | None = None
        self._interrupted = False
        self._failed = False
        self._reply_open = False
        self._closing = False
        self._ready = asyncio.Event()
        self._started = asyncio.Event()
        self._runner_task: asyncio.Task | None = None
        self._user, assistant = LLMContextAggregatorPair(
            self.context,
            user_params=LLMUserAggregatorParams(
                user_turn_strategies=ExternalUserTurnStrategies(),
                empty_user_turn=None,
            ),
        )
        self._llm = TextDeepSeekService(
            api_key=api_key,
            base_url=base_url,
            settings=DeepSeekLLMService.Settings(
                model=model,
                system_instruction="你是小博，一个友好的中文助手。请结合对话上下文，用清晰自然的中文回答。",
            ),
        )
        self._worker = PipelineWorker(
            Pipeline([self._user, self._llm, assistant]),
            enable_rtvi=False,
            enable_turn_tracking=False,
            idle_timeout_secs=None,
            processor_unusable_policy=ProcessorUnusablePolicy.CANCEL,
        )
        self._runner = WorkerRunner(handle_sigint=False)

        @self._worker.event_handler("on_pipeline_started")
        async def on_ready(worker, frame):
            self._ready.set()

        @assistant.event_handler("on_before_process_frame")
        async def on_frame(aggregator, frame):
            if isinstance(frame, LLMTextFrame):
                self._emit({"type": "text", "reply_id": self._reply_id, "text": frame.text})

        @assistant.event_handler("on_after_process_frame")
        async def on_processed(aggregator, frame):
            if isinstance(frame, LLMFullResponseStartFrame):
                self._reply_open = True
                self._started.set()
            elif isinstance(frame, (LLMFullResponseEndFrame, InterruptionFrame, CancelFrame)) and self._reply_open:
                # 同步帧事件发生于原生上下文提交之后，关联仍属于刚完成的回复。
                self._reply_open = False
                status = "failed" if self._failed else (
                    "interrupted" if self._interrupted or isinstance(frame, (InterruptionFrame, CancelFrame)) else "completed"
                )
                self._emit({"type": "finished", "reply_id": self._reply_id, "status": status})

        @self._llm.event_handler("on_error")
        async def on_model_error(service, error):
            self._failed = True
            self._emit({"type": "error", "reply_id": self._reply_id,
                        "error": f"模型请求失败：{error.error}"})

        @self._worker.event_handler("on_pipeline_error")
        async def on_pipeline_error(worker, error):
            if error.processor is not self._llm:
                self._emit({"type": "error", "reply_id": self._reply_id,
                            "error": f"对话处理失败：{error.error}"})

    async def start(self) -> None:
        await self._runner.add_workers(self._worker)
        self._runner_task = asyncio.create_task(self._run(), name="text-conversation")
        await self._wait_until(self._ready)

    async def _run(self) -> None:
        try:
            await self._runner.run()
        finally:
            if not self._closing:
                self._emit({"type": "unavailable", "error": "文字对话已停止，请检查模型配置后重启应用。"})

    async def _wait_until(self, event: asyncio.Event) -> None:
        """等待本地流水线信号，同时传播执行对象的结束与异常。"""
        waiter = asyncio.create_task(event.wait())
        try:
            done, _ = await asyncio.wait((waiter, self._runner_task), return_when=asyncio.FIRST_COMPLETED)
            if self._runner_task in done:
                await self._runner_task
                raise RuntimeError("文字对话已停止，请检查模型配置后重启应用。")
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    async def send(self, reply_id: str, text: str) -> None:
        """按 STS 的打断、提交、追加顺序处理；仅等待本地轮次启动。"""
        self._interrupted = True
        await self._user.broadcast_interruption()
        if not await self._worker.flush_pipeline():
            raise RuntimeError("旧回复提交超时，请重启应用后继续。")
        self._reply_id = reply_id
        self._interrupted = False
        self._failed = False
        self._started.clear()
        self._emit({"type": "started", "reply_id": reply_id})
        await self._worker.queue_frame(LLMMessagesAppendFrame(
            messages=[{"role": "user", "content": text}], run_llm=True,
        ))
        # 等到原生开始事件后再接下一条输入，保证连续文字均已进入上下文。
        await self._wait_until(self._started)

    async def close(self) -> None:
        self._closing = True
        self._interrupted = True
        if self._runner_task is not None:
            await self._runner.cancel()
            await self._runner_task
