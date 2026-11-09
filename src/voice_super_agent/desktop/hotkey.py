import ctypes
from ctypes import wintypes


user32 = ctypes.WinDLL("user32")
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = wintypes.SHORT
MODIFIERS = {"CTRL": 0x11, "ALT": 0x12, "SHIFT": 0x10, "WIN": 0x5B}


class HoldHotkey:
    """轮询全局组合键，持续按住一秒后触发一次，释放后重新计时。"""

    def __init__(self, combination: str):
        parts = [part.strip().upper() for part in combination.split("+")]
        keys = {"\\": 0xDC, "SPACE": 0x20, "ENTER": 0x0D, "TAB": 0x09,
                **{f"F{index}": 0x6F + index for index in range(1, 25)},
                **{chr(code): code for code in range(0x30, 0x5B) if chr(code).isalnum()}}
        ordinary = [part for part in parts if part not in MODIFIERS]
        if len(ordinary) != 1 or ordinary[0] not in keys or len(set(parts)) != len(parts):
            raise ValueError("组合键格式无效，请使用 Ctrl+\\、Ctrl+Shift+K 或 F8 等键位。")
        self._modifiers = {MODIFIERS[part] for part in parts if part in MODIFIERS}
        self._key = keys[ordinary[0]]
        self._started: float | None = None
        self._fired = False

    def pressed(self) -> bool:
        return bool(user32.GetAsyncKeyState(self._key) & 0x8000) and all(
            bool((user32.GetAsyncKeyState(key) | (user32.GetAsyncKeyState(0x5C) if key == 0x5B else 0)) & 0x8000) == (key in self._modifiers)
            for key in MODIFIERS.values()
        )

    def sample(self, pressed: bool, now: float) -> bool:
        if not pressed:
            self._started = None
            self._fired = False
        elif self._started is None:
            self._started = now
        elif not self._fired and now - self._started >= 1:
            self._fired = True
            return True
        return False
