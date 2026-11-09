import asyncio
import struct

import pytest

from pipecat.transports.base_input import BaseInputTransport
from test_voice import MemoryInput, voice
from voice_super_agent.audio.devices import AudioDevices, Endpoint
from voice_super_agent.runtime import Runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
@pytest.mark.parametrize("service", ["asr", "system_one", "tts"])
async def test_voice_service_failure_preserves_text_and_manual_recovery_waits_for_wake(voice, service):
    await voice.wake()
    voice.runtime.update_windows(main_window="hidden", floating_visible=False)
    if service == "tts":
        voice.failures["tts"] = 503
        await voice.transcript("请回答这个问题。")
    else:
        await voice.transcript("请回答这个问题。")
        await voice.wait(lambda s: s["state"]["voice_activity"] == "speaking")
        if service == "asr":
            await voice.asr.send_bytes(b"\x11\xf0\x00\x00" + struct.pack(">I", 45000001))
        else:
            voice.failures["system_one"] = 503
            await voice.transcript("等一下。")
    snapshot = await voice.wait(lambda s: s["state"]["voice_status"] == "error")
    assert snapshot["state"]["model_available"]
    assert snapshot["state"]["last_error"]["service"] == service
    assert snapshot["state"]["main_window"] == "hidden"
    assert not snapshot["state"]["floating_visible"]
    assert snapshot["messages"][1]["text"] == "测试回复。"
    assert snapshot["messages"][1]["status"] == "failed"
    original = snapshot["messages"][1].copy()
    voice.failures.clear()
    voice.release_audio()
    receipt = voice.runtime.send_text("现在继续文字追问。")
    await voice.wait(lambda s: s["messages"][-1]["id"] == receipt["reply_id"] and s["messages"][-1]["status"] == "completed")
    count = len(voice.requests)
    connections = voice.asr_connections
    tts_count = len(voice.tts_requests)
    voice.devices = AudioDevices(Endpoint("mic-2", "新默认麦克风"), Endpoint("speaker-2", "新默认播放设备"),
                                frozenset({"mic-2", "speaker-2"}))
    voice.runtime.command("recover_voice")
    snapshot = await voice.wait(lambda s: s["state"]["voice_status"] == "standby")
    assert snapshot["state"]["voice_recovery"] == "succeeded"
    assert not snapshot["state"]["voice_error"]
    assert voice.asr_connections == connections
    assert len(voice.requests) == count
    assert len(voice.tts_requests) == tts_count
    assert snapshot["messages"][1] == original
    assert voice.runtime._conversation._voice.transport.devices.defaults == voice.devices.defaults
    await voice.wake()
    await voice.transcript("恢复后继续交流。")
    voice.release_audio()
    await voice.wait(lambda s: s["messages"][-1]["source"] == "voice" and s["messages"][-1]["status"] == "completed")
    assert voice.asr_connections == connections + 1
    users = [m["content"] for m in voice.requests[-1]["messages"] if m["role"] == "user"]
    assert users == ["请回答这个问题。", "现在继续文字追问。", "恢复后继续交流。"]


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_transient_model_failure_keeps_voice_usable_without_automatic_retry(voice):
    await voice.wake()
    voice.failures["model"] = 503
    await voice.transcript("这次模型会失败。")
    snapshot = await voice.wait(lambda s: bool(s["messages"]) and s["messages"][-1]["status"] == "failed")
    assert snapshot["state"]["voice_status"] == "active"
    assert snapshot["state"]["model_available"]
    assert snapshot["state"]["last_error"]["service"] == "model"
    assert snapshot["state"]["last_error"]["category"] == "server"
    assert not snapshot["state"]["last_error"]["permanent"]
    assert len(voice.requests) == 1
    with pytest.raises(ValueError, match="文字"):
        voice.runtime.resend_text(snapshot["messages"][0]["id"])
    voice.failures.clear()
    await voice.transcript("我重新问一个问题。")
    voice.release_audio()
    await voice.wait(lambda s: len(s["messages"]) == 4 and s["messages"][-1]["status"] == "completed")
    assert len(voice.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_permanent_model_failure_disables_generation_but_keeps_history_accessible(voice):
    await voice.wake()
    voice.failures["model"] = 401
    await voice.transcript("测试鉴权失败。")
    snapshot = await voice.wait(lambda s: s["state"]["voice_status"] == "error")
    assert not snapshot["state"]["model_available"]
    assert snapshot["state"]["last_error"]["category"] == "authentication"
    assert snapshot["messages"][-1]["status"] == "failed"
    assert "重启" in snapshot["state"]["model_error"]
    voice.runtime.command("show_main")
    voice.runtime.command("recover_voice")
    snapshot = voice.runtime.connect("test")
    assert snapshot["state"]["voice_recovery"] == "failed"
    assert len(voice.requests) == 1
    with pytest.raises(ValueError, match="重启"):
        voice.runtime.send_text("再发送一次。")


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_resend_appends_original_text_and_preserves_failed_partial_reply(voice):
    voice.fail_model_after_text = True
    first = voice.runtime.send_text("请保留这条原消息。")
    snapshot = await voice.wait(lambda s: s["messages"][-1]["status"] == "failed")
    assert snapshot["state"]["model_available"]
    assert snapshot["messages"][1]["text"]
    original = snapshot["messages"][:2]
    voice.fail_model_after_text = False
    resent = voice.runtime.resend_text(first["user_message_id"])
    snapshot = await voice.wait(lambda s: len(s["messages"]) == 4 and s["messages"][-1]["status"] == "completed")
    assert snapshot["messages"][:2] == original
    assert first["user_message_id"] != resent["user_message_id"]
    assert snapshot["messages"][2]["text"] == original[0]["text"]
    assert snapshot["messages"][3]["source"] == "text"
    assert snapshot["state"]["voice_status"] == "standby"
    assert len(voice.requests) == 2
    assert not voice.tts_requests
    assert voice.asr_connections == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_recovery_interrupts_text_and_old_callbacks_keep_their_original_owner(voice):
    old_voice = voice.runtime._conversation
    await voice.backend(old_voice._voice.transport.input().push_error(
        "测试录音设备失败", exception=OSError("设备不可用"), force_treat_as_permanent=True))
    await voice.wait(lambda s: s["state"]["voice_status"] == "error")
    voice.hold_model_after_text = True
    receipt = voice.runtime.send_text("正在进行的文字回复。")
    await voice.wait(lambda s: bool(s["messages"][-1]["text"]))
    old_text = voice.runtime._conversation
    voice.runtime.command("recover_voice")
    snapshot = await voice.wait(lambda s: s["state"]["voice_status"] == "standby")
    assert snapshot["messages"][-1]["status"] == "interrupted"
    assert snapshot["messages"][-1]["text"] == "测试"
    assert snapshot["state"]["voice_recovery"] == "succeeded"
    assert voice.asr_connections == 0
    assert len(voice.requests) == 1
    for conversation in (old_voice, old_text):
        voice.runtime._loop.call_soon_threadsafe(conversation._emit,
            {"type": "text", "reply_id": receipt["reply_id"], "text": "迟到文字"})
        voice.runtime._loop.call_soon_threadsafe(conversation._emit,
            {"type": "unavailable", "voice": True, "reply_ids": (receipt["reply_id"],), "error": "旧失败"})
    snapshot = voice.runtime.connect("test")
    assert snapshot["messages"][-1]["text"] == "测试"
    assert snapshot["state"]["voice_status"] == "standby"
    assert voice.runtime._model_records[-1]["message"]["content"] == "测试"
    voice.release_model.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_failed_recovery_keeps_specific_reason_and_can_be_requested_again(voice, monkeypatch):
    original_setup = MemoryInput.setup

    async def fail_setup(self, setup):
        await BaseInputTransport.setup(self, setup)
        raise OSError("新的默认录音设备不可用")

    monkeypatch.setattr(MemoryInput, "setup", fail_setup)
    voice.runtime.command("recover_voice")
    snapshot = await voice.wait(lambda s: s["state"]["voice_recovery"] == "failed")
    assert snapshot["state"]["voice_status"] == "error"
    assert "新的默认录音设备不可用" in snapshot["state"]["voice_error"]
    assert snapshot["state"]["model_available"]
    assert voice.runtime._conversation is None
    monkeypatch.setattr(MemoryInput, "setup", original_setup)
    voice.runtime.command("recover_voice")
    snapshot = await voice.wait(lambda s: s["state"]["voice_status"] == "standby")
    assert snapshot["state"]["voice_recovery"] == "succeeded"
    assert not snapshot["state"]["voice_error"]
    assert voice.asr_connections == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
@pytest.mark.parametrize("missing", ["voice", "model", "both"])
async def test_missing_capabilities_preserve_saved_history_and_available_text(voice, monkeypatch, tmp_path, missing):
    voice.runtime.send_text("这条记录需要跨重启保留。")
    snapshot = await voice.wait(lambda s: s["messages"][-1]["status"] == "completed")
    original = snapshot["messages"]
    await asyncio.to_thread(voice.runtime.stop)
    if missing in {"voice", "both"}:
        monkeypatch.setenv("VOLCENGINE_API_KEY", "")
        monkeypatch.setenv("ARK_API_KEY", "")
    if missing in {"model", "both"}:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    voice.runtime = Runtime(database_path=tmp_path / "voice.sqlite3", wake_model_dir=tmp_path)
    voice.runtime.start()
    snapshot = await voice.wait(lambda s: s["state"]["voice_status"] == "error")
    assert snapshot["messages"] == original
    voice.runtime.command("show_main")
    voice.runtime.command("recover_voice")
    assert voice.runtime.connect("test")["state"]["voice_recovery"] == "failed"
    if missing == "voice":
        voice.runtime.send_text("只用文字继续。")
        await voice.wait(lambda s: len(s["messages"]) == 4 and s["messages"][-1]["status"] == "completed")
        users = [m["content"] for m in voice.requests[-1]["messages"] if m["role"] == "user"]
        assert users == ["这条记录需要跨重启保留。", "只用文字继续。"]
    else:
        with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
            voice.runtime.send_text("需要配置模型后再发送。")
        assert len(voice.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
@pytest.mark.parametrize("active", [False, True])
async def test_text_accepted_during_failed_worker_cleanup_runs_after_cleanup(voice, monkeypatch, active):
    if active:
        await voice.wake()
    conversation = voice.runtime._conversation
    native_close = conversation.close
    release_cleanup = asyncio.Event()
    cleanup_started = False

    async def failed_send(*args, **kwargs):
        raise RuntimeError("旧执行对象已停止")

    async def delayed_close():
        nonlocal cleanup_started
        cleanup_started = True
        await release_cleanup.wait()
        await native_close()

    monkeypatch.setattr(conversation, "send", failed_send)
    monkeypatch.setattr(conversation, "close", delayed_close)
    voice.runtime.send_text("遇到执行错误的消息。")
    await voice.wait(lambda s: cleanup_started and s["messages"][-1]["status"] == "failed")
    receipt = voice.runtime.send_text("清理期间接收的新消息。")
    voice.runtime._loop.call_soon_threadsafe(release_cleanup.set)
    snapshot = await voice.wait(lambda s: s["messages"][-1]["id"] == receipt["reply_id"]
                                and s["messages"][-1]["status"] == "completed")
    assert snapshot["messages"][1]["status"] == "failed"
    assert snapshot["state"]["voice_status"] == "error"
    assert len(voice.requests) == 1
