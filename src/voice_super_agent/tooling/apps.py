import ctypes
from ctypes import wintypes
import json
import os
from dataclasses import dataclass
from pathlib import Path
import subprocess
import time
from typing import Iterable, Literal, Protocol


@dataclass(frozen=True, slots=True)
class Application:
    name: str
    kind: Literal["apps", "exe"]
    target: str
    executable: Path | None = None


class AppPlatform(Protocol):
    def installed(self) -> list[Application]: ...
    def find_window(self, application: Application) -> int | None: ...
    def show_window(self, window: int) -> None: ...
    def launch(self, application: Application) -> None: ...


class ApplicationTools:
    def __init__(self, grants: Iterable[str], platform: AppPlatform):
        self._grants = frozenset(target.strip().casefold() for target in grants)
        self._platform = platform

    def resolve(self, target: str) -> Application:
        target = target.strip()
        normalized = target.casefold()
        if normalized not in self._grants:
            raise PermissionError("未授权打开该应用。")
        path = Path(target)
        if path.is_absolute():
            if (path.drive.startswith("\\\\") or path.suffix.casefold() != ".exe"
                    or any(":" in part for part in path.parts[1:])):
                raise ValueError("请输入有效的本机应用程序绝对路径。")
            resolved = path.resolve(strict=True)
            if not resolved.is_file() or resolved.suffix.casefold() != ".exe":
                raise ValueError("指定路径需要指向本机应用程序文件。")
            return Application(resolved.stem, "exe", str(resolved))
        candidates = {
            app.target.casefold(): app for app in self._platform.installed()
            if normalized in {app.name.casefold(), app.target.casefold()}
        }
        if not candidates:
            raise ValueError(f"找不到应用「{target}」，请指定开始菜单中的名称或应用程序绝对路径。")
        if len(candidates) != 1:
            names = "、".join(f"{app.name}（{app.target}）" for app in candidates.values())
            raise ValueError(f"存在多个同名应用，请指定目标：{names}")
        return next(iter(candidates.values()))

    def open(self, application: Application) -> dict:
        existing = self._platform.find_window(application)
        if existing is None:
            self._platform.launch(application)
            return {"target": application.target, "outcome": "launch_requested"}
        self._platform.show_window(existing)
        return {"target": application.target, "outcome": "window_shown", "window": existing}


class WindowsApps:
    """通过应用标识或可执行文件核对窗口归属。"""

    def __init__(self):
        self._user = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._shell = ctypes.WinDLL("shell32", use_last_error=True)
        self._enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self._user.EnumWindows.argtypes = [self._enum_callback, wintypes.LPARAM]
        self._user.EnumChildWindows.argtypes = [wintypes.HWND, self._enum_callback, wintypes.LPARAM]
        self._user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self._user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self._user.IsWindowVisible.argtypes = [wintypes.HWND]
        self._user.IsIconic.argtypes = [wintypes.HWND]
        self._user.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
        self._user.SetForegroundWindow.argtypes = [wintypes.HWND]
        self._user.GetForegroundWindow.argtypes = []
        self._user.GetForegroundWindow.restype = wintypes.HWND
        self._kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel.OpenProcess.restype = wintypes.HANDLE
        self._kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        ]
        self._kernel.GetApplicationUserModelId.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
        ]
        self._shell.ShellExecuteW.argtypes = [
            wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int,
        ]
        self._shell.ShellExecuteW.restype = ctypes.c_void_p

    def installed(self) -> list[Application]:
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        script = """
            $ErrorActionPreference = 'Stop'
            [Console]::OutputEncoding = [Text.Encoding]::UTF8
            $shell = New-Object -ComObject Shell.Application
            $items = $shell.NameSpace('shell:AppsFolder').Items()
            $catalog = foreach ($item in $items) {
                [pscustomobject]@{
                    Name = $item.Name
                    AppID = $item.Path
                    Executable = $item.ExtendedProperty('System.Link.TargetParsingPath')
                }
            }
            ConvertTo-Json -InputObject @($catalog) -Compress
        """
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
            creationflags=subprocess.CREATE_NO_WINDOW,
            capture_output=True, text=True, encoding="utf-8-sig", timeout=10,
        )
        if result.returncode:
            raise RuntimeError(f"无法读取开始菜单应用：{result.stderr.strip()}")
        entries = [item for item in json.loads(result.stdout) if item["Name"] and item["AppID"]]
        paths: dict[str, Path] = {}
        owners: dict[Path, set[str]] = {}
        for item in entries:
            if not item["Executable"]:
                continue
            path = Path(item["Executable"])
            if not path.is_absolute() or path.suffix.casefold() != ".exe" or not path.is_file():
                continue
            path = path.resolve()
            paths[item["AppID"]] = path
            owners.setdefault(path, set()).add(item["AppID"])
        applications = []
        for item in entries:
            path = paths.get(item["AppID"])
            executable = path if path is not None and len(owners[path]) == 1 else None
            applications.append(Application(item["Name"], "apps", item["AppID"], executable))
        return applications

    def _matches(self, window: int, application: Application) -> bool:
        pid = wintypes.DWORD()
        self._user.GetWindowThreadProcessId(window, ctypes.byref(pid))
        process = self._kernel.OpenProcess(0x1000, False, pid.value)
        if not process:
            # 枚举期间窗口可能关闭，也可能属于当前用户无权读取的进程。
            return False
        try:
            if application.kind == "apps":
                app_id = ctypes.create_unicode_buffer(256)
                size = wintypes.DWORD(len(app_id))
                if self._kernel.GetApplicationUserModelId(process, ctypes.byref(size), app_id) == 0:
                    return app_id.value.casefold() == application.target.casefold()
            executable = Path(application.target) if application.kind == "exe" else application.executable
            if executable is None:
                return False
            # 传统桌面程序没有进程 AppID 时，使用开始菜单登记的唯一可执行文件。
            image = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(image))
            return bool(self._kernel.QueryFullProcessImageNameW(process, 0, image, ctypes.byref(size))
                        and Path(image.value) == executable)
        finally:
            self._kernel.CloseHandle(process)

    def find_window(self, application: Application) -> int | None:
        found = None

        def visit(window, _):
            nonlocal found
            if not self._user.IsWindowVisible(window) or not self._user.GetWindowTextLengthW(window):
                return True
            if self._matches(window, application):
                found = window
                return False
            if application.kind == "apps":
                # 部分商店应用的顶层窗口由系统宿主拥有，应用身份在子窗口进程。
                def visit_child(child, _data):
                    nonlocal found
                    if self._matches(child, application):
                        found = window
                        return False
                    return True

                self._user.EnumChildWindows(window, self._enum_callback(visit_child), 0)
            return found is None

        self._user.EnumWindows(self._enum_callback(visit), 0)
        return found

    def show_window(self, window: int) -> None:
        if self._user.IsIconic(window):
            self._user.ShowWindowAsync(window, 9)
        self._user.SetForegroundWindow(window)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if self._user.GetForegroundWindow() == window and not self._user.IsIconic(window):
                return
            time.sleep(0.02)
        raise RuntimeError("Windows 未将该应用窗口切到前台。")

    def launch(self, application: Application) -> None:
        target = "shell:AppsFolder\\" + application.target if application.kind == "apps" else application.target
        result = self._shell.ShellExecuteW(None, "open", target, None, None, 1)
        if not result or result <= 32:
            raise RuntimeError(f"Windows 无法启动应用「{application.name}」（返回值 {result or 0}）。")
