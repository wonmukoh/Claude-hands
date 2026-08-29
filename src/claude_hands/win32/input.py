"""Background input: window messages posted straight at a control's HWND.

No ``SendInput``, no cursor movement, no focus stealing. The target window
receives exactly the messages it would have received from a real click, so it
reacts while sitting minimised behind everything else.

The key-string parser at the bottom is pure Python and unit-tested on any OS.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .defs import (
    MK_CONTROL,
    MK_LBUTTON,
    MK_MBUTTON,
    MK_RBUTTON,
    MK_SHIFT,
    WHEEL_DELTA,
    WM_CHAR,
    WM_KEYDOWN,
    WM_KEYUP,
    WM_LBUTTONDBLCLK,
    WM_LBUTTONDOWN,
    WM_LBUTTONUP,
    WM_MBUTTONDOWN,
    WM_MBUTTONUP,
    WM_MOUSEMOVE,
    WM_MOUSEWHEEL,
    WM_RBUTTONDBLCLK,
    WM_RBUTTONDOWN,
    WM_RBUTTONUP,
    WM_SETTEXT,
    WM_SYSKEYDOWN,
    WM_SYSKEYUP,
    ClaudeHandsError,
    make_lparam,
    require_windows,
)
from .windows import client_to_screen, deepest_child_at, screen_to_client

# --------------------------------------------------------------------------
# Virtual key codes
# --------------------------------------------------------------------------

VK_CODES: dict[str, int] = {
    "backspace": 0x08,
    "tab": 0x09,
    "clear": 0x0C,
    "enter": 0x0D,
    "return": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "menu": 0x12,
    "pause": 0x13,
    "capslock": 0x14,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "select": 0x29,
    "print": 0x2A,
    "printscreen": 0x2C,
    "insert": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    "help": 0x2F,
    "win": 0x5B,
    "lwin": 0x5B,
    "rwin": 0x5C,
    "apps": 0x5D,
    "numpad0": 0x60,
    "numpad1": 0x61,
    "numpad2": 0x62,
    "numpad3": 0x63,
    "numpad4": 0x64,
    "numpad5": 0x65,
    "numpad6": 0x66,
    "numpad7": 0x67,
    "numpad8": 0x68,
    "numpad9": 0x69,
    "multiply": 0x6A,
    "add": 0x6B,
    "subtract": 0x6D,
    "decimal": 0x6E,
    "divide": 0x6F,
    "numlock": 0x90,
    "scrolllock": 0x91,
    ";": 0xBA,
    "=": 0xBB,
    ",": 0xBC,
    "-": 0xBD,
    ".": 0xBE,
    "/": 0xBF,
    "+": 0xBB,
    "`": 0xC0,
    "[": 0xDB,
    "\\": 0xDC,
    "]": 0xDD,
    "'": 0xDE,
}
for _i in range(1, 25):
    VK_CODES[f"f{_i}"] = 0x6F + _i
for _c in "abcdefghijklmnopqrstuvwxyz":
    VK_CODES[_c] = ord(_c.upper())
for _d in "0123456789":
    VK_CODES[_d] = ord(_d)

MODIFIER_ALIASES = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "ctl": "ctrl",
    "shift": "shift",
    "alt": "alt",
    "win": "win",
    "cmd": "win",
    "meta": "win",
    "super": "win",
}


@dataclass(frozen=True)
class KeyStroke:
    """One chord: zero or more modifiers plus exactly one main key."""

    key: str
    vk: int
    modifiers: tuple[str, ...] = ()

    def describe(self) -> str:
        return "+".join([*self.modifiers, self.key])


class KeyParseError(ClaudeHandsError):
    """Raised for a key string we cannot turn into virtual key codes."""


_SPLIT_CHORDS = re.compile(r"[\s,]+")


def parse_keys(spec: str) -> list[KeyStroke]:
    """Parse ``"ctrl+s"`` / ``"alt+f4"`` / ``"ctrl+shift+n enter"`` into chords.

    Chords are separated by whitespace or commas; parts of a chord by ``+``.
    A bare ``+`` is accepted as a literal key (``"ctrl++"``).
    """

    if not spec or not spec.strip():
        raise KeyParseError("빈 키 문자열입니다.")

    strokes: list[KeyStroke] = []
    for chunk in _SPLIT_CHORDS.split(spec.strip()):
        if not chunk:
            continue
        parts = _split_chord(chunk)
        modifiers: list[str] = []
        main: str | None = None
        last_index = len(parts) - 1
        for index, part in enumerate(parts):
            lowered = part.lower()
            if lowered in MODIFIER_ALIASES and index < last_index:
                canonical = MODIFIER_ALIASES[lowered]
                if canonical not in modifiers:
                    modifiers.append(canonical)
            else:
                main = part
        if main is None:
            raise KeyParseError(f"{chunk!r} 에 실제 키가 없습니다 (수식키만 있음).")
        vk = VK_CODES.get(main.lower())
        if vk is None:
            if len(main) == 1:
                vk = ord(main.upper())
            else:
                raise KeyParseError(
                    f"알 수 없는 키 이름입니다: {main!r}. "
                    "예: enter, esc, tab, f5, ctrl+s, alt+f4"
                )
        strokes.append(KeyStroke(main.lower(), vk, tuple(modifiers)))
    if not strokes:
        raise KeyParseError(f"{spec!r} 에서 키를 찾지 못했습니다.")
    return strokes


def _split_chord(chunk: str) -> list[str]:
    """Split on ``+`` while letting a trailing ``+`` be a literal plus key."""

    if chunk == "+":
        return ["+"]
    parts = chunk.split("+")
    out: list[str] = []
    for index, part in enumerate(parts):
        if part == "":
            # "ctrl++" -> ['ctrl', '', ''] : the empty pair means literal '+'
            if index == len(parts) - 1 and out:
                out.append("+")
            continue
        out.append(part)
    return out


def modifier_mk_flags(modifiers: tuple[str, ...] | list[str]) -> int:
    flags = 0
    if "ctrl" in modifiers:
        flags |= MK_CONTROL
    if "shift" in modifiers:
        flags |= MK_SHIFT
    return flags


# --------------------------------------------------------------------------
# Message posting
# --------------------------------------------------------------------------


def _post(hwnd: int, message: int, wparam: int, lparam: int) -> None:
    from .defs import user32

    if not user32.PostMessageW(hwnd, message, wparam, lparam):
        raise ClaudeHandsError(
            f"PostMessage(msg=0x{message:04X}) 실패 — 창이 닫혔거나 권한이 부족합니다 "
            "(대상이 관리자 권한이면 이 도구도 관리자로 실행해야 합니다)."
        )


BUTTON_MESSAGES = {
    "left": (WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK, MK_LBUTTON),
    "right": (WM_RBUTTONDOWN, WM_RBUTTONUP, WM_RBUTTONDBLCLK, MK_RBUTTON),
    "middle": (WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MBUTTONDOWN, MK_MBUTTON),
}


def click_at_screen_point(
    top_hwnd: int,
    screen_x: int,
    screen_y: int,
    *,
    button: str = "left",
    double: bool = False,
    modifiers: tuple[str, ...] = (),
    target_hwnd: int | None = None,
) -> int:
    """Post a click to the control at a screen point inside ``top_hwnd``.

    Returns the HWND that actually received the messages.
    """

    require_windows()
    if button not in BUTTON_MESSAGES:
        raise ClaudeHandsError(f"지원하지 않는 버튼: {button!r} (left/right/middle)")

    target = target_hwnd or deepest_child_at(top_hwnd, screen_x, screen_y)
    client_x, client_y = screen_to_client(target, screen_x, screen_y)
    lparam = make_lparam(client_x, client_y)
    down, up, dbl, mk = BUTTON_MESSAGES[button]
    flags = mk | modifier_mk_flags(modifiers)

    _post(target, WM_MOUSEMOVE, modifier_mk_flags(modifiers), lparam)
    _post(target, down, flags, lparam)
    _post(target, up, modifier_mk_flags(modifiers), lparam)
    if double:
        time.sleep(0.02)
        _post(target, dbl, flags, lparam)
        _post(target, up, modifier_mk_flags(modifiers), lparam)
    return target


def scroll_at_screen_point(
    top_hwnd: int,
    screen_x: int,
    screen_y: int,
    *,
    clicks: int = 3,
    horizontal: bool = False,
    target_hwnd: int | None = None,
) -> int:
    """Post wheel messages. Positive ``clicks`` scrolls up / left-to-right."""

    require_windows()
    from .defs import WM_MOUSEHWHEEL

    target = target_hwnd or deepest_child_at(top_hwnd, screen_x, screen_y)
    # WM_MOUSEWHEEL uses *screen* coordinates in lParam, unlike button messages.
    lparam = make_lparam(screen_x, screen_y)
    message = WM_MOUSEHWHEEL if horizontal else WM_MOUSEWHEEL
    delta = WHEEL_DELTA * clicks
    wparam = (delta & 0xFFFF) << 16
    _post(target, message, wparam, lparam)
    return target


def send_key(hwnd: int, stroke: KeyStroke, *, hold_seconds: float = 0.01) -> None:
    """Post one chord to ``hwnd`` (modifiers are held down around the key)."""

    require_windows()
    alt_held = "alt" in stroke.modifiers
    down_msg = WM_SYSKEYDOWN if alt_held else WM_KEYDOWN
    up_msg = WM_SYSKEYUP if alt_held else WM_KEYUP

    for modifier in stroke.modifiers:
        _post(hwnd, WM_KEYDOWN, VK_CODES[modifier], 0)
    _post(hwnd, down_msg, stroke.vk, 1 | (0x20000000 if alt_held else 0))
    time.sleep(hold_seconds)
    _post(hwnd, up_msg, stroke.vk, 0xC0000001 | (0x20000000 if alt_held else 0))
    for modifier in reversed(stroke.modifiers):
        _post(hwnd, WM_KEYUP, VK_CODES[modifier], 0xC0000000)


def send_keys(hwnd: int, spec: str, *, delay: float = 0.02) -> list[str]:
    strokes = parse_keys(spec)
    for stroke in strokes:
        send_key(hwnd, stroke)
        time.sleep(delay)
    return [s.describe() for s in strokes]


def send_text_chars(hwnd: int, text: str, *, delay: float = 0.004) -> None:
    """Type text as WM_CHAR messages — works with IME-free plain input."""

    require_windows()
    for char in text:
        if char == "\n":
            _post(hwnd, WM_KEYDOWN, VK_CODES["enter"], 1)
            _post(hwnd, WM_KEYUP, VK_CODES["enter"], 0xC0000001)
        else:
            _post(hwnd, WM_CHAR, ord(char), 1)
        if delay:
            time.sleep(delay)


def set_window_text(hwnd: int, text: str, *, timeout_ms: int = 1500) -> bool:
    """Replace a control's text via WM_SETTEXT (classic Edit/Static controls)."""

    require_windows()
    import ctypes

    from .defs import SMTO_ABORTIFHUNG, user32

    result = ctypes.c_size_t(0)
    buffer = ctypes.create_unicode_buffer(text)
    sent = user32.SendMessageTimeoutW(
        hwnd,
        WM_SETTEXT,
        0,
        ctypes.cast(buffer, ctypes.c_void_p).value,
        SMTO_ABORTIFHUNG,
        timeout_ms,
        ctypes.byref(result),
    )
    return bool(sent)


def screen_point_in_element(rect, offset_x: float = 0.5, offset_y: float = 0.5) -> tuple[int, int]:
    """Pick a screen point inside a rect (centre by default)."""

    x = int(rect.left + rect.width * offset_x)
    y = int(rect.top + rect.height * offset_y)
    return (min(x, rect.right - 1), min(y, rect.bottom - 1))


__all__ = [
    "KeyStroke",
    "KeyParseError",
    "VK_CODES",
    "parse_keys",
    "click_at_screen_point",
    "scroll_at_screen_point",
    "send_key",
    "send_keys",
    "send_text_chars",
    "set_window_text",
    "screen_point_in_element",
    "client_to_screen",
]
