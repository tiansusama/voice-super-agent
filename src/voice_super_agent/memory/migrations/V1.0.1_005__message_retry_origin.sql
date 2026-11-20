-- 用户确认版本：V1.0.1_005；重发消息保留首次请求的归属。
BEGIN;
ALTER TABLE display_messages
    ADD COLUMN retry_origin_message_id TEXT REFERENCES display_messages(id);
PRAGMA user_version = 5;
COMMIT;
