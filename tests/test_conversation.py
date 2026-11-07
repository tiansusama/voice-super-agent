import asyncio
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from aiohttp import web
from loguru import logger

from voice_super_agent.conversation import TextConversation
from voice_super_agent.runtime import Runtime


class ConversationTests(unittest.IsolatedAsyncioTestCase):
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
