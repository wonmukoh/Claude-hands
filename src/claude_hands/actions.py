"""High-level operations on an attached window.

Every action walks a strategy chain from most to least background-friendly:

1. **UIA pattern** — a direct method call into the app (``Invoke``, ``SetValue``,
   ``Toggle``…). Needs neither focus, nor visibility, nor a cursor.
2. **Window message** — ``PostMessage`` aimed at the exact child HWND under the
   element. Still no cursor, still no focus, but the app must be message-driven.
3. **Focused input** — only used when explicitly allowed, because it requires
   the control to hold keyboard focus.

The result says which rung was used, so a caller can tell a clean UIA invoke
from a best-effort synthetic click.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .elements import NodeInfo
from .session import WindowSession
from .win32.defs import ClaudeHandsError
from .win32.input import (
    click_at_screen_point,
    parse_keys,
    scroll_at_screen_point,
    screen_point_in_element,
    send_keys,
    send_text_chars,
    set_window_text,
)
from .win32.controls import button_check_state, is_button_class, press_button
from .win32.windows import Rect, deepest_child_at


class ActionFailedError(ClaudeHandsError):
    """Raised when every strategy for an action failed."""


@dataclass
class ActionResult:
    ok: bool
    action: str
    strategy: str
    target: str = ""
    detail: str = ""
    attempts: list[str] = field(default_factory=list)
    warning: str = ""
    """Said the action went through, but not that it will have the effect asked
    for. Used where the limit is known in advance and cannot be checked after."""
    value: Any = None
    """What a call answered, for actions that ask a question (``call_control``)."""

    def describe(self) -> str:
        status = "완료" if self.ok else "실패"
        line = f"{self.action} {status} — {self.target}" if self.target else f"{self.action} {status}"
        line += f" (방식: {self.strategy})"
        if self.detail:
            line += f"\n{self.detail}"
        if self.warning:
            line += f"\n주의: {self.warning}"
        if not self.ok and self.attempts:
            line += "\n시도한 방법: " + " → ".join(self.attempts)
        return line

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "action": self.action,
            "strategy": self.strategy,
            "target": self.target,
            "detail": self.detail,
            "attempts": self.attempts,
            "warning": self.warning,
            "value": self.value,
        }


def _target_label(node: NodeInfo) -> str:
    label = node.name or node.automation_id or node.class_name or "(이름 없음)"
    return f'{node.role} "{label}"'


def _owning_hwnd(session: WindowSession, node: NodeInfo) -> int:
    return node.hwnd or session.hwnd


def _live_rect(element, node: NodeInfo) -> Rect:
    """Read the element's rectangle now, not as it was when snapshotted.

    Anything that scrolls, resizes, or relayouts between the snapshot and the
    action would otherwise send the click to stale coordinates.
    """

    try:
        rect = _call(element, lambda: element.refresh().rect)
    except Exception:  # noqa: BLE001 - fall back to what the snapshot saw
        rect = None
    if rect is None or not rect.width or not rect.height:
        rect = node.rect
    if rect is None or not rect.width or not rect.height:
        raise ActionFailedError(
            f"{_target_label(node)} 의 화면 좌표를 알 수 없어 좌표 기반 조작을 할 수 없습니다."
        )
    return rect


def _element_point(element, node: NodeInfo) -> tuple[int, int]:
    return screen_point_in_element(_live_rect(element, node))


def _engine(element) -> str:
    """Which backend this element came from — reported so results never lie."""

    return getattr(element, "engine", "uia")


def _call(element, func, *args, **kwargs):
    """Run one backend call on the thread that backend needs.

    UI Automation calls are marshalled to the COM worker. Window-message calls
    must NOT be: ``SendMessage`` from inside a COM apartment fails with
    RPC_E_CANTCALLOUT_ININPUTSYNCCALL, and touching the worker at all would
    boot the COM stack that the win32 engine exists to avoid.
    """

    if _engine(element) == "win32":
        return func(*args, **kwargs)

    if _engine(element) == "mshtml":
        # IE's objects only work from the apartment kind they were fetched in.
        from .uia.core import get_sta_worker

        return get_sta_worker().call(func, *args, **kwargs)

    from .uia.core import get_worker

    return get_worker().call(func, *args, **kwargs)


# --------------------------------------------------------------------------
# Click
# --------------------------------------------------------------------------


def click(
    session: WindowSession,
    ref: str,
    *,
    button: str = "left",
    double: bool = False,
    modifiers: tuple[str, ...] = (),
    force_message: bool = False,
) -> ActionResult:
    """Activate an element — the default way to 'press' anything.

    For a left single click the UIA pattern route is preferred because it works
    on a minimised window; anything else (right click, double click, modifier
    click) falls straight through to window messages, which is the only way to
    express those.
    """

    node, element = session.resolve(ref)
    attempts: list[str] = []
    label = _target_label(node)
    _refuse_user_only(node.name, node.automation_id, node.role, _inside_pressable(element))

    if not node.enabled:
        raise ActionFailedError(f"{label} 은(는) 비활성 상태라 누를 수 없습니다.")

    simple_left = button == "left" and not double and not modifiers
    native = _win32_button(element, node)
    if simple_left and not force_message and native:
        attempts.append("message:BM_CLICK")
        press_button(native)
        return ActionResult(True, "click", "message:BM_CLICK", label, attempts=attempts)

    if simple_left and not force_message:
        for strategy, runner in _click_pattern_chain(element, node):
            attempts.append(strategy)
            try:
                _call(element, runner)
                return ActionResult(True, "click", strategy, label, attempts=attempts)
            except Exception as exc:  # noqa: BLE001 - try the next strategy
                attempts[-1] = f"{strategy}(실패: {_short(exc)})"

    if button == "right" and not force_message:
        context_label = f"{_engine(element)}:context-menu"
        attempts.append(context_label)
        try:
            _call(element, lambda: _show_context_menu(element))
            return ActionResult(True, "click", context_label, label, attempts=attempts)
        except Exception as exc:  # noqa: BLE001
            attempts[-1] = f"{context_label}(실패: {_short(exc)})"

    if node.rect_space != "screen":
        # The only route left aims at a screen coordinate, and this element's
        # rectangle is not one. Clicking it would press whatever happens to sit
        # at that spot on the desktop, which could be another program entirely.
        raise ActionFailedError(
            f"{label} 은(는) 화면 좌표가 없어 좌표 클릭으로 누를 수 없습니다 "
            f"(좌표계={node.rect_space}).\n"
            "시도한 방법: " + (" → ".join(attempts) if attempts else "없음") + "\n"
            "문서 안의 요소라면 값을 바꾸는 type_text 를 쓰거나, 리본·메뉴를 눌러야 하면 "
            "attach_window(engine='uia') 로 다시 연결하세요."
        )

    # Message-based click at the element's centre.
    attempts.append("message:click")
    scroll_into_view(session, ref, quiet=True)
    x, y = _element_point(element, node)
    target_hwnd = deepest_child_at(_owning_hwnd(session, node), x, y)
    click_at_screen_point(
        _owning_hwnd(session, node),
        x,
        y,
        button=button,
        double=double,
        modifiers=modifiers,
        target_hwnd=target_hwnd,
    )
    return ActionResult(
        True,
        "click",
        "message:click",
        label,
        detail=f"좌표 {x},{y} → hwnd={target_hwnd}",
        attempts=attempts,
    )


USER_ONLY_LABELS = {
    # Sending a draft on, for approval or out of the school.
    "결재올림", "결재상신", "상신", "발송", "발송처리", "발송의뢰", "즉시발송", "일괄발송",
    # Approving someone else's document. In K-에듀파인's 결재 window there is no
    # button by those names: 문서처리 opens the approval dialog, and when the
    # approval line has changed it submits after a single confirm.
    "결재", "결재하기", "승인", "전결", "대결", "연속결재", "일괄결재", "일괄과제결재", "문서처리",
    # Acting on it outward from the 결재 window: asking the drafter to redo
    # the approval line, and posting it to the 공문 board.
    "재지정요청", "공문게시지정",
}
"""Buttons that commit the user to a document, said by their whole label.

The user's words: "발송은 내가 하더라도 임시저장은 하게 해줘", and later "결재(승인)
버튼도 내가 직접 누를게". Preparing a draft and saving it is this tool's job;
sending it on or signing someone else's is the user's, because neither can be
taken back. Only a whole label counts: 결재경로, 결재정보 and 나의결재선 are
drafting, and stay pressable.
"""

CHOOSING_ROLES = {"listitem", "radiobutton", "checkbox", "combobox", "edit", "tabitem", "treeitem"}
"""Roles that only choose, never commit. The approver dialog's 결재유형 list
offers 결재·전결·대결 as options, and picking one is part of drafting."""

USER_ONLY_IDS = {"sanctn", "processdoc", "requestmodifyline", "ofldcntt", "ofldcnttspan"}
"""The same buttons by DOM id, lower-cased: K-에듀파인's 결재올림 (기안 window),
and 문서처리, 재지정요청 and 공문게시지정 with its caption span (결재 window)."""

ALLOW_SEND_ENV = "CLAUDE_HANDS_ALLOW_SEND"


class UserOnlyActionError(ActionFailedError):
    """Raised for a button the user has kept for themselves."""


def _bare_labels(text: str) -> set[str]:
    # "결재올림(F2)", "[발송]", "발 송" all name the same button: a bracketed
    # hint is dropped, unless the brackets hold the whole label.
    def bare(value: str) -> str:
        return re.sub(r"[^0-9A-Za-z가-힣]", "", value)

    return {bare(re.sub(r"[(\[（［].*?[)\]）］]", "", text or "")), bare(text or "")}


def _refuse_user_only(
    name: str, automation_id: str = "", role: str = "", inside_pressable: bool = True
) -> None:
    """Refuse to press 결재올림·발송·결재 and their kin.

    Refused, not warned about: a warning arrives after the click, and by then
    the document has gone. Plain text counts as well as buttons and links,
    because a page lists a link's caption a second time as text, and clicking
    that text lands on the link. Only whoever starts the server can lift this,
    with ``CLAUDE_HANDS_ALLOW_SEND`` — a caller cannot argue its way past it.
    """

    if os.environ.get(ALLOW_SEND_ENV, "").lower() in {"1", "true", "yes"}:
        return
    if role in CHOOSING_ROLES:
        return
    if role == "text" and not inside_pressable:
        # A table cell reading 결재 in the 결재경로 table presses nothing.
        return
    if _bare_labels(name) & USER_ONLY_LABELS or (automation_id or "").lower() in USER_ONLY_IDS:
        shown = (name or automation_id).strip()
        raise UserOnlyActionError(
            f"「{shown}」은(는) 사용자가 직접 누르는 버튼이라 이 도구는 누르지 않습니다. "
            "결재를 올리거나 승인하거나 문서를 보내는 일은 되돌릴 수 없어서 사용자의 몫입니다. "
            "준비가 끝났다고 사용자에게 알리세요. 저장만 필요하면 임시저장을 누르면 됩니다."
        )


def _inside_pressable(element) -> bool:
    # Only the web engine knows where a piece of text sits; anywhere else,
    # text is assumed to be a caption, which is the cautious answer.
    return bool(getattr(element, "inside_pressable", True))


def _win32_button(element, node: NodeInfo) -> int:
    """The HWND of a classic Win32 button behind a UIA element, or 0.

    UI Automation's proxy for these activates the window when it invokes or
    toggles them. Measured with the user's Acrobat in front: `uia:toggle` on a
    plain checkbox brought its window to the foreground, and `uia:invoke` on a
    message box's 확인 handed the foreground to the box. The same buttons
    pressed with BM_CLICK left the foreground alone. These are the most common
    controls there are — every message box is made of them — so they go
    through the button's own message first.
    """

    if _engine(element) != "uia" or not node.hwnd:
        return 0
    if node.role not in {"button", "checkbox", "radiobutton"}:
        return 0
    if (node.class_name or "").lower() != "button":
        return 0
    try:
        return node.hwnd if is_button_class(node.hwnd) else 0
    except Exception:  # noqa: BLE001 - a window that vanished is not a button
        return 0


def _session_engine(session: WindowSession) -> str:
    """The engine a session last used, for results not tied to one element."""

    return getattr(session, "active_engine", "") or getattr(session, "engine", "uia")


def _click_pattern_chain(element, node: NodeInfo):
    """Yield ``(name, callable)`` click strategies best-first for this element."""

    chain: list[tuple[str, Any]] = []
    engine = _engine(element)

    if node.role in {"checkbox", "radiobutton"} or (
        node.toggle_state is not None and node.role not in {"button", "splitbutton", "menuitem"}
    ):
        toggle = element.pattern("toggle")
        if toggle is not None:
            chain.append((f"{engine}:toggle", lambda: toggle.Toggle()))

    if node.role in {"listitem", "treeitem", "tabitem", "dataitem"}:
        selection = element.pattern("selectionitem")
        if selection is not None:
            chain.append((f"{engine}:select", lambda: selection.Select()))

    invoke = element.pattern("invoke")
    if invoke is not None:
        chain.append((f"{engine}:invoke", lambda: invoke.Invoke()))

    if node.role in {"combobox", "menuitem", "treeitem", "splitbutton"}:
        expand = element.pattern("expandcollapse")
        if expand is not None:
            chain.append((f"{engine}:expand", lambda: _toggle_expand(expand)))

    selection = element.pattern("selectionitem")
    if selection is not None and not any(name == "uia:select" for name, _ in chain):
        chain.append((f"{engine}:select", lambda: selection.Select()))

    legacy = element.pattern("legacy")
    if legacy is not None:
        chain.append((f"{engine}:legacy-default-action", lambda: legacy.DoDefaultAction()))

    return chain


def _toggle_expand(pattern) -> None:
    state = int(pattern.CurrentExpandCollapseState)
    if state == 1:  # expanded
        pattern.Collapse()
    else:
        pattern.Expand()


def _show_context_menu(element) -> None:
    from .uia.core import get_automation

    module, _ = get_automation()
    interface = getattr(module, "IUIAutomationElement3", None)
    if interface is None:
        raise ActionFailedError("이 Windows 버전은 UIA 컨텍스트 메뉴 호출을 지원하지 않습니다.")
    element.com.QueryInterface(interface).ShowContextMenu()


def _short(exc: BaseException, limit: int = 80) -> str:
    text = " ".join(str(exc).split())
    return text[:limit] + ("…" if len(text) > limit else "")


# --------------------------------------------------------------------------
# Text entry
# --------------------------------------------------------------------------


UNPOSTABLE_MODIFIERS = {"ctrl", "shift", "win"}
"""Modifiers a posted message cannot make an application believe in.

An accelerator table asks ``GetKeyState`` what is physically held down, and
posting WM_KEYDOWN for Ctrl does not change that. Alt is the exception: it goes
as WM_SYSKEYDOWN, which menus handle directly.
"""

TEXT_ENTRY_ROLES = {"edit", "combobox", "document", "spinner", "searchbox"}
"""Roles where a window's text is its contents rather than its label.

Anywhere else, setting the window text renames the control instead of filling
it in, so the fallbacks that work that way are not offered.
"""


def type_text(
    session: WindowSession,
    ref: str,
    text: str,
    *,
    clear: bool = True,
    submit: bool = False,
    allow_focus: bool = False,
) -> ActionResult:
    """Put text into an element.

    ``ValuePattern.SetValue`` is atomic and IME-safe — it hands the app the
    finished string, so Korean/Japanese input needs no composition at all, and
    it works while the window is minimised.

    Typing character by character needs keyboard focus, which means taking it
    from whatever the person is doing. That is contract 2, so it is off unless
    the caller asks for it. It defaulted to on, and the documentation said the
    opposite — anything without a value pattern quietly pulled its window
    forward mid-sentence. Failing with an explanation is the better answer:
    the caller can decide whether interrupting is worth it.
    """

    node, element = session.resolve(ref)
    label = _target_label(node)
    attempts: list[str] = []

    if not node.enabled:
        raise ActionFailedError(f"{label} 은(는) 비활성 상태라 입력할 수 없습니다.")

    engine = _engine(element)

    def took(pattern, expected: str) -> bool:
        """Did the write land, as far as the control will say?

        A set that returns without raising has not necessarily done anything —
        measured: `legacy.SetValue` on a button reports success and changes
        nothing, so `type_text` answered "입력 완료" for a control that cannot
        hold text. Text is one of the few things that can be checked, so it is.

        A control that will not report its value at all counts as success: the
        write may well have landed, and turning an unverifiable call into a
        failure would break the ones that work.
        """

        try:
            actual = _call(element, lambda: pattern.CurrentValue)
        except Exception:  # noqa: BLE001 - no readback available
            return True
        if actual is None:
            return True
        actual = str(actual)
        if expected and not actual:
            # Nothing came back where something was just written. An empty
            # string is "in" every string, so a plain containment test calls
            # this a success — which is how a button reported 입력 완료.
            return False
        # Controls reformat what they are given — trimming, padding a number,
        # truncating at a maximum length — so containment rather than equality.
        return expected in actual or actual in expected

    value_pattern = element.pattern("value")
    if value_pattern is not None:
        set_value_label = f"{engine}:value.SetValue"
        attempts.append(set_value_label)
        try:
            is_readonly = False
            try:
                is_readonly = bool(_call(element, lambda: value_pattern.CurrentIsReadOnly))
            except Exception:  # noqa: BLE001 - property missing on some apps
                is_readonly = False
            if is_readonly:
                raise ActionFailedError("읽기 전용 필드입니다.")
            new_text = text if clear else (node.value or "") + text
            _call(element, lambda: value_pattern.SetValue(new_text))
            if not took(value_pattern, new_text):
                raise ActionFailedError("값이 반영되지 않았습니다.")
            if submit:
                send_keys(_owning_hwnd(session, node), "enter")
            return ActionResult(
                True, "type", set_value_label, label,
                detail=f"{len(new_text)}자 입력", attempts=attempts,
            )
        except Exception as exc:  # noqa: BLE001
            attempts[-1] = f"{set_value_label}(실패: {_short(exc)})"

    legacy = element.pattern("legacy")
    if legacy is not None:
        legacy_label = f"{engine}:legacy.SetValue"
        attempts.append(legacy_label)
        try:
            new_text = text if clear else (node.value or "") + text
            _call(element, lambda: legacy.SetValue(new_text))
            if not took(legacy, new_text):
                raise ActionFailedError("값이 반영되지 않았습니다.")
            if submit:
                send_keys(_owning_hwnd(session, node), "enter")
            return ActionResult(
                True, "type", legacy_label, label,
                detail=f"{len(new_text)}자 입력", attempts=attempts,
            )
        except Exception as exc:  # noqa: BLE001
            attempts[-1] = f"{legacy_label}(실패: {_short(exc)})"

    hwnd = node.hwnd
    # WM_SETTEXT sets a window's caption, and on a control that is not a text
    # box that is its label rather than its contents — sending it to a button
    # renames the button. That is a visible change to the person's program
    # that nobody asked for, so it is only offered where "the window's text"
    # and "what the user typed" are the same thing.
    if hwnd and clear and node.role in TEXT_ENTRY_ROLES:
        attempts.append("message:WM_SETTEXT")
        try:
            if set_window_text(hwnd, text):
                if submit:
                    send_keys(hwnd, "enter")
                return ActionResult(
                    True, "type", "message:WM_SETTEXT", label,
                    detail=f"{len(text)}자 입력", attempts=attempts,
                )
        except Exception as exc:  # noqa: BLE001
            attempts[-1] = f"message:WM_SETTEXT(실패: {_short(exc)})"

    tried = " → ".join(attempts) if attempts else "없음"
    if node.role not in TEXT_ENTRY_ROLES:
        # Nothing here can put text into, say, a button. The remaining routes
        # would rename it or send keystrokes it ignores, so the honest answer
        # is that this element is the wrong target.
        raise ActionFailedError(
            f"{label} 은(는) 글자를 담는 요소가 아닙니다 (역할={node.role}).\n"
            f"시도한 방법: {tried}\n"
            "버튼이라면 click_element 를, 목록이나 체크박스라면 set_element_state 를 "
            "쓰세요. 입력란을 찾으려면 find_elements(role='edit') 로 확인하세요."
        )

    if not allow_focus:
        raise ActionFailedError(
            f"{label} 에 배경 상태로 입력할 방법이 없습니다 (Value 패턴 없음).\n"
            f"시도한 방법: {tried}\n"
            "남은 방법은 포커스를 가져와 한 글자씩 보내는 것뿐인데, 그러면 사용자가 "
            "하던 입력이 끊기고 이 창이 앞으로 나옵니다. 그래도 괜찮다면 "
            "allow_focus=True 로 다시 부르세요."
        )

    attempts.append("focus+message:WM_CHAR")
    focus(session, ref)
    target = node.hwnd or session.hwnd
    if clear:
        send_keys(target, "ctrl+a")
        send_keys(target, "delete")
    send_text_chars(target, text)
    if submit:
        send_keys(target, "enter")
    return ActionResult(
        True,
        "type",
        "focus+message:WM_CHAR",
        label,
        detail=(
            f"{len(text)}자 입력. 이 방식은 대상 컨트롤이 키보드 포커스를 가져야 하므로 "
            "창이 잠시 활성화될 수 있습니다."
        ),
        attempts=attempts,
    )


def set_value(session: WindowSession, ref: str, value: str | float) -> ActionResult:
    """Set a value directly (edit boxes, sliders, spinners)."""

    node, element = session.resolve(ref)
    label = _target_label(node)

    range_pattern = element.pattern("rangevalue")
    if range_pattern is not None:
        try:
            _call(element, lambda: range_pattern.SetValue(float(value)))
            return ActionResult(True, "set_value", f"{_engine(element)}:rangevalue", label, detail=str(value))
        except Exception as exc:  # noqa: BLE001
            raise ActionFailedError(f"{label} 값 설정 실패: {_short(exc)}") from exc

    return type_text(session, ref, str(value), clear=True)


# --------------------------------------------------------------------------
# Keyboard
# --------------------------------------------------------------------------


def press_keys(
    session: WindowSession,
    keys: str,
    *,
    ref: str | None = None,
    repeat: int = 1,
) -> ActionResult:
    """Post a key chord to the window (or to one element's HWND).

    Alt is delivered as ``WM_SYSKEYDOWN``, which classic menus act on, so
    ``alt+f`` genuinely opens a File menu. Ctrl, Shift and Win are posted as
    ordinary key messages, and an accelerator table decides what a shortcut
    means by reading the *physical* keyboard with ``GetKeyState`` — which
    posting never touches. Those chords arrive with the modifier ignored.
    """

    target_hwnd = session.hwnd
    label = "창"
    if ref:
        node, _element = session.resolve(ref)
        # Enter on a focused link presses it as surely as a click does.
        _refuse_user_only(node.name, node.automation_id, node.role, _inside_pressable(_element))
        target_hwnd = _owning_hwnd(session, node)
        label = _target_label(node)

    ineffective = sorted(
        {m for stroke in parse_keys(keys) for m in stroke.modifiers if m in UNPOSTABLE_MODIFIERS}
    )

    sent: list[str] = []
    for _ in range(max(1, repeat)):
        sent = send_keys(target_hwnd, keys)

    warning = ""
    if ineffective:
        # Nothing here can be checked afterwards, and the limit is known in
        # advance, so say it rather than let the caller read "완료" and assume.
        warning = (
            f"{'/'.join(ineffective)} 는 이 방식으로 앱에 전달되지 않습니다. "
            "단축키 표는 물리 키보드 상태를 보는데 메시지 전송은 그것을 바꾸지 않습니다. "
            "명령을 실행하려면 menu_select 나 버튼 클릭을 쓰세요. "
            "alt 조합과 단독 키(enter·tab·방향키·F1~F12·문자)는 그대로 동작합니다."
        )
    return ActionResult(
        True,
        "keys",
        "message:WM_KEYDOWN",
        label,
        detail=f"{' '.join(sent)} × {max(1, repeat)} → hwnd={target_hwnd}",
        warning=warning,
    )


def focus(session: WindowSession, ref: str) -> ActionResult:
    """Give an element keyboard focus (may raise the window — that is Windows)."""

    node, element = session.resolve(ref)
    label = _target_label(node)
    # Focus it, then send Enter to the window: the same press by two steps.
    _refuse_user_only(node.name, node.automation_id, node.role, _inside_pressable(element))
    try:
        _call(element, lambda: element.com.SetFocus())
        return ActionResult(True, "focus", f"{_engine(element)}:SetFocus", label)
    except Exception as exc:  # noqa: BLE001
        raise ActionFailedError(f"{label} 에 포커스를 줄 수 없습니다: {_short(exc)}") from exc


# --------------------------------------------------------------------------
# Scrolling
# --------------------------------------------------------------------------

_SCROLL_AMOUNTS = {
    "up": ("vertical", -1),
    "down": ("vertical", 1),
    "left": ("horizontal", -1),
    "right": ("horizontal", 1),
}


def scroll(
    session: WindowSession,
    *,
    ref: str | None = None,
    direction: str = "down",
    amount: int = 3,
    to_percent: float | None = None,
) -> ActionResult:
    """Scroll a container, by wheel clicks or straight to a percentage."""

    if direction not in _SCROLL_AMOUNTS:
        raise ActionFailedError(f"방향은 up/down/left/right 중 하나여야 합니다: {direction!r}")

    if ref:
        node, element = session.resolve(ref)
    else:
        node, element = _scrollable_root(session)
    label = _target_label(node)
    attempts: list[str] = []

    pattern = element.pattern("scroll")
    if pattern is not None:
        from .uia.core import get_automation

        module, _ = get_automation()
        axis, sign = _SCROLL_AMOUNTS[direction]
        attempts.append(f"{_engine(element)}:scroll")
        try:
            if to_percent is not None:
                percent = max(0.0, min(100.0, float(to_percent)))
                no_scroll = getattr(module, "ScrollPatternNoScroll", -1)
                horizontal = percent if axis == "horizontal" else no_scroll
                vertical = percent if axis == "vertical" else no_scroll
                _call(element, lambda: pattern.SetScrollPercent(horizontal, vertical))
                return ActionResult(
                    True, "scroll", f"{_engine(element)}:scroll", label, detail=f"{axis} {percent}%", attempts=attempts
                )
            small_inc = getattr(module, "ScrollAmount_SmallIncrement", 4)
            small_dec = getattr(module, "ScrollAmount_SmallDecrement", 1)
            no_amount = getattr(module, "ScrollAmount_NoAmount", 2)
            step = small_inc if sign > 0 else small_dec
            horizontal = step if axis == "horizontal" else no_amount
            vertical = step if axis == "vertical" else no_amount
            for _ in range(max(1, amount)):
                _call(element, lambda: pattern.Scroll(horizontal, vertical))
            return ActionResult(
                True, "scroll", f"{_engine(element)}:scroll", label,
                detail=f"{direction} × {amount}", attempts=attempts,
            )
        except Exception as exc:  # noqa: BLE001
            attempts[-1] = f"uia:scroll(실패: {_short(exc)})"

    attempts.append("message:wheel")
    x, y = _element_point(element, node)
    axis, sign = _SCROLL_AMOUNTS[direction]
    if axis == "horizontal":
        clicks = amount if sign > 0 else -amount  # WM_MOUSEHWHEEL: positive = right
    else:
        clicks = -amount if sign > 0 else amount  # WM_MOUSEWHEEL: positive = up
    target = scroll_at_screen_point(
        _owning_hwnd(session, node),
        x,
        y,
        clicks=clicks,
        horizontal=(axis == "horizontal"),
    )
    return ActionResult(
        True, "scroll", "message:wheel", label,
        detail=f"{direction} × {amount} → hwnd={target}", attempts=attempts,
    )


def _scrollable_root(session: WindowSession):
    """Pick the biggest element that actually has a scroll pattern."""

    if session.tree is None:
        session.capture_tree()
    assert session.tree is not None
    best: Optional[NodeInfo] = None
    for node in session.tree.walk():
        if "scroll" in node.patterns and node.rect:
            area = node.rect.width * node.rect.height
            if best is None or (best.rect and area > best.rect.width * best.rect.height):
                best = node
    node = best or session.tree
    return node, session.element_for(node)


def scroll_into_view(session: WindowSession, ref: str, *, quiet: bool = False) -> Optional[ActionResult]:
    """Bring an element into its container's viewport before acting on it."""

    try:
        node, element = session.resolve(ref)
        pattern = element.pattern("scrollitem")
        if pattern is None:
            return None
        _call(element, lambda: pattern.ScrollIntoView())
        return ActionResult(True, "scroll_into_view", f"{_engine(element)}:scrollitem", _target_label(node))
    except Exception:  # noqa: BLE001 - purely an optimisation
        if quiet:
            return None
        raise


# --------------------------------------------------------------------------
# Selection / toggles / expansion
# --------------------------------------------------------------------------


def select(session: WindowSession, ref: str, *, add: bool = False) -> ActionResult:
    node, element = session.resolve(ref)
    label = _target_label(node)
    pattern = element.pattern("selectionitem")
    if pattern is None:
        return click(session, ref)
    try:
        _call(element, lambda: pattern.AddToSelection() if add else pattern.Select())
        return ActionResult(True, "select", f"{_engine(element)}:selectionitem", label)
    except Exception as exc:  # noqa: BLE001
        raise ActionFailedError(f"{label} 선택 실패: {_short(exc)}") from exc


def toggle(session: WindowSession, ref: str, *, to: bool | None = None) -> ActionResult:
    node, element = session.resolve(ref)
    label = _target_label(node)
    native = _win32_button(element, node)
    if native:
        return _toggle_button(native, label, to)
    pattern = element.pattern("toggle")
    if pattern is None:
        return click(session, ref)
    try:
        current = _call(element, lambda: int(pattern.CurrentToggleState))
        if to is not None:
            want = 1 if to else 0
            if current == want:
                return ActionResult(
                    True, "toggle", "noop", label, detail=f"이미 {'켜짐' if to else '꺼짐'} 상태"
                )
            for _ in range(3):
                _call(element, lambda: pattern.Toggle())
                if int(_call(element, lambda: pattern.CurrentToggleState)) == want:
                    break
        else:
            _call(element, lambda: pattern.Toggle())
        state = _call(element, lambda: int(pattern.CurrentToggleState))
        from .uia.core import TOGGLE_STATES

        return ActionResult(
            True, "toggle", f"{_engine(element)}:toggle", label, detail=f"현재 상태: {TOGGLE_STATES.get(state, state)}"
        )
    except Exception as exc:  # noqa: BLE001
        raise ActionFailedError(f"{label} 토글 실패: {_short(exc)}") from exc


def _toggle_button(hwnd: int, label: str, to: bool | None) -> ActionResult:
    """Toggle a Win32 checkbox or radio button without activating its window."""

    from .uia.core import TOGGLE_STATES

    current = button_check_state(hwnd)
    if to is not None:
        want = 1 if to else 0
        if current == want:
            return ActionResult(
                True, "toggle", "noop", label, detail=f"이미 {'켜짐' if to else '꺼짐'} 상태"
            )
        # A three-state box passes through "indeterminate" on the way.
        for _ in range(3):
            press_button(hwnd)
            if button_check_state(hwnd) == want:
                break
    else:
        press_button(hwnd)
    state = button_check_state(hwnd)
    return ActionResult(
        True, "toggle", "message:BM_CLICK", label, detail=f"현재 상태: {TOGGLE_STATES.get(state, state)}"
    )


def expand(session: WindowSession, ref: str, *, collapse: bool = False) -> ActionResult:
    node, element = session.resolve(ref)
    label = _target_label(node)
    pattern = element.pattern("expandcollapse")
    if pattern is None:
        return click(session, ref)
    try:
        _call(element, lambda: pattern.Collapse() if collapse else pattern.Expand())
        return ActionResult(
            True, "collapse" if collapse else "expand", f"{_engine(element)}:expandcollapse", label
        )
    except Exception as exc:  # noqa: BLE001
        raise ActionFailedError(f"{label} {'접기' if collapse else '펼치기'} 실패: {_short(exc)}") from exc


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def get_text(session: WindowSession, ref: str | None = None, *, max_chars: int = 8000) -> str:
    """Read an element's text: TextPattern first, then value, then name."""

    if ref:
        node, element = session.resolve(ref)
    else:
        if session.tree is None:
            session.capture_tree()
        assert session.tree is not None
        node = session.tree
        element = session.element_for(node)

    text_pattern = element.pattern("text")
    if text_pattern is not None:
        try:
            text = _call(element, lambda: text_pattern.DocumentRange.GetText(max_chars))
            if text:
                return str(text)
        except Exception:  # noqa: BLE001 - fall through to simpler reads
            pass

    value = element.value()
    if value:
        return value

    # A container's own name is its title, not its contents, and the title is
    # almost never what someone reading a window wanted: an Office document
    # root answers with its filename, so `read_text()` on a whole presentation
    # used to return "발표.pptx" and stop. Gather what is inside first and keep
    # the title as the last resort.
    stitched = _stitch_text(node, max_chars)
    if stitched:
        return stitched
    return node.name


TEXT_BEARING_ROLES = {"text", "edit", "document", "listitem", "treeitem", "dataitem"}


def _stitch_text(node: NodeInfo, max_chars: int) -> str:
    """Join the text held below a container, in the order it is laid out.

    A node's value is its content where the backend supplies one — a slide
    shape's text, an edit box's contents. Names only stand in for it on the
    roles that are text in the first place, because elsewhere the name is a
    label ("TextBox 4") rather than anything a reader wants.
    """

    pieces = []
    for child in node.walk():
        if child is node:
            continue
        text = (child.value or "").strip()
        if not text and "value" not in child.patterns and child.role in TEXT_BEARING_ROLES:
            # An element that can hold a value and holds none is empty, not
            # unnamed — reporting its label instead put "Rectangle 2" in the
            # middle of a slide's text. Names only stand in where the backend
            # has no value to give at all, as with static labels.
            text = (child.name or "").strip()
        if text:
            pieces.append(text)
    return "\n".join(dict.fromkeys(pieces))[:max_chars]


# --------------------------------------------------------------------------
# Waiting and menus
# --------------------------------------------------------------------------


def wait_for(
    session: WindowSession,
    query: str,
    *,
    role: str | None = None,
    timeout: float = 10.0,
    poll: float = 0.4,
    enabled: bool = False,
    vanish: bool = False,
) -> ActionResult:
    """Poll the UI tree until an element appears (or disappears)."""

    deadline = time.monotonic() + timeout
    last: Optional[NodeInfo] = None
    while time.monotonic() < deadline:
        matches = session.find(query, role=role, limit=3)
        hit = None
        for score, node in matches:
            if score < 0.5:
                continue
            if enabled and not node.enabled:
                continue
            hit = node
            break
        if vanish and hit is None:
            return ActionResult(True, "wait_for", "poll", query, detail="요소가 사라졌습니다.")
        if not vanish and hit is not None:
            return ActionResult(
                True, "wait_for", "poll", _target_label(hit), detail=f"ref={hit.ref}"
            )
        last = hit
        time.sleep(poll)

    state = "사라지지 않았습니다" if vanish else "나타나지 않았습니다"
    raise ActionFailedError(
        f"{timeout:.0f}초 안에 {query!r} 이(가) {state}."
        + (f" 마지막으로 본 상태: {_target_label(last)}" if last else "")
    )


def menu_select(session: WindowSession, path: str, *, separator: str = ">") -> ActionResult:
    """Walk a menu by name, e.g. ``"파일 > 다른 이름으로 저장"``.

    Each step expands (or invokes) the matching menu item, then re-snapshots so
    the next level is visible. Menus are UIA-native, so this works on a window
    that never comes to the front.
    """

    steps = [part.strip() for part in path.split(separator) if part.strip()]
    if not steps:
        raise ActionFailedError("메뉴 경로가 비어 있습니다. 예: '파일 > 저장'")

    # A real menu bar is the dependable route: posting the WM_COMMAND the menu
    # itself would post works while the window is minimised, and unlike a
    # keyboard accelerator it does not depend on physical key state.
    from .win32.menus import find_menu_item, invoke_menu_item, read_menu

    try:
        menu = read_menu(session.hwnd)
    except ClaudeHandsError:
        menu = []
    _refuse_user_only(steps[-1])
    if menu:
        item = find_menu_item(menu, steps)
        _refuse_user_only(item.label)
        command_id = invoke_menu_item(session.hwnd, item)
        return ActionResult(
            True,
            "menu_select",
            "menu:WM_COMMAND",
            " > ".join(steps),
            detail=f"{item.label!r} (명령 id={command_id})",
        )

    walked: list[str] = []
    for index, step in enumerate(steps):
        matches = session.find(step, limit=8)
        candidates = [
            node
            for _score, node in matches
            if node.role in {"menuitem", "menubar", "menu", "button", "splitbutton", "listitem"}
        ]
        if not candidates:
            candidates = [node for _score, node in matches]
        if not candidates:
            raise ActionFailedError(
                f"메뉴 항목 {step!r} 을(를) 찾지 못했습니다. 이미 지나온 경로: {' > '.join(walked) or '(없음)'}"
            )
        node = candidates[0]
        last_step = index == len(steps) - 1
        if last_step:
            click(session, node.ref)
        else:
            element = session.element_for(node)
            pattern = element.pattern("expandcollapse")
            if pattern is not None:
                _call(element, lambda: pattern.Expand())
            else:
                click(session, node.ref)
            time.sleep(0.25)
        walked.append(step)

    return ActionResult(True, "menu_select", f"{_session_engine(session)}:menu", " > ".join(walked))


# --------------------------------------------------------------------------
# Methods of a page's ActiveX controls
# --------------------------------------------------------------------------


def _control_pattern(element):
    # Only the web engine has controls to call; asking UIA for an unknown
    # pattern is an error, not a None.
    if _engine(element) != "mshtml":
        return None
    return element.pattern("control")


def _control_target(session: WindowSession, ref: str | None, method: str):
    """The control to call: the one named, or the only one that has ``method``."""

    if ref:
        node, element = session.resolve(ref)
        pattern = _control_pattern(element)
        if pattern is None:
            raise ActionFailedError(
                f"{_target_label(node)} 은(는) 메서드를 부를 수 있는 컨트롤이 아닙니다. "
                "snapshot 에서 custom 역할로 나온 한글 컨트롤·첨부 부품의 ref 를 쓰세요."
                + (f" ({node.help_text})" if node.help_text else "")
            )
        return node, element, pattern

    def candidates():
        found = []
        for name, node in session.refs.items():
            element = session.elements.get(id(node))
            pattern = _control_pattern(element) if element is not None else None
            if pattern is not None:
                found.append((name, node, pattern))
        return found

    found = candidates()
    if not found:
        session.capture_tree()
        found = candidates()
    if not found:
        raise ActionFailedError(
            "이 창에서 메서드를 부를 수 있는 컨트롤을 찾지 못했습니다 "
            f"(엔진={_session_engine(session)}). IE 웹 화면 안의 한글 컨트롤·첨부 부품만 됩니다."
        )
    fitting = [entry for entry in found if method in entry[2].control.methods]
    if len(fitting) == 1 or len(found) == 1:
        name = (fitting or found)[0][0]
        node, element = session.resolve(name)
        return node, element, _control_pattern(element)
    listed = ", ".join(f"{name} ({pattern.control.label})" for name, _node, pattern in (fitting or found))
    raise ActionFailedError(f"컨트롤이 여럿입니다. ref 를 지정하세요: {listed}")


def _field_parts(text) -> list[str]:
    from .office.hwp import FIELD_SEPARATOR

    return [part.replace("\r\n", "\n").strip() for part in str(text or "").split(FIELD_SEPARATOR)]


def call_control(
    session: WindowSession,
    ref: str | None,
    method: str,
    args: list | tuple = (),
    *,
    overwrite: bool = False,
    timeout: float = 30.0,
    read_only: bool = False,
) -> ActionResult:
    """Call one allowlisted method of a control embedded in a web page.

    Everything that can be checked is checked before the page sees the call
    (method, arguments, paths), and everything that can be confirmed after it
    is: a saved file must exist and be new, and 누름틀 text must read back.
    """

    from .mshtml.activex import READ, WRITE, check_call, lookup

    node, element, pattern = _control_target(session, ref, method)
    control = pattern.control
    spec = lookup(control, method)
    if read_only and spec.kind != READ:
        raise ActionFailedError(
            f"읽기 전용 모드라 {method} 는 부를 수 없습니다. 읽기 메서드만 됩니다:\n{control.describe()}"
        )
    checked = check_call(spec, method, list(args), overwrite=overwrite)
    label = _target_label(node)
    strategy = f"{_engine(element)}:control.{method}"

    started = time.time()
    value = _call(element, lambda: pattern.Call(method, spec, checked, timeout))
    result = ActionResult(True, "call", strategy, label, value=value)

    if spec.path_out is not None:
        path = checked[spec.path_out]
        try:
            stat = os.stat(path)
        except OSError:
            stat = None
        if value is False or stat is None or stat.st_mtime < started - 2:
            result.ok = False
            result.detail = (
                f"{path} 에 새 파일이 생기지 않았습니다 (컨트롤의 답: {value!r}). "
                "형식 이름이 맞는지, 폴더에 쓸 수 있는지 확인하세요."
            )
        else:
            result.detail = f"저장 확인: {path} ({stat.st_size:,}바이트)"
    elif value is False and spec.kind != READ:
        result.ok = False
        result.detail = f"{control.label}가 False 를 돌려줬습니다 — 동작하지 않은 것으로 봅니다."
    elif method == "PutFieldText" and checked:
        # Writing into a 누름틀 that does not exist succeeds silently (lesson 24),
        # so the fields are read back.
        read_spec = lookup(control, "GetFieldText")
        actual = _call(element, lambda: pattern.Call("GetFieldText", read_spec, checked[:1], timeout))
        wanted = _field_parts(checked[1] if len(checked) > 1 else "")
        if _field_parts(actual) != wanted:
            result.ok = False
            result.detail = (
                f"넣은 뒤 되읽은 값이 다릅니다: {actual!r}. 누름틀 이름이 맞는지 "
                "GetFieldList 로 확인하세요."
            )
        else:
            result.detail = "되읽어 확인했습니다."
    elif spec.kind == WRITE:
        result.warning = "되읽어 확인할 수 없는 동작입니다. 필요하면 GetTextFile 로 결과를 읽어 보세요."
    return result
