"""主对话、桌面和内置工具共同使用的任务数据与异步接口。"""

from collections.abc import Awaitable, Callable
from typing import Literal, NotRequired, Protocol, TypedDict


TaskState = Literal[
    "queued", "running", "waiting_user", "pausing", "paused", "cancelling",
    "cancelled", "recovering", "storage_error", "failed", "completed",
]
TaskAction = Literal["supplement", "answer", "pause", "resume", "cancel", "retry"]
Operation = Literal["application.open", "window.show", "window.hide", "directory.list", "file.read", "file.create"]
StopIntent = Literal["pause", "cancel"]


class TaskSource(TypedDict):
    """来源由宿主从已保存的正式用户消息注入。"""

    message_id: str
    reply_id: str
    source: Literal["text", "voice"]


class ScopeGrant(TypedDict):
    operation: Operation
    target: str


class TaskPlan(TypedDict):
    """依赖优先匹配本批次 key，其余引用已有任务 ID，提交后统一保存实际 ID。"""

    key: str
    title: str
    goal: str
    scope: list[ScopeGrant]
    depends_on: NotRequired[list[str]]
    priority: NotRequired[int]
    related_task_id: NotRequired[str]
    references: NotRequired[list[str]]


class TaskQuestion(TypedDict):
    id: str
    task_id: str
    execution_id: str | None
    step_id: str | None
    text: str
    status: Literal["open", "answered", "closed"]
    answer: str | None
    created_at: str
    requested_operation: NotRequired[Literal["file.create"]]
    replacement_for: NotRequired[str]


class TaskSummary(TypedDict):
    id: str
    title: str
    goal: str
    state: TaskState
    source: TaskSource
    execution_id: str | None
    stop_intent: StopIntent | None
    stop_reason: Literal["user", "shutdown"] | None
    progress: str
    result: str
    error: str
    actions: list[TaskAction]
    unread: bool
    created_at: str
    updated_at: str


class TaskPage(TypedDict):
    tasks: list[TaskSummary]
    next_cursor: int | None


class TaskBatch(TypedDict):
    source: TaskSource
    tasks: list[TaskSummary]


class TaskCommandPayload(TypedDict, total=False):
    source: TaskSource
    text: str
    question_id: str
    scope: list[ScopeGrant]
    resolution: Literal["succeeded", "not_executed", "cancelled"]


class TaskDetail(TypedDict):
    task: TaskSummary
    priority: int
    scope: list[ScopeGrant]
    depends_on: list[str]
    related_task_id: str | None
    references: list[str]
    executions: list[dict]
    steps: list[dict]
    inputs: list[dict]
    questions: list[TaskQuestion]
    artifacts: list[dict]


class ActionRequest(TypedDict):
    """工具解析真实目标后申请授权，resources 使用该目标的稳定资源标识。"""

    tool: str
    arguments: dict
    operation: Operation
    target: str
    resources: list[str]
    cancellable: bool
    expected: dict
    request_id: NotRequired[str | int]


class ActionResult(TypedDict):
    status: Literal["succeeded", "not_executed", "uncertain", "failed", "cancelled"]
    result: dict
    artifacts: list[dict]
    error: str


class TaskChange(TypedDict):
    """只有已提交的变化产生稳定通知 ID，重连按 ID 读取和去重。"""

    id: str
    position: int
    task_id: str
    kind: Literal["state", "question", "result", "error", "supplement"]
    source: Literal["text", "voice"]
    question_id: str | None
    step_id: str | None
    text: str
    read: bool
    created_at: str


class ShutdownProgress(TypedDict):
    requested: bool
    ready: bool
    waiting: list[dict]
    error: str


class ExecutionAccess(Protocol):
    """绑定一次执行的动作入口，工具在真实动作前申请、实际结束后登记。"""

    task_id: str
    execution_id: str

    async def start_action(self, request: ActionRequest) -> str: ...

    async def finish_action(self, step_id: str, result: ActionResult) -> None: ...


class TaskRuntimeFault(TypedDict):
    """写入失败时报告内存中的真实结果，区别于已经提交的持久通知。"""

    kind: Literal["storage_error"]
    task_id: str
    error: str
    known_result: ActionResult | None


TaskChangeHandler = Callable[[TaskChange | TaskRuntimeFault], Awaitable[None]]


class TaskService(Protocol):
    """命令等待存储提交，长工具动作由独立 Worker 推进。"""

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def dispatch(self, source: TaskSource, plans: list[TaskPlan]) -> TaskBatch: ...

    async def find_batch(self, message_id: str) -> TaskBatch | None: ...

    async def task_command(self, task_id: str, action: TaskAction,
                           payload: TaskCommandPayload | None = None) -> TaskSummary: ...

    async def list_tasks(self, cursor: int | None = None, limit: int = 20) -> TaskPage: ...

    async def get_task(self, task_id: str) -> TaskDetail: ...

    async def apply_concurrency(self, limit: int) -> int: ...

    async def get_concurrency(self) -> int: ...

    async def list_changes(self, after_position: int = 0, limit: int = 50) -> list[TaskChange]: ...

    async def mark_read(self, notification_id: str) -> None: ...

    async def next_change(self) -> TaskChange | TaskRuntimeFault: ...

    async def request_shutdown(self) -> ShutdownProgress: ...

    async def withdraw_shutdown(self) -> ShutdownProgress: ...
