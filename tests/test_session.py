"""Ref re-resolution: the part that keeps a plan working while the UI moves."""

from claude_hands.elements import NodeInfo, assign_refs
from claude_hands.session import WindowSession
from claude_hands.win32.windows import Rect, WindowInfo


def make_session() -> WindowSession:
    info = WindowInfo(
        hwnd=1234,
        title="테스트 창",
        class_name="Test",
        pid=999,
        process="test.exe",
        rect=Rect(0, 0, 800, 600),
        minimized=False,
        maximized=False,
        visible=True,
    )
    return WindowSession(hwnd=1234, info=info)


def tree_with(name: str, runtime_id=(42, 7)) -> NodeInfo:
    button = NodeInfo(
        role="button",
        name=name,
        automation_id="saveBtn",
        class_name="Button",
        patterns=("invoke",),
        runtime_id=runtime_id,
    )
    root = NodeInfo(role="window", name="테스트 창", children=[button])
    assign_refs(root)
    return root


def test_find_equivalent_prefers_runtime_id_over_name():
    session = make_session()
    session.tree = tree_with("이름이 바뀐 저장", runtime_id=(42, 7))
    stale = NodeInfo(
        role="button", name="저장", automation_id="saveBtn",
        class_name="Button", runtime_id=(42, 7),
    )
    match = session._find_equivalent(stale)
    assert match is not None
    assert match.name == "이름이 바뀐 저장"


def test_find_equivalent_falls_back_to_fingerprint():
    session = make_session()
    session.tree = tree_with("저장", runtime_id=(99, 1))  # runtime id changed
    stale = NodeInfo(
        role="button", name="저장", automation_id="saveBtn",
        class_name="Button", runtime_id=(42, 7),
    )
    match = session._find_equivalent(stale)
    assert match is not None
    assert match.runtime_id == (99, 1)


def test_find_equivalent_falls_back_to_role_and_name():
    session = make_session()
    tree = tree_with("저장")
    tree.children[0].automation_id = "somethingElse"
    tree.children[0].class_name = "OtherClass"
    tree.children[0].runtime_id = ()
    session.tree = tree
    stale = NodeInfo(
        role="button", name="저장", automation_id="saveBtn",
        class_name="Button", runtime_id=(42, 7),
    )
    assert session._find_equivalent(stale) is not None


def test_find_equivalent_returns_none_when_element_is_gone():
    session = make_session()
    session.tree = tree_with("취소", runtime_id=(1, 1))
    session.tree.children[0].automation_id = "cancelBtn"
    stale = NodeInfo(
        role="button", name="저장", automation_id="saveBtn",
        class_name="Button", runtime_id=(42, 7),
    )
    assert session._find_equivalent(stale) is None


def test_element_for_raises_a_readable_error():
    import pytest

    from claude_hands.session import RefNotFoundError

    session = make_session()
    node = NodeInfo(role="button", name="저장")
    with pytest.raises(RefNotFoundError) as excinfo:
        session.element_for(node)
    assert "저장" in str(excinfo.value)


def test_window_info_state_reporting():
    info = make_session().info
    assert info.state == "normal"
    info.minimized = True
    assert info.state == "minimized"
    info.minimized = False
    info.maximized = True
    assert info.state == "maximized"
    info.maximized = False
    info.visible = False
    assert info.state == "hidden"
