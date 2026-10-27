"""Pages inside an IE WebBrowser control, through their own DOM."""

from .core import (
    MshtmlUnavailableError,
    PageChangedError,
    WebElement,
    build_mshtml_tree,
    dominant_ie_server,
    ie_servers,
)

__all__ = [
    "MshtmlUnavailableError",
    "PageChangedError",
    "WebElement",
    "build_mshtml_tree",
    "dominant_ie_server",
    "ie_servers",
]
