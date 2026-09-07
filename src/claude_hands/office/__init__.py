"""Document-model automation as a claude-hands engine.

PowerPoint, Word and Excel live in ``core``; 한글 needs a different way in and
lives in ``hwp``. It registers itself here rather than in ``core`` because it
imports from there.
"""

from .core import (
    ADAPTERS,
    OFFICE_PROGIDS,
    OfficeElement,
    OfficeUnavailableError,
    build_office_tree,
)
from .hwp import HwpAdapter

ADAPTERS.setdefault("hwp.exe", HwpAdapter)

__all__ = [
    "ADAPTERS",
    "OFFICE_PROGIDS",
    "HwpAdapter",
    "OfficeElement",
    "OfficeUnavailableError",
    "build_office_tree",
]
