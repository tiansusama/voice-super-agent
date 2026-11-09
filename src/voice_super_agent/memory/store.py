import asyncio
from concurrent.futures import ThreadPoolExecutor
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

    async def close(self) -> None:
        try:
            if self._connection is not None:
                await asyncio.get_running_loop().run_in_executor(self._executor, self._connection.close)
        finally:
            self._executor.shutdown(wait=True)
