import asyncio
from collections import deque
from dataclasses import dataclass
from threading import Lock
import time

import pyaudio
from pipecat.frames.frames import BotStoppedSpeakingFrame, InputAudioRawFrame, InterruptionFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport

from voice_super_agent.audio.devices import read_devices
from voice_super_agent.audio.processing import BLOCK_SAMPLES, EchoAudio, RATE, audio_level


@dataclass
class _Write:
    audio: bytes
    done: asyncio.Future
    offset: int = 0


class DesktopAudioTransport(BaseTransport):
    """设备回调交付播放与收音，Pipecat 继续独占 TTS 队列、轮次和打断。"""

    def __init__(self, params, emit):
        super().__init__()
        self._params = params
        self._emit = emit
        self._input = DesktopAudioInput(self, params)
        self._output = DesktopAudioOutput(self, params)
        self._pa = None
        self._in_stream = self._out_stream = None
        self._open_lock = asyncio.Lock()
        self._writes: deque[_Write] = deque()
        self._write_lock = Lock()
        self._echo = EchoAudio()
        self._render_paused = False
        self._last_dac_end = 0.0
        self._level_handles: set[asyncio.TimerHandle] = set()
        self._mic_level = self._playback_level = 0.0
        self._level_time = 0.0
        self.devices = None
        self.input_epoch = 0

    def input(self):
        return self._input

    def output(self):
        return self._output

    def is_active(self):
        recording, playback = self._in_stream, self._out_stream
        return (recording is not None and playback is not None
                and recording.is_active() and playback.is_active())

    async def open(self):
        async with self._open_lock:
            if self._pa is None:
                self._loop = asyncio.get_running_loop()
                await asyncio.to_thread(self._open_streams)
                self._emit({"type": "audio_devices", "microphone": self.devices.microphone.name,
                            "speaker": self.devices.speaker.name, "sample_rate": RATE})

    def _open_streams(self):
        self.devices = read_devices()
        self._pa = pyaudio.PyAudio()
        microphone = int(self._pa.get_default_input_device_info()["index"])
        speaker = int(self._pa.get_default_output_device_info()["index"])
        self._in_stream = self._pa.open(format=pyaudio.paInt16, channels=1, rate=RATE, input=True,
                                       input_device_index=microphone, frames_per_buffer=BLOCK_SAMPLES,
                                       stream_callback=self._capture, start=False)
        self._out_stream = self._pa.open(format=pyaudio.paInt16, channels=1, rate=RATE, output=True,
                                        output_device_index=speaker, frames_per_buffer=BLOCK_SAMPLES,
                                        stream_callback=self._render, start=False)

    def _capture(self, audio, count, times, status):
        epoch = self.input_epoch
        try:
            if status:
                raise OSError(f"录音设备回调异常，状态码 {status}")
            clean = self._echo.capture(audio, times["input_buffer_adc_time"])
            self._loop.call_soon_threadsafe(self._levels, audio_level(clean), None)
            frame = InputAudioRawFrame(clean, RATE, 1)
            frame.metadata["voice_epoch"] = epoch
            asyncio.run_coroutine_threadsafe(self._input.push_audio_frame(frame), self._loop)
            return None, pyaudio.paContinue
        except (OSError, ValueError, RuntimeError) as exc:
            asyncio.run_coroutine_threadsafe(self._input.push_error(
                error_msg=f"录音设备不可用：{exc}", exception=exc, force_treat_as_permanent=True), self._loop)
            return None, pyaudio.paAbort

    def _render(self, audio, count, times, status):
        if status:
            error = OSError(f"播放设备回调异常，状态码 {status}")
            asyncio.run_coroutine_threadsafe(self._output.push_error(
                error_msg=str(error), exception=error, force_treat_as_permanent=True), self._loop)
            return bytes(count * 2), pyaudio.paAbort
        data = bytearray(count * 2)
        copied = 0
        completed = []
        with self._write_lock:
            while self._writes and copied < len(data) and not self._render_paused:
                pending = self._writes[0]
                length = min(len(data) - copied, len(pending.audio) - pending.offset)
                data[copied:copied + length] = pending.audio[pending.offset:pending.offset + length]
                pending.offset += length
                copied += length
                if pending.offset == len(pending.audio):
                    self._writes.popleft()
                    completed.append(pending.done)
        pcm = bytes(data)
        dac_time = times["output_buffer_dac_time"]
        self._echo.rendered(pcm, dac_time)
        delay = max(0, dac_time - times["current_time"])
        if copied:
            self._last_dac_end = time.perf_counter() + delay + count / RATE
        self._loop.call_soon_threadsafe(self._schedule_level, audio_level(pcm), delay)
        for future in completed:
            self._loop.call_soon_threadsafe(self._complete_write, future)
        return pcm, pyaudio.paContinue

    def _complete_write(self, future):
        if not future.done():
            future.set_result(True)

    def discard_pending(self):
        """只丢弃尚未交付设备的音频，已交付参考继续覆盖硬件缓冲及回声尾部。"""
        with self._write_lock:
            pending = list(self._writes)
            self._writes.clear()
        for write in pending:
            self._complete_write(write.done)

    def _schedule_level(self, level, delay):
        def deliver():
            self._level_handles.discard(handle)
            self._levels(None, level)
        handle = self._loop.call_later(delay + time.get_clock_info("monotonic").resolution, deliver)
        self._level_handles.add(handle)

    def _levels(self, microphone, playback):
        if microphone is not None:
            self._mic_level = microphone
        if playback is not None:
            self._playback_level = playback
        now = self._loop.time()
        if now - self._level_time >= 0.05:
            self._level_time = now
            self._emit({"type": "audio_levels", "microphone": self._mic_level, "playback": self._playback_level})

    async def reopen(self):
        """原生 worker 保留，关闭所有旧流后重建 PortAudio 枚举、AEC 和默认设备。"""
        await self.close()
        self._echo = EchoAudio()
        self._last_dac_end = 0.0
        await self.open()
        await asyncio.to_thread(self._out_stream.start_stream)
        await asyncio.to_thread(self._in_stream.start_stream)

    def _close_streams(self):
        try:
            for stream in (self._in_stream, self._out_stream):
                if stream is not None:
                    stream.close()
        finally:
            self._in_stream = self._out_stream = None
            if self._pa is not None:
                self._pa.terminate()
                self._pa = None

    async def close(self):
        # 原生 pipeline 会并行 cleanup 输入和输出，设备释放与打开共用同一把锁。
        async with self._open_lock:
            if self._pa is None:
                return
            self._render_paused = True
            self.discard_pending()
            await asyncio.to_thread(self._close_streams)
            for handle in self._level_handles:
                handle.cancel()
            self._level_handles.clear()
            self._mic_level = self._playback_level = 0.0
            self._emit({"type": "audio_levels", "microphone": 0.0, "playback": 0.0})
            self._render_paused = False


class DesktopAudioInput(BaseInputTransport):
    def __init__(self, owner, params):
        super().__init__(params)
        self._owner = owner

    async def setup(self, setup):
        await super().setup(setup)
        await self._owner.open()

    async def start(self, frame):
        await super().start(frame)
        await asyncio.to_thread(self._owner._in_stream.start_stream)
        await self.set_transport_ready(frame)

    async def cleanup(self):
        await self._owner.close()
        await super().cleanup()


class DesktopAudioOutput(BaseOutputTransport):
    def __init__(self, owner, params):
        super().__init__(params)
        self._owner = owner

    async def setup(self, setup):
        await super().setup(setup)
        await self._owner.open()

    async def start(self, frame):
        await super().start(frame)
        await asyncio.to_thread(self._owner._out_stream.start_stream)
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame):
        done = asyncio.get_running_loop().create_future()
        with self._owner._write_lock:
            self._owner._writes.append(_Write(frame.audio, done))
        return await done

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        if isinstance(frame, BotStoppedSpeakingFrame):
            # 队列处理结束后，等待最后一次 DAC 交付的硬件尾部再发原生停止事件。
            remaining = self._owner._last_dac_end - time.perf_counter()
            if remaining > 0:
                # Windows asyncio 的计时分辨率可能提前唤醒一个 tick。
                await asyncio.sleep(remaining + time.get_clock_info("monotonic").resolution)
        await super().push_frame(frame, direction)

    async def process_frame(self, frame, direction):
        if isinstance(frame, InterruptionFrame):
            started = time.perf_counter()
            self._owner._render_paused = True
            self._owner.discard_pending()
            try:
                await super().process_frame(frame, direction)
            finally:
                self._owner.discard_pending()
                self._owner._render_paused = False
            self._owner._emit({"type": "audio_stop", "elapsed_ms": (time.perf_counter() - started) * 1000})
        else:
            await super().process_frame(frame, direction)

    async def cleanup(self):
        await self._owner.close()
        await super().cleanup()
