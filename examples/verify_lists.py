"""Live check that choosing in classic Win32 lists leaves the foreground alone.

    py examples/verify_lists.py                 # visible, then minimised
    py examples/verify_lists.py --mode minimized

UI Automation's Select, Expand and Invoke on ListBox, ComboBox, SysTreeView32
and SysListView32 items hand the keyboard to the list's process, even when its
window is minimised (lesson 38). This runs every list action against
``list_target.py`` and checks three things per case:

- the call came back ok and named the message route it took;
- the control told its parent, as it would for a person (the log file);
- the foreground did not go to the target's process.

Each case gets a freshly started target, so a case that does take the
foreground cannot leave the next one starting with the target already in
front. A just-started process may take the foreground once on its own
(lesson 31); that is why the target opens without activation and far off
screen, and why a case is only failed when the foreground lands on the
target's own process. A foreground that moved to some other window means a
person was using the PC: the case is marked "?" rather than passed.
"""

from __future__ import annotations

import argparse
import ctypes
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_hands import attach  # noqa: E402
from claude_hands.win32.defs import force_utf8_output  # noqa: E402

HERE = Path(__file__).resolve().parent
user32 = ctypes.windll.user32
WM_CLOSE = 0x0010

# Notifications in the log: WM_COMMAND codes for the list box (id 101) and the
# combo (id 102), WM_NOTIFY codes for the tree (103) and the list view (104).
LBN_SELCHANGE = "WM_COMMAND id=101 code=1"
CBN_DROPDOWN = "WM_COMMAND id=102 code=7"
CBN_SELCHANGE = "WM_COMMAND id=102 code=1"
TVN_SELCHANGED = "WM_NOTIFY id=103 code=-451"
TVN_ITEMEXPANDED = "WM_NOTIFY id=103 code=-455"
LVN_ITEMCHANGED = "WM_NOTIFY id=104 code=-101"


def _find(app, name, role):
    app.snapshot()
    found = [n for n in app.find(name, role=role) if not name or n.name == name]
    if not found:
        raise LookupError(f"{role} 「{name}」 을(를) 못 찾음")
    return found[0].ref


def _subitem(app, ref):
    # A collapsed branch's children are not in the tree until it is opened.
    app.expand(ref)
    return app.select(_find(app, "감", "treeitem"))


# (label, item name, role, what to do with its ref, the notification it must produce)
CASES = [
    ("목록 상자 고르기", "배", "listitem", lambda app, r: app.select(r), LBN_SELCHANGE),
    ("목록 상자 누르기", "포도", "listitem", lambda app, r: app.click(r), LBN_SELCHANGE),
    ("콤보 펼치기", "", "combobox", lambda app, r: app.expand(r), CBN_DROPDOWN),
    ("콤보에 입력", "", "combobox", lambda app, r: app.type(r, "셋"), CBN_SELCHANGE),
    ("트리 펼치기", "과일", "treeitem", lambda app, r: app.expand(r), TVN_ITEMEXPANDED),
    ("트리 접기", "과일", "treeitem",
     lambda app, r: (app.expand(r), app.expand(r, collapse=True))[-1], TVN_ITEMEXPANDED),
    ("늦게 채우는 가지 펼치기", "늦게", "treeitem", lambda app, r: app.expand(r), TVN_ITEMEXPANDED),
    ("트리 하위 항목 고르기", "과일", "treeitem", _subitem, TVN_SELCHANGED),
    ("트리 누르기", "채소", "treeitem", lambda app, r: app.click(r), TVN_SELCHANGED),
    ("리스트뷰 고르기", "나래", "listitem", lambda app, r: app.select(r), LVN_ITEMCHANGED),
    ("리스트뷰 누르기", "다솜", "listitem", lambda app, r: app.click(r), LVN_ITEMCHANGED),
]


def _pid(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _start(log: Path):
    proc = subprocess.Popen([sys.executable, str(HERE / "list_target.py"), str(log)],
                            stdout=subprocess.PIPE, text=True)
    hwnd = int(proc.stdout.readline())
    time.sleep(0.5)
    return proc, hwnd


def _lines(log: Path):
    try:
        return log.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def run_case(index: int, mode: str, log: Path):
    proc, hwnd = _start(log)
    try:
        app = attach(hwnd=hwnd, engine="uia")
        if mode == "minimized":
            app.minimize()
            time.sleep(0.5)
        label, name, role, act, expected = CASES[index]
        seen = len(_lines(log))
        fg0 = user32.GetForegroundWindow()
        try:
            result = act(app, _find(app, name, role))
            own, ok = f"{result.strategy} {result.detail or ''}".strip(), bool(result.ok)
        except Exception as exc:  # noqa: BLE001 - a failed case is a result
            own, ok = f"{type(exc).__name__}: {exc}", False
        time.sleep(0.6)
        fg1 = user32.GetForegroundWindow()
        notes = _lines(log)[seen:]
        app.detach()

        if not ok:
            verdict = "FAIL"
        elif fg1 and _pid(fg1) == proc.pid:
            verdict, own = "FAIL", own + " — 전경이 대상으로 넘어감"
        elif expected not in notes:
            verdict, own = "FAIL", own + f" — 알림 없음(받은 것: {notes})"
        elif fg1 != fg0:
            verdict, own = "?", own + " — 전경이 다른 창으로 바뀜(사람이 쓰는 중)"
        else:
            verdict = "PASS"
        return label, verdict, own
    finally:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main():
    force_utf8_output()
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["visible", "minimized", "both"], default="both")
    args = parser.parse_args()
    modes = ["visible", "minimized"] if args.mode == "both" else [args.mode]

    tally = {"PASS": 0, "FAIL": 0, "?": 0}
    with tempfile.TemporaryDirectory() as tmp:
        for mode in modes:
            for index in range(len(CASES)):
                log = Path(tmp) / f"{mode}-{index}.log"
                label, verdict, detail = run_case(index, mode, log)
                tally[verdict] += 1
                print(f"[{mode:9}] {verdict:4} {label:16} {detail}")
    print(f"\nPASS {tally['PASS']} · FAIL {tally['FAIL']} · ? {tally['?']}")
    return 1 if tally["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
