"""A web page inside an IE WebBrowser control, to verify the mshtml engine against.

K-에듀파인's WXSClient is a dialog around an IE WebBrowser control
(``Internet Explorer_Server``) and its forms are old-style pages: checkboxes
labelled by the text beside them, a link whose handler opens a modal dialog,
a same-origin iframe. ``web_target/outer.html`` reproduces those shapes, so
verification never needs a logged-in business application.

The control is hosted through ATL's ``AtlAxWin`` window class, the same IE
control in the same default IE7 document mode an MFC application gets.

The window is shown without activation, so launching it does not take the
keyboard from whoever is typing.

    py examples/web_target.py
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

TITLE = "claude-hands 웹 검증 대상"
CLASS_NAME = "ClaudeHandsWebTarget"
PAGE = Path(__file__).resolve().parent / "web_target" / "outer.html"

WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
SW_SHOWNOACTIVATE = 4
WM_DESTROY = 0x0002
WM_SIZE = 0x0005
SIZE_MINIMIZED = 1

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


user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.MoveWindow.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.BOOL]

_browser = [None]


def _on_message(hwnd, message, wparam, lparam):
    # A minimised window is told its size is 0x0. Passing that on would leave
    # the page collapsed, which is not what a real host does.
    if message == WM_SIZE and _browser[0] and wparam != SIZE_MINIMIZED:
        user32.MoveWindow(_browser[0], 0, 0, lparam & 0xFFFF, (lparam >> 16) & 0xFFFF, True)
        return 0
    if message == WM_DESTROY:
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, message, wparam, lparam)


def main() -> int:
    ctypes.oledll.ole32.OleInitialize(None)
    atl = ctypes.WinDLL("atl")
    if not atl.AtlAxWinInit():
        print("AtlAxWinInit 실패", file=sys.stderr)
        return 1

    hinstance = ctypes.windll.kernel32.GetModuleHandleW(None)
    proc = WNDPROC(_on_message)
    cls = WNDCLASSW()
    cls.lpfnWndProc = proc
    cls.hInstance = hinstance
    cls.lpszClassName = CLASS_NAME
    cls.hbrBackground = 6  # COLOR_WINDOW + 1
    if not user32.RegisterClassW(ctypes.byref(cls)):
        print("RegisterClass 실패", file=sys.stderr)
        return 1

    frame = user32.CreateWindowExW(
        0, CLASS_NAME, TITLE, WS_OVERLAPPEDWINDOW,
        40, 40, 760, 640, None, None, hinstance, None,
    )
    # AtlAxWin reads its window text as what to host: a URL becomes a
    # WebBrowser control navigated there.
    _browser[0] = user32.CreateWindowExW(
        0, "AtlAxWin", PAGE.as_uri(), WS_CHILD | WS_VISIBLE,
        0, 0, 744, 600, frame, None, hinstance, None,
    )
    user32.ShowWindow(frame, SW_SHOWNOACTIVATE)
    print(f"hwnd={frame}", flush=True)

    message = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(message))
        user32.DispatchMessageW(ctypes.byref(message))
    return 0


if __name__ == "__main__":
    sys.exit(main())
