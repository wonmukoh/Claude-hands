"""The Office adapters against stand-in object models.

What each adapter asks the application for, and what it does with the answer,
is checked here; the applications themselves are checked live
(examples/verify_live.py). These cover the faults a review found that a live
run with a small, tidy document never shows.
"""

from __future__ import annotations

import pytest

from claude_hands.office import core
from claude_hands.office.core import ExcelAdapter, OfficeUnavailableError, WordAdapter, _Budget
from claude_hands.office.hwp import HwpAdapter


def collect(adapter, document, max_nodes=800):
    emitted = []

    class Node:
        def __init__(self):
            self.truncated_children = 0

    def emit(element, *, depth, parent):
        emitted.append((element, depth))
        return Node()

    budget = _Budget(max_nodes)

    def counting_emit(element, *, depth, parent):
        budget.count += 1
        return emit(element, depth=depth, parent=parent)

    adapter.walk(document, counting_emit, budget)
    return emitted


# --------------------------------------------------------------------------
# Excel
# --------------------------------------------------------------------------


class Counted:
    def __init__(self, n):
        self.Count = n


class FakeRange:
    def __init__(self, sheet, values=None, row=1, col=1, rows=1, cols=1, address=""):
        self.sheet = sheet
        self._values = values
        self.Row, self.Column = row, col
        self.Rows, self.Columns = Counted(rows), Counted(cols)
        self.Count = rows * cols
        self.address = address
        self.Text = sheet.cells.get(address, "")

    @property
    def Value2(self):
        self.sheet.value_reads += 1
        return self._values

    @Value2.setter
    def Value2(self, value):
        self.sheet.cells[self.address] = value

    def __iter__(self):  # pragma: no cover - the old per-cell walk
        raise AssertionError("cells must not be walked one call at a time")


class FakeSheet:
    def __init__(self, name, values, row=1, col=1, rows=None):
        self.Name = name
        self.value_reads = 0
        self.cells = {}
        self._values = values
        self._row, self._col = row, col
        self._rows = rows if rows is not None else len(values)
        self.UsedRange = FakeRange(self, values, row, col, self._rows, len(values[0]))

    def Cells(self, r, c):
        return (r, c)

    def Range(self, first, last=None):
        if last is None:
            return FakeRange(self, address=first)
        rows = last[0] - first[0] + 1
        return FakeRange(self, self._values[:rows], first[0], first[1], rows, len(self._values[0]))


class FakeSheets:
    def __init__(self, sheets):
        self._sheets = sheets
        self.Count = len(sheets)

    def Item(self, i):
        return self._sheets[i - 1]


class FakeWorkbook:
    def __init__(self, *sheets):
        self.Worksheets = FakeSheets(list(sheets))


def test_excel_reads_a_sheet_in_one_call_and_names_cells_by_address():
    sheet = FakeSheet("시트1", ((None, "이름"), (3, None)), row=4, col=2)

    emitted = collect(ExcelAdapter, FakeWorkbook(sheet))

    assert [e.name for e, depth in emitted if depth == 2] == ["C4", "B5"]
    assert sheet.value_reads == 1


def test_excel_does_not_walk_a_sheet_formatted_to_the_last_row():
    # UsedRange of a sheet formatted down to row 1,048,576.
    values = tuple((i, None) for i in range(core.CELL_SCAN_LIMIT))
    sheet = FakeSheet("큰 시트", values, rows=1_048_576)

    emitted = collect(ExcelAdapter, FakeWorkbook(sheet), max_nodes=10)

    assert sheet.value_reads == 1
    assert len(emitted) <= 11


def test_excel_writes_go_to_the_cell_by_its_address():
    sheet = FakeSheet("시트1", (("가",),))
    emitted = collect(ExcelAdapter, FakeWorkbook(sheet))
    cell = [e for e, depth in emitted if depth == 2][0]

    cell._write("나")

    assert sheet.cells["A1"] == "나"


# --------------------------------------------------------------------------
# Word
# --------------------------------------------------------------------------


class WordRange:
    def __init__(self, paragraph):
        self.paragraph = paragraph
        self.end_moved = 0

    @property
    def Text(self):
        return self.paragraph.text

    @Text.setter
    def Text(self, value):
        self.paragraph.written = (value, self.end_moved)

    @property
    def Duplicate(self):
        return WordRange(self.paragraph)

    def MoveEnd(self, unit, count):
        self.end_moved += count


class Paragraph:
    def __init__(self, text):
        self.text = text
        self.written = None

    @property
    def Range(self):
        return WordRange(self)


class Paragraphs:
    def __init__(self, texts):
        self.items = [Paragraph(t) for t in texts]
        self.Count = len(self.items)

    def Item(self, i):
        return self.items[i - 1]


def test_writing_a_paragraph_keeps_its_paragraph_mark():
    # Replacing the whole range deleted the mark and joined the paragraph to
    # the next one.
    document = type("Doc", (), {"Paragraphs": Paragraphs(["첫째\r", "둘째\r"])})()
    emitted = collect(WordAdapter, document)

    emitted[0][0]._write("새 문장")

    assert document.Paragraphs.items[0].written == ("새 문장", -1)


def test_paragraph_refs_follow_the_text_not_the_position():
    document = type("Doc", (), {"Paragraphs": Paragraphs(["첫째\r", "둘째\r"])})()
    before = [e.automation_id for e, _ in collect(WordAdapter, document)]
    document.Paragraphs = Paragraphs(["새로 끼운 문단\r", "첫째\r", "둘째\r"])
    after = [e.automation_id for e, _ in collect(WordAdapter, document)]

    # 문단 2 is now the old 문단 1; it must not carry 문단 2's identity.
    assert after[1] == before[0] and after[1] != before[1]


# --------------------------------------------------------------------------
# 한글
# --------------------------------------------------------------------------


class HwpApp:
    def __init__(self, documents=1, windows=1, handle=500):
        self.documents, self.windows, self.handle = documents, windows, handle
        self.put = []

    def GetTextFile(self, kind, option):
        return "본문"

    def GetFieldList(self, a, b):
        return "제목"

    def GetFieldText(self, name):
        return ""

    def PutFieldText(self, name, value):
        self.put.append((name, value))

    @property
    def XHwpDocuments(self):
        return type("Docs", (), {"Count": self.documents})()

    @property
    def XHwpWindows(self):
        app = self

        class Windows:
            Count = app.windows

            def Item(self, i):
                return type("W", (), {"WindowHandle": app.handle})()

        return Windows()


@pytest.fixture
def root_is_self(monkeypatch):
    monkeypatch.setattr(core, "_root_hwnd", lambda hwnd: hwnd)


def field(app, hwnd=500):
    emitted = collect(HwpAdapter, {"app": app, "windows": app.windows, "hwnd": hwnd})
    return [e for e, _ in emitted if e.name == "제목"][0]


def test_hwp_writes_into_the_only_document(root_is_self):
    app = HwpApp()

    field(app)._write("가정통신문")

    assert app.put == [("제목", "가정통신문")]


def test_a_second_document_opened_as_a_tab_makes_fields_read_only(root_is_self):
    app = HwpApp(documents=2)

    assert field(app)._write is None or field(app)._read_only


def test_a_document_opened_after_the_snapshot_stops_the_write(root_is_self):
    app = HwpApp()
    writable = field(app)
    app.documents = 2  # the user opened another one since

    with pytest.raises(OfficeUnavailableError):
        writable._write("가정통신문")
    assert app.put == []


def test_the_control_inside_a_page_is_its_own_document():
    # K-에듀파인's embedded 한글 control has no attached window to check.
    app = HwpApp(documents=3)
    emitted = collect(HwpAdapter, {"app": app, "windows": 1})
    writable = [e for e, _ in emitted if e.name == "제목"][0]

    writable._write("기안 제목")

    assert app.put == [("제목", "기안 제목")]


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


def test_a_com_error_mid_walk_becomes_this_engine_cannot(monkeypatch, root_is_self):
    # Excel in cell-edit mode refuses calls mid-walk. The raw COM error reached
    # the caller untranslated and kept auto from falling back to the UI tree.
    class Refusing:
        label = "Excel"

        @staticmethod
        def acquire():
            return object()

        @staticmethod
        def documents(app):
            yield None, object(), 77

        @staticmethod
        def title(document):
            return "통합 문서"

        @staticmethod
        def saved(document):
            return True

        @staticmethod
        def walk(document, emit, budget):
            raise OSError(-2146827284, "cell edit mode")

    monkeypatch.setitem(core.ADAPTERS, "refusing.exe", Refusing)

    with pytest.raises(OfficeUnavailableError, match="거절"):
        core.build_office_tree(77, "refusing.exe")
