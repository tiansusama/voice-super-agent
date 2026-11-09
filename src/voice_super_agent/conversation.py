import asyncio
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from httpx2 import InvalidURL
from openai import AsyncOpenAI
from pipecat.frames.frames import (
    CancelFrame, InterruptionFrame, LLMFullResponseEndFrame,
    LLMFullResponseStartFrame, LLMTextFrame, LLMMessagesAppendFrame,
    BotStartedSpeakingFrame, BotStoppedSpeakingFrame, InterimTranscriptionFrame,
    LLMContextFrame, LLMContextUserTurnFrame, LLMConfigureOutputFrame, TranscriptionFrame, UserBackchannelFrame,
    VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame, UserStartedSpeakingFrame, UserStoppedSpeakingFrame,
    FunctionCallsStartedFrame, FunctionCallResultFrame, FunctionCallCancelFrame,
)
from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.deepseek.llm import DeepSeekLLMService
from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies
from pipecat.turns.user_stop import UserTurnStoppedParams
from pipecat.workers.runner import WorkerRunner


class TextDeepSeekService(DeepSeekLLMService):
    """模型请求失败直接反馈，由用户决定后续输入。"""

    def create_client(self, api_key=None, base_url=None, **kwargs):
        try:
            return AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0, timeout=60)
        except InvalidURL as exc:
            raise ValueError(f"DEEPSEEK_BASE_URL 配置无效：{exc}") from exc

    async def cleanup(self):
        await super().cleanup()
        await self._client.close()


class Conversation:
    """原生聚合器独占模型上下文，展示事件绑定当前回复。"""

    def __init__(self, api_key: str, model: str, base_url: str, emit: Callable[[dict], None],
                 messages: list[dict] | None = None, voice: bool = False):
        self.context = LLMContext(messages=messages)
        self._emit = emit
        self._reply_id: str | None = None
        self._generation_reply_id: str | None = None
        self._assistant_reply_id: str | None = None
        self._interrupted = False
        self._failed = False
        self._failure_reason = ""
        self._reply_open = False
        self._closing = False
        self._ready = asyncio.Event()
        self._started = asyncio.Event()
        self._runner_task: asyncio.Task | None = None
        self.voice_enabled = voice
        self._unavailable_hint = "语音对话已停止，请检查服务或设备后重新启动语音。" if voice else "文字对话已停止，请检查模型配置后重启应用。"
        self._voice = None
        self._voice_pending: dict | None = None
        self._voice_finals = ""
        self._voice_interim = ""
        self._response_has_tools = False
        self._voice_tools = ToolsSchema([FunctionSchema(
            name="end_voice", description="用户明确要求结束语音对话时调用。引用、讨论结束字样时继续交流。调用后可简短收尾，也可不回复正文。",
            properties={}, required=[], handler=self._end_voice,
        )]) if voice else None
        self._llm = TextDeepSeekService(
            api_key=api_key,
            base_url=base_url,
            settings=DeepSeekLLMService.Settings(
                model=model,
                system_instruction=("你是小博，一个友好的中文助手。请结合对话上下文，用清晰自然的中文回答。"
                                      + ("回答会直接朗读，请使用适合口语的表达，每次一到三句话。用户明确表示结束本次语音时调用 end_voice，可自行决定简短收尾。" if voice else "")),
            ),
        )
        if voice:
            from voice_super_agent.voice.pipeline import VoicePipeline

            self._voice = VoicePipeline(self.context, emit)
        self._user, assistant = LLMContextAggregatorPair(
            self.context,
            user_params=self._voice.user_params if voice else LLMUserAggregatorParams(
                user_turn_strategies=ExternalUserTurnStrategies(),
                empty_user_turn=None,
            ),
        )
        processors = self._voice.processors(self._user, self._llm, assistant) if voice else [self._user, self._llm, assistant]
        self._worker = PipelineWorker(
            Pipeline(processors),
            params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000),
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
            if isinstance(frame, LLMTextFrame) and not self.voice_enabled:
                self._emit({"type": "text", "reply_id": self._reply_id, "text": frame.text})

        @self._llm.event_handler("on_before_push_frame")
        async def on_generated(service, frame):
            if isinstance(frame, (LLMFullResponseStartFrame, LLMFullResponseEndFrame, LLMTextFrame)):
                frame.metadata["reply_id"] = self._generation_reply_id
            # 语音展示采用模型原文，原生助手聚合器仍在播放路径末端提交实际文本。
            if self.voice_enabled and isinstance(frame, LLMTextFrame):
                self._emit({"type": "text", "reply_id": self._generation_reply_id, "text": frame.text})
            if self.voice_enabled:
                if isinstance(frame, LLMFullResponseStartFrame):
                    self._response_has_tools = False
                    self._busy("model", True)
                elif isinstance(frame, FunctionCallsStartedFrame):
                    self._response_has_tools = True
                    self._busy("tool", True)
                elif isinstance(frame, LLMFullResponseEndFrame):
                    frame.metadata["has_function_calls"] = self._response_has_tools
                    self._busy("model", False)

        @self._llm.event_handler("on_before_process_frame")
        async def on_model_input(service, frame):
            if isinstance(frame, LLMContextFrame):
                self._generation_reply_id = frame.metadata["reply_id"]

        @self._user.event_handler("on_before_process_frame")
        async def on_user_frame(aggregator, frame):
            if not self.voice_enabled:
                return
            self._observe_voice_frame(frame)
            if isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
                if not frame.text.strip() and self._voice_pending is None:
                    return
                if self._voice_pending is None:
                    # 放行的新语音将开启原生接管，取消时产生的结束帧也属于旧回复。
                    self._interrupted = True
                    self._voice_pending = {"user_id": uuid4().hex, "reply_id": uuid4().hex,
                                           "created_at": datetime.now(timezone.utc).isoformat()}
                if isinstance(frame, TranscriptionFrame):
                    self._voice_finals += frame.text
                    self._voice_interim = ""
                else:
                    self._voice_interim = frame.text
                self._emit({"type": "voice_input", **self._voice_pending,
                            "text": self._voice_finals + self._voice_interim})
            elif isinstance(frame, UserBackchannelFrame):
                self._emit({"type": "backchannel", "user_id": uuid4().hex,
                            "reply_id": self._reply_id, "text": frame.text,
                            "created_at": datetime.now(timezone.utc).isoformat()})
            elif isinstance(frame, (BotStartedSpeakingFrame, BotStoppedSpeakingFrame)):
                self._emit({"type": "voice_activity", "activity": "speaking" if isinstance(frame, BotStartedSpeakingFrame) else "listening"})

        @self._user.event_handler("on_before_push_frame")
        async def on_user_commit(aggregator, frame):
            if self.voice_enabled:
                self._observe_voice_frame(frame)
            if self.voice_enabled and self._voice_pending is not None:
                if isinstance(frame, LLMContextUserTurnFrame) and not frame.speculation:
                    event_type = "voice_committed"
                    text = frame.text
                elif isinstance(frame, LLMContextFrame):
                    # 空轮次恢复只发模型上下文；保留已识别片段及未完成状态。
                    event_type = "voice_unfinished"
                    text = self._voice_finals + self._voice_interim
                else:
                    return
                pending = self._voice_pending
                self._reply_id = pending["reply_id"]
                self._interrupted = False
                self._failed = False
                self._failure_reason = ""
                self._emit({"type": event_type, **pending, "text": text,
                            "messages": deepcopy(self.context.get_messages())})
                self._voice_pending = None
                self._voice_finals = self._voice_interim = ""
            if isinstance(frame, LLMContextFrame):
                # 原生取消和新输入可并行传播；回复关联随帧穿过生成与播放队列。
                frame.metadata["reply_id"] = self._reply_id

        @assistant.event_handler("on_after_process_frame")
        async def on_processed(aggregator, frame):
            if isinstance(frame, LLMFullResponseStartFrame):
                self._assistant_reply_id = frame.metadata["reply_id"]
                self._reply_open = True
                self._emit({"type": "context", "reply_id": self._assistant_reply_id,
                            "messages": deepcopy(self.context.get_messages())})
                self._started.set()
            elif isinstance(frame, (LLMFullResponseEndFrame, InterruptionFrame, CancelFrame)) and self._reply_open:
                if isinstance(frame, LLMFullResponseEndFrame) and frame.metadata.get("has_function_calls", False):
                    self._emit({"type": "context", "reply_id": self._assistant_reply_id,
                                "messages": deepcopy(self.context.get_messages())})
                    return
                # 同步帧事件发生于原生上下文提交之后，关联仍属于刚完成的回复。
                self._reply_open = False
                status = "failed" if self._failed else "unfinished" if self._closing else (
                    "interrupted" if self._interrupted or self._assistant_reply_id != self._reply_id
                    or isinstance(frame, (InterruptionFrame, CancelFrame)) else "completed"
                )
                self._emit({"type": "finished", "reply_id": self._assistant_reply_id, "status": status,
                            "messages": deepcopy(self.context.get_messages())})
            if self.voice_enabled and isinstance(frame, (FunctionCallResultFrame, FunctionCallCancelFrame)):
                self._busy("tool", assistant.has_function_calls_in_progress)

        @self._llm.event_handler("on_error")
        async def on_model_error(service, error):
            self._failed = True
            self._failure_reason = f"模型请求失败：{error.error}"
            self._emit({"type": "error", "reply_id": self._reply_id,
                        "error": self._failure_reason})

        @self._worker.event_handler("on_pipeline_error")
        async def on_pipeline_error(worker, error):
            if error.processor is not self._llm:
                self._failed = True
                self._emit({"type": "voice_error" if self.voice_enabled else "error", "reply_id": self._reply_id,
                            "error": f"对话处理失败：{error.error}"})

        if voice:
            async def on_voice_error(processor, error):
                self._failed = True
                self._failure_reason = f"语音处理失败：{error.error}"
                self._emit({"type": "voice_error", "reply_id": self._reply_id, "error": self._failure_reason})

            # setup 阶段的错误也在处理器自身同步采集，保留原始设备或连接原因。
            for processor in processors:
                if processor is not self._llm:
                    processor.add_event_handler("on_error", on_voice_error)

            @self._voice.interruption.event_handler("on_decision")
            async def on_decision(processor, text, decision):
                self._emit({"type": "voice_activity", "activity": "speaking" if decision == "backchannel" else "recognizing"})

            @self._voice.interruption.event_handler("on_activity")
            async def on_classifying(processor, active):
                self._busy("classification", active)

            @self._voice.stt.event_handler("on_recognition")
            async def on_recognizing(service, active):
                self._busy("recognition", active)

            @self._voice.stt.event_handler("on_before_push_frame")
            async def on_recognition(service, frame):
                if isinstance(frame, (InterimTranscriptionFrame, TranscriptionFrame)) and frame.text:
                    self._emit({"type": "voice_activity", "activity": "classifying" if self._voice.interruption._bot_speaking else "recognizing"})

    async def start(self) -> None:
        await self._runner.add_workers(self._worker)
        self._runner_task = asyncio.create_task(self._run(), name="text-conversation")
        await self._wait_until(self._ready)

    def _busy(self, activity: str, active: bool):
        self._emit({"type": "voice_busy", "activity": activity, "active": active})

    def _observe_voice_frame(self, frame):
        if isinstance(frame, (VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame)):
            active = isinstance(frame, VADUserStartedSpeakingFrame)
            self._busy("capture", active)
            if active:
                self._busy("recognition", True)
        elif isinstance(frame, (UserStartedSpeakingFrame, UserStoppedSpeakingFrame)):
            self._busy("user_turn", isinstance(frame, UserStartedSpeakingFrame))
        elif isinstance(frame, (BotStartedSpeakingFrame, BotStoppedSpeakingFrame)):
            self._busy("playback", isinstance(frame, BotStartedSpeakingFrame))
            if isinstance(frame, BotStoppedSpeakingFrame):
                self._voice.playback_stopped()

    async def _end_voice(self, params):
        if self._voice.gate.mode == "active" and self._generation_reply_id == self._reply_id:
            self._emit({"type": "end_requested", "reply_id": self._generation_reply_id})
            await params.result_callback({"status": "本轮回复和实际播报完成后结束语音，可简短收尾。"})
        else:
            await params.result_callback({"status": "当前语音未激活，继续文字交流。"})

    async def activate_voice(self):
        self._voice.gate.mode = "paused"
        self._interrupted = True
        await self._user.broadcast_interruption()
        if not await self._worker.flush_pipeline():
            raise RuntimeError("唤醒前原生提交超时。")
        self.context.set_tools(self._voice_tools)
        await self._worker.queue_frame(LLMConfigureOutputFrame(skip_tts=False))
        await self._voice.stt.connect()
        tone = asyncio.create_task(self._voice.play_ready_tone())
        try:
            done, _ = await asyncio.wait((tone, self._runner_task), return_when=asyncio.FIRST_COMPLETED)
            if self._runner_task in done:
                await self._runner_task
                raise RuntimeError(self._failure_reason or self._unavailable_hint)
            await tone
        finally:
            tone.cancel()
            await asyncio.gather(tone, return_exceptions=True)
        self._voice.gate.mode = "active"

    async def standby_voice(self):
        """按钮或超时通过原生取消和轮次结束保留上下文，设备及 worker 继续存续。"""
        self._voice.gate.mode = "paused"
        self._interrupted = True
        await self._user.broadcast_interruption()
        await self._voice.stt.disconnect()
        await self._user.broadcast_interruption()
        if not await self._worker.flush_pipeline():
            raise RuntimeError("语音结束时原生提交超时。")
        # 锁定版本的原生结束路径提交残余正文，run_llm=False 保持助手静默。
        await self._user._maybe_emit_user_turn_stopped(on_session_end=True, run_llm=False)
        controller = self._user._user_turn_controller
        controller._user_speaking = False
        # 原生控制器负责结束当前轮次并清理 Smart Turn 等策略的音频缓存。
        await controller._trigger_user_turn_stop(None, UserTurnStoppedParams(enable_user_speaking_frames=True))
        if self._voice_pending is not None:
            pending = self._voice_pending
            self._reply_id = pending["reply_id"]
            self._emit({"type": "voice_unfinished", **pending,
                        "text": self._voice_finals + self._voice_interim,
                        "messages": deepcopy(self.context.get_messages())})
            self._voice_pending = None
            self._voice_finals = self._voice_interim = ""
        self.context.set_tools([])
        await self._voice.gate.reset_detector()
        self._voice.gate.mode = "standby"

    async def _run(self) -> None:
        try:
            await self._runner.run()
        finally:
            if self._voice is not None:
                await self._voice.close()
            if not self._closing:
                self._emit({"type": "unavailable", "voice": self.voice_enabled,
                            "error": f"{self._failure_reason} {self._unavailable_hint}".strip()})

    async def _wait_until(self, event: asyncio.Event) -> None:
        """等待本地流水线信号，同时传播执行对象的结束与异常。"""
        waiter = asyncio.create_task(event.wait())
        try:
            done, _ = await asyncio.wait((waiter, self._runner_task), return_when=asyncio.FIRST_COMPLETED)
            if self._runner_task in done:
                await self._runner_task
                raise RuntimeError(f"{self._failure_reason} {self._unavailable_hint}".strip())
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
        self._failure_reason = ""
        self._started.clear()
        self._emit({"type": "started", "reply_id": reply_id})
        await self._worker.queue_frame(LLMConfigureOutputFrame(skip_tts=True))
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
            if self._reply_id is not None:
                # 输入可能已进入原生上下文、但助手尚未开始；退出后再采集最终提交。
                self._emit({"type": "context", "reply_id": self._reply_id,
                            "messages": deepcopy(self.context.get_messages())})
