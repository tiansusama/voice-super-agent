import argparse
import logging
import os
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from dotenv import load_dotenv
import webview

from voice_super_agent.runtime import Runtime
from voice_super_agent.desktop.host import Desktop
from voice_super_agent.desktop.instance import SingleInstance
from voice_super_agent.tasks import create_task_runtime
from voice_super_agent.tooling.apps import WindowsApps
from voice_super_agent.tooling.interface import BuiltinTools
from voice_super_agent.voice.wake import MODEL_NAME


# 从开发工作副本的 src 布局定位前端产物和本地数据。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEV_URL = "http://127.0.0.1:5178"
logger = logging.getLogger(__name__)


def main(*, app_root: Path | None = None, user_dir: Path | None = None) -> None:
    """桌面负责窗口生命周期，后台循环持续运行到用户明确退出。"""
    parser = argparse.ArgumentParser(description="启动小博桌面应用")
    parser.add_argument("--dev", action="store_true", help="加载本地 Vite 开发服务")
    parser.add_argument("--desktop-check", action="store_true", help="显示窗口与持续推送的开发联调入口")
    if user_dir is not None:
        parser.add_argument("--configure", action="store_true", help="重新配置本机服务密钥")
    args = parser.parse_args()
    if app_root is None:
        app_root = PROJECT_ROOT

    instance = SingleInstance()
    try:
        if not instance.primary:
            if user_dir is not None and args.configure:
                raise ValueError("请先从系统托盘退出小博，再打开“配置小博”。")
            instance.notify()
            print("应用已运行，正在显示已有主窗口。")
            return
        if args.dev:
            try:
                with urlopen(DEV_URL, timeout=2):
                    pass
            except (URLError, TimeoutError) as exc:
                parser.error(f"无法连接 {DEV_URL}，请先运行 npm --prefix frontend run dev：{exc}")
            page = DEV_URL
        else:
            index_path = app_root / "frontend" / "dist" / "index.html"
            if not index_path.is_file():
                parser.error("缺少前端构建产物，请先运行 npm --prefix frontend run build")
            page = str(index_path)

        local_dir = user_dir if user_dir is not None else app_root / ".local"
        env_path = (user_dir if user_dir is not None else app_root) / ".env"
        for directory in ("data", "logs", "webview"):
            (local_dir / directory).mkdir(parents=True, exist_ok=True)
        if user_dir is not None and (args.configure or not env_path.is_file()):
            from voice_super_agent.desktop.setup import configure_services

            configure_services(env_path)
        load_dotenv(env_path, encoding="utf-8-sig", override=user_dir is not None, interpolate=user_dir is None)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler(local_dir / "logs" / "app.log", encoding="utf-8"),
            ],
        )

        def task_factory(store, on_change):
            tools = BuiltinTools(WindowsApps(), set_window_visibility=desktop.set_main_visibility,
                                 get_window_visibility=desktop.get_main_visibility)
            return create_task_runtime(store, tools, api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
                                       model=os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
                                       base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
                                       on_change=on_change)

        runtime = Runtime(desktop_check=args.desktop_check, database_path=local_dir / "data" / "agent.sqlite3",
                          wake_model_dir=app_root / "resources" / "models" / MODEL_NAME,
                          task_factory=task_factory)
        desktop = Desktop(runtime, page)
        try:
            runtime.start()
            instance.watch(desktop.activate)
            try:
                logger.info("启动桌面，页面：%s", page)
                webview.start(
                    gui="edgechromium",
                    http_server=not args.dev,
                    storage_path=str(local_dir / "webview"),
                    debug=args.dev,
                )
            finally:
                instance.stop_watching()
        finally:
            try:
                desktop.stop()
            finally:
                runtime.stop()
        logger.info("应用已退出")
    finally:
        instance.close()
