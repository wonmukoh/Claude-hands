"""A ref must keep meaning the same element for as long as the session lives.

Refs used to be numbered from 1 on every snapshot. Measured against the sample
app through the MCP server: `e3` was the text box, clicking the button opened a
modal dialog, and the next snapshot handed `e3` to that dialog's OK button. A
caller still holding `e3` from the first snapshot pressed OK believing it was
typing into the text box.

The session's re-identification does not save you here. It repairs a ref whose
element has gone; it cannot notice a ref that now names something else, because
from its side nothing is stale.
"""

from claude_hands.elements import NodeInfo, assign_refs


def node(role, name, *children, runtime_id=(), automation_id=""):
    return NodeInfo(
        role=role,
        name=name,
        automation_id=automation_id,
        runtime_id=runtime_id,
        children=list(children),
    )


def window(*children):
    return node("window", "claude-hands 검증 대상", *children)


EDIT = lambda: node("edit", "입력란:", automation_id="1001")  # noqa: E731
CHECK = lambda: node("checkbox", "알림 받기", automation_id="1002")  # noqa: E731
BUTTON = lambda: node("button", "대화상자 열기", automation_id="1003")  # noqa: E731
DIALOG = lambda: node("window", "확인 대화상자", node("button", "확인"))  # noqa: E731


def refs_of(tree):
    """ref keyed by what a person would call the element."""

    return {(n.role, n.name): n.ref for n in tree.walk()}


def test_the_same_tree_twice_gives_the_same_refs():
    known = {}
    first = refs_of(_assigned(window(EDIT(), CHECK(), BUTTON()), known))
    second = refs_of(_assigned(window(EDIT(), CHECK(), BUTTON()), known))
    assert first == second


def _assigned(tree, known):
    assign_refs(tree, known=known)
    return tree


def test_a_dialog_appearing_does_not_steal_the_text_boxs_ref():
    """The exact failure: a modal opens, and `e3` stops meaning the text box."""

    known = {}
    before = _assigned(window(EDIT(), CHECK(), BUTTON()), known)
    edit_ref = refs_of(before)[("edit", "입력란:")]

    after = _assigned(window(DIALOG(), EDIT(), CHECK(), BUTTON()), known)
    names = refs_of(after)

    assert names[("edit", "입력란:")] == edit_ref
    assert names[("button", "확인")] != edit_ref
    assert names[("window", "확인 대화상자")] != edit_ref


def test_a_vanished_elements_ref_is_not_handed_to_someone_else():
    known = {}
    first = _assigned(window(DIALOG(), EDIT()), known)
    dialog_ref = refs_of(first)[("window", "확인 대화상자")]
    ok_ref = refs_of(first)[("button", "확인")]

    later = _assigned(window(EDIT(), CHECK(), BUTTON()), known)
    reissued = set(refs_of(later).values())

    assert dialog_ref not in reissued
    assert ok_ref not in reissued


def test_a_reopened_dialog_gets_its_old_ref_back():
    """Coming back should be recognised, not renumbered — a plan stays valid."""

    known = {}
    opened = _assigned(window(DIALOG(), EDIT()), known)
    ok_ref = refs_of(opened)[("button", "확인")]

    _assigned(window(EDIT()), known)
    reopened = _assigned(window(DIALOG(), EDIT()), known)

    assert refs_of(reopened)[("button", "확인")] == ok_ref


def test_duplicate_siblings_keep_separate_refs():
    """PowerPoint really does put "Rectangle 2" on every slide."""

    known = {}
    tree = node(
        "document",
        "deck.pptx",
        node("group", "슬라이드 1", node("edit", "Rectangle 2")),
        node("group", "슬라이드 2", node("edit", "Rectangle 2")),
    )
    assign_refs(tree, known=known)
    first, second = (g.children[0].ref for g in tree.children)
    assert first != second

    again = node(
        "document",
        "deck.pptx",
        node("group", "슬라이드 1", node("edit", "Rectangle 2")),
        node("group", "슬라이드 2", node("edit", "Rectangle 2")),
    )
    assign_refs(again, known=known)
    assert [g.children[0].ref for g in again.children] == [first, second]


def test_a_renamed_element_keeps_its_ref_when_the_backend_identifies_it():
    """UI Automation runtime ids outrank names, so a relabelled button is still it."""

    known = {}
    before = window(node("button", "저장", runtime_id=(42, 7)))
    assign_refs(before, known=known)
    ref = before.children[0].ref

    after = window(node("button", "저장 중...", runtime_id=(42, 7)))
    assign_refs(after, known=known)
    assert after.children[0].ref == ref


def test_refs_are_unique_within_a_snapshot():
    known = {}
    tree = window(EDIT(), CHECK(), BUTTON(), DIALOG())
    registry = assign_refs(tree, known=known)
    all_refs = [n.ref for n in tree.walk()]
    assert len(all_refs) == len(set(all_refs))
    assert set(registry) == set(all_refs)
    assert all(registry[n.ref] is n for n in tree.walk())


def test_without_a_known_map_each_call_still_numbers_from_one():
    """A one-off snapshot outside a session should stay compact and readable."""

    tree = window(EDIT(), CHECK())
    assign_refs(tree)
    assert [n.ref for n in tree.walk()] == ["e1", "e2", "e3"]
