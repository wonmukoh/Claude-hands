"""claude-hands — drive a single Windows application, even while it is minimised.

Unlike a computer-use loop that screenshots the whole desktop and moves the real
mouse, this package attaches to one window and talks to it through UI
Automation and window messages. Nothing is stolen from the user: no cursor
movement, no focus changes, no foreground activation. The window can sit
minimised behind everything else and still be read and driven.
"""

from .win32.defs import enable_dpi_awareness as _enable_dpi_awareness

# Must happen before anything reads a coordinate. Windows hands a DPI-unaware
# process *virtualised* coordinates — on a 200% display GetCursorPos and
# GetWindowRect come back at half the real values — and switching awareness
# mid-run silently changes the coordinate system underneath every rect and
# point already measured. Doing it at import means one coordinate space for
# the life of the process.
DPI_AWARENESS = _enable_dpi_awareness()

from .api import (  # noqa: F401,E402
    Window,
    attach,
    attached,
    current,
    manager,
    window_info,
    windows,
)
from .elements import NodeInfo  # noqa: F401,E402
from .win32.defs import ClaudeHandsError, NotOnWindowsError  # noqa: F401,E402
from .win32.windows import (  # noqa: F401,E402
    AmbiguousWindowError,
    WindowInfo,
    WindowNotFoundError,
)

__version__ = "0.1.0"

__all__ = [
    "Window",
    "WindowInfo",
    "NodeInfo",
    "attach",
    "attached",
    "current",
    "manager",
    "window_info",
    "windows",
    "ClaudeHandsError",
    "NotOnWindowsError",
    "WindowNotFoundError",
    "AmbiguousWindowError",
    "DPI_AWARENESS",
    "__version__",
]
