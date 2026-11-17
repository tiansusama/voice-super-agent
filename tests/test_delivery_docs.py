from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


def test_delivery_document_links_resolve():
    root = Path(__file__).resolve().parents[1]
    documents = (
        "README.md", "docs/development.md", "docs/code-sources.md", "resources/README.md",
        "docs/plan/V1.0.0-implementation-plan.md",
        "docs/plan/V1.0.0/16-dev-delivery.md",
        "docs/verification/stage-16.md",
    )
    missing = []
    for relative in documents:
        document = root / relative
        if not document.is_file():
            missing.append(relative)
            continue
        for link in re.findall(r"\]\(([^)]+)\)", document.read_text(encoding="utf-8")):
            target = urlsplit(link)
            if target.scheme or target.netloc or not target.path:
                continue
            if not (document.parent / unquote(target.path)).is_file():
                missing.append(f"{relative}: {link}")
    assert not missing, "交付文档或本地链接缺失：\n" + "\n".join(missing)
