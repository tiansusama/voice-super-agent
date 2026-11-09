"""Adapt Volcengine speech protocols to Pipecat STT and TTS frames."""

# 来源：STS 的 local_volcengine_services.py，基于 Pipecat de8998789 迁入后独立维护。

import asyncio
import base64
import gzip
import json
import struct
from collections.abc import AsyncGenerator
from uuid import uuid4

import aiohttp
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameProcessorSetup
from pipecat.services.settings import STTSettings, TTSSettings
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601


class VolcengineSTTService(STTService):
    """Publish Volcengine streaming transcripts as Pipecat frames."""

    def __init__(self, *, api_key: str, defer_connection: bool = False):
        """Initialize the ASR adapter.

        Args:
            api_key: Volcengine speech API key.
        """
        super().__init__(
            sample_rate=16000,
            settings=STTSettings(model="bigmodel", language=Language.ZH),
        )
        self._api_key = api_key
        self._defer_connection = defer_connection
        self._register_event_handler("on_recognition", sync=True)
        self._socket: ClientConnection | None = None
        self._receive_task = None
        self._sequence = 1
        self._audio = bytearray()
        self._final_end_ms = -1
        self._interim_text = ""
        self._ready = asyncio.Event()
        self.input_epoch = 0
        self.send_paused = False
        self._opening = False

    async def setup(self, setup: FrameProcessorSetup):
        """准备处理器；常驻语音宿主在唤醒时建立云端连接。"""
        await super().setup(setup)
        if not self._defer_connection:
            await self.connect()

    async def connect(self, *, keep_buffer=False):
        self._opening = True
        self._ready.clear()
        self._sequence = 1
        if not keep_buffer:
            self._audio.clear()
        self._final_end_ms = -1
        self._interim_text = ""
        self._socket = await connect(
            "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async",
            additional_headers={
                "X-Api-Key": self._api_key,
                "X-Api-Resource-Id": "volc.seedasr.sauc.duration",
                "X-Api-Connect-Id": str(uuid4()),
            },
            open_timeout=10,
            close_timeout=1,
        )
        await self._send_packet(
            1,
            json.dumps(
                {
                    "user": {"uid": "pipecat-local"},
                    "audio": {
                        "format": "pcm", "codec": "raw", "rate": self.sample_rate,
                        "bits": 16, "channel": 1,
                    },
                    "request": {
                        "model_name": "bigmodel",
                        "enable_itn": True,
                        "enable_punc": True,
                        "enable_nonstream": True,
                        "show_utterances": True,
                        "result_type": "full",
                    },
                }
            ).encode(),
        )
        self._receive_task = self.create_task(self._receive_results(self._socket, self.input_epoch), name="volcengine-stt")
        ready = asyncio.create_task(self._ready.wait())
        try:
            async with asyncio.timeout(10):
                done, _ = await asyncio.wait((ready, self._receive_task), return_when=asyncio.FIRST_COMPLETED)
                if self._receive_task in done:
                    await self._receive_task
                    raise RuntimeError("云端 ASR 在确认就绪前结束连接。")
        finally:
            ready.cancel()
            await asyncio.gather(ready, return_exceptions=True)
        self._opening = False
        await self._flush_audio()

    async def _send_packet(self, message_type: int, payload: bytes):
        compressed = gzip.compress(payload)
        serialization = 1 if message_type == 1 else 0
        header = bytes((0x11, (message_type << 4) | 1, (serialization << 4) | 1, 0))
        await self._socket.send(
            header + struct.pack(">iI", self._sequence, len(compressed)) + compressed
        )
        self._sequence += 1

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        """Send PCM while the receive task publishes transcripts."""
        if self.send_paused or (self._socket is None and not self._opening):
            yield None
            return
        self._audio.extend(audio)
        if not self._opening:
            await self._flush_audio()
        yield None

    async def process_audio_frame(self, frame, direction):
        if (frame.metadata["voice_epoch"] == self.input_epoch
                and (not self._opening or frame.metadata["voice_admitted"])):
            await super().process_audio_frame(frame, direction)

    async def _flush_audio(self):
        # 火山建议每包发送 200 毫秒，Pipecat 负责输入音频重采样。
        while len(self._audio) >= 6400:
            await self._send_packet(2, bytes(self._audio[:6400]))
            del self._audio[:6400]

    async def _receive_results(self, socket, epoch):
        try:
            async for packet in socket:
                if epoch != self.input_epoch:
                    continue
                offset = (packet[0] & 0x0F) * 4
                message_type = packet[1] >> 4
                flags = packet[1] & 0x0F
                if message_type == 0x0F:
                    code = struct.unpack_from(">I", packet, offset)[0]
                    raise RuntimeError(f"Volcengine ASR returned error code {code}")
                if message_type != 9:
                    raise RuntimeError(f"Unexpected Volcengine ASR message type {message_type}")
                if flags & 1:
                    offset += 4
                payload_size = struct.unpack_from(">I", packet, offset)[0]
                payload = packet[offset + 4 : offset + 4 + payload_size]
                if packet[2] & 0x0F == 1:
                    payload = gzip.decompress(payload)
                response = json.loads(payload)
                if response.get("code", 0) not in (0, 20000000):
                    raise RuntimeError(f"Volcengine ASR returned error code {response['code']}")
                if epoch == self.input_epoch:
                    self._ready.set()
                result = response.get("result", {})
                interim = []
                # definite 只表示识别分句定稿；用户轮次由 Pipecat 的策略决定。
                for utterance in result.get("utterances", []):
                    text = utterance["text"].strip()
                    if utterance.get("definite", False):
                        if utterance["end_time"] > self._final_end_ms:
                            self._final_end_ms = utterance["end_time"]
                            if text:
                                frame = TranscriptionFrame(text, self._user_id, time_now_iso8601(), Language.ZH)
                                frame.metadata["voice_epoch"] = epoch
                                await self.push_frame(frame)
                    else:
                        interim.append(text)
                text = "".join(interim)
                if text != self._interim_text:
                    self._interim_text = text
                    frame = InterimTranscriptionFrame(text, self._user_id, time_now_iso8601(), Language.ZH)
                    frame.metadata["voice_epoch"] = epoch
                    await self.push_frame(frame)
                if epoch == self.input_epoch:
                    await self._call_event_handler("on_recognition", bool(text))
            if epoch == self.input_epoch:
                raise RuntimeError("Volcengine ASR connection ended")
        except (WebSocketException, OSError, RuntimeError, json.JSONDecodeError) as exc:
            if epoch != self.input_epoch:
                return
            await self.push_error(
                error_msg=str(exc), exception=exc, force_treat_as_permanent=True
            )

    async def disconnect(self):
        self._opening = False
        self._ready.clear()
        self._audio.clear()
        if self._receive_task is not None:
            await self.cancel_task(self._receive_task)
            self._receive_task = None
        if self._socket is not None:
            await self._socket.close()
            self._socket = None

    async def stop(self, frame: EndFrame):
        """Close ASR when the pipeline ends."""
        await super().stop(frame)
        await self.disconnect()

    async def cancel(self, frame: CancelFrame):
        """Close ASR when the pipeline is cancelled."""
        await super().cancel(frame)
        await self.disconnect()

    async def cleanup(self):
        """Release the provider connection during processor cleanup."""
        await self.disconnect()
        await super().cleanup()


class VolcengineTTSService(TTSService):
    """Stream Volcengine TTS 2.0 audio through Pipecat audio contexts."""

    def __init__(self, *, api_key: str, session: aiohttp.ClientSession):
        """Initialize the TTS adapter.

        Args:
            api_key: Volcengine speech API key.
            session: HTTP session owned by the bot.
        """
        super().__init__(
            sample_rate=24000,
            push_start_frame=True,
            push_stop_frames=True,
            settings=TTSSettings(
                model="seed-tts-2.0",
                voice="zh_female_shuangkuaisisi_uranus_bigtts",
                language=Language.ZH,
            ),
        )
        self._api_key = api_key
        self._session = session

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame | None, None]:
        """Yield streamed PCM for the audio context supplied by Pipecat."""
        headers = {
            "X-Api-Key": self._api_key,
            "X-Api-Resource-Id": self._settings.model,
            "X-Api-Request-Id": str(uuid4()),
        }
        payload = {
            "user": {"uid": "pipecat-local"},
            "req_params": {
                "text": text,
                "speaker": self._settings.voice,
                "audio_params": {"format": "pcm", "sample_rate": self.sample_rate},
            },
        }
        await self.start_ttfb_metrics()
        await self.start_tts_usage_metrics(text)
        try:
            async with self._session.post(
                "https://openspeech.bytedance.com/api/v3/tts/unidirectional",
                headers=headers,
                json=payload,
            ) as response:
                response.raise_for_status()
                # aiohttp 按行组装 HTTP 分块，逐条转换为原生 TTS 音频帧。
                async for line in response.content:
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    code = event["code"]
                    if code == 20000000:
                        return
                    if code != 0:
                        raise RuntimeError(f"Volcengine TTS returned error code {code}")
                    if event.get("data"):
                        await self.stop_ttfb_metrics()
                        yield TTSAudioRawFrame(
                            base64.b64decode(event["data"]), self.sample_rate, 1,
                            context_id=context_id,
                        )
                raise RuntimeError("Volcengine TTS stream ended before completion")
        except (aiohttp.ClientError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
            await self.push_error(
                error_msg=str(exc), exception=exc, force_treat_as_permanent=True
            )
        finally:
            await self.stop_ttfb_metrics()
