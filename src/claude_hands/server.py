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
from typing import Any, Literal, Optional

from . import __version__, actions as _actions
from .api import manager, windows as _list_windows
from .elements import format_node_line
from .session import SnapshotOptions, WindowSession
from .win32.capture import capture_window
from .win32.defs import IS_WINDOWS, ClaudeHandsError, force_utf8_output
from .win32.desktop import ensure_default_desktop, failure_notice
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


def _read_only_requested(argv: list[str] | None = None) -> bool:
    """Whether this server should refuse to change anything.

    The command-line flag has to be read here rather than in ``main``, because
    the server's instructions are fixed when it is constructed and that happens
    at import. Setting it later left ``--read-only`` running read-only while
    telling the caller it could click.
    """

    if os.environ.get("CLAUDE_HANDS_READONLY", "").lower() in {"1", "true", "yes"}:
        return True
    return "--read-only" in (sys.argv[1:] if argv is None else argv)


READ_ONLY = _read_only_requested()

# Kept here rather than imported from the office engine so that listing a window
# never pulls the COM stack in behind it.
OFFICE_PROCESSES = {"powerpnt.exe", "winword.exe", "excel.exe", "hwp.exe"}

READ_ONLY_NOTICE = (
    "\n\n지금은 읽기 전용 모드입니다 (CLAUDE_HANDS_READONLY). 읽기·검색·캡처는 되지만 "
    "클릭·입력·창 제어는 거절됩니다. 이 설정은 서버를 띄운 사람만 바꿀 수 있습니다."
)

mcp = _Server(
    "claude-hands",
    instructions=(
        "Windows 프로그램 하나에 붙어서 조작하는 도구입니다. 데스크톱 전체를 보지 않고 "
        "지정한 창(HWND)만 다루며, 그 창이 최소화되어 있거나 다른 창에 완전히 가려져 있어도 "
        "동작합니다. 커서와 키보드 포커스를 빼앗지 않고 창 크기·위치도 바꾸지 않으므로, "
        "사용자는 옆에서 계속 자기 일을 합니다. 이 셋이 이 도구의 존재 이유입니다.\n\n"
        "ref(e12 같은 값)는 snapshot 과 find_elements 가 발급하며, 그 연결이 살아 있는 동안 "
        "같은 요소를 가리킵니다. 스냅샷을 다시 찍어도 앞서 받은 ref 가 그대로 유효합니다. "
        "hwnd 를 생략하면 마지막에 연결한 창이 대상입니다.\n\n"
        "클릭 결과는 명령을 전달했다는 뜻이지 앱이 그 일을 했다는 뜻이 아닙니다. "
        "입력은 다릅니다 — 쓴 뒤 되읽어 확인하므로 완료면 실제로 들어간 것입니다.\n\n"
        "결재올림·발송처럼 문서를 다른 사람에게 넘기는 버튼은 사용자가 직접 누르므로 "
        "이 도구는 거절합니다. 준비가 끝나면 사용자에게 알리세요."
        + (READ_ONLY_NOTICE if READ_ONLY else "")
    ),
)


def _advertise_version(server, version: str) -> None:
    """Put this package's version in the initialize reply's ``serverInfo``.

    Nothing set it, so it went out empty with mcp 2.x (``MCPServer`` defaults
    ``version`` to "") and as the SDK's own version with 1.x — MAGISTER read
    the empty one when checking which claude-hands a PC had (2026-11-11). 1.x's
    FastMCP takes no ``version`` argument, so the low-level server under
    either is set directly; both read it when they build the reply.
    """

    for name in ("_lowlevel_server", "_mcp_server"):
        low = getattr(server, name, None)
        if low is not None and hasattr(low, "version"):
            low.version = version
            return


_advertise_version(mcp, __version__)


def _guard_write() -> None:
    if READ_ONLY:
        raise ClaudeHandsError(
            "읽기 전용 모드입니다 (CLAUDE_HANDS_READONLY). 조작 도구는 비활성화되어 있습니다."
        )


def _on_user_desktop(func, *args, **kwargs):
    """Call ``func`` after putting this worker thread on the user's desktop.

    asyncio hands the work to pool threads; a pool thread of a process started
    on another desktop (a sandbox) sees no windows until it is moved, and the
    move only applies to the thread that makes it (``win32/desktop.py``).
    """

    if IS_WINDOWS:
        ensure_default_desktop()
    return func(*args, **kwargs)


async def _run(func, *args, **kwargs):
    """Run blocking Win32/UIA work off the event loop, on the user's desktop."""

    return await asyncio.to_thread(_on_user_desktop, func, *args, **kwargs)


def _session(hwnd: Optional[int] = None) -> WindowSession:
    return manager().get(hwnd)


def _fail(exc: BaseException) -> str:
    return f"오류: {exc}"


def _tool():
    """``mcp.tool()`` with a last net under every tool.

    Each tool turns this package's own errors into a sentence. Anything else —
    a raw COM error, a timeout from a worker — used to reach the client as a
    bare traceback or an empty message, which a caller cannot act on.
    """

    import functools

    def register(func):
        @functools.wraps(func)
        async def guarded(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except ClaudeHandsError as exc:
                return _fail(exc)
            except Exception as exc:  # noqa: BLE001 - the net, see above
                return (
                    f"오류: 예상하지 못한 문제로 {func.__name__} 를 마치지 못했습니다 "
                    f"({type(exc).__name__}: {exc}). 대상 프로그램이 바쁘거나 창이 바뀌었을 수 "
                    "있습니다. snapshot 으로 지금 상태를 확인한 뒤 다시 시도하세요."
                )

        # Every tool answers in prose. Left to its default, the SDK wraps a
        # ``str`` return in an output schema and sends each answer twice — once
        # as text, once as ``structuredContent: {"result": ...}`` — and lists
        # that schema for all sixteen tools. Measured 2026-11-25: a snapshot
        # went over the wire at twice its size. Nothing reads the copy (the
        # smoke test and kakao-pc read ``content``), so it is turned off.
        try:
            decorator = mcp.tool(structured_output=False)
        except TypeError:  # pragma: no cover - mcp 1.x before structured output
            decorator = mcp.tool()
        return decorator(guarded)

    return register


def _compact_schema(schema: Any) -> Any:
    """Drop what a JSON schema repeats without telling the caller anything.

    pydantic gives every argument a ``title`` that is its own name re-cased
    ("Hwnd" for ``hwnd``) and spells an optional argument as
    ``anyOf: [{type: X}, {type: null}], default: null``. Both are sent in
    every tools/list, sixteen tools over. An argument that is not in
    ``required`` is already optional, so ``{type: X}`` says the same thing.

    Only the advertised schema changes: the SDK validates calls against the
    function's own argument model, so a caller that still sends ``null``
    is accepted as before.
    """

    if isinstance(schema, list):
        return [_compact_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for key, value in schema.items():
        if key == "title":
            continue
        if key in ("properties", "$defs") and isinstance(value, dict):
            # Keys here are argument names — attach_window has one called
            # "title" — so they are kept and only their schemas compacted.
            out[key] = {name: _compact_schema(sub) for name, sub in value.items()}
        else:
            out[key] = _compact_schema(value)
    options = out.get("anyOf")
    # Only an argument whose default is null reads null as "left out". Inside
    # call_control_method's args a null is a real value passed to the control,
    # so an anyOf with no null default keeps its null.
    if isinstance(options, list) and "default" in out and out["default"] is None:
        kept = [o for o in options if o != {"type": "null"}]
        if len(kept) < len(options):
            del out["default"]
            if len(kept) == 1 and isinstance(kept[0], dict):
                del out["anyOf"]
                out = {**kept[0], **out}
            else:
                out["anyOf"] = kept
    return out


def _compact_registered_schemas(server) -> None:
    manager = getattr(server, "_tool_manager", None)
    for tool in manager.list_tools() if manager is not None else ():
        params = getattr(tool, "parameters", None)
        if isinstance(params, dict):
            tool.parameters = _compact_schema(params)


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


@_tool()
async def list_windows(
    title_contains: str | None = None,
    process: str | None = None,
    include_hidden: bool = False,
    wait_seconds: float = 0,
) -> str:
    """열린 창 목록(최소화 포함)과 hwnd. 고른 창은 attach_window 로 연결합니다.

    Args:
        title_contains: 제목에 들어간 문자열(대소문자 무시).
        process: 실행 파일 이름 일부 (예: "notepad").
        include_hidden: 숨김·제목 없는 창도 포함.
        wait_seconds: 맞는 창이 없으면 나타날 때까지 기다릴 초(최대 60). 버튼 뒤에 뜨는 대화상자용.
    """

    if not IS_WINDOWS:
        return _fail(ClaudeHandsError("이 도구는 Windows 에서만 동작합니다."))
    deadline = asyncio.get_running_loop().time() + max(0.0, min(float(wait_seconds), 60.0))
    try:
        while True:
            found = await _run(
                _list_windows,
                title_contains=title_contains,
                process=process,
                include_hidden=include_hidden,
            )
            if found or asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.25)
    except ClaudeHandsError as exc:
        return _fail(exc)
    if not found:
        if wait_seconds:
            return (
                f"{wait_seconds:g}초 동안 기다렸지만 조건에 맞는 창이 나타나지 않았습니다."
                + failure_notice()
            )
        return "조건에 맞는 창이 없습니다." + failure_notice()
    lines = [f"창 {len(found)}개:"]
    lines += [f"  {info.describe()}" for info in found]
    return "\n".join(lines)


@_tool()
async def attach_window(
    hwnd: int | None = None,
    title: str | None = None,
    process: str | None = None,
    pid: int | None = None,
    engine: Literal["auto", "office", "mshtml", "uia", "win32"] = "auto",
) -> str:
    """창 하나에 연결합니다(앞으로 꺼내지 않음). 이후 도구는 이 창이 대상입니다.

    Args:
        hwnd: list_windows 의 창 핸들. 가장 정확합니다.
        title: 창 제목 일부.
        process: 실행 파일 이름 일부.
        pid: 프로세스 ID.
        engine: auto = PowerPoint·Word·Excel·한글이면 office, 창 대부분이 IE 웹 화면이면
            mshtml, 아니면 uia(안 되면 win32).
            office = 문서 내용(슬라이드·문단·셀·누름틀)을 읽고 씀. 리본·메뉴는 없음.
            mshtml = IE 웹 컨트롤(K-에듀파인 WXSClient 등) 페이지의 DOM. 체크칸 상태·입력란 값·
                같은 출처 iframe·페이지 안 한글 누름틀까지. 페이지 밖 버튼은 없음.
            uia = 화면의 UI 트리. 리본·메뉴·대화상자.
            win32 = 창 메시지만. 보이는 요소가 가장 적음.
    """

    try:
        session = await _run(
            manager().attach, hwnd=hwnd, title=title, process=process, pid=pid, engine=engine
        )
        info = await _run(session.refresh_info)
    except ClaudeHandsError as exc:
        return _fail(exc)

    lines = [
        f"연결됨: {info.describe()}",
        f"창 크기 {info.rect.width}x{info.rect.height} @ {info.rect.left},{info.rect.top}",
    ]
    # Which engine ends up serving this window decides what the caller can even
    # ask for — office sees document content but no ribbon, uia the reverse —
    # and `auto` does not settle it until the first snapshot. Say so here rather
    # than letting the caller discover it from a tree that is missing what they
    # were looking for.
    if engine == "auto" and (info.process or "").lower() in OFFICE_PROCESSES:
        lines.append(
            "Office 문서라 office 엔진을 씁니다(문서 내용을 직접 읽고 씀, 리본 없음). "
            "리본·대화상자는 engine='uia' 로 다시 연결하세요."
        )
    elif engine == "auto" and await _run(_is_web_window, info.hwnd):
        lines.append(
            "대부분이 IE 웹 화면이라 mshtml 엔진으로 페이지 DOM 을 읽습니다. "
            "페이지 밖 버튼은 engine='uia' 로 다시 연결하세요."
        )
    elif engine != "auto":
        lines.append(f"엔진을 {engine} 로 고정했습니다.")
    lines.append("다음: snapshot()")
    return "\n".join(lines)


def _is_web_window(hwnd: int) -> bool:
    from .mshtml.core import dominant_ie_server

    try:
        return dominant_ie_server(hwnd) is not None
    except Exception:  # noqa: BLE001 - only decides whether to add a hint
        return False


def _has_web_area(hwnd: int) -> bool:
    from .mshtml.core import ie_servers

    try:
        return bool(ie_servers(hwnd))
    except Exception:  # noqa: BLE001 - only decides whether to add a hint
        return False


@_tool()
async def detach_window(hwnd: int | None = None) -> str:
    """연결을 해제합니다 (창 자체는 그대로 둡니다)."""

    removed = await _run(manager().detach, hwnd)
    return "연결을 해제했습니다." if removed else "해제할 연결이 없습니다."


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


@_tool()
async def snapshot(
    hwnd: int | None = None,
    interactive_only: bool = False,
    depth: int = 12,
    max_lines: int = 400,
    show_coordinates: bool = True,
    expand: str | None = None,
) -> str:
    """창의 UI 트리를 [e12] 같은 ref 와 함께 돌려줍니다. 최소화돼 있어도 읽힙니다.

    office 엔진은 노드마다 프로세스 간 호출이라 큰 문서는 느리고 깁니다
    (37장 발표자료 전체 859노드·5만 자·10초). depth=1 로 목차를 보고 expand 로 하나만 펼치세요.

    Args:
        interactive_only: 누를 수 있는 요소만 평면 목록으로.
        depth: 최대 깊이. office 에서 1 이면 슬라이드·시트 목록까지만.
        max_lines: 출력 줄 수 상한.
        show_coordinates: 좌표/크기 표시. office 좌표는 문서 안 좌표라 클릭·자르기에 못 씁니다.
        expand: depth 를 넘어 펼칠 항목 이름 (예: "슬라이드 5", "Sheet1"). office 전용.
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
                expand=expand,
            ),
        )
    except ClaudeHandsError as exc:
        return _fail(exc)


@_tool()
async def find_elements(
    query: str,
    role: str | None = None,
    limit: int = 10,
    actionable_only: bool = False,
    hwnd: int | None = None,
) -> str:
    """이름·자동화 ID·값으로 요소를 찾아 ref 와 함께 돌려줍니다(일치도 순).

    Args:
        query: 찾을 문자열 (예: "저장", "파일 이름").
        role: 종류 제한 (button, edit, checkbox, listitem, menuitem, tabitem …).
        limit: 최대 개수.
        actionable_only: 조작 가능한 요소만.
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
    lines = [f"{len(matches)}개 찾음:"]
    for score, node in matches:
        lines.append(f"  {score:.2f}  {format_node_line(node)}")
    return "\n".join(lines)


@_tool()
async def read_text(ref: str | None = None, hwnd: int | None = None, max_chars: int = 8000) -> str:
    """요소(또는 창 전체)의 텍스트를 읽습니다. 문서/편집기 내용 읽기에 씁니다."""

    try:
        session = _session(hwnd)
        text = await _run(_actions.get_text, session, ref, max_chars=max_chars)
    except ClaudeHandsError as exc:
        return _fail(exc)
    return text or "(읽을 수 있는 텍스트가 없습니다.)"


@_tool()
async def screenshot_window(
    hwnd: int | None = None,
    ref: str | None = None,
    restore_if_minimized: bool = True,
    max_side: int = 1400,
) -> Any:
    """창(또는 요소 하나)의 그림. 가려져 있어도 실제 내용이 찍힙니다. ref 는 없습니다.

    글자를 읽는 데는 snapshot·read_text 가 싸고 정확합니다. 그림 토큰은 픽셀 수에
    비례하므로, 한 부분만 볼 때는 ref 로 잘라내세요.

    Args:
        ref: 이 요소만 잘라냄.
        restore_if_minimized: 최소화된 창을 다른 창 뒤에서 잠깐 복원해 찍고 되돌림(포커스 그대로).
        max_side: 그림의 긴 변 최대 픽셀.
    """

    try:
        session = _session(hwnd)
        capture = await _run(
            capture_window, session.hwnd, restore_if_minimized=restore_if_minimized
        )
        notes: list[str] = []
        if ref:
            node, _element = await _run(session.resolve, ref)
            # Cropping needs screen pixels. A document rectangle would cut some
            # unrelated corner of the window and look like a real answer, so
            # the whole window comes back instead.
            if capture.restored_offscreen:
                # The element's rectangle was read while the window sat
                # minimised at -32000; the pixels come from where it was
                # briefly restored. Cropping one with the other cuts the
                # wrong place or fails, so the whole window is sent instead.
                notes.append("창이 최소화되어 있어 요소 위치를 알 수 없으므로 창 전체를 보냅니다.")
            elif node.rect and node.rect_space == "screen":
                capture = capture.crop(node.rect)
        png = await _run(capture.to_png, max_side=max_side)
        web = await _run(_has_web_area, session.hwnd)
    except ClaudeHandsError as exc:
        return _fail(exc)
    image = Image(data=png, format="png")
    if notes and not web:
        return [*notes, image]
    if web:
        # IE paints its page on the GPU, and PrintWindow does not see that
        # surface even with PW_RENDERFULLCONTENT — measured: the page area
        # came back blank with the window visible and with it minimised. The
        # picture is not wrong about the frame, only silent about the page,
        # so it is returned with that said.
        return [
            *notes,
            "이 창의 IE 웹 영역은 캡처에 비어 보입니다(IE 가 GPU 로 그려 창 캡처에 잡히지 "
            "않습니다). 페이지 내용은 snapshot / read_text 로 읽으세요.",
            image,
        ]
    return image


# --------------------------------------------------------------------------
# Acting
# --------------------------------------------------------------------------


@_tool()
async def click_element(
    ref: str,
    button: str = "left",
    double: bool = False,
    modifiers: str = "",
    hwnd: int | None = None,
) -> str:
    """요소를 누릅니다(커서를 움직이지 않고, 최소화돼 있어도 됨). 결과에 쓴 방식이 나옵니다.

    결재올림·발송처럼 문서를 다른 사람에게 넘기는 버튼은 누르지 않고 거절합니다.
    되돌릴 수 없는 일이라 사용자가 직접 누릅니다. 임시저장은 됩니다.

    Args:
        ref: snapshot·find_elements 의 ref (예: "e12").
        button: left / right / middle. 왼쪽은 UIA 패턴, 나머지는 창 메시지.
        double: 더블클릭.
        modifiers: 수식키, 쉼표 구분 (예: "ctrl,shift").
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


@_tool()
async def type_text(
    ref: str,
    text: str,
    clear: bool = True,
    submit: bool = False,
    hwnd: int | None = None,
    allow_focus: bool = False,
) -> str:
    """입력란(Office 문서면 슬라이드·문단·셀)에 글자를 넣고 되읽어 확인합니다.

    문자열을 통째로 넣어 한글 IME 문제가 없고 최소화돼 있어도 됩니다. 완료면 실제로
    들어간 것입니다. 값을 담을 수 없는 컨트롤이면 넣지 않고 실패합니다.

    Args:
        ref: 입력란의 ref.
        text: 넣을 문자열.
        clear: 기존 내용을 지울지(False 면 뒤에 붙임).
        submit: 넣은 뒤 Enter.
        allow_focus: 배경으로 넣을 방법이 없을 때 포커스를 가져와서라도 넣을지. 사용자의
            입력을 끊고 창이 앞으로 나오므로 기본은 끔.
    """

    try:
        _guard_write()
        session = _session(hwnd)
        result = await _run(
            _actions.type_text,
            session,
            ref,
            text,
            clear=clear,
            submit=submit,
            allow_focus=allow_focus,
        )
    except ClaudeHandsError as exc:
        return _fail(exc)
    return result.describe()


@_tool()
async def press_keys(
    keys: str,
    ref: str | None = None,
    repeat: int = 1,
    hwnd: int | None = None,
) -> str:
    """키를 창 메시지로 보냅니다 (예: "enter", "alt+f", "down down enter").

    단독 키(enter·tab·방향키·F1~F12·문자)와 alt 조합은 됩니다. ctrl·shift·win 조합은
    수식키가 빠진 채 도착합니다(결과에 경고). 단축키 대신 menu_select 나 버튼을 쓰세요.

    Args:
        keys: 공백/쉼표로 여러 조합을 순서대로.
        ref: 이 요소의 컨트롤에 보냄.
        repeat: 반복 횟수.
    """

    try:
        _guard_write()
        session = _session(hwnd)
        result = await _run(_actions.press_keys, session, keys, ref=ref, repeat=repeat)
    except ClaudeHandsError as exc:
        return _fail(exc)
    return result.describe()


@_tool()
async def scroll(
    direction: str = "down",
    amount: int = 3,
    ref: str | None = None,
    to_percent: float | None = None,
    hwnd: int | None = None,
) -> str:
    """스크롤합니다(Scroll 패턴, 없으면 휠 메시지).

    Args:
        direction: up / down / left / right.
        amount: 단계 수.
        ref: 스크롤할 영역 (생략 시 가장 큰 스크롤 영역).
        to_percent: 0~100 위치로 바로 (amount 무시).
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


@_tool()
async def set_element_state(
    ref: str,
    action: Literal[
        "select", "toggle", "check", "uncheck", "expand", "collapse", "focus", "value"
    ],
    value: str | None = None,
    hwnd: int | None = None,
) -> str:
    """체크박스·목록·트리 등의 상태를 직접 바꿉니다.

    Args:
        ref: 대상 요소의 ref.
        action: select / toggle / check / uncheck / expand / collapse / focus / value.
        value: action="value" 일 때 넣을 값.
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


@_tool()
async def call_control_method(
    method: str,
    args: list[str | int | float | bool | None] | None = None,
    ref: str | None = None,
    overwrite: bool = False,
    timeout: float = 30.0,
    max_chars: int = 8000,
    hwnd: int | None = None,
) -> str:
    """웹 화면에 박힌 ActiveX 컨트롤(한글 컨트롤·첨부 부품)의 메서드를 부릅니다.

    허용 목록만 됩니다 — 읽기(GetTextFile·GetFieldList·GetFieldText·GetPageText·
    PageCount 등), 쓰기(PutFieldText·MoveToField·SetTextFile·Insert·AddFile 등),
    저장(Save·SaveAs·CreatePageImage). 다른 파일 열기·인쇄·매크로·서버 전송은 없고,
    없는 이름을 부르면 허용 목록을 돌려줍니다. 컨트롤이 대화상자를 띄우면 timeout 뒤에
    알립니다. 경로는 절대 경로만, 있는 파일은 overwrite=True 일 때만 덮어씁니다.
    저장과 PutFieldText 는 결과를 확인합니다.

    Args:
        method: 메서드·속성 이름, 대소문자 구분 (예: "GetTextFile").
        args: 문자열·숫자·참거짓·null. 누름틀 여러 개는 이름과 값을 각각 \\u0002 로 잇습니다.
        ref: 컨트롤의 ref. 생략하면 그 메서드를 가진 컨트롤이 하나일 때 그것.
        overwrite: 이미 있는 파일을 덮어쓸지.
        timeout: 기다릴 최대 초.
        max_chars: 돌려줄 값의 최대 글자 수.
    """

    try:
        session = _session(hwnd)
        result = await _run(
            _actions.call_control,
            session,
            ref,
            method,
            list(args or []),
            overwrite=overwrite,
            timeout=max(1.0, min(float(timeout), 300.0)),
            read_only=READ_ONLY,
        )
    except ClaudeHandsError as exc:
        return _fail(exc)
    return result.describe() + _format_value(result.value, max_chars)


def _format_value(value: Any, max_chars: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        return f"\n값: {value!r}"
    if "\x02" in value:
        parts = value.split("\x02")
        value = f"(\\u0002 로 구분된 {len(parts)}개) " + " | ".join(parts)
    if len(value) > max_chars:
        return f"\n값 (전체 {len(value):,}자 중 앞 {max_chars:,}자):\n{value[:max_chars]}"
    return f"\n값:\n{value}" if "\n" in value else f"\n값: {value}"


@_tool()
async def menu_select(path: str, hwnd: int | None = None) -> str:
    """메뉴를 이름으로 따라 들어갑니다.

    Args:
        path: '>' 로 구분한 경로 (예: "파일 > 다른 이름으로 저장").
    """

    try:
        _guard_write()
        session = _session(hwnd)
        result = await _run(_actions.menu_select, session, path)
    except ClaudeHandsError as exc:
        return _fail(exc)
    return result.describe()


@_tool()
async def wait_for_element(
    query: str,
    role: str | None = None,
    timeout: float = 10.0,
    enabled: bool = False,
    vanish: bool = False,
    hwnd: int | None = None,
) -> str:
    """요소가 나타날(또는 사라질) 때까지 기다립니다.

    Args:
        query: 요소 이름.
        role: 종류 제한.
        timeout: 최대 초.
        enabled: 활성 상태가 될 때까지.
        vanish: 사라질 때까지 (진행 표시줄 등).
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


@_tool()
async def control_window(
    action: Literal["minimize", "restore", "maximize", "move", "close", "info"],
    hwnd: int | None = None,
    x: int = 0,
    y: int = 0,
    width: int | None = None,
    height: int | None = None,
) -> str:
    """창 자체를 다룹니다. 복원은 활성화 없이 하므로 사용자가 보던 창이 밀리지 않습니다.

    Args:
        action: minimize / restore / maximize / move / close / info.
        x, y, width, height: action="move" 의 위치·크기.
    """

    try:
        action = action.strip().lower()
        if action != "info":
            _guard_write()  # info only reads
        session = _session(hwnd)
        target = session.hwnd
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


# Every tool is registered by now; what tools/list advertises is settled here.
_compact_registered_schemas(mcp)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    force_utf8_output()
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
