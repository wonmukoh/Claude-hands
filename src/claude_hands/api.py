"""The scripting facade: attach to a window, then drive it.

    from claude_hands import attach, windows

    for info in windows():
        print(info.describe())

    notepad = attach(title="메모장")
    print(notepad.snapshot())        # 창이 최소화되어 있어도 읽힙니다
    notepad.type("e7", "안녕하세요")
    notepad.keys("ctrl+s")
"""

from __future__ import annotations

from . import actions as _actions
from .elements import NodeInfo, format_node_line
from .session import SessionManager, SnapshotOptions, WindowSession
from .win32.capture import Capture, capture_window
from .win32.windows import (
    WindowInfo,
    close_window,
    describe_window,
    list_windows,
    maximize,
    minimize,
    move_window,
    restore,
)

_manager = SessionManager()


def manager() -> SessionManager:
    """The process-wide session manager (shared by the CLI and MCP server)."""

    return _manager


def windows(**kwargs) -> list[WindowInfo]:
    """List top-level windows, including minimised ones."""

    return list_windows(**kwargs)


class Window:
    """A window you have attached to. All methods are background-safe."""

    def __init__(self, session: WindowSession) -> None:
        self._session = session

    # -- identity ---------------------------------------------------------
    @property
    def session(self) -> WindowSession:
        return self._session

    @property
    def hwnd(self) -> int:
        return self._session.hwnd

    @property
    def info(self) -> WindowInfo:
        return self._session.refresh_info()

    @property
    def engine(self) -> str:
        """Which backend actually served the last snapshot."""

        return self._session.active_engine or self._session.engine

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Window hwnd={self.hwnd} {self._session.info.title!r}>"

    # -- reading ----------------------------------------------------------
    def snapshot(
        self,
        *,
        depth: int = 12,
        max_lines: int = 400,
        interactive_only: bool = False,
        show_rect: bool = True,
        keep_offscreen: bool = False,
    ) -> str:
        return self._session.render(
            SnapshotOptions(
                max_depth=depth,
                max_lines=max_lines,
                interactive_only=interactive_only,
                show_rect=show_rect,
                keep_offscreen=keep_offscreen,
            )
        )

    def tree(self, **kwargs) -> NodeInfo:
        return self._session.capture_tree(SnapshotOptions(**kwargs))

    def find(self, query: str, *, role: str | None = None, limit: int = 10) -> list[NodeInfo]:
        return [node for _score, node in self._session.find(query, role=role, limit=limit)]

    def describe(self, query: str, *, limit: int = 10) -> str:
        found = self.find(query, limit=limit)
        if not found:
            return f"{query!r} 와(과) 일치하는 요소가 없습니다."
        return "\n".join(format_node_line(node) for node in found)

    def text(self, ref: str | None = None, *, max_chars: int = 8000) -> str:
        return _actions.get_text(self._session, ref, max_chars=max_chars)

    def screenshot(self, ref: str | None = None, *, restore_if_minimized: bool = True) -> Capture:
        capture = capture_window(self.hwnd, restore_if_minimized=restore_if_minimized)
        if ref:
            node, _element = self._session.resolve(ref)
            if node.rect:
                return capture.crop(node.rect)
        return capture

    # -- acting -----------------------------------------------------------
    def click(self, ref: str, **kwargs) -> _actions.ActionResult:
        return _actions.click(self._session, ref, **kwargs)

    def right_click(self, ref: str) -> _actions.ActionResult:
        return _actions.click(self._session, ref, button="right")

    def double_click(self, ref: str) -> _actions.ActionResult:
        return _actions.click(self._session, ref, double=True)

    def type(self, ref: str, text: str, **kwargs) -> _actions.ActionResult:
        return _actions.type_text(self._session, ref, text, **kwargs)

    def set_value(self, ref: str, value) -> _actions.ActionResult:
        return _actions.set_value(self._session, ref, value)

    def keys(self, spec: str, *, ref: str | None = None, repeat: int = 1) -> _actions.ActionResult:
        return _actions.press_keys(self._session, spec, ref=ref, repeat=repeat)

    def focus(self, ref: str) -> _actions.ActionResult:
        return _actions.focus(self._session, ref)

    def scroll(self, **kwargs) -> _actions.ActionResult:
        return _actions.scroll(self._session, **kwargs)

    def select(self, ref: str, *, add: bool = False) -> _actions.ActionResult:
        return _actions.select(self._session, ref, add=add)

    def toggle(self, ref: str, *, to: bool | None = None) -> _actions.ActionResult:
        return _actions.toggle(self._session, ref, to=to)

    def expand(self, ref: str, *, collapse: bool = False) -> _actions.ActionResult:
        return _actions.expand(self._session, ref, collapse=collapse)

    def menu(self, path: str) -> _actions.ActionResult:
        return _actions.menu_select(self._session, path)

    def wait_for(self, query: str, **kwargs) -> _actions.ActionResult:
        return _actions.wait_for(self._session, query, **kwargs)

    # -- window state -----------------------------------------------------
    def minimize(self) -> None:
        minimize(self.hwnd)

    def restore(self, *, activate: bool = False) -> None:
        restore(self.hwnd, activate=activate)

    def maximize(self) -> None:
        maximize(self.hwnd)

    def move(self, x: int, y: int, width: int | None = None, height: int | None = None) -> None:
        move_window(self.hwnd, x, y, width, height)

    def close(self) -> None:
        close_window(self.hwnd)

    def detach(self) -> bool:
        return _manager.detach(self.hwnd)


def attach(
    *,
    hwnd: int | None = None,
    title: str | None = None,
    process: str | None = None,
    pid: int | None = None,
    exact_title: bool = False,
    engine: str = "auto",
) -> Window:
    """Attach to one window and return a :class:`Window` handle.

    ``engine`` picks the backend: ``"uia"`` (UI Automation — richer, the
    default preference), ``"win32"`` (window messages only — sees just real
    HWND controls, but needs no COM), or ``"auto"`` to prefer UIA and drop to
    win32 if UIA cannot start.
    """

    session = _manager.attach(
        hwnd=hwnd, title=title, process=process, pid=pid,
        exact_title=exact_title, engine=engine,
    )
    return Window(session)


def current() -> Window:
    """The most recently attached window."""

    return Window(_manager.current())


def attached() -> list[Window]:
    return [Window(session) for session in _manager.all()]


def window_info(hwnd: int) -> WindowInfo:
    return describe_window(hwnd)
