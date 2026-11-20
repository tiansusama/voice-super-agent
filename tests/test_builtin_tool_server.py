import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("mcp")
from mcp import McpError, types
from mcp.shared.memory import create_connected_server_and_client_session

from voice_super_agent.tooling.apps import Application, ApplicationTools
from voice_super_agent.tooling.files import FileGrant, FileTools
from voice_super_agent.tooling.server import create_builtin_tool_server
from voice_super_agent.tooling.window import WindowTools


class RecordingAccess:
    task_id = "test-task"
    execution_id = "test-execution"

    def __init__(self):
        self.actions = []
        self.results = []

    async def start_action(self, request):
        self.actions.append(request)
        return str(len(self.actions))

    async def finish_action(self, step_id, result):
        self.results.append((step_id, result))


@pytest.mark.asyncio
async def test_mcp_file_tools_enforce_scope_and_report_actual_results(tmp_path):
    source = tmp_path / "来源.txt"
    source.write_text("需要整理的资料", encoding="utf-8")
    output = tmp_path / "结果.json"
    unapproved = tmp_path / "另一个结果.json"
    files = FileTools([FileGrant("read", source), FileGrant("create", output)])
    access = RecordingAccess()
    async with create_connected_server_and_client_session(create_builtin_tool_server(files, access)) as client:
        response = await client.list_tools()
        assert {tool.name for tool in response.tools} == {
            "list_directory", "read_text_file", "create_text_file",
        }
        fetched = await client.call_tool("read_text_file", {"path": str(source)})
        assert fetched.structuredContent["text"] == "需要整理的资料"
        rejected = await client.call_tool("create_text_file", {
            "path": str(unapproved), "content": "{}",
        })
        assert rejected.isError
        assert not unapproved.exists()

        created = await client.call_tool("create_text_file", {
            "path": str(output), "content": '{"结果":"完成"}',
        })
        assert not created.isError
        assert json.loads(output.read_text(encoding="utf-8")) == {"结果": "完成"}
        assert created.structuredContent == access.results[-1][1]["result"]
        assert access.results[-1][1]["artifacts"] == [{"kind": "file", "path": str(output.resolve())}]
        action = access.actions[-1]
        assert action["operation"] == "file.create" and not action["cancellable"]
        assert Path(action["target"]) == output.resolve()
        assert "content" not in action["arguments"]

        collision = await client.call_tool("create_text_file", {
            "path": str(output), "content": "{}",
        })
        assert collision.isError
        assert access.results[-1][1]["status"] == "not_executed"
        assert json.loads(output.read_text(encoding="utf-8")) == {"结果": "完成"}


@pytest.mark.asyncio
async def test_mcp_window_action_uses_host_visibility_result():
    visible = True
    access = RecordingAccess()

    async def set_visibility(target):
        nonlocal visible
        visible = target
        return visible

    windows = WindowTools({"hide"}, set_visibility)
    server = create_builtin_tool_server(FileTools([]), access, windows=windows)
    async with create_connected_server_and_client_session(server) as client:
        assert "set_main_window" in {tool.name for tool in (await client.list_tools()).tools}
        hidden = await client.call_tool("set_main_window", {"operation": "hide"})
        assert hidden.structuredContent == {"window": "main", "visible": False}
        assert [action["resources"] for action in access.actions] == [["window:main"]]
        denied = await client.call_tool("set_main_window", {"operation": "show"})
        assert denied.isError
        assert visible is False


@pytest.mark.asyncio
async def test_mcp_cancellation_waits_for_actual_non_cancellable_result():
    started = asyncio.Event()
    complete = asyncio.Event()
    recorded = asyncio.Event()
    request_id = None

    class Access(RecordingAccess):
        async def finish_action(self, step_id, result):
            await super().finish_action(step_id, result)
            recorded.set()

    async def set_visibility(visible):
        nonlocal request_id
        request_id = server.request_context.request_id
        started.set()
        await complete.wait()
        return visible

    access = Access()
    server = create_builtin_tool_server(FileTools([]), access, windows=WindowTools({"hide"}, set_visibility))
    async with create_connected_server_and_client_session(server) as client:
        pending = asyncio.create_task(client.call_tool("set_main_window", {"operation": "hide"}))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            await client.send_notification(types.ClientNotification(
                types.CancelledNotification(params=types.CancelledNotificationParams(requestId=request_id)),
            ))
            with pytest.raises(McpError, match="Request cancelled"):
                await asyncio.wait_for(pending, timeout=2)
            assert not access.results
        finally:
            complete.set()
        await asyncio.wait_for(recorded.wait(), timeout=2)
        assert access.results == [("1", {
            "status": "succeeded", "result": {"window": "main", "visible": False},
            "artifacts": [], "error": "",
        })]


@pytest.mark.asyncio
async def test_mcp_application_tool_exposes_actual_launch_outcome():
    application = Application("测试工具", "apps", "Test.App")

    class Platform:
        def installed(self):
            return [application]

        def find_window(self, target):
            return None

        def launch(self, target):
            assert target == application

    access = RecordingAccess()
    server = create_builtin_tool_server(
        FileTools([]), access, applications=ApplicationTools({"测试工具"}, Platform()),
    )
    async with create_connected_server_and_client_session(server) as client:
        assert "open_application" in {tool.name for tool in (await client.list_tools()).tools}
        denied = await client.call_tool("open_application", {"target": "未授权工具"})
        assert denied.isError
        assert not access.actions
        opened = await client.call_tool("open_application", {"target": "测试工具"})
        assert opened.structuredContent == {"target": "Test.App", "outcome": "launch_requested"}
        assert access.actions[0]["target"] == "Test.App"
