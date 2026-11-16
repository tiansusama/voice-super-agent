-- 用户确认版本：V1.0.0_003；保存交互设置及待应用的唤醒词。
BEGIN;
CREATE TABLE interaction_settings (
    name TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);
PRAGMA user_version = 3;
COMMIT;
