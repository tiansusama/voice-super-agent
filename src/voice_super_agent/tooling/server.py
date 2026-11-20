import asyncio
from collections.abc import Awaitable, Callable
from functools import partial

from anyio import CancelScope
from anyio.lowlevel import checkpoint_if_cancelled
from mcp import types
from mcp.server import Server

from voice_super_agent.tasks.interface import ActionRequest, ExecutionAccess
from voice_super_agent.tooling.apps import ApplicationTools
from voice_super_agent.tooling.files import FileTools
from voice_super_agent.tooling.window import WindowTools


PATH = {"type": "string", "minLength": 1, "description": "用户明确指定的本机绝对路径。"}
OFFSET = {"type": "integer", "minimum": 0}


async def _execute_action(
    access: ExecutionAccess,
    request: ActionRequest,
    perform: Callable[[], Awaitable[dict]],
) -> dict:
    step_id = await access.start_action(request)
    operation = request["operation"]
    try:
        # 原生动作开始后，等待动作结果落库，再响应 MCP 取消。
        with CancelScope(shield=True):
            try:
                result = await perform()
            except (FileExistsError, ValueError) as exc:
                await access.finish_action(step_id, {
                    "status": "not_executed", "result": {}, "artifacts": [], "error": str(exc),
                })
                raise
            except (OSError, RuntimeError) as exc:
                await access.finish_action(step_id, {
                    "status": "failed" if operation in {"directory.list", "file.read"} else "uncertain",
                    "result": {}, "artifacts": [], "error": str(exc),
                })
                raise
            await access.finish_action(step_id, {
                "status": "succeeded",
                "result": result,
                "artifacts": [{"kind": "file", "path": result["path"]}] if operation == "file.create" else [],
                "error": "",
            })
    finally:
        await checkpoint_if_cancelled()
    return result


def create_builtin_tool_server(
    files: FileTools,
    access: ExecutionAccess,
    *,
    windows: WindowTools | None = None,
    applications: ApplicationTools | None = None,
) -> Server:
    """每次执行绑定一组授权，实际动作交给运行时登记和结算。"""
    server = Server("xiaobo-builtin-tools")
    operations = {
        "list_directory": ("list", files.list_directory, "分页查询指定目录的文件和子目录。", {
            "offset": OFFSET, "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        }),
        "read_text_file": ("read", files.read_text, "按字符分页读取指定的 UTF-8 文本文件。", {
            "offset": OFFSET, "limit": {"type": "integer", "minimum": 1, "maximum": 8000},
        }),
        "create_text_file": ("create", files.create_text, "在指定路径生成新文本文件，保留已有的同名文件。", {
            "content": {"type": "string"},
        }),
    }

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        tools = [
            types.Tool(name=name, description=description, inputSchema={
                "type": "object", "properties": {"path": PATH, **properties},
                "required": ["path", "content"] if operation == "create" else ["path"],
                "additionalProperties": False,
            })
            for name, (operation, _, description, properties) in operations.items()
        ]
        if windows is not None:
            tools.append(types.Tool(name="set_main_window", description="显示或隐藏小博主窗口。", inputSchema={
                "type": "object", "properties": {"operation": {"enum": ["show", "hide"]}},
                "required": ["operation"], "additionalProperties": False,
            }))
        if applications is not None:
            tools.append(types.Tool(name="open_application", description="按明确目标打开本机应用或显示已有窗口。", inputSchema={
                "type": "object", "properties": {"target": {"type": "string", "minLength": 1}},
                "required": ["target"], "additionalProperties": False,
            }))
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> dict:
        if name == "set_main_window" and windows is not None:
            operation = arguments["operation"]
            visible = windows.approved_visibility(operation)
            action = ActionRequest(
                tool=name, arguments=arguments,
                operation="window.show" if visible else "window.hide", target="main",
                resources=["window:main"], cancellable=False, expected={"visible": visible},
            )
            return await _execute_action(access, action, partial(windows.set_main_window, operation))
        if name == "open_application" and applications is not None:
            app = await asyncio.to_thread(applications.resolve, arguments["target"])
            action = ActionRequest(
                tool=name, arguments=arguments, operation="application.open", target=app.target,
                resources=[f"application:{app.target.casefold()}"],
                cancellable=False, expected={"application": app.target},
            )
            return await _execute_action(access, action, partial(asyncio.to_thread, applications.open, app))
        if name not in operations:
            raise ValueError("未知的本机工具。")
        operation, handler, _, _ = operations[name]
        path = str(await asyncio.to_thread(files.approved_path, operation, arguments["path"]))
        params = {**arguments, "path": path}
        recorded = {key: value for key, value in params.items() if key != "content"}
        if operation == "create":
            recorded["content_characters"] = len(params["content"])
        action = ActionRequest(
            tool=name,
            operation={"list": "directory.list", "read": "file.read", "create": "file.create"}[operation],
            target=path,
            resources=[f"file:{path.casefold()}"],
            arguments=recorded,
            cancellable=False,
            expected={"path": path},
        )
        return await _execute_action(access, action, partial(asyncio.to_thread, handler, **params))

    return server
