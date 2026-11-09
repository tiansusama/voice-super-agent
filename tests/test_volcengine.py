"""Protocol and native frame lifecycle tests for the local speech adapters."""

import asyncio
import base64
import gzip
import json
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from voice_super_agent.voice.volcengine import VolcengineSTTService, VolcengineTTSService
from pipecat.frames.frames import (
    InterimTranscriptionFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
)
from pipecat.tests.utils import run_test


@pytest.mark.asyncio
async def test_stt_converts_provider_snapshots_and_publishes_each_final_once():
    packets = []
    for utterances in (
        [{"text": "你", "start_time": 0, "end_time": 200}],
        [{"text": "你好。", "start_time": 0, "end_time": 500, "definite": True}],
        [
            {"text": "你好。", "start_time": 0, "end_time": 500, "definite": True},
            {"text": "再见。", "start_time": 1000, "end_time": 1500, "definite": True},
        ],
    ):
        payload = gzip.compress(json.dumps({"result": {"utterances": utterances}}).encode())
        packets.append(b"\x11\x91\x11\x00" + struct.pack(">iI", 1, len(payload)) + payload)

    async def incoming():
        for packet in packets:
            yield packet

    service = VolcengineSTTService(api_key="test-key")
    service._socket = incoming()
    service.push_frame = AsyncMock()
    service.push_error = AsyncMock()
    await service._receive_results(service._socket, 0)
    frames = [call.args[0] for call in service.push_frame.call_args_list]
    assert [(type(frame), frame.text) for frame in frames] == [
        (InterimTranscriptionFrame, "你"),
        (TranscriptionFrame, "你好。"),
        (InterimTranscriptionFrame, ""),
        (TranscriptionFrame, "再见。"),
    ]
    assert service.push_error.call_args.kwargs["force_treat_as_permanent"] is True


@pytest.mark.asyncio
async def test_stt_sends_pcm_in_provider_packets_without_ending_user_turns():
    service = VolcengineSTTService(api_key="test-key")
    service._socket = SimpleNamespace(send=AsyncMock())
    assert [frame async for frame in service.run_stt(b"\x01\x00" * 1600)] == [None]
    service._socket.send.assert_not_awaited()
    assert [frame async for frame in service.run_stt(b"\x02\x00" * 1600)] == [None]
    packet = service._socket.send.call_args.args[0]
    assert packet[:4] == b"\x11\x21\x01\x00"
    assert struct.unpack_from(">i", packet, 4)[0] == 1
    assert gzip.decompress(packet[12:]) == b"\x01\x00" * 1600 + b"\x02\x00" * 1600


@pytest.mark.asyncio
async def test_old_asr_connection_cannot_retag_results_or_change_new_connection_deduplication():
    def packet(text, end):
        body = gzip.compress(json.dumps({"result": {"utterances": [
            {"text": text, "end_time": end, "definite": True},
        ]}}).encode())
        return b"\x11\x90\x11\x00" + struct.pack(">I", len(body)) + body

    async def incoming(text, end):
        yield packet(text, end)

    service = VolcengineSTTService(api_key="test-key")
    old_socket = incoming("云端迟到的旧结果。", 9999)
    service.input_epoch = 1
    service._socket = incoming("新连接的结果。", 100)
    service.push_frame = AsyncMock()
    service.push_error = AsyncMock()
    await service._receive_results(old_socket, 0)
    service.push_frame.assert_not_awaited()
    service.push_error.assert_not_awaited()
    assert service._final_end_ms == -1
    await service._receive_results(service._socket, 1)
    frame = service.push_frame.call_args.args[0]
    assert frame.text == "新连接的结果。" and frame.metadata["voice_epoch"] == 1


class _Response:
    def __init__(self, events):
        self.events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def raise_for_status(self):
        pass

    @property
    def content(self):
        async def lines():
            for event in self.events:
                yield json.dumps(event).encode() + b"\n"

        return lines()


@pytest.mark.asyncio
async def test_tts_streams_through_native_audio_context_and_closes_at_turn_end():
    first = b"\x01\x00" * 240
    second = b"\x02\x00" * 240
    session = SimpleNamespace(
        post=Mock(return_value=_Response([
            {"code": 0, "data": base64.b64encode(first).decode()},
            {"code": 0, "data": base64.b64encode(second).decode()},
            {"code": 20000000},
        ]))
    )
    service = VolcengineTTSService(api_key="test-key", session=session)
    down, _ = await run_test(service, frames_to_send=[TTSSpeakFrame("你好。")])
    audio = [frame for frame in down if isinstance(frame, TTSAudioRawFrame)]
    assert b"".join(frame.audio for frame in audio) == first + second
    assert {frame.sample_rate for frame in audio} == {24000}
    assert len({frame.context_id for frame in audio}) == 1
    assert session.post.call_args.kwargs["json"]["req_params"]["speaker"] == (
        "zh_female_shuangkuaisisi_uranus_bigtts"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("events", [[{"code": 45000001}], [{"code": 0}]])
async def test_tts_reports_provider_errors_and_truncated_streams(events):
    session = SimpleNamespace(post=Mock(return_value=_Response(events)))
    service = VolcengineTTSService(api_key="test-key", session=session)
    service.push_error = AsyncMock()
    frames = [frame async for frame in service.run_tts("你好。", "context")]
    assert frames == []
    assert service.push_error.call_args.kwargs["force_treat_as_permanent"] is True


@pytest.mark.asyncio
async def test_tts_cancellation_closes_the_http_stream():
    entered = asyncio.Event()
    closed = asyncio.Event()

    class WaitingResponse(_Response):
        async def __aexit__(self, *args):
            closed.set()

        @property
        def content(self):
            async def lines():
                entered.set()
                await asyncio.Event().wait()
                yield b""

            return lines()

    session = SimpleNamespace(post=Mock(return_value=WaitingResponse([])))
    service = VolcengineTTSService(api_key="test-key", session=session)
    task = asyncio.create_task(anext(service.run_tts("你好。", "context")))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
