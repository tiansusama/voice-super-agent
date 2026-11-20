import asyncio

import anyio
from mcp import types
from mcp.server import Server
import pytest

from voice_super_agent.tasks.mcp_client import MCPExecutionClient


@pytest.fixture
def server_factory():
    def create(handler):
        server = Server("task-test")

        @server.list_tools()
        async def tools():
            return [types.Tool(name="test_action", description="受控任务测试工具", inputSchema={"type": "object"})]

        @server.call_tool()
        async def call(name, arguments):
            return [types.TextContent(type="text", text=await handler(arguments))]

        return server
    return create


@pytest.mark.asyncio
async def test_two_mcp_sessions_discover_and_call_with_independent_results(server_factory):
    async def first(arguments):
        await asyncio.sleep(0)
        return "任务一：" + arguments["value"]

    async def second(arguments):
        return "任务二：" + arguments["value"]

    async with MCPExecutionClient(server_factory(first)) as a, MCPExecutionClient(server_factory(second)) as b:
        tools = await asyncio.gather(a.list_tools(), b.list_tools())
        assert [items[0]["name"] for items in tools] == ["test_action", "test_action"]
        results = await asyncio.gather(a.call_tool("test_action", {"value": "甲"}), b.call_tool("test_action", {"value": "乙"}))
        assert [result["content"][0]["text"] for result in results] == ["任务一：甲", "任务二：乙"]


@pytest.mark.asyncio
async def test_mcp_tool_error_is_reported_without_retry(server_factory):
    calls = 0

    async def failing(arguments):
        nonlocal calls
        calls += 1
        raise ValueError("测试工具失败")

    async with MCPExecutionClient(server_factory(failing)) as client:
        with pytest.raises(RuntimeError, match="测试工具失败"):
            await client.call_tool("test_action", {})
    assert calls == 1


@pytest.mark.asyncio
async def test_business_error_keeps_original_type_after_mcp_session_closes(server_factory):
    async def unused(arguments):
        return "完成"

    with pytest.raises(ValueError, match="测试执行失败"):
        async with MCPExecutionClient(server_factory(unused)):
            raise ValueError("测试执行失败")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellable", [True, False])
async def test_protocol_cancellation_is_distinct_from_actual_action_completion(server_factory, cancellable):
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    facts = []

    async def action(arguments):
        with anyio.CancelScope(shield=not cancellable):
            started.set()
            try:
                await release.wait()
                facts.append("succeeded")
            finally:
                finished.set()
        # 事实已经登记后交还取消权，服务端按已取消请求结束协议处理。
        await anyio.lowlevel.checkpoint()
        return "实际完成"

    async with MCPExecutionClient(server_factory(action)) as client:
        request = asyncio.create_task(client.call_tool("test_action", {}))
        try:
            await asyncio.wait_for(started.wait(), 2)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            if cancellable:
                await asyncio.wait_for(finished.wait(), 2)
                assert facts == []
            else:
                assert not finished.is_set()
                release.set()
                await asyncio.wait_for(finished.wait(), 2)
                assert facts == ["succeeded"]
        finally:
            release.set()
