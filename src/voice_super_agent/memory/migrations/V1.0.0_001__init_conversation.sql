-- 用户确认版本：V1.0.0_001；SQLite user_version 对应序号 1。
BEGIN;

CREATE TABLE display_messages (
    position INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL,
    source TEXT NOT NULL,
    reply_id TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    error TEXT NOT NULL
);

CREATE TABLE model_messages (
    position INTEGER PRIMARY KEY,
    display_message_id TEXT REFERENCES display_messages(id),
    message_json TEXT NOT NULL
);

CREATE TABLE working_window (
    id INTEGER PRIMARY KEY,
    display_start_position INTEGER NOT NULL,
    model_start_position INTEGER NOT NULL
);

INSERT INTO working_window VALUES (1, 0, 0);
PRAGMA user_version = 1;
COMMIT;
