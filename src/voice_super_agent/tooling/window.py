from collections.abc import Awaitable, Callable, Iterable
from typing import Literal


WindowOperation = Literal["show", "hide"]
SetVisibility = Callable[[bool], Awaitable[bool]]


class WindowTools:
    """主窗口动作的结果以 GUI 完成后返回的可见状态为准。"""

    def __init__(self, grants: Iterable[WindowOperation], set_visibility: SetVisibility):
        self._grants = frozenset(grants)
        self._set_visibility = set_visibility

    def approved_visibility(self, operation: WindowOperation) -> bool:
        if operation not in self._grants:
            raise PermissionError("尚未授权该主窗口操作。")
        return {"show": True, "hide": False}[operation]

    async def set_main_window(self, operation: WindowOperation) -> dict:
        desired = self.approved_visibility(operation)
        actual = await self._set_visibility(desired)
        if actual != desired:
            raise RuntimeError("主窗口未显示。" if desired else "主窗口未隐藏。")
        return {"window": "main", "visible": actual}
