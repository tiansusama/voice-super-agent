"""独立于主对话和语音流水线的任务执行与恢复。"""

from voice_super_agent.tasks.recovery import ActionReconciler
from voice_super_agent.tasks.runtime import TaskRuntime
from voice_super_agent.tasks.worker import TaskWorker


def create_task_runtime(store, tools, *, api_key, model="deepseek-flash",
                        base_url="https://api.deepseek.com/v1", on_change=None):
    worker = TaskWorker(tools.create_server, api_key=api_key, model=model, base_url=base_url)
    return TaskRuntime(store, worker, ActionReconciler(tools.inspect_action), on_change=on_change)
