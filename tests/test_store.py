from pathlib import Path
from contextlib import closing
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from voice_super_agent.memory.store import ConversationStore


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.path = Path(self.directory.name) / "agent.sqlite3"
        self.store = ConversationStore(self.path)
        await self.store.open()
        self.messages = [
            {"id": "user", "role": "user", "source": "text", "reply_id": "reply", "text": "问题",
             "status": "completed", "created_at": "2026-11-09T00:00:00+00:00", "error": ""},
            {"id": "reply", "role": "assistant", "source": "text", "reply_id": "reply", "text": "界面已展示的完整文字",
             "status": "interrupted", "created_at": "2026-11-09T00:00:00+00:00", "error": ""},
        ]
        self.records = [
            {"display_message_id": "user", "message": {"role": "user", "content": "问题"}},
            {"display_message_id": "reply", "message": {"role": "assistant", "content": "原生实际提交片段"}},
        ]
        self.window = {"display_start_position": 0, "model_start_position": 0}

    async def asyncTearDown(self):
        await self.store.close()
        self.directory.cleanup()

    async def test_reopen_preserves_display_native_order_and_links(self):
        await self.store.save(self.messages, self.records, self.window)
        await self.store.close()
        self.store = ConversationStore(self.path)
        saved = await self.store.open()
        self.assertEqual(saved, {"messages": self.messages, "model_records": self.records, "window": self.window})
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)

    async def test_failed_native_reference_rolls_back_display_update(self):
        await self.store.save(self.messages, self.records, self.window)
        changed = [dict(message) for message in self.messages]
        changed[1]["text"] = "本次事务尚未完成的新文字"
        invalid = [{"display_message_id": "missing", "message": {"role": "assistant", "content": "错误关联"}}]
        with self.assertRaises(sqlite3.IntegrityError):
            await self.store.save(changed, invalid, {"display_start_position": 1, "model_start_position": 1})
        await self.store.close()
        self.store = ConversationStore(self.path)
        saved = await self.store.open()
        self.assertEqual(saved["messages"], self.messages)
        self.assertEqual(saved["model_records"], self.records)
        self.assertEqual(saved["window"], self.window)


if __name__ == "__main__":
    unittest.main()
