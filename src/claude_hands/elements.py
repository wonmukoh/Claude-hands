"""Platform-neutral representation of a UI tree and how it is rendered.

Keeping this free of Win32/COM means the interesting logic — what counts as an
actionable element, how the tree gets pruned, how refs are handed out — is
testable on any machine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Optional

from .win32.windows import Rect

# Control types a model can act on even with no name of their own.
ACTIONABLE_ROLES = {
    "button",
    "checkbox",
    "combobox",
    "edit",
    "hyperlink",
    "listitem",
    "menuitem",
    "radiobutton",
    "splitbutton",
    "tabitem",
    "treeitem",
    "slider",
    "spinner",
    "document",
    "datagrid",
    "dataitem",
    "calendar",
    "custom",
}

# Roles that are pure scaffolding: kept only when a descendant matters.
CONTAINER_ROLES = {"pane", "group", "custom", "window", "toolbar", "menubar", "table", "list", "tree", "tab"}

@dataclass
class NodeInfo:
    """One element in a captured UI tree."""

    role: str = "custom"
    name: str = ""
    value: str = ""
    automation_id: str = ""
    class_name: str = ""
    rect: Optional[Rect] = None
    enabled: bool = True
    offscreen: bool = False
    focusable: bool = False
    focused: bool = False
    hwnd: int = 0
    runtime_id: tuple[int, ...] = ()
    patterns: tuple[str, ...] = ()
    toggle_state: Optional[str] = None
    expand_state: Optional[str] = None
    selected: Optional[bool] = None
    help_text: str = ""
    depth: int = 0
    ref: str = ""
    children: list["NodeInfo"] = field(default_factory=list)
    truncated_children: int = 0

    # -- predicates -------------------------------------------------------
    @property
    def is_actionable(self) -> bool:
        if not self.enabled:
            return False
        if self.patterns and set(self.patterns) & {
            "invoke",
            "toggle",
            "selectionitem",
            "expandcollapse",
            "rangevalue",
        }:
            return True
        if "value" in self.patterns and self.role in {"edit", "combobox", "document", "spinner"}:
            return True
        return self.role in ACTIONABLE_ROLES and bool(self.name or self.automation_id)

    @property
    def has_content(self) -> bool:
        return bool(self.name.strip() or self.value.strip())

    @property
    def is_interesting(self) -> bool:
        if self.is_actionable:
            return True
        if self.has_content and self.role not in {"pane", "group"}:
            return True
        if self.role in {"edit", "combobox", "document"}:
            return True
        return False

    def walk(self) -> Iterator["NodeInfo"]:
        yield self
        for child in self.children:
            yield from child.walk()

    def as_dict(self) -> dict:
        data = {
            "ref": self.ref,
            "role": self.role,
            "name": self.name,
            "enabled": self.enabled,
        }
        if self.value:
            data["value"] = self.value
        if self.automation_id:
            data["automation_id"] = self.automation_id
        if self.rect:
            data["rect"] = self.rect.as_dict()
        if self.toggle_state:
            data["toggle_state"] = self.toggle_state
        if self.expand_state:
            data["expand_state"] = self.expand_state
        if self.selected is not None:
            data["selected"] = self.selected
        if self.offscreen:
            data["offscreen"] = True
        if self.children:
            data["children"] = [c.as_dict() for c in self.children]
        return data


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def format_node_line(node: NodeInfo, *, show_rect: bool = True, text_limit: int = 80) -> str:
    """Render one node as a single line of the snapshot."""

    parts: list[str] = []
    if node.ref:
        parts.append(f"[{node.ref}]")
    parts.append(node.role)
    if node.name:
        parts.append(f'"{_truncate(node.name, text_limit)}"')
    if node.value:
        parts.append(f'value="{_truncate(node.value, text_limit)}"')

    flags: list[str] = []
    if not node.enabled:
        flags.append("disabled")
    if node.offscreen:
        flags.append("offscreen")
    if node.toggle_state and node.toggle_state != "off":
        flags.append(f"checked={node.toggle_state}")
    elif node.toggle_state == "off":
        flags.append("unchecked")
    if node.selected:
        flags.append("selected")
    if node.expand_state in {"collapsed", "expanded"}:
        flags.append(node.expand_state)
    if node.focused:
        flags.append("focused")
    if node.automation_id:
        flags.append(f"id={_truncate(node.automation_id, 32)}")
    if flags:
        parts.append("(" + ", ".join(flags) + ")")

    if show_rect and node.rect and node.rect.width and node.rect.height:
        parts.append(f"@{node.rect.left},{node.rect.top} {node.rect.width}x{node.rect.height}")
    return " ".join(parts)


def prune(node: NodeInfo, *, keep_offscreen: bool = False) -> Optional[NodeInfo]:
    """Drop scaffolding that carries neither content nor a descendant worth showing."""

    kept_children = [
        pruned
        for pruned in (prune(child, keep_offscreen=keep_offscreen) for child in node.children)
        if pruned is not None
    ]
    node.children = kept_children

    if node.offscreen and not keep_offscreen and not kept_children:
        return None
    if kept_children:
        return node
    if node.is_interesting:
        return node
    return None


def _is_anonymous_container(node: NodeInfo) -> bool:
    return (
        node.role in CONTAINER_ROLES
        and not node.has_content
        and not node.is_actionable
        and bool(node.children)
    )


def collapse_chains(node: NodeInfo) -> NodeInfo:
    """Flatten ``pane > pane > pane > [controls]`` down to just the controls.

    Towers of nameless containers are the main source of noise in real Windows
    apps (Electron and WPF especially). A container is spliced away only when
    it is an only child, so genuine grouping — a toolbar next to a document
    pane — survives.
    """

    node.children = [collapse_chains(child) for child in node.children]
    guard = 0
    while len(node.children) == 1 and guard < 64:
        only = node.children[0]
        if not _is_anonymous_container(only):
            break
        node.children = only.children
        for child in node.children:
            child.depth = node.depth + 1
        guard += 1
    return node


def render_tree(
    root: NodeInfo,
    *,
    max_lines: int = 400,
    show_rect: bool = True,
    indent: str = "  ",
) -> str:
    """Render a pruned tree into the indented text a model reads."""

    lines: list[str] = []
    overflow = 0

    def _emit(node: NodeInfo, depth: int) -> None:
        nonlocal overflow
        if len(lines) >= max_lines:
            overflow += 1
            return
        lines.append(indent * depth + format_node_line(node, show_rect=show_rect))
        for child in node.children:
            _emit(child, depth + 1)
        if node.truncated_children:
            lines.append(indent * (depth + 1) + f"… ({node.truncated_children}개 더 있음)")

    _emit(root, 0)
    if overflow:
        lines.append(f"… 출력 한도({max_lines}줄)를 넘어 {overflow}개 노드를 생략했습니다.")
    return "\n".join(lines)


def assign_refs(root: NodeInfo, *, prefix: str = "e", start: int = 1) -> dict[str, NodeInfo]:
    """Give every node a short, stable-within-snapshot handle like ``e12``."""

    registry: dict[str, NodeInfo] = {}
    counter = start
    for node in root.walk():
        node.ref = f"{prefix}{counter}"
        registry[node.ref] = node
        counter += 1
    return registry


def score_match(node: NodeInfo, query: str, *, role: str | None = None) -> float:
    """Score how well a node matches a free-text query. 0 means no match."""

    if role and node.role != role.lower():
        return 0.0
    needle = query.strip().lower()
    if not needle:
        return 1.0 if not role else 0.5

    haystacks = [
        (node.name.lower(), 1.0),
        (node.automation_id.lower(), 0.9),
        (node.value.lower(), 0.7),
        (node.help_text.lower(), 0.6),
        (node.class_name.lower(), 0.4),
    ]
    best = 0.0
    for text, weight in haystacks:
        if not text:
            continue
        if text == needle:
            best = max(best, 1.0 * weight)
        elif text.startswith(needle):
            best = max(best, 0.85 * weight)
        elif needle in text:
            best = max(best, 0.7 * weight)
        else:
            # every query word present, in any order
            words = [w for w in needle.split() if w]
            if words and all(w in text for w in words):
                best = max(best, 0.6 * weight)
    if best and node.is_actionable:
        best += 0.05
    if best and not node.enabled:
        best -= 0.2
    return max(0.0, best)


def search(
    root: NodeInfo,
    query: str,
    *,
    role: str | None = None,
    limit: int = 20,
    actionable_only: bool = False,
) -> list[tuple[float, NodeInfo]]:
    """Rank nodes by how well they match ``query``."""

    scored: list[tuple[float, NodeInfo]] = []
    for node in root.walk():
        if actionable_only and not node.is_actionable:
            continue
        value = score_match(node, query, role=role)
        if value > 0:
            scored.append((value, node))
    scored.sort(key=lambda pair: (-pair[0], pair[1].depth))
    return scored[:limit]


def flatten_interactive(root: NodeInfo, *, limit: int = 200) -> list[NodeInfo]:
    """List just the things a model can act on — a compact 'what can I do' view."""

    out: list[NodeInfo] = []
    for node in root.walk():
        if node.is_actionable:
            out.append(node)
        if len(out) >= limit:
            break
    return out

