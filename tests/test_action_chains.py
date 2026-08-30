"""Drive the action layer against fake UIA elements.

This exercises everything below the COM boundary for real: which strategy each
action picks, in what order, what it falls back to when a pattern refuses, and
what actually gets posted to the window. Only the ctypes/COM calls themselves
are stubbed, so a wrong decision here is a wrong decision on Windows too.
"""

from __future__ import annotations

import pytest

from claude_hands import actions
from claude_hands.actions import ActionFailedError
from claude_hands.elements import NodeInfo
from claude_hands.session import WindowSession
from claude_hands.win32.windows import Rect, WindowInfo


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakePattern:
    """Records calls; can be told to fail to force the next strategy."""

    def __init__(self, **state):
        self.calls: list[tuple] = []
        self.fail_on: set[str] = set()
        self.__dict__.update(state)

    def _record(self, name, *args):
        self.calls.append((name,) + args)
        if name in self.fail_on:
            raise RuntimeError(f"{name} refused")

    def Invoke(self):
        self._record("Invoke")

    def Toggle(self):
        self._record("Toggle")
        self.CurrentToggleState = 0 if getattr(self, "CurrentToggleState", 0) else 1

    def Select(self):
        self._record("Select")

    def AddToSelection(self):
        self._record("AddToSelection")

    def Expand(self):
        self._record("Expand")
        self.CurrentExpandCollapseState = 1

    def Collapse(self):
        self._record("Collapse")
        self.CurrentExpandCollapseState = 0

    def DoDefaultAction(self):
        self._record("DoDefaultAction")

    def SetValue(self, value):
        self._record("SetValue", value)
        self.CurrentValue = value

    def Scroll(self, horizontal, vertical):
        self._record("Scroll", horizontal, vertical)

    def SetScrollPercent(self, horizontal, vertical):
        self._record("SetScrollPercent", horizontal, vertical)

    def ScrollIntoView(self):
        self._record("ScrollIntoView")


class FakeElement:
    """Duck-types the bits of UiaElement that actions.py touches."""

    def __init__(self, node: NodeInfo, patterns: dict[str, FakePattern] | None = None,
                 live_rect: Rect | None = None):
        self.node = node
        self.patterns = patterns or {}
        self._live_rect = live_rect or node.rect
        self.focus_calls = 0
        self.com = self

    def pattern(self, key):
        return self.patterns.get(key)

    def refresh(self):
        return self

    @property
    def rect(self):
        return self._live_rect

    @property
    def name(self):
        return self.node.name

    @property
    def enabled(self):
        return self.node.enabled

    def value(self):
        return self.node.value

    def SetFocus(self):
        self.focus_calls += 1


class Recorder:
    """Captures what would have gone out over Win32."""

    def __init__(self):
        self.clicks: list[dict] = []
        self.scrolls: list[dict] = []
        self.keys: list[tuple[int, str]] = []
        self.chars: list[tuple[int, str]] = []
        self.set_texts: list[tuple[int, str]] = []
        self.set_text_result = True


@pytest.fixture
def rec(monkeypatch):
    recorder = Recorder()

    monkeypatch.setattr(actions, "_call", lambda element, func, *a, **k: func(*a, **k))
    monkeypatch.setattr(actions, "deepest_child_at", lambda hwnd, x, y: hwnd + 1)

    def fake_click(top, x, y, *, button="left", double=False, modifiers=(), target_hwnd=None):
        recorder.clicks.append(
            {"top": top, "x": x, "y": y, "button": button,
             "double": double, "modifiers": modifiers, "target": target_hwnd}
        )
        return target_hwnd or top

    def fake_scroll(top, x, y, *, clicks=3, horizontal=False, target_hwnd=None):
        recorder.scrolls.append(
            {"top": top, "x": x, "y": y, "clicks": clicks, "horizontal": horizontal}
        )
        return target_hwnd or top

    def fake_keys(hwnd, spec, **kwargs):
        recorder.keys.append((hwnd, spec))
        return [spec]

    def fake_chars(hwnd, text, **kwargs):
        recorder.chars.append((hwnd, text))

    def fake_set_text(hwnd, text, **kwargs):
        recorder.set_texts.append((hwnd, text))
        return recorder.set_text_result

    monkeypatch.setattr(actions, "click_at_screen_point", fake_click)
    monkeypatch.setattr(actions, "scroll_at_screen_point", fake_scroll)
    monkeypatch.setattr(actions, "send_keys", fake_keys)
    monkeypatch.setattr(actions, "send_text_chars", fake_chars)
    monkeypatch.setattr(actions, "set_window_text", fake_set_text)

    class FakeModule:
        ScrollAmount_SmallIncrement = 4
        ScrollAmount_SmallDecrement = 1
        ScrollAmount_NoAmount = 2
        ScrollPatternNoScroll = -1
        # deliberately no IUIAutomationElement3 → context menu is unavailable

    import claude_hands.uia.core as core

    monkeypatch.setattr(core, "get_automation", lambda: (FakeModule, object()))
    return recorder


def make_session(nodes: dict[str, tuple[NodeInfo, FakeElement]], hwnd: int = 1000) -> WindowSession:
    info = WindowInfo(
        hwnd=hwnd, title="시험 창", class_name="Test", pid=1, process="test.exe",
        rect=Rect(0, 0, 800, 600), minimized=True, maximized=False, visible=False,
    )
    session = WindowSession(hwnd=hwnd, info=info)
    root = NodeInfo(role="window", name="시험 창")
    for ref, (node, element) in nodes.items():
        node.ref = ref
        session.refs[ref] = node
        session.elements[id(node)] = element
        root.children.append(node)
    session.tree = root
    return session


def button(name="저장", hwnd=0, **kw):
    return NodeInfo(role="button", name=name, patterns=("invoke",),
                    rect=Rect(100, 200, 180, 228), hwnd=hwnd, **kw)


# --------------------------------------------------------------------------
# click
# --------------------------------------------------------------------------


def test_button_click_uses_uia_invoke_and_posts_nothing(rec):
    node = button()
    invoke = FakePattern()
    session = make_session({"e1": (node, FakeElement(node, {"invoke": invoke}))})

    result = actions.click(session, "e1")

    assert result.ok and result.strategy == "uia:invoke"
    assert invoke.calls == [("Invoke",)]
    assert rec.clicks == []  # no synthetic click was needed


def test_checkbox_click_prefers_toggle_over_invoke(rec):
    node = NodeInfo(role="checkbox", name="쪽 번호", patterns=("toggle", "invoke"),
                    toggle_state="off", rect=Rect(10, 10, 60, 30))
    toggle, invoke = FakePattern(CurrentToggleState=0), FakePattern()
    session = make_session({"e1": (node, FakeElement(node, {"toggle": toggle, "invoke": invoke}))})

    result = actions.click(session, "e1")

    assert result.strategy == "uia:toggle"
    assert toggle.calls == [("Toggle",)]
    assert invoke.calls == []


def test_listitem_click_prefers_selection(rec):
    node = NodeInfo(role="listitem", name="슬라이드 3", patterns=("selectionitem", "invoke"),
                    rect=Rect(0, 0, 100, 40))
    selection, invoke = FakePattern(), FakePattern()
    session = make_session(
        {"e1": (node, FakeElement(node, {"selectionitem": selection, "invoke": invoke}))}
    )

    result = actions.click(session, "e1")

    assert result.strategy == "uia:select"
    assert selection.calls == [("Select",)]


def test_click_falls_through_invoke_to_legacy_then_message(rec):
    node = button()
    invoke = FakePattern()
    invoke.fail_on = {"Invoke"}
    legacy = FakePattern()
    legacy.fail_on = {"DoDefaultAction"}
    session = make_session(
        {"e1": (node, FakeElement(node, {"invoke": invoke, "legacy": legacy}))}
    )

    result = actions.click(session, "e1")

    assert result.strategy == "message:click"
    assert [a.split("(")[0] for a in result.attempts] == [
        "uia:invoke", "uia:legacy-default-action", "message:click",
    ]
    assert "실패" in result.attempts[0]
    assert rec.clicks[0]["x"] == 140 and rec.clicks[0]["y"] == 214  # element centre


def test_click_with_no_patterns_goes_straight_to_messages(rec):
    node = NodeInfo(role="custom", name="캔버스", rect=Rect(0, 0, 100, 100))
    session = make_session({"e1": (node, FakeElement(node))})

    result = actions.click(session, "e1")

    assert result.strategy == "message:click"
    assert rec.clicks[0]["target"] == 1001  # deepest child under the point


def test_click_uses_the_live_rect_not_the_snapshot_rect(rec):
    """A slide that scrolled since the snapshot must not be clicked at stale coords."""

    node = NodeInfo(role="custom", name="슬라이드", rect=Rect(0, 0, 100, 100))
    element = FakeElement(node, live_rect=Rect(0, 500, 100, 600))
    session = make_session({"e1": (node, element)})

    actions.click(session, "e1")

    assert (rec.clicks[0]["x"], rec.clicks[0]["y"]) == (50, 550)


def test_double_click_skips_the_pattern_chain(rec):
    node = button()
    invoke = FakePattern()
    session = make_session({"e1": (node, FakeElement(node, {"invoke": invoke}))})

    result = actions.click(session, "e1", double=True)

    assert result.strategy == "message:click"
    assert invoke.calls == []
    assert rec.clicks[0]["double"] is True


def test_modifier_click_skips_the_pattern_chain(rec):
    node = button()
    invoke = FakePattern()
    session = make_session({"e1": (node, FakeElement(node, {"invoke": invoke}))})

    actions.click(session, "e1", modifiers=("ctrl",))

    assert invoke.calls == []
    assert rec.clicks[0]["modifiers"] == ("ctrl",)


def test_right_click_falls_back_to_messages_when_uia_cannot(rec):
    node = button()
    session = make_session({"e1": (node, FakeElement(node, {"invoke": FakePattern()}))})

    result = actions.click(session, "e1", button="right")

    assert result.strategy == "message:click"
    assert rec.clicks[0]["button"] == "right"


def test_click_scrolls_the_element_into_view_first(rec):
    node = NodeInfo(role="listitem", name="슬라이드 40", rect=Rect(0, 0, 100, 40))
    scroll_item = FakePattern()
    session = make_session({"e1": (node, FakeElement(node, {"scrollitem": scroll_item}))})

    actions.click(session, "e1")

    assert scroll_item.calls == [("ScrollIntoView",)]


def test_click_on_a_disabled_element_is_refused(rec):
    node = button(enabled=False)
    session = make_session({"e1": (node, FakeElement(node, {"invoke": FakePattern()}))})

    with pytest.raises(ActionFailedError, match="비활성"):
        actions.click(session, "e1")


def test_force_message_bypasses_uia(rec):
    node = button()
    invoke = FakePattern()
    session = make_session({"e1": (node, FakeElement(node, {"invoke": invoke}))})

    result = actions.click(session, "e1", force_message=True)

    assert result.strategy == "message:click"
    assert invoke.calls == []


# --------------------------------------------------------------------------
# type_text
# --------------------------------------------------------------------------


def edit(name="제목 텍스트 상자", value="", hwnd=0):
    return NodeInfo(role="edit", name=name, value=value, patterns=("value",),
                    rect=Rect(50, 50, 400, 90), hwnd=hwnd)


def test_type_uses_set_value_and_never_touches_the_keyboard(rec):
    node = edit()
    value = FakePattern(CurrentIsReadOnly=False)
    session = make_session({"e1": (node, FakeElement(node, {"value": value}))})

    result = actions.type_text(session, "e1", "2026 학년도 운영 계획")

    assert result.strategy == "uia:value.SetValue"
    assert value.calls == [("SetValue", "2026 학년도 운영 계획")]
    assert rec.chars == [] and rec.keys == []


def test_type_appends_when_clear_is_false(rec):
    node = edit(value="기존 내용")
    value = FakePattern(CurrentIsReadOnly=False)
    session = make_session({"e1": (node, FakeElement(node, {"value": value}))})

    actions.type_text(session, "e1", " 추가", clear=False)

    assert value.calls == [("SetValue", "기존 내용 추가")]


def test_type_with_submit_sends_enter(rec):
    node = edit(hwnd=77)
    value = FakePattern(CurrentIsReadOnly=False)
    session = make_session({"e1": (node, FakeElement(node, {"value": value}))})

    actions.type_text(session, "e1", "제목", submit=True)

    assert rec.keys == [(77, "enter")]


def test_readonly_value_pattern_falls_back_to_wm_settext(rec):
    node = edit(hwnd=42)
    value = FakePattern(CurrentIsReadOnly=True)
    session = make_session({"e1": (node, FakeElement(node, {"value": value}))})

    result = actions.type_text(session, "e1", "내용")

    assert result.strategy == "message:WM_SETTEXT"
    assert rec.set_texts == [(42, "내용")]


def test_last_resort_typing_focuses_then_sends_characters(rec):
    node = NodeInfo(role="edit", name="주소", rect=Rect(0, 0, 100, 20), hwnd=55)
    element = FakeElement(node)
    session = make_session({"e1": (node, element)})
    rec.set_text_result = False

    result = actions.type_text(session, "e1", "안녕")

    assert result.strategy == "focus+message:WM_CHAR"
    assert element.focus_calls == 1
    assert rec.chars == [(55, "안녕")]
    assert [spec for _hwnd, spec in rec.keys] == ["ctrl+a", "delete"]
    assert "포커스" in result.detail  # the caller is told focus was needed


def test_typing_can_refuse_to_take_focus(rec):
    node = NodeInfo(role="edit", name="주소", rect=Rect(0, 0, 100, 20), hwnd=0)
    session = make_session({"e1": (node, FakeElement(node))})

    with pytest.raises(ActionFailedError, match="allow_focus"):
        actions.type_text(session, "e1", "안녕", allow_focus=False)


# --------------------------------------------------------------------------
# toggle / expand / select
# --------------------------------------------------------------------------


def test_toggle_to_a_target_state_is_idempotent(rec):
    node = NodeInfo(role="checkbox", name="눈금자", patterns=("toggle",), toggle_state="on")
    pattern = FakePattern(CurrentToggleState=1)
    session = make_session({"e1": (node, FakeElement(node, {"toggle": pattern}))})

    result = actions.toggle(session, "e1", to=True)

    assert result.strategy == "noop"
    assert pattern.calls == []


def test_toggle_flips_when_the_state_differs(rec):
    node = NodeInfo(role="checkbox", name="눈금자", patterns=("toggle",), toggle_state="off")
    pattern = FakePattern(CurrentToggleState=0)
    session = make_session({"e1": (node, FakeElement(node, {"toggle": pattern}))})

    result = actions.toggle(session, "e1", to=True)

    assert pattern.calls == [("Toggle",)]
    assert "on" in result.detail


def test_expand_and_collapse(rec):
    node = NodeInfo(role="combobox", name="글꼴", patterns=("expandcollapse",))
    pattern = FakePattern(CurrentExpandCollapseState=0)
    session = make_session({"e1": (node, FakeElement(node, {"expandcollapse": pattern}))})

    actions.expand(session, "e1")
    actions.expand(session, "e1", collapse=True)

    assert [c[0] for c in pattern.calls] == ["Expand", "Collapse"]


def test_select_without_a_pattern_degrades_to_click(rec):
    node = NodeInfo(role="listitem", name="슬라이드 2", rect=Rect(0, 0, 80, 40))
    session = make_session({"e1": (node, FakeElement(node))})

    result = actions.select(session, "e1")

    assert result.action == "click"
    assert rec.clicks


# --------------------------------------------------------------------------
# scroll
# --------------------------------------------------------------------------


def scrollable(name="슬라이드 창"):
    return NodeInfo(role="pane", name=name, patterns=("scroll",), rect=Rect(0, 0, 400, 400))


def test_scroll_down_uses_the_increment_constant(rec):
    node = scrollable()
    pattern = FakePattern()
    session = make_session({"e1": (node, FakeElement(node, {"scroll": pattern}))})

    actions.scroll(session, ref="e1", direction="down", amount=2)

    # (horizontal=NoAmount, vertical=SmallIncrement) twice
    assert pattern.calls == [("Scroll", 2, 4), ("Scroll", 2, 4)]


def test_scroll_up_uses_the_decrement_constant(rec):
    node = scrollable()
    pattern = FakePattern()
    session = make_session({"e1": (node, FakeElement(node, {"scroll": pattern}))})

    actions.scroll(session, ref="e1", direction="up", amount=1)

    assert pattern.calls == [("Scroll", 2, 1)]


def test_scroll_right_moves_the_horizontal_axis(rec):
    node = scrollable()
    pattern = FakePattern()
    session = make_session({"e1": (node, FakeElement(node, {"scroll": pattern}))})

    actions.scroll(session, ref="e1", direction="right", amount=1)

    assert pattern.calls == [("Scroll", 4, 2)]


def test_scroll_to_percent(rec):
    node = scrollable()
    pattern = FakePattern()
    session = make_session({"e1": (node, FakeElement(node, {"scroll": pattern}))})

    actions.scroll(session, ref="e1", direction="down", to_percent=80)

    assert pattern.calls == [("SetScrollPercent", -1, 80.0)]


def test_wheel_fallback_signs_match_win32_conventions(rec):
    node = NodeInfo(role="pane", name="문서", rect=Rect(0, 0, 400, 400))
    session = make_session({"e1": (node, FakeElement(node))})

    actions.scroll(session, ref="e1", direction="down", amount=3)
    actions.scroll(session, ref="e1", direction="up", amount=3)
    actions.scroll(session, ref="e1", direction="right", amount=2)
    actions.scroll(session, ref="e1", direction="left", amount=2)

    assert [(s["clicks"], s["horizontal"]) for s in rec.scrolls] == [
        (-3, False),  # WM_MOUSEWHEEL: negative scrolls down
        (3, False),
        (2, True),    # WM_MOUSEHWHEEL: positive scrolls right
        (-2, True),
    ]


def test_scroll_rejects_a_bad_direction(rec):
    session = make_session({})
    with pytest.raises(ActionFailedError, match="up/down/left/right"):
        actions.scroll(session, direction="sideways")


# --------------------------------------------------------------------------
# keys
# --------------------------------------------------------------------------


def test_keys_go_to_the_window_by_default(rec):
    session = make_session({})
    actions.press_keys(session, "ctrl+s")
    assert rec.keys == [(1000, "ctrl+s")]


def test_keys_go_to_the_element_hwnd_when_a_ref_is_given(rec):
    node = edit(hwnd=321)
    session = make_session({"e1": (node, FakeElement(node))})

    actions.press_keys(session, "f5", ref="e1")

    assert rec.keys == [(321, "f5")]


def test_keys_repeat(rec):
    session = make_session({})
    actions.press_keys(session, "down", repeat=3)
    assert len(rec.keys) == 3


# --------------------------------------------------------------------------
# menu_select
# --------------------------------------------------------------------------


def menu_session(names):
    nodes = {}
    for index, name in enumerate(names, start=1):
        node = NodeInfo(role="menuitem", name=name, patterns=("expandcollapse", "invoke"),
                        rect=Rect(0, index * 20, 80, index * 20 + 20))
        nodes[f"e{index}"] = (node, FakeElement(node, {
            "expandcollapse": FakePattern(CurrentExpandCollapseState=0),
            "invoke": FakePattern(),
        }))
    session = make_session(nodes)

    def find(query, **kwargs):
        return [(1.0, n) for n, _e in nodes.values() if query in n.name]

    session.find = find
    return session, nodes


def test_single_step_menu_path_does_not_crash(rec):
    """A one-level path never enters the expand branch; the result must still build."""

    session, _nodes = menu_session(["저장"])

    result = actions.menu_select(session, "저장")

    assert result.ok
    assert result.target == "저장"
    assert result.strategy.endswith(":menu")


def test_multi_step_menu_expands_then_invokes(rec):
    session, nodes = menu_session(["파일", "다른 이름으로 저장"])

    result = actions.menu_select(session, "파일 > 다른 이름으로 저장")

    assert result.ok and result.target == "파일 > 다른 이름으로 저장"
    file_expand = nodes["e1"][1].patterns["expandcollapse"]
    assert file_expand.calls == [("Expand",)]          # 상위 메뉴는 펼치고
    assert nodes["e2"][1].patterns["invoke"].calls == [("Invoke",)]  # 말단은 실행


def test_menu_reports_a_missing_step_with_the_path_so_far(rec):
    session, _nodes = menu_session(["파일"])

    with pytest.raises(ActionFailedError, match="없는메뉴"):
        actions.menu_select(session, "파일 > 없는메뉴")


# --------------------------------------------------------------------------
# Engine routing
# --------------------------------------------------------------------------


class Win32ishElement(FakeElement):
    """An element from the window-message backend."""

    engine = "win32"


def test_win32_engine_calls_never_touch_the_com_worker(monkeypatch):
    """The fallback engine exists for machines where COM cannot start.

    Marshalling its window messages onto the UIA worker both boots the COM
    stack it is meant to avoid and makes SendMessage fail with
    RPC_E_CANTCALLOUT_ININPUTSYNCCALL.
    """

    import claude_hands.uia.core as core

    def explode():
        raise AssertionError("win32 engine must not use the COM worker")

    monkeypatch.setattr(core, "get_worker", explode)

    node = button()
    invoke = FakePattern()
    element = Win32ishElement(node, {"invoke": invoke})
    session = make_session({"e1": (node, element)})

    result = actions.click(session, "e1")

    assert result.strategy == "win32:invoke"
    assert invoke.calls == [("Invoke",)]


def test_uia_engine_calls_go_through_the_com_worker(monkeypatch):
    import claude_hands.uia.core as core

    used = []

    class Worker:
        def call(self, func, *a, **k):
            used.append(func)
            return func(*a, **k)

    monkeypatch.setattr(core, "get_worker", lambda: Worker())

    node = button()
    invoke = FakePattern()
    session = make_session({"e1": (node, FakeElement(node, {"invoke": invoke}))})

    result = actions.click(session, "e1")

    assert result.strategy == "uia:invoke"
    assert used, "UIA calls must be marshalled to the COM worker"


def test_strategy_names_report_the_engine_that_actually_ran(monkeypatch):
    monkeypatch.setattr(actions, "_call", lambda element, func, *a, **k: func(*a, **k))

    node = NodeInfo(role="checkbox", name="옵션", patterns=("toggle",), toggle_state="off")
    element = Win32ishElement(node, {"toggle": FakePattern(CurrentToggleState=0)})
    session = make_session({"e1": (node, element)})

    assert actions.click(session, "e1").strategy == "win32:toggle"
