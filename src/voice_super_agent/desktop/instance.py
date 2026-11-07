import ctypes
from ctypes import wintypes
from threading import Thread
from collections.abc import Callable


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.SetEvent.argtypes = [wintypes.HANDLE]
kernel32.SetEvent.restype = wintypes.BOOL
kernel32.WaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD]
kernel32.WaitForMultipleObjects.restype = wintypes.DWORD
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


class SingleInstance:
    """以当前 Windows 会话的内核对象存续期确定主实例，并传递唤出通知。"""

    def __init__(self, name: str = r"Local\VoiceSuperAgent"):
        self._mutex = kernel32.CreateMutexW(None, False, name)
        if not self._mutex:
            raise ctypes.WinError(ctypes.get_last_error())
        self.primary = ctypes.get_last_error() != 183
        # 通知事件可以先由第二次启动创建，信号会保留到主窗口准备完成。
        self._activate = kernel32.CreateEventW(None, False, False, name + ".Activate")
        self._stop = kernel32.CreateEventW(None, True, False, None)
        if not self._activate or not self._stop:
            raise ctypes.WinError(ctypes.get_last_error())
        self._thread: Thread | None = None

    def notify(self) -> None:
        if not kernel32.SetEvent(self._activate):
            raise ctypes.WinError(ctypes.get_last_error())

    def watch(self, activate: Callable[[], None]) -> None:
        self._thread = Thread(target=self._listen, args=(activate,), name="instance-notify")
        self._thread.start()

    def _listen(self, activate: Callable[[], None]) -> None:
        handles = (wintypes.HANDLE * 2)(self._stop, self._activate)
        while True:
            result = kernel32.WaitForMultipleObjects(2, handles, False, 0xFFFFFFFF)
            if result == 0:
                return
            if result == 1:
                activate()
            else:
                raise ctypes.WinError(ctypes.get_last_error())

    def stop_watching(self) -> None:
        if self._thread is not None:
            kernel32.SetEvent(self._stop)
            self._thread.join()
            self._thread = None

    def close(self) -> None:
        self.stop_watching()
        for handle in (self._stop, self._activate, self._mutex):
            kernel32.CloseHandle(handle)
