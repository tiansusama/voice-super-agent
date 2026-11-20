import asyncio
from contextlib import asynccontextmanager

import anyio
from mcp import ClientSession, types
from mcp.server import Server
from mcp.shared.memory import create_client_server_memory_streams


class _ExecutionSession(ClientSession):
    async def send_request(self, request, *arguments, **options):
        # SDK 1.27 在发送前同步分配该序号；取消等待时需显式发送协议通知。
        request_id = self._request_id
        try:
            return await super().send_request(request, *arguments, **options)
        except asyncio.CancelledError:
            if isinstance(request.root, types.CallToolRequest):
                with anyio.CancelScope(shield=True):
                    await self.send_notification(types.ClientNotification(types.CancelledNotification(
                        params=types.CancelledNotificationParams(requestId=request_id, reason="执行收到停止请求"),
                    )))
            raise


@asynccontextmanager
async def _connect(server):
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as group:
            group.start_soon(server.run, *server_streams, server.create_initialization_options())
            try:
                async with _ExecutionSession(*client_streams) as session:
                    await session.initialize()
                    yield session
            finally:
                group.cancel_scope.cancel()


class MCPExecutionClient:
    """一次执行独占一条真实 MCP 内存会话，业务结果由执行授权入口另行登记。"""

    def __init__(self, server: Server):
        self._connection = _connect(server)

    async def __aenter__(self):
        self._session = await self._connection.__aenter__()
        return self

    async def __aexit__(self, *exception):
        # 业务异常由调用方接收，协议上下文独立完成关停。
        await self._connection.__aexit__(None, None, None)

    async def list_tools(self) -> list[dict]:
        response = await self._session.list_tools()
        return [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in response.tools]

    @property
    def next_request_id(self):
        return self._session._request_id

    async def call_tool(self, name: str, arguments: dict) -> dict:
        response = await self._session.call_tool(name, arguments)
        if response.isError:
            text = "\n".join(item.text for item in response.content if item.type == "text")
            raise RuntimeError(f"MCP 工具 {name} 调用失败：{text}")
        return response.model_dump(mode="json", by_alias=True, exclude_none=True)
