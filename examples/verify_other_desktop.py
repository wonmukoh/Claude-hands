"""Run claude-hands from a process that lives on another desktop, and see what it sees.

    python examples/verify_other_desktop.py            # launcher (from the normal desktop)

Sandboxed shells (Antigravity puts its processes on an ``exebox-…`` desktop of
WinSta0) start this package off the ``Default`` desktop where the user's
windows are. A thread there gets ``EnumWindows`` → access denied, UI Automation
→ an empty desktop, and the tools answer "0 windows" without an error.

The launcher creates a throwaway desktop (``claude-hands-test``) and starts this
same file on it with ``CreateProcess``'s ``lpDesktop`` — the same way the
sandbox does. The probe then reports, from that desktop:

  * the desktop each thread starts on, and what ``desktop_state()`` says
  * raw ``EnumWindows`` (return value, last error, count) on a fresh thread
  * ``claude_hands.api.windows()`` on the main thread and on a fresh thread
  * the UIA root's children, counted on the UIA worker
  * ``PrintWindow`` of a window from the normal desktop (handed over by hwnd)
  * the MCP server started from there: ``list_windows`` over stdio

Nothing is clicked or typed; no window is moved. The probe's report is JSON,
printed by the launcher. The test desktop is closed when the probe exits.
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

DESKTOP = "claude-hands-test"

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _thread_desktop_name() -> str:
    user32.GetThreadDesktop.restype = wintypes.HANDLE
    user32.GetUserObjectInformationW.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
    ]
    handle = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
    buf = ctypes.create_unicode_buffer(256)
    need = wintypes.DWORD()
    user32.GetUserObjectInformationW(handle, 2, buf, ctypes.sizeof(buf), ctypes.byref(need))
    return buf.value


def _on_fresh_thread(fn):
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            box["value"] = f"error: {type(exc).__name__}: {exc}"

    t = threading.Thread(target=run)
    t.start()
    t.join(60)
    return box.get("value", "timeout")


def _raw_enum() -> dict:
    """EnumWindows with nothing from claude_hands touching this thread first."""

    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    count = [0]

    @proc
    def cb(_h, _l):
        count[0] += 1
        return True

    ctypes.set_last_error(0)
    ok = bool(user32.EnumWindows(cb, 0))
    return {"desktop": _thread_desktop_name(), "ok": ok, "error": ctypes.get_last_error(), "count": count[0]}


def _hands_windows() -> dict:
    import claude_hands.api as ch

    found = ch.windows()
    return {"desktop": _thread_desktop_name(), "count": len(found),
            "processes": sorted({w.process for w in found})[:8]}


def _uia_children() -> int:
    from claude_hands.uia.core import get_automation, get_worker

    def count():
        _module, uia = get_automation()
        root = uia.GetRootElement()
        return int(root.FindAll(2, uia.CreateTrueCondition()).Length)  # TreeScope_Children

    return get_worker().call(count)


def _print_window(hwnd: int) -> dict:
    from claude_hands.win32.capture import capture_window

    try:
        cap = capture_window(hwnd, restore_if_minimized=False)
        return {"ok": True, "size": [cap.width, cap.height]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _mcp_list_windows() -> dict:
    server = subprocess.Popen([sys.executable, "-m", "claude_hands.server"], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    n = 0

    def call(method, params=None, notify=False):
        nonlocal n
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            n += 1
            message["id"] = n
        server.stdin.write((json.dumps(message) + "\n").encode())
        server.stdin.flush()
        if notify:
            return None
        while True:
            reply = json.loads(server.stdout.readline())
            if reply.get("id") == n:
                return reply

    try:
        init = call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "other-desktop-probe", "version": "0"}})
        call("notifications/initialized", notify=True)
        result = call("tools/call", {"name": "list_windows", "arguments": {}})["result"]
        text = "".join(c.get("text", "") for c in result.get("content", []))
        return {"version": init["result"]["serverInfo"].get("version"), "first_line": text.splitlines()[0] if text else "",
                "text_tail": text[-300:]}
    finally:
        server.stdin.close()
        server.wait(timeout=15)


def probe(out_path: str, foreign_hwnd: int) -> None:
    report: dict = {"main_thread_desktop_at_start": _thread_desktop_name()}
    report["raw_enum_fresh_thread"] = _on_fresh_thread(_raw_enum)
    report["hands_windows_main"] = _hands_windows()
    report["hands_windows_fresh_thread"] = _on_fresh_thread(_hands_windows)
    try:
        report["uia_root_children"] = _uia_children()
    except Exception as exc:  # noqa: BLE001
        report["uia_root_children"] = f"error: {type(exc).__name__}: {exc}"
    report["print_window_fresh_thread"] = _on_fresh_thread(lambda: _print_window(foreign_hwnd))
    try:
        from claude_hands.win32.desktop import desktop_state

        report["desktop_state"] = desktop_state()
    except ImportError:
        report["desktop_state"] = "(this version has no desktop module)"
    report["mcp_list_windows"] = _on_fresh_thread(_mcp_list_windows)
    Path(out_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


# -- launcher ---------------------------------------------------------------


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.c_void_p),
                ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


def _a_window_on_this_desktop() -> int:
    """Some visible, titled top-level window here, to hand to the probe."""

    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found: list[int] = []

    @proc
    def cb(h, _l):
        if user32.IsWindowVisible(h) and user32.GetWindowTextLengthW(h) > 0 and not user32.IsIconic(h):
            found.append(h)
            return False
        return True

    user32.EnumWindows(cb, 0)
    return int(found[0]) if found else 0


def launch() -> int:
    user32.CreateDesktopW.restype = wintypes.HANDLE
    user32.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD,
                                      wintypes.DWORD, ctypes.c_void_p]
    desk = user32.CreateDesktopW(DESKTOP, None, None, 0, 0x10000000, None)  # GENERIC_ALL
    if not desk:
        print(f"CreateDesktop failed: {ctypes.get_last_error()}")
        return 2
    out = Path(tempfile.mkdtemp(prefix="hands-desktop-")) / "report.json"
    foreign = _a_window_on_this_desktop()
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    si.lpDesktop = f"WinSta0\\{DESKTOP}"
    pi = PROCESS_INFORMATION()
    command = f'"{sys.executable}" "{Path(__file__).resolve()}" --probe "{out}" {foreign}'
    kernel32.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                        wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                                        ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION)]
    ok = kernel32.CreateProcessW(None, ctypes.create_unicode_buffer(command), None, None, False,
                                 0x08000000, None, None, ctypes.byref(si), ctypes.byref(pi))  # CREATE_NO_WINDOW
    if not ok:
        print(f"CreateProcess failed: {ctypes.get_last_error()}")
        user32.CloseDesktop(desk)
        return 2
    kernel32.WaitForSingleObject(pi.hProcess, 180_000)
    code = wintypes.DWORD()
    kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
    kernel32.CloseHandle(pi.hProcess)
    kernel32.CloseHandle(pi.hThread)
    time.sleep(0.5)
    user32.CloseDesktop(desk)
    print(f"probe exit code {code.value}, handed hwnd {foreign}")
    if out.exists():
        print(out.read_text(encoding="utf-8"))
        return 0
    print("probe wrote no report")
    return 1


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--probe":
        sys.stdout.reconfigure(encoding="utf-8")
        probe(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 0)
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.exit(launch())
