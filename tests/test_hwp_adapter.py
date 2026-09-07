"""한글, whose automation differs from Office in two ways that matter.

It does not answer to its own ProgID — `GetActiveObject("HWPFrame.HwpObject")`
fails against a running, visible 한글 — and its field API acts on whichever
document is active rather than on one you name. Both are measured, and both
shape what this adapter is allowed to do.
"""

import pytest

from claude_hands.office import hwp
from claude_hands.office.core import OfficeUnavailableError, _Budget
from claude_hands.elements import NodeInfo


class FakeHwp:
    """Enough of the 한글 object model to exercise the adapter."""

    def __init__(self, fields=None, text="본문 내용", windows=1, full_name=""):
        self._fields = dict(fields or {})
        self._text = text
        self._windows = windows
        self._full_name = full_name
        self.writes = []

    # -- what the adapter reads ------------------------------------------
    def GetFieldList(self, number, option):
        return hwp.FIELD_SEPARATOR.join(self._fields)

    def GetFieldText(self, field):
        return self._fields.get(field, "")

    def PutFieldText(self, field, text):
        self.writes.append((field, text))
        self._fields[field] = text

    def GetTextFile(self, fmt, option):
        return self._text

    @property
    def XHwpWindows(self):
        return FakeCollection([FakeWindow(1000 + i) for i in range(self._windows)])

    @property
    def XHwpDocuments(self):
        return FakeCollection([FakeDoc(self._full_name)])


class FakeCollection:
    def __init__(self, items):
        self._items = items

    @property
    def Count(self):
        return len(self._items)

    def Item(self, index):
        return self._items[index]


class FakeWindow:
    def __init__(self, handle):
        self.WindowHandle = handle


class FakeDoc:
    def __init__(self, full_name):
        self.FullName = full_name


def walk(app, windows=1, max_nodes=100):
    """Run the adapter's walk and collect what it emitted."""

    document = {"app": app, "windows": windows}
    emitted = []
    budget = _Budget(max_nodes)

    def emit(element, *, depth, parent):
        node = element.to_node_info(depth)
        node._element = element  # so tests can reach the patterns
        emitted.append(node)
        budget.count += 1  # the real emit charges the budget; so must this one
        return node

    hwp.HwpAdapter.walk(document, emit, budget)
    return emitted


# -- getting in -------------------------------------------------------------


def test_it_says_what_to_do_when_no_hangul_is_running(monkeypatch):
    class EmptyTable:
        def __iter__(self):
            return iter(())

    fake_pythoncom = type(
        "m",
        (),
        {
            "GetRunningObjectTable": staticmethod(lambda: EmptyTable()),
            "CreateBindCtx": staticmethod(lambda _n: None),
        },
    )
    monkeypatch.setitem(__import__("sys").modules, "pythoncom", fake_pythoncom)
    monkeypatch.setitem(__import__("sys").modules, "win32com.client", type("m", (), {}))

    with pytest.raises(OfficeUnavailableError) as caught:
        hwp._running_hwp()
    assert "한글" in str(caught.value)


# -- fields -----------------------------------------------------------------


def test_the_field_list_is_one_string_split_on_a_control_character():
    """Measured: GetFieldList answered '0\\x02성명\\x02학교'."""

    app = FakeHwp(fields={"0": "", "성명": "오원묵", "학교": "어란진초"})
    assert hwp._field_names(app) == ["0", "성명", "학교"]


def test_an_empty_field_list_is_no_fields_not_one_blank_one():
    assert hwp._field_names(FakeHwp(fields={})) == []


def test_each_field_becomes_a_writable_element():
    app = FakeHwp(fields={"성명": "오원묵"}, text="오원묵")
    nodes = walk(app)

    field = [n for n in nodes if n.name == "성명"][0]
    assert field.role == "edit"
    assert field.value == "오원묵"
    assert "value" in field.patterns

    field._element.pattern("value").SetValue("김철수")
    assert app.writes == [("성명", "김철수")]


def test_the_body_comes_through_as_read_only_text():
    """Replacing a whole 한글 document wholesale is not something this does."""

    nodes = walk(FakeHwp(text="가나다"))
    body = [n for n in nodes if n.name == "본문"][0]
    assert body.value == "가나다"
    assert body._element.pattern("value").CurrentIsReadOnly


def test_carriage_returns_do_not_survive_into_the_body():
    nodes = walk(FakeHwp(text="첫 줄\r\n둘째 줄"))
    assert [n for n in nodes if n.name == "본문"][0].value == "첫 줄\n둘째 줄"


# -- the reason writing is ever refused -------------------------------------


def test_fields_are_read_only_while_more_than_one_document_is_open():
    """한글's field API names no document; it writes into the active one.

    Writing under that rule is how you edit somebody else's open form, so with
    two windows this reads and refuses rather than writing somewhere plausible.
    """

    app = FakeHwp(fields={"성명": "오원묵"}, windows=2)
    field = [n for n in walk(app, windows=2) if n.name == "성명"][0]

    pattern = field._element.pattern("value")
    assert pattern.CurrentIsReadOnly
    assert pattern.CurrentValue == "오원묵"
    with pytest.raises(Exception):
        pattern.SetValue("김철수")
    assert app.writes == [], "거절했으면 아무것도 쓰지 않아야 합니다"
    assert "한글 창이 여럿" in field.help_text


def test_one_document_open_means_writing_is_allowed():
    app = FakeHwp(fields={"성명": "오원묵"}, windows=1)
    field = [n for n in walk(app, windows=1) if n.name == "성명"][0]
    field._element.pattern("value").SetValue("김철수")
    assert app.writes == [("성명", "김철수")]


# -- identity ---------------------------------------------------------------


def test_every_window_is_offered_with_its_handle():
    app = FakeHwp(windows=2)
    found = [(handle, doc["windows"]) for _w, doc, handle in hwp.HwpAdapter.documents(app)]
    assert found == [(1000, 2), (1001, 2)]


def test_an_unsaved_document_is_named_rather_than_left_blank():
    assert hwp.HwpAdapter.title({"app": FakeHwp(full_name=""), "windows": 1}) == "빈 문서"
    assert (
        hwp.HwpAdapter.title({"app": FakeHwp(full_name="공문.hwpx"), "windows": 1})
        == "공문.hwpx"
    )


def test_the_node_budget_stops_the_field_walk():
    app = FakeHwp(fields={f"칸{i}": "" for i in range(20)})
    assert len(walk(app, max_nodes=5)) <= 6  # 본문 + 예산만큼의 누름틀


def test_emitted_nodes_are_plain_node_info():
    nodes = walk(FakeHwp(fields={"성명": ""}))
    assert all(isinstance(n, NodeInfo) for n in nodes)
