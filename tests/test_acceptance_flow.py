"""以同一运行时连续串联语音、文字、历史、错误恢复与重启。"""

import asyncio
import json
import struct

import pytest

from test_voice import voice
from voice_super_agent.runtime import Runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_continuous_agent_flow_preserves_context_history_and_recovery(voice):
    runtime = voice.runtime
    runtime.update_windows(main_window="hidden")
    await voice.wake()
    await voice.transcript("项目代号是琥珀松鼠7426，请介绍语音方案。")
    first = await voice.wait(lambda s: s["state"]["voice_activity"] == "speaking")
    first_id = first["messages"][0]["id"]
    first_reply = first["messages"][1]["id"]
    assert first["state"]["floating_visible"]
    await voice.transcript("嗯嗯，你继续。")
    await voice.wait(lambda s: any(row["status"] == "backchannel" for row in s["messages"]))
    assert len(voice.requests) == 1
    voice.decision = "interrupt"
    await voice.transcript("等一下，改聊唤醒方式。")
    await voice.wait(lambda s: any(row["id"] == first_reply and row["status"] == "interrupted" for row in s["messages"])
                     and len([row for row in s["messages"] if row["role"] == "user" and row["status"] != "backchannel"]) >= 2)
    receipt = runtime.send_text("现在改用文字，保留项目代号。")
    text = await voice.wait(lambda s: any(row["id"] == receipt["reply_id"] and row["status"] == "completed" for row in s["messages"]))
    assert text["state"]["voice_status"] == "active"
    assert next(row for row in text["messages"] if row["id"] == receipt["reply_id"])["source"] == "text"
    assert all("嗯嗯" not in str(record["message"].get("content", "")) for record in runtime._model_records)
    runtime.command("end_voice")
    await voice.wait(lambda s: s["state"]["voice_status"] == "standby")
    assert runtime._conversation._voice.stt._socket is None
    runtime.command("restart_conversation")
    assert not runtime.connect("test")["messages"]
    history = runtime.list_history()
    assert any(row["id"] == first_id for row in history["messages"])
    assert "琥珀松鼠7426" in runtime.read_history(first_id)["message"]["text"]
    voice.tools["读取之前的项目代号。"] = ("history_read", {"message_id": first_id})
    await voice.wake()
    await voice.transcript("读取之前的项目代号。")
    await voice.wait(lambda s: s["state"]["history_status"] == "found")
    voice.release_audio()
    await voice.wait(lambda s: s["messages"][-1]["role"] == "assistant" and s["messages"][-1]["status"] == "completed")
    assert any(row["role"] == "tool" and "琥珀松鼠7426" in str(row["content"])
               for row in voice.requests[-1]["messages"])
    runtime.command("hide_floating")
    await voice.asr.send_bytes(b"\x11\xf0\x00\x00" + struct.pack(">I", 45000001))
    failed = await voice.wait(lambda s: s["state"]["voice_status"] == "error")
    assert failed["state"]["floating_hidden"] and not failed["state"]["floating_visible"]
    receipt = runtime.send_text("语音失败后继续文字交流。")
    await voice.wait(lambda s: any(row["id"] == receipt["reply_id"] and row["status"] == "completed" for row in s["messages"]))
    runtime.command("recover_voice")
    recovered = await voice.wait(lambda s: s["state"]["voice_status"] == "standby")
    assert recovered["state"]["voice_recovery"] == "succeeded"
    assert runtime._conversation._voice.stt._socket is None
    window = dict(runtime._window)
    path = runtime._store._path
    assert await asyncio.to_thread(runtime.prepare_exit)
    saved_messages = runtime.connect("test")["messages"]
    saved_model = json.loads(json.dumps(runtime._model_records))
    restored = Runtime(database_path=path)
    restored.start()
    try:
        assert restored.connect("test")["messages"] == saved_messages
        assert restored._model_records == saved_model
        assert restored._window == window
        assert "琥珀松鼠7426" in restored.read_history(first_id)["message"]["text"]
    finally:
        restored.stop()
