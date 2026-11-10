import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


class ConversationStore:
    """同一 I/O 线程独占 SQLite 连接，相关展示与原生提交在一个事务中保存。"""

    def __init__(self, database_path: str | Path):
        self._path = database_path
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="conversation-store")
        self._connection: sqlite3.Connection | None = None

    async def open(self) -> dict:
        return await asyncio.get_running_loop().run_in_executor(self._executor, self._open)

    def _open(self) -> dict:
        self._connection = sqlite3.connect(self._path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            script = Path(__file__).with_name("migrations") / "V1.0.0_001__init_conversation.sql"
            self._connection.executescript(script.read_text(encoding="utf-8"))
        if version < 2:
            script = Path(__file__).with_name("migrations") / "V1.0.0_002__index_history_reply.sql"
            self._connection.executescript(script.read_text(encoding="utf-8"))
        messages = [dict(row) for row in self._connection.execute("SELECT * FROM display_messages ORDER BY position")]
        records = [
            {"display_message_id": row["display_message_id"], "message": json.loads(row["message_json"])}
            for row in self._connection.execute("SELECT * FROM model_messages ORDER BY position")
        ]
        window = dict(self._connection.execute("SELECT * FROM working_window WHERE id = 1").fetchone())
        for message in messages:
            del message["position"]
        del window["id"]
        return {"messages": messages, "model_records": records, "window": window}

    async def save(self, messages: list[dict], model_records: list[dict], window: dict) -> None:
        await asyncio.get_running_loop().run_in_executor(self._executor, self._save, messages, model_records, window)

    def _save(self, messages: list[dict], model_records: list[dict], window: dict) -> None:
        # 上下文引用的展示消息先写入；任一语句失败时，整轮关联更新回滚。
        with self._connection:
            self._connection.executemany(
                """INSERT INTO display_messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET text=excluded.text, status=excluded.status, error=excluded.error""",
                [(position, message["id"], message["role"], message["source"], message["reply_id"],
                  message["text"], message["status"], message["created_at"], message["error"])
                 for position, message in enumerate(messages)],
            )
            self._connection.executemany(
                """INSERT INTO model_messages VALUES (?, ?, ?)
                   ON CONFLICT(position) DO UPDATE SET
                       display_message_id=excluded.display_message_id, message_json=excluded.message_json""",
                [(position, record["display_message_id"], json.dumps(record["message"], ensure_ascii=False))
                 for position, record in enumerate(model_records)],
            )
            self._connection.execute("DELETE FROM model_messages WHERE position >= ?", (len(model_records),))
            self._connection.execute(
                "UPDATE working_window SET display_start_position=?, model_start_position=? WHERE id=1",
                (window["display_start_position"], window["model_start_position"]),
            )

    async def history_search(self, *, keywords: list[str] | None = None, start_time: str | None = None,
                             end_time: str | None = None, limit: int = 5, before_position: int | None = None,
                             exclude_reply_id: str | None = None) -> dict:
        """按原文字面和半开时间范围定位；位置游标向更早记录翻页。"""
        keywords = [] if keywords is None else keywords
        if (not isinstance(keywords, list) or len(keywords) > 8
                or any(not isinstance(word, str) or not word.strip() or len(word) > 100 for word in keywords)):
            raise ValueError("keywords 接受最多 8 个非空关键词，每词最多 100 个字符。")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("limit 必须是 1～20 的整数。")
        if before_position is not None and (type(before_position) is not int or before_position < 0):
            raise ValueError("before_position 必须是非负整数。")
        times = []
        for value in (start_time, end_time):
            if value is None:
                times.append(None)
                continue
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                raise ValueError("时间必须是带时区的 ISO 8601 时间。")
            times.append(parsed.astimezone(timezone.utc).isoformat())
        if times[0] and times[1] and times[0] >= times[1]:
            raise ValueError("start_time 必须早于 end_time。")
        if not keywords and not any(times):
            return {"status": "needs_clarification", "message": "请先向用户确认主题、关键词或大致时间。"}
        return await asyncio.get_running_loop().run_in_executor(
            self._executor, self._history_search, keywords, *times, limit, before_position, exclude_reply_id,
        )

    def _history_search(self, keywords, start_time, end_time, limit, before_position, exclude_reply_id):
        conditions, arguments = [], []
        if keywords:
            conditions.append("(" + " OR ".join("instr(lower(text), lower(?)) > 0" for _ in keywords) + ")")
            arguments.extend(keywords)
        for value, operator in ((start_time, ">="), (end_time, "<")):
            if value is not None:
                conditions.append(f"julianday(created_at) {operator} julianday(?)")
                arguments.append(value)
        if before_position is not None:
            conditions.append("position < ?")
            arguments.append(before_position)
        if exclude_reply_id is not None:
            conditions.append("reply_id != ?")
            arguments.append(exclude_reply_id)
        rows = self._connection.execute(
            "SELECT position, id, role, source, reply_id, status, created_at, error, "
            "substr(text, 1, 160) AS preview, length(text) AS total_characters FROM display_messages WHERE "
            + " AND ".join(conditions) + " ORDER BY position DESC LIMIT ?", (*arguments, limit + 1),
        ).fetchall()
        return {"status": "found" if rows else "not_found", "matches": [dict(row) for row in rows[:limit]],
                "next_before_position": rows[limit - 1]["position"] if len(rows) > limit else None}

    async def history_read(self, *, message_id: str, offset: int = 0, limit: int = 2000,
                           related_offset: int = 0) -> dict:
        """正文偏移按 Unicode 字符计数，同轮关联单独分页。"""
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("message_id 必须是检索返回的消息 ID。")
        if type(offset) is not int or offset < 0 or type(related_offset) is not int or related_offset < 0:
            raise ValueError("offset 和 related_offset 必须是非负整数。")
        if type(limit) is not int or not 1 <= limit <= 8000:
            raise ValueError("limit 必须是 1～8000 的整数。")
        return await asyncio.get_running_loop().run_in_executor(
            self._executor, self._history_read, message_id, offset, limit, related_offset,
        )

    def _history_read(self, message_id, offset, limit, related_offset):
        row = self._connection.execute(
            "SELECT position, id, role, source, reply_id, status, created_at, error, "
            "substr(text, ?, ?) AS text, length(text) AS total_characters FROM display_messages WHERE id = ?",
            (offset + 1, limit, message_id),
        ).fetchone()
        if row is None:
            return {"status": "not_found", "message_id": message_id}
        if offset > row["total_characters"]:
            raise ValueError("offset 超出正文字符数，请从返回的 next_offset 续读。")
        related = self._connection.execute(
            "SELECT id, role, source, status, created_at, length(text) AS total_characters "
            "FROM display_messages WHERE reply_id = ? ORDER BY position LIMIT 11 OFFSET ?",
            (row["reply_id"], related_offset),
        ).fetchall()
        previous = self._connection.execute(
            "SELECT id FROM display_messages WHERE position < ? ORDER BY position DESC LIMIT 1", (row["position"],),
        ).fetchone()
        following = self._connection.execute(
            "SELECT id FROM display_messages WHERE position > ? ORDER BY position LIMIT 1", (row["position"],),
        ).fetchone()
        next_offset = offset + len(row["text"])
        return {"status": "found", "message": dict(row), "offset": offset,
                "next_offset": next_offset if next_offset < row["total_characters"] else None,
                "related": [dict(message) for message in related[:10]],
                "next_related_offset": related_offset + 10 if len(related) > 10 else None,
                "previous_message_id": previous["id"] if previous else None,
                "next_message_id": following["id"] if following else None}

    async def close(self) -> None:
        try:
            if self._connection is not None:
                await asyncio.get_running_loop().run_in_executor(self._executor, self._connection.close)
        finally:
            self._executor.shutdown(wait=True)
