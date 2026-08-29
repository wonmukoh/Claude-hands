"""UI Automation access layer (Windows only)."""

from .core import (  # noqa: F401
    UiaElement,
    UiaUnavailableError,
    UiaWorker,
    build_tree,
    element_from_hwnd,
    ensure_com,
    focused_element,
    get_automation,
    shutdown,
)
