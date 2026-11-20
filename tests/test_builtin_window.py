import pytest

from voice_super_agent.tooling.window import WindowTools


@pytest.mark.asyncio
async def test_window_tool_confirms_actual_visibility():
    visible = True

    async def set_visibility(target):
        nonlocal visible
        visible = target
        return visible

    tool = WindowTools({"hide"}, set_visibility)
    assert await tool.set_main_window("hide") == {"window": "main", "visible": False}
    with pytest.raises(PermissionError, match="未授权"):
        await tool.set_main_window("show")
    assert visible is False


@pytest.mark.asyncio
async def test_window_tool_reports_when_host_did_not_change_visibility():
    async def keep_window_visible(_target):
        return True

    tool = WindowTools({"hide"}, keep_window_visible)
    with pytest.raises(RuntimeError, match="窗口未隐藏"):
        await tool.set_main_window("hide")
