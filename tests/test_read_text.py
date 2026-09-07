"""Reading a whole window, rather than one element.

`read_text()` with no ref used to answer with the container's own name. On an
Office document that is the filename: asking a 37-slide presentation what it
said returned "발표.pptx" and stopped.
"""

from claude_hands.actions import _stitch_text
from claude_hands.elements import NodeInfo


def node(role, name="", value="", patterns=(), children=()):
    return NodeInfo(
        role=role, name=name, value=value, patterns=tuple(patterns), children=list(children)
    )


SLIDE = node(
    "group",
    "슬라이드 1",
    children=[
        node("edit", "TextBox 4", "2026학년도 교육발전특구 사업설명회", patterns=("value",)),
        node("edit", "Rectangle 2", "", patterns=("value",)),
        node("image", "Picture 1"),
        node("edit", "TextBox 5", "미래다움 · 인간다움", patterns=("value",)),
    ],
)


def test_a_slides_text_comes_back_in_layout_order():
    assert _stitch_text(SLIDE, 8000).splitlines() == [
        "2026학년도 교육발전특구 사업설명회",
        "미래다움 · 인간다움",
    ]


def test_an_empty_shape_contributes_nothing_not_its_label():
    """"Rectangle 2" is what the shape is called, not something the slide says."""

    assert "Rectangle 2" not in _stitch_text(SLIDE, 8000)
    assert "Picture 1" not in _stitch_text(SLIDE, 8000)


def test_a_static_label_still_counts_as_text():
    """A backend with no value to give leaves the name as the only content."""

    tree = node("window", "대화상자", children=[node("text", "저장하시겠습니까?")])
    assert _stitch_text(tree, 8000) == "저장하시겠습니까?"


def test_an_empty_edit_box_is_empty_not_labelled():
    tree = node("window", "폼", children=[node("edit", "이름", "", patterns=("value",))])
    assert _stitch_text(tree, 8000) == ""


def test_repeated_lines_are_said_once():
    tree = node(
        "window",
        "덱",
        children=[
            node("edit", "a", "같은 머리글", patterns=("value",)),
            node("edit", "b", "같은 머리글", patterns=("value",)),
            node("edit", "c", "다른 줄", patterns=("value",)),
        ],
    )
    assert _stitch_text(tree, 8000).splitlines() == ["같은 머리글", "다른 줄"]


def test_the_container_itself_is_not_part_of_its_own_text():
    tree = node("document", "발표.pptx", "", children=[node("edit", "t", "본문", patterns=("value",))])
    assert _stitch_text(tree, 8000) == "본문"


def test_the_character_cap_is_honoured():
    tree = node(
        "window",
        "긴 문서",
        # Distinct lines, or the de-duplication above collapses them to one and
        # the cap is never reached.
        children=[
            node("edit", f"t{i}", f"{i:02d}" + "가" * 98, patterns=("value",)) for i in range(50)
        ],
    )
    assert len(_stitch_text(tree, 120)) == 120
