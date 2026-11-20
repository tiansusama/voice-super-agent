-- 用户确认版本：V1.0.1_004；任务事实、派工批次、通知与独立运行设置。
BEGIN;

CREATE TABLE task_batches (
    source_message_id TEXT PRIMARY KEY REFERENCES display_messages(id),
    reply_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('text', 'voice')),
    created_at TEXT NOT NULL
);

CREATE TABLE task_records (
    position INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    source_message_id TEXT NOT NULL REFERENCES task_batches(source_message_id),
    batch_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'queued', 'running', 'waiting_user', 'pausing', 'paused', 'cancelling',
        'cancelled', 'recovering', 'storage_error', 'failed', 'completed'
    )),
    priority INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    UNIQUE (source_message_id, batch_key)
);
CREATE INDEX idx_task_records_dispatch ON task_records(state, priority DESC, position);

CREATE TABLE task_notifications (
    position INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL REFERENCES task_records(id),
    kind TEXT NOT NULL,
    question_id TEXT,
    step_id TEXT,
    text TEXT NOT NULL,
    read INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_task_notifications_unread ON task_notifications(task_id, read);

CREATE TABLE task_runtime_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    concurrency_limit INTEGER NOT NULL CHECK (concurrency_limit > 0),
    shutdown_requested INTEGER NOT NULL CHECK (shutdown_requested IN (0, 1))
);
INSERT INTO task_runtime_settings VALUES (1, 2, 0);

PRAGMA user_version = 4;
COMMIT;
