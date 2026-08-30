"""PowerPoint 실기검증 — 이 패키지의 핵심 주장을 실제 창으로 확인합니다.

Windows PC 에서 PowerPoint 를 하나 열어 둔 뒤 실행하세요.

    python examples/verify_pptx.py              # 읽기 전용 (파일을 건드리지 않음)
    python examples/verify_pptx.py --write      # 입력까지 검증 (되돌리기 포함)

검증하는 주장:
  1. 최소화된 PowerPoint 창의 UI 트리를 정확히 읽는다
  2. 다른 창에 가려진 PowerPoint 의 실제 화면을 캡처한다
  3. 최소화 상태 그대로 슬라이드에 글자를 넣는다 (--write)
  4. 그동안 사용자의 마우스 커서와 전경 창을 한 번도 빼앗지 않는다

4번이 이 도구의 존재 이유이므로, 매 단계마다 커서 좌표와 전경 창 핸들을
확인해서 어긋나면 그 단계를 실패로 기록합니다.
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from dataclasses import dataclass, field

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))

from claude_hands import attach, windows  # noqa: E402
from claude_hands.win32.defs import IS_WINDOWS, ClaudeHandsError  # noqa: E402
from claude_hands.win32.windows import (  # noqa: E402
    cursor_pos,
    foreground_hwnd,
    is_minimized,
)

PPT_PROCESSES = ("powerpnt.exe", "powerpoint")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    evidence: list[str] = field(default_factory=list)


class Report:
    """Collects results and, at every step, that nothing was stolen from the user."""

    def __init__(self) -> None:
        self.checks: list[Check] = []
        self.cursor_at_start = cursor_pos()
        self.foreground_at_start = foreground_hwnd()
        self.cursor_moved = False
        self.foreground_changed = False

    def watch(self) -> list[str]:
        """Return violations of the 'never steal from the user' invariant."""

        problems = []
        if cursor_pos() != self.cursor_at_start:
            self.cursor_moved = True
            problems.append(f"커서가 {self.cursor_at_start} → {cursor_pos()} 로 이동함")
        if foreground_hwnd() != self.foreground_at_start:
            self.foreground_changed = True
            problems.append(
                f"전경 창이 {self.foreground_at_start} → {foreground_hwnd()} 로 바뀜"
            )
        return problems

    def record(self, name: str, ok: bool, detail: str = "", strict: bool = True) -> Check:
        problems = self.watch()
        if problems and strict:
            ok = False
            detail = (detail + " / " if detail else "") + "; ".join(problems)
        check = Check(name, ok, detail, problems)
        self.checks.append(check)
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f"\n         {detail}" if detail else ""))
        return check

    def fail(self, name: str, exc: BaseException) -> Check:
        return self.record(name, False, f"{type(exc).__name__}: {exc}", strict=False)

    def summary(self) -> int:
        passed = sum(1 for c in self.checks if c.ok)
        total = len(self.checks)
        print("\n" + "=" * 68)
        print(f"결과: {passed}/{total} 통과")
        print(f"커서 이동     : {'있었음 (문제)' if self.cursor_moved else '없음'}")
        print(f"전경 창 변경  : {'있었음 (문제)' if self.foreground_changed else '없음'}")
        failed = [c for c in self.checks if not c.ok]
        if failed:
            print("\n실패한 항목:")
            for check in failed:
                print(f"  - {check.name}: {check.detail}")
        print("=" * 68)
        return 0 if not failed else 1


def find_powerpoint():
    for process in PPT_PROCESSES:
        found = windows(process=process)
        if found:
            return found
    return []


def run(write: bool) -> int:
    if not IS_WINDOWS:
        print("이 검증은 Windows 에서만 의미가 있습니다. 현재 플랫폼:", sys.platform)
        return 2

    print("=" * 68)
    print("claude-hands 실기검증 — PowerPoint")
    print("=" * 68)

    report = Report()
    print(f"\n시작 시점  커서 {report.cursor_at_start}, 전경 창 hwnd={report.foreground_at_start}")

    # ---- 0. PowerPoint 찾기 --------------------------------------------
    print("\n[0] PowerPoint 창 찾기")
    found = find_powerpoint()
    if not found:
        print("  PowerPoint 창을 찾지 못했습니다. 프레젠테이션을 하나 열고 다시 실행하세요.")
        print("  (현재 열린 창 목록)")
        for info in windows()[:10]:
            print("   ", info.describe())
        return 2
    for info in found:
        print("   ", info.describe())
    target = found[0]
    report.record("PowerPoint 창 탐지", True, target.describe())

    # ---- 1. 연결 (활성화 없이) -----------------------------------------
    print("\n[1] 창에 연결 — 앞으로 꺼내지 않아야 함")
    try:
        ppt = attach(hwnd=target.hwnd)
        report.record("attach 가 전경 창을 바꾸지 않음", True, ppt.info.describe())
    except ClaudeHandsError as exc:
        report.fail("attach", exc)
        return report.summary()

    was_minimized = ppt.info.state == "minimized"

    # ---- 2. 최소화 상태로 만들기 ---------------------------------------
    print("\n[2] 창을 최소화한 뒤 그대로 조작 — 이 도구의 핵심 주장")
    try:
        ppt.minimize()
        time.sleep(0.6)
        minimized = is_minimized(ppt.hwnd)
        report.record("창이 실제로 최소화됨", minimized, f"state={ppt.info.state}")
    except ClaudeHandsError as exc:
        report.fail("최소화", exc)
        minimized = False

    # ---- 3. 최소화 상태에서 UI 트리 읽기 -------------------------------
    print("\n[3] 최소화 상태에서 snapshot 읽기")
    tree_text = ""
    try:
        started = time.time()
        tree_text = ppt.snapshot(interactive_only=True, max_lines=60)
        elapsed = time.time() - started
        lines = [ln for ln in tree_text.splitlines() if ln.strip().startswith("[")]
        report.record(
            "최소화 상태에서 UI 요소를 읽음",
            len(lines) >= 3,
            f"조작 가능한 요소 {len(lines)}개, {elapsed:.1f}초 소요",
        )
        print("\n  --- 읽어낸 요소 (앞 15줄) ---")
        for line in tree_text.splitlines()[:15]:
            print("   ", line)
    except ClaudeHandsError as exc:
        report.fail("최소화 상태 snapshot", exc)

    # ---- 4. 슬라이드/리본 요소 검색 ------------------------------------
    print("\n[4] 최소화 상태에서 이름으로 요소 검색")
    for query, label in (("슬라이드", "슬라이드 영역"), ("홈", "리본 탭"), ("제목", "제목 개체틀")):
        try:
            hits = ppt.find(query, limit=3)
            report.record(
                f"검색: {label} ({query!r})",
                bool(hits),
                "; ".join(f'{h.role} "{h.name}" → {h.ref}' for h in hits[:2]) or "결과 없음",
            )
        except ClaudeHandsError as exc:
            report.fail(f"검색 {query!r}", exc)

    # ---- 5. 최소화 상태에서 화면 캡처 ----------------------------------
    print("\n[5] 최소화 상태에서 화면 캡처")
    try:
        capture = ppt.screenshot()
        data = capture.to_png(max_side=1200)
        out = "pptx_verify_minimized.png"
        with open(out, "wb") as handle:
            handle.write(data)
        report.record(
            "최소화된 창의 픽셀을 얻음",
            len(data) > 5000,
            f"{out} — {capture.width}x{capture.height}, {len(data)/1024:.0f}KB, "
            f"화면밖복원={capture.restored_offscreen}",
        )
    except ClaudeHandsError as exc:
        report.fail("최소화 상태 캡처", exc)

    # ---- 6. 최소화 상태에서 입력 (--write) -----------------------------
    if write:
        print("\n[6] 최소화 상태에서 슬라이드에 입력 (--write)")
        marker = f"claude-hands 검증 {time.strftime('%H:%M:%S')}"
        try:
            targets = (
                ppt.find("제목", role="edit", limit=1)
                or ppt.find("", role="edit", limit=1)
                or ppt.find("", role="document", limit=1)
            )
            if not targets:
                report.record("입력 대상 탐색", False, "편집 가능한 개체틀을 찾지 못했습니다.")
            else:
                node = targets[0]
                result = ppt.type(node.ref, marker, allow_focus=False)
                report.record(
                    "최소화 상태에서 입력 성공",
                    result.ok,
                    f"{result.strategy} → {node.role} \"{node.name}\"",
                )
                time.sleep(0.4)
                readback = ppt.text(node.ref)
                report.record(
                    "입력한 글자를 다시 읽어 확인",
                    marker in readback,
                    f"읽은 값: {readback[:80]!r}",
                )
                print("      되돌리기(ctrl+z) 전송")
                ppt.keys("ctrl+z")
                time.sleep(0.4)
        except ClaudeHandsError as exc:
            report.fail("최소화 상태 입력", exc)
    else:
        print("\n[6] 입력 검증 생략 (--write 로 켤 수 있음, 문서를 수정한 뒤 ctrl+z 로 되돌립니다)")

    # ---- 7. 원상복구 ----------------------------------------------------
    print("\n[7] 원래 상태로 되돌리기")
    try:
        if not was_minimized:
            ppt.restore()  # 활성화 없이 복원
            time.sleep(0.4)
        report.record(
            "복원이 전경 창을 빼앗지 않음",
            True,
            f"최종 상태={ppt.info.state}",
        )
    except ClaudeHandsError as exc:
        report.fail("복원", exc)

    return report.summary()


def main() -> int:
    parser = argparse.ArgumentParser(description="PowerPoint 로 claude-hands 실기검증")
    parser.add_argument(
        "--write",
        action="store_true",
        help="슬라이드에 실제로 글자를 넣어 검증합니다 (끝나면 ctrl+z 로 되돌립니다).",
    )
    args = parser.parse_args()
    if args.write:
        print("주의: --write 는 현재 열린 프레젠테이션을 수정합니다.")
        print("      되돌리기를 보내지만, 중요한 파일이라면 사본에서 실행하세요.")
        try:
            if input("계속할까요? [y/N] ").strip().lower() not in {"y", "yes"}:
                print("취소했습니다.")
                return 130
        except EOFError:
            print("대화형 확인이 불가능해 읽기 전용으로 진행합니다.")
            args.write = False
    try:
        return run(args.write)
    except Exception:  # noqa: BLE001 - 검증 스크립트는 무엇이든 보고해야 합니다
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
