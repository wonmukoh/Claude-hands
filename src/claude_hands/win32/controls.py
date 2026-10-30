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

# Roles whose window text carries keyboard-accelerator markers. Windows draws
# "&Save" as "Save" with the S underlined, so the ampersand must never reach a
# caller: nobody searches for "Add appli&cation". UI Automation strips it for
# us; the message backend has to do it itself.
ACCELERATOR_ROLES = {"button", "checkbox", "radiobutton", "group", "text",
                     "menuitem", "tabitem", "toolbar", "statusbar"}
TOGGLE_STYLES = {BS_CHECKBOX, BS_AUTOCHECKBOX, BS_3STATE, BS_AUTO3STATE}
RADIO_STYLES = {BS_RADIOBUTTON, BS_AUTORADIOBUTTON}


def _class_name(hwnd: int) -> str:
    from .defs import user32

    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def strip_accelerator(text: str) -> str:
    """Turn a control's raw label into what the user actually sees.

    ``&`` marks the next character as the accelerator and is not drawn; ``&&``
    is a literal ampersand.
    """

    if "&" not in text:
        return text
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "&":
            if index + 1 < len(text) and text[index + 1] == "&":
                out.append("&")
                index += 2
                continue
            index += 1  # drop the marker, keep the letter after it
            continue
        out.append(char)
        index += 1
    return "".join(out)


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


def is_button_class(hwnd: int) -> bool:
    """Whether ``hwnd`` is a classic Win32 button (push, check or radio)."""

    return _class_name(hwnd).lower() == "button"


WM_COMMAND = 0x0111
BM_SETCHECK = 0x00F1
BN_CLICKED = 0
GA_ROOT = 2


def _button_style(hwnd: int) -> int:
    from .defs import user32

    return user32.GetWindowLongW(hwnd, GWL_STYLE) & BS_TYPEMASK


def _ensure_pressable(hwnd: int) -> None:
    """Refuse what a person could not press either.

    The notification below reaches the parent whether or not the button would
    let itself be clicked: a real click on a disabled button, or on a dialog
    that a modal box has disabled, does nothing at all.
    """

    from .defs import user32

    root = user32.GetAncestor(hwnd, GA_ROOT) or hwnd
    if not user32.IsWindowEnabled(hwnd) or not user32.IsWindowEnabled(root):
        raise ClaudeHandsError(
            "비활성 버튼이거나 창이 다른 대화상자에 막혀 있어 누르지 않았습니다. "
            "떠 있는 대화상자를 먼저 처리하세요."
        )


def _notify_clicked(hwnd: int) -> None:
    from .defs import user32

    parent = user32.GetParent(hwnd)
    if not parent:
        raise ClaudeHandsError("버튼의 부모 창이 없어 누를 수 없습니다.")
    control_id = user32.GetDlgCtrlID(hwnd) & 0xFFFF
    _send(parent, WM_COMMAND, (BN_CLICKED << 16) | control_id, hwnd)


def press_button(hwnd: int) -> None:
    """Press a Win32 button by telling its parent it was clicked.

    BM_CLICK was used first and is not enough. It replays a mouse press inside
    the button, and the button answers a press by taking the keyboard focus —
    which activates its window. Measured on target_app while visible and
    inactive: three runs of BM_CLICK on its checkbox, three times the window
    came to the foreground. WM_COMMAND/BN_CLICKED is the message the button
    itself sends its parent when clicked, so the program does what a click
    does, and nothing gets focus.
    """

    _ensure_pressable(hwnd)
    style = _button_style(hwnd)
    if style in TOGGLE_STYLES or style in RADIO_STYLES:
        toggle_button(hwnd)
        return
    _notify_clicked(hwnd)


def toggle_button(hwnd: int) -> None:
    """Change a checkbox or radio button as a click would, without focusing it.

    An *auto* button changes its own state when clicked and then notifies its
    parent, so both are done here. A plain BS_CHECKBOX or BS_RADIOBUTTON
    leaves its state to the program, which sets it on the notification.
    """

    _ensure_pressable(hwnd)
    style = _button_style(hwnd)
    state = button_check_state(hwnd)
    if style == BS_AUTOCHECKBOX:
        _send(hwnd, BM_SETCHECK, 0 if state else 1)
    elif style == BS_AUTO3STATE:
        _send(hwnd, BM_SETCHECK, (state + 1) % 3)
    elif style == BS_AUTORADIOBUTTON:
        _check_radio(hwnd)
    _notify_clicked(hwnd)


def _check_radio(hwnd: int) -> None:
    # What clicking an auto radio button does: it goes on, and the other auto
    # radio buttons of its group go off.
    from .defs import user32

    _send(hwnd, BM_SETCHECK, 1)
    parent = user32.GetParent(hwnd)
    seen = {hwnd}
    sibling = user32.GetNextDlgGroupItem(parent, hwnd, False)
    while sibling and sibling not in seen:
        seen.add(sibling)
        if is_button_class(sibling) and _button_style(sibling) == BS_AUTORADIOBUTTON:
            _send(sibling, BM_SETCHECK, 0)
        sibling = user32.GetNextDlgGroupItem(parent, sibling, False)


def button_check_state(hwnd: int) -> int:
    return _send(hwnd, BM_GETCHECK)


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
        press_button(self.hwnd)


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
        # The state and the notification together, as a click produces them —
        # BM_SETCHECK alone would not tell the program (see press_button).
        toggle_button(self.hwnd)


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

    def is_alive(self) -> bool:
        # The memoised properties outlive the window, and GetWindowLong on a
        # dead handle reads as "not disabled" — only IsWindow can say.
        from .defs import user32

        return bool(user32.IsWindow(self.hwnd))

    @property
    def role(self) -> str:
        if self._role is None:
            self._role = role_for(self.hwnd)
        return self._role

    @property
    def name(self) -> str:
        if self._name is None:
            raw = control_text(self.hwnd) if self.role not in EDIT_ROLES else ""
            self._name = strip_accelerator(raw) if self.role in ACCELERATOR_ROLES else raw
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
