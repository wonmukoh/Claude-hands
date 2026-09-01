"""The Office object model as a third engine.

UI Automation shows what an Office window *draws*. That is enough to read a
ribbon and press a button, and not enough to touch a document: PowerPoint
publishes its slide surface as ``custom "슬라이드 5"`` with no value pattern
and its text frames as ``image "TextBox 25"``, so there is no UIA path that
puts a character on a slide. Measured, not assumed — see CLAUDE.md lesson 10.

The applications do expose their documents, through the same automation
interface their macros use. Talking to that is strictly better here than
driving the UI:

* it never moves the cursor and never touches the foreground — measured over
  reads and writes with the window minimised, 0 violations;
* it works on a minimised or fully covered window, because it never involves
  the screen at all;
* it addresses content by name (``TextBox 4``) rather than by coordinate.

So this engine keeps the tool's three contracts by construction rather than by
care.

It plugs in behind the same protocol as the other two engines — an element
answering ``pattern`` / ``available_patterns`` / ``to_node_info``, and a
builder returning ``(root, element_index)`` — so ``actions.py`` drives it
unchanged and every result says ``office:value.SetValue``.

**Coordinates here are document coordinates, not screen coordinates.** A
shape's rectangle is its position on the slide or page in points. Nothing in
this engine clicks by coordinate, and a minimised window has no screen
position for its content anyway; presenting these as screen pixels would
repeat the DPI confusion of lesson 2 in a new place.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from ..elements import NodeInfo
from ..win32.defs import ClaudeHandsError, require_windows
from ..win32.windows import Rect


class OfficeUnavailableError(ClaudeHandsError):
    """Raised when the attached window is not an automatable Office document."""


# Process name -> the ProgID its running instance registers.
OFFICE_PROGIDS = {
    "powerpnt.exe": "PowerPoint.Application",
    "winword.exe": "Word.Application",
    "excel.exe": "Excel.Application",
}

GA_ROOT = 2


def _root_hwnd(hwnd: int) -> int:
    """The top-level frame a document window belongs to.

    Office reports a document window's own HWND, which is not the frame the
    caller attached to — PowerPoint gave 2953710 for a document whose frame was
    10622250. Comparing without walking up matches nothing.
    """

    from ..win32.defs import user32

    root = user32.GetAncestor(hwnd, GA_ROOT)
    return int(root) if root else int(hwnd)


# Office says "busy" rather than "no" while it is opening a document, painting,
# or sitting in a modal state. The call is not wrong, just early — measured:
# a freshly opened 37-slide deck rejected the first Shapes.Item call outright.
# Anything that touches the object model has to expect this and wait.
RPC_E_SERVERCALL_RETRYLATER = -2147417846
RPC_E_CALL_REJECTED = -2147418111
_BUSY_HRESULTS = {RPC_E_SERVERCALL_RETRYLATER, RPC_E_CALL_REJECTED}


def _hresult_of(exc: BaseException) -> Optional[int]:
    code = getattr(exc, "hresult", None)
    if isinstance(code, int):
        return code
    args = getattr(exc, "args", ())
    return args[0] if args and isinstance(args[0], int) else None


def com_retry(func: Callable[[], Any], *, attempts: int = 6, delay: float = 0.25) -> Any:
    """Run a COM call, waiting out "server busy" instead of failing on it.

    Only busy answers are retried. A genuine error — a shape that does not
    exist, a document that was closed — is raised immediately, because
    retrying it would just take six times as long to report the same thing.
    """

    last: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            return func()
        except Exception as exc:  # noqa: BLE001 - re-raised below unless busy
            if _hresult_of(exc) not in _BUSY_HRESULTS:
                raise
            last = exc
            time.sleep(delay * (attempt + 1))
    raise ClaudeHandsError(
        "Office 응용 프로그램이 계속 사용 중이라고 답합니다. "
        "대화상자가 열려 있거나 문서를 여는 중일 수 있습니다. "
        "그 상태를 정리한 뒤 다시 시도하세요."
    ) from last


def _window_hwnd(window) -> Optional[int]:
    """The window's handle, or None while it is not ready to say.

    Office spells this differently per application (``HWND`` in PowerPoint,
    ``Hwnd`` in Word and Excel), and a window that is still opening answers
    DISP_E_MEMBERNOTFOUND to all of them — measured while a second
    presentation was being loaded. A window that cannot identify itself is
    skipped rather than guessed at; the caller retries the whole lookup.
    """

    for attribute in ("HWND", "Hwnd", "hwnd"):
        try:
            value = com_retry(lambda attribute=attribute: getattr(window, attribute))
        except Exception:  # noqa: BLE001 - try the next spelling
            continue
        if value:
            return int(value)
    return None


def _text_of(getter: Callable[[], Any]) -> str:
    try:
        value = com_retry(getter)
    except ClaudeHandsError:
        raise
    except Exception:  # noqa: BLE001 - a shape that refuses its text is empty here
        return ""
    return "" if value is None else str(value)


# --------------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------------


class _OfficeValuePattern:
    """Reads and writes one piece of document text.

    Deliberately not implemented through ``Select`` + typing: selecting a shape
    activates its window, which would break the focus contract. Assigning the
    text range is a direct model write and leaves the window where it was.
    """

    def __init__(self, read: Callable[[], str], write: Callable[[str], None], read_only: bool):
        self._read = read
        self._write = write
        self._read_only = read_only

    @property
    def CurrentValue(self) -> str:
        return self._read()

    @property
    def CurrentIsReadOnly(self) -> bool:
        return self._read_only

    def SetValue(self, value: str) -> None:
        if self._read_only:
            raise ClaudeHandsError("이 요소는 읽기 전용이라 값을 바꿀 수 없습니다.")
        com_retry(lambda: self._write(value))


# --------------------------------------------------------------------------
# Element
# --------------------------------------------------------------------------


class OfficeElement:
    """One addressable piece of an open document."""

    engine = "office"

    def __init__(
        self,
        *,
        role: str,
        name: str,
        rect: Optional[Rect] = None,
        read: Optional[Callable[[], str]] = None,
        write: Optional[Callable[[str], None]] = None,
        read_only: bool = False,
        help_text: str = "",
        hwnd: int = 0,
    ) -> None:
        self.role = role
        self.name = name
        self._rect = rect
        self._read = read
        self._write = write
        self._read_only = read_only
        self.help_text = help_text
        self.hwnd = hwnd

    # -- pattern protocol -------------------------------------------------
    def pattern(self, key: str) -> Any:
        if key == "value" and self._read is not None:
            return _OfficeValuePattern(
                self._read,
                self._write or (lambda _v: None),
                self._read_only or self._write is None,
            )
        return None

    def available_patterns(self) -> tuple[str, ...]:
        return tuple(k for k in ("value",) if self.pattern(k) is not None)

    def value(self) -> str:
        return self._read() if self._read is not None else ""

    def to_node_info(self, depth: int = 0) -> NodeInfo:
        patterns = self.available_patterns()
        return NodeInfo(
            role=self.role,
            name=self.name,
            value=self.value() if patterns else "",
            class_name="office",
            rect=self._rect,
            enabled=True,
            offscreen=False,
            focusable=False,
            hwnd=self.hwnd,
            runtime_id=(),
            patterns=patterns,
            help_text=self.help_text,
            depth=depth,
        )


# --------------------------------------------------------------------------
# Application adapters
# --------------------------------------------------------------------------


def _points_rect(left, top, width, height) -> Optional[Rect]:
    try:
        left, top = int(left), int(top)
        return Rect(left, top, left + int(width), top + int(height))
    except Exception:  # noqa: BLE001 - a shape without geometry is still listed
        return None


class PowerPointAdapter:
    """Slides and the shapes on them."""

    progid = "PowerPoint.Application"
    label = "PowerPoint"

    @staticmethod
    def documents(app):
        count = com_retry(lambda: app.Windows.Count)
        for i in range(1, count + 1):
            window = com_retry(lambda i=i: app.Windows.Item(i))
            hwnd = _window_hwnd(window)
            if hwnd is None:
                continue
            yield window, com_retry(lambda: window.Presentation), hwnd

    @staticmethod
    def title(document) -> str:
        return str(document.Name)

    @staticmethod
    def walk(document, emit, budget):
        count = com_retry(lambda: document.Slides.Count)
        for index in range(1, count + 1):
            if budget.spent():
                return
            slide = com_retry(lambda index=index: document.Slides.Item(index))
            slide_node = emit(
                OfficeElement(role="group", name=f"슬라이드 {index}"),
                depth=1,
                parent=None,
            )
            shape_count = com_retry(lambda slide=slide: slide.Shapes.Count)
            for j in range(1, shape_count + 1):
                if budget.spent():
                    return
                shape = com_retry(lambda slide=slide, j=j: slide.Shapes.Item(j))
                emit(_shape_element(shape), depth=2, parent=slide_node)

    @staticmethod
    def saved(document) -> bool:
        return bool(document.Saved)


def _shape_element(shape) -> OfficeElement:
    name = _text_of(lambda: shape.Name)
    rect = com_retry(
        lambda: _points_rect(shape.Left, shape.Top, shape.Width, shape.Height)
    )

    has_text_frame = False
    try:
        has_text_frame = bool(com_retry(lambda: shape.HasTextFrame))
    except Exception:  # noqa: BLE001 - group shapes and media answer nothing
        has_text_frame = False

    if not has_text_frame:
        # A picture is still worth listing: it is what a person sees, and its
        # absence from the tree would read as "the slide is empty".
        return OfficeElement(role="image", name=name, rect=rect)

    def read() -> str:
        return _text_of(lambda: shape.TextFrame.TextRange.Text)

    def write(value: str) -> None:
        shape.TextFrame.TextRange.Text = value

    return OfficeElement(role="edit", name=name, rect=rect, read=read, write=write)


class WordAdapter:
    """The document body, one paragraph at a time."""

    progid = "Word.Application"
    label = "Word"

    @staticmethod
    def documents(app):
        count = com_retry(lambda: app.Documents.Count)
        for i in range(1, count + 1):
            document = com_retry(lambda i=i: app.Documents.Item(i))
            try:
                window = com_retry(lambda: document.ActiveWindow)
            except Exception:  # noqa: BLE001 - a document with no window yet
                continue
            hwnd = _window_hwnd(window)
            if hwnd is None:
                continue
            yield window, document, hwnd

    @staticmethod
    def title(document) -> str:
        return str(document.Name)

    @staticmethod
    def walk(document, emit, budget):
        paragraphs = document.Paragraphs
        for index in range(1, paragraphs.Count + 1):
            if budget.spent():
                return
            paragraph = paragraphs.Item(index)

            def read(paragraph=paragraph) -> str:
                # Word terminates every paragraph with \r; it is a delimiter,
                # not content, and returning it makes a read-back comparison
                # fail against the text that was written.
                return _text_of(lambda: paragraph.Range.Text).rstrip("\r\x07")

            def write(value: str, paragraph=paragraph) -> None:
                paragraph.Range.Text = value

            text = read()
            emit(
                OfficeElement(
                    role="edit",
                    name=f"문단 {index}",
                    read=read,
                    write=write,
                    help_text=text[:60],
                ),
                depth=1,
                parent=None,
            )

    @staticmethod
    def saved(document) -> bool:
        return bool(document.Saved)


class ExcelAdapter:
    """Sheets and the cells that actually hold something."""

    progid = "Excel.Application"
    label = "Excel"

    @staticmethod
    def documents(app):
        count = com_retry(lambda: app.Workbooks.Count)
        for i in range(1, count + 1):
            workbook = com_retry(lambda i=i: app.Workbooks.Item(i))
            try:
                window = com_retry(lambda: workbook.Windows.Item(1))
            except Exception:  # noqa: BLE001 - a workbook with no window yet
                continue
            hwnd = _window_hwnd(window)
            if hwnd is None:
                continue
            yield window, workbook, hwnd

    @staticmethod
    def title(document) -> str:
        return str(document.Name)

    @staticmethod
    def walk(document, emit, budget):
        for i in range(1, document.Worksheets.Count + 1):
            if budget.spent():
                return
            sheet = document.Worksheets.Item(i)
            sheet_node = emit(
                OfficeElement(role="group", name=str(sheet.Name)), depth=1, parent=None
            )
            used = sheet.UsedRange
            for cell in used:
                if budget.spent():
                    return
                if cell.Value2 is None:
                    continue

                def read(cell=cell) -> str:
                    return _text_of(lambda: cell.Text)

                def write(value: str, cell=cell) -> None:
                    cell.Value2 = value

                emit(
                    OfficeElement(
                        role="edit", name=str(cell.Address(False, False)), read=read, write=write
                    ),
                    depth=2,
                    parent=sheet_node,
                )

    @staticmethod
    def saved(document) -> bool:
        return bool(document.Saved)


ADAPTERS = {
    "powerpnt.exe": PowerPointAdapter,
    "winword.exe": WordAdapter,
    "excel.exe": ExcelAdapter,
}


class _Budget:
    def __init__(self, max_nodes: int) -> None:
        self.max_nodes = max_nodes
        self.count = 0

    def spent(self) -> bool:
        return self.count >= self.max_nodes


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _running_application(progid: str):
    import comtypes.client

    try:
        return comtypes.client.GetActiveObject(progid)
    except Exception as exc:  # noqa: BLE001 - not running, or no automation
        raise OfficeUnavailableError(
            f"{progid} 의 실행 중인 인스턴스를 찾지 못했습니다. "
            "프로그램이 떠 있는지 확인하고, 안 되면 --engine uia 로 UI 트리를 쓰세요."
        ) from exc


def build_office_tree(hwnd: int, process: str, *, max_nodes: int = 800):
    """Build a NodeInfo tree for the document shown in ``hwnd``.

    Mirrors :func:`claude_hands.win32.controls.build_win32_tree` so a session
    can use this engine wherever it uses the others.
    """

    require_windows()
    adapter = ADAPTERS.get((process or "").lower())
    if adapter is None:
        raise OfficeUnavailableError(
            f"{process!r} 는 Office 자동화 대상이 아닙니다. "
            f"지원: {', '.join(sorted(ADAPTERS))}"
        )

    app = _running_application(adapter.progid)

    # Match on the frame window, because that is what the caller attached to.
    # The search is retried as a whole: a window that is mid-open cannot answer
    # for itself and is skipped, so a single pass can miss a document that is
    # about to be there.
    wanted = _root_hwnd(hwnd)

    def locate():
        for _window, candidate, doc_hwnd in adapter.documents(app):
            if _root_hwnd(doc_hwnd) == wanted:
                return candidate
        return None

    document = None
    for attempt in range(4):
        document = locate()
        if document is not None:
            break
        time.sleep(0.3 * (attempt + 1))
    if document is None:
        raise OfficeUnavailableError(
            f"hwnd={hwnd} 에 해당하는 열린 문서를 찾지 못했습니다. "
            "다른 창을 골랐거나, 문서가 닫혔거나, 아직 여는 중일 수 있습니다."
        )

    element_index: dict[int, OfficeElement] = {}
    budget = _Budget(max_nodes)

    root_element = OfficeElement(
        role="document",
        name=adapter.title(document),
        hwnd=wanted,
        help_text=f"{adapter.label} · 저장됨={adapter.saved(document)}",
    )
    root = root_element.to_node_info(0)
    element_index[id(root)] = root_element

    def emit(element: OfficeElement, *, depth: int, parent: Optional[NodeInfo]) -> NodeInfo:
        node = element.to_node_info(depth)
        element_index[id(node)] = element
        (parent.children if parent is not None else root.children).append(node)
        budget.count += 1
        return node

    adapter.walk(document, emit, budget)
    if budget.spent():
        root.truncated_children += 1
    return root, element_index
