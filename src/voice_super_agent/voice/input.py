from contextvars import ContextVar
from dataclasses import dataclass

from pipecat.audio.vad.vad_analyzer import VADState
from pipecat.frames.frames import InputAudioRawFrame, TranscriptionFrame, InterimTranscriptionFrame, UserBackchannelFrame, LLMContextFrame
from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregator
from pipecat.processors.frame_processor import FrameDirection
from pipecat.turns.user_stop import UserTurnStoppedParams


@dataclass
class VoiceInput:
    epoch: int = 0
    frozen: bool = False
    wait_for_quiet: bool = False
    quiet_samples: int = 0

    def advance(self):
        self.epoch += 1
        self.frozen = True
        self.quiet_samples = 0
        return self.epoch

    def accepts(self, frame):
        return frame.metadata["voice_epoch"] == self.epoch and not self.frozen


class VoiceUserAggregator(LLMUserAggregator):
    """保留原生聚合与轮次，文字接管期间冻结旧语音提交并继续观察真实 VAD。"""

    def __init__(self, context, *, params, boundary, on_quiet):
        self.boundary = boundary
        self.text_input = ContextVar("text_input", default=False)
        self._on_quiet = on_quiet
        super().__init__(context, params=params)

    async def process_frame(self, frame, direction):
        speech = isinstance(frame, (InputAudioRawFrame, TranscriptionFrame, InterimTranscriptionFrame, UserBackchannelFrame))
        if speech and frame.metadata["voice_epoch"] != self.boundary.epoch:
            return
        if self.boundary.frozen and speech:
            if isinstance(frame, InputAudioRawFrame):
                await self._vad_controller.process_frame(frame)
            return
        await super().process_frame(frame, direction)
        if (isinstance(frame, InputAudioRawFrame) and self.boundary.wait_for_quiet
                and frame.metadata["voice_admitted"]):
            analyzer = self._params.vad_analyzer
            if analyzer._vad_state == VADState.QUIET:
                self.boundary.quiet_samples += frame.num_frames
            else:
                self.boundary.quiet_samples = 0
            if self.boundary.quiet_samples >= frame.sample_rate * analyzer.params.stop_secs:
                self.boundary.wait_for_quiet = False
                self._on_quiet()

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        if isinstance(frame, LLMContextFrame):
            text = self.text_input.get()
            if self.boundary.frozen and not text:
                return
            frame.metadata.update(input_source="text" if text else "voice", voice_epoch=self.boundary.epoch)
        await super().push_frame(frame, direction)

    async def _on_user_turn_inference_triggered(self, *args):
        if not self.boundary.frozen:
            await super()._on_user_turn_inference_triggered(*args)

    async def _on_user_turn_stopped(self, *args):
        if not self.boundary.frozen:
            await super()._on_user_turn_stopped(*args)

    async def _on_vad_speech_started(self, *args):
        if not self.boundary.frozen:
            await super()._on_vad_speech_started(*args)

    async def _on_vad_speech_stopped(self, *args):
        if self.boundary.wait_for_quiet:
            self.boundary.wait_for_quiet = False
            self._on_quiet()
        if not self.boundary.frozen:
            await super()._on_vad_speech_stopped(*args)

    async def _on_vad_speech_activity(self, *args):
        if not self.boundary.frozen:
            await super()._on_vad_speech_activity(*args)

    async def _handle_llm_messages_append(self, frame):
        token = self.text_input.set(True)
        try:
            await super()._handle_llm_messages_append(frame)
        finally:
            self.text_input.reset(token)

    async def discard_voice(self):
        """仅清理未提交聚合，原生控制器负责结束旧轮次与释放策略中的缓存。"""
        await self.reset()
        self._full_user_turn_aggregation = None
        self._turn_perception = None
        self._user_turn_interrupted_bot = False
        self._user_turn_answer_voided = False
        controller = self._user_turn_controller
        was_open = controller._user_turn
        controller._user_speaking = False
        await controller._trigger_user_turn_stop(None, UserTurnStoppedParams(enable_user_speaking_frames=True))
        if not was_open:
            for strategy in (*controller.user_turn_strategies.start, *controller._user_turn_stop_strategies):
                await strategy.handle_user_turn_stopped()
