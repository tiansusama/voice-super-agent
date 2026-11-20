import asyncio
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from mcp.server import Server

from voice_super_agent.tasks.interface import ActionRequest, ExecutionAccess, Operation, ScopeGrant
from voice_super_agent.tooling.apps import Application, ApplicationTools, AppPlatform
from voice_super_agent.tooling.files import FileGrant, FileOperation, FileTools
from voice_super_agent.tooling.server import create_builtin_tool_server
from voice_super_agent.tooling.window import SetVisibility, WindowOperation, WindowTools


FILE_OPERATIONS: dict[Operation, FileOperation] = {
    "directory.list": "list",
    "file.read": "read",
    "file.create": "create",
}
WINDOW_OPERATIONS: dict[Operation, WindowOperation] = {
    "window.show": "show",
    "window.hide": "hide",
}


class BuiltinTools:
    """组合层为每次执行绑定已保存范围，恢复时只观察实际目标。"""

    def __init__(
        self,
        applications: AppPlatform | None,
        *,
        set_window_visibility: SetVisibility | None = None,
        get_window_visibility: Callable[[], Awaitable[bool]] | None = None,
    ):
        self._applications = applications
        self._set_window_visibility = set_window_visibility
        self._get_window_visibility = get_window_visibility

    def create_server(self, access: ExecutionAccess, scope: Sequence[ScopeGrant]) -> Server:
        files = FileTools(
            (FileGrant(FILE_OPERATIONS[grant["operation"]], grant["target"])
             for grant in scope if grant["operation"] in FILE_OPERATIONS),
            canonical=True,
        )
        windows = None
        if self._set_window_visibility is not None:
            windows = WindowTools(
                (WINDOW_OPERATIONS[grant["operation"]] for grant in scope
                 if grant["operation"] in WINDOW_OPERATIONS and grant["target"] == "main"),
                self._set_window_visibility,
            )
        applications = None
        if self._applications is not None:
            applications = ApplicationTools(
                (grant["target"] for grant in scope if grant["operation"] == "application.open"),
                self._applications,
            )
        return create_builtin_tool_server(files, access, windows=windows, applications=applications)

    async def inspect_action(self, request: ActionRequest) -> dict:
        operation = request["operation"]
        target = request["target"]
        if operation in {"directory.list", "file.read"}:
            return {"path": target, "may_read_again": True}
        if operation == "file.create":
            try:
                await asyncio.to_thread(Path(target).lstat)
            except FileNotFoundError:
                return {"path": target, "exists": False}
            return {"path": target, "exists": True}
        if operation in WINDOW_OPERATIONS:
            if self._get_window_visibility is None:
                raise RuntimeError("主窗口当前不可查询。")
            return {"window": "main", "visible": await self._get_window_visibility()}
        if self._applications is None:
            raise RuntimeError("Windows 应用当前不可查询。")
        if Path(target).is_absolute():
            application = Application(target, "exe", target)
        else:
            application = await asyncio.to_thread(ApplicationTools({target}, self._applications).resolve, target)
        window = await asyncio.to_thread(self._applications.find_window, application)
        return {"target": target, "window": window}
