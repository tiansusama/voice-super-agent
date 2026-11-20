import asyncio
import sqlite3

import pytest
import pytest_asyncio

from voice_super_agent.memory.store import ConversationStore
from voice_super_agent.tasks.runtime import TaskRuntime
from voice_super_agent.tasks.store import TaskStore


SOURCE = {"message_id": "request", "reply_id": "reply", "source": "text"}


def plan(key, **values):
    return {"key": key, "title": key, "goal": "完成已确认工作", "scope": [
        {"operation": "window.show", "target": "main"}], **values}


def action(*, cancellable=False, target="main"):
    return {"tool": "show", "arguments": {}, "operation": "window.show", "target": target,
            "resources": [f"window:{target}"], "cancellable": cancellable, "expected": {"visible": True}}


def outcome(status="succeeded"):
    return {"status": status, "result": {"visible": True}, "artifacts": [], "error": ""}


async def uncertain(execution, step):
    return outcome("uncertain")


async def state(runtime, task_id, expected):
    async with asyncio.timeout(3):
        while True:
            detail = await runtime.get_task(task_id)
            if detail["task"]["state"] == expected:
                return detail
            await asyncio.sleep(0.005)


@pytest_asyncio.fixture
async def owner(tmp_path):
    store = ConversationStore(tmp_path / "task.sqlite3")
    await store.open()
    await store.save([{"id": "request", "role": "user", "source": "text", "reply_id": "reply",
                       "text": "请完成这些工作", "status": "completed", "error": "", "created_at": "2026-11-20"}],
                     [], {"display_start_position": 0, "model_start_position": 0})
    try:
        yield store
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_capacity_priority_dependencies_and_lowering_limit(owner):
    started = asyncio.Queue()
    release = {}

    async def worker(execution):
        detail = await execution.get_task()
        name = detail["task"]["title"]
        release[name] = asyncio.Event()
        started.put_nowait(name)
        await release[name].wait()
        await execution.complete(name)

    runtime = TaskRuntime(owner, worker, uncertain)
    await runtime.dispatch(SOURCE, [plan("low"), plan("high", priority=10), plan("after", depends_on=["high"])])
    await runtime.start()
    try:
        assert [await asyncio.wait_for(started.get(), 2) for _ in range(2)] == ["high", "low"]
        await runtime.apply_concurrency(1)
        release["high"].set()
        await asyncio.sleep(0.04)
        assert started.empty()
        release["low"].set()
        assert await asyncio.wait_for(started.get(), 2) == "after"
        release["after"].set()
    finally:
        for event in release.values():
            event.set()
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("command,cancellable", [("pause", True), ("pause", False), ("cancel", False)])
async def test_stop_intent_saved_before_action_stops_and_resources_release(owner, command, cancellable):
    entered, release = asyncio.Event(), asyncio.Event()
    stopped = []

    async def worker(execution):
        step = await execution.start_action(action(cancellable=cancellable))
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            saved = await TaskStore(owner).get_task(execution.task_id)
            stopped.append(saved["task"]["stop_intent"])
            await execution.finish_action(step, outcome("cancelled"))
            raise
        await execution.finish_action(step, outcome())
        # 动作完成后仍在等待的 Worker 必须被运行时停止，不能继续占名额。
        await asyncio.Event().wait()

    runtime = TaskRuntime(owner, worker, uncertain)
    await runtime.start()
    task_id = (await runtime.dispatch(SOURCE, [plan("show")]))["tasks"][0]["id"]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await runtime.task_command(task_id, command)
        if not cancellable:
            assert (await runtime.get_task(task_id))["task"]["state"] == ("pausing" if command == "pause" else "cancelling")
            assert runtime._resources
            release.set()
        detail = await state(runtime, task_id, "paused" if command == "pause" else "cancelled")
        assert detail["steps"][0]["status"] == ("cancelled" if cancellable else "succeeded")
        assert not runtime._resources
        assert stopped == ([command] if cancellable else [])
    finally:
        release.set()
        await runtime.close()


@pytest.mark.asyncio
async def test_same_resource_serializes_actual_actions(owner):
    entered, release = asyncio.Queue(), asyncio.Event()

    async def worker(execution):
        step = await execution.start_action(action())
        entered.put_nowait(execution.task_id)
        await release.wait()
        await execution.finish_action(step, outcome())
        await execution.complete("完成")

    runtime = TaskRuntime(owner, worker, uncertain)
    await runtime.start()
    batch = await runtime.dispatch(SOURCE, [plan("a"), plan("b")])
    try:
        first = await asyncio.wait_for(entered.get(), 2)
        await asyncio.sleep(0.04)
        assert entered.empty()
        release.set()
        assert await asyncio.wait_for(entered.get(), 2) != first
        for task in batch["tasks"]:
            await state(runtime, task["id"], "completed")
    finally:
        release.set()
        await runtime.close()


@pytest.mark.asyncio
async def test_waiting_answer_requeues_under_capacity_and_keeps_pause_intent(owner):
    blocker = asyncio.Event()
    asked = asyncio.Event()

    async def worker(execution):
        detail = await execution.get_task()
        if detail["task"]["title"] == "question" and not detail["questions"]:
            await execution.ask("请选择目标")
            asked.set()
        elif detail["task"]["title"] == "blocker":
            await blocker.wait()
            await execution.complete("完成")
        else:
            await execution.complete("已回答")

    runtime = TaskRuntime(owner, worker, uncertain)
    await runtime.apply_concurrency(1)
    await runtime.start()
    batch = await runtime.dispatch(SOURCE, [plan("question"), plan("blocker")])
    task_id = batch["tasks"][0]["id"]
    try:
        await asyncio.wait_for(asked.wait(), 2)
        await state(runtime, batch["tasks"][1]["id"], "running")
        detail = await runtime.get_task(task_id)
        question_id = detail["questions"][0]["id"]
        with pytest.raises(ValueError, match="对应"):
            await runtime.task_command(task_id, "answer", {"text": "选 A", "question_id": "other"})
        await runtime.task_command(task_id, "pause")
        await runtime.task_command(task_id, "answer", {"text": "选 A", "question_id": question_id})
        assert (await runtime.get_task(task_id))["task"]["state"] == "paused"
        await runtime.task_command(task_id, "resume")
        await asyncio.sleep(0.02)
        assert len((await runtime.get_task(task_id))["executions"]) == 1
        blocker.set()
        await state(runtime, task_id, "completed")
    finally:
        blocker.set()
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("inspection_error", [False, True])
async def test_reboot_reconciles_uncertain_step_without_replay_and_preserves_cancel(owner, inspection_error):
    store = TaskStore(owner)
    task_id = (await store.dispatch(SOURCE, [plan("cancelled"), plan("independent")]))["tasks"][0]["id"]
    detail = await store.get_task(task_id)
    detail["task"].update(state="cancelling", stop_intent="cancel", stop_reason="user", execution_id="old")
    detail["executions"].append({"id": "old", "state": "running"})
    detail["steps"].append({"id": "step", "execution_id": "old", "request": action(), "status": "started", "outcome": None})
    await store.save_task(detail, [])
    ran = []

    async def worker(execution):
        ran.append(execution.task_id)
        await execution.complete("独立任务已完成")

    async def reconcile(execution, step):
        if inspection_error:
            raise OSError("目标暂时不可查询")
        return await uncertain(execution, step)

    runtime = TaskRuntime(owner, worker, reconcile)
    await runtime.start()
    try:
        detail = await state(runtime, task_id, "waiting_user")
        assert detail["task"]["stop_intent"] == "cancel"
        assert runtime._resources
        if inspection_error:
            count = len(detail["executions"])
            await runtime.task_command(task_id, "cancel")
            async with asyncio.timeout(2):
                while True:
                    updated = await runtime.get_task(task_id)
                    if updated["task"]["state"] == "waiting_user" and len(updated["executions"]) > count:
                        break
                    await asyncio.sleep(0.005)
            assert len(updated["questions"]) == 1
        await runtime.task_command(task_id, "answer", {"question_id": detail["questions"][0]["id"],
                                                        "text": "已经显示", "resolution": "succeeded"})
        assert (await runtime.get_task(task_id))["task"]["state"] == "cancelled"
        assert task_id not in ran
        assert not runtime._resources
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("old_state", ["running", "paused"])
async def test_recovery_releases_old_resource_when_higher_priority_waiters_fill_slots(owner, old_state):
    store = TaskStore(owner)
    batch = await store.dispatch(SOURCE, [plan("recovery"), plan("b", priority=10), plan("c", priority=10)])
    task_id = batch["tasks"][0]["id"]
    detail = await store.get_task(task_id)
    detail["task"].update(state=old_state, execution_id="old", stop_intent="pause" if old_state == "paused" else None)
    detail["executions"].append({"id": "old", "state": "running"})
    detail["steps"].append({"id": "old-step", "execution_id": "old", "status": "started", "outcome": None, "request": action()})
    await store.save_task(detail, [])

    async def worker(execution):
        step = await execution.start_action(action())
        await execution.finish_action(step, outcome())
        await execution.complete("已完成")

    async def reconcile(execution, step):
        return outcome("not_executed")

    runtime = TaskRuntime(owner, worker, reconcile)
    await runtime.start()
    try:
        if old_state == "paused":
            async with asyncio.timeout(2):
                while len(runtime._running) < 2:
                    await asyncio.sleep(0.005)
            await runtime.task_command(task_id, "resume")
        for task in batch["tasks"]:
            await state(runtime, task["id"], "completed")
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_resource_waiters_release_capacity_and_obey_limit_when_ready(owner):
    store = TaskStore(owner)
    batch = await store.dispatch(SOURCE, [
        plan("recovery"), plan("b", priority=10), plan("c", priority=10),
        plan("independent", scope=[{"operation": "window.show", "target": "other"}]),
    ])
    ids = {task["title"]: task["id"] for task in batch["tasks"]}
    detail = await store.get_task(ids["recovery"])
    detail["task"].update(state="cancelling", stop_intent="cancel", stop_reason="user", execution_id="old")
    detail["executions"].append({"id": "old", "state": "running"})
    detail["steps"].append({"id": "old-step", "execution_id": "old", "status": "started", "outcome": None, "request": action()})
    await store.save_task(detail, [])
    entered = asyncio.Queue()
    release = asyncio.Event()

    async def worker(execution):
        independent = execution.task_id == ids["independent"]
        step = await execution.start_action(action(target="other" if independent else "main"))
        entered.put_nowait(execution.task_id)
        if independent:
            await release.wait()
        await execution.finish_action(step, outcome())
        await execution.complete("已完成")

    runtime = TaskRuntime(owner, worker, uncertain)
    await runtime.start()
    try:
        recovery = await state(runtime, ids["recovery"], "waiting_user")
        assert await asyncio.wait_for(entered.get(), 1) == ids["independent"]
        assert (await runtime.get_task(ids["independent"]))["task"]["progress"] == "正在执行工具：show"
        for name in ("b", "c"):
            assert (await runtime.get_task(ids[name]))["task"]["progress"] == "等待本机资源"
        await runtime.apply_concurrency(1)
        await runtime.task_command(ids["recovery"], "answer", {
            "question_id": recovery["questions"][0]["id"], "text": "结束旧步骤", "resolution": "cancelled",
        })
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(entered.get(), 0.05)
        release.set()
        assert {await asyncio.wait_for(entered.get(), 2) for _ in range(2)} == {ids["b"], ids["c"]}
        for name in ("independent", "b", "c"):
            await state(runtime, ids[name], "completed")
    finally:
        release.set()
        await runtime.close()


@pytest.mark.asyncio
async def test_scheduler_reports_read_error_and_continues_after_storage_recovers(owner, monkeypatch):
    store = TaskStore(owner)
    task_id = (await store.dispatch(SOURCE, [plan("pending")]))["tasks"][0]["id"]
    load = TaskStore.load_unfinished
    reads = 0

    async def fail_first_scheduler_read(self):
        nonlocal reads
        reads += 1
        if reads == 2:
            raise sqlite3.OperationalError("测试调度读取失败")
        return await load(self)

    async def worker(execution):
        await execution.complete("任务已完成")

    monkeypatch.setattr(TaskStore, "load_unfinished", fail_first_scheduler_read)
    runtime = TaskRuntime(owner, worker, uncertain)
    await runtime.start()
    try:
        error = await asyncio.wait_for(runtime.next_change(), 1)
        assert error["kind"] == "scheduler_error"
        assert "测试调度读取失败" in error["error"]
        assert (await state(runtime, task_id, "completed"))["task"]["result"] == "任务已完成"
        async with asyncio.timeout(2):
            while (update := await runtime.next_change())["kind"] != "scheduler_error":
                pass
        assert update["error"] == ""
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_successful_action_write_failure_retains_fact_and_retry_does_not_repeat(owner, monkeypatch):
    executed = []
    original = TaskStore.save_task
    fail = True

    async def save(store, detail, changes):
        if fail and any(step["status"] == "succeeded" for step in detail["steps"]):
            raise sqlite3.OperationalError("磁盘已满")
        return await original(store, detail, changes)

    async def worker(execution):
        detail = await execution.get_task()
        if not detail["steps"]:
            step = await execution.start_action(action())
            executed.append(step)
            await execution.finish_action(step, outcome())
        await execution.complete("已显示")

    runtime = TaskRuntime(owner, worker, uncertain)
    monkeypatch.setattr(TaskStore, "save_task", save)
    await runtime.start()
    task_id = (await runtime.dispatch(SOURCE, [plan("show")]))["tasks"][0]["id"]
    try:
        detail = await state(runtime, task_id, "storage_error")
        assert detail["steps"][0]["outcome"] == outcome()
        assert (await TaskStore(owner).get_task(task_id))["steps"][0]["status"] == "started"
        assert runtime._resources
        async with asyncio.timeout(2):
            while runtime._running:
                await asyncio.sleep(0.005)
        fail = False
        with pytest.raises(RuntimeError, match="尚未保存"):
            await runtime.close()
        assert not (await runtime.request_shutdown())["ready"]
        assert (await runtime.get_task(task_id))["steps"][0]["outcome"] == outcome()
        await runtime.withdraw_shutdown()
        await runtime.task_command(task_id, "retry")
        await state(runtime, task_id, "completed")
        assert len(executed) == 1
        assert not runtime._resources
    finally:
        fail = False
        await runtime.close()


@pytest.mark.asyncio
async def test_shutdown_withdraw_keeps_uncancellable_action_and_user_pause(owner):
    entered, release = asyncio.Event(), asyncio.Event()

    async def worker(execution):
        step = await execution.start_action(action())
        entered.set()
        await release.wait()
        await execution.finish_action(step, outcome())
        await execution.complete("已显示")

    runtime = TaskRuntime(owner, worker, uncertain)
    batch = await runtime.dispatch(SOURCE, [plan("active"), plan("paused")])
    active, paused = [task["id"] for task in batch["tasks"]]
    await runtime.task_command(paused, "pause")
    await runtime.start()
    try:
        await asyncio.wait_for(entered.wait(), 2)
        progress = await runtime.request_shutdown()
        assert not progress["ready"] and progress["waiting"][0]["task_id"] == active
        await runtime.withdraw_shutdown()
        release.set()
        await state(runtime, active, "completed")
        assert len((await runtime.get_task(active))["executions"]) == 1
        assert (await runtime.get_task(paused))["task"]["state"] == "paused"
    finally:
        release.set()
        await runtime.close()


@pytest.mark.asyncio
async def test_untrusted_target_waits_for_user(owner):
    async def worker(execution):
        await execution.start_action(action(target="another-window"))

    runtime = TaskRuntime(owner, worker, uncertain)
    await runtime.start()
    task_id = (await runtime.dispatch(SOURCE, [plan("show")]))["tasks"][0]["id"]
    try:
        detail = await state(runtime, task_id, "waiting_user")
        assert detail["steps"] == []
        assert "another-window" in detail["questions"][0]["text"]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_retry_storage_error_keeps_saved_user_stop_intent(owner):
    store = TaskStore(owner)
    task_id = (await store.dispatch(SOURCE, [plan("stopped")]))["tasks"][0]["id"]
    detail = await store.get_task(task_id)
    detail["task"].update(state="storage_error", stop_intent="cancel", stop_reason="user")
    await store.save_task(detail, [])
    ran = []

    async def worker(execution):
        ran.append(execution.task_id)
        await execution.complete("完成")

    runtime = TaskRuntime(owner, worker, uncertain)
    await runtime.task_command(task_id, "retry")
    assert (await runtime.get_task(task_id))["task"]["stop_intent"] == "cancel"
    await runtime.start()
    try:
        await state(runtime, task_id, "cancelled")
        assert not ran
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_cancelled_dispatch_waits_for_commit_and_tasks_continue(owner, monkeypatch):
    committed, release, worked = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = TaskStore.dispatch

    async def dispatch(store, source, plans):
        batch = await original(store, source, plans)
        committed.set()
        await release.wait()
        return batch

    async def worker(execution):
        worked.set()
        await execution.complete("已完成")

    monkeypatch.setattr(TaskStore, "dispatch", dispatch)
    runtime = TaskRuntime(owner, worker, uncertain)
    await runtime.start()
    job = asyncio.create_task(runtime.dispatch(SOURCE, [plan("show")]))
    try:
        await asyncio.wait_for(committed.wait(), 2)
        job.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await job
        await asyncio.wait_for(worked.wait(), 2)
        original_batch = await runtime.find_batch("request")
        assert (await runtime.dispatch(SOURCE, [plan("duplicate")]))["tasks"][0]["id"] == original_batch["tasks"][0]["id"]
    finally:
        release.set()
        await runtime.close()
