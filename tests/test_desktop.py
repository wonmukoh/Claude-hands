"""Moving work onto the user's desktop when started on another one (win32/desktop.py).

The decisions run against a fake Win32 layer. The real thing — a process
started with CreateProcess's lpDesktop on a throwaway desktop — is
``examples/verify_other_desktop.py`` (measured 2026-11-22: 0 windows before,
the user's windows after).
"""

from __future__ import annotations

import asyncio
import inspect
import threading

import pytest

from claude_hands.win32 import desktop


class FakeApi:
    def __init__(self, start="exebox-ABC", open_ok=True, set_ok=True):
        self.start = start
        self.open_ok = open_ok
        self.set_ok = set_ok
        self.opened: list[tuple[str, int]] = []
        self.set_calls: list[tuple[int, int]] = []
        self.moved: set[int] = set()

    def current_desktop_name(self):
        return "Default" if threading.get_ident() in self.moved else self.start

    def open_desktop(self, name, access):
        self.opened.append((name, access))
        return (777, 0) if self.open_ok else (0, 5)

    def set_thread_desktop(self, handle):
        self.set_calls.append((threading.get_ident(), handle))
        if self.set_ok:
            self.moved.add(threading.get_ident())
            return True, 0
        return False, 170


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    desktop._reset_for_tests()
    api = FakeApi()
    monkeypatch.setattr(desktop, "api", api)
    yield api
    desktop._reset_for_tests()


def in_thread(fn):
    out = []
    t = threading.Thread(target=lambda: out.append(fn()))
    t.start()
    t.join(10)
    return out[0]


def test_a_thread_already_on_default_is_left_alone(fake):
    fake.start = "Default"
    assert in_thread(desktop.ensure_default_desktop) is True
    assert fake.opened == [] and fake.set_calls == []
    state = desktop.desktop_state()
    assert state == {"first_desktop": "Default"}
    assert desktop.failure_notice() == ""


def test_each_thread_moves_once_and_the_handle_is_opened_once(fake):
    def twice():
        return desktop.ensure_default_desktop() and desktop.ensure_default_desktop()

    assert in_thread(twice) is True
    assert in_thread(twice) is True
    # one OpenDesktop for the process, read + enumerate only (not GENERIC_ALL)
    assert fake.opened == [("default", 0x41)]
    # one SetThreadDesktop per thread, from that thread, with that handle
    assert len(fake.set_calls) == 2
    assert len({tid for tid, _ in fake.set_calls}) == 2
    assert all(handle == 777 for _, handle in fake.set_calls)
    state = desktop.desktop_state()
    assert state["first_desktop"] == "exebox-ABC"
    assert state["moved"] == "옮김 (스레드 2개)"
    assert "failure" not in state


def test_an_open_failure_is_kept_and_said(fake):
    fake.open_ok = False
    assert in_thread(desktop.ensure_default_desktop) is False
    state = desktop.desktop_state()
    assert state["moved"] == "실패"
    assert "OpenDesktop 오류 5" in state["failure"]
    assert "exebox-ABC" in state["failure"]
    assert "OpenDesktop 오류 5" in desktop.failure_notice()
    assert "OpenDesktop 오류 5" in desktop.describe_desktop()


def test_a_move_failure_is_kept_and_said(fake):
    fake.set_ok = False
    assert in_thread(desktop.ensure_default_desktop) is False
    assert "SetThreadDesktop 오류 170" in desktop.desktop_state()["failure"]


def test_off_windows_nothing_is_called(monkeypatch):
    desktop._reset_for_tests()
    api = FakeApi()
    monkeypatch.setattr(desktop, "api", api)
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    try:
        assert in_thread(desktop.ensure_default_desktop) is True
        assert api.opened == [] and api.set_calls == []
    finally:
        desktop._reset_for_tests()


def test_require_windows_moves_the_calling_thread(fake, monkeypatch):
    from claude_hands.win32 import defs

    monkeypatch.setattr(defs, "IS_WINDOWS", True)
    in_thread(defs.require_windows)
    assert len(fake.set_calls) == 1


def test_the_server_runs_every_tool_call_on_a_moved_thread(fake, monkeypatch):
    server = pytest.importorskip("claude_hands.server")
    monkeypatch.setattr(server, "IS_WINDOWS", True)
    seen = []

    def work():
        seen.append(threading.get_ident())
        return fake.current_desktop_name()

    assert asyncio.run(server._run(work)) == "Default"
    assert fake.set_calls and fake.set_calls[0][0] == seen[0]


def test_an_empty_list_says_why_when_the_move_failed(fake, monkeypatch):
    server = pytest.importorskip("claude_hands.server")
    fake.open_ok = False
    monkeypatch.setattr(server, "IS_WINDOWS", True)
    monkeypatch.setattr(server, "_list_windows", lambda **_kw: [])
    text = asyncio.run(server.list_windows())
    assert text.startswith("조건에 맞는 창이 없습니다.")
    assert "OpenDesktop 오류 5" in text
    assert "다른 데스크톱" in text


def test_the_uia_workers_move_before_they_join_com():
    from claude_hands.uia.core import UiaWorker

    source = inspect.getsource(UiaWorker._run)
    assert source.index("ensure_default_desktop()") < source.index("CoInitializeEx")


def test_doctor_shows_the_desktop(fake, monkeypatch, capsys):
    from claude_hands import cli

    fake.open_ok = False
    if not cli.IS_WINDOWS:
        pytest.skip("doctor stops early off Windows")
    code = cli.cmd_doctor(None)
    out = capsys.readouterr().out
    assert "데스크톱" in out and "OpenDesktop 오류 5" in out
    assert code == 1


def test_the_real_api_reads_this_threads_desktop():
    if desktop.sys.platform != "win32":
        pytest.skip("Windows only")
    name = in_thread(desktop.api.current_desktop_name)
    assert isinstance(name, str) and name
