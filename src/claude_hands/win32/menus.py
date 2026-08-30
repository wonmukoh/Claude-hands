"""Reading and invoking a window's real menu bar.

Sending ``ctrl+s`` as window messages does not reliably trigger a command:
accelerator tables are matched against the *physical* keyboard state via
``GetKeyState``, which ``PostMessage`` never touches. The dependable way to
run a menu command in the background is to read the window's ``HMENU``, find
the item by name, and post the ``WM_COMMAND`` the menu itself would post.

That works while the window is minimised, because a menu command is just a
message the application handles.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
from typing import Iterator, Optional

from .controls import strip_accelerator
from .defs import ClaudeHandsError, require_windows

MF_BYPOSITION = 0x00000400
MF_SEPARATOR = 0x00000800
MF_GRAYED = 0x00000001
MF_DISABLED = 0x00000002
MF_CHECKED = 0x00000008
WM_COMMAND = 0x0111
MENU_ID_NONE = 0xFFFFFFFF


class MenuNotFoundError(ClaudeHandsError):
    """Raised when a menu path cannot be resolved."""


@dataclass
class MenuItem:
    """One entry of a window's menu bar."""

    label: str
    command_id: Optional[int]
    position: int
    enabled: bool = True
    checked: bool = False
    separator: bool = False
    children: list["MenuItem"] = field(default_factory=list)

    @property
    def is_submenu(self) -> bool:
        return bool(self.children) or self.command_id is None

    def walk(self, prefix: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], "MenuItem"]]:
        path = prefix + (self.label,)
        yield path, self
        for child in self.children:
            yield from child.walk(path)

    def describe(self) -> str:
        bits = [self.label or "(구분선)"]
        if not self.enabled:
            bits.append("(disabled)")
        if self.checked:
            bits.append("(checked)")
        if self.command_id is not None:
            bits.append(f"id={self.command_id}")
        return " ".join(bits)


def _menu_label(raw: str) -> str:
    """Turn ``"&Save\\tCtrl+S"`` into ``"Save"`` — what the user reads."""

    label = raw.split("\t", 1)[0]
    return strip_accelerator(label).strip()


def _declare(user32) -> None:
    from .defs import wintypes

    user32.GetMenu.argtypes = [wintypes.HWND]
    user32.GetMenu.restype = ctypes.c_void_p
    user32.GetSubMenu.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.GetSubMenu.restype = ctypes.c_void_p
    user32.GetMenuItemCount.argtypes = [ctypes.c_void_p]
    user32.GetMenuItemCount.restype = ctypes.c_int
    user32.GetMenuItemID.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.GetMenuItemID.restype = ctypes.c_uint
    user32.GetMenuStringW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, wintypes.LPWSTR, ctypes.c_int, ctypes.c_uint
    ]
    user32.GetMenuStringW.restype = ctypes.c_int
    user32.GetMenuState.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
    user32.GetMenuState.restype = ctypes.c_uint
    user32.IsMenu.argtypes = [ctypes.c_void_p]
    user32.IsMenu.restype = ctypes.c_bool


def read_menu(hwnd: int, *, max_depth: int = 6) -> list[MenuItem]:
    """Read a window's menu bar into a tree.

    Returns an empty list when the window has no menu, or when its menu is not
    readable from this process. Windows keeps menus in a shared handle table so
    another process can inspect them, which is what makes this work; some
    compatibility layers keep them process-local, and there ``IsMenu`` rejects
    the handle. Callers treat an empty result as "no menu route available" and
    fall back to the UI tree.
    """

    require_windows()
    from .defs import user32

    _declare(user32)
    handle = user32.GetMenu(hwnd)
    if not handle or not user32.IsMenu(handle):
        return []

    def read(menu, depth: int) -> list[MenuItem]:
        items: list[MenuItem] = []
        count = user32.GetMenuItemCount(menu)
        for position in range(max(0, count)):
            state = user32.GetMenuState(menu, position, MF_BYPOSITION)
            separator = bool(state & MF_SEPARATOR)
            length = user32.GetMenuStringW(menu, position, None, 0, MF_BYPOSITION)
            label = ""
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetMenuStringW(menu, position, buf, length + 1, MF_BYPOSITION)
                label = _menu_label(buf.value)
            submenu = user32.GetSubMenu(menu, position)
            command_id = None
            children: list[MenuItem] = []
            if submenu and depth < max_depth:
                children = read(submenu, depth + 1)
            elif not submenu:
                raw_id = user32.GetMenuItemID(menu, position)
                command_id = None if raw_id == MENU_ID_NONE else int(raw_id)
            items.append(
                MenuItem(
                    label=label,
                    command_id=command_id,
                    position=position,
                    enabled=not (state & (MF_GRAYED | MF_DISABLED)),
                    checked=bool(state & MF_CHECKED),
                    separator=separator,
                    children=children,
                )
            )
        return items

    return read(handle, 0)


def find_menu_item(items: list[MenuItem], steps: list[str]) -> MenuItem:
    """Walk a name path like ``["Search", "Find..."]`` through a menu tree."""

    current = items
    found: Optional[MenuItem] = None
    walked: list[str] = []
    for step in steps:
        needle = step.strip().lower()
        match = None
        for item in current:
            if item.separator:
                continue
            label = item.label.lower()
            if label == needle or label.rstrip(".") == needle.rstrip("."):
                match = item
                break
        if match is None:
            for item in current:
                if not item.separator and needle in item.label.lower():
                    match = item
                    break
        if match is None:
            available = ", ".join(i.label for i in current if i.label)[:200]
            raise MenuNotFoundError(
                f"메뉴 {step!r} 을(를) 찾지 못했습니다. "
                f"지나온 경로: {' > '.join(walked) or '(최상위)'}. "
                f"이 단계에서 가능한 항목: {available}"
            )
        walked.append(match.label)
        found = match
        current = match.children
    if found is None:
        raise MenuNotFoundError("메뉴 경로가 비어 있습니다.")
    return found


def invoke_menu_item(hwnd: int, item: MenuItem) -> int:
    """Post the WM_COMMAND a menu click would post. Works while minimised."""

    require_windows()
    from .defs import user32

    if item.command_id is None:
        raise MenuNotFoundError(
            f"{item.label!r} 은(는) 하위 메뉴라 실행할 명령이 없습니다. "
            "끝 항목까지 경로를 지정하세요."
        )
    if not item.enabled:
        raise MenuNotFoundError(f"{item.label!r} 메뉴는 현재 비활성 상태입니다.")
    if not user32.PostMessageW(hwnd, WM_COMMAND, item.command_id & 0xFFFF, 0):
        raise ClaudeHandsError(f"WM_COMMAND 전송 실패 (hwnd={hwnd}, id={item.command_id}).")
    return item.command_id


def render_menu(items: list[MenuItem], indent: str = "  ", depth: int = 0) -> str:
    lines: list[str] = []
    for item in items:
        if item.separator:
            continue
        lines.append(indent * depth + item.describe())
        if item.children:
            lines.append(render_menu(item.children, indent, depth + 1))
    return "\n".join(line for line in lines if line)
