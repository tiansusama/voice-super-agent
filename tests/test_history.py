import asyncio
import json
import sqlite3
from threading import Event
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
import pytest_asyncio

from test_voice import voice
from voice_super_agent.memory.store import ConversationStore
from voice_super_agent import conversation as conversation_module


def message(index, text, *, reply_id=None, role="user", source="text", status="completed", created_at="2026-11-08T01:00:00+00:00"):
    return {"id": f"message-{index}", "role": role, "source": source, "reply_id": reply_id or f"reply-{index}",
            "text": text, "status": status, "created_at": created_at, "error": ""}


@pytest_asyncio.fixture
async def history(tmp_path):
    store = ConversationStore(tmp_path / "history.sqlite3")
    await store.open()
    messages = [message(0, "杭州旅行，预算 3000 元。", reply_id="trip"),
                message(1, "先去西湖。🙂\n" * 500, reply_id="trip", role="assistant", status="interrupted"),
                message(2, "嗯嗯", reply_id="trip", source="voice", status="backchannel"),
                message(3, "杭州旅行还没说完", source="voice", status="unfinished"),
                message(4, "100%_原文", created_at="2026-11-09T00:00:00+00:00")]
    await store.save(messages, [], {"display_start_position": 3, "model_start_position": 0})
    try:
        yield store, messages
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_existing_database_upgrade_preserves_history_context_and_window(tmp_path):
    path = tmp_path / "existing.sqlite3"
    original = message(0, "杭州的旧记录")
    native = {"role": "user", "content": original["text"]}
    script = Path(__file__).parents[1] / "src/voice_super_agent/memory/migrations/V1.0.0_001__init_conversation.sql"
    with sqlite3.connect(path) as connection:
        connection.executescript(script.read_text(encoding="utf-8"))
        connection.execute("INSERT INTO display_messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (0, *original.values()))
        connection.execute("INSERT INTO model_messages VALUES (?, ?, ?)", (0, original["id"], json.dumps(native)))
        connection.execute("UPDATE working_window SET display_start_position = 1, model_start_position = 1")
    store = ConversationStore(path)
    try:
        restored = await store.open()
        assert restored == {"messages": [original], "model_records": [{"display_message_id": original["id"], "message": native}],
                            "window": {"display_start_position": 1, "model_start_position": 1}}
        assert (await store.history_search(keywords=["杭州"]))["matches"][0]["id"] == original["id"]
        assert (await store.history_read(message_id=original["id"]))["message"]["text"] == original["text"]
        with sqlite3.connect(path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
            assert "idx_display_messages_reply_position" in {row[1] for row in connection.execute("PRAGMA index_list(display_messages)")}
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_short_chinese_literal_time_filter_and_source_status(history):
    store, _ = history
    result = await store.history_search(keywords=["杭州", "西湖"], limit=10)
    assert [row["id"] for row in result["matches"]] == ["message-3", "message-1", "message-0"]
    assert result["matches"][0]["status"] == "unfinished"
    assert result["matches"][0]["source"] == "voice"
    assert len(result["matches"][1]["preview"]) == 160
    assert (await store.history_search(keywords=["%_"]))["matches"][0]["id"] == "message-4"
    assert (await store.history_search(keywords=["不存在"]))["status"] == "not_found"
    result = await store.history_search(start_time="2026-11-08T09:00:00+09:00", end_time="2026-11-09T09:00:00+09:00")
    assert {row["id"] for row in result["matches"]} == {f"message-{i}" for i in range(4)}
    assert (await store.history_search())["status"] == "needs_clarification"


@pytest.mark.asyncio
async def test_position_paging_survives_new_records_and_excludes_asking_round(history):
    store, messages = history
    first = await store.history_search(keywords=["杭州", "西湖"], limit=1)
    await store.save(messages + [message(5, "杭州新问题")], [], {"display_start_position": 0, "model_start_position": 0})
    second = await store.history_search(keywords=["杭州", "西湖"], limit=1, before_position=first["next_before_position"])
    third = await store.history_search(keywords=["杭州", "西湖"], limit=1, before_position=second["next_before_position"])
    assert [page["matches"][0]["id"] for page in (first, second, third)] == ["message-3", "message-1", "message-0"]
    assert third["next_before_position"] is None
    result = await store.history_search(keywords=["杭州"], exclude_reply_id="reply-5")
    assert all(row["reply_id"] != "reply-5" for row in result["matches"])


@pytest.mark.asyncio
async def test_unicode_original_continuation_and_associated_messages(history):
    store, messages = history
    chunks, offset = [], 0
    while offset is not None:
        result = await store.history_read(message_id="message-1", offset=offset, limit=97)
        chunks.append(result["message"]["text"])
        offset = result["next_offset"]
    assert "".join(chunks) == messages[1]["text"]
    assert result["message"]["status"] == "interrupted"
    assert [row["id"] for row in result["related"]] == ["message-0", "message-1", "message-2"]
    assert result["related"][-1]["status"] == "backchannel"
    assert result["previous_message_id"] == "message-0"
    assert result["next_message_id"] == "message-2"
    assert (await store.history_read(message_id="missing"))["status"] == "not_found"
    assert (await store.history_read(message_id="message-1", offset=len(messages[1]["text"])))["next_offset"] is None
    for arguments in ({"offset": -1}, {"offset": 10000}, {"limit": 0}, {"limit": 8001}):
        with pytest.raises(ValueError):
            await store.history_read(message_id="message-1", **arguments)


@pytest.mark.asyncio
async def test_related_list_has_bounded_continuation(history):
    store, messages = history
    messages.extend(message(i, "嗯", reply_id="trip", source="voice", status="backchannel") for i in range(5, 30))
    await store.save(messages, [], {"display_start_position": 0, "model_start_position": 0})
    ids, offset = [], 0
    while offset is not None:
        result = await store.history_read(message_id="message-0", related_offset=offset)
        assert len(result["related"]) <= 10
        ids.extend(row["id"] for row in result["related"])
        offset = result["next_related_offset"]
    assert ids == [m["id"] for m in messages if m["reply_id"] == "trip"]
    for arguments in ({"start_time": "2026-11-08"}, {"start_time": "bad"}, {"keywords": [""]}, {"limit": 21}):
        with pytest.raises(ValueError):
            await store.history_search(**arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
async def test_history_relative_time_updates_across_midnight(voice, monkeypatch):
    clock = Mock()
    before = datetime(2026, 11, 10, 23, 59).astimezone()
    after = datetime(2026, 11, 11, 0, 1).astimezone()
    clock.now.return_value = before
    monkeypatch.setattr(conversation_module, "datetime", clock)
    voice.runtime.send_text("今天的记录。")
    await voice.wait(lambda s: s["messages"][-1]["status"] == "completed")
    clock.now.return_value = after
    voice.runtime.send_text("昨天的记录。")
    await voice.wait(lambda s: len(voice.requests) == 2 and s["messages"][-1]["status"] == "completed")
    for request, instant in zip(voice.requests, (before, after)):
        assert instant.astimezone().isoformat() in request["messages"][0]["content"]
    assert all("当前本地时间" not in row["message"].get("content", "") for row in voice.runtime._model_records)


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
@pytest.mark.parametrize("mode", ["text", "voice"])
async def test_native_tools_read_saved_original_and_preserve_result_context(voice, mode):
    voice.runtime.send_text("杭州旅行预算 3000 元。")
    await voice.wait(lambda s: s["messages"][-1]["status"] == "completed")
    original_id = voice.runtime.connect("test")["messages"][0]["id"]
    voice.tools["查历史。"] = ("history_search", {"keywords": ["杭州"]})
    voice.tools["读原文。"] = ("history_read", {"message_id": original_id, "offset": 2, "limit": 6})
    if mode == "voice":
        await voice.wake()
        voice.release_audio()
    for prompt in ("查历史。", "读原文。"):
        count = len(voice.requests)
        if mode == "voice":
            await voice.transcript(prompt)
        else:
            voice.runtime.send_text(prompt)
        snapshot = await voice.wait(lambda s: len(voice.requests) >= count + 2 and s["messages"][-1]["status"] == "completed")
        assert snapshot["state"]["history_status"] == "found"
        assert snapshot["state"]["history_reply_id"] == snapshot["messages"][-1]["id"]
    tools = [record["message"] for record in voice.runtime._model_records if record["message"]["role"] == "tool"]
    assert json.loads(tools[0]["content"])["matches"][0]["id"] == original_id
    assert json.loads(tools[1]["content"])["message"]["text"] == "旅行预算 3"
    assert all(row["reply_id"] != snapshot["messages"][-1]["id"] for row in json.loads(tools[0]["content"])["matches"])
    await voice.backend(voice.runtime._flush())
    with sqlite3.connect(voice.runtime._store._path) as connection:
        persisted = [json.loads(row[0]) for row in connection.execute("SELECT message_json FROM model_messages ORDER BY position")]
    assert [row for row in persisted if row["role"] == "tool"] == tools


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
@pytest.mark.parametrize("kind, expected", [("empty", "not_found"), ("clue", "needs_clarification"), ("failure", "failed"), ("invalid", "invalid_arguments")])
async def test_native_history_outcomes_and_text_continuation(voice, monkeypatch, kind, expected):
    arguments = {"keywords": ["不存在"]} if kind != "clue" else {}
    if kind == "failure":
        async def fail(**kwargs):
            raise sqlite3.OperationalError("测试读取故障")
        monkeypatch.setattr(voice.runtime._store, "history_search", fail)
    elif kind == "invalid":
        arguments["limit"] = 0
    voice.tools["查历史。"] = ("history_search", arguments)
    voice.runtime.send_text("查历史。")
    snapshot = await voice.wait(lambda s: len(voice.requests) == 2 and s["messages"][-1]["status"] == "completed")
    assert snapshot["state"]["history_status"] == expected
    assert snapshot["state"]["model_available"]
    result = json.loads(voice.requests[-1]["messages"][-1]["content"])
    assert result["status"] == expected
    if kind == "failure":
        assert "测试读取故障" in snapshot["state"]["history_error"]
    voice.runtime.send_text("继续交流。")
    await voice.wait(lambda s: len(voice.requests) == 3 and s["messages"][-1]["status"] == "completed")


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
@pytest.mark.parametrize("chunk_mode", ["separate", "combined", "interleaved"])
async def test_parallel_calls_keep_completed_result_when_sibling_is_cancelled(voice, monkeypatch, chunk_mode):
    voice.tool_chunk_mode = chunk_mode
    original = voice.runtime._store._history_search
    entered, release = Event(), Event()

    def blocked(keywords, *arguments):
        if keywords == ["慢查询"]:
            entered.set()
            release.wait(8)
        return original(keywords, *arguments)

    monkeypatch.setattr(voice.runtime._store, "_history_search", blocked)
    voice.tools["并行查询。"] = [("history_search", {"keywords": ["杭州"]}), ("history_search", {"keywords": ["慢查询"]})]
    voice.runtime.send_text("并行查询。")
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        assert voice.runtime.connect("test")["state"]["history_status"] == "reading"
        voice.runtime.send_text("停止查找。")
        await voice.wait(lambda s: len(voice.requests) == 2 and s["messages"][-1]["status"] == "completed")
    finally:
        release.set()
    await voice.backend(voice.runtime._flush())
    results = [row["message"] for row in voice.runtime._model_records if row["message"]["role"] == "tool"]
    assert len(results) == 2
    assert json.loads(results[0]["content"])["status"] == "not_found"
    assert results[1]["content"] == "CANCELLED"
    assert len({row["tool_call_id"] for row in results}) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [True], indirect=True)
@pytest.mark.parametrize("takeover", ["text", "voice"])
@pytest.mark.parametrize("origin", ["text", "voice"])
async def test_native_cancel_keeps_completed_result_and_drops_late_io(voice, monkeypatch, takeover, origin):
    voice.tools["已完成查询。"] = ("history_search", {"keywords": ["不存在"]})
    voice.runtime.send_text("已完成查询。")
    await voice.wait(lambda s: s["messages"][-1]["status"] == "completed")
    original = voice.runtime._store._history_search
    entered, release, done = Event(), Event(), Event()

    def blocked(*arguments):
        entered.set()
        release.wait(8)
        try:
            return original(*arguments)
        finally:
            done.set()

    monkeypatch.setattr(voice.runtime._store, "_history_search", blocked)
    voice.tools["慢查询。"] = ("history_search", {"keywords": ["杭州"]})
    if takeover == "voice" or origin == "voice":
        await voice.wake()
    if origin == "voice":
        await voice.transcript("慢查询。")
    else:
        voice.runtime.send_text("慢查询。")
    if takeover == "voice" and origin == "text":
        await voice.resume_voice_input()
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        old = voice.runtime.connect("test")["messages"][-1]["id"]
        assert voice.runtime.connect("test")["state"]["history_status"] == "reading"
        if takeover == "voice":
            # 回读和模型处理期间暂停空闲计时，未播报时新语音直接进入轮次。
            assert "tool" in voice.runtime._lifecycle.busy
            assert voice.runtime._lifecycle.deadline is None
            await voice.transcript("换个问题。")
            voice.release_audio()
        else:
            voice.runtime.send_text("换个问题。")
        snapshot = await voice.wait(lambda s: s["messages"][-1]["id"] != old and s["messages"][-1]["status"] == "completed")
        assert snapshot["state"]["history_status"] == "cancelled"
        assert next(m for m in snapshot["messages"] if m["id"] == old)["status"] == "interrupted"
        assert not voice.classifications
        count = len(voice.requests)
    finally:
        release.set()
    assert await asyncio.to_thread(done.wait, 3)
    await voice.backend(voice.runtime._flush())
    assert len(voice.requests) == count
    results = [row["message"]["content"] for row in voice.runtime._model_records if row["message"]["role"] == "tool"]
    assert len(results) == 2
    assert json.loads(results[0])["status"] == "not_found"
    assert results[1] == "CANCELLED"
