import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest
from pipecat.frames.frames import BotStoppedSpeakingFrame, InterruptionFrame, OutputAudioRawFrame, TTSStartedFrame, TTSStoppedFrame, TTSAudioRawFrame
from pipecat.tests.utils import SleepFrame, run_test

from voice_super_agent.audio import transport as module
from voice_super_agent.audio.devices import AudioDevices, Endpoint
from voice_super_agent.audio.processing import EchoAudio, RATE, audio_level


def test_reference_uses_dac_time_and_preserves_only_played_audio():
    audio = EchoAudio()
    first = np.full(160, 1000, dtype=np.int16)
    second = np.full(160, 2000, dtype=np.int16)
    audio.rendered(first.tobytes(), 10.02)
    audio.rendered(second.tobytes(), 10.03)
    assert not audio.reference(10, 160).any()
    np.testing.assert_array_equal(audio.reference(10.025, 160), np.concatenate((first[:80], second[:80])))
    assert not audio.reference(10.04, 160).any()


def test_actual_aec_reduces_delayed_synthetic_echo_after_convergence():
    rng = np.random.default_rng(7)
    speech = np.convolve(rng.normal(size=RATE * 8), np.ones(9) / 9, mode="same")
    far = (speech * 9000).astype(np.int16)
    near = (np.roll(far.astype(float), 640) * .5 + np.roll(far.astype(float), 900) * .15).astype(np.int16)
    engine = EchoAudio()
    clean = []
    for offset in range(0, len(far), 160):
        timestamp = offset / RATE
        engine.rendered(far[offset:offset + 160].tobytes(), timestamp)
        clean.append(np.frombuffer(engine.capture(near[offset:offset + 160].tobytes(), timestamp), dtype=np.int16))
    raw_energy = np.mean(near[RATE * 5:].astype(float) ** 2)
    clean_energy = np.mean(np.concatenate(clean)[RATE * 5:].astype(float) ** 2)
    assert 10 * np.log10(raw_energy / clean_energy) > 10


def test_levels_are_independent_normalized_and_increase_with_volume():
    assert audio_level(bytes(320)) == 0
    assert 0 < audio_level(np.full(160, 1000, dtype=np.int16).tobytes()) < audio_level(np.full(160, 8000, dtype=np.int16).tobytes()) < 1


@pytest.fixture
def audio(monkeypatch):
    events, sessions = [], []
    devices = AudioDevices(Endpoint("mic", "麦克风"), Endpoint("speaker", "播放设备"), frozenset({"mic", "speaker"}))

    class Stream:
        def __init__(self, options):
            self.options = options
            self.closed = False
            self.active = False

        def start_stream(self):
            self.active = True

        def is_active(self):
            return self.active

        def close(self):
            assert not self.closed
            self.closed = True
            self.active = False

    class Session:
        def __init__(self):
            self.streams = []
            self.terminated = False
            sessions.append(self)

        def get_default_input_device_info(self):
            return {"index": 1}

        def get_default_output_device_info(self):
            return {"index": 2}

        def open(self, **options):
            stream = Stream(options)
            self.streams.append(stream)
            return stream

        def terminate(self):
            self.terminated = True

    monkeypatch.setattr(module, "read_devices", lambda: devices)
    monkeypatch.setattr(module.pyaudio, "PyAudio", Session)
    from pipecat.transports.local.audio import LocalAudioTransportParams
    transport = module.DesktopAudioTransport(LocalAudioTransportParams(
        audio_in_enabled=True, audio_out_enabled=True, audio_in_sample_rate=RATE,
        audio_out_sample_rate=RATE, audio_in_channels=1, audio_out_channels=1,
        audio_out_end_silence_secs=0), events.append)
    return SimpleNamespace(transport=transport, events=events, sessions=sessions)


@pytest.mark.asyncio
async def test_callback_submission_and_interruption_preserve_actual_reference_and_drop_unplayed_audio(audio):
    transport = audio.transport
    await transport.open()
    try:
        pcm = np.concatenate((np.full(160, 1000, dtype=np.int16), np.full(160, 2000, dtype=np.int16))).tobytes()
        write = asyncio.create_task(transport.output().write_audio_frame(OutputAudioRawFrame(pcm, RATE, 1)))
        await asyncio.sleep(0)
        assert not transport._echo.reference(100, 320).any()
        rendered, _ = transport._render(None, 160, {"output_buffer_dac_time": 100.02, "current_time": 100}, 0)
        assert rendered == pcm[:320]
        assert not write.done()
        transport.discard_pending()
        await write
        rendered, _ = transport._render(None, 160, {"output_buffer_dac_time": 100.03, "current_time": 100.01}, 0)
        assert rendered == bytes(320)
        np.testing.assert_array_equal(transport._echo.reference(100.02, 160), np.full(160, 1000, dtype=np.int16))
        assert not transport._echo.reference(100.03, 160).any()
        assert transport._last_dac_end > time.perf_counter()
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_reopen_releases_both_streams_and_resets_reference_while_processors_survive(audio):
    transport = audio.transport
    await transport.open()
    old = audio.sessions[0]
    processors = transport.input(), transport.output()
    transport._echo.rendered(np.full(160, 1000, dtype=np.int16).tobytes(), 20)
    try:
        await transport.reopen()
        assert all(stream.closed for stream in old.streams)
        assert old.terminated
        assert len(audio.sessions) == 2
        assert transport.is_active()
        assert processors == (transport.input(), transport.output())
        assert not transport._echo.reference(20, 160).any()
        assert all(stream.options["rate"] == RATE and stream.options["frames_per_buffer"] == 160
                   for session in audio.sessions for stream in session.streams)
    finally:
        await transport.close()
    assert all(session.terminated for session in audio.sessions)
    assert audio.events[-1] == {"type": "audio_levels", "microphone": 0.0, "playback": 0.0}


@pytest.mark.asyncio
async def test_parallel_processor_cleanup_releases_device_resources_once(audio):
    await audio.transport.open()
    await asyncio.gather(audio.transport.close(), audio.transport.close())
    assert len(audio.sessions) == 1
    assert all(stream.closed for stream in audio.sessions[0].streams)
    assert audio.sessions[0].terminated


@pytest.mark.asyncio
async def test_capture_uses_actual_adc_time_and_routes_callback_failure_to_native_error(audio, monkeypatch):
    transport = audio.transport
    await transport.open()
    transport.input().push_audio_frame = AsyncMock()
    transport.input().push_error = AsyncMock()
    try:
        transport._capture(bytes(320), 160, {"input_buffer_adc_time": 10}, 0)
        monkeypatch.setattr(transport._echo, "capture", Mock(side_effect=RuntimeError("音频处理失败")))
        assert transport._capture(bytes(320), 160, {"input_buffer_adc_time": 10.01}, 0)[1] == module.pyaudio.paAbort
        await asyncio.sleep(.01)
        assert transport.input().push_audio_frame.await_count == 1
        frame = transport.input().push_audio_frame.call_args.args[0]
        assert frame.sample_rate == RATE and len(frame.audio) == 320
        assert transport.input().push_error.call_args.kwargs["force_treat_as_permanent"]
    finally:
        await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [module.pyaudio.paInputOverflow, module.pyaudio.paInputUnderflow])
async def test_input_buffer_flags_keep_capture_running_and_preserve_input_epoch(audio, status):
    transport = audio.transport
    await transport.open()
    transport.input().push_audio_frame = AsyncMock()
    transport.input().push_error = AsyncMock()
    transport.input_epoch = 7
    try:
        result = transport._capture(bytes(320), 160, {"input_buffer_adc_time": 10}, status)
        await asyncio.sleep(.01)
        assert result == (None, module.pyaudio.paContinue)
        transport.input().push_audio_frame.assert_awaited_once()
        frame = transport.input().push_audio_frame.call_args.args[0]
        assert frame.metadata["voice_epoch"] == 7
        assert frame.sample_rate == RATE and len(frame.audio) == 320
        transport.input().push_error.assert_not_awaited()
    finally:
        await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [module.pyaudio.paOutputUnderflow, module.pyaudio.paOutputOverflow,
                                  module.pyaudio.paPrimingOutput])
async def test_output_buffer_flags_keep_delivery_and_actual_playback_reference(audio, status):
    transport = audio.transport
    await transport.open()
    transport.output().push_error = AsyncMock()
    try:
        pcm = np.full(160, 1000, dtype=np.int16).tobytes()
        write = asyncio.create_task(transport.output().write_audio_frame(OutputAudioRawFrame(pcm, RATE, 1)))
        await asyncio.sleep(0)
        rendered, result = transport._render(None, 160, {"output_buffer_dac_time": 10, "current_time": 10}, status)
        assert result == module.pyaudio.paContinue
        assert rendered == pcm
        assert await asyncio.wait_for(write, timeout=1)
        np.testing.assert_array_equal(transport._echo.reference(10, 160), np.full(160, 1000, dtype=np.int16))
        transport.output().push_error.assert_not_awaited()
    finally:
        await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", [False, True])
async def test_native_output_resamples_and_waits_for_dac_on_completion_and_interruption(audio, interrupted):
    transport = audio.transport
    delivered, rates = [], []
    write = transport.output().write_audio_frame
    async def recording_write(frame):
        rates.append(frame.sample_rate)
        return await write(frame)
    transport.output().write_audio_frame = recording_write
    finished = asyncio.Event()
    async def pump_device():
        while not finished.is_set():
            if transport._out_stream is not None and transport._out_stream.is_active():
                now = time.perf_counter()
                pcm, _ = transport._render(None, 160, {"current_time": now, "output_buffer_dac_time": now + .005}, 0)
                delivered.append(pcm)
            await asyncio.sleep(.005)
    pump = asyncio.create_task(pump_device())
    pcm = np.full(48000 if interrupted else 2400, 4000, dtype=np.int16).tobytes()
    frames = []
    for context in (("reply",) if interrupted else ("wake-tone", "reply")):
        frames.extend([TTSStartedFrame(context_id=context), TTSAudioRawFrame(pcm, 24000, 1, context_id=context),
                       TTSStoppedFrame(context_id=context)])
    if interrupted:
        frames.extend([SleepFrame(sleep=.03), InterruptionFrame(), SleepFrame(sleep=.03)])
    try:
        down, _ = await run_test(transport.output(), frames_to_send=frames)
        assert rates and set(rates) == {16000}
        assert any(np.frombuffer(block, dtype=np.int16).any() for block in delivered)
        assert sum(isinstance(frame, BotStoppedSpeakingFrame) for frame in down) == (1 if interrupted else 2)
        assert time.perf_counter() >= transport._last_dac_end
        if interrupted:
            assert any(event["type"] == "audio_stop" and event["elapsed_ms"] > 0 for event in audio.events)
            assert sum(np.count_nonzero(np.frombuffer(block, dtype=np.int16)) for block in delivered) < RATE
    finally:
        finished.set()
        await pump
        await transport.close()
