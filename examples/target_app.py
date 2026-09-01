"""A window to verify against, so verification never needs someone's real file.

Live verification has to type into something, toggle something, and press
something that visibly reacts. Pointing that at whatever the person happens to
have open is how you end up writing a marker string into their document —
Windows 11 Notepad restores its last session, so even launching it fresh can
put a real file under the cursor.

This is that target instead: one window, an edit box, a checkbox, and a button
that opens a modal dialog. Plain Win32 controls with real HWNDs, so the
`win32` engine sees them, and UI Automation maps them too, so the `uia` engine
sees the same window. Nothing here is worth keeping, so a verification run can
write whatever it likes.

    python examples/target_app.py
    python examples/verify_live.py --title "claude-hands 검증 대상" --engine win32

Closing the window (or the run's own close) ends the process.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_hands.win32.defs import force_utf8_output  # noqa: E402

TITLE = "claude-hands 검증 대상"
CLASS_NAME = "ClaudeHandsVerifyTarget"

WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_BORDER = 0x00800000
WS_TABSTOP = 0x00010000
ES_AUTOHSCROLL = 0x0080
BS_AUTOCHECKBOX = 0x0003
BS_PUSHBUTTON = 0x0000
SW_SHOWNORMAL = 1
WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
MB_OK = 0x0000

ID_EDIT = 1001
ID_CHECK = 1002
ID_BUTTON = 1003

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]

_keep_alive: list = []


def _child(parent, class_name, text, style, x, y, width, height, control_id, hinstance):
    hwnd = user32.CreateWindowExW(
        0,
        class_name,
        text,
        WS_CHILD | WS_VISIBLE | style,
        x,
        y,
        width,
        height,
        parent,
        control_id,
        hinstance,
        None,
    )
    if not hwnd:
        raise ctypes.WinError(ctypes.get_last_error())
    return hwnd


def _on_message(hwnd, message, wparam, lparam):
    if message == WM_DESTROY:
        user32.PostQuitMessage(0)
        return 0
    if message == WM_COMMAND and (wparam & 0xFFFF) == ID_BUTTON:
        # A separate top-level window is what proves the click landed: the
        # caller can find it by title instead of trusting a return value.
        user32.MessageBoxW(hwnd, "버튼이 눌렸습니다.", "확인 대화상자", MB_OK)
        return 0
    return user32.DefWindowProcW(hwnd, message, wparam, lparam)


def build() -> int:
    hinstance = kernel32.GetModuleHandleW(None)

    proc = WNDPROC(_on_message)
    _keep_alive.append(proc)  # a collected WNDPROC crashes the message loop

    cls = WNDCLASSW()
    cls.lpfnWndProc = proc
    cls.hInstance = hinstance
    cls.lpszClassName = CLASS_NAME
    cls.hbrBackground = 16  # COLOR_BTNFACE + 1
    cls.hCursor = user32.LoadCursorW(None, ctypes.c_wchar_p(32512))  # IDC_ARROW
    if not user32.RegisterClassW(ctypes.byref(cls)):
        raise ctypes.WinError(ctypes.get_last_error())

    hwnd = user32.CreateWindowExW(
        0, CLASS_NAME, TITLE, WS_OVERLAPPEDWINDOW, 200, 200, 460, 260, None, None, hinstance, None
    )
    if not hwnd:
        raise ctypes.WinError(ctypes.get_last_error())

    _child(hwnd, "STATIC", "입력란:", 0, 20, 22, 70, 22, None, hinstance)
    _child(
        hwnd,
        "EDIT",
        "",
        WS_BORDER | WS_TABSTOP | ES_AUTOHSCROLL,
        95,
        20,
        320,
        26,
        ID_EDIT,
        hinstance,
    )
    _child(
        hwnd,
        "BUTTON",
        "알림 받기",
        BS_AUTOCHECKBOX | WS_TABSTOP,
        95,
        70,
        200,
        26,
        ID_CHECK,
        hinstance,
    )
    _child(
        hwnd,
        "BUTTON",
        "대화상자 열기",
        BS_PUSHBUTTON | WS_TABSTOP,
        95,
        120,
        180,
        34,
        ID_BUTTON,
        hinstance,
    )
    return hwnd


def main() -> int:
    force_utf8_output()
    hwnd = build()
    user32.ShowWindow(hwnd, SW_SHOWNORMAL)
    user32.UpdateWindow(hwnd)
    print(f"검증 대상 창을 띄웠습니다. hwnd={hwnd}  제목={TITLE!r}")
    print("검증기를 다른 터미널에서 실행하세요:")
    print(f'  python examples/verify_live.py --title "{TITLE}" --engine win32')

    message = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(message))
        user32.DispatchMessageW(ctypes.byref(message))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
