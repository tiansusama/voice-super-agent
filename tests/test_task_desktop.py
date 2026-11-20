import asyncio
import json
import sqlite3
from threading import Event

from aiohttp import web
import pytest

from voice_super_agent.desktop.host import DesktopApi
from voice_super_agent.memory.store import ConversationStore
from voice_super_agent.runtime import Runtime
from voice_super_agent.tasks.mcp_client import MCPExecutionClient
from voice_super_agent.tasks.runtime import TaskRuntime
from voice_super_agent.tasks.store import TaskStore
from voice_super_agent.tooling.interface import BuiltinTools


def test_desktop_task_operations_share_the_runtime_state():
    class TaskService:
        async def start(self):
            self.state = "running"
            self.limit = 2

        async def close(self):
            self.state = "closed"

        async def get_concurrency(self):
            return self.limit

        async def apply_concurrency(self, limit):
            self.limit = limit
            return self.limit

        async def list_tasks(self, cursor, limit):
            assert (cursor, limit) == (9, 20)
            return {"tasks": [{"id": "task", "state": self.state}], "next_cursor": None}

        async def list_changes(self, after_position, limit):
            return []

        async def next_change(self):
            await asyncio.Future()

        async def get_task(self, task_id):
            return {"task": {"id": task_id, "state": self.state}}

        async def task_command(self, task_id, action, payload):
            assert (task_id, action, payload) == ("task", "answer", {"question_id": "question", "text": "确认"})
            self.state = "queued"
            return {"id": task_id, "state": self.state, "source": {"message_id": "message"}}

    tasks = TaskService()
    runtime = Runtime(task_factory=lambda store: tasks)
    runtime.start()
    try:
        api = DesktopApi(runtime, "main")
        initial = api.connect()
        assert initial["state"]["task_concurrency"] == 2
        assert api.list_tasks(9)["tasks"] == [{"id": "task", "state": "running"}]
        api.task_command("task", "answer", {"question_id": "question", "text": "确认"})
        assert api.get_task("task")["task"]["state"] == "queued"
        assert api.save_task_concurrency(1) == 1
        changed = api.connect()
        assert changed["state"]["task_revision"] > initial["state"]["task_revision"]
        assert changed["state"]["task_concurrency"] == 1
    finally:
        runtime.stop()
    assert tasks.state == "closed"


def test_saved_task_notification_is_recovered_once_and_marks_read(tmp_path):
    class TaskService(TaskStore):
        async def start(self):
            pass

        async def close(self):
            pass

        async def next_change(self):
            await asyncio.Future()

    path = tmp_path / "tasks.sqlite3"

    async def seed():
        store = ConversationStore(path)
        await store.open()
        try:
            source = {"message_id": "ask", "reply_id": "reply", "source": "voice"}
            await store.save([{
                "id": "ask", "role": "user", "source": "voice", "reply_id": "reply", "text": "显示小博",
                "status": "completed", "created_at": "2026-11-20T03:00:00+00:00", "error": "",
            }], [], {"display_start_position": 1, "model_start_position": 0})
            tasks = TaskStore(store)
            batch = await tasks.dispatch(source, [{
                "key": "show", "title": "显示窗口", "goal": "显示小博",
                "scope": [{"operation": "window.show", "target": "main"}],
            }])
            detail = await tasks.get_task(batch["tasks"][0]["id"])
            detail["task"].update(state="completed", progress="", result="主窗口已显示", actions=[])
            notification, = await tasks.save_task(detail, [{"kind": "result", "text": "主窗口已显示"}])
            return notification
        finally:
            await store.close()

    notification = asyncio.run(seed())
    for reopened in (False, True):
        runtime = Runtime(database_path=path, task_factory=TaskService)
        runtime.start()
        try:
            api = DesktopApi(runtime, "main")
            message, = api.connect()["messages"]
            assert message["text"] == "显示窗口 · 主窗口已显示"
            assert message["task_notice"]["id"] == notification["id"]
            assert message["task_notice"]["read"] is reopened
            if not reopened:
                api.mark_task_read(notification["id"])
        finally:
            runtime.stop()


def test_quit_waits_for_the_action_and_can_be_withdrawn(tmp_path):
    action_started = Event()
    action_finished = Event()
    path = tmp_path / "quit.sqlite3"

    async def seed():
        store = ConversationStore(path)
        await store.open()
        try:
            await store.save([{
                "id": "ask", "role": "user", "source": "text", "reply_id": "reply", "text": "隐藏小博",
                "status": "completed", "created_at": "2026-11-20T03:00:00+00:00", "error": "",
            }], [], {"display_start_position": 0, "model_start_position": 0})
            batch = await TaskStore(store).dispatch({"message_id": "ask", "reply_id": "reply", "source": "text"}, [{
                "key": "hide", "title": "隐藏小博", "goal": "隐藏小博",
                "scope": [{"operation": "window.hide", "target": "main"}],
            }])
            return batch["tasks"][0]["id"]
        finally:
            await store.close()

    async def set_visibility(visible):
        action_started.set()
        assert await asyncio.to_thread(action_finished.wait, 8)
        return visible

    tools = BuiltinTools(applications=None, set_window_visibility=set_visibility)

    async def worker(execution):
        detail = await execution.get_task()
        async with MCPExecutionClient(tools.create_server(execution, detail["scope"])) as client:
            await client.call_tool("set_main_window", {"operation": "hide"})
        await execution.complete("主窗口已隐藏")

    task_id = asyncio.run(seed())
    runtime = Runtime(database_path=path, task_factory=lambda store: TaskRuntime(store, worker, tools.reconcile_action))
    runtime.start()
    try:
        api = DesktopApi(runtime, "main")
        assert action_started.wait(5)
        api.command("quit")
        while (event := runtime.events.get(timeout=3)).get("command") != "show_main":
            assert event.get("command") != "quit_ready"
        waiting = api.connect()["state"]["task_shutdown"]["waiting"][0]
        assert waiting["task_id"] == task_id and waiting["step_id"]
        assert (waiting["title"], waiting["tool"]) == ("隐藏小博", "set_main_window")
        api.command("withdraw_task_exit")
        assert not api.connect()["state"]["task_shutdown"]["requested"]
        assert api.get_task(task_id)["task"]["state"] == "running"
        api.command("quit")
        while (event := runtime.events.get(timeout=3)).get("command") != "show_main":
            assert event.get("command") != "quit_ready"
        action_finished.set()
        while runtime.events.get(timeout=3).get("command") != "quit_ready":
            pass
        assert api.connect()["state"]["exiting"]
    finally:
        action_finished.set()
        runtime.stop()


def test_task_projection_reports_transient_read_failure_and_keeps_processing():
    class TaskService:
        async def start(self):
            self.events = asyncio.Queue()
            self.unreadable = True

        async def close(self):
            pass

        async def get_concurrency(self):
            return 2

        async def list_changes(self, after_position, limit):
            return []

        async def next_change(self):
            return await self.events.get()

        async def get_task(self, task_id):
            if self.unreadable:
                self.unreadable = False
                raise sqlite3.OperationalError("测试读取暂不可用")
            return {"task": {"id": task_id, "title": "生成清单", "source": {"message_id": "message"}}, "questions": []}

    tasks = TaskService()
    runtime = Runtime(task_factory=lambda store: tasks)
    runtime.start()
    try:
        runtime._loop.call_soon_threadsafe(tasks.events.put_nowait, {
            "id": "result", "position": 1, "task_id": "task", "source": "text", "kind": "result",
            "read": False, "question_id": None, "step_id": None, "text": "产物已保存",
            "created_at": "2026-11-20T03:00:00+00:00",
        })
        while not (snapshot := runtime.events.get(timeout=3)["snapshot"])["state"]["task_error"]:
            pass
        assert "测试读取暂不可用" in snapshot["state"]["task_error"]
        while not (snapshot := runtime.events.get(timeout=3)["snapshot"])["messages"]:
            pass
        assert snapshot["messages"][0]["text"] == "生成清单 · 产物已保存"
        assert snapshot["state"]["task_error"] == ""
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_main_agent_dispatches_from_persisted_user_message(tmp_path, monkeypatch):
    target = str(tmp_path / "报告.txt")
    tool_results = asyncio.Queue()
    tools = BuiltinTools(applications=None)

    async def worker(execution):
        detail = await execution.get_task()
        async with MCPExecutionClient(tools.create_server(execution, detail["scope"])) as session:
            result = await session.call_tool("create_text_file", {"path": target, "content": "测试产物"})
        assert result["structuredContent"]["path"] == target
        await execution.complete("报告已生成")

    async def complete(request):
        body = await request.json()
        message = body["messages"][-1]
        if message["role"] == "tool":
            result = json.loads(message["content"])
            tool_results.put_nowait(result)
            if "tasks" in result:
                return web.json_response({"error": {"message": "任务已保存，但聊天回复失败"}}, status=503)
            delta, finish = {"content": "已收到。"}, "stop"
        else:
            assert any(tool["function"]["name"] == "dispatch_tasks" for tool in body["tools"])
            plans = [{"key": "report", "title": "生成报告", "goal": "生成报告",
                      "scope": [{"operation": "file.create", "target": target}]}]
            delta, finish = {"tool_calls": [{"index": 0, "id": "submit-" + str(tool_results.qsize()) + message["content"],
                                             "type": "function", "function": {"name": "dispatch_tasks",
                                             "arguments": json.dumps({"plans": plans})}}]}, "tool_calls"
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        chunk = {"id": "model", "object": "chat.completion.chunk", "created": 0, "model": "test",
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        await response.write(f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode())
        return response

    app = web.Application()
    app.router.add_post("/chat/completions", complete)
    server = web.AppRunner(app)
    await server.setup()
    site = web.TCPSite(server, "127.0.0.1", 0)
    await site.start()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "task-test-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", f"http://127.0.0.1:{server.addresses[0][1]}")
    runtime = Runtime(task_factory=lambda store: TaskRuntime(store, worker, tools.reconcile_action))
    runtime.start()
    try:
        accepted = runtime.send_text(f"请生成文件「{target}」")
        result = await asyncio.wait_for(tool_results.get(), 5)
        task, = runtime.list_tasks()["tasks"]
        assert result["tasks"][0]["id"] == task["id"]
        assert task["source"] == {"message_id": accepted["user_message_id"], "reply_id": accepted["reply_id"], "source": "text"}
        async with asyncio.timeout(5):
            while not runtime.connect("main")["task_cards"]:
                await asyncio.sleep(.01)
        assert runtime.connect("main")["task_cards"][0]["id"] == task["id"]
        async with asyncio.timeout(5):
            while not any(message["id"] == accepted["reply_id"] and message["status"] == "failed"
                          for message in runtime.connect("main")["messages"]):
                await asyncio.sleep(.01)
        assert runtime.resend_text(accepted["user_message_id"])["existing_task_ids"] == [task["id"]]
        assert sum(message["role"] == "user" for message in runtime.connect("main")["messages"]) == 1
        async with asyncio.timeout(5):
            while not any(message.get("task_notice") for message in runtime.connect("main")["messages"]):
                await asyncio.sleep(.01)
        assert (tmp_path / "报告.txt").read_text(encoding="utf-8") == "测试产物"

        for instruction in ("请告诉我还有哪些任务？", f"请读取「{target}」", f"不要生成「{target}」",
                            f"请生成「{target} 备份.txt」"):
            runtime.send_text(instruction)
            denied = await asyncio.wait_for(tool_results.get(), 5)
            assert denied["status"] == "rejected"
            assert "当前消息" in denied["message"]
        assert [record["id"] for record in runtime.list_tasks()["tasks"]] == [task["id"]]
    finally:
        await asyncio.to_thread(runtime.stop)
        await server.cleanup()
