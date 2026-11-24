import ctypes
import os
from pathlib import Path
import sys
import traceback
import winreg


def data_directory() -> Path:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\VoiceSuperAgent",
                            access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as settings:
            directory, _ = winreg.QueryValueEx(settings, "DataLocation")
    except FileNotFoundError:
        return Path(os.environ["LOCALAPPDATA"]) / "VoiceSuperAgent"
    return Path(directory)


def launch() -> None:
    log = None
    try:
        user_dir = data_directory()
        log = user_dir / "logs" / "desktop.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = log.open("a", encoding="utf-8", buffering=1)
        from voice_super_agent.app import main

        main(app_root=Path(__file__).resolve().parent, user_dir=user_dir)
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        traceback.print_exc()
        details = f"启动失败：{error}"
        if log is not None:
            details += f"\n\n日志：{log}"
        ctypes.windll.user32.MessageBoxW(None, details, "小博启动失败", 0x10)
        raise SystemExit(1) from error


if __name__ == "__main__":
    launch()
