import ctypes
import os
from pathlib import Path
import sys
import traceback


def launch() -> None:
    user_dir = Path(os.environ["LOCALAPPDATA"]) / "VoiceSuperAgent"
    log = user_dir / "logs" / "desktop.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = log.open("a", encoding="utf-8", buffering=1)
        from voice_super_agent.app import main

        main(app_root=Path(__file__).resolve().parent, user_dir=user_dir)
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        traceback.print_exc()
        ctypes.windll.user32.MessageBoxW(None, f"启动失败：{error}\n\n日志：{log}", "小博启动失败", 0x10)
        raise SystemExit(1) from error


if __name__ == "__main__":
    launch()
