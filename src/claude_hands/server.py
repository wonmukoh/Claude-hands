"""MCP server exposing claude-hands to Claude Code / Claude Desktop.

The tool surface mirrors how a model actually works a UI: list windows, attach
to one, snapshot it to get refs, then act on refs. Every action names the
strategy it used so the model can tell a clean UI Automation call from a
best-effort synthetic click.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any, Optional

from . import actions as _actions
from .api import manager, windows as _list_windows
from .elements import format_node_line
from .session import SnapshotOptions, WindowSession
from .win32.capture import capture_window
from .win32.defs import IS_WINDOWS, ClaudeHandsError
from .win32.windows import (
    close_window,
    describe_window,
    maximize,
    minimize,
    move_window,
    restore,
)

# The MCP SDK renamed FastMCP to MCPServer in 2.x; support both.
try:  # mcp >= 2
    from mcp.server.mcpserver import Image, MCPServer as _Server
except ImportError:  # pragma: no cover - depends on the installed SDK
    try:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP as _Server, Image
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise SystemExit(
            "mcp 패키지가 필요합니다. `pip install mcp` 또는 `pip install claude-hands` 로 설치하세요."
        ) from exc


READ_ONLY = os.environ.get("CLAUDE_HANDS_READONLY", "").lower() in {"1", "true", "yes"}

mcp = _Server(
    "claude-hands",
    instructions=(
        "Windows 프로그램 하나에 붙어서 조작하는 도구입니다. 데스크톱 전체를 보지 않고 "
        "지정한 창(HWND)만 다루며, 그 창이 최소화되어 있거나 다른 창에 완전히 가려져 있어도 "
        "동작합니다. 마우스 커서와 키보드 포커스를 빼앗지 않으므로 사용자는 옆에서 다른 일을 "
        "계속할 수 있습니다.\n\n"
        "사용 순서: list_windows → attach_window → snapshot(ref 목록 확인) → "
        "click_element / type_text / press_keys 등으로 조작.\n"
        "ref(e12 같은 값)는 snapshot 또는 find_elements 가 발급합니다. UI가 바뀌면 자동으로 "
        "다시 연결하지만, 크게 달라졌다면 snapshot 을 다시 찍으세요."
    ),
)


def _guard_write() -> None:
    if READ_ONLY:
        raise ClaudeHandsError(
            "읽기 전용 모드입니다 (CLAUDE_HANDS_READONLY). 조작 도구는 비활성화되어 있습니다."
        )


async def _run(func, *args, **kwargs):
    """Run blocking Win32/UIA work off the event loop."""

    return await asyncio.to_thread(func, *args, **kwargs)


def _session(hwnd: Optional[int] = None) -> WindowSession:
    return manager().get(hwnd)


def _fail(exc: BaseException) -> str:
    return f"오류: {exc}"


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


@mcp.tool()
async def list_windows(
    title_contains: str | None = None,
    process: str | None = None,
    include_hidden: bool = False,
) -> str:
    """열려 있는 창 목록을 반환합니다 (최소화된 창 포함).

    Args:
        title_contains: 제목에 이 문자열이 들어간 창만 (대소문자 무시).
        process: 실행 파일 이름 일부로 거르기 (예: "chrome.exe", "notepad").
        include_hidden: 숨김/제목 없는 창까지 포함할지 여부.
    """

    if not IS_WINDOWS:
        return _fail(ClaudeHandsError("이 도구는 Windows 에서만 동작합니다."))
    try:
        found = await _run(
            _list_windows,
            title_contains=title_contains,
            process=process,
            include_hidden=include_hidden,
        )
    except ClaudeHandsError as exc:
        return _fail(exc)
    if not found:
        return "조건에 맞는 창이 없습니다."
    lines = [f"창 {len(found)}개:"]
    lines += [f"  {info.describe()}" for info in found]
    lines.append("\nattach_window(hwnd=...) 로 조작할 창을 지정하세요.")
    return "\n".join(lines)


@mcp.tool()
async def attach_window(
    hwnd: int | None = None,
    title: str | None = None,
    process: str | None = None,
    pid: int | None = None,
) -> str:
    """조작할 창 하나에 연결합니다. 이후 모든 도구는 이 창을 대상으로 동작합니다.

    창을 앞으로 가져오거나 활성화하지 않습니다. 최소화 상태 그대로 연결됩니다.

    Args:
        hwnd: 창 핸들 (list_windows 결과에 있음). 가장 정확한 지정 방법.
        title: 창 제목 일부.
        process: 실행 파일 이름 일부.
        pid: 프로세스 ID.
    """

    try:
        session = await _run(
            manager().attach, hwnd=hwnd, title=title, process=process, pid=pid
        )
        info = await _run(session.refresh_info)
    except ClaudeHandsError as exc:
        return _fail(exc)
    return (
        f"연결됨: {info.describe()}\n"
        f"창 크기 {info.rect.width}x{info.rect.height} @ {info.rect.left},{info.rect.top}\n"
        "다음: snapshot() 으로 조작 가능한 요소와 ref 를 확인하세요."
    )


@mcp.tool()
async def detach_window(hwnd: int | None = None) -> str:
    """연결을 해제합니다 (창 자체는 그대로 둡니다)."""

    removed = await _run(manager().detach, hwnd)
    return "연결을 해제했습니다." if removed else "해제할 연결이 없습니다."


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


@mcp.tool()
async def snapshot(
    hwnd: int | None = None,
    interactive_only: bool = False,
    depth: int = 12,
    max_lines: int = 400,
    show_coordinates: bool = True,
) -> str:
    """연결된 창의 UI 트리를 읽어 각 요소에 ref 를 붙여 돌려줍니다.

    화면 캡처가 아니라 UI Automation 트리라서, 창이 최소화되어 있어도 정확히 읽힙니다.
    출력의 [e12] 같은 값이 ref 이며 click_element 등에 그대로 넣으면 됩니다.

    Args:
        hwnd: 다른 창을 지정하려면 입력 (생략하면 마지막에 연결한 창).
        interactive_only: True 면 누를 수 있는 요소만 평면 목록으로 보여줍니다.
        depth: 트리 탐색 최대 깊이.
        max_lines: 출력 줄 수 상한.
        show_coordinates: 각 요소의 화면 좌표/크기 표시 여부.
    """

    try:
        session = _session(hwnd)
        return await _run(
            session.render,
            SnapshotOptions(
                max_depth=depth,
                max_lines=max_lines,
                interactive_only=interactive_only,
                show_rect=show_coordinates,
            ),
        )
    except ClaudeHandsError as exc:
        return _fail(exc)


@mcp.tool()
async def find_elements(
    query: str,
    role: str | None = None,
    limit: int = 10,
    actionable_only: bool = False,
    hwnd: int | None = None,
) -> str:
    """이름·자동화 ID·값으로 요소를 검색해 ref 와 함께 돌려줍니다.

    Args:
        query: 찾을 문자열 (예: "저장", "확인", "파일 이름").
        role: 종류로 제한 (button, edit, checkbox, listitem, menuitem, tabitem ...).
        limit: 최대 개수.
        actionable_only: 실제로 조작 가능한 요소만.
        hwnd: 대상 창 (생략 시 현재 창).
    """

    try:
        session = _session(hwnd)
        matches = await _run(
            session.find,
            query,
            role=role,
            limit=limit,
            actionable_only=actionable_only,
        )
    except ClaudeHandsError as exc:
        return _fail(exc)
    if not matches:
        return (
            f"{query!r} 와(과) 일치하는 요소가 없습니다. "
            "snapshot() 으로 전체 트리를 확인해 보세요."
        )
    lines = [f"{len(matches)}개 찾음 (일치도 높은 순):"]
    for score, node in matches:
        lines.append(f"  {score:.2f}  {format_node_line(node)}")
    return "\n".join(lines)


@mcp.tool()
async def read_text(ref: str | None = None, hwnd: int | None = None, max_chars: int = 8000) -> str:
    """요소(또는 창 전체)의 텍스트를 읽습니다. 문서/편집기 내용 읽기에 씁니다."""

    try:
        session = _session(hwnd)
        text = await _run(_actions.get_text, session, ref, max_chars=max_chars)
    except ClaudeHandsError as exc:
        return _fail(exc)
    return text or "(읽을 수 있는 텍스트가 없습니다.)"


@mcp.tool()
async def screenshot_window(
    hwnd: int | None = None,
    ref: str | None = None,
    restore_if_minimized: bool = True,
    max_side: int = 1400,
) -> Any:
    """창(또는 한 요소)의 픽셀을 캡처합니다. 다른 창에 가려져 있어도 실제 내용이 찍힙니다.

    최소화된 창은 픽셀이 존재하지 않으므로, restore_if_minimized 가 True 면 화면
    바깥에서 잠깐 복원했다가 원래 상태로 되돌려 캡처합니다(사용자 포커스는 그대로).
    창 내용 파악은 대개 snapshot() 이 더 정확하고 빠릅니다.

    Args:
        hwnd: 대상 창 (생략 시 현재 창).
        ref: 특정 요소만 잘라내려면 ref 지정.
        restore_if_minimized: 최소화된 창을 화면 밖에서 잠깐 복원해 캡처할지 여부.
        max_side: 이미지 최대 변 길이(픽셀).
    """

    try:
        session = _session(hwnd)
        capture = await _run(
            capture_window, session.hwnd, restore_if_minimized=restore_if_minimized
        )
        if ref:
            node, _element = await _run(session.resolve, ref)
            if node.rect:
                capture = capture.crop(node.rect)
        png = await _run(capture.to_png, max_side=max_side)
    except ClaudeHandsError as exc:
        return _fail(exc)
    return Image(data=png, format="png")


# --------------------------------------------------------------------------
# Acting
# --------------------------------------------------------------------------


@mcp.tool()
async def click_element(
    ref: str,
    button: str = "left",
    double: bool = False,
    modifiers: str = "",
    hwnd: int | None = None,
) -> str:
    """요소를 누릅니다. 커서를 움직이지 않고 창을 앞으로 꺼내지도 않습니다.

    기본 왼쪽 클릭은 UI Automation 패턴(Invoke/Toggle/Select)으로 처리하므로
    창이 최소화되어 있어도 동작합니다. 오른쪽/더블/수식키 클릭은 창 메시지로 보냅니다.

    Args:
        ref: snapshot 또는 find_elements 가 준 ref (예: "e12").
        button: left / right / middle.
        double: 더블클릭 여부.
        modifiers: 함께 누를 수식키, 쉼표 구분 (예: "ctrl", "ctrl,shift").
        hwnd: 대상 창 (생략 시 현재 창).
    """

    try:
        _guard_write()
        session = _session(hwnd)
        mods = tuple(m.strip().lower() for m in modifiers.split(",") if m.strip())
        result = await _run(
            _actions.click, session, ref, button=button, double=double, modifiers=mods
        )
    except ClaudeHandsError as exc:
        return _fail(exc)
    return result.describe()


@mcp.tool()
async def type_text(
    ref: str,
    text: str,
    clear: bool = True,
    submit: bool = False,
    hwnd: int | None = None,
) -> str:
    """입력란에 글자를 넣습니다.

    UI Automation 의 SetValue 로 문자열을 통째로 전달하므로 한글 IME 조합 문제가 없고,
    창이 최소화되어 있어도 입력됩니다. SetValue 를 지원하지 않는 컨트롤에서만
    포커스를 잡고 한 글자씩 보내는 방식으로 내려갑니다(그 경우 결과에 표시됩니다).

    Args:
        ref: 입력란의 ref.
        text: 넣을 문자열.
        clear: 기존 내용을 지우고 넣을지(False 면 뒤에 이어붙임).
        submit: 입력 후 Enter 를 보낼지.
        hwnd: 대상 창.
    """

    try:
        _guard_write()
        session = _session(hwnd)
        result = await _run(
            _actions.type_text, session, ref, text, clear=clear, submit=submit
        )
    except ClaudeHandsError as exc:
        return _fail(exc)
    return result.describe()


@mcp.tool()
async def press_keys(
    keys: str,
    ref: str | None = None,
    repeat: int = 1,
    hwnd: int | None = None,
) -> str:
    """단축키를 창에 보냅니다 (예: "ctrl+s", "alt+f4", "ctrl+shift+n enter").

    창 메시지로 보내므로 사용자의 실제 키보드를 가로채지 않습니다.

    Args:
        keys: 키 조합. 공백/쉼표로 여러 조합을 순서대로 보낼 수 있습니다.
        ref: 특정 요소의 컨트롤에 보내려면 ref 지정.
        repeat: 반복 횟수.
        hwnd: 대상 창.
    """

    try:
        _guard_write()
        session = _session(hwnd)
        result = await _run(_actions.press_keys, session, keys, ref=ref, repeat=repeat)
    except ClaudeHandsError as exc:
        return _fail(exc)
    return result.describe()


@mcp.tool()
async def scroll(
    direction: str = "down",
    amount: int = 3,
    ref: str | None = None,
    to_percent: float | None = None,
    hwnd: int | None = None,
) -> str:
    """스크롤합니다. Scroll 패턴이 있으면 그것으로, 없으면 휠 메시지로 처리합니다.

    Args:
        direction: up / down / left / right.
        amount: 스크롤 단계 수.
        ref: 스크롤할 영역의 ref (생략 시 창에서 가장 큰 스크롤 영역).
        to_percent: 0~100 으로 위치를 직접 지정 (지정 시 amount 무시).
        hwnd: 대상 창.
    """

    try:
        _guard_write()
        session = _session(hwnd)
        result = await _run(
            _actions.scroll,
            session,
            ref=ref,
            direction=direction,
            amount=amount,
            to_percent=to_percent,
        )
    except ClaudeHandsError as exc:
        return _fail(exc)
    return result.describe()


@mcp.tool()
async def set_element_state(
    ref: str,
    action: str,
    value: str | None = None,
    hwnd: int | None = None,
) -> str:
    """체크박스·목록·트리 등의 상태를 직접 바꿉니다.

    Args:
        ref: 대상 요소의 ref.
        action: select(선택) / toggle(체크 전환) / check / uncheck / expand(펼치기) /
            collapse(접기) / focus(포커스) / value(값 설정).
        value: action="value" 일 때 넣을 값.
        hwnd: 대상 창.
    """

    try:
        _guard_write()
        session = _session(hwnd)
        action = action.strip().lower()
        if action == "select":
            result = await _run(_actions.select, session, ref)
        elif action == "toggle":
            result = await _run(_actions.toggle, session, ref)
        elif action == "check":
            result = await _run(_actions.toggle, session, ref, to=True)
        elif action == "uncheck":
            result = await _run(_actions.toggle, session, ref, to=False)
        elif action == "expand":
            result = await _run(_actions.expand, session, ref)
        elif action == "collapse":
            result = await _run(_actions.expand, session, ref, collapse=True)
        elif action == "focus":
            result = await _run(_actions.focus, session, ref)
        elif action == "value":
            if value is None:
                return "오류: action='value' 에는 value 인자가 필요합니다."
            result = await _run(_actions.set_value, session, ref, value)
        else:
            return (
                f"오류: 알 수 없는 action {action!r}. "
                "select/toggle/check/uncheck/expand/collapse/focus/value 중에서 고르세요."
            )
    except ClaudeHandsError as exc:
        return _fail(exc)
    return result.describe()


@mcp.tool()
async def menu_select(path: str, hwnd: int | None = None) -> str:
    """메뉴를 이름으로 따라 들어갑니다. 예: "파일 > 다른 이름으로 저장".

    Args:
        path: '>' 로 구분한 메뉴 경로.
        hwnd: 대상 창.
    """

    try:
        _guard_write()
        session = _session(hwnd)
        result = await _run(_actions.menu_select, session, path)
    except ClaudeHandsError as exc:
        return _fail(exc)
    return result.describe()


@mcp.tool()
async def wait_for_element(
    query: str,
    role: str | None = None,
    timeout: float = 10.0,
    enabled: bool = False,
    vanish: bool = False,
    hwnd: int | None = None,
) -> str:
    """요소가 나타날 때까지(또는 사라질 때까지) 기다립니다.

    Args:
        query: 기다릴 요소의 이름.
        role: 종류 제한.
        timeout: 최대 대기 초.
        enabled: 활성 상태가 될 때까지 기다릴지.
        vanish: True 면 사라질 때까지 기다립니다 (진행 표시줄 등).
        hwnd: 대상 창.
    """

    try:
        session = _session(hwnd)
        result = await _run(
            _actions.wait_for,
            session,
            query,
            role=role,
            timeout=timeout,
            enabled=enabled,
            vanish=vanish,
        )
    except ClaudeHandsError as exc:
        return _fail(exc)
    return result.describe()


@mcp.tool()
async def control_window(action: str, hwnd: int | None = None, x: int = 0, y: int = 0,
                         width: int | None = None, height: int | None = None) -> str:
    """창 자체를 제어합니다 (최소화/복원/최대화/이동/닫기).

    복원은 활성화 없이 이뤄지므로 사용자가 보던 창이 뒤로 밀리지 않습니다.

    Args:
        action: minimize / restore / maximize / move / close / info.
        hwnd: 대상 창.
        x, y, width, height: action="move" 일 때의 위치·크기.
    """

    try:
        _guard_write()
        session = _session(hwnd)
        target = session.hwnd
        action = action.strip().lower()
        if action == "minimize":
            await _run(minimize, target)
        elif action == "restore":
            await _run(restore, target, activate=False)
        elif action == "maximize":
            await _run(maximize, target)
        elif action == "move":
            await _run(move_window, target, x, y, width, height)
        elif action == "close":
            await _run(close_window, target)
            return f"hwnd={target} 에 닫기 요청을 보냈습니다."
        elif action != "info":
            return (
                f"오류: 알 수 없는 action {action!r}. "
                "minimize/restore/maximize/move/close/info 중에서 고르세요."
            )
        info = await _run(describe_window, target)
    except ClaudeHandsError as exc:
        return _fail(exc)
    return f"{action} 완료 — {info.describe()}"


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="claude-hands-mcp",
        description="claude-hands MCP 서버 (Windows 프로그램 조작)",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="조작 도구를 끄고 읽기 전용(창 목록/스냅샷/캡처)으로만 실행합니다.",
    )
    args = parser.parse_args(argv)

    global READ_ONLY
    if args.read_only:
        READ_ONLY = True

    if not IS_WINDOWS:
        print(
            "경고: Windows 가 아닌 환경입니다. 서버는 뜨지만 모든 조작 도구가 오류를 반환합니다.",
            file=sys.stderr,
        )
    mcp.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
