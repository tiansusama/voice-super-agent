from voice_super_agent.tasks.interface import ActionResult


class ActionReconciler:
    """观察当前目标和已有记录；同名文件只作证据，等待用户确认归属。"""

    def __init__(self, inspect_action):
        self._inspect = inspect_action

    async def __call__(self, execution, step) -> ActionResult:
        request = step["request"]
        try:
            evidence = await self._inspect(request)
        except (OSError, RuntimeError, ValueError) as exc:
            return ActionResult(status="uncertain", result={}, artifacts=[], error=f"核对实际状态失败：{exc}")
        operation = request["operation"]
        status = "uncertain"
        error = "请确认前次动作结果。"
        if operation in {"directory.list", "file.read"} or operation == "file.create" and not evidence["exists"]:
            status = "not_executed"
            error = ""
        elif operation in {"window.show", "window.hide"}:
            status = "succeeded" if evidence["visible"] == request["expected"]["visible"] else "not_executed"
            error = ""
        elif operation == "file.create":
            error = "目标文件已存在，请确认产物归属。"
        return ActionResult(status=status, result={"current_observation": evidence}, artifacts=[], error=error)
