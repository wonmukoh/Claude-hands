"""The PowerPoint verification harness must itself be correct.

The user runs `examples/verify_pptx.py` on a real machine and trusts its
PASS/FAIL report. These tests drive that harness against a fake PowerPoint so
a bug in the harness cannot masquerade as a bug in the tool (or hide one).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from claude_hands.actions import ActionResult
from claude_hands.elements import NodeInfo
from claude_hands.win32.windows import Rect, WindowInfo

HARNESS_PATH = Path(__file__).resolve().parents[1] / "examples" / "verify_pptx.py"


@pytest.fixture
def harness():
    spec = importlib.util.spec_from_file_location("verify_pptx", HARNESS_PATH)
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve their annotations through sys.modules, so the module
    # has to be registered before it executes.
    sys.modules["verify_pptx"] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("verify_pptx", None)


class FakeCapture:
    def __init__(self, blank=False):
        self.width, self.height = 1280, 720
        self.restored_offscreen = True
        self._blank = blank

    def to_png(self, **kwargs):
        return b"\x89PNG\r\n\x1a\n" + (b"" if self._blank else b"x" * 20000)


class FakePowerPoint:
    """Behaves like a well-mannered app: everything works, nothing is stolen."""

    def __init__(self, hwnd=4242):
        self.hwnd = hwnd
        self._state = "normal"
        self.typed: list[tuple[str, str]] = []
        self.keys_sent: list[str] = []
        self.stored = ""

    @property
    def info(self):
        return WindowInfo(
            hwnd=self.hwnd, title="발표자료.pptx - PowerPoint", class_name="PPTFrameClass",
            pid=7788, process="POWERPNT.EXE", rect=Rect(0, 0, 1280, 720),
            minimized=self._state == "minimized", maximized=False,
            visible=self._state != "minimized",
        )

    def minimize(self):
        self._state = "minimized"

    def restore(self, **kwargs):
        self._state = "normal"

    def snapshot(self, **kwargs):
        return "\n".join([
            "창: 발표자료.pptx - PowerPoint | POWERPNT.EXE (pid 7788)",
            '[e3] tabitem "홈" @0,60 60x30',
            '[e7] listitem "슬라이드 1" @10,120 180x110',
            '[e9] edit "제목 개체틀" value="" @300,200 600x120',
            '[e12] button "슬라이드 쇼" @900,60 90x30',
        ])

    def find(self, query, **kwargs):
        catalogue = [
            NodeInfo(role="tabitem", name="홈", ref="e3"),
            NodeInfo(role="listitem", name="슬라이드 1", ref="e7"),
            NodeInfo(role="edit", name="제목 개체틀", ref="e9", patterns=("value",)),
            NodeInfo(role="button", name="슬라이드 쇼", ref="e12"),
        ]
        role = kwargs.get("role")
        return [
            n for n in catalogue
            if (not query or query in n.name) and (not role or n.role == role)
        ]

    def screenshot(self, **kwargs):
        return FakeCapture()

    def type(self, ref, text, **kwargs):
        self.typed.append((ref, text))
        self.stored = text
        return ActionResult(True, "type", "uia:value.SetValue", "edit 제목 개체틀")

    def text(self, ref=None, **kwargs):
        return self.stored

    def keys(self, spec, **kwargs):
        self.keys_sent.append(spec)
        if spec == "ctrl+z":
            self.stored = ""
        return ActionResult(True, "keys", "message:WM_KEYDOWN", "창")


def wire(harness, monkeypatch, app, *, cursor=(500, 500), foreground=999,
         found=None, minimized_after=True):
    """Point the harness at a fake desktop."""

    info = app.info
    monkeypatch.setattr(harness, "IS_WINDOWS", True)
    monkeypatch.setattr(harness, "windows", lambda **kw: (found if found is not None else [info]))
    monkeypatch.setattr(harness, "attach", lambda **kw: app)
    monkeypatch.setattr(harness, "cursor_pos", lambda: cursor() if callable(cursor) else cursor)
    monkeypatch.setattr(
        harness, "foreground_hwnd",
        lambda: foreground() if callable(foreground) else foreground,
    )
    monkeypatch.setattr(harness, "is_minimized", lambda hwnd: minimized_after)


def test_clean_run_passes_every_check(harness, monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    app = FakePowerPoint()
    wire(harness, monkeypatch, app)

    exit_code = harness.run(write=False)
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "FAIL" not in out
    assert "커서 이동     : 없음" in out
    assert "전경 창 변경  : 없음" in out
    assert (tmp_path / "pptx_verify_minimized.png").exists()


def test_write_mode_types_reads_back_and_undoes(harness, monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    app = FakePowerPoint()
    wire(harness, monkeypatch, app)

    exit_code = harness.run(write=True)
    out = capsys.readouterr().out

    assert exit_code == 0
    assert app.typed and app.typed[0][0] == "e9"        # 제목 개체틀에 입력
    assert "ctrl+z" in app.keys_sent                     # 되돌렸는가
    assert "입력한 글자를 다시 읽어 확인" in out


def test_a_moved_cursor_fails_the_run(harness, monkeypatch, tmp_path, capsys):
    """If anything drags the user's mouse, the report must say so loudly."""

    monkeypatch.chdir(tmp_path)
    app = FakePowerPoint()
    positions = iter([(500, 500)] + [(640, 480)] * 50)
    wire(harness, monkeypatch, app, cursor=lambda: next(positions, (640, 480)))

    exit_code = harness.run(write=False)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "커서 이동     : 있었음 (문제)" in out
    assert "커서가 (500, 500) → (640, 480) 로 이동함" in out


def test_a_stolen_foreground_fails_the_run(harness, monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    app = FakePowerPoint()
    handles = iter([999] + [4242] * 50)
    wire(harness, monkeypatch, app, foreground=lambda: next(handles, 4242))

    exit_code = harness.run(write=False)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "전경 창 변경  : 있었음 (문제)" in out


def test_missing_powerpoint_is_reported_not_crashed(harness, monkeypatch, capsys):
    wire(harness, monkeypatch, FakePowerPoint(), found=[])

    assert harness.run(write=False) == 2
    assert "PowerPoint 창을 찾지 못했습니다" in capsys.readouterr().out


def test_a_window_that_refuses_to_minimize_is_a_failure(harness, monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    app = FakePowerPoint()
    wire(harness, monkeypatch, app, minimized_after=False)

    exit_code = harness.run(write=False)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "창이 실제로 최소화됨" in out


def test_an_empty_tree_is_a_failure_not_a_pass(harness, monkeypatch, tmp_path, capsys):
    """A snapshot that reads nothing must not be reported as success."""

    monkeypatch.chdir(tmp_path)
    app = FakePowerPoint()
    app.snapshot = lambda **kw: "창: 발표자료.pptx"  # 요소 0개
    wire(harness, monkeypatch, app)

    exit_code = harness.run(write=False)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "최소화 상태에서 UI 요소를 읽음" in out


def test_a_blank_capture_is_a_failure(harness, monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    app = FakePowerPoint()
    app.screenshot = lambda **kw: FakeCapture(blank=True)
    wire(harness, monkeypatch, app)

    assert harness.run(write=False) == 1
    assert "최소화된 창의 픽셀을 얻음" in capsys.readouterr().out


def test_non_windows_refuses_early(harness, monkeypatch):
    monkeypatch.setattr(harness, "IS_WINDOWS", False)
    assert harness.run(write=False) == 2
