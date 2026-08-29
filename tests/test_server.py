"""The MCP surface: every tool registers and carries a usable schema."""

import pytest

pytest.importorskip("mcp", reason="mcp SDK not installed")

import claude_hands.server as server  # noqa: E402

EXPECTED_TOOLS = {
    "list_windows",
    "attach_window",
    "detach_window",
    "snapshot",
    "find_elements",
    "read_text",
    "screenshot_window",
    "click_element",
    "type_text",
    "press_keys",
    "scroll",
    "set_element_state",
    "menu_select",
    "wait_for_element",
    "control_window",
}


def registered_tools():
    manager = getattr(server.mcp, "_tool_manager", None)
    if manager is None:  # pragma: no cover - SDK shape changed
        pytest.skip("installed mcp SDK exposes no tool manager")
    return {tool.name: tool for tool in manager.list_tools()}


def test_all_tools_are_registered():
    assert set(registered_tools()) == EXPECTED_TOOLS


def test_every_tool_has_a_description():
    for name, tool in registered_tools().items():
        assert tool.description and len(tool.description) > 20, name


def test_click_tool_schema_exposes_the_expected_arguments():
    tool = registered_tools()["click_element"]
    assert set(tool.parameters["properties"]) == {
        "ref", "button", "double", "modifiers", "hwnd",
    }
    assert tool.parameters.get("required") == ["ref"]


def test_read_only_mode_blocks_writes_but_not_reads(monkeypatch):
    from claude_hands.win32.defs import ClaudeHandsError

    monkeypatch.setattr(server, "READ_ONLY", True)
    with pytest.raises(ClaudeHandsError):
        server._guard_write()

    monkeypatch.setattr(server, "READ_ONLY", False)
    server._guard_write()  # does not raise


def test_main_exposes_the_read_only_flag():
    with pytest.raises(SystemExit):
        server.main(["--help"])
