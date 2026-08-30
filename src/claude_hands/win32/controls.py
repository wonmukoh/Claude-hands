"""A Win32-message backend that stands in when UI Automation is unavailable.

UIA is the better engine and stays the default, but it is not always there: a
locked-down machine where comtypes cannot write its generated modules, an
install whose type library will not load, or a compatibility layer that ships
only a provider-side type library. Without a fallback the whole tool dies in
those places.

This backend builds the same :class:`~claude_hands.elements.NodeInfo` tree out
of child window handles, and exposes the *same pattern protocol* the action
layer already speaks — ``invoke`` on a push button becomes ``BM_CLICK``,
``value`` on an edit becomes ``WM_SETTEXT``/``WM_GETTEXT``, ``toggle`` on a
checkbox becomes ``BM_GETCHECK``/``BM_SETCHECK``. So one action chain drives
both engines, and both report honestly which one ran.

It sees only real HWND-backed controls, so it is blind to the owner-drawn
interiors of modern apps. On classic Win32 software it is enough to work with.
"""

from __future__ import annotations

import ctypes
from typing import Any, Optional

from .defs import (
    BM_CLICK,
    BM_GETCHECK,
    BS_3STATE,
    BS_AUTO3STATE,
    BS_AUTOCHECKBOX,
    BS_AUTORADIOBUTTON,
    BS_CHECKBOX,
    BS_GROUPBOX,
    BS_RADIOBUTTON,
    BS_TYPEMASK,
    GWL_STYLE,
    SMTO_ABORTIFHUNG,
    WM_GETTEXT,
    WM_GETTEXTLENGTH,
    WS_DISABLED,
    ClaudeHandsError,
    require_windows,
)
from .input import set_window_text
from .windows import Rect, enum_child_windows, window_rect

# Window class → the role a model should see. Prefixes are matched too, which
# covers the versioned classes (RichEdit20W, WindowsForms10.BUTTON.app.…).
CLASS_ROLES: tuple[tuple[str, str], ...] = (
    ("button", "button"),
    ("edit", "edit"),
    ("richedit", "edit"),
    ("richtext", "edit"),
    ("static", "text"),
    ("combobox", "combobox"),
    ("combolbox", "list"),
    ("listbox", "list"),
    ("syslistview32", "list"),
    ("systreeview32", "tree"),
    ("systabcontrol32", "tab"),
    ("msctls_progress32", "progressbar"),
    ("msctls_statusbar32", "statusbar"),
    ("msctls_trackbar32", "slider"),
    ("msctls_updown32", "spinner"),
    ("toolbarwindow32", "toolbar"),
    ("rebarwindow32", "toolbar"),
    ("scrollbar", "scrollbar"),
    ("#32770", "window"),
    ("tooltips_class32", "tooltip"),
    ("notepad", "window"),
)

EDIT_ROLES = {"edit"}
TOGGLE_STYLES = {BS_CHECKBOX, BS_AUTOCHECKBOX, BS_3STATE, BS_AUTO3STATE}
RADIO_STYLES = {BS_RADIOBUTTON, BS_AUTORADIOBUTTON}


def _class_name(hwnd: int) -> str:
    from .defs import user32

    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def control_text(hwnd: int, *, limit: int = 32768, timeout_ms: int = 1500) -> str:
    """Read a control's text with WM_GETTEXT, without hanging on a busy app."""

    require_windows()
    from .defs import user32

    length = ctypes.c_size_t(0)
    if not user32.SendMessageTimeoutW(
        hwnd, WM_GETTEXTLENGTH, 0, 0, SMTO_ABORTIFHUNG, timeout_ms, ctypes.byref(length)
    ):
        return ""
    size = min(int(length.value), limit)
    if size <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(size + 1)
    result = ctypes.c_size_t(0)
    user32.SendMessageTimeoutW(
        hwnd,
        WM_GETTEXT,
        size + 1,
        ctypes.cast(buf, ctypes.c_void_p).value,
        SMTO_ABORTIFHUNG,
        timeout_ms,
        ctypes.byref(result),
    )
    return buf.value


def _send(hwnd: int, message: int, wparam: int = 0, lparam: int = 0, timeout_ms: int = 1500) -> int:
    from .defs import user32

    result = ctypes.c_size_t(0)
    user32.SendMessageTimeoutW(
        hwnd, message, wparam, lparam, SMTO_ABORTIFHUNG, timeout_ms, ctypes.byref(result)
    )
    return int(result.value)


def role_for(hwnd: int) -> str:
    """Classify a control by window class, refining buttons by their style."""

    name = _class_name(hwnd).lower()
    role = "custom"
    for prefix, mapped in CLASS_ROLES:
        if name == prefix or name.startswith(prefix) or prefix in name:
            role = mapped
            break
    if role == "button":
        from .defs import user32

        style = user32.GetWindowLongW(hwnd, GWL_STYLE) & BS_TYPEMASK
        if style in TOGGLE_STYLES:
            return "checkbox"
        if style in RADIO_STYLES:
            return "radiobutton"
        if style == BS_GROUPBOX:
            return "group"
    return role


# --------------------------------------------------------------------------
# Message-backed pattern objects
# --------------------------------------------------------------------------


class _InvokePattern:
    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd

    def Invoke(self) -> None:
        _send(self.hwnd, BM_CLICK)


class _ValuePattern:
    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd

    @property
    def CurrentValue(self) -> str:
        return control_text(self.hwnd)

    @property
    def CurrentIsReadOnly(self) -> bool:
        from .defs import user32

        # ES_READONLY is 0x0800 on edit controls.
        return bool(user32.GetWindowLongW(self.hwnd, GWL_STYLE) & 0x0800)

    def SetValue(self, value: str) -> None:
        if not set_window_text(self.hwnd, value):
            raise ClaudeHandsError(f"WM_SETTEXT 가 hwnd={self.hwnd} 에서 실패했습니다.")


class _TogglePattern:
    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd

    @property
    def CurrentToggleState(self) -> int:
        return _send(self.hwnd, BM_GETCHECK)

    def Toggle(self) -> None:
        # BM_SETCHECK moves the box but does not tell the app; BM_CLICK does
        # both, which is what a user pressing it would produce.
        _send(self.hwnd, BM_CLICK)


class Win32Element:
    """Duck-types :class:`~claude_hands.uia.core.UiaElement` over an HWND."""

    engine = "win32"
    __slots__ = ("hwnd", "_role", "_rect", "_name", "_enabled")

    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self._role: Optional[str] = None
        self._rect: Optional[Rect] = None
        self._name: Optional[str] = None
        self._enabled: Optional[bool] = None

    # -- properties -------------------------------------------------------
    @property
    def com(self) -> "Win32Element":
        return self

    def SetFocus(self) -> None:
        from .defs import user32

        if not user32.SetFocus(self.hwnd):
            raise ClaudeHandsError(
                f"hwnd={self.hwnd} 에 포커스를 줄 수 없습니다 "
                "(다른 프로세스의 컨트롤은 스레드 연결이 필요합니다)."
            )

    def refresh(self) -> "Win32Element":
        self._rect = self._name = self._enabled = None
        return self

    @property
    def role(self) -> str:
        if self._role is None:
            self._role = role_for(self.hwnd)
        return self._role

    @property
    def name(self) -> str:
        if self._name is None:
            self._name = control_text(self.hwnd) if self.role != "edit" else ""
        return self._name

    @property
    def enabled(self) -> bool:
        if self._enabled is None:
            from .defs import user32

            self._enabled = not (user32.GetWindowLongW(self.hwnd, GWL_STYLE) & WS_DISABLED)
        return self._enabled

    @property
    def rect(self) -> Optional[Rect]:
        if self._rect is None:
            try:
                self._rect = window_rect(self.hwnd, frame_bounds=False)
            except ClaudeHandsError:
                return None
        return self._rect

    def value(self) -> str:
        return control_text(self.hwnd) if self.role in EDIT_ROLES else ""

    # -- pattern protocol -------------------------------------------------
    def pattern(self, key: str) -> Any:
        role = self.role
        if key == "invoke" and role in {"button", "radiobutton"}:
            return _InvokePattern(self.hwnd)
        if key == "value" and role in EDIT_ROLES:
            return _ValuePattern(self.hwnd)
        if key == "toggle" and role in {"checkbox", "radiobutton"}:
            return _TogglePattern(self.hwnd)
        return None

    def available_patterns(self) -> tuple[str, ...]:
        return tuple(k for k in ("invoke", "value", "toggle") if self.pattern(k) is not None)

    def to_node_info(self, depth: int = 0):
        from ..elements import NodeInfo

        patterns = self.available_patterns()
        toggle_state = None
        if "toggle" in patterns:
            toggle_state = {0: "off", 1: "on", 2: "indeterminate"}.get(
                _send(self.hwnd, BM_GETCHECK)
            )
        return NodeInfo(
            role=self.role,
            name=self.name,
            value=self.value(),
            automation_id=str(self.hwnd),
            class_name=_class_name(self.hwnd),
            rect=self.rect,
            enabled=self.enabled,
            offscreen=False,
            focusable=self.role in EDIT_ROLES or "invoke" in patterns,
            hwnd=self.hwnd,
            runtime_id=(self.hwnd,),
            patterns=patterns,
            toggle_state=toggle_state,
            depth=depth,
        )


# --------------------------------------------------------------------------
# Tree building
# --------------------------------------------------------------------------


def build_win32_tree(hwnd: int, *, max_depth: int = 8, max_nodes: int = 800):
    """Build a NodeInfo tree from ``hwnd``'s child windows.

    Returns ``(tree, element_index)`` mirroring
    :func:`claude_hands.uia.core.build_tree`, so sessions can use either engine.
    """

    require_windows()
    from ..elements import NodeInfo
    from .defs import user32

    element_index: dict[int, Win32Element] = {}
    counter = {"nodes": 0}

    def descend(handle: int, depth: int) -> NodeInfo:
        element = Win32Element(handle)
        node = element.to_node_info(depth)
        element_index[id(node)] = element
        counter["nodes"] += 1
        if depth >= max_depth or counter["nodes"] >= max_nodes:
            return node
        for child in enum_child_windows(handle, lambda h: user32.GetParent(h) == handle):
            if counter["nodes"] >= max_nodes:
                node.truncated_children += 1
                break
            node.children.append(descend(child, depth + 1))
        return node

    root = descend(hwnd, 0)
    root.role = "window"
    from .windows import _window_text

    root.name = _window_text(hwnd)
    return root, element_index
