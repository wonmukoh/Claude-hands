"""Win32 layer: ctypes bindings, window discovery, capture, and message input."""

from .defs import (  # noqa: F401
    IS_WINDOWS,
    ClaudeHandsError,
    NotOnWindowsError,
    enable_dpi_awareness,
    make_lparam,
    require_windows,
)
from .windows import (  # noqa: F401
    AmbiguousWindowError,
    Rect,
    WindowInfo,
    WindowNotFoundError,
    cursor_pos,
    describe_window,
    find_window,
    foreground_hwnd,
    list_windows,
)
