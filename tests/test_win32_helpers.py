import pytest

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


# --------------------------------------------------------------------------
# Pressing Win32 buttons without giving them focus
# --------------------------------------------------------------------------


class FakeUser32:
    """Just enough of user32 for the button helpers, recording messages."""

    def __init__(self, styles, enabled=True, root_enabled=True, group=()):
        self.styles = styles
        self.enabled = enabled
        self.root_enabled = root_enabled
        self.group = list(group)

    def GetWindowLongW(self, hwnd, index):
        return self.styles.get(hwnd, 0)

    def IsWindowEnabled(self, hwnd):
        return self.root_enabled if hwnd == 1 else self.enabled

    def GetAncestor(self, hwnd, flag):
        return 1

    def GetParent(self, hwnd):
        return 1

    def GetDlgCtrlID(self, hwnd):
        return hwnd

    def GetNextDlgGroupItem(self, parent, hwnd, previous):
        if not self.group:
            return 0
        i = self.group.index(hwnd)
        return self.group[(i + 1) % len(self.group)]


@pytest.fixture
def buttons(monkeypatch):
    from claude_hands.win32 import controls, defs

    sent = []
    checks = {}

    def send(hwnd, message, wparam=0, lparam=0, timeout_ms=1500):
        sent.append((hwnd, message, wparam, lparam))
        if message == controls.BM_SETCHECK:
            checks[hwnd] = wparam
        if message == controls.BM_GETCHECK:
            return checks.get(hwnd, 0)
        return 0

    monkeypatch.setattr(controls, "_send", send)
    monkeypatch.setattr(controls, "is_button_class", lambda hwnd: True)

    def install(**kw):
        fake = FakeUser32(**kw)
        monkeypatch.setattr(defs, "user32", fake, raising=False)
        return fake

    return controls, sent, checks, install


def test_a_push_button_is_pressed_by_telling_its_parent(buttons):
    # BM_CLICK replays a mouse press, the button takes focus, and its window
    # came to the front (measured three times on target_app).
    controls, sent, _checks, install = buttons
    install(styles={10: 0})  # BS_PUSHBUTTON

    controls.press_button(10)

    assert sent == [(1, controls.WM_COMMAND, 10, 10)]
    assert not any(message == controls.BM_CLICK for _h, message, _w, _l in sent)


def test_a_checkbox_changes_state_and_then_tells_its_parent(buttons):
    from claude_hands.win32.defs import BS_AUTOCHECKBOX

    controls, sent, checks, install = buttons
    install(styles={20: BS_AUTOCHECKBOX})

    controls.toggle_button(20)

    assert checks[20] == 1
    assert sent[-1] == (1, controls.WM_COMMAND, 20, 20)


def test_choosing_a_radio_button_clears_the_rest_of_its_group(buttons):
    from claude_hands.win32.defs import BS_AUTORADIOBUTTON

    controls, _sent, checks, install = buttons
    install(styles={30: BS_AUTORADIOBUTTON, 31: BS_AUTORADIOBUTTON}, group=(30, 31))
    checks[31] = 1

    controls.toggle_button(30)

    assert checks[30] == 1 and checks[31] == 0


@pytest.mark.parametrize("enabled, root_enabled", [(False, True), (True, False)])
def test_a_button_a_person_could_not_press_is_not_pressed(buttons, enabled, root_enabled):
    # The notification would reach the parent regardless; a click would not.
    controls, sent, _checks, install = buttons
    install(styles={10: 0}, enabled=enabled, root_enabled=root_enabled)

    with pytest.raises(controls.ClaudeHandsError):
        controls.press_button(10)
    assert sent == []
