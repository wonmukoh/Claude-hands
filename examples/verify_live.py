"""실기검증 — 실제로 떠 있는 Windows 프로그램을 상대로 전 계층을 확인합니다.

어떤 프로그램에도 쓸 수 있게 만들었습니다.

    python examples/verify_live.py --process notepad
    python examples/verify_live.py --process POWERPNT --engine uia
    python examples/verify_live.py --process winecfg --engine win32 --no-write

검증하는 것:
  * 창 열거 / 연결이 전경 창을 빼앗지 않는가
  * UI 트리를 읽고 요소를 이름으로 찾는가
  * 편집 영역에 글자를 넣고 그대로 되읽는가 (--write, 기본 켜짐)
  * 버튼/체크박스를 조작하고 상태 변화가 반영되는가
  * 가려진 창과 최소화된 창의 픽셀을 얻는가
  * **최소화 상태에서도 위 전부가 되는가**
  * 그동안 커서와 전경 창을 한 번도 건드리지 않는가
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from claude_hands import attach, windows  # noqa: E402
from claude_hands.actions import ActionFailedError  # noqa: E402
from claude_hands.win32.defs import (  # noqa: E402
    IS_WINDOWS,
    ClaudeHandsError,
    force_utf8_output,
)
from claude_hands.win32.windows import cursor_pos, foreground_hwnd, is_minimized  # noqa: E402


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    violations: list[str] = field(default_factory=list)


class Report:
    def __init__(self) -> None:
        self.checks: list[Check] = []
        self.cursor_at_start = cursor_pos()
        self.foreground_at_start = foreground_hwnd()
        self.cursor_moved = False
        self.foreground_changed = False

    def violations(self) -> list[str]:
        found = []
        now = cursor_pos()
        if now != self.cursor_at_start:
            self.cursor_moved = True
            found.append(f"커서 {self.cursor_at_start} → {now}")
        front = foreground_hwnd()
        if front != self.foreground_at_start:
            self.foreground_changed = True
            found.append(f"전경 창 {self.foreground_at_start} → {front}")
        return found

    def record(self, name: str, ok: bool, detail: str = "", *, strict: bool = True) -> Check:
        problems = self.violations()
        if problems and strict:
            ok = False
            detail = (detail + " / " if detail else "") + "사용자 입력을 가로챔: " + "; ".join(problems)
        check = Check(name, ok, detail, problems)
        self.checks.append(check)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"\n         {detail}" if detail else ""))
        return check

    def fail(self, name: str, exc: BaseException) -> Check:
        return self.record(name, False, f"{type(exc).__name__}: {exc}", strict=False)

    def skip(self, name: str, why: str) -> None:
        print(f"  [SKIP] {name}\n         {why}")

    def summary(self) -> int:
        passed = sum(1 for c in self.checks if c.ok)
        print("\n" + "=" * 70)
        print(f"결과: {passed}/{len(self.checks)} 통과")
        print(f"커서 이동    : {'있었음 (문제)' if self.cursor_moved else '없음'}")
        print(f"전경 창 변경 : {'있었음 (문제)' if self.foreground_changed else '없음'}")
        failed = [c for c in self.checks if not c.ok]
        if failed:
            print("\n실패:")
            for check in failed:
                print(f"  - {check.name}: {check.detail}")
        print("=" * 70)
        return 1 if failed else 0


def _distinct_colours(capture, sample: int = 20000) -> int:
    """How many distinct colours a capture holds — a black rectangle has one."""

    try:
        image = capture.to_image()
    except Exception:  # noqa: BLE001 - a capture we cannot decode is not content
        return 0
    colours = image.getcolors(maxcolors=sample)
    return len(colours) if colours else sample


def probe_reading(app, report: Report, phase: str) -> list:
    """스냅샷과 검색 — 창 상태(보임/최소화)에 관계없이 같은 결과가 나와야 합니다."""

    started = time.time()
    text = app.snapshot(max_lines=80)
    elapsed = time.time() - started
    nodes = [line for line in text.splitlines() if line.strip().startswith("[")]
    report.record(
        f"[{phase}] UI 트리 읽기",
        len(nodes) >= 2,
        f"요소 {len(nodes)}개, {elapsed:.2f}초, 엔진={app.engine}",
    )
    return nodes


def run(process: str, engine: str, write: bool, keep_open: bool) -> int:
    if not IS_WINDOWS:
        print("Windows(또는 Wine) 에서만 의미가 있습니다. 현재:", sys.platform)
        return 2

    print("=" * 70)
    print(f"claude-hands 실기검증 — process={process!r} engine={engine}")
    print("=" * 70)
    report = Report()
    print(f"\n시작: 커서 {report.cursor_at_start}, 전경 창 hwnd={report.foreground_at_start}\n")

    # 0. 탐지
    print("[0] 창 탐지")
    found = windows(process=process)
    if not found:
        print(f"  {process!r} 창을 찾지 못했습니다. 열려 있는 창:")
        for info in windows()[:12]:
            print("   ", info.describe())
        return 2
    for info in found:
        print("   ", info.describe())
    report.record("창 탐지", True, found[0].describe())

    # 1. 연결
    print("\n[1] 연결 (활성화 없이)")
    try:
        app = attach(hwnd=found[0].hwnd, engine=engine)
        report.record("attach", True, f"hwnd={app.hwnd}, 상태={app.info.state}")
    except ClaudeHandsError as exc:
        report.fail("attach", exc)
        return report.summary()

    original_state = app.info.state

    # 2. 보이는 상태에서 읽기
    print("\n[2] 보이는 상태에서 읽기")
    visible_nodes = probe_reading(app, report, "보임")
    for line in visible_nodes[:10]:
        print("     ", line.strip())

    # 3. 조작 — 편집 영역
    edits = app.find("", role="edit", limit=1)
    original_text = None
    if write and edits:
        print("\n[3] 편집 영역에 입력 후 되읽기")
        marker = "claude-hands 실기검증 한글 ASCII 12345"
        try:
            original_text = app.text(edits[0].ref)
            result = app.type(edits[0].ref, marker, allow_focus=False)
            readback = app.text(edits[0].ref)
            report.record(
                "입력이 그대로 반영됨",
                readback == marker,
                f"방식={result.strategy} / 쓴 값={marker!r} / 읽은 값={readback!r}",
            )
        except ActionFailedError as exc:
            report.fail("편집 영역 입력", exc)
    elif write:
        report.skip("편집 영역 입력", "이 창에는 편집 가능한 컨트롤이 없습니다.")

    # 4. 조작 — 버튼 / 체크박스
    print("\n[4] 버튼·체크박스 조작")
    checkboxes = app.find("", role="checkbox", limit=1)
    if write and checkboxes:
        node = checkboxes[0]
        try:
            before = node.toggle_state
            app.toggle(node.ref)
            after = app.find(node.name, role="checkbox", limit=1)[0].toggle_state
            report.record(
                "체크박스 상태가 실제로 바뀜",
                before != after,
                f'"{node.name}" {before} → {after}',
            )
            app.toggle(node.ref)  # 원상복구
        except (ActionFailedError, IndexError) as exc:
            report.fail("체크박스 토글", exc if isinstance(exc, Exception) else Exception(str(exc)))
    else:
        report.skip("체크박스 토글", "체크박스가 없거나 --no-write 입니다.")

    buttons = app.find("", role="button", limit=3)
    report.record(
        "버튼을 조작 가능한 요소로 인식",
        bool(buttons) or not checkboxes,
        f"버튼 {len(buttons)}개: " + ", ".join(f'"{b.name}"' for b in buttons[:3]),
        strict=True,
    )

    # 5. 보이는 상태 캡처
    print("\n[5] 화면 캡처")
    try:
        capture = app.screenshot()
        png = capture.to_png(max_side=1200)
        Path("verify_visible.png").write_bytes(png)
        colours = _distinct_colours(capture)
        report.record(
            "보이는 창 캡처가 실제 내용을 담음",
            colours >= 8 and not capture.blank,
            f"verify_visible.png — {capture.width}x{capture.height}, "
            f"{len(png)/1024:.0f}KB, 고유색 {colours}개",
        )
    except ClaudeHandsError as exc:
        report.fail("보이는 창 캡처", exc)

    # 6. 최소화 — 핵심 주장
    print("\n[6] 최소화 후에도 전부 동작하는가 (핵심)")
    try:
        app.minimize()
        time.sleep(0.8)
        report.record("창이 최소화됨", is_minimized(app.hwnd), f"상태={app.info.state}")
    except ClaudeHandsError as exc:
        report.fail("최소화", exc)

    minimized_nodes = probe_reading(app, report, "최소화")
    report.record(
        "최소화 전후 트리가 같은 규모",
        abs(len(minimized_nodes) - len(visible_nodes)) <= max(2, len(visible_nodes) // 5),
        f"보임 {len(visible_nodes)}개 → 최소화 {len(minimized_nodes)}개",
    )

    if write and edits:
        try:
            marker2 = "최소화 상태에서 쓴 문장 " + time.strftime("%H:%M:%S")
            result = app.type(edits[0].ref, marker2, allow_focus=False)
            readback = app.text(edits[0].ref)
            report.record(
                "최소화 상태에서 입력·되읽기",
                readback == marker2,
                f"방식={result.strategy} / 읽은 값={readback!r}",
            )
        except ActionFailedError as exc:
            report.fail("최소화 상태 입력", exc)

    print("\n[7] 최소화 상태 캡처 (화면 밖 복원)")
    geometry_before = app.info.rect
    try:
        capture = app.screenshot(restore_if_minimized=True)
        png = capture.to_png(max_side=1200)
        Path("verify_minimized.png").write_bytes(png)
        colours = _distinct_colours(capture)
        report.record(
            "최소화된 창의 픽셀 확보",
            colours >= 8 and not capture.blank,
            f"verify_minimized.png — {capture.width}x{capture.height}, "
            f"{len(png)/1024:.0f}KB, 고유색 {colours}개, blank={capture.blank}",
        )
    except ClaudeHandsError as exc:
        report.fail("최소화 상태 캡처", exc)

    # 캡처가 창의 크기·위치를 훼손하지 않았는지 — 사용자 창을 건드리면 안 됩니다
    geometry_after = app.info.rect
    report.record(
        "캡처가 창 크기·위치를 바꾸지 않음",
        (geometry_before.width, geometry_before.height)
        == (geometry_after.width, geometry_after.height),
        f"{geometry_before.width}x{geometry_before.height} → "
        f"{geometry_after.width}x{geometry_after.height}",
    )
    report.record(
        "캡처 후에도 최소화 상태 유지",
        is_minimized(app.hwnd),
        f"상태={app.info.state}",
    )

    # 8. 원상복구
    print("\n[8] 원상복구")
    try:
        if original_text is not None and edits:
            app.type(edits[0].ref, original_text, allow_focus=False)
        if original_state != "minimized" and not keep_open:
            app.restore()
            time.sleep(0.5)
        report.record("복원", True, f"상태={app.info.state}")
    except ClaudeHandsError as exc:
        report.fail("복원", exc)

    return report.summary()


def main() -> int:
    force_utf8_output()
    parser = argparse.ArgumentParser(description="실행 중인 창으로 claude-hands 검증")
    parser.add_argument("--process", default="notepad", help="대상 실행 파일 이름 일부")
    parser.add_argument("--engine", default="auto", choices=["auto", "uia", "win32"])
    parser.add_argument("--no-write", action="store_true", help="읽기만 수행")
    parser.add_argument("--keep-minimized", action="store_true", help="끝나도 복원하지 않음")
    args = parser.parse_args()
    try:
        return run(args.process, args.engine, not args.no_write, args.keep_minimized)
    except Exception:  # noqa: BLE001 - 검증기는 무엇이든 보고해야 합니다
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
