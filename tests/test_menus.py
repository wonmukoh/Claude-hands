"""Menu label parsing and path resolution."""

import pytest

from claude_hands.win32.menus import (
    MenuItem,
    MenuNotFoundError,
    _menu_label,
    find_menu_item,
    render_menu,
)


@pytest.mark.parametrize(
    "raw,shown",
    [
        ("&Save", "Save"),
        ("&Save\tCtrl+S", "Save"),
        ("Save &As...\tCtrl+Shift+S", "Save As..."),
        ("E&xit", "Exit"),
        ("&Find...\tCtrl+F", "Find..."),
        ("파일 저장(&S)\tCtrl+S", "파일 저장(S)"),
        ("", ""),
    ],
)
def test_menu_label_strips_accelerators_and_shortcut_columns(raw, shown):
    assert _menu_label(raw) == shown


def sample_menu():
    return [
        MenuItem("File", None, 0, children=[
            MenuItem("New", 101, 0),
            MenuItem("Open...", 102, 1),
            MenuItem("", None, 2, separator=True),
            MenuItem("Exit", 103, 3),
        ]),
        MenuItem("Search", None, 1, children=[
            MenuItem("Find...", 201, 0),
            MenuItem("Find Next", 202, 1, enabled=False),
        ]),
        MenuItem("View", None, 2, children=[
            MenuItem("Status Bar", 301, 0, checked=True),
        ]),
    ]


def test_find_walks_a_named_path():
    item = find_menu_item(sample_menu(), ["Search", "Find..."])
    assert item.command_id == 201


def test_find_tolerates_missing_trailing_dots():
    assert find_menu_item(sample_menu(), ["Search", "Find"]).command_id == 201


def test_find_falls_back_to_substring():
    assert find_menu_item(sample_menu(), ["File", "Open"]).command_id == 102


def test_find_is_case_insensitive():
    assert find_menu_item(sample_menu(), ["file", "EXIT"]).command_id == 103


def test_find_skips_separators():
    item = find_menu_item(sample_menu(), ["File"])
    assert [c.label for c in item.children if not c.separator] == ["New", "Open...", "Exit"]


def test_missing_step_lists_what_was_available():
    with pytest.raises(MenuNotFoundError) as excinfo:
        find_menu_item(sample_menu(), ["Search", "Replace..."])
    message = str(excinfo.value)
    assert "Replace..." in message
    assert "Find..." in message  # tells the caller what it could have picked
    assert "Search" in message   # and how far it got


def test_disabled_and_checked_state_is_reported():
    rendered = render_menu(sample_menu())
    assert "Find Next (disabled)" in rendered
    assert "Status Bar (checked)" in rendered


def test_submenu_has_no_command_of_its_own():
    assert find_menu_item(sample_menu(), ["File"]).is_submenu
