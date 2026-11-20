import os

import pytest

from voice_super_agent.tooling.files import FileGrant, FileTools


def test_lists_and_reads_text_from_explicit_paths(tmp_path):
    source = tmp_path / "清单.csv"
    source.write_text("名称,数量\n苹果,3\n", encoding="utf-8-sig", newline="")
    (tmp_path / "第二项.txt").write_text("另一个文件", encoding="utf-8")
    tools = FileTools([FileGrant("list", tmp_path), FileGrant("read", source)])

    page = tools.list_directory(str(tmp_path), limit=1)
    next_page = tools.list_directory(str(tmp_path), offset=page["next_offset"], limit=1)
    assert {page["entries"][0]["name"], next_page["entries"][0]["name"]} == {"清单.csv", "第二项.txt"}
    assert next_page["next_offset"] is None
    first = tools.read_text(str(source), limit=6)
    assert first["text"] == "名称,数量\n"
    assert tools.read_text(str(source), offset=first["next_offset"])["text"] == "苹果,3\n"


def test_directory_listing_grant_does_not_authorize_file_content(tmp_path):
    sibling = tmp_path / "未授权.txt"
    sibling.write_text("不在读取范围", encoding="utf-8")
    tools = FileTools([FileGrant("list", tmp_path)])

    with pytest.raises(PermissionError, match="未授权"):
        tools.read_text(str(sibling))


def test_generation_publishes_new_file_and_preserves_existing_content(tmp_path):
    target = tmp_path / "结果.json"
    tools = FileTools([FileGrant("create", target)])

    result = tools.create_text(str(target), '{"数量":3}\n')
    assert result["path"] == str(target.resolve())
    assert target.read_text(encoding="utf-8") == '{"数量":3}\n'
    with pytest.raises(FileExistsError, match="同名"):
        tools.create_text(str(target), '{"数量":4}')
    assert target.read_text(encoding="utf-8") == '{"数量":3}\n'
    assert list(tmp_path.iterdir()) == [target]


def test_invalid_json_stops_before_publishing_file(tmp_path):
    target = tmp_path / "结果.json"
    tools = FileTools([FileGrant("create", target)])

    with pytest.raises(ValueError, match="JSON"):
        tools.create_text(str(target), "{invalid")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows 的备用数据流语法")
def test_windows_alternate_stream_is_not_a_new_file_target(tmp_path):
    target = tmp_path / "已有.txt"
    target.write_text("保留", encoding="utf-8")

    with pytest.raises(ValueError, match="文件路径"):
        FileTools([FileGrant("create", f"{target}:附加流.txt")])
    assert target.read_text(encoding="utf-8") == "保留"


@pytest.mark.skipif(os.name != "nt", reason="Windows 的目录联接")
def test_directory_junction_cannot_redirect_a_confirmed_file_to_another_target(tmp_path):
    from _winapi import CreateJunction

    allowed = tmp_path / "allowed"
    other = tmp_path / "other"
    alias = tmp_path / "alias"
    for directory, text in ((allowed, "已确认"), (other, "超出范围")):
        directory.mkdir()
        (directory / "report.txt").write_text(text, encoding="utf-8")

    CreateJunction(str(allowed), str(alias))
    tools = FileTools([FileGrant("read", alias / "report.txt")])
    assert tools.read_text(str(alias / ".." / "ALIAS" / "REPORT.TXT"))["text"] == "已确认"

    alias.rmdir()
    CreateJunction(str(other), str(alias))
    with pytest.raises(PermissionError, match="未授权"):
        tools.read_text(str(alias / "report.txt"))
