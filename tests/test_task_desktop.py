from voice_super_agent.desktop.host import DesktopApi
from voice_super_agent.runtime import Runtime


def test_desktop_task_operations_share_the_runtime_state():
    class TaskService:
        async def start(self):
            self.state = "running"
            self.limit = 2

        async def close(self):
            self.state = "closed"

        async def get_concurrency(self):
            return self.limit

        async def apply_concurrency(self, limit):
            self.limit = limit
            return self.limit

        async def list_tasks(self, cursor, limit):
            assert (cursor, limit) == (9, 20)
            return {"tasks": [{"id": "task", "state": self.state}], "next_cursor": None}

        async def get_task(self, task_id):
            return {"task": {"id": task_id, "state": self.state}}

        async def task_command(self, task_id, action, payload):
            assert (task_id, action, payload) == ("task", "answer", {"question_id": "question", "text": "确认"})
            self.state = "queued"
            return {"id": task_id, "state": self.state}

    tasks = TaskService()
    runtime = Runtime(task_factory=lambda store, notify: tasks)
    runtime.start()
    try:
        api = DesktopApi(runtime, "main")
        initial = api.connect()
        assert initial["state"]["task_concurrency"] == 2
        assert api.list_tasks(9)["tasks"] == [{"id": "task", "state": "running"}]
        api.task_command("task", "answer", {"question_id": "question", "text": "确认"})
        assert api.get_task("task")["task"]["state"] == "queued"
        assert api.save_task_concurrency(1) == 1
        changed = api.connect()
        assert changed["state"]["task_revision"] > initial["state"]["task_revision"]
        assert changed["state"]["task_concurrency"] == 1
    finally:
        runtime.stop()
    assert tasks.state == "closed"
