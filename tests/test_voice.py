import asyncio
import base64
import gzip
import json
import sqlite3
import struct
from types import SimpleNamespace

import aiohttp
from aiohttp import web
from loguru import logger
import pytest
import pytest_asyncio
from pipecat.frames.frames import StartFrame
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from voice_super_agent.runtime import Runtime
from voice_super_agent.voice import pipeline as voice_pipeline
from voice_super_agent.voice import volcengine


class MemoryInput(BaseInputTransport):
    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self.set_transport_ready(frame)


class MemoryOutput(BaseOutputTransport):
    def __init__(self, params):
        super().__init__(params)
        self.release = asyncio.Event()
        self.audio = bytearray()

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame):
        self.audio.extend(frame.audio)
        await self.release.wait()
        await asyncio.sleep(len(frame.audio) / (2 * self.sample_rate))
        return True


class VoiceHarness:
    def __init__(self):
        self.requests = []
        self.asr = None
        self.end_time = 0
        self.decision = "backchannel"
        self.stt_packets = []
        self.closed_audio = False
        self.classifications = []
        self.release_model = asyncio.Event()

    async def recognize(self, request):
        self.asr = web.WebSocketResponse()
        await self.asr.prepare(request)
        async for message in self.asr:
            self.stt_packets.append(message.data)
        return self.asr

    async def transcript(self, text, final=True):
        self.end_time += 100
        body = gzip.compress(json.dumps({"result": {"utterances": [
            {"text": text, "definite": final, "end_time": self.end_time},
        ]}}).encode())
        await self.asr.send_bytes(b"\x11\x90\x11\x00" + struct.pack(">I", len(body)) + body)

    async def classify(self, request):
        self.classifications.append(await request.json())
        return web.json_response({"answers": {"interruption_intent": {"choice": self.decision}}})

    async def synthesize(self, request):
        self.tts_request = await request.json()
        events = [{"code": 0, "data": base64.b64encode(b"\x01\x00" * 4800).decode()},
                  {"code": 20000000}]
        return web.Response(body=b"".join(json.dumps(event).encode() + b"\n" for event in events))

    async def complete(self, request):
        payload = await request.json()
        self.requests.append(payload)
        if payload["messages"][-1]["content"] == "等待模型。":
            await self.release_model.wait()
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        try:
            await response.prepare(request)
            for text in ("测试", "回复。"):
                chunk = {"id": "voice-test", "object": "chat.completion.chunk", "created": 0,
                         "model": "test-model", "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
                await response.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            await response.write(b"data: [DONE]\n\n")
        except ConnectionResetError:
            pass
        return response

    async def wait(self, predicate):
        async with asyncio.timeout(8):
            while True:
                snapshot = self.runtime.connect("test")
                if predicate(snapshot):
                    return snapshot
                await asyncio.sleep(0.01)

    def release_audio(self):
        self.runtime._loop.call_soon_threadsafe(self.output.release.set)


@pytest_asyncio.fixture
async def voice(tmp_path, monkeypatch):
    logger.disable("pipecat")
    harness = VoiceHarness()
    app = web.Application()
    app.router.add_get("/asr", harness.recognize)
    app.router.add_post("/systemone", harness.classify)
    app.router.add_post("/tts", harness.synthesize)
    app.router.add_post("/chat/completions", harness.complete)
    server = web.AppRunner(app)
    await server.setup()
    site = web.TCPSite(server, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{server.addresses[0][1]}"
    original_session = aiohttp.ClientSession
    original_connect = volcengine.connect

    def session(**kwargs):
        client = original_session(**kwargs)
        post = client.post
        client.post = lambda endpoint, **options: post(
            url + ("/systemone" if endpoint.endswith("systemone") else "/tts"), **options)
        return client

    def audio(params):
        harness.input = MemoryInput(params)
        harness.output = MemoryOutput(params)
        return SimpleNamespace(input=lambda: harness.input, output=lambda: harness.output,
                               _pyaudio=SimpleNamespace(terminate=lambda: setattr(harness, "closed_audio", True)))

    monkeypatch.setattr(voice_pipeline, "LocalAudioTransport", audio)
    monkeypatch.setattr(voice_pipeline.aiohttp, "ClientSession", session)
    monkeypatch.setattr(volcengine, "connect", lambda endpoint, **kwargs: original_connect(
        url.replace("http://", "ws://") + "/asr", **kwargs))
    # 测试用确定的静音超时推进轮次，生产装配继续采用 Smart Turn。
    monkeypatch.setattr(voice_pipeline, "UserTurnStrategies", lambda start: UserTurnStrategies(
        start=start, stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.02)]))
    for key in ("DEEPSEEK_API_KEY", "VOLCENGINE_API_KEY", "ARK_API_KEY"):
        monkeypatch.setenv(key, "test-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", url)
    harness.runtime = Runtime(desktop_check=True, database_path=tmp_path / "voice.sqlite3")
    harness.runtime.start()
    harness.runtime.command("start_voice")
    try:
        await harness.wait(lambda s: s["state"]["voice_status"] in {"active", "error"})
        assert harness.runtime.connect("test")["state"]["voice_status"] == "active"
        yield harness
    finally:
        harness.release_model.set()
        await asyncio.to_thread(harness.runtime.stop)
        await server.cleanup()
        logger.enable("pipecat")


@pytest.mark.asyncio
async def test_voice_display_precedes_playback_commit_and_backchannel_is_only_display_history(voice):
    await voice.transcript("请介绍杭州。")
    snapshot = await voice.wait(lambda s: s["state"]["voice_activity"] == "speaking")
    assert snapshot["messages"][0]["source"] == "voice"
    assert snapshot["messages"][1]["text"] == "测试回复。"
    assert [r["message"]["role"] for r in voice.runtime._model_records] == ["user"]
    await voice.transcript("嗯嗯，你继续。")
    await voice.wait(lambda s: any(m["status"] == "backchannel" for m in s["messages"]))
    assert len(voice.requests) == 1
    voice.release_audio()
    snapshot = await voice.wait(lambda s: s["messages"][1]["status"] == "completed")
    records = voice.runtime._model_records
    assert [r["message"]["role"] for r in records] == ["user", "assistant"]
    assert all("嗯嗯" not in r["message"]["content"] for r in records)
    assert voice.tts_request["req_params"]["audio_params"]["sample_rate"] == 24000
    assert snapshot["messages"][1]["source"] == "voice"
    voice.runtime.command("stop_voice")
    await voice.wait(lambda s: s["state"]["voice_status"] == "off")
    assert voice.closed_audio


@pytest.mark.asyncio
async def test_confirmed_voice_interrupt_commits_old_reply_before_new_user(voice):
    await voice.transcript("第一个问题。")
    await voice.wait(lambda s: s["state"]["voice_activity"] == "speaking")
    voice.decision = "interrupt"
    await voice.transcript("等一下，换一个问题。")
    await voice.wait(lambda s: len(s["messages"]) >= 4 and s["messages"][1]["status"] == "interrupted")
    voice.release_audio()
    snapshot = await voice.wait(lambda s: s["messages"][-1]["status"] == "completed")
    assert snapshot["messages"][1]["text"] == "测试回复。"
    history = voice.requests[-1]["messages"]
    assert [m["content"] for m in history if m["role"] == "user"] == ["第一个问题。", "等一下，换一个问题。"]
    assert snapshot["messages"][-1]["reply_id"] != snapshot["messages"][1]["reply_id"]


@pytest.mark.asyncio
async def test_stopping_voice_and_text_continuation_restore_the_same_context(voice):
    await voice.transcript("请记住语音问题。")
    await voice.wait(lambda s: s["state"]["voice_activity"] == "speaking")
    with pytest.raises(ValueError, match="停止语音"):
        voice.runtime.send_text("联调期间的文字")
    voice.release_audio()
    await voice.wait(lambda s: s["messages"][-1]["status"] == "completed")
    voice.runtime.command("stop_voice")
    await voice.wait(lambda s: s["state"]["voice_status"] == "off")
    voice.runtime.send_text("继续追问。")
    await voice.wait(lambda s: s["messages"][-1]["status"] == "completed")
    assert [m["content"] for m in voice.requests[-1]["messages"] if m["role"] == "user"] == ["请记住语音问题。", "继续追问。"]


@pytest.mark.asyncio
async def test_unfinished_voice_and_backchannel_survive_restart(voice):
    await voice.transcript("继续讲。")
    await voice.wait(lambda s: s["state"]["voice_activity"] == "speaking")
    await voice.transcript("我在听。")
    await voice.wait(lambda s: any(m["status"] == "backchannel" for m in s["messages"]))
    path = voice.runtime._store._path
    assert await asyncio.to_thread(voice.runtime.prepare_exit)
    restored = Runtime(database_path=path)
    restored.start()
    try:
        snapshot = restored.connect("test")
        assert snapshot["messages"][1]["status"] == "unfinished"
        assert snapshot["messages"][1]["text"] == "测试回复。"
        assert snapshot["messages"][-1]["status"] == "backchannel"
        assert restored._user_ids[snapshot["messages"][1]["reply_id"]] == snapshot["messages"][0]["id"]
        assert all(r["message"]["content"] != "我在听。" for r in restored._model_records)
        assert snapshot["state"]["voice_status"] == "off"
    finally:
        restored.stop()


@pytest.mark.asyncio
async def test_multiple_final_asr_segments_form_one_native_user_turn(voice):
    await voice.transcript("先说第一句。")
    await voice.transcript("再补第二句。")
    voice.release_audio()
    await voice.wait(lambda s: s["messages"][-1]["status"] == "completed")
    assert len(voice.requests) == 1
    user = [m for m in voice.requests[0]["messages"] if m["role"] == "user"]
    assert len(user) == 1
    assert "第一句" in user[0]["content"] and "第二句" in user[0]["content"]


@pytest.mark.asyncio
async def test_speech_during_model_generation_bypasses_overlap_classifier(voice):
    await voice.transcript("等待模型。")
    await voice.wait(lambda s: bool(voice.requests))
    await voice.transcript("我有新问题。")
    await voice.wait(lambda s: len(voice.requests) == 2)
    voice.release_model.set()
    voice.release_audio()
    snapshot = await voice.wait(lambda s: s["messages"][-1]["status"] == "completed")
    assert voice.classifications == []
    assert snapshot["messages"][1]["status"] == "interrupted"
    assert [m["content"] for m in voice.requests[-1]["messages"] if m["role"] == "user"] == ["等待模型。", "我有新问题。"]


@pytest.mark.asyncio
async def test_audio_start_failure_is_reported_and_text_can_continue(voice, monkeypatch):
    voice.runtime.command("stop_voice")
    await voice.wait(lambda s: s["state"]["voice_status"] == "off")

    async def fail_setup(self, setup):
        await BaseInputTransport.setup(self, setup)
        raise OSError("测试设备不可用")

    monkeypatch.setattr(MemoryInput, "setup", fail_setup)
    voice.runtime.command("start_voice")
    snapshot = await voice.wait(lambda s: s["state"]["voice_status"] == "error")
    assert "设备不可用" in snapshot["state"]["voice_error"]
    voice.runtime.send_text("文字仍可继续。")
    snapshot = await voice.wait(lambda s: s["messages"][-1]["status"] == "completed")
    assert snapshot["state"]["model_available"]
    assert voice.closed_audio


@pytest.mark.asyncio
async def test_storage_failure_stops_voice_input_and_preserves_unsaved_content(voice):
    original_save = voice.runtime._store.save

    async def fail_save(*args):
        raise sqlite3.OperationalError("测试磁盘写入失败")

    voice.runtime._store.save = fail_save
    try:
        await voice.transcript("保存出错前的输入。")
        snapshot = await voice.wait(lambda s: s["state"]["storage_status"] == "error" and s["state"]["voice_status"] == "off")
        assert "磁盘写入失败" in snapshot["state"]["storage_error"]
        assert snapshot["messages"][0]["text"] == "保存出错前的输入。"
        assert voice.closed_audio
    finally:
        voice.runtime._store.save = original_save


@pytest.mark.asyncio
async def test_interim_only_takeover_keeps_recovery_instruction_out_of_user_display(voice):
    await voice.transcript("请继续讲。")
    await voice.wait(lambda s: s["state"]["voice_activity"] == "speaking")
    voice.decision = "interrupt"
    await voice.transcript("等一下", final=False)
    await voice.wait(lambda s: len(s["messages"]) == 4 and s["messages"][1]["status"] == "interrupted")
    voice.release_audio()
    snapshot = await voice.wait(lambda s: len(s["messages"]) == 4 and s["messages"][-1]["status"] == "completed")
    assert snapshot["messages"][2]["text"] == "等一下"
    assert snapshot["messages"][2]["status"] == "unfinished"
    assert all("The user may have" not in m["text"] for m in snapshot["messages"])
    assert any(r["message"]["role"] == "developer" for r in voice.runtime._model_records)


@pytest.mark.asyncio
async def test_starting_voice_requires_previous_conversation_save_to_succeed(voice):
    voice.runtime.command("stop_voice")
    await voice.wait(lambda s: s["state"]["voice_status"] == "off")
    original_save = voice.runtime._store.save

    async def fail_save(*args):
        raise sqlite3.OperationalError("准备期间保存失败")

    voice.runtime._store.save = fail_save
    try:
        voice.runtime.command("start_voice")
        snapshot = await voice.wait(lambda s: s["state"]["voice_status"] == "error")
        assert snapshot["state"]["storage_status"] == "error"
        assert "准备期间保存失败" in snapshot["state"]["voice_error"]
        assert voice.runtime._conversation is None
    finally:
        voice.runtime._store.save = original_save
