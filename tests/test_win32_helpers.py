from claude_hands.win32.defs import make_lparam
from claude_hands.win32.input import modifier_mk_flags, screen_point_in_element
from claude_hands.win32.windows import Rect


def test_make_lparam_packs_two_shorts():
    assert make_lparam(0, 0) == 0
    assert make_lparam(10, 20) == (20 << 16) | 10


def test_make_lparam_handles_negative_coordinates():
    # A control scrolled above its container has a negative client y.
    packed = make_lparam(-1, -1)
    assert packed == 0xFFFFFFFF
    assert packed & 0xFFFF == 0xFFFF


def test_rect_geometry():
    rect = Rect(100, 200, 180, 228)
    assert (rect.width, rect.height) == (80, 28)
    assert rect.center == (140, 214)
    assert rect.contains(140, 214)
    assert not rect.contains(180, 228)  # right/bottom are exclusive


def test_screen_point_stays_inside_the_rect():
    rect = Rect(0, 0, 10, 10)
    assert screen_point_in_element(rect) == (5, 5)
    assert screen_point_in_element(rect, 1.0, 1.0) == (9, 9)


def test_modifier_flags():
    assert modifier_mk_flags(()) == 0
    assert modifier_mk_flags(("ctrl",)) == 0x0008
    assert modifier_mk_flags(("ctrl", "shift")) == 0x000C


def test_a_title_is_read_without_sending_the_window_a_message(monkeypatch):
    # GetWindowText sends WM_GETTEXT to this process's own windows, and the web
    # engine's worker owns some: list_windows hung for good on one of them.
    from claude_hands.win32 import windows

    class User32:
        def GetWindowTextLengthW(self, hwnd):
            raise AssertionError("sends WM_GETTEXTLENGTH to this process's windows")

        def GetWindowTextW(self, hwnd, buf, size):
            raise AssertionError("sends WM_GETTEXT to this process's windows")

        def InternalGetWindowText(self, hwnd, buf, size):
            buf.value = "웹 페이지 메시지"
            return len(buf.value)

    monkeypatch.setattr("claude_hands.win32.defs.user32", User32())
    assert windows._window_text(5) == "웹 페이지 메시지"
