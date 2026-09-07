"""The Office engine: matching the right document, and surviving a busy app.

These run everywhere — the COM boundary is the only thing replaced, and the
logic above it (window matching, retry policy, tree shape, the value pattern)
runs for real. What they cannot prove is that Office answers the way the fakes
do; that is what the live verification in CLAUDE.md is for.
"""

import pytest

from claude_hands.office import core
from claude_hands.win32.defs import ClaudeHandsError


class ComError(Exception):
    """Stands in for _ctypes.COMError, which carries its HRESULT in args[0]."""

    def __init__(self, hresult, message="COM"):
        super().__init__(hresult, message)


BUSY = core.RPC_E_SERVERCALL_RETRYLATER
REJECTED = core.RPC_E_CALL_REJECTED
MEMBER_NOT_FOUND = -2147352573


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    monkeypatch.setattr(core.time, "sleep", lambda _s: None)


# -- com_retry --------------------------------------------------------------


def test_a_busy_answer_is_waited_out():
    answers = [ComError(BUSY), ComError(REJECTED), "결과"]

    def call():
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    assert core.com_retry(call) == "결과"
    assert answers == []


def test_a_real_error_is_raised_at_once():
    calls = []

    def call():
        calls.append(1)
        raise ComError(MEMBER_NOT_FOUND)

    with pytest.raises(ComError):
        core.com_retry(call)
    assert calls == [1], "재시도하면 같은 오류를 여섯 배 느리게 보고할 뿐입니다"


def test_an_app_that_stays_busy_gives_up_with_an_actionable_message():
    with pytest.raises(ClaudeHandsError) as caught:
        core.com_retry(lambda: (_ for _ in ()).throw(ComError(BUSY)), attempts=3)
    assert "사용 중" in str(caught.value)


# -- window handles ---------------------------------------------------------


class Window:
    def __init__(self, **attrs):
        self._attrs = attrs

    def __getattr__(self, name):
        if name in self._attrs:
            return self._attrs[name]
        raise ComError(MEMBER_NOT_FOUND, name)


def test_each_application_spells_the_handle_its_own_way():
    assert core._window_hwnd(Window(HWND=11)) == 11  # PowerPoint
    assert core._window_hwnd(Window(Hwnd=22)) == 22  # Word, Excel
    assert core._window_hwnd(Window(hwnd=33)) == 33


def test_a_window_that_cannot_identify_itself_is_skipped_not_guessed():
    assert core._window_hwnd(Window()) is None


# -- the value pattern ------------------------------------------------------


def test_writing_goes_through_the_model_and_reads_back():
    box = {"text": "처음"}
    element = core.OfficeElement(
        role="edit",
        name="TextBox 4",
        read=lambda: box["text"],
        write=lambda v: box.__setitem__("text", v),
    )

    assert element.available_patterns() == ("value",)
    pattern = element.pattern("value")
    pattern.SetValue("바꾼 값")
    assert box["text"] == "바꾼 값"
    assert pattern.CurrentValue == "바꾼 값"


def test_an_element_with_no_writer_is_read_only():
    element = core.OfficeElement(role="image", name="Picture 1", read=lambda: "")
    pattern = element.pattern("value")
    assert pattern.CurrentIsReadOnly
    with pytest.raises(ClaudeHandsError):
        pattern.SetValue("아무거나")


def test_a_picture_offers_no_patterns_at_all():
    element = core.OfficeElement(role="image", name="Picture 1")
    assert element.available_patterns() == ()
    assert element.to_node_info().patterns == ()


def test_an_element_answers_the_liveness_questions_the_session_asks():
    """Without these the session thinks every ref is dead and re-walks the document.

    Measured before the properties existed: reading one slide of a 37-slide
    deck already held in the tree took twelve seconds, because resolving the
    ref rebuilt the whole thing.
    """

    from claude_hands.session import WindowSession
    from claude_hands.win32.windows import Rect, WindowInfo

    element = core.OfficeElement(
        role="edit", name="TextBox 4", rect=Rect(0, 0, 100, 40), read=lambda: "글"
    )
    assert element.rect == Rect(0, 0, 100, 40)
    assert element.enabled is True

    session = WindowSession(
        hwnd=1,
        info=WindowInfo(
            hwnd=1,
            title="발표.pptx",
            class_name="",
            pid=1,
            process="powerpnt.exe",
            rect=Rect(0, 0, 1, 1),
            minimized=False,
            maximized=False,
            visible=True,
        ),
    )
    assert session._element_alive(element) is True


# -- building the tree ------------------------------------------------------


class FakeAdapter:
    label = "가짜"
    progid = "Fake.Application"

    def __init__(self, documents):
        self._documents = documents

    def documents(self, app):
        for hwnd, document in self._documents:
            yield object(), document, hwnd

    @staticmethod
    def title(document):
        return document["name"]

    @staticmethod
    def saved(document):
        return True

    @staticmethod
    def walk(document, emit, budget):
        for slide_name, shapes in document["slides"]:
            if budget.spent():
                return
            slide = emit(core.OfficeElement(role="group", name=slide_name), depth=1, parent=None)
            if not budget.allows(2, slide_name):
                slide.truncated_children = len(shapes)
                continue
            for shape_name in shapes:
                if budget.spent():
                    return
                emit(
                    core.OfficeElement(role="edit", name=shape_name, read=lambda: "글"),
                    depth=2,
                    parent=slide,
                )


DECK = {
    "name": "발표.pptx",
    "slides": [
        ("슬라이드 1", ["TextBox 1", "TextBox 2"]),
        ("슬라이드 5", ["TextBox 3", "Picture 4", "TextBox 5"]),
        ("슬라이드 9", ["TextBox 6"]),
    ],
}

MINE = {"name": "내문서.pptx", "slides": [("슬라이드 1", ["TextBox 4", "TextBox 5"])]}
THEIRS = {"name": "남의문서.pptx", "slides": [("슬라이드 1", ["건드리면 안 되는 것"])]}


@pytest.fixture
def office(monkeypatch):
    monkeypatch.setattr(core, "require_windows", lambda: None)
    # The frame handle is what the caller attached to; document windows report
    # their own. 900 is the frame both 901 and 902 live under.
    roots = {901: 900, 902: 900, 911: 910, 900: 900, 910: 910}
    monkeypatch.setattr(core, "_root_hwnd", lambda h: roots.get(h, h))
    monkeypatch.setattr(core, "_running_application", lambda progid: object())
    return monkeypatch


def install(monkeypatch, documents):
    monkeypatch.setitem(core.ADAPTERS, "fake.exe", FakeAdapter(documents))


def test_the_document_is_matched_through_the_frame_window(office):
    install(office, [(911, THEIRS), (901, MINE)])
    root, index = core.build_office_tree(900, "fake.exe")

    assert root.name == "내문서.pptx"
    names = [child.name for child in root.children]
    assert names == ["슬라이드 1"]
    assert [n.name for n in root.children[0].children] == ["TextBox 4", "TextBox 5"]
    assert all(id(node) in index for node in root.children)


def test_another_persons_open_document_is_not_reachable_by_accident(office):
    install(office, [(911, THEIRS), (901, MINE)])
    root, _ = core.build_office_tree(910, "fake.exe")
    assert root.name == "남의문서.pptx", "hwnd 로만 고르므로 창을 잘못 고르면 그 문서가 나와야 합니다"


def test_a_window_with_no_matching_document_is_an_error_not_a_wrong_document(office):
    install(office, [(901, MINE)])
    with pytest.raises(core.OfficeUnavailableError):
        core.build_office_tree(999, "fake.exe")


def test_a_non_office_process_says_what_is_supported(office):
    with pytest.raises(core.OfficeUnavailableError) as caught:
        core.build_office_tree(900, "notepad.exe")
    assert "powerpnt.exe" in str(caught.value)


def test_the_node_budget_is_respected_and_marked(office):
    install(office, [(901, MINE)])
    root, _ = core.build_office_tree(900, "fake.exe", max_nodes=2)
    assert root.truncated_children == 1, "잘렸으면 잘렸다고 말해야 합니다"


def test_text_shapes_carry_the_value_pattern_so_actions_can_type(office):
    install(office, [(901, MINE)])
    root, _ = core.build_office_tree(900, "fake.exe")
    shape = root.children[0].children[0]
    assert "value" in shape.patterns
    assert shape.is_actionable


# -- how much of the document to walk ---------------------------------------
#
# Depth is not cosmetic here. Every node is a cross-process call, and a real
# 37-slide deck measured 859 nodes and fifteen seconds walked in full against
# 38 nodes and half a second for the outline. `max_depth` was reaching this
# engine as nothing at all, so asking for an outline bought the whole deck.


def test_an_outline_lists_the_slides_without_opening_them(office):
    install(office, [(901, DECK)])
    root, _ = core.build_office_tree(900, "fake.exe", max_depth=1)

    assert [s.name for s in root.children] == ["슬라이드 1", "슬라이드 5", "슬라이드 9"]
    assert all(s.children == [] for s in root.children)
    assert [s.truncated_children for s in root.children] == [2, 3, 1]


def test_a_closed_slide_survives_pruning_so_it_can_be_opened(office):
    """It holds nothing, and it is exactly the node the caller needs."""

    from claude_hands.elements import prune

    install(office, [(901, DECK)])
    root, _ = core.build_office_tree(900, "fake.exe", max_depth=1)
    kept = prune(root, keep_offscreen=False) or root

    assert [s.name for s in kept.children] == ["슬라이드 1", "슬라이드 5", "슬라이드 9"]


def test_expand_opens_one_slide_and_leaves_the_rest_closed(office):
    install(office, [(901, DECK)])
    root, _ = core.build_office_tree(900, "fake.exe", max_depth=1, expand="슬라이드 5")

    by_name = {s.name: s for s in root.children}
    assert [c.name for c in by_name["슬라이드 5"].children] == [
        "TextBox 3",
        "Picture 4",
        "TextBox 5",
    ]
    assert by_name["슬라이드 5"].truncated_children == 0
    assert by_name["슬라이드 1"].children == []
    assert by_name["슬라이드 9"].children == []


def test_expand_matches_case_insensitively(office):
    install(office, [(901, DECK)])
    root, _ = core.build_office_tree(900, "fake.exe", max_depth=1, expand="SHEET")
    assert all(s.children == [] for s in root.children), "일치하는 게 없으면 아무것도 안 열립니다"

    deck = {"name": "x.xlsx", "slides": [("Sheet1", ["A1"])]}
    install(office, [(901, deck)])
    root, _ = core.build_office_tree(900, "fake.exe", max_depth=1, expand="sheet1")
    assert [c.name for c in root.children[0].children] == ["A1"]


def test_the_default_depth_still_walks_the_whole_document(office):
    install(office, [(901, DECK)])
    root, _ = core.build_office_tree(900, "fake.exe")
    assert [len(s.children) for s in root.children] == [2, 3, 1]
    assert all(s.truncated_children == 0 for s in root.children)


def test_an_opened_slide_keeps_the_refs_the_full_walk_gave_it(office):
    """Reading the outline then opening one slide must not renumber anything."""

    from claude_hands.elements import assign_refs

    install(office, [(901, DECK)])
    known = {}

    full, _ = core.build_office_tree(900, "fake.exe")
    assign_refs(full, known=known)
    wanted = {
        (s.name, c.name): c.ref for s in full.children for c in s.children
    }

    partial, _ = core.build_office_tree(900, "fake.exe", max_depth=1, expand="슬라이드 5")
    assign_refs(partial, known=known)
    got = {(s.name, c.name): c.ref for s in partial.children for c in s.children}

    assert got, "슬라이드 5 는 열려 있어야 합니다"
    assert all(wanted[key] == ref for key, ref in got.items())
