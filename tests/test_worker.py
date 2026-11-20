import asyncio
import json
import sqlite3
from threading import Event
from uuid import uuid4

import pytest
import pytest_asyncio
import httpx
from openai import AsyncOpenAI
import anyio
from anyio.lowlevel import checkpoint_if_cancelled
from mcp import types
from mcp.server import Server

from voice_super_agent.memory.context import WorkingContext
from voice_super_agent.memory.store import ConversationStore
from voice_super_agent.tasks.recovery import ActionReconciler
from voice_super_agent.tasks.runtime import TaskRuntime
from voice_super_agent.tasks.store import TaskStore
from voice_super_agent.tasks.worker import TaskWorker
from voice_super_agent.tooling.files import FileTools
from voice_super_agent.tooling.interface import BuiltinTools


SOURCE = {"message_id": "source", "reply_id": "reply", "source": "text"}


def call(name, **arguments):
    return {"role": "assistant", "tool_calls": [{"id": uuid4().hex, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}


def definition(params):
    return json.loads(params["messages"][0]["content"].split("\n任务定义：\n", 1)[1])


def plan(name, scopes, **values):
    return {"key": name, "title": name, "goal": f"完成{name}",
            "scope": [{"operation": operation, "target": str(path)} for operation, path in scopes], **values}


async def state(runtime, task_id, expected):
    async with asyncio.timeout(4):
        while True:
            detail = await runtime.get_task(task_id)
            if detail["task"]["state"] == expected:
                return detail
            await asyncio.sleep(0.005)


@pytest_asyncio.fixture
async def owner(tmp_path):
    store = ConversationStore(tmp_path / "tasks.sqlite3")
    await store.open()
    await store.save([{"id": name, "role": "user", "reply_id": reply, "text": name * 100,
                       "source": "text", "status": "completed", "created_at": "2026-11-20", "error": ""}
                      for name, reply in [("source", "reply"), ("reference", "ref"), ("secret", "unrelated")]],
                     [], {"display_start_position": 0, "model_start_position": 0})
    try:
        yield store
    finally:
        await store.close()


def runtime_with(owner, completion):
    tools = BuiltinTools(None)
    return TaskRuntime(owner, TaskWorker(tools.create_server, completion=completion), ActionReconciler(tools.inspect_action))


@pytest.mark.asyncio
async def test_worker_reads_and_creates_file_through_real_mcp_then_restores_saved_facts(owner, tmp_path):
    source, output = tmp_path / "source.txt", tmp_path / "result.md"
    source.write_text("真实资料", encoding="utf-8")
    requests = []

    async def model(params):
        requests.append(params)
        if len(requests) == 1:
            return call("read_text_file", path=str(source), final_step=False)
        if len(requests) == 2:
            assert "真实资料" in params["messages"][-1]["content"]
            return call("create_text_file", path=str(output), content="# 已读真实资料", final_step=True)
        return call("task_complete", text=f"已生成 {output}")

    runtime = runtime_with(owner, model)
    await runtime.start()
    task_id = (await runtime.dispatch(SOURCE, [plan("整理", [("file.read", source), ("file.create", output)])]))["tasks"][0]["id"]
    try:
        detail = await state(runtime, task_id, "completed")
        assert output.read_text(encoding="utf-8") == "# 已读真实资料"
        assert [step["status"] for step in detail["steps"]] == ["succeeded", "succeeded"]
        assert all(step["request"]["request_id"] is not None and step["model_call_id"] for step in detail["steps"])
        assert detail["artifacts"] == [{"kind": "file", "path": str(output)}]
        assert all(WorkingContext.size(params["messages"]) + WorkingContext.size(params["tools"]) <= 65536 for params in requests)
    finally:
        await runtime.close()
    restored = runtime_with(owner, model)
    await restored.start()
    try:
        assert (await restored.get_task(task_id))["task"]["state"] == "completed"
        assert len(requests) == 3
        assert (await restored.find_batch("source"))["tasks"][0]["id"] == task_id
    finally:
        await restored.close()


@pytest.mark.asyncio
async def test_worker_contexts_and_real_results_are_isolated(owner, tmp_path):
    outputs = {name: tmp_path / f"{name}.txt" for name in ("one", "two")}
    seen = {}

    async def model(params):
        initial = definition(params)
        title = initial["title"]
        seen[title] = seen.get(title, 0) + 1
        other = "two" if title == "one" else "one"
        assert str(outputs[other]) not in json.dumps(params["messages"], ensure_ascii=False)
        if seen[title] == 1:
            await asyncio.sleep(0.01)
            return call("create_text_file", path=str(outputs[title]), content=title, final_step=True)
        return call("task_complete", text=title)

    runtime = runtime_with(owner, model)
    await runtime.start()
    batch = await runtime.dispatch(SOURCE, [plan(name, [("file.create", path)]) for name, path in outputs.items()])
    try:
        for task in batch["tasks"]:
            detail = await state(runtime, task["id"], "completed")
            assert detail["task"]["result"] == detail["task"]["title"]
            assert len(detail["steps"]) == 1
        assert {name: path.read_text(encoding="utf-8") for name, path in outputs.items()} == {"one": "one", "two": "two"}
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_supplement_during_model_request_replans_before_tool_runs(owner, tmp_path):
    old, new = tmp_path / "old.txt", tmp_path / "new.txt"
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def model(params):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            return call("create_text_file", path=str(old), content="old", final_step=True)
        if calls == 2:
            assert "改用新文件" in params["messages"][-1]["content"]
            return call("create_text_file", path=str(new), content="new", final_step=True)
        return call("task_complete", text="已按补充生成新文件")

    runtime = runtime_with(owner, model)
    await runtime.start()
    task_id = (await runtime.dispatch(SOURCE, [plan("生成", [("file.create", old), ("file.create", new)])]))["tasks"][0]["id"]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await runtime.task_command(task_id, "supplement", {"text": "改用新文件"})
        release.set()
        detail = await state(runtime, task_id, "completed")
        assert not old.exists() and new.read_text(encoding="utf-8") == "new"
        assert len(detail["steps"]) == 1
        assert all(item["consumed"] for item in detail["inputs"])
    finally:
        release.set()
        await runtime.close()


@pytest.mark.asyncio
async def test_supplement_during_final_native_action_preserves_original_and_notifies(owner, tmp_path, monkeypatch):
    output = tmp_path / "original.csv"
    entered, release = asyncio.Event(), Event()
    loop = asyncio.get_running_loop()
    original = FileTools.create_text

    def create(files, path, content):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(3)
        return original(files, path, content)

    monkeypatch.setattr(FileTools, "create_text", create)
    calls = 0

    async def model(params):
        nonlocal calls
        calls += 1
        if calls == 1:
            return call("create_text_file", path=str(output), content="a,b\n1,2", final_step=True)
        assert "再加一列" not in json.dumps(params["messages"], ensure_ascii=False)
        return call("task_complete", text="原文件已生成")

    runtime = runtime_with(owner, model)
    await runtime.start()
    task_id = (await runtime.dispatch(SOURCE, [plan("CSV", [("file.create", output)])]))["tasks"][0]["id"]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await runtime.task_command(task_id, "supplement", {"text": "再加一列"})
        release.set()
        detail = await state(runtime, task_id, "completed")
        assert output.read_text(encoding="utf-8") == "a,b\n1,2"
        assert not detail["inputs"][-1]["consumed"]
        assert detail["inputs"][-1]["after_step"] == detail["steps"][0]["id"]
        assert any("关联任务" in change["text"] for change in await runtime.list_changes())
    finally:
        release.set()
        await runtime.close()


@pytest.mark.asyncio
async def test_worker_write_failure_after_real_file_creation_then_retry_preserves_file(owner, tmp_path, monkeypatch):
    output = tmp_path / "result.txt"
    fail = True
    original = TaskStore.save_task

    async def save(store, detail, changes):
        if fail and any(step["status"] == "succeeded" for step in detail["steps"]):
            raise sqlite3.OperationalError("写入失败")
        return await original(store, detail, changes)

    monkeypatch.setattr(TaskStore, "save_task", save)
    calls = 0

    async def model(params):
        nonlocal calls
        calls += 1
        if calls == 1:
            return call("create_text_file", path=str(output), content="完整原文", final_step=True)
        assert "succeeded" in params["messages"][1]["content"]
        return call("task_complete", text="文件已核对并保存")

    runtime = runtime_with(owner, model)
    await runtime.start()
    task_id = (await runtime.dispatch(SOURCE, [plan("生成", [("file.create", output)])]))["tasks"][0]["id"]
    try:
        await state(runtime, task_id, "storage_error")
        assert output.read_text(encoding="utf-8") == "完整原文"
        async with asyncio.timeout(2):
            while runtime._running:
                await asyncio.sleep(0.005)
        fail = False
        await runtime.task_command(task_id, "retry")
        detail = await state(runtime, task_id, "completed")
        assert len(detail["steps"]) == 1 and calls == 2
        assert len(detail["executions"]) == 2
    finally:
        fail = False
        await runtime.close()


@pytest.mark.asyncio
async def test_worker_restricted_history_and_task_read(owner):
    reads = []

    async def worker(execution):
        reads.append(await execution.read("history", "source", offset=4, limit=10))
        reads.append(await execution.read("history", "reference", limit=12))
        with pytest.raises(PermissionError):
            await execution.read("history", "secret")
        with pytest.raises(PermissionError):
            await execution.read("task", "unrelated-task")
        await execution.complete("已回读")

    tools = BuiltinTools(None)
    runtime = TaskRuntime(owner, worker, ActionReconciler(tools.inspect_action))
    await runtime.start()
    task_id = (await runtime.dispatch(SOURCE, [plan("回读", [("window.show", "main")], references=["reference"])]))["tasks"][0]["id"]
    try:
        await state(runtime, task_id, "completed")
        assert reads[0]["next_offset"] == 14
        assert reads[1]["text"] == "referenceref"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_long_tool_loop_stays_bounded_with_original_results_readable(owner, tmp_path):
    source = tmp_path / "long.txt"
    source.write_text("历史正文" * 1000, encoding="utf-8")
    calls, request_sizes = 0, []

    async def model(params):
        nonlocal calls
        calls += 1
        request_sizes.append(WorkingContext.size(params["messages"]) + WorkingContext.size(params["tools"]))
        assert definition(params)["goal"] == "完成读取"
        assert definition(params)["task_id"]
        if calls == 13:
            await runtime.task_command(definition(params)["task_id"], "supplement", {"text": "继续回读并保留原目标"})
        if calls <= 16:
            return call("read_text_file", path=str(source), limit=2000, final_step=False)
        return call("task_complete", text="已读取")

    runtime = runtime_with(owner, model)
    await runtime.start()
    task_id = (await runtime.dispatch(SOURCE, [plan("读取", [("file.read", source)])]))["tasks"][0]["id"]
    try:
        detail = await state(runtime, task_id, "completed")
        assert max(request_sizes) <= 65536
        assert len(detail["steps"]) == 15
        assert "历史正文" in detail["executions"][0]["messages"][4]["content"]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_real_openai_client_serializes_worker_tool_loop(owner, tmp_path, monkeypatch):
    output = tmp_path / "sdk.txt"
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.url.path == "/v1/chat/completions"
        assert body["model"] == "deepseek-flash" and body["thinking"] == {"type": "disabled"}
        assert body["max_tokens"] == 8192 and body["parallel_tool_calls"] is False
        message = (call("create_text_file", path=str(output), content="SDK真实序列化", final_step=True)
                   if len(requests) == 1 else call("task_complete", text="SDK请求完成"))
        return httpx.Response(200, json={"id": "completion", "created": 1, "model": "deepseek-flash",
            "object": "chat.completion", "choices": [{"index": 0, "finish_reason": "tool_calls", "message": message}]})

    def client(**options):
        return AsyncOpenAI(**options, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))

    monkeypatch.setattr("voice_super_agent.tasks.worker.AsyncOpenAI", client)
    tools = BuiltinTools(None)
    runtime = TaskRuntime(owner, TaskWorker(tools.create_server, api_key="test-key"), ActionReconciler(tools.inspect_action))
    await runtime.start()
    task_id = (await runtime.dispatch(SOURCE, [plan("SDK", [("file.create", output)])]))["tasks"][0]["id"]
    try:
        await state(runtime, task_id, "completed")
        assert output.read_text(encoding="utf-8") == "SDK真实序列化"
        assert requests[1]["messages"][-1]["role"] == "tool"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_restart_after_unrecorded_file_publish_waits_confirmation_and_other_task_continues(owner, tmp_path):
    existing, independent = tmp_path / "existing.txt", tmp_path / "independent.txt"
    existing.write_text("原内容", encoding="utf-8")
    store = TaskStore(owner)
    batch = await store.dispatch(SOURCE, [plan("恢复", [("file.create", existing)]), plan("独立", [("file.create", independent)])])
    old_id, independent_id = [task["id"] for task in batch["tasks"]]
    detail = await store.get_task(old_id)
    detail["task"].update(state="running", execution_id="old-execution")
    detail["executions"].append({"id": "old-execution", "state": "running"})
    detail["steps"].append({"id": "published", "execution_id": "old-execution", "status": "started", "outcome": None,
                            "request": {"tool": "create_text_file", "operation": "file.create", "target": str(existing),
                                        "resources": [f"file:{str(existing).casefold()}"], "cancellable": False,
                                        "arguments": {"path": str(existing)}, "expected": {"path": str(existing)}}})
    await store.save_task(detail, [])
    requests = []

    async def model(params):
        initial = definition(params)
        requests.append(initial["title"])
        if initial["title"] == "独立" and len(params["messages"]) == 3:
            return call("create_text_file", path=str(independent), content="独立工作", final_step=True)
        return call("task_complete", text="已核对完成")

    runtime = runtime_with(owner, model)
    await runtime.start()
    try:
        detail = await state(runtime, old_id, "waiting_user")
        await state(runtime, independent_id, "completed")
        assert requests == ["独立", "独立"]
        assert existing.read_text(encoding="utf-8") == "原内容"
        assert detail["artifacts"] == []
        assert detail["steps"][0]["outcome"]["result"]["current_observation"]["exists"] is True
        question = detail["questions"][0]
        with pytest.raises(ValueError, match="明确确认"):
            await runtime.task_command(old_id, "answer", {"question_id": question["id"], "text": "我看到了"})
        await runtime.task_command(old_id, "answer", {"question_id": question["id"], "text": "文件是这次生成的",
                                                       "resolution": "succeeded"})
        detail = await state(runtime, old_id, "completed")
        assert len(detail["steps"]) == 1
        assert detail["artifacts"] == [{"kind": "file", "path": str(existing)}]
        assert existing.read_text(encoding="utf-8") == "原内容"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_consumed_supplement_survives_worker_failure_and_retry(owner):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def model(params):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            return call("task_complete", text="旧要求")
        if calls == 2:
            assert "采用蓝色标题" in params["messages"][-1]["content"]
            return call("task_read", kind="history", id="source")
        if calls == 3:
            raise RuntimeError("模型临时故障")
        assert "采用蓝色标题" in params["messages"][1]["content"]
        return call("task_complete", text="已继承蓝色标题要求")

    runtime = runtime_with(owner, model)
    await runtime.start()
    task_id = (await runtime.dispatch(SOURCE, [plan("补充", [("window.show", "main")])]))["tasks"][0]["id"]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await runtime.task_command(task_id, "supplement", {"text": "采用蓝色标题"})
        release.set()
        detail = await state(runtime, task_id, "failed")
        assert detail["inputs"][-1]["consumed"]
        await runtime.task_command(task_id, "retry")
        detail = await state(runtime, task_id, "completed")
        assert detail["task"]["result"] == "已继承蓝色标题要求"
        assert len(detail["executions"]) == 2
    finally:
        release.set()
        await runtime.close()


@pytest.mark.asyncio
async def test_malformed_model_tool_arguments_fail_visibly_and_retry_is_available(owner):
    async def model(params):
        return call("task_complete")

    runtime = runtime_with(owner, model)
    await runtime.start()
    task_id = (await runtime.dispatch(SOURCE, [plan("错误参数", [("window.show", "main")])]))["tasks"][0]["id"]
    try:
        detail = await state(runtime, task_id, "failed")
        assert "Worker 工具参数无效" in detail["task"]["error"]
        assert "retry" in detail["task"]["actions"]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_runtime_cancels_real_mcp_action_then_saves_actual_stop(owner):
    entered = asyncio.Event()

    def server_factory(access, scope):
        server = Server("cancellable-action")

        @server.list_tools()
        async def tools():
            return [types.Tool(name="show", inputSchema={"type": "object", "properties": {}})]

        @server.call_tool()
        async def run(name, arguments):
            step_id = await access.start_action({"tool": name, "arguments": arguments, "operation": "window.show",
                "target": "main", "resources": ["window:main"], "cancellable": True, "expected": {"visible": True}})
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                with anyio.CancelScope(shield=True):
                    await access.finish_action(step_id, {"status": "cancelled", "result": {"actually_stopped": True},
                                                        "artifacts": [], "error": ""})
                raise
            finally:
                await checkpoint_if_cancelled()

        return server

    async def model(params):
        return call("show", final_step=True)

    async def reconcile(execution, step):
        raise AssertionError("本轮尚未发生重启核对")

    runtime = TaskRuntime(owner, TaskWorker(server_factory, completion=model), reconcile)
    await runtime.start()
    task_id = (await runtime.dispatch(SOURCE, [plan("停止", [("window.show", "main")])]))["tasks"][0]["id"]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await runtime.task_command(task_id, "cancel")
        detail = await state(runtime, task_id, "cancelled")
        assert detail["steps"][0]["outcome"]["result"] == {"actually_stopped": True}
        assert not runtime._resources
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_existing_application_window_is_insufficient_recovery_evidence():
    async def inspect(request):
        return {"window": 123, "target": request["target"]}

    result = await ActionReconciler(inspect)(None, {"request": {
        "operation": "application.open", "target": "existing-minimized-app", "expected": {"application": "existing-minimized-app"}}})
    assert result["status"] == "uncertain" and result["result"]["current_observation"]["window"] == 123


@pytest.mark.asyncio
@pytest.mark.parametrize("choices", [[], [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant"}}]])
async def test_empty_model_response_is_persisted_as_retryable_failure(owner, monkeypatch, choices):
    responses = iter([choices, [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": "重试完成"}}]])

    def respond(request):
        return httpx.Response(200, json={"id": "reply", "created": 0, "model": "test",
                                         "object": "chat.completion", "choices": next(responses)})

    def client(**options):
        return AsyncOpenAI(**options, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))

    monkeypatch.setattr("voice_super_agent.tasks.worker.AsyncOpenAI", client)
    tools = BuiltinTools(None)
    worker = TaskWorker(tools.create_server, api_key="test", base_url="http://model.test", model="test")
    runtime = TaskRuntime(owner, worker, ActionReconciler(tools.inspect_action))
    await runtime.start()
    try:
        batch = await runtime.dispatch(SOURCE, [plan("空回复", [("window.show", "main")])])
        task_id = batch["tasks"][0]["id"]
        failed = await state(runtime, task_id, "failed")
        assert "有效回复" in failed["task"]["error"]
        assert "retry" in (await runtime.get_task(task_id))["task"]["actions"]
        await runtime.task_command(task_id, "retry")
        completed = await state(runtime, task_id, "completed")
        assert completed["task"]["result"] == "重试完成"
        assert len(completed["executions"]) == 2
    finally:
        await runtime.close()
