"""Office document automation as a claude-hands engine."""

from .core import (
    ADAPTERS,
    OFFICE_PROGIDS,
    OfficeElement,
    OfficeUnavailableError,
    build_office_tree,
)

__all__ = [
    "ADAPTERS",
    "OFFICE_PROGIDS",
    "OfficeElement",
    "OfficeUnavailableError",
    "build_office_tree",
]
