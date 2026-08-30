"""Top-level window discovery and non-activating window state control."""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass, field
from typing import Callable, Iterator

from .defs import (
    HWND_BOTTOM,
    IS_WINDOWS,
    ClaudeHandsError,
    DWMWA_CLOAKED,
    DWMWA_EXTENDED_FRAME_BOUNDS,
    GWL_EXSTYLE,
    PROCESS_QUERY_LIMITED_INFORMATION,
    SM_CXVIRTUALSCREEN,
    SM_CYVIRTUALSCREEN,
    SM_XVIRTUALSCREEN,
    SM_YVIRTUALSCREEN,
    SW_MINIMIZE,
    SW_RESTORE,
    SW_SHOWMAXIMIZED,
    SW_SHOWNOACTIVATE,
    SWP_NOACTIVATE,
    SWP_NOMOVE,
    SWP_NOOWNERZORDER,
    SWP_NOSIZE,
    SWP_NOZORDER,
    WM_CLOSE,
    WS_EX_TOOLWINDOW,
    require_windows,
)


class WindowNotFoundError(ClaudeHandsError):
    """Raised when no window matches the caller's selector."""


class AmbiguousWindowError(ClaudeHandsError):
    """Raised when a selector matches several windows and none is preferred."""

    def __init__(self, message: str, candidates: "list[WindowInfo]") -> None:
        super().__init__(message)
        self.candidates = candidates


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.width // 2, self.top + self.height // 2)

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    def as_dict(self) -> dict:
        return {
            "x": self.left,
            "y": self.top,
            "width": self.width,
            "height": self.height,
        }


@dataclass
class WindowInfo:
    """A snapshot of one top-level window's identity and state."""

    hwnd: int
    title: str
    class_name: str
    pid: int
    process: str
    rect: Rect
    minimized: bool
    maximized: bool
    visible: bool
    cloaked: bool = False
    foreground: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def state(self) -> str:
        if self.minimized:
            return "minimized"
        if self.cloaked or not self.visible:
            return "hidden"
        if self.maximized:
            return "maximized"
        return "normal"

    def as_dict(self) -> dict:
        return {
            "hwnd": self.hwnd,
            "title": self.title,
            "class_name": self.class_name,
            "pid": self.pid,
            "process": self.process,
            "state": self.state,
            "foreground": self.foreground,
            "rect": self.rect.as_dict(),
        }

    def describe(self) -> str:
        return (
            f"hwnd={self.hwnd} [{self.state}] {self.process} (pid {self.pid}) "
            f"— {self.title or '(제목 없음)'}"
        )


# --------------------------------------------------------------------------
# Low-level helpers
# --------------------------------------------------------------------------


def _window_text(hwnd: int) -> str:
    from .defs import user32

    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _class_name(hwnd: int) -> str:
    from .defs import user32

    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _process_of(hwnd: int) -> tuple[int, str]:
    from .defs import kernel32, user32, wintypes

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return 0, ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return pid.value, ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return pid.value, os.path.basename(buf.value)
        return pid.value, ""
    finally:
        kernel32.CloseHandle(handle)


def _is_cloaked(hwnd: int) -> bool:
    from .defs import dwmapi, wintypes

    cloaked = wintypes.DWORD(0)
    hresult = dwmapi.DwmGetWindowAttribute(
        hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
    )
    return hresult == 0 and cloaked.value != 0


def window_rect(hwnd: int, *, frame_bounds: bool = True) -> Rect:
    """Return the window rectangle in screen coordinates.

    ``frame_bounds`` uses the DWM extended frame bounds, which excludes the
    invisible resize border modern Windows adds — that border is what makes
    naive ``PrintWindow`` captures look padded.
    """

    require_windows()
    from .defs import RECT, dwmapi, user32

    if frame_bounds:
        rect = RECT()
        hresult = dwmapi.DwmGetWindowAttribute(
            hwnd,
            DWMWA_EXTENDED_FRAME_BOUNDS,
            ctypes.byref(rect),
            ctypes.sizeof(rect),
        )
        if hresult == 0 and rect.right > rect.left and rect.bottom > rect.top:
            return Rect(rect.left, rect.top, rect.right, rect.bottom)

    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise ClaudeHandsError(f"GetWindowRect failed for hwnd={hwnd}")
    return Rect(rect.left, rect.top, rect.right, rect.bottom)


def client_rect(hwnd: int) -> Rect:
    require_windows()
    from .defs import RECT, user32

    rect = RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise ClaudeHandsError(f"GetClientRect failed for hwnd={hwnd}")
    return Rect(rect.left, rect.top, rect.right, rect.bottom)


def screen_to_client(hwnd: int, x: int, y: int) -> tuple[int, int]:
    require_windows()
    from .defs import POINT, user32

    point = POINT(x, y)
    user32.ScreenToClient(hwnd, ctypes.byref(point))
    return point.x, point.y


def client_to_screen(hwnd: int, x: int, y: int) -> tuple[int, int]:
    require_windows()
    from .defs import POINT, user32

    point = POINT(x, y)
    user32.ClientToScreen(hwnd, ctypes.byref(point))
    return point.x, point.y


def is_window(hwnd: int) -> bool:
    if not IS_WINDOWS:
        return False
    from .defs import user32

    return bool(user32.IsWindow(hwnd))


def is_minimized(hwnd: int) -> bool:
    require_windows()
    from .defs import user32

    return bool(user32.IsIconic(hwnd))


def describe_window(hwnd: int) -> WindowInfo:
    """Build a :class:`WindowInfo` for one HWND."""

    require_windows()
    from .defs import user32

    if not user32.IsWindow(hwnd):
        raise WindowNotFoundError(f"hwnd={hwnd} 는 더 이상 존재하지 않는 창입니다.")
    pid, process = _process_of(hwnd)
    try:
        rect = window_rect(hwnd)
    except ClaudeHandsError:
        rect = Rect(0, 0, 0, 0)
    return WindowInfo(
        hwnd=hwnd,
        title=_window_text(hwnd),
        class_name=_class_name(hwnd),
        pid=pid,
        process=process,
        rect=rect,
        minimized=bool(user32.IsIconic(hwnd)),
        maximized=bool(user32.IsZoomed(hwnd)),
        visible=bool(user32.IsWindowVisible(hwnd)),
        cloaked=_is_cloaked(hwnd),
        foreground=user32.GetForegroundWindow() == hwnd,
    )


def iter_top_level_hwnds() -> Iterator[int]:
    require_windows()
    from .defs import WNDENUMPROC, user32

    found: list[int] = []

    @WNDENUMPROC
    def _collect(hwnd, _lparam):  # pragma: no cover - callback
        found.append(hwnd)
        return True

    user32.EnumWindows(_collect, 0)
    return iter(found)


def list_windows(
    *,
    include_hidden: bool = False,
    include_tool_windows: bool = False,
    title_contains: str | None = None,
    process: str | None = None,
    pid: int | None = None,
) -> list[WindowInfo]:
    """Enumerate top-level windows, newest-interesting first.

    Minimised windows are always included — they are the whole point of this
    package.
    """

    require_windows()
    from .defs import user32

    results: list[WindowInfo] = []
    for hwnd in iter_top_level_hwnds():
        try:
            info = describe_window(hwnd)
        except ClaudeHandsError:
            continue

        if not include_tool_windows:
            ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if ex_style & WS_EX_TOOLWINDOW:
                continue
        if not include_hidden:
            # A minimised window is not "visible" in the IsWindowVisible sense
            # on some shells, so keep it explicitly.
            if not info.visible and not info.minimized:
                continue
            if info.cloaked and not info.minimized:
                continue
            if not info.title:
                continue
        if title_contains and title_contains.lower() not in info.title.lower():
            continue
        if process and process.lower() not in (info.process or "").lower():
            continue
        if pid is not None and info.pid != pid:
            continue
        results.append(info)

    results.sort(key=lambda w: (w.state == "hidden", not w.foreground, w.title.lower()))
    return results


def find_window(
    *,
    hwnd: int | None = None,
    title: str | None = None,
    process: str | None = None,
    pid: int | None = None,
    exact_title: bool = False,
    include_hidden: bool = False,
) -> WindowInfo:
    """Resolve a single window from a selector, or explain why it can't."""

    require_windows()
    if hwnd is not None:
        return describe_window(hwnd)

    candidates = list_windows(
        include_hidden=include_hidden,
        title_contains=None if exact_title else title,
        process=process,
        pid=pid,
    )
    if exact_title and title is not None:
        candidates = [c for c in candidates if c.title == title]

    if not candidates:
        selector = ", ".join(
            part
            for part in (
                f"title~{title!r}" if title else None,
                f"process={process!r}" if process else None,
                f"pid={pid}" if pid is not None else None,
            )
            if part
        )
        raise WindowNotFoundError(
            f"조건에 맞는 창을 찾지 못했습니다 ({selector or '조건 없음'}). "
            "list_windows 로 목록을 먼저 확인하세요."
        )
    if len(candidates) > 1:
        exact = [c for c in candidates if title and c.title == title]
        if len(exact) == 1:
            return exact[0]
        foreground = [c for c in candidates if c.foreground]
        if len(foreground) == 1:
            return foreground[0]
        raise AmbiguousWindowError(
            "여러 창이 조건에 일치합니다. hwnd 로 정확히 지정하세요:\n"
            + "\n".join(f"  - {c.describe()}" for c in candidates[:12]),
            candidates,
        )
    return candidates[0]


# --------------------------------------------------------------------------
# Non-activating window state control
# --------------------------------------------------------------------------


def show_without_activating(hwnd: int) -> None:
    """Restore a window without stealing focus from whatever the user is doing."""

    require_windows()
    from .defs import user32

    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)


def minimize(hwnd: int) -> None:
    require_windows()
    from .defs import user32

    user32.ShowWindow(hwnd, SW_MINIMIZE)


def restore(hwnd: int, *, activate: bool = False) -> None:
    require_windows()
    from .defs import user32

    user32.ShowWindow(hwnd, SW_RESTORE if activate else SW_SHOWNOACTIVATE)


def maximize(hwnd: int) -> None:
    require_windows()
    from .defs import user32

    user32.ShowWindow(hwnd, SW_SHOWMAXIMIZED)


def move_window(
    hwnd: int, x: int, y: int, width: int | None = None, height: int | None = None
) -> None:
    """Move/resize without activating or changing z-order."""

    require_windows()
    from .defs import user32

    flags = SWP_NOACTIVATE | SWP_NOZORDER | SWP_NOOWNERZORDER
    if width is None or height is None:
        flags |= SWP_NOSIZE
        width = height = 0
    if not user32.SetWindowPos(hwnd, 0, x, y, width, height, flags):
        raise ClaudeHandsError(f"SetWindowPos failed for hwnd={hwnd}")


def send_to_bottom(hwnd: int) -> None:
    """Push a window behind every other window without activating it."""

    require_windows()
    from .defs import user32

    user32.SetWindowPos(
        hwnd, HWND_BOTTOM, 0, 0, 0, 0,
        SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE | SWP_NOOWNERZORDER,
    )


def close_window(hwnd: int) -> None:
    """Ask the window to close (same as clicking the X), never force-kills."""

    require_windows()
    from .defs import user32

    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


def cursor_pos() -> tuple[int, int]:
    """Where the user's mouse pointer is. Used to prove we never moved it."""

    require_windows()
    from .defs import POINT, user32

    point = POINT()
    user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def foreground_hwnd() -> int:
    """The window the user is actually working in right now."""

    require_windows()
    from .defs import user32

    return int(user32.GetForegroundWindow())


def virtual_screen_rect() -> Rect:
    require_windows()
    from .defs import user32

    x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return Rect(x, y, x + w, y + h)


def get_placement(hwnd: int):
    require_windows()
    from .defs import WINDOWPLACEMENT, user32

    placement = WINDOWPLACEMENT()
    placement.length = ctypes.sizeof(WINDOWPLACEMENT)
    if not user32.GetWindowPlacement(hwnd, ctypes.byref(placement)):
        raise ClaudeHandsError(f"GetWindowPlacement failed for hwnd={hwnd}")
    return placement


def set_placement(hwnd: int, placement) -> None:
    require_windows()
    from .defs import user32

    user32.SetWindowPlacement(hwnd, ctypes.byref(placement))


def deepest_child_at(hwnd: int, screen_x: int, screen_y: int) -> int:
    """Walk down the child-window chain to the control under a screen point.

    Unlike ``WindowFromPoint`` this never consults the desktop z-order, so it
    gives the right answer for a window buried behind five others.
    """

    require_windows()
    from .defs import POINT, CWP_SKIPINVISIBLE, CWP_SKIPTRANSPARENT, user32

    current = hwnd
    for _ in range(32):  # depth guard; real UIs never nest this deep
        cx, cy = screen_to_client(current, screen_x, screen_y)
        point = POINT(cx, cy)
        child = user32.ChildWindowFromPointEx(
            current, point, CWP_SKIPINVISIBLE | CWP_SKIPTRANSPARENT
        )
        if not child or child == current:
            return current
        current = child
    return current


def enum_child_windows(hwnd: int, predicate: Callable[[int], bool] | None = None) -> list[int]:
    require_windows()
    from .defs import WNDENUMPROC, user32

    found: list[int] = []

    @WNDENUMPROC
    def _collect(child, _lparam):  # pragma: no cover - callback
        if predicate is None or predicate(child):
            found.append(child)
        return True

    user32.EnumChildWindows(hwnd, _collect, 0)
    return found
