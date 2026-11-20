from pathlib import Path

import pytest

from voice_super_agent.tooling.apps import Application, ApplicationTools


class DesktopApps:
    def __init__(self, catalog, existing=None):
        self.catalog = catalog
        self.existing = existing
        self.opened = []
        self.shown = []

    def installed(self):
        return self.catalog

    def find_window(self, application):
        return self.existing

    def show_window(self, window):
        self.shown.append(window)

    def launch(self, application):
        self.opened.append(application)


def test_application_resolution_reports_duplicate_names_before_actions():
    platform = DesktopApps([Application("工具", "apps", "Test.One"), Application("工具", "apps", "Test.Two")])
    tools = ApplicationTools({"工具"}, platform)
    with pytest.raises(ValueError, match=r"Test.One.*Test.Two"):
        tools.resolve("工具")
    assert platform.opened == platform.shown == []


def test_application_tool_shows_the_verified_existing_window():
    app = Application("工具", "apps", "Test.One")
    platform = DesktopApps([app], existing=42)
    tools = ApplicationTools({"工具"}, platform)
    result = tools.open(tools.resolve("工具"))
    assert result["outcome"] == "window_shown"
    assert platform.shown == [42]
    assert platform.opened == []


def test_absolute_application_path_is_allowed_only_as_explicit_target(tmp_path):
    executable = tmp_path / "测试程序.exe"
    executable.write_bytes(b"test")
    platform = DesktopApps([])
    tools = ApplicationTools({str(executable)}, platform)
    with pytest.raises(PermissionError, match="未授权"):
        tools.resolve(str(tmp_path / "其他程序.exe"))
    app = tools.resolve(str(executable))
    assert Path(app.target) == executable.resolve()
    assert tools.open(app)["outcome"] == "launch_requested"
    assert platform.opened == [app]
