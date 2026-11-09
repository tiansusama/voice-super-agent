from dataclasses import dataclass
import os


@dataclass
class WakeSettings:
    word: str = "你好小博"
    hotkey: str = "Ctrl+\\"
    idle_seconds: int = 60

    @classmethod
    def from_environment(cls):
        try:
            seconds = int(os.getenv("VOICE_IDLE_TIMEOUT_SECONDS", "60"))
        except ValueError as exc:
            raise ValueError("语音空闲超时需要是大于 0 的整数秒。") from exc
        settings = cls(os.getenv("VOICE_WAKE_WORD", "你好小博"), os.getenv("VOICE_WAKE_HOTKEY", "Ctrl+\\"), seconds)
        if settings.idle_seconds <= 0:
            raise ValueError("语音空闲超时需要是大于 0 的整数秒。")
        return settings


class VoiceLifecycle:
    """以真实活动集合维护等待期限，并把结束意图关联到当前回复。"""

    def __init__(self, idle_seconds: int = 60):
        self.idle_seconds = idle_seconds
        self.active = False
        self.busy: set[str] = set()
        self.deadline: float | None = None
        self.ending_reply: str | None = None
        self.finished_reply: str | None = None

    def activate(self, now: float):
        self.active = True
        self.deadline = None if self.busy else now + self.idle_seconds

    def deactivate(self):
        self.active = False
        self.busy.clear()
        self.deadline = None
        self.ending_reply = self.finished_reply = None

    def activity(self, name: str, active: bool, now: float):
        if (name in self.busy) == active:
            return
        if active:
            self.busy.add(name)
        else:
            self.busy.discard(name)
        self.deadline = now + self.idle_seconds if self.active and not self.busy else None

    def new_input(self):
        self.ending_reply = self.finished_reply = None

    def should_end(self, now: float) -> bool:
        if not self.active or self.busy:
            return False
        return ((self.ending_reply is not None and self.finished_reply == self.ending_reply)
                or (self.deadline is not None and now >= self.deadline))
