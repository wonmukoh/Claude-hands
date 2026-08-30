"""Command line front end — handy for trying things without an MCP client.

    claude-hands windows --process notepad
    claude-hands snapshot --title 메모장 --interactive
    claude-hands click --title 메모장 --query 저장
    claude-hands doctor
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from .api import attach, windows as list_all_windows
from .elements import format_node_line
from .win32.defs import IS_WINDOWS, ClaudeHandsError, force_utf8_output


def _attach(args):
    return attach(
        hwnd=args.hwnd,
        title=args.title,
        process=args.process,
        pid=args.pid,
        engine=getattr(args, "engine", "auto"),
    )


def _resolve_ref(window, args) -> str:
    """Accept either an explicit --ref or a --query to search for."""

    if getattr(args, "ref", None):
        return args.ref
    query = getattr(args, "query", None)
    if not query:
        raise ClaudeHandsError("--ref 또는 --query 중 하나는 지정해야 합니다.")
    matches = window.session.find(query, role=getattr(args, "role", None), limit=5)
    if not matches:
        raise ClaudeHandsError(f"{query!r} 와(과) 일치하는 요소를 찾지 못했습니다.")
    score, node = matches[0]
    print(f"→ 선택: {format_node_line(node)} (일치도 {score:.2f})", file=sys.stderr)
    return node.ref


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_windows(args) -> int:
    found = list_all_windows(
        title_contains=args.title,
        process=args.process,
        include_hidden=args.all,
    )
    if not found:
        print("조건에 맞는 창이 없습니다.")
        return 1
    for info in found:
        print(info.describe())
    return 0


def cmd_snapshot(args) -> int:
    window = _attach(args)
    print(
        window.snapshot(
            depth=args.depth,
            max_lines=args.max_lines,
            interactive_only=args.interactive,
            show_rect=not args.no_coords,
        )
    )
    return 0


def cmd_find(args) -> int:
    window = _attach(args)
    matches = window.session.find(args.query, role=args.role, limit=args.limit)
    if not matches:
        print(f"{args.query!r} 와(과) 일치하는 요소가 없습니다.")
        return 1
    for score, node in matches:
        print(f"{score:.2f}  {format_node_line(node)}")
    return 0


def cmd_click(args) -> int:
    window = _attach(args)
    ref = _resolve_ref(window, args)
    modifiers = tuple(m.strip() for m in (args.modifiers or "").split(",") if m.strip())
    result = window.click(ref, button=args.button, double=args.double, modifiers=modifiers)
    print(result.describe())
    return 0 if result.ok else 1


def cmd_type(args) -> int:
    window = _attach(args)
    ref = _resolve_ref(window, args)
    result = window.type(ref, args.text, clear=not args.append, submit=args.enter)
    print(result.describe())
    return 0 if result.ok else 1


def cmd_keys(args) -> int:
    window = _attach(args)
    result = window.keys(args.keys, repeat=args.repeat)
    print(result.describe())
    return 0 if result.ok else 1


def cmd_scroll(args) -> int:
    window = _attach(args)
    ref = args.ref
    if not ref and args.query:
        ref = _resolve_ref(window, args)
    result = window.scroll(direction=args.direction, amount=args.amount, ref=ref)
    print(result.describe())
    return 0 if result.ok else 1


def cmd_text(args) -> int:
    window = _attach(args)
    ref = args.ref
    if not ref and args.query:
        ref = _resolve_ref(window, args)
    print(window.text(ref))
    return 0


def cmd_shot(args) -> int:
    window = _attach(args)
    capture = window.screenshot(ref=args.ref, restore_if_minimized=not args.no_restore)
    data = capture.to_png(max_side=args.max_side)
    with open(args.out, "wb") as handle:
        handle.write(data)
    note = " (최소화된 창을 화면 밖에서 잠시 복원했습니다)" if capture.restored_offscreen else ""
    print(f"{args.out} 저장 — {capture.width}x{capture.height}, {len(data)/1024:.0f}KB{note}")
    return 0


def cmd_menu(args) -> int:
    window = _attach(args)
    result = window.menu(args.path)
    print(result.describe())
    return 0 if result.ok else 1


def cmd_state(args) -> int:
    window = _attach(args)
    if args.action == "minimize":
        window.minimize()
    elif args.action == "restore":
        window.restore()
    elif args.action == "maximize":
        window.maximize()
    elif args.action == "close":
        window.close()
    print(window.info.describe())
    return 0


def cmd_doctor(_args) -> int:
    """Check that everything this package needs is actually available."""

    ok = True
    print("claude-hands 진단")
    print(f"  플랫폼          : {sys.platform}", end="")
    if not IS_WINDOWS:
        print("  ← Windows 가 아니므로 조작 기능은 동작하지 않습니다.")
        return 1
    print()

    from . import DPI_AWARENESS

    print(f"  DPI 인식        : {DPI_AWARENESS} (패키지 임포트 시점에 설정됨)")

    try:
        import comtypes  # noqa: F401

        print("  comtypes        : 설치됨")
    except ImportError:
        print("  comtypes        : 없음  ← `pip install comtypes` 필요")
        ok = False

    try:
        from .uia.core import get_automation

        module, automation = get_automation()
        print("  UI Automation   : 초기화 성공")
        from .uia.core import CONTROL_TYPE_NAMES

        print(f"  컨트롤 타입     : {len(CONTROL_TYPE_NAMES)}종 인식")
    except ClaudeHandsError as exc:
        print(f"  UI Automation   : 실패 — {exc}")
        ok = False

    try:
        import ctypes

        elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
        print(f"  관리자 권한     : {'예' if elevated else '아니오'}")
        if not elevated:
            print("                    (관리자 권한으로 실행 중인 프로그램을 조작하려면 이 도구도 관리자 권한이 필요합니다)")
    except Exception:  # noqa: BLE001
        pass

    try:
        found = list_all_windows()
        print(f"  보이는 창       : {len(found)}개")
        for info in found[:5]:
            print(f"      - {info.describe()}")
    except ClaudeHandsError as exc:
        print(f"  창 목록         : 실패 — {exc}")
        ok = False

    print("\n결과:", "정상" if ok else "문제 있음 (위 항목 확인)")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------


def _add_target_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("대상 창")
    group.add_argument("--hwnd", type=int, help="창 핸들")
    group.add_argument("--title", help="창 제목 일부")
    group.add_argument("--process", help="실행 파일 이름 일부")
    group.add_argument("--pid", type=int, help="프로세스 ID")
    group.add_argument(
        "--engine",
        default="auto",
        choices=["auto", "uia", "win32"],
        help="조작 엔진 (기본 auto: UIA 우선, 불가하면 창 메시지)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-hands",
        description="Windows 프로그램을 창 단위로 조작합니다 (최소화·가려진 상태에서도 동작).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("windows", help="열려 있는 창 목록")
    p.add_argument("--title", help="제목 필터")
    p.add_argument("--process", help="프로세스 필터")
    p.add_argument("--all", action="store_true", help="숨김 창까지 포함")
    p.set_defaults(func=cmd_windows)

    p = sub.add_parser("snapshot", help="UI 트리 출력 (ref 발급)")
    _add_target_args(p)
    p.add_argument("--depth", type=int, default=12)
    p.add_argument("--max-lines", type=int, default=400)
    p.add_argument("--interactive", action="store_true", help="조작 가능한 요소만")
    p.add_argument("--no-coords", action="store_true", help="좌표 숨김")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("find", help="요소 검색")
    _add_target_args(p)
    p.add_argument("query")
    p.add_argument("--role", help="button, edit, checkbox ...")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("click", help="요소 클릭")
    _add_target_args(p)
    p.add_argument("--ref")
    p.add_argument("--query", help="ref 대신 이름으로 찾아서 클릭")
    p.add_argument("--role")
    p.add_argument("--button", default="left", choices=["left", "right", "middle"])
    p.add_argument("--double", action="store_true")
    p.add_argument("--modifiers", help="쉼표 구분 (ctrl,shift)")
    p.set_defaults(func=cmd_click)

    p = sub.add_parser("type", help="입력란에 글자 넣기")
    _add_target_args(p)
    p.add_argument("text")
    p.add_argument("--ref")
    p.add_argument("--query")
    p.add_argument("--role")
    p.add_argument("--append", action="store_true", help="기존 내용 뒤에 이어붙이기")
    p.add_argument("--enter", action="store_true", help="입력 후 Enter")
    p.set_defaults(func=cmd_type)

    p = sub.add_parser("keys", help="단축키 보내기")
    _add_target_args(p)
    p.add_argument("keys", help='예: "ctrl+s", "alt+f4"')
    p.add_argument("--repeat", type=int, default=1)
    p.set_defaults(func=cmd_keys)

    p = sub.add_parser("scroll", help="스크롤")
    _add_target_args(p)
    p.add_argument("--direction", default="down", choices=["up", "down", "left", "right"])
    p.add_argument("--amount", type=int, default=3)
    p.add_argument("--ref")
    p.add_argument("--query")
    p.add_argument("--role")
    p.set_defaults(func=cmd_scroll)

    p = sub.add_parser("text", help="텍스트 읽기")
    _add_target_args(p)
    p.add_argument("--ref")
    p.add_argument("--query")
    p.add_argument("--role")
    p.set_defaults(func=cmd_text)

    p = sub.add_parser("shot", help="창 캡처 (가려져 있어도 실제 내용)")
    _add_target_args(p)
    p.add_argument("--out", default="window.png")
    p.add_argument("--ref", help="특정 요소만 잘라내기")
    p.add_argument("--max-side", type=int, default=1600)
    p.add_argument("--no-restore", action="store_true", help="최소화된 창을 복원하지 않음")
    p.set_defaults(func=cmd_shot)

    p = sub.add_parser("menu", help='메뉴 선택 (예: "파일 > 저장")')
    _add_target_args(p)
    p.add_argument("path")
    p.set_defaults(func=cmd_menu)

    p = sub.add_parser("state", help="창 상태 변경")
    _add_target_args(p)
    p.add_argument("action", choices=["minimize", "restore", "maximize", "close", "info"])
    p.set_defaults(func=cmd_state)

    p = sub.add_parser("doctor", help="환경 진단")
    p.set_defaults(func=cmd_doctor)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    force_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ClaudeHandsError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
