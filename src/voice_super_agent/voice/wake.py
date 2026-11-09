import asyncio
import ctypes
from dataclasses import replace
from pathlib import Path
import re
from tempfile import TemporaryDirectory

import numpy as np
import onnxruntime
from pypinyin import pinyin
from pypinyin.contrib.tone_convert import to_finals_tone, to_initials
# 显式加载 uv 环境中的 DLL，避免 Windows 系统目录里的旧版本抢先被 Sherpa 加载。
_ort_dll = ctypes.WinDLL(str(Path(onnxruntime.__file__).parent / "capi" / "onnxruntime.dll"))
import sherpa_onnx

from pipecat.frames.frames import InputAudioRawFrame, TranscriptionFrame, InterimTranscriptionFrame
from pipecat.processors.frame_processor import FrameProcessor


MODEL_NAME = "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"


class KeywordDetector:
    """按官方 ppinyin 词条格式，在本地 CPU 上检测一个中文唤醒短语。"""

    def __init__(self, model_dir: Path, word: str):
        tokens = model_dir / "tokens.txt"
        if not tokens.is_file():
            raise FileNotFoundError("缺少本地唤醒模型，请运行 ./scripts/prepare-wake-model.ps1")
        self._tokens = {line.split()[0] for line in tokens.read_text(encoding="utf-8").splitlines()}
        keyword = self._encode(word)
        suffix = "epoch-12-avg-2-chunk-16-left-64"
        for filename in (f"encoder-{suffix}.int8.onnx", f"decoder-{suffix}.onnx", f"joiner-{suffix}.int8.onnx"):
            if not (model_dir / filename).is_file():
                raise FileNotFoundError("本地唤醒模型文件缺失，请重新运行 ./scripts/prepare-wake-model.ps1")
        with TemporaryDirectory() as directory:
            keywords = Path(directory) / "keywords.txt"
            keywords.write_text(keyword + "\n", encoding="utf-8")
            self._spotter = sherpa_onnx.KeywordSpotter(
                tokens=str(tokens), encoder=str(model_dir / f"encoder-{suffix}.int8.onnx"),
                decoder=str(model_dir / f"decoder-{suffix}.onnx"),
                joiner=str(model_dir / f"joiner-{suffix}.int8.onnx"),
                keywords_file=str(keywords), num_threads=1, provider="cpu",
            )
        self._stream = self._spotter.create_stream()

    def _encode(self, word: str) -> str:
        if re.fullmatch(r"[\u4e00-\u9fff]+", word) is None:
            raise ValueError("唤醒词需要是一个中文短语。")
        phones = []
        for syllable in pinyin(word):
            phones.extend(part for part in (to_initials(syllable[0], strict=False),
                                             to_finals_tone(syllable[0], strict=False)) if part)
        unsupported = set(phones) - self._tokens
        if unsupported:
            raise ValueError("唤醒模型不支持该词条的发音：" + "、".join(sorted(unsupported)))
        return " ".join(phones) + f" @{word}"

    def reset(self) -> None:
        self._spotter.reset_stream(self._stream)

    def feed(self, audio: bytes, sample_rate: int) -> bool:
        samples = np.frombuffer(audio, dtype="<i2").astype(np.float32) / 32768
        self._stream.accept_waveform(sample_rate, samples)
        while self._spotter.is_ready(self._stream):
            self._spotter.decode_stream(self._stream)
            if self._spotter.get_result(self._stream):
                self.reset()
                return True
        return False


class WakeAudioGate(FrameProcessor):
    """待唤醒时原始 PCM 仅进入本地 KWS，后续轮次处理器收到静音以结束旧 VAD 状态。"""

    def __init__(self, emit, boundary):
        super().__init__()
        self.detector: KeywordDetector | None = None
        self.mode = "paused"
        self._emit = emit
        self._decode_lock = asyncio.Lock()
        self.boundary = boundary

    async def reset_detector(self):
        if self.detector is not None:
            async with self._decode_lock:
                await asyncio.to_thread(self.detector.reset)

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            if frame.metadata["voice_epoch"] != self.boundary.epoch:
                return
            frame.metadata["voice_admitted"] = self.mode == "active"
        if isinstance(frame, InputAudioRawFrame) and self.mode != "active":
            if self.mode == "standby" and self.detector is not None:
                async with self._decode_lock:
                    detected = await asyncio.to_thread(self.detector.feed, frame.audio, frame.sample_rate)
                if detected and self.mode == "standby":
                    self.mode = "paused"
                    self._emit({"type": "wake"})
            frame = replace(frame, audio=bytes(len(frame.audio)))
        await self.push_frame(frame, direction)


class RecognitionGate(FrameProcessor):
    """语音准备与结束期间，识别结果随音频入口一同关闭。"""

    def __init__(self, audio_gate):
        super().__init__()
        self._audio_gate = audio_gate

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
            if (self._audio_gate.mode != "active" or not self._audio_gate.boundary.accepts(frame)
                    or self._audio_gate.boundary.wait_for_quiet):
                return
        await self.push_frame(frame, direction)
