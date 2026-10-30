"""The web engine against a stand-in DOM.

The page side (``collect.js``) runs only inside IE and is checked live with
``examples/web_target.py``. What is checked here is everything this side of
that: how its records become a tree, how an element is found again, and what
each action asks the page to do — in particular that nothing which could open
a modal dialog is called directly.
"""

from __future__ import annotations

import re
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
    def __init__(self, document=None):
        self.scheduled: list[str] = []
        self.document = document
        self.found = True  # what the page's own check would say

    def setTimeout(self, source, delay):
        self.scheduled.append(source)
        # The page answers a guarded action on its next turn.
        token = re.search(r"'(t\d+):ok'", source)
        if token and self.document is not None:
            self.document.claudeHandsAck = f"{token.group(1)}:{'ok' if self.found else 'miss'}"


class FakeDocument:
    def __init__(self, nodes, by_id=None):
        self.all = FakeAll(nodes)
        self.parentWindow = FakeWindow(self)
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

    [script] = doc.parentWindow.scheduled
    assert "d.all[30]" in script and "e.click();" in script


def test_a_checkbox_is_toggled_by_clicking_and_waits_for_the_change(page):
    box = FakeNode("INPUT", "attsave", checked=True)
    doc = page({11: box})

    def run_page_turn(source, delay):
        doc.parentWindow.scheduled.append(source)
        box.checked = not box.checked  # what the page does on its next turn

    doc.parentWindow.setTimeout = run_page_turn
    pattern = _TogglePattern(_Locator(1, (), 11, "INPUT", "attsave", ""), radio=False)

    pattern.Toggle()

    [script] = doc.parentWindow.scheduled
    assert "d.all[11]" in script and "e.click();" in script
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
    assert "d.all[23]" in doc.parentWindow.scheduled[0]


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


@pytest.mark.parametrize("which", ["collect.js", "page text"])
def test_the_page_scripts_stay_within_what_ie5_mode_understands(which):
    # An MFC host gets IE5 quirks mode unless the page asks otherwise, and
    # K-에듀파인's pages are of that age.
    if which == "collect.js":
        source = Path(core.__file__).with_name("collect.js").read_text(encoding="utf-8")
    else:
        source = core.PAGE_TEXT_JS
    for modern in ("JSON.", ".forEach(", ".trim(", "=>", "let ", "const ", "querySelector"):
        assert modern not in source, modern


def test_a_hidden_input_shown_by_its_label_is_still_listed():
    # K-에듀파인's 기안 side panel shrinks every radio and checkbox to nothing
    # and draws a <label for> instead; judged by the input's size alone, none
    # of them existed.
    source = Path(core.__file__).with_name("collect.js").read_text(encoding="utf-8")
    assert "standIn(el)" in source and "rectOf(box || el)" in source


def test_page_text_leaves_out_scripts_and_hidden_parts():
    for skipped in ("SCRIPT:1", "STYLE:1", "display === 'none'", "overflow === 'hidden'", "r.right + sx <= 0"):
        assert skipped in core.PAGE_TEXT_JS


def test_page_text_is_one_line_per_block_with_cells_kept_apart():
    raw = "\n  제목  \n\n\t구분\t 일반 \t\n\n본문   입니다\n"
    assert core._tidy_text(raw) == "제목\n구분\t일반\n본문 입니다"


def test_page_text_reads_each_frame_with_the_same_script(monkeypatch):
    class Frames:
        length = 1

        def item(self, index):
            return type("Frame", (), {"document": "inner"})()

    outer = type("Doc", (), {"frames": Frames()})()
    seen = []

    def fake_run(document, source):
        seen.append((document, source is core.PAGE_TEXT_JS))
        return "바깥" if document is outer else "안쪽"

    monkeypatch.setattr(core, "run_script", fake_run)

    assert core._page_text(lambda: outer)() == "바깥\n안쪽"
    assert seen == [(outer, True), ("inner", True)]


# --------------------------------------------------------------------------
# Calling a control's methods
# --------------------------------------------------------------------------


def control_page(page, answer=None):
    """A page whose next turn runs the scheduled call and leaves ``answer``."""

    import re

    control = FakeNode("OBJECT", "editor1")
    doc = page({40: control})

    def exec_script(source, language):
        match = re.match(r"document\.(\w+) = null;", source)
        setattr(doc, match.group(1), None)

    def run_page_turn(source, delay):
        doc.parentWindow.scheduled.append(source)
        if answer is not None:
            slot = re.search(r"document\.(claudeHandsCall\d+) = t", source).group(1)
            setattr(doc, slot, answer)

    doc.parentWindow.execScript = exec_script
    doc.parentWindow.setTimeout = run_page_turn
    return doc


def hwp_pattern():
    from claude_hands.mshtml.activex import HWP_CONTROL

    return core._ControlPattern(_Locator(1, (1,), 40, "OBJECT", "editor1", ""), HWP_CONTROL)


def test_a_control_call_is_run_by_the_page_and_its_answer_read_back(page, monkeypatch):
    from claude_hands.mshtml.activex import HWP_CONTROL

    doc = control_page(page, answer="string\x1f본문입니다")
    monkeypatch.setattr(_Locator, "document", lambda self: doc)

    value = hwp_pattern().Call("GetTextFile", HWP_CONTROL.methods["GetTextFile"], ["UNICODE", ""])

    assert value == "본문입니다"
    [script] = doc.parentWindow.scheduled
    assert "d.all[40]" in script
    assert 'c.GetTextFile("UNICODE", "")' in script


def test_a_control_that_does_not_answer_is_reported_not_waited_on_forever(page, monkeypatch):
    # A 한글 message box waits for a person. The caller hears so instead of
    # hanging with it — the lesson the modal 확인 link taught (lesson 28).
    from claude_hands.mshtml.activex import HWP_CONTROL

    doc = control_page(page, answer=None)
    monkeypatch.setattr(_Locator, "document", lambda self: doc)
    monkeypatch.setattr(core, "_wait_for", lambda check, timeout=2.0: check())

    with pytest.raises(ClaudeHandsError, match="대화상자"):
        hwp_pattern().Call("SaveAs", HWP_CONTROL.methods["SaveAs"], [r"C:\a.hwp", "HWP", ""], 1)


def test_the_control_refusing_a_call_is_an_error(page, monkeypatch):
    from claude_hands.mshtml.activex import HWP_CONTROL, ControlCallError

    doc = control_page(page, answer="error\x1f알 수 없는 형식")
    monkeypatch.setattr(_Locator, "document", lambda self: doc)

    with pytest.raises(ControlCallError, match="알 수 없는 형식"):
        hwp_pattern().Call("GetTextFile", HWP_CONTROL.methods["GetTextFile"], ["XYZ", ""])


def test_known_controls_get_a_call_pattern_and_unknown_ones_say_why_not(monkeypatch):
    raw = RS.join([
        US.join(["기안", "5", "1", "0"]),
        record(40, -1, "activex", "OBJECT", "", "attach", "", "attach",
               "clsid:205d1f4f-db85-4393-ac6d-d3ff2434e37e"),
        record(41, -1, "activex", "OBJECT", "", "other", "", "other",
               "clsid:11111111-2222-3333-4444-555555555555"),
    ])

    root = walk(monkeypatch, raw)

    attach, other = root.children
    assert "control" in attach.patterns and attach.is_actionable
    assert "call_control_method" in attach.help_text
    assert "control" not in other.patterns
    assert "허용 목록이 없어" in other.help_text


def test_text_says_whether_a_click_on_it_would_press_something(monkeypatch):
    raw = RS.join([
        US.join(["기안", "5", "1", "0"]),
        record(50, -1, "text", "SPAN", "", "", "", "결재올림", "", "press"),
        record(60, -1, "text", "TD", "", "", "", "결재", "", ""),
    ])
    elements = {}
    monkeypatch.setattr(core, "run_script", lambda document, source: raw)
    root = NodeInfo(role="document")
    core._walk_document(object(), server_hwnd=9, frames=(), origin=None, parent=root,
                        depth=0, element_index=elements, budget=[100])

    caption, cell = (elements[id(node)] for node in root.children)
    assert caption.inside_pressable is True
    assert cell.inside_pressable is False


class MinimisedHost:
    """user32 as WXSClient answers while minimised: a 1920x1040 restored frame
    whose IE control has been shrunk to 0x0."""

    def __init__(self, control_size=(0, 0)):
        self.control_size = control_size

    def IsIconic(self, hwnd):
        return 1

    def GetWindowPlacement(self, hwnd, placement):
        rect = placement._obj.rcNormalPosition
        rect.left, rect.top, rect.right, rect.bottom = 0, 0, 1920, 1040
        return 1

    def GetClientRect(self, hwnd, rect):
        rect._obj.right, rect._obj.bottom = self.control_size
        return 1


def test_a_minimised_host_that_shrank_its_page_is_measured_by_the_page(monkeypatch):
    monkeypatch.setattr(core, "_user32", lambda: MinimisedHost())
    monkeypatch.setattr(core, "ie_servers", lambda hwnd: [7])
    monkeypatch.setattr(core, "_page_area", lambda server: 1904 * 992)

    assert core.dominant_ie_server(1) == 7


def test_a_small_page_in_a_minimised_window_is_still_not_the_window(monkeypatch):
    # A help pane stays a help pane: auto must not switch engines for it.
    monkeypatch.setattr(core, "_user32", lambda: MinimisedHost())
    monkeypatch.setattr(core, "ie_servers", lambda hwnd: [7])
    monkeypatch.setattr(core, "_page_area", lambda server: 300 * 200)

    assert core.dominant_ie_server(1) is None


def test_the_page_is_only_asked_when_the_control_has_no_size(monkeypatch):
    monkeypatch.setattr(core, "_user32", lambda: MinimisedHost(control_size=(1904, 1001)))
    monkeypatch.setattr(core, "ie_servers", lambda hwnd: [7])
    monkeypatch.setattr(core, "_page_area", lambda server: pytest.fail("asked the page needlessly"))

    assert core.dominant_ie_server(1) == 7


# --------------------------------------------------------------------------
# Found in review: acting on the element the snapshot saw, or on nothing
# --------------------------------------------------------------------------


def test_an_element_the_page_no_longer_has_is_not_pressed(page):
    link = FakeNode("A", "ok")
    doc = page({30: link})
    doc.parentWindow.found = False  # the page's own check finds something else there

    with pytest.raises(PageChangedError):
        _InvokePattern(_Locator(1, (), 30, "A", "ok", "")).Invoke()


def test_the_page_checks_the_element_when_the_action_runs():
    # The click waits behind the page's own timers; a re-render in between
    # moves the index onto another element, so the check runs inside the page.
    script = _Locator(1, (), 30, "A", "", "", "확인").guarded("e.click();", "t9")
    assert script.isascii()
    assert "d.all[30]" in script and "e.click();" in script
    assert "'t9:miss'" in script
    assert "\\ud655\\uc778" in script  # the words it must still carry


def test_an_element_moved_by_a_re_render_is_acted_on_where_it_is_now():
    # Found again by id, it must not be pressed through the old index.
    script = _Locator(1, (), 30, "INPUT", "attsave", "").script_ref()
    assert "getElementById(ID)" in script


def test_an_onclick_div_without_a_name_is_still_itself(page):
    class Div:
        tagName = "DIV"
        id = "btnSave"

        def __getattr__(self, name):
            raise AttributeError(name)  # what pywin32 does for a missing property

    div = Div()
    page({50: div})

    _doc, found = _Locator(1, (), 50, "DIV", "btnSave", "").element()

    assert found is div


def test_a_frame_that_is_no_longer_a_frame_is_reported(page):
    page({7: FakeNode("DIV")})

    with pytest.raises(PageChangedError):
        _Locator(1, (7,), 11, "INPUT", "attsave", "").document()


def test_a_partial_choice_that_fits_several_options_is_refused(page):
    select = FakeNode("SELECT", "kind", selectedIndex=0,
                      options=FakeOptions([("결재대기", "a"), ("결재완료", "b")]))
    page({25: select})

    with pytest.raises(ClaudeHandsError, match="여럿"):
        _ValuePattern(_Locator(1, (), 25, "SELECT", "kind", ""), "select").SetValue("결재")
    assert select.selectedIndex == 0
