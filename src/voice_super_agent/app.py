import argparse
import logging
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from dotenv import load_dotenv
import webview

from voice_super_agent.runtime import Runtime


# 从开发工作副本的 src 布局定位前端产物和本地数据。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEV_URL = "http://127.0.0.1:5178"
logger = logging.getLogger(__name__)


class StartupApi:
    """仅向页面暴露当前阶段需要的启动状态查询。"""

    def __init__(self, runtime: Runtime):
        self._runtime = runtime

    def get_startup_status(self) -> dict[str, str]:
        return self._runtime.get_startup_status()


def main() -> None:
    """在主线程启动桌面，并在窗口结束后关闭后台循环。"""
    parser = argparse.ArgumentParser(description="启动小博桌面应用")
    parser.add_argument("--dev", action="store_true", help="加载本地 Vite 开发服务")
    args = parser.parse_args()

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

    runtime = Runtime()
    runtime.start()
    try:
        webview.create_window(
            "小博 · Voice Super Agent",
            url=page,
            js_api=StartupApi(runtime),
            width=960,
            height=680,
            min_size=(640, 480),
            background_color="#FBFAF7",
        )
        logger.info("启动桌面，页面：%s", page)
        # GUI 留在主线程；本地构建页面通过宿主静态服务加载。
        webview.start(
            gui="edgechromium",
            http_server=not args.dev,
            storage_path=str(local_dir / "webview"),
            debug=args.dev,
        )
    finally:
        runtime.stop()
    logger.info("应用已退出")
