import asyncio
from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timezone
import logging
import json
from pathlib import Path
import sqlite3
from typing import get_args
from uuid import uuid4

import anyio
from mcp.shared.exceptions import McpError
from openai import APIError

from voice_super_agent.memory.store import ConversationStore
from voice_super_agent.tasks.interface import ActionRequest, ActionResult, Operation, TaskDetail, TaskSource
from voice_super_agent.tasks.store import TaskStore


logger = logging.getLogger(__name__)


class TaskRuntime:
    """SQLite 保存任务事实；独立执行负责长工作，主对话只等待命令提交。"""

    def __init__(self, store: ConversationStore, worker, reconcile, *, on_change=None):
        self._conversation_store = store
        self._store = TaskStore(store)
        self._worker = worker
        self._reconcile = reconcile
        self._on_change = on_change
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._availability_changed = asyncio.Event()
        self._resources: dict[str, str] = {}
        self._running: dict[str, _Execution] = {}
        self._faults: dict[str, TaskDetail] = {}
        self._events = asyncio.Queue()
        self._shutdown = False
        self._shutdown_error = ""
        self._dispatcher: asyncio.Task | None = None

    async def start(self):
        async with self._lock:
            await self._store.set_shutdown(False)
            for detail in await self._store.load_unfinished():
                task = detail["task"]
                confirmed_artifacts = []
                uncertain_artifacts = []
                for step in detail["steps"]:
                    if step["status"] == "succeeded":
                        confirmed_artifacts.extend(step["outcome"]["artifacts"])
                    elif step["status"] == "uncertain" and step["outcome"] is not None:
                        uncertain_artifacts.extend(step["outcome"]["artifacts"])
                    if step["status"] in {"started", "uncertain"}:
                        for resource in step["request"]["resources"]:
                            self._resources[resource] = step["id"]
                artifacts = [artifact for artifact in detail["artifacts"]
                             if artifact not in uncertain_artifacts or artifact in confirmed_artifacts]
                artifacts_changed = artifacts != detail["artifacts"]
                detail["artifacts"] = artifacts
                if task["state"] in {"running", "pausing", "cancelling", "recovering", "storage_error"} or task["stop_reason"] == "shutdown":
                    for record in detail["executions"]:
                        if record["state"] == "running":
                            record.update(state="interrupted", ended_at=datetime.now(timezone.utc).isoformat())
                    task.update(state="recovering", progress="正在核对上次执行", execution_id=None)
                    if task["stop_reason"] == "shutdown":
                        task["stop_reason"] = None
                    await self._commit(detail, [{"kind": "state", "text": task["progress"]}])
                elif artifacts_changed:
                    await self._commit(detail, [])
            self._dispatcher = asyncio.create_task(self._schedule(), name="task-dispatcher")
            self._wake.set()

    async def _submitted(self, operation):
        """命令取消也等到短事务完成，已提交的派工仍会唤起调度。"""
        job = asyncio.create_task(operation)
        cancelled = None
        while not job.done():
            try:
                with anyio.CancelScope(shield=True):
                    await asyncio.shield(job)
            except asyncio.CancelledError as exc:
                cancelled = exc
        result = job.result()
        if cancelled is not None:
            raise cancelled
        return result

    async def dispatch(self, source, plans):
        return await self._submitted(self._dispatch(source, plans))

    async def _dispatch(self, source, plans):
        async with self._lock:
            cursor = await self._store.last_change_position()
            batch = await self._store.dispatch(source, plans)
            while True:
                changes = await self._store.list_changes(cursor, 100)
                for change in changes:
                    await self._publish(change)
                    cursor = change["position"]
                if len(changes) < 100:
                    break
            self._wake.set()
            return batch

    async def find_batch(self, message_id):
        return await self._store.find_batch(message_id)

    async def list_tasks(self, cursor=None, limit=20):
        page = await self._store.list_tasks(cursor, limit)
        page["tasks"] = [deepcopy(self._faults[task["id"]]["task"]) if task["id"] in self._faults else task for task in page["tasks"]]
        return page

    async def get_task(self, task_id):
        if task_id in self._faults:
            return deepcopy(self._faults[task_id])
        return await self._store.get_task(task_id)

    async def list_changes(self, after_position=0, limit=50):
        return await self._store.list_changes(after_position, limit)

    async def next_change(self):
        return await self._events.get()

    async def mark_read(self, notification_id):
        await self._store.mark_read(notification_id)

    async def apply_concurrency(self, limit):
        async with self._lock:
            result = await self._store.apply_concurrency(limit)
            self._signal_availability()
            return result

    async def get_concurrency(self):
        return await self._store.get_concurrency()

    async def _publish(self, change):
        if self._on_change is not None:
            await self._on_change(change)
        else:
            self._events.put_nowait(change)

    def _actions(self, detail):
        task = detail["task"]
        if task["state"] in {"completed", "cancelled"}:
            return []
        if task["state"] == "failed":
            return ["supplement", "retry", "cancel"]
        if task["state"] == "storage_error":
            return ["retry", "pause", "cancel"]
        actions = ["supplement"]
        if any(question["status"] == "open" for question in detail["questions"]):
            actions.append("answer")
        if task["state"] == "paused":
            return actions + ["resume", "cancel"]
        if task["state"] in {"pausing", "cancelling"}:
            return actions + (["cancel"] if task["stop_intent"] != "cancel" else [])
        return actions + ["pause", "cancel"]

    async def _commit(self, detail, changes):
        detail["task"]["actions"] = self._actions(detail)
        try:
            saved = await self._store.save_task(detail, changes)
        except (sqlite3.Error, OSError) as exc:
            logger.exception("任务执行事实保存失败")
            task = detail["task"]
            task.update(state="storage_error", error=f"任务保存失败：{exc}", actions=["retry", "pause", "cancel"])
            self._faults[task["id"]] = deepcopy(detail)
            known = next((step["outcome"] for step in reversed(detail["steps"]) if step.get("outcome")), None)
            await self._publish({"kind": "storage_error", "task_id": task["id"], "error": task["error"], "known_result": known})
            raise
        self._faults.pop(detail["task"]["id"], None)
        for change in saved:
            await self._publish(change)

    async def task_command(self, task_id, action, payload=None):
        return await self._submitted(self._command(task_id, action, payload or {}))

    async def _command(self, task_id, action, payload):
        async with self._lock:
            detail = await self.get_task(task_id)
            task = detail["task"]
            if action not in self._actions(detail):
                raise ValueError(f"任务当前状态 {task['state']} 无法执行 {action}。")
            changes = []
            resolved_step = None
            if action in {"supplement", "answer"}:
                text = payload["text"].strip()
                if not text:
                    raise ValueError("任务补充或答复不能为空。")
                source = payload.get("source")
                if source is not None:
                    await self._validate_source(source)
                item = {"id": uuid4().hex, "kind": action, "source": source, "text": text,
                        "consumed": False, "created_at": datetime.now(timezone.utc).isoformat()}
                final = next((step for step in reversed(detail["steps"]) if step.get("final")
                              and step["status"] in {"started", "uncertain", "succeeded"}), None)
                if final is not None and action == "supplement":
                    item["after_step"] = final["id"]
                if action == "answer":
                    question = next((q for q in detail["questions"] if q["id"] == payload["question_id"] and q["status"] == "open"), None)
                    if question is None:
                        raise ValueError("答复必须对应此任务尚待回答的问题。")
                    if question.get("requested_operation") == "file.create":
                        replacement = payload.get("scope", [])
                        if source is None or len(replacement) != 1 or replacement[0]["operation"] != "file.create":
                            raise ValueError("请确认新的绝对文件路径。")
                        previous = question["replacement_for"]
                        if Path(replacement[0]["target"]) == Path(previous):
                            raise ValueError("请指定不同的文件路径。")
                        detail["scope"] = [grant for grant in detail["scope"]
                                           if grant["operation"] != "file.create" or Path(grant["target"]) != Path(previous)]
                    if question["step_id"] is not None:
                        step = next(s for s in detail["steps"] if s["id"] == question["step_id"])
                        resolution = payload.get("resolution")
                        if resolution not in {"succeeded", "not_executed", "cancelled"}:
                            raise ValueError("请明确确认原步骤已完成、未执行或已停止。")
                        result = {"user_confirmation": text}
                        artifacts = []
                        if resolution == "succeeded" and step["request"]["operation"] == "file.create":
                            path = step["request"]["target"]
                            artifact = {"kind": "file", "path": path}
                            result["path"] = path
                            artifacts.append(artifact)
                            if artifact not in detail["artifacts"]:
                                detail["artifacts"].append(artifact)
                        step.update(status=resolution, confirmed_by=source, confirmation=text, previous_outcome=step["outcome"],
                                    outcome={"status": resolution, "result": result, "artifacts": artifacts, "error": ""})
                        resolved_step = step
                    question.update(status="answered", answer=text, answer_source=source)
                    item["question_id"] = question["id"]
                    if task["stop_intent"]:
                        task["state"] = "cancelled" if task["stop_intent"] == "cancel" else "paused"
                    elif not any(q["status"] == "open" for q in detail["questions"]):
                        task.update(state="queued", progress="答复已保存，等待执行名额")
                detail["inputs"].append(item)
                if payload.get("scope") and source is None:
                    raise ValueError("扩大范围需要关联用户确认的正式消息。")
                for grant in payload.get("scope", []):
                    if grant["operation"] not in get_args(Operation) or not grant["target"].strip():
                        raise ValueError("任务操作类型或目标无效。")
                detail["scope"].extend(payload.get("scope", []))
                changes.append({"kind": "supplement", "text": "补充已保存" if action == "supplement" else "答复已保存"})
            elif action in {"pause", "cancel"}:
                task.update(stop_intent=action, stop_reason="user")
                uncertain = any(step["status"] in {"started", "uncertain"} for step in detail["steps"])
                if task_id in self._running:
                    task["state"] = "pausing" if action == "pause" else "cancelling"
                elif uncertain:
                    task["state"] = "recovering"
                else:
                    task["state"] = "paused" if action == "pause" else "cancelled"
                task["progress"] = "正在暂停" if action == "pause" else "正在取消"
                changes.append({"kind": "state", "text": task["progress"]})
            else:
                if action == "resume":
                    task.update(stop_intent=None, stop_reason=None)
                task.update(state="recovering", error="", progress="正在核对后继续")
                task["next_reason"] = "retry" if action == "retry" else "resume"
                changes.append({"kind": "state", "text": task["progress"]})
            await self._commit(detail, changes)
            if resolved_step is not None:
                self._release_resources(resolved_step)
            for step in detail["steps"]:
                if step["status"] not in {"started", "uncertain"}:
                    self._release_resources(step)
            if action in {"pause", "cancel"} and task_id in self._running:
                self._stop_execution(self._running[task_id])
            self._wake.set()
            return detail["task"]

    async def _validate_source(self, source: TaskSource):
        def read(connection):
            return connection.execute("SELECT role, reply_id, source, status FROM display_messages WHERE id = ?", (source["message_id"],)).fetchone()
        message = await self._conversation_store.task_io(read)
        if (message is None or message["role"] != "user" or message["reply_id"] != source["reply_id"]
                or message["source"] != source["source"] or message["status"] != "completed"):
            raise ValueError("任务输入必须关联已保存的正式用户消息。")

    async def _schedule(self):
        error = ""
        while True:
            await self._wake.wait()
            self._wake.clear()
            try:
                async with self._lock:
                    if self._shutdown:
                        continue
                    await self._schedule_ready()
            except (sqlite3.Error, OSError) as exc:
                message = f"读取任务队列失败：{exc}"
                if message != error:
                    logger.exception("读取任务队列失败")
                    error = message
                    await self._publish({"kind": "scheduler_error", "error": error})
                await asyncio.sleep(0.25)
                self._wake.set()
            else:
                if error:
                    error = ""
                    await self._publish({"kind": "scheduler_error", "error": error})

    async def _schedule_ready(self):
        limit = await self._store.get_concurrency()
        unfinished = await self._store.load_unfinished()
        unfinished.sort(key=lambda detail: detail["task"]["state"] != "recovering")
        for detail in unfinished:
            task = detail["task"]
            if task["id"] in self._running or task["id"] in self._faults or task["state"] not in {"queued", "recovering"}:
                continue
            # 核对可以先释放旧资源，再按并发上限安排后续执行。
            if self._active_count() >= limit:
                needs_inspection = task["state"] == "recovering" and any(
                    step["status"] in {"started", "uncertain"} for step in detail["steps"])
                if not needs_inspection:
                    continue
            dependencies = [await self._store.get_task(dependency) for dependency in detail["depends_on"]]
            if any(dependency["task"]["state"] != "completed" for dependency in dependencies):
                continue
            execution_id = uuid4().hex
            reason = task.pop("next_reason", "recovery" if task["state"] == "recovering" else "dispatch")
            detail["executions"].append({"id": execution_id, "reason": reason, "state": "running",
                                         "started_at": datetime.now(timezone.utc).isoformat(), "ended_at": None, "error": ""})
            task.update(state="running", execution_id=execution_id, progress="开始执行")
            try:
                await self._commit(detail, [{"kind": "state", "text": task["progress"]}])
            except (sqlite3.Error, OSError):
                continue
            execution = _Execution(self, task["id"], execution_id)
            self._running[task["id"]] = execution
            execution.task = asyncio.create_task(self._execute(execution, reason), name=f"task-{task['id']}")

    async def _execute(self, execution, reason):
        try:
            if await self._recover(execution):
                await self._worker(execution)
                detail = await self.get_task(execution.task_id)
                if detail["task"]["state"] in {"pausing", "cancelling"}:
                    await self._finish_stopped(execution)
                if detail["task"]["state"] == "running":
                    raise RuntimeError("Worker 结束时尚未提交结果或待回答问题。")
        except asyncio.CancelledError:
            with anyio.CancelScope(shield=True):
                await execution.settled.wait()
                await self._submitted(self._finish_stopped(execution))
        except (RuntimeError, ValueError, OSError, sqlite3.Error, APIError, McpError) as exc:
            if execution.task_id not in self._faults:
                async with self._lock:
                    detail = await self.get_task(execution.task_id)
                    if detail["task"]["state"] == "running":
                        detail["task"]["error"] = str(exc)
                        pending = next((step for step in detail["steps"] if step["status"] in {"started", "uncertain"}), None)
                        if pending is not None:
                            await self._question(detail, execution.execution_id, f"核对执行失败：{exc}。请确认该步骤的实际结果。", pending["id"])
                        else:
                            detail["task"].update(state="failed", progress="执行失败")
                            await self._commit(detail, [{"kind": "error", "text": str(exc)}])
        finally:
            async with self._lock:
                try:
                    detail = await self.get_task(execution.task_id)
                    if detail["task"]["state"] in {"pausing", "cancelling"}:
                        task = detail["task"]
                        task.update(state="cancelled" if task["stop_intent"] == "cancel" else "paused", progress="已停止当前执行")
                    record = next(record for record in detail["executions"] if record["id"] == execution.execution_id)
                    record.update(state=detail["task"]["state"], ended_at=datetime.now(timezone.utc).isoformat(), error=detail["task"]["error"])
                    if execution.task_id not in self._faults:
                        await self._commit(detail, [])
                except (sqlite3.Error, OSError):
                    # 已发布保存错误及已知事实，保留资源并允许其他任务继续调度。
                    pass
                finally:
                    self._running.pop(execution.task_id)
                    self._signal_availability()

    async def _finish_stopped(self, execution):
        async with self._lock:
            detail = await self.get_task(execution.task_id)
            task = detail["task"]
            if task["state"] not in {"waiting_user", "storage_error"}:
                unresolved = any(step["status"] in {"started", "uncertain"} for step in detail["steps"])
                state = "recovering" if unresolved else "cancelled" if task["stop_intent"] == "cancel" else "paused" if task["stop_intent"] or self._shutdown else "queued"
                task.update(state=state, progress="已停止当前执行")
                await self._commit(detail, [{"kind": "state", "text": task["progress"]}])

    async def _recover(self, execution):
        detail = await self.get_task(execution.task_id)
        pending = [step for step in detail["steps"] if step["status"] in {"started", "uncertain"}]
        for step in pending:
            execution.recovering_steps.add(step["id"])
            outcome = await self._reconcile(execution, deepcopy(step))
            await execution.finish_action(step["id"], outcome)
        async with self._lock:
            detail = await self.get_task(execution.task_id)
            if any(q["status"] == "open" for q in detail["questions"]):
                detail["task"].update(state="waiting_user", progress="等待用户答复")
                await self._commit(detail, [{"kind": "state", "text": "等待用户答复"}])
                return False
            if detail["task"]["stop_intent"]:
                detail["task"]["state"] = "cancelled" if detail["task"]["stop_intent"] == "cancel" else "paused"
                await self._commit(detail, [{"kind": "state", "text": "已完成停止请求"}])
                return False
            if self._active_count() > await self._store.get_concurrency():
                detail["task"].update(state="queued", progress="核对完成，等待执行名额")
                await self._commit(detail, [{"kind": "state", "text": detail["task"]["progress"]}])
                return False
            return detail["task"]["state"] == "running"

    def _allowed(self, detail, request):
        for grant in detail["scope"]:
            if grant["operation"] != request["operation"]:
                continue
            if request["operation"] in {"file.read", "file.create", "directory.list"}:
                target, root = Path(request["target"]).resolve(), Path(grant["target"]).resolve()
                if target == root:
                    return True
            elif request["target"].casefold() == grant["target"].casefold():
                return True
        return False

    async def _start_action(self, execution, request: ActionRequest):
        while True:
            async with self._lock:
                detail = await self.get_task(execution.task_id)
                task = detail["task"]
                if task["execution_id"] != execution.execution_id or task["state"] != "running" or self._shutdown:
                    raise asyncio.CancelledError()
                if not self._allowed(detail, request):
                    await self._question(detail, execution.execution_id, f"需要确认新增操作范围：{request['operation']} {request['target']}。")
                    raise PermissionError("工具目标超出用户确认的任务范围。")
                if execution.model_call_id is not None and execution.pending_inputs(detail):
                    raise ValueError("新的任务输入已保存，请采用补充后重新安排动作。")
                resources_available = all(resource not in self._resources for resource in request["resources"])
                if resources_available and (
                    not execution.waiting_resources or self._active_count() < await self._store.get_concurrency()
                ):
                    execution.waiting_resources = False
                    step = {"id": uuid4().hex, "execution_id": execution.execution_id, "request": deepcopy(request),
                            "status": "started", "outcome": None, "final": execution.final_step,
                            "model_call_id": execution.model_call_id,
                            "created_at": datetime.now(timezone.utc).isoformat()}
                    if execution.protocol_request_id is not None:
                        step["request"]["request_id"] = execution.protocol_request_id
                    detail["steps"].append(step)
                    task["progress"] = f"正在执行工具：{request['tool']}"
                    # 步骤记录与资源占用在取消屏蔽区提交，工具返回许可后才开始真实动作。
                    with anyio.CancelScope(shield=True):
                        await self._commit(detail, [{"kind": "state", "text": task["progress"], "step_id": step["id"]}])
                        for resource in request["resources"]:
                            self._resources[resource] = step["id"]
                        execution.current_step = step
                        execution.settled.clear()
                    return step["id"]
                if not execution.waiting_resources:
                    execution.waiting_resources = True
                    task["progress"] = "等待本机资源"
                    await self._commit(detail, [{"kind": "state", "text": task["progress"]}])
                    self._signal_availability()
                changed = self._availability_changed
            await changed.wait()

    def _active_count(self):
        return sum(1 for execution in self._running.values() if not execution.waiting_resources)

    def _signal_availability(self):
        self._availability_changed.set()
        self._availability_changed = asyncio.Event()
        self._wake.set()

    def _release_resources(self, step):
        released = False
        for resource in step["request"]["resources"]:
            if self._resources.get(resource) == step["id"]:
                del self._resources[resource]
                released = True
        if released:
            self._signal_availability()

    async def _finish_action(self, execution, step_id, outcome: ActionResult):
        async with self._lock:
            detail = await self.get_task(execution.task_id)
            step = next((step for step in detail["steps"] if step["id"] == step_id), None)
            if step is None:
                raise ValueError("执行步骤不存在。")
            if step["execution_id"] != execution.execution_id and step_id not in execution.recovering_steps:
                raise ValueError("步骤不属于此执行或恢复执行。")
            if step.get("outcome") == outcome and outcome["status"] != "uncertain":
                return
            step.update(status=outcome["status"], outcome=deepcopy(outcome), finished_at=datetime.now(timezone.utc).isoformat())
            if outcome["status"] == "succeeded":
                for artifact in outcome["artifacts"]:
                    if artifact not in detail["artifacts"]:
                        detail["artifacts"].append(artifact)
            if outcome["status"] == "uncertain":
                await self._question(detail, execution.execution_id, "无法确定上次动作结果，请确认该步骤已完成、未执行或已停止。", step_id)
            else:
                text = f"工具实际结果：{outcome['status']}"
                if detail["task"]["state"] == "running":
                    detail["task"]["progress"] = text
                await self._commit(detail, [{"kind": "state", "text": text, "step_id": step_id}])
                self._release_resources(step)
            if execution.current_step is not None and execution.current_step["id"] == step_id:
                execution.current_step.update(status=outcome["status"], outcome=outcome)
                execution.settled.set()
                if detail["task"]["state"] in {"pausing", "cancelling"}:
                    self._stop_execution(execution)

    async def _question(self, detail, execution_id, text, step_id=None, *, replacement_for=None):
        if step_id is not None and any(q["step_id"] == step_id and q["status"] == "open" for q in detail["questions"]):
            detail["task"].update(state="waiting_user", progress="等待用户答复")
            await self._commit(detail, [{"kind": "state", "text": "等待用户答复"}])
            return
        question = {"id": uuid4().hex, "task_id": detail["task"]["id"], "execution_id": execution_id,
                    "step_id": step_id, "text": text, "status": "open", "answer": None,
                    "created_at": datetime.now(timezone.utc).isoformat()}
        if replacement_for is not None:
            question.update(requested_operation="file.create", replacement_for=replacement_for)
        detail["questions"].append(question)
        detail["task"].update(state="waiting_user", progress="等待用户答复")
        await self._commit(detail, [{"kind": "question", "text": text, "question_id": question["id"], "step_id": step_id}])

    async def _complete(self, execution, text):
        async with self._lock:
            detail = await self.get_task(execution.task_id)
            task = detail["task"]
            if task["execution_id"] != execution.execution_id:
                raise ValueError("执行已被替换。")
            if task["stop_intent"] or self._shutdown:
                return False
            if execution.model_call_id is not None and execution.pending_inputs(detail):
                return False
            if not isinstance(text, str) or not text.strip():
                raise ValueError("Worker 须提供明确的完成结果。")
            if task["state"] != "running":
                raise ValueError("任务当前尚待处理，无法提交完成结果。")
            if any(step["status"] in {"started", "uncertain"} for step in detail["steps"]):
                raise ValueError("实际动作尚未核实，任务保持未完成。")
            pending = [item for item in detail["inputs"] if not item["consumed"] and item["kind"] == "supplement"]
            if pending:
                text += "\n原结果已经生成，后续补充尚未执行；新增操作请建立关联任务。"
            task.update(state="completed", progress="已完成", result=text, error="")
            await self._commit(detail, [{"kind": "result", "text": text}])
            return True

    async def request_shutdown(self):
        return await self._submitted(self._request_shutdown())

    async def _request_shutdown(self):
        async with self._lock:
            if not self._shutdown:
                await self._store.set_shutdown(True)
                self._shutdown = True
            self._shutdown_error = ""
            try:
                for detail in await self._store.load_unfinished():
                    task = detail["task"]
                    if (task["id"] in self._faults or task["stop_intent"] or task["stop_reason"] == "shutdown"
                            or task["state"] in {"waiting_user", "paused", "storage_error"}):
                        continue
                    task["stop_reason"] = "shutdown"
                    if task["id"] in self._running:
                        task.update(state="pausing", progress="等待当前步骤结束后退出")
                    await self._commit(detail, [{"kind": "state", "text": "正在准备退出"}])
                    execution = self._running.get(task["id"])
                    if execution is not None:
                        self._stop_execution(execution)
            except (sqlite3.Error, OSError) as exc:
                self._shutdown_error = str(exc)
            return self._shutdown_progress()

    def _stop_execution(self, execution):
        step = execution.current_step
        if not execution.stop_requested and (step is None or step["status"] != "started" or step["request"]["cancellable"]):
            execution.stop_requested = True
            execution.task.cancel()

    async def shutdown_progress(self):
        async with self._lock:
            return self._shutdown_progress()

    def _shutdown_progress(self):
        return {"requested": self._shutdown, "ready": self._shutdown and not self._running and not self._faults and not self._shutdown_error,
                "waiting": [{"task_id": task_id, "execution_id": execution.execution_id,
                             "step_id": execution.current_step["id"] if execution.current_step is not None else None}
                            for task_id, execution in self._running.items()],
                "error": "；".join([self._shutdown_error] + [detail["task"]["error"] for detail in self._faults.values()]).strip("；")}

    async def withdraw_shutdown(self):
        return await self._submitted(self._withdraw_shutdown())

    async def _withdraw_shutdown(self):
        async with self._lock:
            await self._store.set_shutdown(False)
            self._shutdown = False
            self._shutdown_error = ""
            for detail in await self._store.load_unfinished():
                task = detail["task"]
                if task["id"] not in self._faults and task["stop_reason"] == "shutdown" and task["stop_intent"] is None:
                    task.update(stop_reason=None, state="running" if task["id"] in self._running else "recovering", progress="已撤回退出")
                    await self._commit(detail, [{"kind": "state", "text": "已撤回退出"}])
            self._wake.set()
            return self._shutdown_progress()

    async def close(self):
        await self.request_shutdown()
        while self._running:
            await asyncio.gather(*(execution.task for execution in list(self._running.values())), return_exceptions=False)
        if not (await self.shutdown_progress())["ready"]:
            raise RuntimeError("任务事实尚未保存，请解决保存错误后重试退出。")
        if self._dispatcher is not None:
            self._dispatcher.cancel()
            with suppress(asyncio.CancelledError):
                await self._dispatcher


class _Execution:
    def __init__(self, runtime, task_id, execution_id):
        self.runtime = runtime
        self.task_id = task_id
        self.execution_id = execution_id
        self.current_step = None
        self.final_step = False
        self.protocol_request_id = None
        self.model_call_id = None
        self.recovering_steps = set()
        self.stop_requested = False
        self.waiting_resources = False
        self.settled = asyncio.Event()
        self.settled.set()
        self.task: asyncio.Task

    async def start_action(self, request):
        return await self.runtime._start_action(self, request)

    async def finish_action(self, step_id, result):
        with anyio.CancelScope(shield=True):
            try:
                await self.runtime._finish_action(self, step_id, result)
            finally:
                self.settled.set()

    async def get_task(self):
        return await self.runtime.get_task(self.task_id)

    @staticmethod
    def active_inputs(detail):
        completed = {step["id"] for step in detail["steps"] if step["status"] == "succeeded"}
        return [item for item in detail["inputs"] if item.get("after_step") not in completed]

    @staticmethod
    def pending_inputs(detail):
        return [item for item in _Execution.active_inputs(detail) if not item["consumed"]]

    async def record_model(self, messages, consumed=()):
        async with self.runtime._lock:
            detail = await self.get_task()
            if detail["task"]["execution_id"] != self.execution_id or detail["task"]["state"] != "running":
                raise asyncio.CancelledError()
            record = next(record for record in detail["executions"] if record["id"] == self.execution_id)
            record["messages"] = deepcopy(messages)
            for item in detail["inputs"]:
                if item["id"] in consumed:
                    item["consumed"] = True
            await self.runtime._commit(detail, [])
            return bool(self.pending_inputs(detail))

    async def read(self, kind, identifier, section="task", offset=0, limit=2000):
        """只按任务关系与明确引用回读，正文按字符分页。"""
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 4000:
            raise ValueError("回读偏移须为非负整数，每次 1～4000 字符。")
        detail = await self.get_task()
        if kind == "task":
            if identifier not in {self.task_id, detail["related_task_id"], *detail["references"]}:
                raise PermissionError("仅能读取本任务、关联任务及明确引用的任务。")
            if section not in {"task", "scope", "inputs", "steps", "executions", "questions", "artifacts"}:
                raise ValueError("任务回读部分无效。")
            target = await self.runtime.get_task(identifier)
            text = json.dumps(target[section], ensure_ascii=False)
        elif kind == "history":
            def read(connection):
                row = connection.execute("SELECT id, reply_id, text FROM display_messages WHERE id = ?", (identifier,)).fetchone()
                if row is None:
                    raise ValueError("引用消息不存在。")
                if row["reply_id"] != detail["task"]["source"]["reply_id"] and identifier not in detail["references"]:
                    raise PermissionError("仅能读取来源对话及明确引用的消息。")
                return row["text"]
            text = await self.runtime._conversation_store.task_io(read)
        else:
            raise ValueError("回读类型须为 task 或 history。")
        end = offset + limit
        return {"id": identifier, "text": text[offset:end], "total_characters": len(text),
                "next_offset": end if end < len(text) else None}

    async def ask(self, text, *, replacement_for=None):
        async with self.runtime._lock:
            detail = await self.get_task()
            if detail["task"]["execution_id"] != self.execution_id or detail["task"]["state"] != "running":
                raise asyncio.CancelledError()
            await self.runtime._question(detail, self.execution_id, text, replacement_for=replacement_for)

    async def complete(self, text):
        return await self.runtime._complete(self, text)
