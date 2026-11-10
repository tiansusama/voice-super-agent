import asyncio
from contextlib import suppress
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
import sqlite3
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
    InputAudioRawFrame,
)
from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
    LLMAssistantAggregator,
)
from pipecat.audio.vad.vad_analyzer import VADState
from websockets.exceptions import WebSocketException
from voice_super_agent.voice.input import VoiceUserAggregator
from pipecat.services.deepseek.llm import DeepSeekLLMService
from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies
from pipecat.turns.user_stop import UserTurnStoppedParams
from pipecat.workers.runner import WorkerRunner


class TextDeepSeekService(DeepSeekLLMService):
    """模型请求失败直接反馈，由用户决定后续输入。"""

    voice_input = None

    async def process_frame(self, frame, direction):
        if isinstance(frame, LLMContextFrame) and frame.metadata.get("input_source") == "voice":
            if self.voice_input.frozen or frame.metadata["voice_epoch"] != self.voice_input.epoch:
                return
        await super().process_frame(frame, direction)

    def create_client(self, api_key=None, base_url=None, **kwargs):
        try:
            return AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0, timeout=60)
        except InvalidURL as exc:
            raise ValueError(f"DEEPSEEK_BASE_URL 配置无效：{exc}") from exc

    def build_chat_completion_params(self, params_from_context):
        params = super().build_chat_completion_params(params_from_context)
        # 每次请求提供当前时间，常驻进程跨日期后仍能解释历史时间线索。
        instruction = params["messages"][0]
        params["messages"] = [dict(instruction, content=instruction["content"] +
                                   f"当前本地时间：{datetime.now().astimezone().isoformat()}。历史时间为 UTC。"),
                              *params["messages"][1:]]
        return params

    async def get_chat_completions(self, context):
        stream = await super().get_chat_completions(context)

        async def chunks():
            calls = {}
            template = None
            try:
                async for chunk in stream:
                    if chunk.choices and chunk.choices[0].delta.tool_calls:
                        template = chunk
                        # 当前 Pipecat 按顺序读取每块的一个调用；先按 index 聚合服务端交错的工具片段。
                        for part in chunk.choices[0].delta.tool_calls:
                            if part.index not in calls:
                                calls[part.index] = part.model_copy(deep=True)
                            else:
                                call = calls[part.index]
                                if part.id:
                                    call.id = part.id
                                if part.function:
                                    call.function.name = (call.function.name or "") + (part.function.name or "")
                                    call.function.arguments = (call.function.arguments or "") + (part.function.arguments or "")
                        chunk = chunk.model_copy(deep=True)
                        chunk.choices[0].delta.tool_calls = None
                    yield chunk
                for index in sorted(calls):
                    choice = template.choices[0]
                    delta = choice.delta.model_copy(update={"content": None, "tool_calls": [calls[index]]})
                    yield template.model_copy(update={"usage": None, "choices": [choice.model_copy(update={"delta": delta})]})
            finally:
                await stream.close()

        return chunks()

    async def cleanup(self):
        await super().cleanup()
        await self._client.close()


class Conversation:
    """原生聚合器独占模型上下文，展示事件绑定当前回复。"""

    def __init__(self, api_key: str, model: str, base_url: str, emit: Callable[[dict], None],
                 messages: list[dict] | None = None, voice: bool = False, history: Callable | None = None):
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
        self._close_status = "unfinished"
        self._ready = asyncio.Event()
        self._started = asyncio.Event()
        self._runner_task: asyncio.Task | None = None
        self.voice_enabled = voice
        self._unavailable_hint = "语音对话已停止，请检查服务或设备后重新启动语音。" if voice else "文字对话已停止，请检查模型配置后重启应用。"
        self._voice = None
        self._voice_pending: dict | None = None
        self._voice_finals = ""
        self._voice_interim = ""
        self._generation_source = "text"
        self._asr_resume_task: asyncio.Task | None = None
        self._response_has_tools = False
        self._history = history
        self._history_calls = {}
        history_tools = [FunctionSchema(
            name="history_search",
            description="只读检索小博自身历史。将主题拆成原文可能出现的关键词，任一词命中即可；可结合大致时间缩小范围。"
                        "无主题或时间线索时先追问；空结果如实说明，可改写关键词继续查找。返回预览仅用于定位，回答历史细节前用 history_read 读取原文。",
            properties={
                "keywords": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 100}, "maxItems": 8},
                "start_time": {"type": "string", "description": "包含此时刻，带时区的 ISO 8601 时间。"},
                "end_time": {"type": "string", "description": "不包含此时刻，带时区的 ISO 8601 时间。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "默认 5 条，按保存顺序从新到旧。"},
                "before_position": {"type": "integer", "minimum": 0, "description": "续页使用 next_before_position，保留原查询条件。"},
            }, required=[], handler=self._history_call,
        ), FunctionSchema(
            name="history_read",
            description="按检索返回的消息 ID 读取自身历史原文，保留来源、状态及同轮关联。text 为原文，偏移以 Unicode 字符计数，从 0 开始；"
                        "next_offset 为 null 表示读完。同轮关联最多 10 条，可用 next_related_offset 续页；前后消息 ID 可继续读取。"
                        "未完成、被打断及附和记录应按其实际状态理解；读取失败如实说明。",
            properties={
                "message_id": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0, "description": "默认 0；续读使用 next_offset。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 8000, "description": "默认 2000 个字符。"},
                "related_offset": {"type": "integer", "minimum": 0, "description": "同轮关联列表偏移，默认 0。"},
            }, required=["message_id"], handler=self._history_call,
        )] if history is not None else []
        self._text_tools = ToolsSchema(history_tools)
        self._voice_tools = ToolsSchema(history_tools + [FunctionSchema(
            name="end_voice", description="用户明确要求结束语音对话时调用。引用、讨论结束字样时继续交流。调用后可简短收尾，也可不回复正文。",
            properties={}, required=[], handler=self._end_voice,
        )]) if voice else self._text_tools
        self.context.set_tools(self._voice_tools if voice else self._text_tools)
        self._llm = TextDeepSeekService(
            api_key=api_key,
            base_url=base_url,
            settings=DeepSeekLLMService.Settings(
                model=model,
                system_instruction=("你是小博，一个友好的中文助手。请结合对话上下文，用清晰自然的中文回答。"
                                        + ("需要回忆历史时使用 history_search 和 history_read，以读取的原文为依据；线索不足先追问。" if history is not None else "")
                                      + ("回答会直接朗读，请使用适合口语的表达，每次一到三句话。用户明确表示结束本次语音时调用 end_voice，可自行决定简短收尾。" if voice else "")),
            ),
        )
        if voice:
            from voice_super_agent.voice.pipeline import VoicePipeline

            self._voice = VoicePipeline(self.context, emit)
            self._llm.voice_input = self._voice.boundary
            self._user = VoiceUserAggregator(self.context, params=self._voice.user_params,
                                             boundary=self._voice.boundary, on_quiet=self._input_quiet)
            assistant = LLMAssistantAggregator(self.context, _paired_user_aggregator=self._user)
        else:
            self._user, assistant = LLMContextAggregatorPair(self.context, user_params=LLMUserAggregatorParams(
                user_turn_strategies=ExternalUserTurnStrategies(),
                empty_user_turn=None,
            ))
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
            if isinstance(frame, LLMFullResponseStartFrame):
                self._response_has_tools = False
                if self.voice_enabled:
                    self._busy("model", True)
            elif isinstance(frame, FunctionCallsStartedFrame):
                self._response_has_tools = True
                if self.voice_enabled:
                    self._busy("tool", True)
            elif isinstance(frame, LLMFullResponseEndFrame):
                frame.metadata["has_function_calls"] = self._response_has_tools
                if self.voice_enabled:
                    self._busy("model", False)

        @assistant.event_handler("on_before_push_frame")
        async def on_tool_context(aggregator, frame):
            if isinstance(frame, LLMContextFrame):
                # 工具循环沿用所属回复和输入来源，经过相同的原生生成入口。
                frame.metadata["reply_id"] = self._assistant_reply_id
                frame.metadata["input_source"] = self._generation_source
                if self.voice_enabled:
                    frame.metadata["voice_epoch"] = self._voice.boundary.epoch

        @self._llm.event_handler("on_before_process_frame")
        async def on_model_input(service, frame):
            if isinstance(frame, LLMContextFrame):
                self._generation_reply_id = frame.metadata["reply_id"]
                self._generation_source = frame.metadata.get("input_source", self._generation_source)

        @self._user.event_handler("on_before_process_frame")
        async def on_user_frame(aggregator, frame):
            if not self.voice_enabled:
                return
            if self._voice.boundary.frozen and not isinstance(frame, (BotStartedSpeakingFrame, BotStoppedSpeakingFrame)):
                return
            if isinstance(frame, (InputAudioRawFrame, TranscriptionFrame, InterimTranscriptionFrame, UserBackchannelFrame)):
                if not self._voice.boundary.accepts(frame):
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
            if self.voice_enabled and not self._voice.boundary.frozen:
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
                self.context.set_tools(self._voice_tools)
                await self._user.push_frame(LLMConfigureOutputFrame(skip_tts=False))
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
                status = "failed" if self._failed else self._close_status if self._closing else (
                    "interrupted" if self._interrupted or self._assistant_reply_id != self._reply_id
                    or isinstance(frame, (InterruptionFrame, CancelFrame)) else "completed"
                )
                self._emit({"type": "finished", "reply_id": self._assistant_reply_id, "status": status,
                            "messages": deepcopy(self.context.get_messages())})
            if isinstance(frame, (FunctionCallResultFrame, FunctionCallCancelFrame)):
                self._emit({"type": "context", "reply_id": self._assistant_reply_id,
                            "messages": deepcopy(self.context.get_messages())})
                if self.voice_enabled:
                    self._busy("tool", assistant.has_function_calls_in_progress)

        services = {self._llm: ("model", "模型请求失败")}
        if voice:
            services.update({self._voice.stt: ("asr", "语音识别失败"),
                             self._voice.interruption: ("system_one", "语音判意失败"),
                             self._voice.tts: ("tts", "语音合成失败"),
                             self._voice.transport.input(): ("audio", "录音设备失败"),
                             self._voice.transport.output(): ("audio", "播放设备失败")})

        async def on_service_error(processor, error):
            service, label = services.get(processor, ("pipeline", "对话处理失败"))
            self._failed = True
            self._failure_reason = f"{label}：{error.error}"
            reply_id = self._generation_reply_id if service in {"model", "tts"} else self._reply_id
            if self._voice_pending is not None and service in {"asr", "system_one"}:
                reply_id = self._voice_pending["reply_id"]
            # 原生分类和可用性判定已在 on_error 前完成，保留其具体失败语义。
            self._emit({"type": "error", "reply_id": reply_id, "error": self._failure_reason,
                        "service": service, "category": error.category.value,
                        "permanent": error.fatal or not processor.is_usable, "voice": self.voice_enabled})

        for processor in processors:
            processor.add_event_handler("on_error", on_service_error)

        if voice:
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
                    if not self._voice.boundary.accepts(frame):
                        return
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

    async def _history_call(self, params):
        reply_id = self._generation_reply_id
        self._history_calls[params.tool_call_id] = reply_id
        self._emit({"type": "history", "reply_id": reply_id, "status": "reading", "active": True, "error": ""})
        status, error = "cancelled", ""
        try:
            try:
                result = await self._history(params.function_name, params.arguments, reply_id)
            except (ValueError, TypeError) as exc:
                result = {"status": "invalid_arguments", "message": str(exc)}
            except (sqlite3.Error, OSError) as exc:
                result = {"status": "failed", "message": f"历史读取失败：{exc}"}
                error = result["message"]
            status = result["status"]
            await params.result_callback(result)
        finally:
            del self._history_calls[params.tool_call_id]
            self._emit({"type": "history", "reply_id": reply_id, "status": status,
                        "active": bool(self._history_calls), "error": error})

    async def _end_voice(self, params):
        if (self._voice.gate.mode == "active" and not self._voice.boundary.frozen
                and self._generation_source == "voice" and self._generation_reply_id == self._reply_id):
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
        self._voice.stt.send_paused = False
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
        await self._stop_asr_resume()
        self._voice.boundary.frozen = self._voice.boundary.wait_for_quiet = False
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

    async def reopen_audio(self):
        self._voice.gate.mode = "paused"
        await self._voice.transport.reopen()
        await self._voice.gate.reset_detector()
        self._voice.gate.mode = "standby"

    async def _run(self) -> None:
        try:
            await self._runner.run()
        finally:
            await self._stop_asr_resume()
            if self._voice is not None:
                await self._voice.close()
            if not self._closing:
                self._emit({"type": "unavailable", "voice": self.voice_enabled,
                            "reply_ids": (self._reply_id, self._generation_reply_id,
                                          self._voice_pending["reply_id"] if self._voice_pending is not None else None),
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

    def begin_text_takeover(self):
        """接收文字时立即划定采集边界，异步队列中的旧识别继续携带原代次。"""
        boundary = self._voice.boundary
        boundary.wait_for_quiet |= (self._voice_pending is not None
                                    or self._voice.interruption._candidate is not None
                                    or self._voice.user_params.vad_analyzer._vad_state != VADState.QUIET)
        epoch = boundary.advance()
        self._voice.transport.input_epoch = self._voice.stt.input_epoch = epoch
        self._voice.stt.send_paused = True
        self._busy("text_handoff", True)
        return epoch

    def _input_quiet(self):
        if not self._voice.boundary.frozen:
            self._voice.stt.send_paused = False
        self._busy("input_tail", False)

    async def _stop_asr_resume(self):
        if self._asr_resume_task is not None:
            self._asr_resume_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._asr_resume_task
            self._asr_resume_task = None

    async def _resume_asr(self):
        self._busy("recognition_setup", True)
        try:
            await self._voice.stt.connect(keep_buffer=True)
        except (OSError, RuntimeError, ValueError, TimeoutError, WebSocketException) as exc:
            await self._voice.stt.push_error(error_msg=f"文字接管后恢复识别失败：{exc}",
                                             exception=exc, force_treat_as_permanent=True)
        finally:
            self._busy("recognition_setup", False)

    async def send(self, reply_id: str, text: str, *, voice_epoch=None) -> None:
        """按 STS 的打断、提交、追加顺序处理；仅等待本地轮次启动。"""
        self._interrupted = True
        active_voice = self.voice_enabled and self._voice.gate.mode == "active"
        if active_voice:
            if voice_epoch is None:
                voice_epoch = self.begin_text_takeover()
            await self._stop_asr_resume()
            await self._voice.stt.disconnect()
            await self._voice.interruption.reset_input()
        await self._user.broadcast_interruption()
        if not await self._worker.flush_pipeline():
            raise RuntimeError("旧回复提交超时，请重启应用后继续。")
        if active_voice:
            await self._user.discard_voice()
            if self._voice_pending is not None:
                self._emit({"type": "voice_superseded", **self._voice_pending,
                            "text": self._voice_finals + self._voice_interim})
                self._voice_pending = None
                self._voice_finals = self._voice_interim = ""
            self._emit({"type": "voice_input_reset"})
        self.context.set_tools(self._text_tools)
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
        if active_voice and voice_epoch == self._voice.boundary.epoch:
            self._voice.stt._opening = True
            self._voice.boundary.frozen = False
            self._voice.stt.send_paused = self._voice.boundary.wait_for_quiet
            self._busy("input_tail", self._voice.boundary.wait_for_quiet)
            self._asr_resume_task = asyncio.create_task(self._resume_asr(), name="resume-asr-after-text")
            self._busy("text_handoff", False)

    async def close(self, *, interrupted: bool = False) -> None:
        self._closing = True
        self._close_status = "interrupted" if interrupted else "unfinished"
        self._interrupted = True
        await self._stop_asr_resume()
        if self._runner_task is not None:
            await self._runner.cancel()
            await self._runner_task
            if self._reply_id is not None:
                # 输入可能已进入原生上下文、但助手尚未开始；退出后再采集最终提交。
                self._emit({"type": "context", "reply_id": self._reply_id,
                            "messages": deepcopy(self.context.get_messages())})
