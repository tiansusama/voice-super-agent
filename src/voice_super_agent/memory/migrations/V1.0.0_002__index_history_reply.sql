-- 用户确认版本：V1.0.0_002；按同轮消息和保存顺序读取关联原文。
BEGIN;
CREATE INDEX idx_display_messages_reply_position ON display_messages (reply_id, position);
PRAGMA user_version = 2;
COMMIT;
