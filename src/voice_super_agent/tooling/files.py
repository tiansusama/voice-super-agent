import json
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterable, Literal


FileOperation = Literal["list", "read", "create"]
MAX_TEXT_BYTES = 1_048_576
TEXT_SUFFIXES = frozenset({
    "", ".txt", ".md", ".markdown", ".json", ".csv", ".log", ".rst",
    ".py", ".pyw", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".css",
    ".scss", ".html", ".xml", ".svg", ".sql", ".ps1", ".psm1", ".psd1", ".cmd",
    ".bat", ".sh", ".java", ".kt", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs",
    ".go", ".rs", ".rb", ".php", ".vue", ".svelte", ".toml", ".yaml", ".yml",
    ".ini", ".cfg", ".conf", ".env",
})


@dataclass(frozen=True, slots=True)
class FileGrant:
    operation: FileOperation
    path: str | Path


def _resolve(path: str | Path, operation: FileOperation) -> Path:
    target = Path(path)
    if not target.is_absolute() or target.drive.startswith("\\\\") or any(
        ":" in part or part.endswith((" ", ".")) or Path(part).is_reserved()
        for part in target.parts[1:]
        if part not in {".", ".."}
    ):
        raise ValueError("请输入有效的本机绝对文件路径。")
    if operation == "create":
        if not target.name:
            raise ValueError("请输入需要生成的文件路径。")
        # 新建目标保留最后一段，已有文件或链接都不能被替换。
        return target.parent.resolve() / target.name
    return target.resolve()


def _check_page(offset: int, limit: int, maximum: int) -> None:
    if type(offset) is not int or offset < 0:
        raise ValueError("偏移需要是非负整数。")
    if type(limit) is not int or not 1 <= limit <= maximum:
        raise ValueError(f"每页条数或字符数需要是 1～{maximum} 的整数。")


class FileTools:
    """文件工具只使用已确认的操作类型和路径。"""

    def __init__(self, grants: Iterable[FileGrant], *, canonical: bool = False):
        self._grants = frozenset(
            (grant.operation, Path(grant.path) if canonical else _resolve(grant.path, grant.operation)) for grant in grants
        )

    def approved_path(self, operation: FileOperation, path: str) -> Path:
        target = _resolve(path, operation)
        if (operation, target) not in self._grants:
            raise PermissionError("该文件或目录未授权此操作。")
        if operation in {"read", "create"} and target.suffix.lower() not in TEXT_SUFFIXES:
            raise ValueError("当前工具只支持纯文本、代码、Markdown、JSON 和 CSV 文件。")
        return target

    def list_directory(self, path: str, *, offset: int = 0, limit: int = 50) -> dict:
        target = self.approved_path("list", path)
        _check_page(offset, limit, 200)
        entries = sorted(target.iterdir(), key=lambda entry: (entry.name.casefold(), entry.name))
        page = entries[offset:offset + limit]
        return {
            "path": str(target),
            "entries": [{"name": entry.name, "is_directory": entry.is_dir()} for entry in page],
            "next_offset": offset + limit if offset + limit < len(entries) else None,
        }

    def read_text(self, path: str, *, offset: int = 0, limit: int = 2000) -> dict:
        target = self.approved_path("read", path)
        _check_page(offset, limit, 8000)
        with target.open("rb") as source:
            content = source.read(MAX_TEXT_BYTES + 1)
        if len(content) > MAX_TEXT_BYTES:
            raise ValueError("文件超过当前 1 MiB 的读取上限。")
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("文件内容需要使用 UTF-8 编码。") from exc
        if "\x00" in text:
            raise ValueError("该文件包含二进制内容。")
        return {
            "path": str(target),
            "text": text[offset:offset + limit],
            "total_characters": len(text),
            "next_offset": offset + limit if offset + limit < len(text) else None,
        }

    def create_text(self, path: str, content: str) -> dict:
        target = self.approved_path("create", path)
        if target.suffix.lower() == ".json":
            try:
                json.loads(content)
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSON 格式无效：{exc.msg}（第 {exc.lineno} 行）") from exc
        data = content.encode("utf-8")
        if "\x00" in content or len(data) > MAX_TEXT_BYTES:
            raise ValueError("生成内容需要是 1 MiB 以内的文本。")
        output = NamedTemporaryFile(dir=target.parent, prefix=".xiaobo-", suffix=".tmp", delete=False)
        temporary = Path(output.name)
        try:
            with output:
                output.write(data)
            # Windows rename 保证目标存在时失败，竞争中的同名文件也保持原样。
            temporary.rename(target)
        except FileExistsError as exc:
            raise FileExistsError(f"目标已存在同名文件，请指定新文件名：{target}") from exc
        finally:
            temporary.unlink(missing_ok=True)
        return {"path": str(target), "bytes_written": len(data)}
