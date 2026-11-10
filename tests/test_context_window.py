import asyncio
from copy import deepcopy
import json
import sqlite3
from threading import Event
from types import SimpleNamespace

import pytest

from test_voice import voice
from voice_super_agent.memory.context import WorkingContext
from voice_super_agent.runtime import Runtime


def references(index):
    return {"message_id": f"message-{index}", "created_at": "2026-11-10T01:00:00Z",
            "source": "voice", "status": "completed", "preview": '中文原文"🙂\n' * 8}


def request(messages):
    return {"messages": [{"role": "system", "content": "系统指令"}] + messages, "tools": [], "max_tokens": None}


def assert_tool_pairs(messages):
    calls = [call["id"] for message in messages for call in message.get("tool_calls", [])]
    results = [message["tool_call_id"] for message in messages if message["role"] == "tool"]
    assert set(calls) == set(results)
    assert len(calls) == len(results)


def test_recent_complete_rounds_previews_and_original_are_bounded():
    messages = []
    for index in range(40):
        messages.extend([{"role": "user", "content": f"主题{index}：" + "问题" * 180},
                         {"role": "assistant", "content": "原文回答" * 180}])
    messages.append({"role": "user", "content": "当前问题"})
    original = deepcopy(messages)
    window = WorkingContext(references, input_bytes=8192, tool_reserve_bytes=2048, preview_bytes=2048, preview_count=3)
    params = window.project(request(messages), messages)
    assert window.size(params["messages"]) <= 8192
    assert params["max_tokens"] == 8192
    assert params["messages"][-1] == messages[-1]
    assert params["messages"][-3:] == messages[-3:]
    assert all(message in original for message in params["messages"] if message["role"] != "system")
    previews = json.loads(params["messages"][1]["content"].split("\n", 1)[1])
    assert len(previews) <= 3
    assert all(preview["message_id"].startswith("message-") for preview in previews)
    assert window.size([params["messages"][1]]) <= 2048
    assert messages == original


def test_tool_cycles_spill_completed_groups_keep_latest_pairs_and_question():
    messages = [{"role": "user", "content": "逐页读出长日志"}]
    for index in range(10):
        messages.extend([{"role": "assistant", "content": None, "tool_calls": [
            {"id": f"call-{index}", "type": "function", "function": {"name": "history_read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"call-{index}", "content": "中文原文" * 300}])
    original = deepcopy(messages)
    window = WorkingContext(references, input_bytes=10000, tool_reserve_bytes=4000, preview_bytes=2000)
    for index in range(10):
        window.record_tool_group([f"call-{index}"])
    result = window.project(request(messages), messages)
    assert window.size(result["messages"]) <= 10000
    assert result["messages"][-2:] == messages[-2:]
    assert messages[0] in result["messages"]
    assert len(result["messages"]) < len(messages)
    assert_tool_pairs(result["messages"])
    assert "较早完成的工具组" in result["messages"][1]["content"]
    assert messages == original


def test_latest_parallel_group_is_kept_or_explicitly_rejected():
    messages = [{"role": "user", "content": "当前问题"}, {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"call-{index}", "type": "function", "function": {"name": "history_read", "arguments": "{}"}}
        for index in range(2)]}]
    messages.extend({"role": "tool", "tool_call_id": f"call-{index}", "content": "x" * 2500} for index in range(2))
    window = WorkingContext(input_bytes=7000, tool_reserve_bytes=3000)
    assert_tool_pairs(window.project(request(messages), messages)["messages"])
    with pytest.raises(ValueError, match="最新完整工具结果组"):
        WorkingContext(input_bytes=4000, tool_reserve_bytes=1000).project(request(messages), messages)


def test_native_parallel_batch_of_separate_messages_is_kept_as_one_group():
    messages = [{"role": "user", "content": "并行读取三条原文"}]
    for index in range(3):
        messages.extend([{"role": "assistant", "tool_calls": [
            {"id": str(index), "type": "function", "function": {"name": "history_read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": str(index), "content": "中" * 8000}])
    window = WorkingContext()
    window.record_tool_group(["0", "1", "2"])
    with pytest.raises(ValueError, match="最新完整工具结果组"):
        window.project(request(messages), messages)


def test_definitions_and_utf8_size_count_against_same_budget():
    messages = [{"role": "user", "content": "🙂中" * 500}]
    params = request(messages)
    params["tools"] = [{"type": "function", "function": {"name": "history_search", "description": "定义" * 800}}]
    with pytest.raises(ValueError, match="预算"):
        WorkingContext(input_bytes=6000, tool_reserve_bytes=1000).project(params, messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_real_request_budget_applies_to_followup_interruption_and_tools(voice):
    for index in range(8):
        voice.runtime.send_text(f"主题{index}：" + "历史中文内容" * 350)
        await voice.wait(lambda s: s["messages"][-1]["status"] == "completed")
    voice.tools["查长历史。"] = ("history_search", {"keywords": ["历史"]})
    voice.runtime.send_text("查长历史。")
    await voice.wait(lambda s: s["messages"][-1]["status"] == "completed")
    for payload in voice.requests:
        assert WorkingContext.size(payload["messages"]) + WorkingContext.size(payload["tools"]) <= 65536
        assert payload["max_tokens"] == 8192
        assert_tool_pairs(payload["messages"])
    assert len(voice.runtime._model_records) > len(voice.requests[-1]["messages"])
    previews = [message for message in voice.requests[-1]["messages"] if message["role"] == "system" and "原文预览" in message["content"]]
    assert previews
    first_id = voice.runtime.connect("test")["messages"][0]["id"]
    assert (await voice.backend(voice.runtime._store.history_read(message_id=first_id)))["message"]["text"].startswith("主题0")


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
@pytest.mark.parametrize("mode", ["text", "voice"])
async def test_native_long_read_loop_spills_old_groups_and_preserves_full_results(voice, mode):
    text = "原文逐页读取，保留所有内容。" * 3000

    async def seed():
        voice.runtime._messages["long-history"] = {
            "id": "long-history", "role": "user", "source": "text", "reply_id": "old-history", "text": text,
            "status": "completed", "created_at": "2026-11-01T00:00:00Z", "error": ""}
        voice.runtime._request_save(immediate=True)
        await voice.runtime._flush()

    await voice.backend(seed())

    def next_tool(prompt):
        if prompt == "连续回读。":
            offset = 0
        elif prompt.startswith('{"status"'):
            offset = json.loads(prompt)["next_offset"]
            if offset is None:
                return None
        else:
            return None
        return "history_read", {"message_id": "long-history", "offset": offset, "limit": 6000}

    voice.tools = SimpleNamespace(get=next_tool)
    if mode == "voice":
        await voice.wake()
        voice.release_audio()
        await voice.transcript("连续回读。")
    else:
        voice.runtime.send_text("连续回读。")
    await voice.wait(lambda s: s["messages"][-1]["status"] == "completed" and len(voice.requests) >= 7)
    for payload in voice.requests:
        assert WorkingContext.size(payload["messages"]) + WorkingContext.size(payload["tools"]) <= 65536
        assert_tool_pairs(payload["messages"])
    results = [json.loads(record["message"]["content"])["message"]["text"] for record in voice.runtime._model_records
               if record["message"]["role"] == "tool"]
    assert "".join(results) == text
    assert len([message for message in voice.requests[-1]["messages"] if message["role"] == "tool"]) < len(results)
    assert any("原文预览" in message.get("content", "") for message in voice.requests[-1]["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_latest_native_parallel_batch_is_rejected_as_a_whole(voice):
    async def seed():
        voice.runtime._messages["parallel-history"] = {
            "id": "parallel-history", "role": "user", "source": "text", "reply_id": "old-history", "text": "中" * 9000,
            "status": "completed", "created_at": "2026-11-01T00:00:00Z", "error": ""}
        voice.runtime._request_save(immediate=True)
        await voice.runtime._flush()
    await voice.backend(seed())
    voice.tool_chunk_mode = "combined"
    voice.tools["并行读长文。"] = [("history_read", {"message_id": "parallel-history", "limit": 8000})] * 3
    voice.runtime.send_text("并行读长文。")
    snapshot = await voice.wait(lambda s: s["messages"][-1]["status"] == "failed")
    assert "最新完整工具结果组" in snapshot["messages"][-1]["error"]
    assert len(voice.requests) == 1
    results = [record["message"] for record in voice.runtime._model_records if record["message"]["role"] == "tool"]
    assert len(results) == 3
    assert all(len(json.loads(result["content"])["message"]["text"]) == 8000 for result in results)
    voice.runtime.send_text("继续简短交流。")
    await voice.wait(lambda s: s["messages"][-1]["status"] == "completed")
    assert len(voice.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_oversized_current_question_has_explicit_failure_and_next_input_works(voice):
    voice.runtime.send_text("超长问题" * 6000)
    snapshot = await voice.wait(lambda s: s["messages"][-1]["status"] == "failed")
    assert "预算" in snapshot["messages"][-1]["error"]
    assert not voice.requests
    assert snapshot["state"]["model_available"]
    voice.runtime.send_text("换个简短问题。")
    await voice.wait(lambda s: s["messages"][-1]["status"] == "completed")
    assert len(voice.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_restart_window_transaction_failure_keeps_old_scope(voice, monkeypatch):
    voice.runtime.send_text("旧内容")
    snapshot = await voice.wait(lambda s: s["messages"][-1]["status"] == "completed" and s["state"]["storage_status"] == "saved")
    window = dict(voice.runtime._window)
    original = voice.runtime._store.save

    async def fail_window(messages, records, candidate):
        if candidate != window:
            raise sqlite3.OperationalError("测试窗口事务失败")
        await original(messages, records, candidate)

    monkeypatch.setattr(voice.runtime._store, "save", fail_window)
    with pytest.raises(ValueError, match="测试窗口事务失败"):
        await asyncio.to_thread(voice.runtime.command, "restart_conversation")
    failed = voice.runtime.connect("test")
    assert failed["messages"] == snapshot["messages"]
    assert voice.runtime._window == window
    assert not failed["state"]["restarting"]
    assert failed["state"]["storage_status"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_restart_during_voice_recognition_ignores_late_old_frames(voice):
    await voice.wake()
    await voice.transcript("未说完的旧话", final=False)
    await voice.wait(lambda s: s["messages"] and s["messages"][0]["text"] == "未说完的旧话")
    old = voice.runtime._conversation
    await asyncio.to_thread(voice.runtime.command, "restart_conversation")
    assert voice.runtime.connect("test")["messages"] == []
    assert voice.runtime.connect("test")["state"]["voice_status"] == "standby"
    assert voice.runtime._conversation is not old
    count = len(voice.requests)
    old._emit({"type": "voice_input", "user_id": "late", "reply_id": "late-reply", "text": "迟到旧转写", "created_at": "2026-11-10T00:00:00Z"})
    assert voice.runtime.connect("test")["messages"] == []
    assert len(voice.requests) == count
    await voice.wake()
    voice.release_audio()
    await voice.transcript("新的语音问题。")
    await voice.wait(lambda s: s["messages"] and s["messages"][-1]["status"] == "completed")
    assert [message["content"] for message in voice.requests[-1]["messages"] if message["role"] == "user"][-1] == "新的语音问题。"


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_restart_waits_for_cancelled_query_save_and_rejects_input(voice, monkeypatch):
    original = voice.runtime._store._history_search
    entered, release = Event(), Event()

    def blocked(*arguments):
        entered.set()
        release.wait(8)
        return original(*arguments)

    monkeypatch.setattr(voice.runtime._store, "_history_search", blocked)
    voice.tools["查询旧记录。"] = ("history_search", {"keywords": ["旧"]})
    voice.runtime.send_text("查询旧记录。")
    assert await asyncio.to_thread(entered.wait, 3)
    restart = asyncio.create_task(asyncio.to_thread(voice.runtime.command, "restart_conversation"))
    try:
        await voice.wait(lambda s: s["state"]["restarting"])
        with pytest.raises(ValueError, match="正在重新开始"):
            voice.runtime.send_text("保存期间的输入")
        voice.runtime.command("wake_voice")
    finally:
        release.set()
    await restart
    assert voice.runtime.connect("test")["messages"] == []
    assert voice.runtime.connect("test")["state"]["voice_status"] == "standby"
    results = [record["message"]["content"] for record in voice.runtime._model_records if record["message"]["role"] == "tool"]
    assert results == ["CANCELLED"]


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_exit_waits_for_new_scope_transaction_and_prevents_voice_recreation(voice, monkeypatch):
    voice.runtime.send_text("退出前的旧内容")
    await voice.wait(lambda s: s["messages"][-1]["status"] == "completed" and s["state"]["storage_status"] == "saved")
    entered, release = Event(), Event()
    original = voice.runtime._store.save

    async def blocked(messages, records, window):
        if window["display_start_position"]:
            entered.set()
            await asyncio.to_thread(release.wait, 5)
        await original(messages, records, window)

    monkeypatch.setattr(voice.runtime._store, "save", blocked)
    restart = asyncio.create_task(asyncio.to_thread(voice.runtime.command, "restart_conversation"))
    assert await asyncio.to_thread(entered.wait, 3)
    exiting = asyncio.create_task(asyncio.to_thread(voice.runtime.prepare_exit))
    try:
        await voice.wait(lambda s: s["state"]["exiting"])
        assert not exiting.done()
    finally:
        release.set()
    await restart
    assert await exiting
    assert voice.runtime._conversation is None
    assert voice.runtime._exit_prepared
    with sqlite3.connect(voice.runtime._store._path) as connection:
        row = connection.execute("SELECT display_start_position, model_start_position FROM working_window").fetchone()
    assert row == (voice.runtime._window["display_start_position"], voice.runtime._window["model_start_position"])
    assert row[0] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_restart_cancels_voice_preparation_and_returns_to_standby(voice, monkeypatch):
    entered = Event()

    async def preparing():
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(voice.runtime._conversation, "activate_voice", preparing)
    voice.runtime.command("wake_voice")
    assert await asyncio.to_thread(entered.wait, 3)
    await asyncio.to_thread(voice.runtime.command, "restart_conversation")
    snapshot = voice.runtime.connect("test")
    assert snapshot["messages"] == []
    assert snapshot["state"]["voice_status"] == "standby"
    assert not snapshot["state"]["restarting"]
    assert voice.runtime._conversation._voice.gate.mode == "standby"
    assert not voice.asr_connections


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_exit_handles_runner_already_cancelling_without_flush_timeout(voice):
    async def cancel_then_close():
        conversation = voice.runtime._conversation
        await conversation._runner.cancel()
        async with asyncio.timeout(1):
            await conversation.close()
        assert conversation._runner_task.done()
    await voice.backend(cancel_then_close())


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_failed_close_releases_worker_and_exit_can_be_retried(voice, monkeypatch):
    conversation = voice.runtime._conversation
    async def fail_flush():
        return False
    monkeypatch.setattr(conversation._worker, "flush_pipeline", fail_flush)
    assert not await asyncio.to_thread(voice.runtime.prepare_exit)
    snapshot = voice.runtime.connect("test")
    assert not snapshot["state"]["exiting"]
    assert "再次退出" in snapshot["state"]["exit_error"]
    assert conversation._runner_task.done()
    assert await asyncio.to_thread(voice.runtime.prepare_exit)
    assert not voice.runtime.connect("test")["state"]["exit_error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_exit_during_reset_voice_setup_cleans_new_hotkey_task(voice, monkeypatch):
    entered, release = Event(), Event()
    original = voice.runtime._prepare_voice_conversation

    async def blocked():
        entered.set()
        await asyncio.to_thread(release.wait, 5)
        await original()

    monkeypatch.setattr(voice.runtime, "_prepare_voice_conversation", blocked)
    restart = asyncio.create_task(asyncio.to_thread(voice.runtime.command, "restart_conversation"))
    assert await asyncio.to_thread(entered.wait, 3)
    exiting = asyncio.create_task(asyncio.to_thread(voice.runtime.prepare_exit))
    try:
        await voice.wait(lambda s: s["state"]["exiting"])
    finally:
        release.set()
    await restart
    assert await exiting
    assert voice.runtime._conversation is None
    assert voice.runtime._hotkey_task.done()
    assert voice.runtime._idle_task.done()
    assert voice.runtime._device_task.done()


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_exit_reports_voice_preparation_cleanup_failure_and_retries(voice, monkeypatch):
    entered = Event()
    conversation = voice.runtime._conversation

    async def preparing():
        entered.set()
        await asyncio.Event().wait()

    async def fail_flush():
        return False

    monkeypatch.setattr(conversation, "activate_voice", preparing)
    monkeypatch.setattr(conversation._worker, "flush_pipeline", fail_flush)
    voice.runtime.command("wake_voice")
    assert await asyncio.to_thread(entered.wait, 3)
    assert not await asyncio.to_thread(voice.runtime.prepare_exit)
    assert not voice.runtime.connect("test")["state"]["exiting"]
    assert voice.runtime.connect("test")["state"]["exit_error"]
    assert conversation._runner_task.done()
    assert await asyncio.to_thread(voice.runtime.prepare_exit)


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_restart_cancels_stream_preserves_original_and_restores_new_scope(voice, monkeypatch):
    voice.hold_model_after_text = True
    voice.runtime.send_text("旧轮次")
    snapshot = await voice.wait(lambda s: s["messages"][-1]["text"] == "测试")
    old_id, user_id = snapshot["messages"][-1]["id"], snapshot["messages"][0]["id"]
    await asyncio.to_thread(voice.runtime.command, "restart_conversation")
    assert voice.runtime.connect("test")["messages"] == []
    assert voice.runtime.connect("test")["state"]["voice_status"] == "standby"
    old = await voice.backend(voice.runtime._store.history_read(message_id=old_id))
    assert old["message"]["text"] == "测试"
    assert old["message"]["status"] == "interrupted"
    voice.hold_model_after_text = False
    voice.release_model.set()
    voice.tools["读取旧记录。"] = ("history_read", {"message_id": user_id})
    voice.runtime.send_text("读取旧记录。")
    await voice.wait(lambda s: s["messages"][-1]["status"] == "completed")
    assert [message["content"] for message in voice.requests[-2]["messages"] if message["role"] == "user"] == ["读取旧记录。"]
    tool = json.loads(voice.requests[-1]["messages"][-1]["content"])
    assert tool["message"]["text"] == "旧轮次"
    assert not any("原文预览" in message.get("content", "") for message in voice.requests[-2]["messages"])
    await voice.backend(voice.runtime._flush())
    path = voice.runtime._store._path
    snapshot = voice.runtime.connect("test")
    await asyncio.to_thread(voice.runtime.stop)
    restored = Runtime(database_path=path)
    restored.start()
    try:
        assert restored.connect("test")["messages"] == snapshot["messages"]
        assert restored._window == voice.runtime._window
    finally:
        await asyncio.to_thread(restored.stop)
    monkeypatch.setattr(voice.runtime, "stop", lambda: None)
