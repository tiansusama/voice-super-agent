from pathlib import Path
import os

import pytest

from voice_super_agent.tasks.mcp_client import MCPExecutionClient
from voice_super_agent.tasks.recovery import ActionReconciler
from voice_super_agent.tooling.interface import BuiltinTools


class RecordingAccess:
    task_id = "task"
    execution_id = "execution"

    def __init__(self):
        self.actions = []

    async def start_action(self, request):
        self.actions.append(request)
        return str(len(self.actions))

    async def finish_action(self, step_id, result):
        assert result["status"] == "succeeded"


@pytest.mark.asyncio
async def test_tool_server_uses_only_this_execution_scope(tmp_path):
    path = tmp_path / "新建.txt"
    access = RecordingAccess()
    tools = BuiltinTools(applications=None)
    read_server = tools.create_server(access, [{"operation": "directory.list", "target": str(tmp_path)}])
    write_server = tools.create_server(access, [{"operation": "file.create", "target": str(path)}])
    async with (
        MCPExecutionClient(read_server) as reader,
        MCPExecutionClient(write_server) as writer,
    ):
        with pytest.raises(RuntimeError, match="未授权"):
            await reader.call_tool("create_text_file", {"path": str(path), "content": "越界"})
        assert not path.exists()
        result = await writer.call_tool("create_text_file", {"path": str(path), "content": "报表"})
        assert result["structuredContent"]["path"] == str(path)
        assert path.read_text(encoding="utf-8") == "报表"
        listing = await reader.call_tool("list_directory", {"path": str(tmp_path)})
        assert listing["structuredContent"]["entries"] == [{"name": "新建.txt", "is_directory": False}]
    assert [request["operation"] for request in access.actions] == ["file.create", "directory.list"]


@pytest.mark.asyncio
async def test_recovery_observes_existing_file_without_claiming_ownership(tmp_path):
    path = tmp_path / "可能已生成.txt"
    path.write_text("保留已有内容", encoding="utf-8")
    tools = BuiltinTools(applications=None)
    request = {
        "tool": "create_text_file", "arguments": {"path": str(path)},
        "operation": "file.create", "target": str(path), "resources": [f"file:{path}"],
        "cancellable": False, "expected": {"path": str(path)},
    }
    observed = await tools.inspect_action(request)
    assert observed == {"path": str(path), "exists": True}
    assert Path(observed["path"]).read_text(encoding="utf-8") == "保留已有内容"
    reconcile = ActionReconciler(tools.inspect_action)
    outcome = await reconcile(RecordingAccess(), {"request": request})
    assert outcome["status"] == "uncertain" and "归属" in outcome["error"]
    assert outcome["artifacts"] == []
    path.unlink()
    assert await tools.inspect_action(request) == {"path": str(path), "exists": False}
    outcome = await reconcile(RecordingAccess(), {"request": request})
    assert outcome["status"] == "not_executed"


@pytest.mark.asyncio
async def test_recovery_records_the_actual_main_window_state():
    async def is_visible():
        return True

    tools = BuiltinTools(applications=None, get_window_visibility=is_visible)
    show = {"request": {"operation": "window.show", "target": "main", "expected": {"visible": True}}}
    hide = {"request": {"operation": "window.hide", "target": "main", "expected": {"visible": False}}}
    reconcile = ActionReconciler(tools.inspect_action)
    observed = await reconcile(RecordingAccess(), show)
    assert observed["status"] == "succeeded" and observed["result"]["current_observation"]["visible"] is True
    pending = await reconcile(RecordingAccess(), hide)
    assert pending["status"] == "not_executed" and pending["result"]["current_observation"]["visible"] is True


@pytest.mark.skipif(os.name != "nt", reason="Windows 的目录联接")
@pytest.mark.asyncio
async def test_rebinding_never_moves_a_persisted_path_grant(tmp_path):
    from _winapi import CreateJunction

    requested = tmp_path / "approved"
    relocated = tmp_path / "other"
    requested.mkdir()
    relocated.mkdir()
    target = requested / "报告.txt"
    target.write_text("原始内容", encoding="utf-8")
    grant = {"operation": "file.read", "target": str(target.resolve())}
    requested.rename(tmp_path / "original")
    (relocated / target.name).write_text("范围之外", encoding="utf-8")
    CreateJunction(str(relocated), str(requested))

    access = RecordingAccess()
    server = BuiltinTools(applications=None).create_server(access, [grant])
    async with MCPExecutionClient(server) as client:
        with pytest.raises(RuntimeError, match="未授权"):
            await client.call_tool("read_text_file", {"path": str(target)})
    assert access.actions == []
