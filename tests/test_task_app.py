import asyncio
from types import SimpleNamespace

from voice_super_agent import app
from voice_super_agent.memory.store import ConversationStore
from voice_super_agent.tasks.store import TaskStore


def test_desktop_entry_keeps_unknown_application_action_waiting_for_confirmation(tmp_path, monkeypatch):
    page = tmp_path / "frontend/dist/index.html"
    page.parent.mkdir(parents=True)
    page.write_text("<html></html>", encoding="utf-8")
    target = str(tmp_path / "approved.exe")
    ran = []

    class Runtime:
        def __init__(self, *, task_factory, **options):
            self.factory = task_factory

        def start(self):
            asyncio.run(self.check_recovery())

        async def check_recovery(self):
            owner = ConversationStore(tmp_path / "app.sqlite3")
            await owner.open()
            await owner.save([{"id": "source", "role": "user", "reply_id": "reply", "source": "text",
                               "text": f"打开 {target}", "status": "completed", "created_at": "2026-11-20", "error": ""}],
                             [], {"display_start_position": 0, "model_start_position": 0})
            store = TaskStore(owner)
            batch = await store.dispatch({"message_id": "source", "reply_id": "reply", "source": "text"}, [{
                "key": "app", "title": "显示应用", "goal": "显示已有应用窗口", "scope": [{"operation": "application.open", "target": target}],
            }])
            task_id = batch["tasks"][0]["id"]
            detail = await store.get_task(task_id)
            detail["task"].update(state="running", execution_id="old")
            detail["executions"].append({"id": "old", "state": "running"})
            detail["steps"].append({"id": "show", "execution_id": "old", "status": "started", "outcome": None,
                                    "request": {"tool": "open_application", "arguments": {"target": target},
                                                "operation": "application.open", "target": target,
                                                "resources": [f"application:{target.casefold()}"], "cancellable": False,
                                                "expected": {"application": target}}})
            await store.save_task(detail, [])

            async def notify(change):
                pass

            service = self.factory(owner, notify)
            await service.start()
            try:
                async with asyncio.timeout(2):
                    while True:
                        detail = await service.get_task(task_id)
                        if detail["task"]["state"] in {"waiting_user", "completed"}:
                            break
                        await asyncio.sleep(0.005)
                assert detail["task"]["state"] == "waiting_user"
                assert detail["questions"][0]["step_id"] == "show"
                assert not ran
            finally:
                await service.close()
                await owner.close()

        def stop(self):
            pass

    async def worker(self, execution):
        ran.append(execution.task_id)
        await execution.complete("旧窗口已被误认作完成")

    monkeypatch.setattr("voice_super_agent.tasks.worker.TaskWorker.__call__", worker)
    monkeypatch.setattr(app, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["voice-super-agent"])
    monkeypatch.setattr(app, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(app, "SingleInstance", lambda: SimpleNamespace(
        primary=True, watch=lambda callback: None, stop_watching=lambda: None, close=lambda: None))
    monkeypatch.setattr(app, "Runtime", Runtime)
    monkeypatch.setattr(app, "WindowsApps", lambda: SimpleNamespace(find_window=lambda application: 123))
    monkeypatch.setattr(app, "Desktop", lambda *args: SimpleNamespace(
        set_main_visibility=lambda visible: None, get_main_visibility=lambda: None,
        activate=lambda: None, stop=lambda: None))
    monkeypatch.setattr(app.webview, "start", lambda **options: None)
    app.main()
