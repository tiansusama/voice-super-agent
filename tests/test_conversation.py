import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from aiohttp import web
from loguru import logger
from pipecat.frames.frames import LLMContextFrame

from voice_super_agent.conversation import Conversation as TextConversation
from voice_super_agent.runtime import Runtime
from voice_super_agent.memory.store import ConversationStore


class ConversationFixture:
    async def asyncSetUp(self):
        logger.disable("pipecat")
        self.requests = []
        self.events = []
        self.changed = asyncio.Event()
        self.release_old = asyncio.Event()
        app = web.Application()
        app.router.add_post("/chat/completions", self.complete)
        self.server = web.AppRunner(app)
        await self.server.setup()
        site = web.TCPSite(self.server, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{self.server.addresses[0][1]}"
        self.conversation = TextConversation("test-key", "test-model", self.url, self.record)
        await self.conversation.start()

    async def asyncTearDown(self):
        self.release_old.set()
        await self.conversation.close()
        await self.server.cleanup()
        logger.enable("pipecat")

    def record(self, event):
        self.events.append(event)
        self.changed.set()

    async def wait_event(self, reply_id, kind):
        async with asyncio.timeout(5):
            while True:
                self.changed.clear()
                matching = [e for e in self.events if e.get("reply_id") == reply_id and e["type"] == kind]
                if matching:
                    return matching[-1]
                await self.changed.wait()

    async def complete(self, request):
        payload = await request.json()
        self.requests.append(deepcopy(payload))
        prompt = payload["messages"][-1]["content"]
        if prompt in {"error", "auth"}:
            return web.json_response({"error": {"message": "测试服务拒绝请求", "type": "test_error"}},
                                     status=503 if prompt == "error" else 401)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        chunks = ["旧回复片段", "迟到旧片段"] if prompt == "slow" else ["答：", prompt]
        try:
            for index, text in enumerate(chunks):
                if prompt == "slow" and index == 1:
                    await self.release_old.wait()
                chunk = {"id": "test", "object": "chat.completion.chunk", "created": 0,
                         "model": "test-model", "choices": [{"index": 0, "delta": {"content": text},
                                                               "finish_reason": None}]}
                await response.write(f"data: {json.dumps(chunk)}\n\n".encode())
            await response.write(b"data: [DONE]\n\n")
            await response.write_eof()
        except ConnectionResetError:
            # 客户端接管时关闭旧流，服务端继续产生的片段无法写入。
            pass
        return response


class ConversationTests(ConversationFixture, unittest.IsolatedAsyncioTestCase):
    async def test_stream_and_followup_use_native_context(self):
        await self.conversation.send("one", "first")
        self.assertEqual((await self.wait_event("one", "finished"))["status"], "completed")
        await self.conversation.send("two", "followup")
        await self.wait_event("two", "finished")
        history = [(m["role"], m["content"]) for m in self.requests[-1]["messages"] if m["role"] != "system"]
        self.assertEqual(history, [("user", "first"), ("assistant", "答：first"), ("user", "followup")])
        self.assertEqual([e["text"] for e in self.events if e["type"] == "text" and e["reply_id"] == "one"],
                         ["答：", "first"])

    async def test_takeover_commits_partial_before_followups_and_rejects_old_stream(self):
        await self.conversation.send("old", "slow")
        await self.wait_event("old", "text")
        await self.conversation.send("new", "correction")
        self.assertEqual((await self.wait_event("old", "finished"))["status"], "interrupted")
        await self.conversation.send("last", "supplement")
        self.release_old.set()
        await self.wait_event("last", "finished")
        context = self.conversation.context.get_messages()
        self.assertEqual([(m["role"], m["content"]) for m in context[:3]],
                         [("user", "slow"), ("assistant", "旧回复片段"), ("user", "correction")])
        self.assertEqual([m["content"] for m in context if m["role"] == "user"],
                         ["slow", "correction", "supplement"])
        self.assertFalse(any("迟到" in e.get("text", "") for e in self.events))
        self.assertEqual("".join(e["text"] for e in self.events if e["type"] == "text" and e["reply_id"] == "last"),
                         "答：supplement")

    async def test_request_error_has_no_automatic_retry_and_next_input_works(self):
        await self.conversation.send("failed", "error")
        self.assertIn("503", (await self.wait_event("failed", "error"))["error"])
        self.assertEqual((await self.wait_event("failed", "finished"))["status"], "failed")
        self.assertEqual(len(self.requests), 1)
        await self.conversation.send("next", "continue")
        self.assertEqual((await self.wait_event("next", "finished"))["status"], "completed")

    async def test_runtime_acknowledges_bursts_and_reconnect_restores_detached_snapshots(self):
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key", "DEEPSEEK_BASE_URL": self.url}):
            runtime = Runtime()
        runtime.start()
        try:
            runtime.connect("main")
            accepted = runtime.send_text("slow")
            async with asyncio.timeout(5):
                while True:
                    event = await asyncio.to_thread(runtime.events.get, True, 2)
                    if event["snapshot"]["messages"][-1]["text"] == "旧回复片段":
                        frozen = event["snapshot"]
                        break
            runtime.disconnect("main")
            runtime.update_windows(main_window="hidden")
            second = runtime.send_text("correction")
            third = runtime.send_text("supplement")
            runtime.command("show_main")
            self.release_old.set()
            async with asyncio.timeout(5):
                while True:
                    event = await asyncio.to_thread(runtime.events.get, True, 2)
                    if event["kind"] == "state" and event["snapshot"]["messages"][-1]["status"] == "completed":
                        break
            restored = runtime.connect("main")
            self.assertEqual([m["text"] for m in restored["messages"] if m["role"] == "user"],
                             ["slow", "correction", "supplement"])
            self.assertEqual(restored["messages"][1]["status"], "interrupted")
            self.assertEqual(restored["messages"][-1]["reply_id"], third["reply_id"])
            self.assertEqual(restored["messages"][-1]["text"], "答：supplement")
            self.assertEqual(frozen["messages"][1]["status"], "streaming")
            self.assertEqual(frozen["messages"][1]["reply_id"], accepted["reply_id"])
            self.assertNotEqual(second["reply_id"], third["reply_id"])
        finally:
            await asyncio.to_thread(runtime.stop)

    async def test_auth_failure_keeps_runtime_accessible_and_rejects_new_input(self):
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key", "DEEPSEEK_BASE_URL": self.url}):
            runtime = Runtime()
        runtime.start()
        try:
            runtime.send_text("auth")
            async with asyncio.timeout(5):
                while True:
                    event = await asyncio.to_thread(runtime.events.get, True, 2)
                    if not event["snapshot"]["state"]["model_available"]:
                        break
            self.assertTrue(all(m["status"] == "failed" for m in runtime.connect("main")["messages"]
                                if m["role"] == "assistant"))
            runtime.command("show_main")
            with self.assertRaisesRegex(ValueError, "重启"):
                runtime.send_text("next")
        finally:
            await asyncio.to_thread(runtime.stop)


class PersistenceTests(ConversationFixture, unittest.IsolatedAsyncioTestCase):
    def runtime(self, path):
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key", "DEEPSEEK_BASE_URL": self.url}):
            runtime = Runtime(database_path=path)
        runtime.start()
        return runtime

    async def wait_snapshot(self, runtime, predicate):
        async with asyncio.timeout(5):
            while True:
                snapshot = runtime.connect("main")
                if predicate(snapshot):
                    return snapshot
                await asyncio.sleep(0.02)

    async def test_restart_restores_completed_round_and_native_followup(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "agent.sqlite3"
            runtime = self.runtime(path)
            try:
                runtime.send_text("first")
                before = await self.wait_snapshot(runtime, lambda s: s["messages"][-1]["status"] == "completed"
                                                  and s["state"]["storage_status"] == "saved")
            finally:
                await asyncio.to_thread(runtime.stop)
            runtime = self.runtime(path)
            try:
                self.assertEqual(runtime.connect("main")["messages"], before["messages"])
                self.assertEqual(len(self.requests), 1)
                runtime.send_text("followup")
                await self.wait_snapshot(runtime, lambda s: s["messages"][-1]["status"] == "completed")
                history = [{"role": m["role"], "content": m["content"]} for m in self.requests[-1]["messages"] if m["role"] != "system"]
                self.assertEqual(history, [{"role": "user", "content": "first"},
                                          {"role": "assistant", "content": "答：first"},
                                          {"role": "user", "content": "followup"}])
            finally:
                await asyncio.to_thread(runtime.stop)

    async def test_exit_during_stream_preserves_partial_and_waits_for_new_input(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "agent.sqlite3"
            runtime = self.runtime(path)
            try:
                runtime.send_text("slow")
                await self.wait_snapshot(runtime, lambda s: s["messages"][-1]["text"] == "旧回复片段")
                self.assertTrue(await asyncio.to_thread(runtime.prepare_exit))
                before = runtime.connect("main")
                self.assertEqual(before["messages"][-1]["status"], "unfinished")
            finally:
                await asyncio.to_thread(runtime.stop)
            runtime = self.runtime(path)
            try:
                restored = runtime.connect("main")
                self.assertEqual(restored["messages"], before["messages"])
                self.assertIsNone(restored["state"]["active_reply_id"])
                self.assertEqual(len(self.requests), 1)
                runtime.send_text("continue")
                await self.wait_snapshot(runtime, lambda s: s["messages"][-1]["status"] == "completed")
                history = [{"role": m["role"], "content": m["content"]} for m in self.requests[-1]["messages"] if m["role"] != "system"]
                self.assertEqual(history, [{"role": "user", "content": "slow"},
                                          {"role": "assistant", "content": "旧回复片段"},
                                          {"role": "user", "content": "continue"}])
            finally:
                await asyncio.to_thread(runtime.stop)

    async def test_exit_after_user_commit_before_assistant_start_preserves_native_input(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "agent.sqlite3"
            entered = Event()
            gate = asyncio.Event()

            def blocked_conversation(*args, **kwargs):
                conversation = TextConversation(*args, **kwargs)

                @conversation._llm.event_handler("on_before_process_frame")
                async def before_model(service, frame):
                    if isinstance(frame, LLMContextFrame):
                        entered.set()
                        await gate.wait()

                return conversation

            with patch("voice_super_agent.runtime.Conversation", side_effect=blocked_conversation):
                runtime = self.runtime(path)
                try:
                    runtime.send_text("first")
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    self.assertTrue(await asyncio.to_thread(runtime.prepare_exit))
                    self.assertEqual(self.requests, [])
                finally:
                    runtime._loop.call_soon_threadsafe(gate.set)
                    await asyncio.to_thread(runtime.stop)
            runtime = self.runtime(path)
            try:
                self.assertEqual(runtime.connect("main")["messages"][-1]["status"], "unfinished")
                runtime.send_text("followup")
                await self.wait_snapshot(runtime, lambda s: s["messages"][-1]["status"] == "completed")
                history = [(m["role"], m["content"]) for m in self.requests[-1]["messages"] if m["role"] != "system"]
                self.assertEqual(history, [("user", "first"), ("user", "followup")])
            finally:
                await asyncio.to_thread(runtime.stop)

    async def test_restored_window_uses_native_text_and_keeps_older_history(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "agent.sqlite3"
            store = ConversationStore(path)
            await store.open()
            messages = [
                {"id": key, "role": role, "source": "text", "reply_id": reply, "text": text,
                 "status": "completed", "created_at": "2026-11-09T00:00:00+00:00", "error": ""}
                for key, role, reply, text in [("old-user", "user", "old-reply", "旧工作范围"),
                                              ("old-reply", "assistant", "old-reply", "旧回复"),
                                              ("user", "user", "reply", "当前问题"),
                                              ("reply", "assistant", "reply", "界面已展示的完整文字")]
            ]
            records = [{"display_message_id": message["id"], "message": {"role": message["role"], "content": message["text"]}}
                       for message in messages]
            records[-1]["message"]["content"] = "原生实际提交片段"
            await store.save(messages, records, {"display_start_position": 2, "model_start_position": 2})
            await store.close()
            runtime = self.runtime(path)
            try:
                self.assertEqual(runtime.connect("main")["messages"], messages[2:])
                runtime.send_text("followup")
                await self.wait_snapshot(runtime, lambda s: s["messages"][-1]["status"] == "completed")
                history = [{"role": m["role"], "content": m["content"]} for m in self.requests[-1]["messages"] if m["role"] != "system"]
                self.assertEqual(history, [{"role": "user", "content": "当前问题"},
                                          {"role": "assistant", "content": "原生实际提交片段"},
                                          {"role": "user", "content": "followup"}])
            finally:
                await asyncio.to_thread(runtime.stop)
            store = ConversationStore(path)
            saved = await store.open()
            try:
                self.assertEqual(saved["messages"][:4], messages)
                self.assertEqual(saved["window"], {"display_start_position": 2, "model_start_position": 2})
            finally:
                await store.close()

    async def test_save_failure_pauses_input_and_retains_exit_until_user_retries(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "agent.sqlite3"
            runtime = self.runtime(path)
            connection = sqlite3.connect(path)
            connection.execute("CREATE TRIGGER fail_save BEFORE INSERT ON display_messages BEGIN SELECT RAISE(ABORT, '测试保存失败'); END")
            connection.commit()
            try:
                runtime.send_text("first")
                failed = await self.wait_snapshot(runtime, lambda s: s["state"]["storage_status"] == "error")
                self.assertIn("测试保存失败", failed["state"]["storage_error"])
                with self.assertRaisesRegex(ValueError, "保存对话失败"):
                    runtime.send_text("rejected")
                self.assertFalse(await asyncio.to_thread(runtime.prepare_exit))
                self.assertFalse(runtime.connect("main")["state"]["exiting"])
                runtime.command("show_main")
                connection.execute("DROP TRIGGER fail_save")
                connection.commit()
                self.assertTrue(await asyncio.to_thread(runtime.prepare_exit))
                saved_messages = runtime.connect("main")["messages"]
                self.assertEqual(runtime.connect("main")["state"]["storage_status"], "saved")
            finally:
                connection.close()
                await asyncio.to_thread(runtime.stop)
            runtime = self.runtime(path)
            try:
                self.assertEqual(runtime.connect("main")["messages"], saved_messages)
            finally:
                await asyncio.to_thread(runtime.stop)

    async def test_slow_io_keeps_commands_responsive_and_newer_state_pending(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "agent.sqlite3"
            runtime = self.runtime(path)
            entered, release = Event(), Event()
            original = runtime._store._save

            def slow_save(*args):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("测试未释放写入线程")
                original(*args)

            try:
                with patch.object(runtime._store, "_save", side_effect=slow_save):
                    runtime.send_text("first")
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    runtime.command("start_activity")
                    runtime.command("show_main")
                    snapshot = await self.wait_snapshot(runtime, lambda s: s["messages"][-1]["status"] == "completed"
                                                        and s["state"]["activity_tick"] > 0)
                    self.assertEqual(snapshot["state"]["storage_status"], "saving")
                    runtime.send_text("followup")
                    latest = await self.wait_snapshot(runtime, lambda s: len(s["messages"]) == 4
                                                      and s["messages"][-1]["status"] == "completed")
                    release.set()
                    await self.wait_snapshot(runtime, lambda s: s["state"]["storage_status"] == "saved")
            finally:
                release.set()
                await asyncio.to_thread(runtime.stop)
            store = ConversationStore(path)
            saved = await store.open()
            try:
                self.assertEqual(saved["messages"], latest["messages"])
                self.assertEqual(saved["model_records"][-1]["message"]["content"], "答：followup")
            finally:
                await store.close()


class MissingConfigurationTests(unittest.TestCase):
    def test_invalid_model_address_reports_configuration_error_and_exits(self):
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key", "DEEPSEEK_BASE_URL": "http://localhost:abc"}):
            runtime = Runtime()
        runtime.start()
        try:
            runtime.send_text("你好")
            while True:
                event = runtime.events.get(timeout=2)
                if not event["snapshot"]["state"]["model_available"]:
                    break
            snapshot = runtime.connect("main")
            self.assertIn("DEEPSEEK_BASE_URL", snapshot["state"]["model_error"])
            self.assertEqual(snapshot["messages"][-1]["status"], "failed")
            runtime.command("show_main")
        finally:
            runtime.stop()

    def test_missing_model_key_keeps_window_commands_available(self):
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": ""}):
            runtime = Runtime()
        runtime.start()
        try:
            snapshot = runtime.connect("main")
            self.assertFalse(snapshot["state"]["model_available"])
            self.assertIn("DEEPSEEK_API_KEY", snapshot["state"]["model_error"])
            runtime.command("show_main")
            with self.assertRaisesRegex(ValueError, "DEEPSEEK_API_KEY"):
                runtime.send_text("你好")
            self.assertEqual(runtime.connect("main")["messages"], [])
        finally:
            runtime.stop()


if __name__ == "__main__":
    unittest.main()
