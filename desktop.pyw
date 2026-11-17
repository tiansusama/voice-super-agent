"""双击入口使用本项目 uv 环境，窗口外的启动输出写入日志。"""

import ctypes
from pathlib import Path
import sys
import traceback


project_root = Path(__file__).resolve().parent
log_directory = project_root / ".local" / "logs"
try:
    log_directory.mkdir(parents=True, exist_ok=True)
    sys.stdout = sys.stderr = (log_directory / "desktop.log").open("a", encoding="utf-8", buffering=1)
    from voice_super_agent.app import main

    main()
except (ImportError, OSError, RuntimeError, ValueError) as error:
    ctypes.windll.user32.MessageBoxW(None, f"启动失败：{error}\n\n详细日志：{log_directory / 'desktop.log'}", "小博启动失败", 0x10)
    if sys.stderr is not None:
        traceback.print_exc()
    raise
except SystemExit as error:
    if error.code:
        traceback.print_exc()
        ctypes.windll.user32.MessageBoxW(None, f"启动未完成，请查看日志：\n{log_directory / 'desktop.log'}", "小博启动失败", 0x10)
    raise
