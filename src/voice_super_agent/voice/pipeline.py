import os
import asyncio

import numpy as np

import aiohttp
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregatorParams
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
from pipecat.turns.user_start import TranscriptionUserTurnStartStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from voice_super_agent.voice.interruption import SystemOneInterruptionProcessor
from voice_super_agent.voice.volcengine import VolcengineSTTService, VolcengineTTSService
from voice_super_agent.voice.wake import WakeAudioGate, RecognitionGate
from pipecat.frames.frames import TTSStartedFrame, TTSStoppedFrame, TTSAudioRawFrame


class VoicePipeline:
    """持有一轮语音执行的本地设备与 HTTP 会话，帧处理保持 STS 原有顺序。"""

    def __init__(self, context, emit):
        self.gate = WakeAudioGate(emit)
        self.recognition_gate = RecognitionGate(self.gate)
        self._tone_done: asyncio.Event | None = None
        self.user_params = LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            user_turn_strategies=UserTurnStrategies(start=[TranscriptionUserTurnStartStrategy()]),
        )
        self.transport = LocalAudioTransport(LocalAudioTransportParams(
            audio_in_enabled=True, audio_out_enabled=True,
            audio_in_sample_rate=16000, audio_out_sample_rate=24000,
            audio_in_channels=1, audio_out_channels=1,
        ))
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        self.stt = VolcengineSTTService(api_key=os.environ["VOLCENGINE_API_KEY"], defer_connection=True)
        self.interruption = SystemOneInterruptionProcessor(
            api_key=os.environ["ARK_API_KEY"], session=self._session, context=context,
        )
        self.tts = VolcengineTTSService(api_key=os.environ["VOLCENGINE_API_KEY"], session=self._session)

    def processors(self, user, llm, assistant):
        return [self.transport.input(), self.gate, self.stt, self.recognition_gate, self.interruption, user,
                llm, self.tts, self.transport.output(), assistant]

    def playback_stopped(self):
        if self._tone_done is not None:
            self._tone_done.set()

    async def play_ready_tone(self):
        """叮声与助手共用可取消的播放队列，等待实际播放结束后才开放输入。"""
        samples = np.arange(4800, dtype=np.float32) / 24000
        wave = (np.sin(2 * np.pi * 1800 * samples) + 0.35 * np.sin(2 * np.pi * 3600 * samples))
        wave *= np.exp(-samples * 22) * np.minimum(samples * 200, 1) * 10000
        self._tone_done = asyncio.Event()
        output = self.transport.output()
        try:
            await output.queue_frame(TTSStartedFrame(context_id="wake-tone", append_to_context=False))
            await output.queue_frame(TTSAudioRawFrame(wave.astype("<i2").tobytes(), 24000, 1, context_id="wake-tone"))
            await output.queue_frame(TTSStoppedFrame(context_id="wake-tone"))
            await self._tone_done.wait()
        finally:
            self._tone_done = None

    async def close(self):
        await self._session.close()
        # worker 已释放输入与输出流，最后结束 PortAudio 的设备资源。
        self.transport._pyaudio.terminate()
