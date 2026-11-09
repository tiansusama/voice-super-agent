import asyncio
import base64
import gzip
import json
import sqlite3
import struct
from types import SimpleNamespace
from websockets.exceptions import InvalidHandshake

import aiohttp
from aiohttp import web
from loguru import logger
import pytest
import pytest_asyncio
from pipecat.frames.frames import StartFrame, InputAudioRawFrame, VADUserStartedSpeakingFrame
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from voice_super_agent.runtime import Runtime
from voice_super_agent import runtime as runtime_module
from voice_super_agent.desktop.hotkey import HoldHotkey
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
        self.release.set()
        self.audio = bytearray()

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame):
        self.audio.extend(frame.audio)
        # 模拟有界设备写入，打断后正在写的一个音频块仍会返回。
        try:
            await asyncio.wait_for(self.release.wait(), timeout=0.25)
        except TimeoutError:
            pass
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
        self.asr_connections = 0
        self.asr_ready = asyncio.Event()
        self.asr_ready.set()
        self.ending = False
        self.silent_ending = False

    async def recognize(self, request):
        self.asr = web.WebSocketResponse()
        await self.asr.prepare(request)
        self.asr_connections += 1
        await self.asr_ready.wait()
        payload = gzip.compress(json.dumps({"code": 0}).encode())
        await self.asr.send_bytes(b"\x11\x90\x11\x00" + struct.pack(">I", len(payload)) + payload)
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
        self.ending = payload["messages"][-1]["role"] == "tool"
        if payload["messages"][-1]["content"] == "等待模型。":
            await self.release_model.wait()
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        try:
            await response.prepare(request)
            if payload["messages"][-1]["content"] == "结束语音。":
                chunk = {"id": "voice-test", "object": "chat.completion.chunk", "created": 0,
                         "model": "test-model", "choices": [{"index": 0, "delta": {"tool_calls": [
                             {"index": 0, "id": "end-test", "type": "function",
                              "function": {"name": "end_voice", "arguments": "{}"}},
                         ]}, "finish_reason": "tool_calls"}]}
                await response.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            for text in (() if payload["messages"][-1]["content"] == "结束语音。" or (self.ending and self.silent_ending)
                         else ("测试", "回复。")):
                chunk = {"id": "voice-test", "object": "chat.completion.chunk", "created": 0,
                         "model": "test-model", "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
                await response.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            await response.write(b"data: [DONE]\n\n")
        except ConnectionResetError:
            pass
        return response

    async def wait(self, predicate):
        try:
            async with asyncio.timeout(8):
                while True:
                    snapshot = self.runtime.connect("test")
                    if predicate(snapshot):
                        return snapshot
                    await asyncio.sleep(0.01)
        except TimeoutError:
            raise AssertionError(self.runtime.connect("test")) from None

    def release_audio(self):
        self.runtime._loop.call_soon_threadsafe(self.output.release.set)

    async def backend(self, coroutine):
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coroutine, self.runtime._loop))

    async def wake(self):
        self.release_audio()
        self.runtime.command("wake_voice")
        await self.wait(lambda s: s["state"]["voice_status"] == "active")
        self.output.audio.clear()
        self.runtime._loop.call_soon_threadsafe(self.output.release.clear)


@pytest_asyncio.fixture
async def voice(tmp_path, monkeypatch, request):
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
    standby = getattr(request, "param", False)
    harness.detector = SimpleNamespace(reset=lambda: None, feed=lambda audio, rate: False)
    monkeypatch.setattr(runtime_module, "KeywordDetector", lambda directory, word: harness.detector)
    monkeypatch.setattr(HoldHotkey, "pressed", lambda self: False)
    harness.runtime = Runtime(desktop_check=True, database_path=tmp_path / "voice.sqlite3",
                              wake_model_dir=tmp_path if standby else None)
    harness.runtime.start()
    if not standby:
        harness.runtime.command("start_voice")
    try:
        expected = "standby" if standby else "active"
        await harness.wait(lambda s: s["state"]["voice_status"] in {expected, "error"})
        assert harness.runtime.connect("test")["state"]["voice_status"] == expected
        if not standby:
            harness.output.audio.clear()
            harness.runtime._loop.call_soon_threadsafe(harness.output.release.clear)
        yield harness
    finally:
        harness.release_model.set()
        harness.asr_ready.set()
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
    await voice.wait(lambda s: bool(s["messages"]) and s["messages"][-1]["status"] == "completed")
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


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_local_wake_waits_for_cloud_ready_and_ding_then_releases_input(voice):
    assert voice.asr_connections == 0
    assert not voice.output.audio
    assert voice.runtime._conversation._voice.stt._socket is None
    voice.asr_ready.clear()
    voice.detector.feed = lambda audio, rate: True
    await voice.backend(voice.input.push_audio_frame(InputAudioRawFrame(b"\x01\x00" * 320, 16000, 1)))
    await voice.wait(lambda s: voice.asr_connections == 1)
    assert voice.runtime.connect("test")["state"]["voice_status"] == "starting"
    assert not voice.output.audio
    voice.asr_ready.set()
    await voice.wait(lambda s: s["state"]["voice_status"] == "active")
    assert voice.output.audio
    assert not voice.runtime.connect("test")["messages"]
    voice.runtime.command("wake_voice")
    await voice.wait(lambda s: s["state"]["voice_status"] == "active")
    assert voice.asr_connections == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_end_during_preparation_returns_to_working_local_standby(voice):
    voice.asr_ready.clear()
    voice.runtime.command("wake_voice")
    await voice.wait(lambda s: voice.asr_connections == 1)
    voice.runtime.command("end_voice")
    await voice.wait(lambda s: s["state"]["voice_status"] == "standby")
    assert voice.runtime._conversation._voice.gate.mode == "standby"
    assert voice.runtime._conversation._voice.stt._socket is None
    voice.asr_ready.set()
    await voice.wake()
    assert voice.asr_connections == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_button_end_cancels_playback_and_rewake_keeps_context_and_worker(voice):
    await voice.wake()
    conversation = voice.runtime._conversation
    await voice.transcript("请记住语音问题。")
    await voice.wait(lambda s: s["state"]["voice_activity"] == "speaking")
    voice.runtime.command("end_voice")
    snapshot = await voice.wait(lambda s: s["state"]["voice_status"] == "standby")
    assert snapshot["messages"][1]["text"] == "测试回复。"
    assert voice.runtime._conversation is conversation
    assert not voice.closed_audio
    assert conversation._voice.stt._socket is None
    await voice.wake()
    await voice.transcript("继续追问。")
    voice.release_audio()
    await voice.wait(lambda s: s["messages"][-1]["status"] == "completed")
    assert [m["content"] for m in voice.requests[-1]["messages"] if m["role"] == "user"] == ["请记住语音问题。", "继续追问。"]


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
@pytest.mark.parametrize("silent", [False, True])
async def test_agent_end_waits_for_final_reply_and_real_playback(voice, silent):
    await voice.wake()
    voice.silent_ending = silent
    await voice.transcript("结束语音。")
    if not silent:
        await voice.wait(lambda s: s["state"]["voice_activity"] == "speaking" and voice.ending)
        assert voice.runtime.connect("test")["state"]["voice_status"] == "active"
        voice.release_audio()
    await voice.wait(lambda s: s["state"]["voice_status"] == "standby")
    assert voice.asr_connections == 1
    assert voice.runtime._conversation._voice.stt._socket is None
    assert any(m["role"] == "tool" for m in voice.requests[-1]["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_new_takeover_cancels_pending_agent_end(voice):
    await voice.wake()
    await voice.transcript("结束语音。")
    await voice.wait(lambda s: s["state"]["voice_activity"] == "speaking" and voice.ending)
    voice.decision = "interrupt"
    await voice.transcript("等一下，继续聊。")
    await voice.wait(lambda s: len(s["messages"]) >= 4)
    voice.release_audio()
    await voice.wait(lambda s: s["messages"][-1]["status"] == "completed")
    assert voice.runtime._lifecycle.ending_reply is None
    assert voice.runtime.connect("test")["state"]["voice_status"] == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_standby_text_stays_text_and_next_voice_reply_still_plays(voice):
    voice.runtime.send_text("先用文字交流。")
    await voice.wait(lambda s: s["messages"][-1]["status"] == "completed")
    assert not voice.output.audio
    assert voice.asr_connections == 0
    assert "tools" not in voice.requests[-1] or not voice.requests[-1]["tools"]
    await voice.wake()
    await voice.transcript("换成语音继续。")
    await voice.wait(lambda s: s["state"]["voice_activity"] == "speaking")
    assert voice.output.audio
    voice.release_audio()
    await voice.wait(lambda s: s["messages"][-1]["status"] == "completed")
    assert [m["content"] for m in voice.requests[-1]["messages"] if m["role"] == "user"] == ["先用文字交流。", "换成语音继续。"]


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_runtime_idle_timeout_closes_asr_without_closing_the_local_worker(voice):
    await voice.wake()
    async def expire_wait():
        voice.runtime._lifecycle.deadline = voice.runtime._loop.time() - 1
    await voice.backend(expire_wait())
    await voice.wait(lambda s: s["state"]["voice_status"] == "standby")
    assert not voice.closed_audio
    assert voice.runtime._conversation._voice.stt._socket is None


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_end_during_unfinished_speech_resets_native_turn_without_recovery_reply(voice):
    await voice.wake()
    conversation = voice.runtime._conversation
    await voice.backend(conversation._user.queue_frame(VADUserStartedSpeakingFrame()))
    await voice.transcript("还没说完", final=False)
    await voice.wait(lambda s: bool(s["messages"]))
    voice.runtime.command("end_voice")
    snapshot = await voice.wait(lambda s: s["state"]["voice_status"] == "standby")
    assert voice.requests == []
    assert snapshot["messages"][0]["text"] == "还没说完"
    assert snapshot["messages"][0]["status"] == "unfinished"
    assert not conversation._user._user_turn_controller._user_turn
    await voice.wake()
    await voice.transcript("重新说一个完整问题。")
    voice.release_audio()
    await voice.wait(lambda s: s["messages"][-1]["status"] == "completed")
    assert len(voice.requests) == 1
    assert voice.requests[0]["messages"][-1]["content"] == "重新说一个完整问题。"


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_end_during_generation_cancels_request_and_resumes_local_standby(voice):
    await voice.wake()
    await voice.transcript("等待模型。")
    await voice.wait(lambda s: bool(voice.requests))
    voice.runtime.command("end_voice")
    await voice.wait(lambda s: s["state"]["voice_status"] == "standby")
    assert len(voice.requests) == 1
    assert not voice.runtime._lifecycle.busy
    voice.release_model.set()
    await voice.wake()
    await voice.transcript("改问新问题。")
    voice.release_audio()
    await voice.wait(lambda s: s["messages"][-1]["status"] == "completed")
    assert len(voice.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_asr_handshake_failure_is_reported_and_text_remains_available(voice, monkeypatch):
    async def fail_connect(*args, **kwargs):
        raise InvalidHandshake("测试 ASR 鉴权失败")
    monkeypatch.setattr(volcengine, "connect", fail_connect)
    voice.runtime.command("wake_voice")
    snapshot = await voice.wait(lambda s: s["state"]["voice_status"] == "error")
    assert "ASR 鉴权失败" in snapshot["state"]["voice_error"]
    assert voice.closed_audio
    voice.runtime.send_text("继续文字交流。")
    await voice.wait(lambda s: s["messages"][-1]["status"] == "completed")
