import argparse
import logging
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from dotenv import load_dotenv
import webview

from voice_super_agent.runtime import Runtime
from voice_super_agent.desktop.host import Desktop
from voice_super_agent.desktop.instance import SingleInstance


# 从开发工作副本的 src 布局定位前端产物和本地数据。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEV_URL = "http://127.0.0.1:5178"
logger = logging.getLogger(__name__)


def main() -> None:
    """桌面负责窗口生命周期，后台循环持续运行到用户明确退出。"""
    parser = argparse.ArgumentParser(description="启动小博桌面应用")
    parser.add_argument("--dev", action="store_true", help="加载本地 Vite 开发服务")
    parser.add_argument("--desktop-check", action="store_true", help="显示窗口与持续推送的开发联调入口")
    args = parser.parse_args()

    instance = SingleInstance()
    try:
        if not instance.primary:
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
            index_path = PROJECT_ROOT / "frontend" / "dist" / "index.html"
            if not index_path.is_file():
                parser.error("缺少前端构建产物，请先运行 npm --prefix frontend run build")
            page = str(index_path)

        load_dotenv(PROJECT_ROOT / ".env", encoding="utf-8-sig")
        local_dir = PROJECT_ROOT / ".local"
        for directory in ("data", "logs", "webview"):
            (local_dir / directory).mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler(local_dir / "logs" / "app.log", encoding="utf-8"),
            ],
        )

        runtime = Runtime(desktop_check=args.desktop_check)
        runtime.start()
        try:
            desktop = Desktop(runtime, page)
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
                desktop.stop()
        finally:
            runtime.stop()
        logger.info("应用已退出")
    finally:
        instance.close()
