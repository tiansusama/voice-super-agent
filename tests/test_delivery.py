import ctypes
from ctypes import wintypes
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
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


def test_prepare_reinstalls_missing_transitive_frontend_dependency(delivery, tmp_path):
    root, environment = delivery
    frontend = root / "frontend"
    frontend.mkdir()
    packages = {
        "b": ({"name": "b", "version": "1.0.0"}, "module.exports = 'ready';\n"),
        "a": ({"name": "a", "version": "1.0.0",
               "dependencies": {"b": f"file:{(frontend / 'b.tgz').as_posix()}"}},
              "module.exports = require('b');\n"),
    }
    # 本地包提供真实的传递依赖，回归过程通过 npm 离线安装验证自动修复。
    for name, (manifest, source) in packages.items():
        with tarfile.open(frontend / f"{name}.tgz", "w:gz") as archive:
            for filename, content in (("package.json", json.dumps(manifest)), ("index.js", source)):
                data = content.encode("utf-8")
                entry = tarfile.TarInfo(f"package/{filename}")
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))
    (frontend / "package.json").write_text(json.dumps({
        "name": "delivery-fixture", "version": "1.0.0", "private": True,
        "dependencies": {"a": "file:./a.tgz"},
        "scripts": {"build": "node -e \"console.log(require('a'))\""},
    }), encoding="utf-8")
    npm = shutil.which("npm.cmd")
    locked = subprocess.run([npm, "install", "--package-lock-only", "--offline", "--ignore-scripts",
                             "--no-audit", "--no-fund"], cwd=frontend,
                            capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert locked.returncode == 0, locked.stderr
    direct = frontend / "node_modules/a"
    direct.mkdir(parents=True)
    (direct / "package.json").write_text(json.dumps(packages["a"][0]), encoding="utf-8")
    (direct / "index.js").write_text(packages["a"][1], encoding="utf-8")
    shallow = subprocess.run([npm, "ls", "--depth=0"], cwd=frontend,
                             capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert shallow.returncode == 0, shallow.stderr
    (tmp_path / "tools/npm.cmd").write_text(
        '@echo off\necho npm %*>>"%DELIVERY_CALLS%"\n'
        f'call "{npm}" %*\nexit /b %errorlevel%\n', encoding="utf-8")
    environment["npm_config_offline"] = "true"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                             str(root / "scripts/prepare-dev.ps1")], cwd=tmp_path, env=environment,
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, (result.stdout, result.stderr)
    calls = Path(environment["DELIVERY_CALLS"]).read_text(encoding="utf-8")
    assert " ci " in calls and "run build" in calls and "Model" in calls
    assert "ready" in result.stdout


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
        _, alive = psutil.wait_procs(children, timeout=5)
        assert not alive
        assert "npm" not in Path(environment["DELIVERY_CALLS"]).read_text(encoding="utf-8")
    finally:
        if process.poll() is None:
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
            process.communicate(timeout=10)


@pytest.mark.parametrize("failure", ["readonly_log", "directory_collision"])
def test_preparation_io_failure_shows_specific_error_and_exits(delivery, tmp_path, failure):
    root, environment = delivery
    log = root / ".local/logs/setup.log"
    if failure == "readonly_log":
        log.parent.mkdir(parents=True)
        log.write_text("existing log\n", encoding="utf-8")
        os.chmod(log, 0o444)
    else:
        (root / ".local").write_text("occupied path", encoding="utf-8")
    process = subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-STA", "-File",
                                str(root / "scripts/start.ps1")], cwd=tmp_path, env=environment,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    user32 = ctypes.windll.user32
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    dialogs = []
    texts = []
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and process.poll() is None:
            def inspect(handle, _):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
                text = ctypes.create_unicode_buffer(2048)
                user32.GetWindowTextW(handle, text, len(text))
                if pid.value == process.pid and text.value == "准备未完成":
                    dialogs.append(handle)
                return True
            user32.EnumWindows(callback_type(inspect), 0)
            if dialogs:
                break
            time.sleep(.05)
        assert dialogs, "准备错误没有进入应用的失败提示"

        def collect(handle, _):
            text = ctypes.create_unicode_buffer(2048)
            user32.GetWindowTextW(handle, text, len(text))
            texts.append(text.value)
            return True
        user32.EnumChildWindows.argtypes = [wintypes.HWND, callback_type, wintypes.LPARAM]
        user32.EnumChildWindows(dialogs[0], callback_type(collect), 0)
        content = "\n".join(texts)
        assert "启动准备失败" in content and "重新双击 start.cmd" in content
        assert ("setup.log" if failure == "readonly_log" else ".local") in content
        assert "Continue" not in content
        assert not Path(environment["DELIVERY_CALLS"]).exists()
        assert user32.PostMessageW(dialogs[0], 0x0010, 0, 0)
        output, error = process.communicate(timeout=10)
        assert process.returncode == 1, (output, error)
    finally:
        if process.poll() is None:
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
            process.communicate(timeout=10)
        if failure == "readonly_log":
            os.chmod(log, 0o666)
