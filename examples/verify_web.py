"""Live check of the mshtml engine against ``web_target.py``.

    py examples/web_target.py          # once, in the background
    py examples/verify_web.py

Every step re-reads the cursor and the foreground window just before it runs
and fails if either changed during it (as verify_live.py does), so a human
moving the mouse between steps is not blamed on the tool.

It also presses the link whose handler opens an alert and checks the call
came back at once: a click delivered over COM would wait for a person to
close that alert.
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_hands import actions  # noqa: E402
from claude_hands.session import SessionManager  # noqa: E402
from claude_hands.win32.defs import force_utf8_output  # noqa: E402
from claude_hands.win32.windows import list_windows  # noqa: E402

TITLE = "claude-hands 웹 검증 대상"
user32 = ctypes.windll.user32


def state():
    point = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    return (point.x, point.y), user32.GetForegroundWindow()


class Report:
    def __init__(self):
        self.results: list[tuple[bool, str]] = []

    def step(self, label, func):
        before = state()
        try:
            detail = func()
            ok = True
        except Exception as exc:  # noqa: BLE001 - a failed step is a result
            detail, ok = f"{type(exc).__name__}: {exc}", False
        after = state()
        if after[0] != before[0]:
            ok, detail = False, f"커서가 움직임 {before[0]} → {after[0]}"
        elif after[1] != before[1]:
            ok, detail = False, f"전경 창이 바뀜 {before[1]} → {after[1]}"
        self.results.append((ok, label))
        print(f"[{'PASS' if ok else 'FAIL'}] {label}: {detail}")


def main() -> int:
    force_utf8_output()
    targets = [w for w in list_windows(title_contains=TITLE)]
    if not targets:
        print("web_target.py 를 먼저 띄우세요.")
        return 2
    session = SessionManager().attach(hwnd=targets[0].hwnd)
    report = Report()

    def ref(name, role):
        return session.find(name, role=role, limit=1, refresh=False)[0][1].ref

    def snapshot():
        session.capture_tree()
        assert session.active_engine == "mshtml", session.active_engine
        boxes = [n for n in session.tree.walk() if n.role == "checkbox"]
        assert boxes and all(n.toggle_state in {"on", "off"} for n in boxes)
        return f"엔진 {session.active_engine}, 요소 {len(session.refs)}개, 체크칸 {len(boxes)}개"

    def expect(result, wanted):
        session.capture_tree()
        return result.describe().replace("\n", " ") + f" | 되읽음 {wanted()}"

    report.step("스냅샷", snapshot)
    report.step("체크칸 끄기", lambda: expect(
        actions.toggle(session, ref("첨부파일", "checkbox"), to=False),
        lambda: session.refs[ref("첨부파일", "checkbox")].toggle_state))
    report.step("라디오 고르기", lambda: expect(
        actions.toggle(session, ref("PDF", "radiobutton"), to=True),
        lambda: session.refs[ref("PDF", "radiobutton")].toggle_state))
    # K-에듀파인's 기안 window hides the real input and shows a <label for>.
    report.step("숨긴 체크칸(라벨로 보임)", lambda: expect(
        actions.toggle(session, ref("긴급", "checkbox"), to=True),
        lambda: session.refs[ref("긴급", "checkbox")].toggle_state))
    report.step("숨긴 라디오(라벨로 보임)", lambda: expect(
        actions.toggle(session, ref("비공개", "radiobutton"), to=True),
        lambda: session.refs[ref("비공개", "radiobutton")].toggle_state))
    report.step("입력", lambda: actions.type_text(session, ref("메모", "edit"), "검증 메모").describe())
    report.step("목록", lambda: actions.type_text(session, ref("구분", "combobox"), "비공개").describe())
    report.step("iframe 안 입력", lambda: actions.type_text(session, ref("안쪽", "edit"), "안쪽 검증").describe())

    def modal_click():
        started = time.perf_counter()
        actions.click(session, ref("확인", "hyperlink"))
        elapsed = time.perf_counter() - started
        assert elapsed < 2, f"{elapsed:.1f}초 — 모달 창에 막혔습니다"
        return f"{elapsed * 1000:.0f}ms 만에 돌아옴"

    report.step("알림창을 띄우는 링크", modal_click)

    def close_alert():
        for _ in range(20):
            alerts = list_windows(title_contains="웹 페이지 메시지")
            if alerts:
                break
            time.sleep(0.1)
        assert alerts, "알림창이 뜨지 않았습니다"
        manager = SessionManager()
        alert = manager.attach(hwnd=alerts[0].hwnd)
        alert.capture_tree()
        return actions.click(alert, alert.find("확인", role="button", limit=1)[0][1].ref).describe()

    report.step("알림창 닫기", close_alert)

    def page_log():
        time.sleep(0.3)
        text = actions.get_text(session)
        for hidden in ("스크립트글자", "숨긴글자", "화면밖글자", "잘린글자"):
            assert hidden not in text, f"읽은 글에 '{hidden}' 가 섞였습니다"
        for word in ("첨부 클릭", "긴급 클릭", "비공개 클릭", "메모 바뀜", "구분 바뀜", "확인 눌림"):
            assert word in text, f"페이지 처리기가 '{word}' 를 남기지 않았습니다"
        return "처리기가 모두 돌았고, 스크립트·숨긴 글은 읽은 글에 없음"

    report.step("페이지 처리기 실행", page_log)

    passed = sum(ok for ok, _ in report.results)
    print(f"\n{passed}/{len(report.results)} 통과")
    return 0 if passed == len(report.results) else 1


if __name__ == "__main__":
    sys.exit(main())
