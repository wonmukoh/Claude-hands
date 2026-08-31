"""Which top-level windows count as windows a person parked.

`list_windows` used to keep anything IsIconic reported, on the theory that a
minimised window stops being IsWindowVisible. Measured on Windows 11 that is
backwards: PowerPoint and Explorer sitting in the taskbar are visible *and*
iconic, while helper windows nobody ever sees (OZ*MsgWnd, "DWM Notification
Window", tray agents) are iconic and *not* visible. Trusting IsIconic let
every one of those into the list.
"""

import pytest

from claude_hands.win32 import windows as W


class FakeUser32:
    """Only the call `list_windows` makes directly."""

    def __init__(self, ex_styles=None):
        self._ex_styles = ex_styles or {}

    def GetWindowLongW(self, hwnd, _index):
        return self._ex_styles.get(hwnd, 0)


def info(hwnd, title, *, visible, minimized, cloaked=False, process="app.exe"):
    return W.WindowInfo(
        hwnd=hwnd,
        title=title,
        class_name="Cls",
        pid=hwnd,
        process=process,
        rect=W.Rect(0, 0, 800, 600),
        minimized=minimized,
        maximized=False,
        visible=visible,
        cloaked=cloaked,
    )


# The exact shapes measured on a live desktop.
PARKED_POWERPOINT = info(398308, "감염병예방노래.pptx - PowerPoint", visible=True, minimized=True)
PARKED_EXPLORER = info(134476, "학교자율시간 - 파일 탐색기", visible=True, minimized=True)
OPEN_EDITOR = info(2494828, "설문지.hwpx - 한글", visible=True, minimized=False)
TRAY_AGENT = info(263364, "ISign+ WA", visible=False, minimized=True)
OZ_HELPER = info(65802, "OZADMsgWnd", visible=False, minimized=True)
DWM_HELPER = info(131144, "DWM Notification Window", visible=False, minimized=True)
DDE_SERVER = info(332784, "DDE Server Window", visible=False, minimized=False)
UNTITLED = info(265180, "", visible=True, minimized=False)

ALL = [
    PARKED_POWERPOINT,
    PARKED_EXPLORER,
    OPEN_EDITOR,
    TRAY_AGENT,
    OZ_HELPER,
    DWM_HELPER,
    DDE_SERVER,
    UNTITLED,
]


@pytest.fixture
def desktop(monkeypatch):
    """A fixed desktop, so the assertions below are about the filter only."""

    by_hwnd = {i.hwnd: i for i in ALL}
    monkeypatch.setattr(W, "require_windows", lambda: None)
    monkeypatch.setattr(W, "iter_top_level_hwnds", lambda: iter(list(by_hwnd)))
    monkeypatch.setattr(W, "describe_window", lambda hwnd: by_hwnd[hwnd])
    monkeypatch.setattr("claude_hands.win32.defs.user32", FakeUser32())
    return by_hwnd


def titles(**kwargs):
    return {w.title for w in W.list_windows(**kwargs)}


def test_windows_a_person_parked_survive(desktop):
    kept = titles()
    assert PARKED_POWERPOINT.title in kept
    assert PARKED_EXPLORER.title in kept
    assert OPEN_EDITOR.title in kept


def test_helper_windows_are_dropped_despite_claiming_to_be_minimised(desktop):
    kept = titles()
    for helper in (TRAY_AGENT, OZ_HELPER, DWM_HELPER):
        assert helper.minimized, "these do report IsIconic — that is the trap"
        assert helper.title not in kept


def test_invisible_and_untitled_windows_are_dropped(desktop):
    kept = titles()
    assert DDE_SERVER.title not in kept
    assert "" not in kept


def test_include_hidden_returns_everything(desktop):
    assert titles(include_hidden=True) == {w.title for w in ALL}


def test_tool_windows_are_dropped_unless_asked_for(monkeypatch, desktop):
    from claude_hands.win32.defs import WS_EX_TOOLWINDOW

    monkeypatch.setattr(
        "claude_hands.win32.defs.user32",
        FakeUser32({OPEN_EDITOR.hwnd: WS_EX_TOOLWINDOW}),
    )
    assert OPEN_EDITOR.title not in titles()
    assert OPEN_EDITOR.title in titles(include_tool_windows=True)
