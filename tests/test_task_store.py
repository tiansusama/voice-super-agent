import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import pytest
import pytest_asyncio

from voice_super_agent.memory.store import ConversationStore
from voice_super_agent.tasks.store import TaskStore


SOURCE = {"message_id": "request-0", "reply_id": "reply-0", "source": "text"}


def plan(key, **values):
    return {"key": key, "title": key, "goal": "按已确认范围完成任务", "scope": [
        {"operation": "file.read", "target": "E:\\任务资料"}], **values}


@pytest_asyncio.fixture
async def task_store(tmp_path):
    owner = ConversationStore(tmp_path / "tasks.sqlite3")
    await owner.open()
    messages = [{"id": f"request-{index}", "role": "user", "source": "text", "reply_id": f"reply-{index}",
                 "text": f"用户已确认的任务{index}", "status": "completed", "error": "",
                 "created_at": "2026-11-20T01:00:00+00:00"} for index in range(3)]
    await owner.save(messages, [], {"display_start_position": 0, "model_start_position": 0})
    try:
        yield TaskStore(owner), owner
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_task_migration_preserves_version_three_conversation_and_settings(tmp_path):
    path = tmp_path / "old.sqlite3"
    scripts = Path(__file__).parents[1] / "src/voice_super_agent/memory/migrations"
    with sqlite3.connect(path) as connection:
        for name in ("V1.0.0_001__init_conversation.sql", "V1.0.0_002__index_history_reply.sql",
                     "V1.0.0_003__interaction_settings.sql"):
            connection.executescript((scripts / name).read_text(encoding="utf-8"))
        connection.execute("INSERT INTO display_messages VALUES (0, 'old-user', 'user', 'text', 'old-reply', '旧原文', 'completed', '2026-11-20', '')")
        connection.execute("UPDATE working_window SET display_start_position = 1")
        connection.execute("INSERT INTO interaction_settings VALUES ('wake_word', ?)", (json.dumps("你好小博"),))
    owner = ConversationStore(path)
    try:
        saved = await owner.open()
        assert saved["messages"][0]["text"] == "旧原文"
        assert saved["window"]["display_start_position"] == 1
        assert await owner.load_settings() == {"wake_word": "你好小博"}
        assert await TaskStore(owner).get_concurrency() == 2
        with sqlite3.connect(path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_batch_is_atomic_and_same_source_is_idempotent_under_concurrent_calls(task_store):
    store, _ = task_store
    plans = [plan("读取"), plan("汇总", depends_on=["读取"])]
    first, duplicate = await asyncio.gather(store.dispatch(SOURCE, plans), store.dispatch(SOURCE, plans))
    assert first == duplicate
    assert len(first["tasks"]) == 2
    detail = await store.get_task(first["tasks"][1]["id"])
    assert detail["depends_on"] == [first["tasks"][0]["id"]]
    assert detail["inputs"][0]["source"] == SOURCE
    assert detail["inputs"][0]["text"] == "用户已确认的任务0"
    assert await store.find_batch(SOURCE["message_id"]) == first
    assert len(await store.list_changes()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [
    {**SOURCE, "message_id": "missing"}, {**SOURCE, "source": "voice"}, {**SOURCE, "reply_id": "other"},
])
async def test_dispatch_requires_matching_persisted_source(task_store, source):
    store, _ = task_store
    with pytest.raises(ValueError, match="已保存的正式用户消息"):
        await store.dispatch(source, [plan("任务")])
    assert (await store.list_tasks())["tasks"] == []


@pytest.mark.asyncio
async def test_unfinished_voice_cannot_dispatch_and_existing_task_dependencies_are_resolved(task_store):
    store, owner = task_store
    saved = await owner.task_io(lambda connection: [dict(row) for row in connection.execute("SELECT * FROM display_messages ORDER BY position")])
    for message in saved:
        del message["position"]
    saved[0]["status"] = "unfinished"
    await owner.save(saved, [], {"display_start_position": 0, "model_start_position": 0})
    with pytest.raises(ValueError, match="正式用户消息"):
        await store.dispatch(SOURCE, [plan("未完成")])
    saved[0]["status"] = "completed"
    await owner.save(saved, [], {"display_start_position": 0, "model_start_position": 0})
    original = await store.dispatch(SOURCE, [plan("原任务")])
    related = await store.dispatch({**SOURCE, "message_id": "request-1", "reply_id": "reply-1"},
                                  [plan("后续", depends_on=[original["tasks"][0]["id"]], related_task_id=original["tasks"][0]["id"])])
    detail = await store.get_task(related["tasks"][0]["id"])
    assert detail["depends_on"] == [original["tasks"][0]["id"]]
    assert detail["related_task_id"] == original["tasks"][0]["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("plans", [[plan("a"), plan("a")], [plan("a", depends_on=["missing"])],
                                   [plan("a", depends_on=["b"]), plan("b", depends_on=["a"])]])
async def test_invalid_dependency_plan_leaves_no_partial_batch(task_store, plans):
    store, _ = task_store
    with pytest.raises(ValueError):
        await store.dispatch(SOURCE, plans)
    assert await store.find_batch(SOURCE["message_id"]) is None
    assert await store.list_changes() == []


@pytest.mark.asyncio
async def test_late_batch_write_failure_rolls_back_tasks_source_and_notifications(task_store, monkeypatch):
    store, _ = task_store
    append = store._append_changes
    count = 0

    def fail_second(*arguments):
        nonlocal count
        count += 1
        if count == 2:
            raise sqlite3.OperationalError("测试第二项保存失败")
        return append(*arguments)

    monkeypatch.setattr(store, "_append_changes", fail_second)
    with pytest.raises(sqlite3.OperationalError):
        await store.dispatch(SOURCE, [plan("a"), plan("b")])
    assert await store.find_batch(SOURCE["message_id"]) is None
    assert (await store.list_tasks())["tasks"] == []
    assert await store.list_changes() == []
    monkeypatch.setattr(store, "_append_changes", append)
    assert len((await store.dispatch(SOURCE, [plan("a"), plan("b")]))["tasks"]) == 2


@pytest.mark.asyncio
async def test_task_facts_and_notification_commit_together_and_unread_survives_reopen(task_store, monkeypatch):
    store, owner = task_store
    batch = await store.dispatch(SOURCE, [plan("任务")])
    detail = await store.get_task(batch["tasks"][0]["id"])
    original = deepcopy(detail)
    detail["task"].update(state="completed", result="实际结果")
    detail["executions"].append({"id": "execution", "state": "completed"})
    detail["steps"].append({"id": "step", "execution_id": "execution", "status": "succeeded", "result": {"text": "原文"}})
    append = store._append_changes

    def failed_append(*arguments):
        raise OSError("测试通知写入失败")

    monkeypatch.setattr(store, "_append_changes", failed_append)
    with pytest.raises(OSError):
        await store.save_task(detail, [{"kind": "result", "text": "实际结果", "step_id": "step"}])
    assert await store.get_task(detail["task"]["id"]) == original
    monkeypatch.setattr(store, "_append_changes", append)
    changes = await store.save_task(detail, [{"kind": "result", "text": "实际结果", "step_id": "step"}])
    reader = ConversationStore(owner._path)
    await reader.open()
    try:
        reopened_view = TaskStore(reader)
        assert (await reopened_view.get_task(detail["task"]["id"]))["steps"] == detail["steps"]
        assert (await reopened_view.list_tasks())["tasks"][0]["unread"]
        assert changes[0]["source"] == "text"
        await reopened_view.mark_read(changes[0]["id"])
    finally:
        await reader.close()
    assert not (await store.get_task(detail["task"]["id"]))["task"]["unread"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mark_read", [False, True])
async def test_state_updates_preserve_notification_read_changes_after_snapshot(task_store, mark_read):
    store, _ = task_store
    batch = await store.dispatch(SOURCE, [plan("任务")])
    task_id = batch["tasks"][0]["id"]
    stale = await store.get_task(task_id)
    changes = await store.save_task(deepcopy(stale), [{"kind": "result", "text": "已保存结果"}])
    if mark_read:
        stale = await store.get_task(task_id)
        await store.mark_read(changes[0]["id"])
    await store.save_task(stale, [{"kind": "state", "text": "进展更新"}])
    assert (await store.get_task(task_id))["task"]["unread"] is not mark_read


@pytest.mark.asyncio
async def test_task_pagination_and_concurrency_preserve_interaction_settings(task_store):
    store, owner = task_store
    await store.dispatch(SOURCE, [plan("a"), plan("b"), plan("c")])
    first = await store.list_tasks(limit=1)
    await store.dispatch({**SOURCE, "message_id": "request-1", "reply_id": "reply-1"}, [plan("后插入")])
    second = await store.list_tasks(cursor=first["next_cursor"], limit=2)
    assert [task["title"] for task in second["tasks"]] == ["b", "a"]
    assert second["next_cursor"] is None
    await owner.save_settings({"wake_word": "你好小博", "voice_idle_seconds": 60})
    assert await store.apply_concurrency(3) == 3
    await owner.save_settings({"wake_word": "小博你好", "voice_idle_seconds": 90})
    assert await store.get_concurrency() == 3
    assert await owner.load_settings() == {"wake_word": "小博你好", "voice_idle_seconds": 90}
    with pytest.raises(ValueError):
        await store.apply_concurrency(True)
