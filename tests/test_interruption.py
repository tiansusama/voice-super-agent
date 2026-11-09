"""System One classification integrated with native Pipecat user turns."""

import asyncio
from types import SimpleNamespace

import aiohttp
import pytest

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    STTMetadataFrame,
    TranscriptionFrame,
    UserBackchannelFrame,
    UserStartedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.tests.utils import SleepFrame, run_test
from pipecat.turns.user_start import TranscriptionUserTurnStartStrategy
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from voice_super_agent.voice.interruption import SystemOneInterruptionProcessor


def _pipeline(respond):
    context = LLMContext()
    requests = []

    class Response:
        async def __aenter__(self):
            self.choice = await respond(self.text)
            return self

        async def __aexit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        async def json(self):
            return {"answers": {"interruption_intent": {"choice": self.choice}}}

    def post(url, **kwargs):
        requests.append(kwargs)
        response = Response()
        response.text = kwargs["json"]["state"]["user_transcript"]
        return response

    gate = SystemOneInterruptionProcessor(
        api_key="test-key", session=SimpleNamespace(post=post), context=context
    )
    user, _ = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            user_turn_strategies=UserTurnStrategies(
                start=[TranscriptionUserTurnStartStrategy()],
                stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.01)],
            )
        ),
    )
    return gate, context, Pipeline([gate, user]), requests


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["嗯嗯", "对", "好的，我在听，你继续说。"])
async def test_backchannel_keeps_native_turn_closed_and_stays_out_of_context(text):
    async def respond(text):
        return "backchannel"

    gate, context, pipeline, requests = _pipeline(respond)
    down, up = await run_test(
        pipeline,
        frames_to_send=[
            BotStartedSpeakingFrame(),
            InterimTranscriptionFrame(text, "user", "now"),
            TranscriptionFrame(text, "user", "now"),
            SleepFrame(sleep=0.1),
        ],
    )
    assert context.get_messages() == []
    assert not any(isinstance(frame, (InterruptionFrame, UserStartedSpeakingFrame)) for frame in [*down, *up])
    assert [frame.text for frame in down if isinstance(frame, UserBackchannelFrame)] == [text]
    assert len(requests) == 1
    assert gate._candidate is None


@pytest.mark.asyncio
async def test_interrupt_uses_native_start_and_completion_and_preserves_final_once():
    async def respond(text):
        await asyncio.sleep(0.02)
        return "interrupt"

    _, context, pipeline, _ = _pipeline(respond)
    down, up = await run_test(
        pipeline,
        frames_to_send=[
            STTMetadataFrame(service_name="test-asr", ttfs_p99_latency=0.01),
            BotStartedSpeakingFrame(),
            InterimTranscriptionFrame("等一下", "user", "now"),
            TranscriptionFrame("等一下，先别说了。", "user", "now"),
            SleepFrame(sleep=0.2),
        ],
    )
    assert any(isinstance(frame, InterruptionFrame) for frame in [*down, *up])
    assert context.get_messages() == [{"role": "user", "content": "等一下，先别说了。"}]


@pytest.mark.asyncio
async def test_idle_speech_bypasses_classifier():
    async def respond(text):
        raise AssertionError("Idle speech should use native turn handling")

    _, context, pipeline, requests = _pipeline(respond)
    await run_test(
        pipeline,
        frames_to_send=[
            STTMetadataFrame(service_name="test-asr", ttfs_p99_latency=0.01),
            TranscriptionFrame("你好", "user", "now"),
            SleepFrame(sleep=0.1),
        ],
    )
    assert requests == []
    assert context.get_messages() == [{"role": "user", "content": "你好"}]


@pytest.mark.asyncio
async def test_revised_text_supersedes_a_late_interrupt_decision():
    async def respond(text):
        await asyncio.sleep(0.03)
        return "interrupt" if text == "等一下" else "backchannel"

    _, context, pipeline, requests = _pipeline(respond)
    down, up = await run_test(
        pipeline,
        frames_to_send=[
            BotStartedSpeakingFrame(),
            InterimTranscriptionFrame("等一下", "user", "now"),
            SleepFrame(sleep=0.01),
            InterimTranscriptionFrame("等一下，不用停，你继续", "user", "now"),
            TranscriptionFrame("等一下，不用停，你继续", "user", "now"),
            SleepFrame(sleep=0.15),
        ],
    )
    assert not any(isinstance(frame, InterruptionFrame) for frame in [*down, *up])
    assert context.get_messages() == []
    assert [request["json"]["state"]["user_transcript"] for request in requests] == [
        "等一下", "等一下，不用停，你继续"
    ]


@pytest.mark.asyncio
async def test_empty_asr_revision_cancels_pending_classification():
    cancelled = asyncio.Event()

    async def respond(text):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    gate, context, pipeline, _ = _pipeline(respond)
    down, up = await run_test(
        pipeline,
        frames_to_send=[
            BotStartedSpeakingFrame(),
            InterimTranscriptionFrame("等一下", "user", "now"),
            SleepFrame(sleep=0.05),
            InterimTranscriptionFrame("", "user", "now"),
            SleepFrame(sleep=0.03),
        ],
    )
    assert cancelled.is_set()
    assert gate._candidate is None
    assert context.get_messages() == []
    assert not any(isinstance(frame, InterruptionFrame) for frame in [*down, *up])


@pytest.mark.asyncio
async def test_new_bot_output_discards_previous_pending_decision():
    cancelled = asyncio.Event()

    async def respond(text):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    gate, _, pipeline, _ = _pipeline(respond)
    await run_test(
        pipeline,
        frames_to_send=[
            BotStartedSpeakingFrame(),
            InterimTranscriptionFrame("等一下", "user", "now"),
            SleepFrame(sleep=0.01),
            BotStoppedSpeakingFrame(),
            BotStartedSpeakingFrame(),
            SleepFrame(sleep=0.03),
        ],
    )
    assert cancelled.is_set()
    assert gate._candidate is None


@pytest.mark.asyncio
async def test_cleanup_cancels_outstanding_request():
    cancelled = asyncio.Event()

    async def respond(text):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    gate, _, pipeline, _ = _pipeline(respond)
    await run_test(
        pipeline,
        frames_to_send=[
            BotStartedSpeakingFrame(),
            InterimTranscriptionFrame("等一下", "user", "now"),
            SleepFrame(sleep=0.05),
        ],
    )
    assert cancelled.is_set()
    assert gate._request_task is None


@pytest.mark.asyncio
async def test_request_error_marks_processor_unusable():
    async def respond(text):
        raise aiohttp.ClientConnectionError("System One unavailable")

    gate, _, pipeline, _ = _pipeline(respond)
    await run_test(
        pipeline,
        frames_to_send=[
            BotStartedSpeakingFrame(),
            InterimTranscriptionFrame("等一下", "user", "now"),
            SleepFrame(sleep=0.03),
        ],
    )
    assert not gate.is_usable
