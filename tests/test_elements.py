from claude_hands.elements import (
    NodeInfo,
    assign_refs,
    collapse_chains,
    flatten_interactive,
    format_node_line,
    prune,
    render_tree,
    score_match,
    search,
)
from claude_hands.win32.windows import Rect


def node(role="custom", name="", **kwargs):
    return NodeInfo(role=role, name=name, **kwargs)


def build_dialog():
    """A shape real apps produce: a pane chain wrapping a few real controls."""

    save = node("button", "저장", patterns=("invoke",), rect=Rect(100, 200, 180, 228))
    cancel = node("button", "취소", patterns=("invoke",), rect=Rect(190, 200, 270, 228))
    filename = node(
        "edit",
        "파일 이름",
        value="보고서.docx",
        patterns=("value",),
        rect=Rect(100, 150, 400, 178),
    )
    readonly = node(
        "checkbox", "읽기 전용", patterns=("toggle",), toggle_state="off",
        rect=Rect(100, 250, 220, 270),
    )
    inner = node("pane", children=[filename, readonly, save, cancel])
    outer = node("pane", children=[inner])
    return node("window", "다른 이름으로 저장", children=[outer])


def test_actionable_detection():
    assert node("button", "저장", patterns=("invoke",)).is_actionable
    assert node("edit", "이름", patterns=("value",)).is_actionable
    assert not node("text", "안내문").is_actionable
    assert not node("button", "저장", patterns=("invoke",), enabled=False).is_actionable


def test_prune_drops_empty_scaffolding_but_keeps_ancestors_of_content():
    tree = node(
        "window",
        "창",
        children=[
            node("pane", children=[node("pane"), node("button", "확인", patterns=("invoke",))]),
            node("pane", children=[node("pane")]),
        ],
    )
    kept = prune(tree)
    assert kept is not None
    names = [n.name for n in kept.walk()]
    assert "확인" in names
    # The purely empty branch is gone.
    assert len(kept.children) == 1


def test_collapse_chains_removes_single_child_container_towers():
    tree = collapse_chains(build_dialog())
    # window > (pane > pane collapsed away) > four controls
    assert tree.role == "window"
    roles = sorted(child.role for child in tree.children)
    assert roles == ["button", "button", "checkbox", "edit"]


def test_refs_are_assigned_depth_first_and_unique():
    tree = collapse_chains(build_dialog())
    registry = assign_refs(tree)
    assert tree.ref == "e1"
    assert len(registry) == len(set(registry))
    assert all(registry[ref] is nodeobj for ref, nodeobj in registry.items())


def test_format_node_line_shows_state_and_geometry():
    button = node("button", "저장", patterns=("invoke",), rect=Rect(100, 200, 180, 228), ref="e4")
    line = format_node_line(button)
    assert line.startswith('[e4] button "저장"')
    assert "@100,200 80x28" in line

    checkbox = node("checkbox", "읽기 전용", toggle_state="off", ref="e5")
    assert "unchecked" in format_node_line(checkbox)

    disabled = node("button", "적용", enabled=False, ref="e6")
    assert "disabled" in format_node_line(disabled)


def test_format_node_line_includes_value():
    edit = node("edit", "파일 이름", value="보고서.docx", ref="e3")
    assert 'value="보고서.docx"' in format_node_line(edit)


def test_render_tree_indents_by_depth():
    tree = collapse_chains(build_dialog())
    assign_refs(tree)
    rendered = render_tree(tree).splitlines()
    assert rendered[0].startswith("[e1] window")
    assert all(line.startswith("  ") for line in rendered[1:])


def test_render_tree_respects_max_lines():
    tree = node("window", "많은 항목", children=[node("button", f"b{i}") for i in range(50)])
    assign_refs(tree)
    rendered = render_tree(tree, max_lines=10)
    assert len(rendered.splitlines()) <= 11  # 10 nodes + overflow note
    assert "생략" in rendered


def test_search_ranks_exact_name_first():
    tree = collapse_chains(build_dialog())
    assign_refs(tree)
    results = search(tree, "저장")
    assert results
    assert results[0][1].name == "저장"


def test_search_matches_partial_and_word_order():
    tree = collapse_chains(build_dialog())
    assign_refs(tree)
    assert search(tree, "파일")[0][1].role == "edit"
    assert search(tree, "읽기")[0][1].role == "checkbox"


def test_search_role_filter():
    tree = collapse_chains(build_dialog())
    assign_refs(tree)
    assert not search(tree, "저장", role="edit")
    assert search(tree, "저장", role="button")


def test_score_match_penalises_disabled():
    enabled = node("button", "확인", patterns=("invoke",))
    disabled = node("button", "확인", patterns=("invoke",), enabled=False)
    assert score_match(enabled, "확인") > score_match(disabled, "확인")


def test_flatten_interactive_lists_only_actionable():
    tree = collapse_chains(build_dialog())
    assign_refs(tree)
    actionable = flatten_interactive(tree)
    assert {n.name for n in actionable} == {"저장", "취소", "파일 이름", "읽기 전용"}


def test_as_dict_round_trips_key_fields():
    button = node("button", "저장", patterns=("invoke",), rect=Rect(1, 2, 3, 4), ref="e2")
    data = button.as_dict()
    assert data["ref"] == "e2"
    assert data["role"] == "button"
    assert data["rect"] == {"x": 1, "y": 2, "width": 2, "height": 2}
