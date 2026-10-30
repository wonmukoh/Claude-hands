"""한글(HWP) as a document-model engine.

Everything here was measured against 한글 2024 rather than taken from a manual,
because the two disagree in ways that cost a session to discover.

**한글 does not answer to its own ProgID.** `GetActiveObject("HWPFrame.HwpObject")`
fails with MK_E_UNAVAILABLE against a 한글 that is running and visible. It
registers in the Running Object Table under a version-stamped name instead —
`!HwpObject.130.1` for 한글 2024 — so the only reliable way in is to enumerate
the table and bind whatever `HwpObject.*` is there. Guessing the ProgID with the
version number appended would break on the next release.

**Its automation is application-level, not document-level.** `PutFieldText` and
friends act on whichever document 한글 currently has active, with no argument
saying which. That is exactly the trap that would write into somebody's open
form (see CLAUDE.md lesson 14), so when more than one document is open this
engine reads and refuses to write, rather than writing somewhere plausible.
"""

from __future__ import annotations

from typing import Optional

from .core import OfficeElement, OfficeUnavailableError, com_retry

# GetFieldList packs the names into one string separated by this.
FIELD_SEPARATOR = "\x02"


def _running_hwp():
    """Bind whatever 한글 is registered in the Running Object Table.

    pywin32 is used here rather than comtypes because it exposes the table
    directly; comtypes only offers the ProgID lookup that 한글 does not answer.
    """

    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise OfficeUnavailableError(
            "한글 자동화에는 pywin32 가 필요합니다. `pip install pywin32` 로 설치하세요."
        ) from exc

    table = pythoncom.GetRunningObjectTable()
    context = pythoncom.CreateBindCtx(0)
    for moniker in table:
        try:
            name = moniker.GetDisplayName(context, None)
        except Exception:  # noqa: BLE001 - a moniker that will not name itself
            continue
        if not name.lstrip("!").lower().startswith("hwpobject"):
            continue
        raw = table.GetObject(moniker)
        return win32com.client.Dispatch(raw.QueryInterface(pythoncom.IID_IDispatch))

    raise OfficeUnavailableError(
        "실행 중인 한글을 찾지 못했습니다. 한글이 떠 있는지 확인하세요. "
        "(한글은 자기 ProgID 로는 응답하지 않아 실행 객체 테이블에서 찾습니다.)"
    )


def _document_text(app) -> str:
    """The whole document as text.

    한글 2024 answers ``"TEXT"``. The web control K-에듀파인 embeds was reported
    answering ``"UNICODE"`` (by the session that drove it; not measured here),
    so that is the second try rather than a guess left out.
    """

    for kind in ("TEXT", "UNICODE"):
        try:
            text = com_retry(lambda kind=kind: app.GetTextFile(kind, ""))
        except Exception:  # noqa: BLE001 - try the other format
            continue
        if text:
            return str(text)
    return ""


def _document_count(app) -> int:
    try:
        return int(com_retry(lambda: app.XHwpDocuments.Count))
    except Exception:  # noqa: BLE001 - unknown is not "one"
        return 2


def _ensure_still_alone(app, attached: int) -> None:
    from .core import _root_hwnd

    windows = com_retry(lambda: app.XHwpWindows)
    alone = _document_count(app) == 1 and int(com_retry(lambda: windows.Count)) == 1
    if alone:
        handle = int(com_retry(lambda: windows.Item(0).WindowHandle) or 0)
        alone = bool(handle) and _root_hwnd(handle) == attached
    if not alone:
        raise OfficeUnavailableError(
            "한글에 문서가 둘 이상 열려 있어 쓰지 않았습니다. 한글의 누름틀 입력은 지금 활성인 "
            "문서에 들어가서, 이 창의 문서라는 보장이 없습니다. 다른 문서를 닫고 다시 시도하세요."
        )


def _field_names(app) -> list[str]:
    raw = com_retry(lambda: app.GetFieldList(0, 0)) or ""
    return [name for name in str(raw).split(FIELD_SEPARATOR) if name]


class HwpAdapter:
    """The open document: its text, and its 누름틀 as writable elements."""

    label = "한글"

    @staticmethod
    def acquire():
        return _running_hwp()

    @staticmethod
    def documents(app):
        windows = com_retry(lambda: app.XHwpWindows)
        count = com_retry(lambda: windows.Count)
        for index in range(count):
            window = com_retry(lambda index=index: windows.Item(index))
            handle = com_retry(lambda: window.WindowHandle)
            if not handle:
                continue
            # The "document" carries the window count with it, because whether
            # writing is safe depends on it and nothing below can see it.
            yield window, {"app": app, "windows": count}, int(handle)

    @staticmethod
    def title(document) -> str:
        app = document["app"]
        try:
            documents = com_retry(lambda: app.XHwpDocuments)
            name = com_retry(lambda: documents.Item(0).FullName)
        except Exception:  # noqa: BLE001 - an unsaved document has no path
            name = ""
        return str(name) if name else "빈 문서"

    @staticmethod
    def saved(document) -> bool:
        return True

    @staticmethod
    def walk(document, emit, budget):
        app = document["app"]
        attached = document.get("hwnd")
        # 한글 opens further documents as tabs of one window, so one window is
        # not one document: count both.
        single = document["windows"] <= 1 and (attached is None or _document_count(app) <= 1)

        text = _document_text(app).replace("\r\n", "\n")
        emit(
            OfficeElement(
                role="document",
                name="본문",
                read=lambda: text,
                read_only=True,
                help_text="문서 전체 텍스트 (읽기 전용 — 통째로 바꾸는 것은 이 도구가 하지 않습니다)",
            ),
            depth=1,
            parent=None,
        )

        for name in _field_names(app):
            if budget.spent():
                return

            def read(name=name) -> str:
                return str(com_retry(lambda: app.GetFieldText(name)) or "")

            def write(value: str, name=name) -> None:
                if attached is not None:
                    # Decided at snapshot time is not enough: the user may
                    # have opened another document since, and PutFieldText
                    # writes to whichever is active.
                    _ensure_still_alone(app, attached)
                com_retry(lambda: app.PutFieldText(name, value))

            emit(
                OfficeElement(
                    role="edit",
                    name=name,
                    read=read,
                    write=None if not single else write,
                    read_only=not single,
                    help_text=(
                        "누름틀"
                        if single
                        else "누름틀 (한글 창이 여럿이라 읽기 전용입니다 — "
                        "한글의 입력 API 는 어느 문서인지 지정할 수 없습니다)"
                    ),
                ),
                depth=1,
                parent=None,
            )
