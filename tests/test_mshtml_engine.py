"""The web engine against a stand-in DOM.

The page side (``collect.js``) runs only inside IE and is checked live with
``examples/web_target.py``. What is checked here is everything this side of
that: how its records become a tree, how an element is found again, and what
each action asks the page to do — in particular that nothing which could open
a modal dialog is called directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_hands.elements import NodeInfo
from claude_hands.mshtml import core
from claude_hands.mshtml.core import (
    RS,
    US,
    PageChangedError,
    _InvokePattern,
    _Locator,
    _TogglePattern,
    _ValuePattern,
)
from claude_hands.win32.defs import ClaudeHandsError


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeOptions:
    def __init__(self, entries):
        self._entries = entries
        self.length = len(entries)

    def item(self, index):
        text, value = self._entries[index]
        return type("Option", (), {"text": text, "value": value})()


class FakeNode:
    def __init__(self, tag, dom_id="", name="", **attrs):
        self.tagName = tag
        self.id = dom_id
        self.name = name
        self.__dict__.update(attrs)


class FakeAll:
    def __init__(self, nodes):
        self.nodes = nodes

    def item(self, index):
        return self.nodes.get(index)


class FakeWindow:
    def __init__(self):
        self.scheduled: list[str] = []

    def setTimeout(self, source, delay):
        self.scheduled.append(source)


class FakeDocument:
    def __init__(self, nodes, by_id=None):
        self.all = FakeAll(nodes)
        self.parentWindow = FakeWindow()
        self._by_id = by_id or {}

    def getElementById(self, dom_id):
        return self._by_id.get(dom_id)


@pytest.fixture
def page(monkeypatch):
    holder = {}

    def install(nodes, by_id=None):
        holder["doc"] = FakeDocument(nodes, by_id)
        monkeypatch.setattr(core, "document_of", lambda hwnd: holder["doc"])
        return holder["doc"]

    return install


# --------------------------------------------------------------------------
# Finding the element again
# --------------------------------------------------------------------------


def test_an_element_is_found_by_its_index_when_nothing_moved(page):
    box = FakeNode("INPUT", "attsave", checked=True)
    page({11: box})

    _doc, found = _Locator(1, (), 11, "INPUT", "attsave", "").element()

    assert found is box


def test_an_element_that_moved_is_found_by_id(page):
    box = FakeNode("INPUT", "attsave")
    page({11: FakeNode("DIV")}, by_id={"attsave": box})

    _doc, found = _Locator(1, (), 11, "INPUT", "attsave", "").element()

    assert found is box


def test_a_page_that_changed_is_reported_not_guessed(page):
    # A different element at the same index must not be acted on: that is how
    # a click meant for 확인 lands on 결재올림.
    page({11: FakeNode("INPUT", "sisave")})

    with pytest.raises(PageChangedError):
        _Locator(1, (), 11, "INPUT", "attsave", "").element()


# --------------------------------------------------------------------------
# Actions go through the page's own event loop
# --------------------------------------------------------------------------


def test_a_click_is_scheduled_inside_the_page_not_called(page):
    link = FakeNode("A", "ok")
    link.click = lambda: pytest.fail("click() over COM blocks while a modal dialog is open")
    doc = page({30: link})

    _InvokePattern(_Locator(1, (), 30, "A", "ok", "")).Invoke()

    assert doc.parentWindow.scheduled == ["document.all[30].click()"]


def test_a_checkbox_is_toggled_by_clicking_and_waits_for_the_change(page):
    box = FakeNode("INPUT", "attsave", checked=True)
    doc = page({11: box})

    def run_page_turn(source, delay):
        doc.parentWindow.scheduled.append(source)
        box.checked = not box.checked  # what the page does on its next turn

    doc.parentWindow.setTimeout = run_page_turn
    pattern = _TogglePattern(_Locator(1, (), 11, "INPUT", "attsave", ""), radio=False)

    pattern.Toggle()

    assert doc.parentWindow.scheduled == ["document.all[11].click()"]
    assert pattern.CurrentToggleState == 0


def test_a_checkbox_the_page_refuses_to_change_is_reported(page, monkeypatch):
    page({11: FakeNode("INPUT", "attsave", checked=True)})
    monkeypatch.setattr(core, "_wait_for", lambda check, timeout=2.0: check())

    with pytest.raises(ClaudeHandsError, match="바뀌지 않았습니다"):
        _TogglePattern(_Locator(1, (), 11, "INPUT", "attsave", ""), radio=False).Toggle()


def test_a_checked_radio_button_cannot_be_turned_off_by_clicking(page):
    doc = page({16: FakeNode("INPUT", "HWPDISTSAVE01", "HWPDISTSAVE", checked=True)})
    locator = _Locator(1, (), 16, "INPUT", "HWPDISTSAVE01", "HWPDISTSAVE")

    with pytest.raises(ClaudeHandsError, match="다른 항목"):
        _TogglePattern(locator, radio=True).Toggle()
    assert doc.parentWindow.scheduled == []


def test_typing_sets_the_value_and_fires_onchange_inside_the_page(page):
    box = FakeNode("INPUT", "memo", value="", type="text")
    doc = page({23: box})
    pattern = _ValuePattern(_Locator(1, (), 23, "INPUT", "memo", ""), "text")

    pattern.SetValue("가나다")

    assert box.value == "가나다"
    assert pattern.CurrentValue == "가나다"
    assert len(doc.parentWindow.scheduled) == 1
    assert "onchange" in doc.parentWindow.scheduled[0]
    assert "document.all[23]" in doc.parentWindow.scheduled[0]


def test_a_password_is_never_read_back(page):
    page({5: FakeNode("INPUT", "pw", value="secret", type="password")})

    assert _ValuePattern(_Locator(1, (), 5, "INPUT", "pw", ""), "text").CurrentValue is None


@pytest.mark.parametrize("wanted, index", [("비공개", 1), ("b", 1), ("공개", 1), ("일반", 0)])
def test_a_list_choice_matches_text_then_value_then_part(page, wanted, index):
    select = FakeNode("SELECT", "kind", selectedIndex=0,
                      options=FakeOptions([("일반", "a"), ("비공개", "b")]))
    page({25: select})

    _ValuePattern(_Locator(1, (), 25, "SELECT", "kind", ""), "select").SetValue(wanted)

    assert select.selectedIndex == index


def test_a_missing_choice_lists_what_is_there(page):
    page({25: FakeNode("SELECT", "kind", selectedIndex=0,
                       options=FakeOptions([("일반", "a"), ("비공개", "b")]))})

    with pytest.raises(ClaudeHandsError, match="일반, 비공개"):
        _ValuePattern(_Locator(1, (), 25, "SELECT", "kind", ""), "select").SetValue("대외비")


# --------------------------------------------------------------------------
# Records → tree
# --------------------------------------------------------------------------


def record(*fields):
    fields = list(fields) + [""] * (13 - len(fields))
    return US.join(str(f) for f in fields)


def walk(monkeypatch, raw, origin=(100, 200)):
    monkeypatch.setattr(core, "run_script", lambda document, source: raw)
    root = NodeInfo(role="document")
    core._walk_document(object(), server_hwnd=9, frames=(), origin=origin, parent=root,
                        depth=0, element_index={}, budget=[100])
    return root


def test_records_become_a_tree_in_document_order(monkeypatch):
    raw = RS.join([
        US.join(["파일저장", "5", "1", "0"]),
        # Out of order on purpose: the page lists controls before text.
        record(11, -1, "checkbox", "INPUT", "checkbox", "hwpsave", "", "본문", "", "on", "0", "0", "3,43,20,20"),
        record(2, -1, "text", "H3", "", "", "", "파일저장"),
        record(25, -1, "combobox", "SELECT", "select-one", "kind", "", "구분", "일반"),
        record(26, 25, "listitem", "OPTION", "", "", "", "일반", "a", "selected"),
        record(27, 25, "listitem", "OPTION", "", "", "", "비공개", "b", ""),
        record(23, -1, "edit", "INPUT", "password", "pw", "", "암호", "", "secret"),
        record(30, -1, "hyperlink", "A", "", "", "", "확인"),
    ])

    root = walk(monkeypatch, raw)

    assert [(n.role, n.name) for n in root.children] == [
        ("text", "파일저장"), ("checkbox", "본문"), ("edit", "암호"),
        ("combobox", "구분"), ("hyperlink", "확인"),
    ]
    box = root.children[1]
    assert box.toggle_state == "on" and "toggle" in box.patterns
    assert (box.rect.left, box.rect.top, box.rect.width) == (103, 243, 20)
    assert box.automation_id == "hwpsave"
    options = root.children[3].children
    assert [(o.name, o.selected) for o in options] == [("일반", True), ("비공개", False)]
    assert "selectionitem" in options[1].patterns
    assert "비밀번호" in root.children[2].help_text
    assert "invoke" in root.children[4].patterns


def test_a_minimised_window_gets_no_screen_rectangles(monkeypatch):
    # Its page has no place on the screen; a rectangle computed against the
    # parking spot would aim a coordinate click at some other program.
    raw = RS.join([
        US.join(["t", "5", "1", "0"]),
        record(11, -1, "checkbox", "INPUT", "checkbox", "a", "", "본문", "", "on", "0", "0", "3,43,20,20"),
    ])

    root = walk(monkeypatch, raw, origin=None)

    assert root.children[0].rect is None


def test_the_page_script_ships_with_the_package():
    assert Path(core.__file__).with_name("collect.js").exists()
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    assert "mshtml/*.js" in pyproject.read_text(encoding="utf-8")


def test_the_page_script_stays_within_what_ie5_mode_understands():
    # An MFC host gets IE5 quirks mode unless the page asks otherwise, and
    # K-에듀파인's pages are of that age.
    source = Path(core.__file__).with_name("collect.js").read_text(encoding="utf-8")
    for modern in ("JSON.", ".forEach(", ".trim(", "=>", "let ", "const ", "querySelector"):
        assert modern not in source, modern
