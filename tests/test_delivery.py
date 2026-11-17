import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import shutil
import subprocess
import time

import psutil
import pytest


@pytest.fixture
def delivery(tmp_path):
    root = tmp_path / "project 小博"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "scripts"
    for name in ("prepare-dev.ps1", "start.ps1"):
        shutil.copy2(source / name, scripts / name)
    (scripts / "prepare-wake-model.ps1").write_text("Add-Content $env:DELIVERY_CALLS Model", encoding="utf-8-sig")
    (root / ".env.example").write_text("DEEPSEEK_API_KEY=\n", encoding="utf-8")
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "uv.cmd").write_text(
        '@echo off\necho uv %*>>"%DELIVERY_CALLS%"\n'
        'if "%DELIVERY_SLOW%"=="1" powershell.exe -NoProfile -Command "Start-Sleep -Seconds 60"\n'
        'if "%DELIVERY_FAIL_UV%"=="1" exit /b 17\nexit /b 0\n', encoding="utf-8")
    (tools / "npm.cmd").write_text(
        '@echo off\necho npm %*>>"%DELIVERY_CALLS%"\n'
        'if "%~3"=="ls" if "%DELIVERY_NEEDS_NPM%"=="1" exit /b 1\n'
        'if "%~3"=="ci" if "%DELIVERY_FAIL_NPM%"=="1" exit /b 23\nexit /b 0\n', encoding="utf-8")
    environment = {**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"],
                   "DELIVERY_CALLS": str(tmp_path / "calls.log"), "DELIVERY_SLOW": "0",
                   "DELIVERY_FAIL_UV": "0", "DELIVERY_NEEDS_NPM": "0", "DELIVERY_FAIL_NPM": "0"}
    return root, environment


@pytest.mark.parametrize("needs_npm", [False, True])
def test_prepare_from_other_directory_preserves_configuration_and_installs_missing_dependencies(delivery, tmp_path, needs_npm):
    root, environment = delivery
    environment["DELIVERY_NEEDS_NPM"] = "1" if needs_npm else "0"
    (root / ".env").write_text("DEEPSEEK_API_KEY=existing-value\n", encoding="utf-8")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                             str(root / "scripts/prepare-dev.ps1")], cwd=tmp_path, env=environment,
                            capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    calls = Path(environment["DELIVERY_CALLS"]).read_text(encoding="utf-8")
    assert "sync --locked --project" in calls and str(root) in calls
    assert (" ci " in calls) == needs_npm
    assert "run build" in calls and "Model" in calls
    assert (root / ".env").read_text(encoding="utf-8") == "DEEPSEEK_API_KEY=existing-value\n"


@pytest.mark.parametrize("failure,code", [("DELIVERY_FAIL_UV", 17), ("DELIVERY_FAIL_NPM", 23)])
def test_prepare_stops_after_failure_and_keeps_later_steps_unstarted(delivery, tmp_path, failure, code):
    root, environment = delivery
    environment.update(DELIVERY_NEEDS_NPM="1", **{failure: "1"})
    result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                             str(root / "scripts/prepare-dev.ps1")], cwd=tmp_path, env=environment,
                            capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == code
    calls = Path(environment["DELIVERY_CALLS"]).read_text(encoding="utf-8")
    assert "run build" not in calls and "Model" not in calls
    if failure == "DELIVERY_FAIL_UV":
        assert "npm" not in calls and not (root / ".env").exists()


def test_closing_preparation_window_cancels_owned_process_tree(delivery, tmp_path):
    root, environment = delivery
    environment["DELIVERY_SLOW"] = "1"
    process = subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-STA", "-File",
                                str(root / "scripts/start.ps1")], cwd=tmp_path, env=environment,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    user32 = ctypes.windll.user32
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    handles = []
    children = []
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            def inspect(handle, _):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
                if pid.value == process.pid and user32.IsWindowVisible(handle):
                    handles.append(handle)
                return True
            user32.EnumWindows(callback_type(inspect), 0)
            if handles and Path(environment["DELIVERY_CALLS"]).exists():
                children = psutil.Process(process.pid).children(recursive=True)
                if len(children) >= 2:
                    break
            time.sleep(.05)
        assert handles and children
        assert user32.PostMessageW(handles[0], 0x0010, 0, 0)
        output, error = process.communicate(timeout=10)
        assert process.returncode == 1, (output, error)
        assert all(not child.is_running() for child in children)
        assert "npm" not in Path(environment["DELIVERY_CALLS"]).read_text(encoding="utf-8")
    finally:
        if process.poll() is None:
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
            process.communicate(timeout=10)
