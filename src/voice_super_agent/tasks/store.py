from datetime import datetime, timezone
from graphlib import CycleError, TopologicalSorter
import json
from typing import get_args
from uuid import uuid4

from voice_super_agent.memory.store import ConversationStore
from voice_super_agent.tasks.interface import Operation, TaskBatch, TaskChange, TaskDetail, TaskPage, TaskPlan, TaskSource


class TaskStore:
    """每项任务保存完整执行事实，批次及通知与任务变化在同一事务内提交。"""

    def __init__(self, conversation_store: ConversationStore):
        self._store = conversation_store

    async def dispatch(self, source: TaskSource, plans: list[TaskPlan]) -> TaskBatch:
        return await self._store.task_io(self._dispatch, source, plans)

    def _dispatch(self, connection, source, plans):
        with connection:
            existing = self._find_batch(connection, source["message_id"])
            if existing is not None:
                return existing
            message = connection.execute(
                "SELECT role, source, reply_id, status, text FROM display_messages WHERE id = ?", (source["message_id"],),
            ).fetchone()
            if (message is None or message["role"] != "user" or message["status"] != "completed"
                    or message["source"] != source["source"] or message["reply_id"] != source["reply_id"]):
                raise ValueError("派工来源必须是已保存的正式用户消息，并保留原回复和输入来源。")
            if not plans:
                raise ValueError("派工至少包含一个明确任务。")
            keys = [plan["key"] for plan in plans]
            if any(not key.strip() for key in keys) or len(set(keys)) != len(keys):
                raise ValueError("同一批次的任务 key 必须非空且唯一。")
            identifiers = {key: uuid4().hex for key in keys}
            dependencies = {}
            for plan in plans:
                if not plan["title"].strip() or not plan["goal"].strip() or not plan["scope"]:
                    raise ValueError("任务需要名称、明确目标和用户确认的操作范围。")
                for grant in plan["scope"]:
                    if grant["operation"] not in get_args(Operation) or not grant["target"].strip():
                        raise ValueError("任务操作类型或目标无效。")
                if type(plan.get("priority", 0)) is not int:
                    raise ValueError("任务优先级必须是整数。")
                resolved = []
                for dependency in plan.get("depends_on", []):
                    if dependency in identifiers:
                        resolved.append(identifiers[dependency])
                    elif connection.execute("SELECT 1 FROM task_records WHERE id = ?", (dependency,)).fetchone():
                        resolved.append(dependency)
                    else:
                        raise ValueError("任务依赖必须引用本批次 key 或已有任务 ID。")
                dependencies[identifiers[plan["key"]]] = resolved
                if plan.get("related_task_id") and not connection.execute(
                        "SELECT 1 FROM task_records WHERE id = ?", (plan["related_task_id"],)).fetchone():
                    raise ValueError("关联任务不存在。")
            try:
                tuple(TopologicalSorter(dependencies).static_order())
            except CycleError as exc:
                raise ValueError("任务依赖存在循环，请调整执行安排。") from exc
            now = datetime.now(timezone.utc).isoformat()
            connection.execute("INSERT INTO task_batches VALUES (?, ?, ?, ?)",
                               (source["message_id"], source["reply_id"], source["source"], now))
            summaries = []
            for plan in plans:
                task_id = identifiers[plan["key"]]
                task = {"id": task_id, "title": plan["title"], "goal": plan["goal"], "state": "queued",
                        "source": dict(source), "execution_id": None, "stop_intent": None, "stop_reason": None,
                        "progress": "已排队", "result": "", "error": "", "actions": ["supplement", "pause", "cancel"],
                        "unread": False, "created_at": now, "updated_at": now}
                detail = {"task": task, "priority": plan.get("priority", 0), "scope": plan["scope"],
                          "depends_on": dependencies[task_id], "related_task_id": plan.get("related_task_id"),
                          "references": plan.get("references", []), "executions": [], "steps": [], "questions": [],
                          "artifacts": [], "inputs": [{"id": uuid4().hex, "kind": "dispatch", "source": dict(source),
                                                       "text": message["text"], "consumed": False, "created_at": now}]}
                connection.execute(
                    "INSERT INTO task_records (id, source_message_id, batch_key, state, priority, created_at, updated_at, detail_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (task_id, source["message_id"], plan["key"], "queued", detail["priority"], now, now,
                     json.dumps(detail, ensure_ascii=False)),
                )
                self._append_changes(connection, task_id, [{"kind": "state", "text": "已排队"}], now)
                summaries.append(task)
            return {"source": dict(source), "tasks": summaries}

    async def find_batch(self, message_id: str) -> TaskBatch | None:
        return await self._store.task_io(self._find_batch, message_id)

    def _find_batch(self, connection, message_id):
        source = connection.execute("SELECT source_message_id AS message_id, reply_id, source FROM task_batches "
                                    "WHERE source_message_id = ?", (message_id,)).fetchone()
        if source is None:
            return None
        tasks = [json.loads(row[0])["task"] for row in connection.execute(
            "SELECT detail_json FROM task_records WHERE source_message_id = ? ORDER BY position", (message_id,))]
        return {"source": dict(source), "tasks": tasks}

    async def get_task(self, task_id: str) -> TaskDetail:
        return await self._store.task_io(self._get_task, task_id)

    def _get_task(self, connection, task_id):
        row = connection.execute("SELECT detail_json FROM task_records WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise ValueError("任务不存在。")
        return json.loads(row[0])

    async def list_tasks(self, cursor: int | None = None, limit: int = 20) -> TaskPage:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("任务每页条数必须是 1～50 的整数。")
        if cursor is not None and (type(cursor) is not int or cursor < 0):
            raise ValueError("任务游标必须是非负整数。")
        return await self._store.task_io(self._list_tasks, cursor, limit)

    def _list_tasks(self, connection, cursor, limit):
        condition, arguments = ("", ()) if cursor is None else (" WHERE position < ?", (cursor,))
        rows = connection.execute("SELECT position, json_extract(detail_json, '$.task') AS summary FROM task_records"
                                  + condition + " ORDER BY position DESC LIMIT ?", (*arguments, limit + 1)).fetchall()
        return {"tasks": [json.loads(row["summary"]) for row in rows[:limit]],
                "next_cursor": rows[limit - 1]["position"] if len(rows) > limit else None}

    async def load_unfinished(self) -> list[TaskDetail]:
        return await self._store.task_io(self._load_unfinished)

    def _load_unfinished(self, connection):
        return [json.loads(row[0]) for row in connection.execute(
            "SELECT detail_json FROM task_records WHERE state NOT IN ('completed', 'cancelled', 'failed') "
            "ORDER BY priority DESC, position")]

    async def save_task(self, detail: TaskDetail, changes: list[dict]) -> list[TaskChange]:
        return await self._store.task_io(self._save_task, detail, changes)

    def _save_task(self, connection, detail, changes):
        task = detail["task"]
        with connection:
            task["updated_at"] = datetime.now(timezone.utc).isoformat()
            unread = connection.execute("SELECT 1 FROM task_notifications WHERE task_id = ? AND read = 0 LIMIT 1", (task["id"],)).fetchone()
            # 已读状态以通知表为准，执行器持有的旧详情只提交任务事实。
            task["unread"] = unread is not None or any(change["kind"] != "state" for change in changes)
            connection.execute("UPDATE task_records SET state = ?, priority = ?, updated_at = ?, detail_json = ? WHERE id = ?",
                               (task["state"], detail["priority"], task["updated_at"], json.dumps(detail, ensure_ascii=False), task["id"]))
            return self._append_changes(connection, task["id"], changes, task["updated_at"])

    def _append_changes(self, connection, task_id, changes, now):
        positions = []
        for change in changes:
            cursor = connection.execute("INSERT INTO task_notifications "
                                        "(id, task_id, kind, question_id, step_id, text, read, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                        (uuid4().hex, task_id, change["kind"], change.get("question_id"), change.get("step_id"),
                                         change["text"], int(change["kind"] == "state"), now))
            positions.append(cursor.lastrowid)
        if not positions:
            return []
        return self._list_changes(connection, positions[0] - 1, len(positions))

    async def list_changes(self, after_position: int = 0, limit: int = 50) -> list[TaskChange]:
        if type(after_position) is not int or after_position < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("通知游标须为非负整数，每页 1～100 条。")
        return await self._store.task_io(self._list_changes, after_position, limit)

    async def last_change_position(self) -> int:
        return await self._store.task_io(lambda connection: connection.execute(
            "SELECT COALESCE(MAX(position), 0) FROM task_notifications").fetchone()[0])

    def _list_changes(self, connection, after_position, limit):
        rows = connection.execute("SELECT n.*, b.source FROM task_notifications n JOIN task_records t ON t.id = n.task_id "
                                  "JOIN task_batches b ON b.source_message_id = t.source_message_id "
                                  "WHERE n.position > ? ORDER BY n.position LIMIT ?", (after_position, limit)).fetchall()
        return [dict(row, read=bool(row["read"])) for row in rows]

    async def mark_read(self, notification_id: str) -> None:
        await self._store.task_io(self._mark_read, notification_id)

    def _mark_read(self, connection, notification_id):
        with connection:
            row = connection.execute("SELECT task_id FROM task_notifications WHERE id = ?", (notification_id,)).fetchone()
            if row is None:
                raise ValueError("任务通知不存在。")
            connection.execute("UPDATE task_notifications SET read = 1 WHERE id = ?", (notification_id,))
            unread = connection.execute("SELECT 1 FROM task_notifications WHERE task_id = ? AND read = 0 LIMIT 1", (row[0],)).fetchone()
            detail = self._get_task(connection, row[0])
            detail["task"]["unread"] = unread is not None
            connection.execute("UPDATE task_records SET detail_json = ? WHERE id = ?", (json.dumps(detail, ensure_ascii=False), row[0]))

    async def get_concurrency(self) -> int:
        return await self._store.task_io(self._get_concurrency)

    def _get_concurrency(self, connection):
        return connection.execute("SELECT concurrency_limit FROM task_runtime_settings WHERE id = 1").fetchone()[0]

    async def apply_concurrency(self, limit: int) -> int:
        if type(limit) is not int or limit < 1:
            raise ValueError("任务并发上限必须是正整数。")
        return await self._store.task_io(self._apply_concurrency, limit)

    def _apply_concurrency(self, connection, limit):
        with connection:
            connection.execute("UPDATE task_runtime_settings SET concurrency_limit = ? WHERE id = 1", (limit,))
        return limit

    async def set_shutdown(self, requested: bool) -> None:
        await self._store.task_io(self._set_shutdown, requested)

    def _set_shutdown(self, connection, requested):
        with connection:
            connection.execute("UPDATE task_runtime_settings SET shutdown_requested = ? WHERE id = 1", (int(requested),))
