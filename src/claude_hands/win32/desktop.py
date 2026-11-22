"""Work on the user's desktop even when this process was started on another one.

Sandboxed shells (Antigravity among them) start their processes on a separate
desktop of the same window station — ``exebox-…`` instead of ``Default``. A
thread on that desktop sees none of the user's windows: ``EnumWindows`` finds
nothing, UI Automation's root has no children, ``GetWindowRect`` on a handle
from ``Default`` fails. Every tool then answered "no windows" without an error
(measured 2026-11-22 on WMBOOK by starting the server with ``CreateProcess``'s
``lpDesktop``; see ``examples/verify_other_desktop.py``).

``SetThreadDesktop`` moves **only the calling thread**, so every thread that
calls into Win32 or UIA must do it at its start: the MCP server's worker
threads (``server._run``), the UIA workers (``uia.core.UiaWorker``), and —
through :func:`require_windows` — whatever thread a script or the CLI uses.
Each thread checks once and remembers; a thread already on ``Default`` is left
alone, so the normal case costs one name lookup per thread.

- Access: ``DESKTOP_READOBJECTS | DESKTOP_ENUMERATE`` (0x41). kakao-pc measured
  on the same kind of test desktop that 0, 0x1, 0x40, 0x41 and GENERIC_ALL all
  allow enumeration, messages, PrintWindow and UIA; this asks for no more.
- One handle per process, never closed: a desktop may not be closed while a
  thread is attached to it, and the process exit releases it.
- ``SetThreadDesktop`` fails (ERROR_BUSY, 170) once the thread owns a window or
  a hook, so the call has to come first on a thread — before COM creates its
  hidden window on an STA thread.
- Failure is not swallowed: :func:`desktop_state` keeps the reason, ``doctor``
  prints it and ``list_windows`` says it instead of a bare "no windows".

The idea and the access mask follow kakao-pc 0.5.2 (``desktop.py``, 846b658);
this is claude-hands' own copy so neither package depends on the other.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from typing import Optional

TARGET = "default"
UOI_NAME = 2
DESKTOP_READOBJECTS = 0x0001
DESKTOP_ENUMERATE = 0x0040
ACCESS = DESKTOP_READOBJECTS | DESKTOP_ENUMERATE


class _Api:
    """The Win32 calls, swappable in tests."""

    def __init__(self) -> None:
        self._user32 = None
        self._kernel32 = None

    def _load(self):  # pragma: no cover - Windows only
        if self._user32 is None:
            from ctypes import wintypes

            # Private instances, so these signatures cannot clash with defs.user32's.
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            user32.GetThreadDesktop.restype = wintypes.HANDLE
            user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
            user32.SetThreadDesktop.restype = wintypes.BOOL
            user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
            user32.OpenDesktopW.restype = wintypes.HANDLE
            user32.OpenDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            user32.GetUserObjectInformationW.restype = wintypes.BOOL
            user32.GetUserObjectInformationW.argtypes = [
                wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            ]
            kernel32.GetCurrentThreadId.restype = wintypes.DWORD
            self._user32, self._kernel32 = user32, kernel32
        return self._user32, self._kernel32

    def current_desktop_name(self) -> str:  # pragma: no cover - Windows only
        from ctypes import wintypes

        user32, kernel32 = self._load()
        handle = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
        if not handle:
            return ""
        buf = ctypes.create_unicode_buffer(256)
        need = wintypes.DWORD()
        if not user32.GetUserObjectInformationW(handle, UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(need)):
            return ""
        return buf.value

    def open_desktop(self, name: str, access: int) -> tuple[int, int]:  # pragma: no cover - Windows only
        user32, _ = self._load()
        handle = user32.OpenDesktopW(name, 0, False, access)
        return int(handle or 0), ctypes.get_last_error()

    def set_thread_desktop(self, handle: int) -> tuple[bool, int]:  # pragma: no cover - Windows only
        user32, _ = self._load()
        ok = bool(user32.SetThreadDesktop(handle))
        return ok, ctypes.get_last_error()


api = _Api()
_local = threading.local()
_lock = threading.Lock()
_handle: Optional[int] = None  # one per process, never closed (see above)
_state: dict = {"first_desktop": None, "moved_threads": 0, "failure": None}


def _open_default() -> tuple[int, int]:
    global _handle
    with _lock:
        if _handle:
            return _handle, 0
        handle, error = api.open_desktop(TARGET, ACCESS)
        if handle:
            _handle = handle
        return handle, error


def ensure_default_desktop() -> bool:
    """Put the calling thread on the ``Default`` desktop. True when it is there.

    Checks once per thread and remembers the answer. A thread already on
    ``Default`` is not touched. Off Windows there is nothing to do.
    """

    done = getattr(_local, "result", None)
    if done is not None:
        return done
    if sys.platform != "win32":
        _local.result = True
        return True
    name = api.current_desktop_name()
    with _lock:
        if _state["first_desktop"] is None:
            _state["first_desktop"] = name or "알 수 없음"
    if name.lower() == TARGET:
        _local.result = True
        return True
    handle, error = _open_default()
    if not handle:
        _state["failure"] = (
            f"기본 데스크톱(Default)을 열지 못했습니다(OpenDesktop 오류 {error}). "
            f"이 프로세스의 데스크톱: {name or '알 수 없음'}"
        )
        _local.result = False
        return False
    ok, error = api.set_thread_desktop(handle)
    if not ok:
        # 170 (ERROR_BUSY): this thread already owns a window or a hook here.
        _state["failure"] = (
            f"작업 스레드를 기본 데스크톱(Default)으로 옮기지 못했습니다(SetThreadDesktop 오류 {error}). "
            f"이 프로세스의 데스크톱: {name or '알 수 없음'}"
        )
        _local.result = False
        return False
    with _lock:
        _state["moved_threads"] += 1
    _local.result = True
    return True


def desktop_state() -> dict:
    """What happened so far, for ``doctor`` and for tool replies."""

    first = _state["first_desktop"]
    out: dict = {"first_desktop": first or "확인 전"}
    if first and first.lower() != TARGET:
        out["moved"] = "실패" if _state["failure"] else f"옮김 (스레드 {_state['moved_threads']}개)"
    if _state["failure"]:
        out["failure"] = _state["failure"]
    return out


def describe_desktop() -> str:
    """One line for ``doctor``."""

    state = desktop_state()
    line = state["first_desktop"]
    if "moved" in state:
        line += f" → Default {state['moved']}"
    if "failure" in state:
        line += f"\n                    {state['failure']}"
    return line


def failure_notice() -> str:
    """A sentence to append to an empty or short result; empty when all is well."""

    failure = _state["failure"]
    if not failure:
        return ""
    return (
        f"\n\n주의: {failure}. 이 프로그램이 사용자 창이 없는 다른 데스크톱(샌드박스)에서 떠서, "
        "창을 못 보고 있을 수 있습니다."
    )


def _reset_for_tests() -> None:
    global _handle
    _handle = None
    _state.update({"first_desktop": None, "moved_threads": 0, "failure": None})
    if hasattr(_local, "result"):
        del _local.result
