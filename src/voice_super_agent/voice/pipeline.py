import os

import aiohttp
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregatorParams
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
from pipecat.turns.user_start import TranscriptionUserTurnStartStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from voice_super_agent.voice.interruption import SystemOneInterruptionProcessor
from voice_super_agent.voice.volcengine import VolcengineSTTService, VolcengineTTSService


class VoicePipeline:
    """持有一轮语音执行的本地设备与 HTTP 会话，帧处理保持 STS 原有顺序。"""

    def __init__(self, context):
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
        self.stt = VolcengineSTTService(api_key=os.environ["VOLCENGINE_API_KEY"])
        self.interruption = SystemOneInterruptionProcessor(
            api_key=os.environ["ARK_API_KEY"], session=self._session, context=context,
        )
        self.tts = VolcengineTTSService(api_key=os.environ["VOLCENGINE_API_KEY"], session=self._session)

    def processors(self, user, llm, assistant):
        return [self.transport.input(), self.stt, self.interruption, user,
                llm, self.tts, self.transport.output(), assistant]

    async def close(self):
        await self._session.close()
        # worker 已释放输入与输出流，最后结束 PortAudio 的设备资源。
        self.transport._pyaudio.terminate()
