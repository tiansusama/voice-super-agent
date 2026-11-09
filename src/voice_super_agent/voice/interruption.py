"""Classify overlapping speech before Pipecat opens a user turn."""

# 来源：STS 的 system_one_interruption.py，基于 Pipecat de8998789 迁入后独立维护。

import asyncio
import time
from dataclasses import dataclass, field
from enum import StrEnum

import aiohttp
from loguru import logger

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    DataFrame,
    EndFrame,
    Frame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    TranscriptionFrame,
    UserBackchannelFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class Decision(StrEnum):
    """Intent labels returned by System One."""

    BACKCHANNEL = "backchannel"
    INTERRUPT = "interrupt"
    UNDECIDED = "undecided"


QUESTION = {
    "type": "choice",
    "instructions": (
        "判断最新用户话语表达的接管意图。当前助手正在播报。"
        "结合对话上下文，使用完整短句及其修饰语判断。"
        "短促附和、表示正在听、让助手继续归为backchannel；"
        "要求等待、暂缓、暂停、停止当前播报，或提出纠正、异议、新问题、新要求归为interrupt。"
        "停止或暂缓指令本身即表达了完整接管意图，即使短促、没有进一步解释，也归为interrupt；"
        "转写只有尚未表达明确意图的前缀时归为undecided。"
    ),
    "criteria": {
        "backchannel": "附和或示意继续听，助手继续当前播报。",
        "interrupt": "明确要求等待、暂缓或停止播报，或者纠正、异议、新问题或新要求，用户接管当前对话。",
        "undecided": "转写片段尚未表达完整意图，等待后续语音。",
    },
}


@dataclass
class _Candidate:
    finals: list[TranscriptionFrame] = field(default_factory=list)
    interim: InterimTranscriptionFrame | None = None
    decision: Decision | None = None
    decided_text: str = ""

    @property
    def text(self) -> str:
        return "".join(frame.text for frame in self.finals) + (
            self.interim.text if self.interim is not None else ""
        )


@dataclass
class _ClassificationResultFrame(DataFrame):
    candidate: _Candidate
    text: str
    decision: Decision | None = None
    error: Exception | None = None
    # 分类结果与转写按帧序处理，已确认输入的放行过程由原生打断机制保留。
    interruptible: bool = field(default=False, init=False)


class SystemOneInterruptionProcessor(FrameProcessor):
    """Publish confirmed interruptions and native user backchannel frames."""

    def __init__(self, *, api_key: str, session: aiohttp.ClientSession, context: LLMContext):
        """Initialize the overlap classifier.

        Args:
            api_key: Ark System One API key.
            session: HTTP session owned by the bot.
            context: Pipecat conversation context.
        """
        super().__init__()
        self._api_key = api_key
        self._session = session
        self._context = context
        self._bot_speaking = False
        self._user_turn_active = False
        self._candidate: _Candidate | None = None
        self._request_task: asyncio.Task | None = None
        self._register_event_handler("on_decision", sync=True)
        self._register_event_handler("on_activity", sync=True)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Keep audio flowing while overlapping transcripts await classification."""
        await super().process_frame(frame, direction)
        if isinstance(frame, _ClassificationResultFrame):
            if frame.candidate is not self._candidate:
                return
            self._request_task = None
            await self._call_event_handler("on_activity", False)
            if frame.text == frame.candidate.text:
                if frame.error is not None:
                    await self.push_error(
                        error_msg=f"System One classification failed: {frame.error}",
                        exception=frame.error,
                        force_treat_as_permanent=True,
                    )
                    return
                frame.candidate.decision = frame.decision
                frame.candidate.decided_text = frame.text
                await self._settle()
            self._start_request()
            return
        if isinstance(frame, BotStartedSpeakingFrame):
            await self._discard_candidate()
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
        elif isinstance(frame, UserStartedSpeakingFrame):
            self._user_turn_active = True
            await self._discard_candidate()
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._user_turn_active = False
        elif isinstance(frame, (InterruptionFrame, EndFrame, CancelFrame)):
            await self._discard_candidate()

        if direction == FrameDirection.DOWNSTREAM and isinstance(
            frame, (TranscriptionFrame, InterimTranscriptionFrame)
        ):
            if self._candidate is None and not frame.text.strip():
                return
            if self._candidate is None and (
                self._user_turn_active or not self._bot_speaking
            ):
                if frame.text.strip():
                    await self.push_frame(frame, direction)
                return
            if self._candidate is None:
                self._candidate = _Candidate()
            if isinstance(frame, TranscriptionFrame):
                self._candidate.finals.append(frame)
                self._candidate.interim = None
            else:
                self._candidate.interim = frame if frame.text.strip() else None
            if not self._candidate.text.strip():
                await self._discard_candidate()
                return
            await self._settle()
            self._start_request()
            return
        await self.push_frame(frame, direction)

    def _start_request(self) -> None:
        candidate = self._candidate
        if (
            self._request_task is None
            and candidate is not None
            and candidate.text.strip()
            and candidate.text != candidate.decided_text
        ):
            self._request_task = self.create_task(
                self._classify(candidate, candidate.text), name="system-one-interruption"
            )

    async def _classify(self, candidate: _Candidate, text: str) -> None:
        await self._call_event_handler("on_activity", True)
        started = time.perf_counter()
        result = _ClassificationResultFrame(candidate=candidate, text=text)
        try:
            async with self._session.post(
                "https://ark.cn-beijing.volces.com/api/v1/systemone",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "ark-beta-systemone-api": "true",
                },
                json={
                    "model": "doubao-seed-2-1-lite-260915",
                    "state": {
                        "assistant_is_speaking": True,
                        "conversation": list(self._context.get_messages()),
                        "user_transcript": text,
                    },
                    "questions": {"interruption_intent": QUESTION},
                },
                timeout=aiohttp.ClientTimeout(total=5),
            ) as response:
                response.raise_for_status()
                result.decision = Decision(
                    (await response.json())["answers"]["interruption_intent"]["choice"]
                )
            logger.info(
                "System One decision={} chars={} latency={:.0f}ms",
                result.decision, len(text), (time.perf_counter() - started) * 1000,
            )
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError) as exc:
            result.error = exc
        # 使用 Pipecat 自身队列串行采用结果，已到达的新转写先更新候选内容。
        await self.queue_frame(result)

    async def _settle(self) -> None:
        candidate = self._candidate
        if candidate is None or candidate.decided_text != candidate.text:
            return
        if candidate.decision == "interrupt":
            await self._call_event_handler("on_decision", candidate.text, candidate.decision)
            await self._discard_candidate()
            self._user_turn_active = True
            frames = list(candidate.finals)
            if candidate.interim is not None:
                frames.append(candidate.interim)
            for transcript in frames:
                # 确认接管的输入应跨过它自己触发的 InterruptionFrame。
                transcript.interruptible = False
                await self.push_frame(transcript)
        elif candidate.decision == "backchannel" and candidate.finals and candidate.interim is None:
            await self._call_event_handler("on_decision", candidate.text, candidate.decision)
            await self._discard_candidate()
            final = candidate.finals[-1]
            await self.push_frame(
                UserBackchannelFrame(candidate.text, final.user_id, final.timestamp)
            )

    async def _discard_candidate(self) -> None:
        self._candidate = None
        task = self._request_task
        self._request_task = None
        if task is not None:
            await self.cancel_task(task)
            await self._call_event_handler("on_activity", False)

    async def cleanup(self) -> None:
        """Cancel the session's outstanding classification before cleanup."""
        await self._discard_candidate()
        await super().cleanup()
