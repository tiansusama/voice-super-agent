from pathlib import Path

import pytest

pytest.importorskip("mcp")
from mcp.shared.memory import create_connected_server_and_client_session

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
        create_connected_server_and_client_session(read_server) as reader,
        create_connected_server_and_client_session(write_server) as writer,
    ):
        rejected = await reader.call_tool("create_text_file", {"path": str(path), "content": "越界"})
        assert rejected.isError
        assert not path.exists()
        result = await writer.call_tool("create_text_file", {"path": str(path), "content": "报表"})
        assert not result.isError
        assert path.read_text(encoding="utf-8") == "报表"
        listing = await reader.call_tool("list_directory", {"path": str(tmp_path)})
        assert listing.structuredContent["entries"] == [{"name": "新建.txt", "is_directory": False}]
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
    path.unlink()
    assert await tools.inspect_action(request) == {"path": str(path), "exists": False}
