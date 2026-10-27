"""Web pages inside an IE WebBrowser control, read through their own DOM.

Some business applications are a native frame around an IE WebBrowser control
(window class ``Internet Explorer_Server``). K-에듀파인's WXSClient is one: an
MFC dialog whose every form is a web page. UI Automation sees that page as IE's
accessibility layer draws it, and for old-style pages that is not enough —
measured on its 파일저장 window, the checkboxes and radio buttons came through as
plain text with no role and no checked state, so nothing could tell which were
ticked, let alone change them.

The page's own document object answers both questions. The control hands it
out to any process that asks with ``WM_HTML_GETOBJECT``, the same way screen
readers get it, and from there everything is a property read or write. Nothing
here moves the cursor, takes focus or touches the window, so the three
contracts hold by construction, as they do for the Office engine.

Three things learned the hard way, by the session that first drove WXSClient:

* **A click that opens a modal dialog blocks the caller.** ``element.click()``
  over COM does not return until the handler does, and a handler that calls
  ``showModalDialog`` or opens the folder picker does not return until a person
  closes it. So clicks are scheduled inside the page with ``setTimeout`` and
  the call returns at once — the same thing a real click does.
* **Event handlers are what make a form work.** Setting ``checked`` or
  ``value`` from outside fires nothing, and a page that enables its 확인 button
  in an ``onclick`` handler stays disabled. Checkboxes are therefore toggled by
  clicking them, and text and selections get their ``onchange``.
* **Same-origin iframes are part of the page.** The 기안 window's editor lives
  in one, so frames are walked into.

Reading the page is one script run per document rather than a walk from out
here: each property read is a cross-process round trip, and a form page has
hundreds of elements.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable, Optional

from ..elements import NodeInfo
from ..win32.defs import ClaudeHandsError, require_windows
from ..win32.windows import Rect

IE_SERVER_CLASS = "Internet Explorer_Server"
SMTO_ABORTIFHUNG = 0x0002
RS, US = "\x1e", "\x1f"
OUTPUT_SLOT = "claudeHandsOut"

# K-에듀파인's 기안 window embeds the 한글 web control under this class id.
HWP_CONTROL_CLSIDS = {"bd9c32e4-3155-4691-8972-097d53b10052"}

_COLLECT_JS = (Path(__file__).with_name("collect.js")).read_text(encoding="utf-8")


class MshtmlUnavailableError(ClaudeHandsError):
    """Raised when the window has no IE document this engine can reach."""


class PageChangedError(ClaudeHandsError):
    """Raised when the element a ref named is no longer where it was."""


# --------------------------------------------------------------------------
# Finding the documents
# --------------------------------------------------------------------------


def _user32():
    return ctypes.WinDLL("user32", use_last_error=True)


def ie_servers(hwnd: int) -> list[int]:
    """Every IE document window under ``hwnd`` (or ``hwnd`` itself)."""

    require_windows()
    user32 = _user32()
    found: list[int] = []

    def class_of(handle) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(handle, buffer, 256)
        return buffer.value

    if class_of(hwnd) == IE_SERVER_CLASS:
        return [hwnd]

    proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(handle, _lparam):
        if class_of(handle) == IE_SERVER_CLASS and user32.IsWindowVisible(handle):
            found.append(int(handle))
        return True

    user32.EnumChildWindows(wintypes.HWND(hwnd), proc_type(visit), 0)
    return found


class _WindowPlacement(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("rcNormalPosition", wintypes.RECT),
    ]


def dominant_ie_server(hwnd: int, share: float = 0.5) -> Optional[int]:
    """The IE document that fills most of ``hwnd``, if one does.

    ``auto`` only switches engines when the page *is* the window. An
    application that shows a small help pane in an IE control is still a native
    application, and reading it through the page would hide everything else.
    """

    user32 = _user32()
    outer = wintypes.RECT()
    if user32.IsIconic(wintypes.HWND(hwnd)):
        # Minimised, the frame has no client area to compare against, while
        # its children keep their size. Its restored size is the measure.
        placement = _WindowPlacement()
        placement.length = ctypes.sizeof(placement)
        if not user32.GetWindowPlacement(wintypes.HWND(hwnd), ctypes.byref(placement)):
            return None
        outer = placement.rcNormalPosition
    elif not user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(outer)):
        return None
    area = max(1, (outer.right - outer.left) * (outer.bottom - outer.top))
    best, best_area = None, 0
    for server in ie_servers(hwnd):
        inner = wintypes.RECT()
        user32.GetClientRect(wintypes.HWND(server), ctypes.byref(inner))
        size = (inner.right - inner.left) * (inner.bottom - inner.top)
        if size > best_area:
            best, best_area = server, size
    if best is not None and best_area / area >= share:
        return best
    return None


def _dispatch_modules():
    import sys

    try:
        if "pythoncom" not in sys.modules:
            # pythoncom joins an apartment on the thread that first imports it,
            # and the kind comes from sys.coinit_flags — which this process
            # sets to the MTA for UI Automation (lesson 3). Imported first
            # here, on the STA worker, that asks for the wrong kind and fails
            # with RPC_E_CHANGED_MODE. Ask for the kind this thread already is.
            saved = getattr(sys, "coinit_flags", None)
            sys.coinit_flags = 2  # COINIT_APARTMENTTHREADED
            try:
                import pythoncom  # noqa: F401
            finally:
                if saved is None:
                    del sys.coinit_flags
                else:
                    sys.coinit_flags = saved
        import pythoncom
        import win32com.client.dynamic
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise MshtmlUnavailableError(
            "웹 문서 엔진에는 pywin32 가 필요합니다. `pip install pywin32` 로 설치하세요."
        ) from exc
    return pythoncom, win32com.client.dynamic


def document_of(server_hwnd: int):
    """The HTML document shown in one ``Internet Explorer_Server`` window."""

    pythoncom, dynamic = _dispatch_modules()
    user32 = _user32()
    message = user32.RegisterWindowMessageW("WM_HTML_GETOBJECT")
    result = ctypes.c_size_t(0)
    user32.SendMessageTimeoutW(
        wintypes.HWND(server_hwnd), message, 0, 0, SMTO_ABORTIFHUNG, 3000, ctypes.byref(result)
    )
    if not result.value:
        raise MshtmlUnavailableError(
            f"hwnd={server_hwnd} 의 웹 문서가 응답하지 않습니다. 페이지를 읽는 중이거나 "
            "프로그램이 멈춰 있을 수 있습니다."
        )
    raw = pythoncom.ObjectFromLresult(result.value, pythoncom.IID_IDispatch, 0)
    return dynamic.Dispatch(raw)


def run_script(document, source: str) -> str:
    """Evaluate ``source`` inside the page and return its value as a string.

    ``execScript`` returns nothing, so the value travels back through a
    property on the document. It is cleared afterwards so the page is left as
    it was found.
    """

    window = document.parentWindow
    wrapped = (
        f"document.{OUTPUT_SLOT} = (function () {{ try {{ return String({source}); }} "
        f"catch (e) {{ return '\\u0000' + (e.message || e.description || e); }} }})();"
    )
    window.execScript(wrapped, "JScript")
    value = str(getattr(document, OUTPUT_SLOT))
    window.execScript(f"document.{OUTPUT_SLOT} = null;", "JScript")
    if value.startswith("\x00"):
        raise ClaudeHandsError(f"페이지 안 스크립트가 실패했습니다: {value[1:]}")
    return value


def schedule_script(document, source: str) -> None:
    """Run ``source`` inside the page on its next turn, without waiting for it.

    This is how anything that might open a modal dialog is done: the dialog
    belongs to the page's event loop, and a caller waiting on it would wait
    until a person closed it.
    """

    document.parentWindow.setTimeout(source, 0)


# --------------------------------------------------------------------------
# Locating one element again
# --------------------------------------------------------------------------


class _Locator:
    """Where an element lives: which IE window, which frames, which index.

    The document is fetched again for every action rather than kept, because
    a page that has navigated leaves the old object answering for nothing.
    """

    def __init__(self, server_hwnd: int, frames: tuple[int, ...], index: int, tag: str, dom_id: str, dom_name: str):
        self.server_hwnd = server_hwnd
        self.frames = frames
        self.index = index
        self.tag = tag
        self.dom_id = dom_id
        self.dom_name = dom_name

    def document(self):
        document = document_of(self.server_hwnd)
        for frame_index in self.frames:
            document = document.all.item(frame_index).contentWindow.document
        return document

    def element(self):
        document = self.document()
        candidate = document.all.item(self.index)
        if candidate is not None and self._same(candidate):
            return document, candidate
        # The page shifted under the index — an element was added above this
        # one. An id still finds it; anything less would be a guess.
        if self.dom_id:
            candidate = document.getElementById(self.dom_id)
            if candidate is not None and self._same(candidate):
                return document, candidate
        raise PageChangedError(
            f"페이지가 바뀌어 <{self.tag.lower()} id={self.dom_id!r}> 를 다시 찾지 못했습니다. "
            "snapshot 을 다시 찍으세요."
        )

    def _same(self, candidate) -> bool:
        try:
            return (
                str(candidate.tagName) == self.tag
                and str(candidate.id or "") == self.dom_id
                and str(candidate.name or "") == self.dom_name
            )
        except Exception:  # noqa: BLE001 - a node without these is not ours
            return False

    def script_ref(self) -> str:
        """How the page's own script reaches this element."""

        return f"document.all[{self.index}]"


# --------------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------------


FIRE_CHANGE = (
    "var e = {ref}; if (e.fireEvent) {{ e.fireEvent('onchange'); }} else {{ "
    "var ev = document.createEvent('HTMLEvents'); ev.initEvent('change', true, false); "
    "e.dispatchEvent(ev); }}"
)


def _wait_for(check: Callable[[], bool], timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if check():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


class _InvokePattern:
    def __init__(self, locator: _Locator):
        self._locator = locator

    def Invoke(self) -> None:
        document, _element = self._locator.element()
        schedule_script(document, f"{self._locator.script_ref()}.click()")


class _TogglePattern:
    """A checkbox or radio button, changed the way a person changes it."""

    def __init__(self, locator: _Locator, radio: bool):
        self._locator = locator
        self._radio = radio

    @property
    def CurrentToggleState(self) -> int:
        _document, element = self._locator.element()
        return 1 if element.checked else 0

    def Toggle(self) -> None:
        document, element = self._locator.element()
        before = bool(element.checked)
        if self._radio and before:
            raise ClaudeHandsError(
                "라디오 버튼은 눌러서 끌 수 없습니다. 같은 묶음의 다른 항목을 고르세요."
            )
        schedule_script(document, f"{self._locator.script_ref()}.click()")
        # The click runs on the page's next turn. Wait for it, so whoever reads
        # the state next reads the new one — and say so if the page refused.
        if not _wait_for(lambda: bool(self._locator.element()[1].checked) != before):
            raise ClaudeHandsError(
                "눌렀지만 체크 상태가 바뀌지 않았습니다. 페이지가 클릭을 막았거나 "
                "대화상자가 떠 있을 수 있습니다."
            )


class _ValuePattern:
    """A text box, text area or drop-down list."""

    def __init__(self, locator: _Locator, kind: str):
        self._locator = locator
        self._kind = kind  # "text" or "select"

    @property
    def CurrentValue(self) -> str:
        _document, element = self._locator.element()
        if self._kind == "select":
            index = int(element.selectedIndex)
            return str(element.options.item(index).text) if index >= 0 else ""
        if str(element.type or "").lower() == "password":
            return None  # never read a password back into the conversation
        return str(element.value or "")

    @property
    def CurrentIsReadOnly(self) -> bool:
        _document, element = self._locator.element()
        return bool(element.disabled) or bool(getattr(element, "readOnly", False))

    def SetValue(self, value: str) -> None:
        document, element = self._locator.element()
        if self._kind == "select":
            _choose_option(element, value)
        else:
            element.value = value
        schedule_script(document, FIRE_CHANGE.format(ref=self._locator.script_ref()))


def _choose_option(select, wanted: str) -> None:
    """Pick an option by its text, then by its value, then by a partial match."""

    options = select.options
    count = int(options.length)
    entries = [(str(options.item(i).text), str(options.item(i).value)) for i in range(count)]
    for test in (
        lambda text, value: text == wanted,
        lambda text, value: value == wanted,
        lambda text, value: wanted in text,
    ):
        for index, (text, value) in enumerate(entries):
            if test(text, value):
                select.selectedIndex = index
                return
    raise ClaudeHandsError(
        f"{wanted!r} 항목이 목록에 없습니다. 있는 항목: " + ", ".join(t for t, _ in entries[:20])
    )


class _OptionPattern:
    """One option of a drop-down, selectable on its own ref."""

    def __init__(self, select: _Locator, text: str):
        self._select = select
        self._text = text

    def Select(self) -> None:
        _ValuePattern(self._select, "select").SetValue(self._text)

    AddToSelection = Select

    @property
    def CurrentIsSelected(self) -> bool:
        return _ValuePattern(self._select, "select").CurrentValue == self._text


class _DocumentRange:
    def __init__(self, read: Callable[[], str]):
        self._read = read

    def GetText(self, max_chars: int = -1) -> str:
        text = self._read()
        return text if max_chars is None or max_chars < 0 else text[:max_chars]


class _TextPattern:
    def __init__(self, read: Callable[[], str]):
        self.DocumentRange = _DocumentRange(read)


# --------------------------------------------------------------------------
# Element
# --------------------------------------------------------------------------


class WebElement:
    """One element of a page, answering the same protocol as the other engines."""

    engine = "mshtml"

    def __init__(
        self,
        *,
        role: str,
        name: str,
        value: str = "",
        locator: Optional[_Locator] = None,
        rect: Optional[Rect] = None,
        enabled: bool = True,
        toggle_state: Optional[str] = None,
        selected: Optional[bool] = None,
        patterns: Optional[dict[str, Any]] = None,
        help_text: str = "",
        hwnd: int = 0,
    ) -> None:
        self.role = role
        self.name = name
        self._value = value
        self.locator = locator
        self._rect = rect
        self._enabled = enabled
        self.toggle_state = toggle_state
        self.selected = selected
        self._patterns = patterns or {}
        self.help_text = help_text
        self.hwnd = hwnd

    # What the session reads to decide a ref is still alive (lesson 18).
    @property
    def rect(self) -> Optional[Rect]:
        return self._rect

    @property
    def enabled(self) -> bool:
        return self._enabled

    def pattern(self, key: str) -> Any:
        return self._patterns.get(key)

    def available_patterns(self) -> tuple[str, ...]:
        return tuple(self._patterns)

    def to_node_info(self, depth: int = 0) -> NodeInfo:
        locator = self.locator
        return NodeInfo(
            role=self.role,
            name=self.name,
            value=self._value,
            automation_id=(locator.dom_id or locator.dom_name) if locator else "",
            class_name=locator.tag.lower() if locator else "html",
            rect=self._rect,
            rect_space="screen",
            enabled=self._enabled,
            hwnd=self.hwnd,
            patterns=self.available_patterns(),
            toggle_state=self.toggle_state,
            selected=self.selected,
            help_text=self.help_text,
            depth=depth,
        )


# --------------------------------------------------------------------------
# Building the tree
# --------------------------------------------------------------------------


def _parse(raw: str) -> tuple[list[str], list[list[str]]]:
    records = raw.split(RS)
    header = records[0].split(US)
    rows = [record.split(US) for record in records[1:] if record]
    return header, rows


def _screen_origin(server_hwnd: int) -> Optional[tuple[int, int]]:
    """Where the page's (0,0) is on the screen, or None while minimised.

    A minimised window has no place on the screen, and a rectangle computed
    against its parking spot at -32000 would send a coordinate click to
    whatever is really at that spot.
    """

    user32 = _user32()
    root = user32.GetAncestor(wintypes.HWND(server_hwnd), 2)
    if root and user32.IsIconic(root):
        return None
    point = wintypes.POINT(0, 0)
    if not user32.ClientToScreen(wintypes.HWND(server_hwnd), ctypes.byref(point)):
        return None
    return point.x, point.y


def _rect(text: str, origin: Optional[tuple[int, int]]) -> Optional[Rect]:
    if origin is None or not text:
        return None
    try:
        x, y, width, height = (int(part) for part in text.split(","))
    except ValueError:
        return None
    left, top = origin[0] + x, origin[1] + y
    return Rect(left, top, left + width, top + height)


ROLE_LABELS = {"activex": "custom"}


def _page_text(locator_document: Callable[[], Any]) -> Callable[[], str]:
    def read() -> str:
        document = locator_document()
        parts = [str(document.body.innerText or "")]
        frames = document.frames
        for index in range(int(frames.length)):
            try:
                parts.append(str(frames.item(index).document.body.innerText or ""))
            except Exception:  # noqa: BLE001 - another origin keeps its text
                continue
        return "\n".join(part for part in parts if part.strip())

    return read


def _walk_document(
    document,
    *,
    server_hwnd: int,
    frames: tuple[int, ...],
    origin: Optional[tuple[int, int]],
    parent: NodeInfo,
    depth: int,
    element_index: dict[int, Any],
    budget: list[int],
) -> None:
    raw = run_script(document, f"{_COLLECT_JS}({budget[0]})")
    header, rows = _parse(raw)
    if len(header) > 3 and header[3] == "1":
        parent.truncated_children += 1
    rows.sort(key=lambda row: int(row[0]))

    by_index: dict[int, NodeInfo] = {}
    for row in rows:
        if budget[0] <= 0:
            parent.truncated_children += 1
            return
        (index, parent_index, role, tag, input_type, dom_id, dom_name,
         name, value, state, disabled, readonly, rect_text) = (row + [""] * 13)[:13]
        index, parent_index = int(index), int(parent_index)
        locator = _Locator(server_hwnd, frames, index, tag, dom_id, dom_name)
        rect = _rect(rect_text, origin)
        enabled = disabled != "1"
        patterns: dict[str, Any] = {}
        toggle_state = None
        selected = None
        help_text = ""

        if role in {"checkbox", "radiobutton"}:
            patterns["toggle"] = _TogglePattern(locator, radio=role == "radiobutton")
            toggle_state = state
        elif role == "edit":
            patterns["value"] = _ValuePattern(locator, "text")
            if state == "secret":
                help_text = "비밀번호 칸 — 값을 읽어 오지 않습니다"
        elif role == "combobox":
            patterns["value"] = _ValuePattern(locator, "select")
        elif role == "listitem":
            owner = by_index.get(parent_index)
            owner_element = element_index.get(id(owner)) if owner is not None else None
            if owner_element is not None and owner_element.locator is not None:
                patterns["selectionitem"] = _OptionPattern(owner_element.locator, name)
            selected = state == "selected"
            value = ""
        elif role in {"button", "hyperlink"}:
            patterns["invoke"] = _InvokePattern(locator)

        element = WebElement(
            role=ROLE_LABELS.get(role, role if role != "frame" else "pane"),
            name=name,
            value=value if role in {"edit", "combobox"} else "",
            locator=locator,
            rect=rect,
            enabled=enabled,
            toggle_state=toggle_state,
            selected=selected,
            patterns=patterns,
            help_text=help_text,
            hwnd=server_hwnd,
        )
        container = by_index.get(parent_index, parent)
        node = element.to_node_info(container.depth + 1)
        container.children.append(node)
        element_index[id(node)] = element
        by_index[index] = node
        budget[0] -= 1

        if role == "frame":
            _walk_frame(document, index, node, server_hwnd, frames, origin, element_index, budget, rect_text)
        elif role == "activex" and value.replace("clsid:", "").strip("{}") in HWP_CONTROL_CLSIDS:
            _walk_hwp_control(document, index, node, element_index, budget)


def _walk_frame(document, index, node, server_hwnd, frames, origin, element_index, budget, rect_text):
    try:
        inner = document.all.item(index).contentWindow.document
    except Exception:  # noqa: BLE001 - another origin refuses
        node.help_text = "다른 출처의 프레임이라 안을 읽을 수 없습니다"
        return
    inner_origin = None
    if origin is not None and rect_text:
        try:
            x, y, _w, _h = (int(part) for part in rect_text.split(","))
            # The frame's border sits between its box and its page.
            frame = document.all.item(index)
            inner_origin = (origin[0] + x + int(frame.clientLeft or 0), origin[1] + y + int(frame.clientTop or 0))
        except Exception:  # noqa: BLE001 - geometry is a nicety here
            inner_origin = None
    try:
        _walk_document(
            inner,
            server_hwnd=server_hwnd,
            frames=frames + (index,),
            origin=inner_origin,
            parent=node,
            depth=node.depth,
            element_index=element_index,
            budget=budget,
        )
    except ClaudeHandsError as exc:
        node.help_text = f"프레임을 읽지 못했습니다: {exc}"


def _walk_hwp_control(document, index, node, element_index, budget) -> None:
    """The 한글 web control: its text and 누름틀, through the 한글 adapter.

    Lesson 21 keeps 누름틀 read-only when several 한글 windows are open,
    because 한글's API writes to whichever document is active. A control
    embedded in a page is one document on its own, so that ambiguity does not
    arise here and the fields are writable.
    """

    from ..office.hwp import HwpAdapter

    control = document.all.item(index)
    node.help_text = "한글 컨트롤 — 본문과 누름틀을 읽고 쓸 수 있습니다"

    class _Budget:
        def spent(self) -> bool:
            return budget[0] <= 0

    def emit(office_element, *, depth, parent):
        # Fetched on the STA worker, so it must be called from there too.
        office_element.engine = "mshtml"
        child = office_element.to_node_info(node.depth + 1)
        node.children.append(child)
        element_index[id(child)] = office_element
        budget[0] -= 1
        return child

    try:
        HwpAdapter.walk({"app": control, "windows": 1}, emit, _Budget())
    except Exception as exc:  # noqa: BLE001 - a control still loading
        node.help_text = f"한글 컨트롤에 접근하지 못했습니다: {exc}"


def build_mshtml_tree(hwnd: int, *, max_nodes: int = 1200):
    """Build a NodeInfo tree for the page(s) shown in ``hwnd``.

    Mirrors the other engines' builders so a session can use it the same way.
    """

    require_windows()
    servers = ie_servers(hwnd)
    if not servers:
        raise MshtmlUnavailableError(
            f"hwnd={hwnd} 안에 IE 웹 문서({IE_SERVER_CLASS})가 없습니다. "
            "engine='uia' 로 연결하세요."
        )

    element_index: dict[int, Any] = {}
    budget = [max_nodes]
    root_document = document_of(servers[0])
    title = str(root_document.title or "")
    root_element = WebElement(
        role="document",
        name=title,
        patterns={"text": _TextPattern(_page_text(lambda: document_of(servers[0])))},
        help_text=f"웹 문서 · 문서 모드 {root_document.documentMode}",
        hwnd=hwnd,
    )
    root = root_element.to_node_info(0)
    element_index[id(root)] = root_element

    for position, server in enumerate(servers):
        document = root_document if position == 0 else document_of(server)
        if len(servers) > 1:
            holder_element = WebElement(role="pane", name=str(document.title or ""), hwnd=server)
            holder = holder_element.to_node_info(1)
            root.children.append(holder)
            element_index[id(holder)] = holder_element
        else:
            holder = root
        _walk_document(
            document,
            server_hwnd=server,
            frames=(),
            origin=_screen_origin(server),
            parent=holder,
            depth=holder.depth,
            element_index=element_index,
            budget=budget,
        )
    return root, element_index
