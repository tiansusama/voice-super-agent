from voice_super_agent.tasks.interface import ActionResult


class ActionReconciler:
    """观察当前目标和已有记录；同名文件只作证据，等待用户确认归属。"""

    def __init__(self, inspect_action):
        self._inspect = inspect_action

    async def __call__(self, execution, step) -> ActionResult:
        request = step["request"]
        evidence = await self._inspect(request)
        operation = request["operation"]
        status = "uncertain"
        if operation in {"directory.list", "file.read"} or operation == "file.create" and not evidence["exists"]:
            status = "not_executed"
        elif operation in {"window.show", "window.hide"} and evidence["visible"] == request["expected"]["visible"]:
            status = "succeeded"
        artifacts = [{"kind": "file", "path": request["target"]}] if operation == "file.create" and evidence["exists"] else []
        return {"status": status, "result": {"current_observation": evidence}, "artifacts": artifacts,
                "error": "请确认前次动作结果。" if status == "uncertain" else ""}
