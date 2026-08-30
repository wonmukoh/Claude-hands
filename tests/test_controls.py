"""The window-message backend: label handling and control classification."""

import pytest

from claude_hands.win32.controls import ACCELERATOR_ROLES, CLASS_ROLES, strip_accelerator


@pytest.mark.parametrize(
    "raw,shown",
    [
        ("&Save", "Save"),
        ("Add appli&cation...", "Add application..."),
        ("&Remove application", "Remove application"),
        ("Save && Exit", "Save & Exit"),
        ("No accelerator here", "No accelerator here"),
        ("", ""),
        ("&", ""),
        ("Trailing&", "Trailing"),
        ("&&", "&"),
        ("&&&File", "&File"),
        ("한글 &저장", "한글 저장"),
    ],
)
def test_strip_accelerator_matches_what_windows_draws(raw, shown):
    assert strip_accelerator(raw) == shown


def test_edit_content_is_never_treated_as_a_label():
    """An edit box's text is data — an ampersand in it is a real ampersand."""

    assert "edit" not in ACCELERATOR_ROLES
    assert "combobox" not in ACCELERATOR_ROLES


def test_buttons_and_labels_are_accelerator_bearing():
    for role in ("button", "checkbox", "radiobutton", "text", "menuitem", "tabitem"):
        assert role in ACCELERATOR_ROLES


def test_class_role_table_covers_the_common_controls():
    mapping = dict(CLASS_ROLES)
    assert mapping["button"] == "button"
    assert mapping["edit"] == "edit"
    assert mapping["static"] == "text"
    assert mapping["combobox"] == "combobox"
    assert mapping["systreeview32"] == "tree"
    assert mapping["syslistview32"] == "list"
