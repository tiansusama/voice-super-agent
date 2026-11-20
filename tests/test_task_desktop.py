import asyncio
import json
import sqlite3
from threading import Event

from aiohttp import web
import pytest

from voice_super_agent.desktop.host import DesktopApi
from voice_super_agent.memory.store import ConversationStore
from voice_super_agent.runtime import Runtime
from voice_super_agent.task_tools import replacement_file_scope, task_command_intent
from voice_super_agent.tasks.mcp_client import MCPExecutionClient
from voice_super_agent.tasks.recovery import ActionReconciler
from voice_super_agent.tasks.runtime import TaskRuntime
from voice_super_agent.tasks.store import TaskStore
from voice_super_agent.tasks.worker import TaskWorker
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

        async def get_task(self, task_id):
            return {"task": {"id": task_id, "state": self.state},
                    "questions": [{"id": "question", "status": "open"}]}

        async def task_command(self, task_id, action, payload):
            assert (task_id, action, payload) == ("task", "answer", {"question_id": "question", "text": "确认"})
            self.state = "queued"
            return {"id": task_id, "state": self.state, "source": {"message_id": "message"}}

    tasks = TaskService()
    runtime = Runtime(task_factory=lambda store, on_change: tasks)
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
        runtime = Runtime(database_path=path, task_factory=lambda store, on_change: TaskService(store))
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
    runtime = Runtime(database_path=path, task_factory=lambda store, on_change: TaskRuntime(
        store, worker, ActionReconciler(tools.inspect_action), on_change=on_change,
    ))
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
            self.unreadable = True

        async def close(self):
            pass

        async def get_concurrency(self):
            return 2

        async def list_changes(self, after_position, limit):
            return []

        async def get_task(self, task_id):
            if self.unreadable:
                self.unreadable = False
                raise sqlite3.OperationalError("测试读取暂不可用")
            return {"task": {"id": task_id, "title": "生成清单", "source": {"message_id": "message"}}, "questions": []}

    tasks = TaskService()

    def task_factory(store, on_change):
        tasks.notify = on_change
        return tasks

    runtime = Runtime(task_factory=task_factory)
    runtime.start()
    try:
        asyncio.run_coroutine_threadsafe(tasks.notify({
            "id": "result", "position": 1, "task_id": "task", "source": "text", "kind": "result",
            "read": False, "question_id": None, "step_id": None, "text": "产物已保存",
            "created_at": "2026-11-20T03:00:00+00:00",
        }), runtime._loop).result()
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
@pytest.mark.parametrize("dispatch_on_retry", [False, True])
async def test_main_agent_dispatches_from_persisted_user_message(tmp_path, monkeypatch, dispatch_on_retry):
    target = str(tmp_path / "报告.txt")
    tool_results = asyncio.Queue()
    tools = BuiltinTools(applications=None)
    fail_before_dispatch = dispatch_on_retry

    async def complete(request):
        nonlocal fail_before_dispatch
        body = await request.json()
        message = body["messages"][-1]
        if not body["stream"]:
            if message["role"] == "tool":
                name, arguments = "task_complete", {"text": "报告已生成"}
                assert json.loads(message["content"])["structuredContent"]["path"] == target
            else:
                name, arguments = "create_text_file", {"path": target, "content": "测试产物", "final_step": True}
            return web.json_response({
                "id": "worker", "object": "chat.completion", "created": 0, "model": "test",
                "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                    "role": "assistant", "tool_calls": [{"id": name, "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)}}],
                }}],
            })
        if message["role"] == "tool":
            result = json.loads(message["content"])
            tool_results.put_nowait(result)
            if "tasks" in result:
                return web.json_response({"error": {"message": "任务已保存，但聊天回复失败"}}, status=503)
            delta, finish = {"content": "已收到。"}, "stop"
        else:
            if fail_before_dispatch:
                fail_before_dispatch = False
                return web.json_response({"error": {"message": "尚未创建任务"}}, status=503)
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
    base_url = f"http://127.0.0.1:{server.addresses[0][1]}"
    monkeypatch.setenv("DEEPSEEK_BASE_URL", base_url)
    worker = TaskWorker(tools.create_server, api_key="task-test-key", base_url=base_url)
    def new_runtime():
        return Runtime(database_path=tmp_path / "desktop.sqlite3",
                       task_factory=lambda store, on_change: TaskRuntime(
                           store, worker, ActionReconciler(tools.inspect_action), on_change=on_change,
                       ))

    async def wait_for_failed_reply(reply_id):
        async with asyncio.timeout(5):
            while not any(message["id"] == reply_id and message["status"] == "failed"
                          for message in runtime.connect("main")["messages"]):
                await asyncio.sleep(.01)

    runtime = new_runtime()
    runtime.start()
    try:
        accepted = runtime.send_text(f"请生成文件「{target}」")
        issued = accepted
        if dispatch_on_retry:
            await wait_for_failed_reply(accepted["reply_id"])
            assert runtime.list_tasks()["tasks"] == []
            issued = runtime.resend_text(accepted["user_message_id"])
            assert issued["user_message_id"] != accepted["user_message_id"]
        result = await asyncio.wait_for(tool_results.get(), 5)
        task, = runtime.list_tasks()["tasks"]
        assert result["tasks"][0]["id"] == task["id"]
        assert task["source"] == {"message_id": accepted["user_message_id"], "reply_id": accepted["reply_id"], "source": "text"}
        async with asyncio.timeout(5):
            while not runtime.connect("main")["task_cards"]:
                await asyncio.sleep(.01)
        assert runtime.connect("main")["task_cards"][0]["id"] == task["id"]
        await wait_for_failed_reply(issued["reply_id"])
        async with asyncio.timeout(5):
            while not any(message.get("task_notice") for message in runtime.connect("main")["messages"]):
                await asyncio.sleep(.01)
        assert (tmp_path / "报告.txt").read_text(encoding="utf-8") == "测试产物"
        await asyncio.to_thread(runtime.stop)
        runtime = new_runtime()
        runtime.start()
        assert runtime.resend_text(accepted["user_message_id"])["existing_task_ids"] == [task["id"]]
        assert runtime.resend_text(issued["user_message_id"])["existing_task_ids"] == [task["id"]]
        assert sum(message["role"] == "user" for message in runtime.connect("main")["messages"]) == (2 if dispatch_on_retry else 1)

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


@pytest.mark.asyncio
async def test_model_cannot_pause_a_task_without_the_users_explicit_target(tmp_path, monkeypatch):
    results = asyncio.Queue()
    path = tmp_path / "control.sqlite3"
    store = ConversationStore(path)
    await store.open()
    try:
        source = {"message_id": "request", "reply_id": "reply", "source": "text"}
        await store.save([{"id": "request", "reply_id": "reply", "role": "user", "source": "text",
                           "text": "生成报告", "status": "completed", "error": "", "created_at": "2026-11-20"}],
                         [], {"display_start_position": 0, "model_start_position": 0})
        batch = await TaskStore(store).dispatch(source, [{
            "key": "report", "title": "生成报告", "goal": "生成报告",
            "scope": [{"operation": "window.show", "target": "main"}],
        }])
        task_id = batch["tasks"][0]["id"]
    finally:
        await store.close()

    async def worker(execution):
        await asyncio.Event().wait()

    async def model(request):
        body = await request.json()
        latest = body["messages"][-1]
        if latest["role"] == "tool":
            results.put_nowait(json.loads(latest["content"]))
            delta, finish = {"content": "收到。"}, "stop"
        else:
            delta, finish = {"tool_calls": [{"index": 0, "id": f"control-{len(body['messages'])}",
                                             "type": "function", "function": {"name": "task_command",
                                             "arguments": json.dumps({"task_id": task_id, "action": "pause"})}}]}, "tool_calls"
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        chunk = {"id": "model", "object": "chat.completion.chunk", "created": 0, "model": "test",
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        await response.write(f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode())
        return response

    app = web.Application()
    app.router.add_post("/chat/completions", model)
    server = web.AppRunner(app)
    await server.setup()
    await web.TCPSite(server, "127.0.0.1", 0).start()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "task-test-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", f"http://127.0.0.1:{server.addresses[0][1]}")
    runtime = Runtime(database_path=path, task_factory=lambda store, on_change: TaskRuntime(
        store, worker, ActionReconciler(BuiltinTools(applications=None).inspect_action), on_change=on_change,
    ))
    runtime.start()
    try:
        for instruction in ("只停止当前播报，后台任务继续。", "暂停任务", "先不要暂停生成报告"):
            runtime.send_text(instruction)
            assert (await asyncio.wait_for(results.get(), 5))["status"] == "rejected"
        assert runtime.get_task(task_id)["task"]["state"] == "running"
        runtime.send_text("请暂停「生成报告」。")
        result = await asyncio.wait_for(results.get(), 5)
        assert result["task"]["state"] in {"pausing", "paused"}, result
        assert runtime.get_task(task_id)["task"]["stop_intent"] == "pause"
    finally:
        await asyncio.to_thread(runtime.stop)
        await server.cleanup()


def test_chat_replacement_path_keeps_user_punctuation(tmp_path):
    path = str(tmp_path / "报告!")
    _, answer = task_command_intent("answer", f"回答生成报告：{path}", {"id": "report", "title": "生成报告"})
    assert replacement_file_scope(answer)["target"] == path


@pytest.mark.parametrize("saved_question", [False, True])
def test_task_detail_reply_can_authorize_the_replacement_file(tmp_path, saved_question):
    database, original, replacement = tmp_path / "task.sqlite3", tmp_path / "旧.txt", tmp_path / "新.txt"
    original.write_text("保留原文件", encoding="utf-8")
    tools = BuiltinTools(applications=None)

    async def seed():
        owner = ConversationStore(database)
        await owner.open()
        try:
            source = {"message_id": "ask", "reply_id": "reply", "source": "text"}
            await owner.save([{"id": "ask", "reply_id": "reply", "role": "user", "source": "text",
                               "text": f"生成「{original}」", "status": "completed", "error": "",
                               "created_at": "2026-11-20T03:00:00+00:00"}], [],
                             {"display_start_position": 0, "model_start_position": 0})
            store = TaskStore(owner)
            result = await store.dispatch(source, [{"key": "report", "title": "生成报告", "goal": "创建文件",
                                                    "scope": [{"operation": "file.create", "target": str(original)}]}])
            task_id = result["tasks"][0]["id"]
            if saved_question:
                detail = await store.get_task(task_id)
                error = f"目标已存在同名文件，请指定新文件名：{original}"
                text = f"文件生成尚未执行：{error}。请确认新的路径或内容。"
                detail["task"].update(state="waiting_user", progress="等待用户答复", execution_id="previous",
                                      actions=["answer", "pause", "cancel", "supplement"])
                detail["executions"].append({"id": "previous", "state": "waiting_user"})
                detail["steps"].append({
                    "id": "previous-step", "execution_id": "previous", "status": "not_executed",
                    "request": {"operation": "file.create", "target": str(original), "resources": []},
                    "outcome": {"status": "not_executed", "error": error, "artifacts": [], "result": {}},
                })
                detail["questions"].append({
                    "id": "previous-question", "task_id": task_id, "execution_id": "previous", "step_id": None,
                    "status": "open", "text": text, "answer": None, "created_at": "2026-11-20",
                })
                await store.save_task(detail, [{"kind": "question", "text": text, "question_id": "previous-question"}])
            return task_id
        finally:
            await owner.close()

    async def model(params):
        if params["messages"][-1]["role"] == "tool":
            return {"role": "assistant", "content": "新文件已生成"}
        scope = json.loads(params["messages"][0]["content"].split("\n任务定义：\n", 1)[1])["scope"]
        return {"role": "assistant", "tool_calls": [{"id": "write", "type": "function",
                 "function": {"name": "create_text_file", "arguments": json.dumps({
                     "path": scope[0]["target"], "content": "生成产物", "final_step": True,
                 })}}]}

    task_id = asyncio.run(seed())
    worker = TaskWorker(tools.create_server, completion=model)
    runtime = Runtime(database_path=database, task_factory=lambda store, on_change: TaskRuntime(
        store, worker, ActionReconciler(tools.inspect_action), on_change=on_change,
    ))
    runtime.start()
    try:
        api = DesktopApi(runtime, "main")
        while not any(task["state"] == "waiting_user" for task in runtime.events.get(timeout=3)["snapshot"]["task_cards"]):
            pass
        question, = api.get_task(task_id)["questions"]
        assert question["requested_operation"] == "file.create"
        with pytest.raises(ValueError, match="本机绝对文件路径"):
            api.task_command(task_id, "answer", {"question_id": question["id"], "text": "相对路径.txt"})
        api.task_command(task_id, "answer", {"question_id": question["id"], "text": str(replacement)})
        while not any(task["state"] == "completed" for task in runtime.events.get(timeout=3)["snapshot"]["task_cards"]):
            pass
        detail = api.get_task(task_id)
        assert detail["task"]["state"] == "completed"
        assert detail["questions"][0]["answer"] == str(replacement)
        assert detail["scope"] == [{"operation": "file.create", "target": str(replacement)}]
        assert replacement.read_text(encoding="utf-8") == "生成产物"
        assert original.read_text(encoding="utf-8") == "保留原文件"
    finally:
        runtime.stop()
