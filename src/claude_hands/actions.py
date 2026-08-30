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

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .elements import NodeInfo
from .session import WindowSession
from .win32.defs import ClaudeHandsError
from .win32.input import (
    click_at_screen_point,
    scroll_at_screen_point,
    screen_point_in_element,
    send_keys,
    send_text_chars,
    set_window_text,
)
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

    def describe(self) -> str:
        status = "완료" if self.ok else "실패"
        line = f"{self.action} {status} — {self.target}" if self.target else f"{self.action} {status}"
        line += f" (방식: {self.strategy})"
        if self.detail:
            line += f"\n{self.detail}"
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
        rect = _uia(lambda: element.refresh().rect)
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


def _uia(func, *args, **kwargs):
    """Run a COM call on the UIA worker thread."""

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

    if not node.enabled:
        raise ActionFailedError(f"{label} 은(는) 비활성 상태라 누를 수 없습니다.")

    simple_left = button == "left" and not double and not modifiers
    if simple_left and not force_message:
        for strategy, runner in _click_pattern_chain(element, node):
            attempts.append(strategy)
            try:
                _uia(runner)
                return ActionResult(True, "click", strategy, label, attempts=attempts)
            except Exception as exc:  # noqa: BLE001 - try the next strategy
                attempts[-1] = f"{strategy}(실패: {_short(exc)})"

    if button == "right" and not force_message:
        context_label = f"{_engine(element)}:context-menu"
        attempts.append(context_label)
        try:
            _uia(lambda: _show_context_menu(element))
            return ActionResult(True, "click", context_label, label, attempts=attempts)
        except Exception as exc:  # noqa: BLE001
            attempts[-1] = f"{context_label}(실패: {_short(exc)})"

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


def _engine(element) -> str:
    """Which backend this element came from — reported so results never lie."""

    return getattr(element, "engine", "uia")


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


def type_text(
    session: WindowSession,
    ref: str,
    text: str,
    *,
    clear: bool = True,
    submit: bool = False,
    allow_focus: bool = True,
) -> ActionResult:
    """Put text into an element.

    ``ValuePattern.SetValue`` is atomic and IME-safe — it hands the app the
    finished string, so Korean/Japanese input needs no composition at all, and
    it works while the window is minimised. Character-by-character typing is a
    fallback that does require focus.
    """

    node, element = session.resolve(ref)
    label = _target_label(node)
    attempts: list[str] = []

    if not node.enabled:
        raise ActionFailedError(f"{label} 은(는) 비활성 상태라 입력할 수 없습니다.")

    engine = _engine(element)
    value_pattern = element.pattern("value")
    if value_pattern is not None:
        set_value_label = f"{engine}:value.SetValue"
        attempts.append(set_value_label)
        try:
            is_readonly = False
            try:
                is_readonly = bool(_uia(lambda: value_pattern.CurrentIsReadOnly))
            except Exception:  # noqa: BLE001 - property missing on some apps
                is_readonly = False
            if is_readonly:
                raise ActionFailedError("읽기 전용 필드입니다.")
            new_text = text if clear else (node.value or "") + text
            _uia(lambda: value_pattern.SetValue(new_text))
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
            _uia(lambda: legacy.SetValue(new_text))
            if submit:
                send_keys(_owning_hwnd(session, node), "enter")
            return ActionResult(
                True, "type", legacy_label, label,
                detail=f"{len(new_text)}자 입력", attempts=attempts,
            )
        except Exception as exc:  # noqa: BLE001
            attempts[-1] = f"{legacy_label}(실패: {_short(exc)})"

    hwnd = node.hwnd
    if hwnd and clear:
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

    if not allow_focus:
        raise ActionFailedError(
            f"{label} 에 배경 상태로 입력할 방법이 없습니다 "
            "(Value 패턴 없음). allow_focus=True 로 다시 시도하세요.\n"
            "시도한 방법: " + " → ".join(attempts)
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
            _uia(lambda: range_pattern.SetValue(float(value)))
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
    """Post a key chord to the window (or to one element's HWND)."""

    target_hwnd = session.hwnd
    label = "창"
    if ref:
        node, _element = session.resolve(ref)
        target_hwnd = _owning_hwnd(session, node)
        label = _target_label(node)

    sent: list[str] = []
    for _ in range(max(1, repeat)):
        sent = send_keys(target_hwnd, keys)
    return ActionResult(
        True,
        "keys",
        "message:WM_KEYDOWN",
        label,
        detail=f"{' '.join(sent)} × {max(1, repeat)} → hwnd={target_hwnd}",
    )


def focus(session: WindowSession, ref: str) -> ActionResult:
    """Give an element keyboard focus (may raise the window — that is Windows)."""

    node, element = session.resolve(ref)
    label = _target_label(node)
    try:
        _uia(lambda: element.com.SetFocus())
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
                _uia(lambda: pattern.SetScrollPercent(horizontal, vertical))
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
                _uia(lambda: pattern.Scroll(horizontal, vertical))
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
        _uia(lambda: pattern.ScrollIntoView())
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
        _uia(lambda: pattern.AddToSelection() if add else pattern.Select())
        return ActionResult(True, "select", f"{_engine(element)}:selectionitem", label)
    except Exception as exc:  # noqa: BLE001
        raise ActionFailedError(f"{label} 선택 실패: {_short(exc)}") from exc


def toggle(session: WindowSession, ref: str, *, to: bool | None = None) -> ActionResult:
    node, element = session.resolve(ref)
    label = _target_label(node)
    pattern = element.pattern("toggle")
    if pattern is None:
        return click(session, ref)
    try:
        current = _uia(lambda: int(pattern.CurrentToggleState))
        if to is not None:
            want = 1 if to else 0
            if current == want:
                return ActionResult(
                    True, "toggle", "noop", label, detail=f"이미 {'켜짐' if to else '꺼짐'} 상태"
                )
            for _ in range(3):
                _uia(lambda: pattern.Toggle())
                if int(_uia(lambda: pattern.CurrentToggleState)) == want:
                    break
        else:
            _uia(lambda: pattern.Toggle())
        state = _uia(lambda: int(pattern.CurrentToggleState))
        from .uia.core import TOGGLE_STATES

        return ActionResult(
            True, "toggle", f"{_engine(element)}:toggle", label, detail=f"현재 상태: {TOGGLE_STATES.get(state, state)}"
        )
    except Exception as exc:  # noqa: BLE001
        raise ActionFailedError(f"{label} 토글 실패: {_short(exc)}") from exc


def expand(session: WindowSession, ref: str, *, collapse: bool = False) -> ActionResult:
    node, element = session.resolve(ref)
    label = _target_label(node)
    pattern = element.pattern("expandcollapse")
    if pattern is None:
        return click(session, ref)
    try:
        _uia(lambda: pattern.Collapse() if collapse else pattern.Expand())
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
            text = _uia(lambda: text_pattern.DocumentRange.GetText(max_chars))
            if text:
                return str(text)
        except Exception:  # noqa: BLE001 - fall through to simpler reads
            pass

    value = element.value()
    if value:
        return value
    if node.name:
        return node.name

    # Nothing directly readable: stitch descendant text together.
    pieces = [
        child.name
        for child in node.walk()
        if child.role in {"text", "edit", "document", "listitem", "treeitem", "dataitem"}
        and child.name
    ]
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
                _uia(lambda: pattern.Expand())
            else:
                click(session, node.ref)
            time.sleep(0.25)
        walked.append(step)

    return ActionResult(True, "menu_select", f"{_session_engine(session)}:menu", " > ".join(walked))
