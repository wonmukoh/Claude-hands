"""A session is one attached window plus the element refs handed out for it.

The model works the way it does in a browser: take a snapshot, get short refs
(``e12``), then act on refs. Refs stay valid across actions; when the UI has
moved on, the session re-resolves a ref against a fresh tree by identity
(runtime id first, then role + name + automation id) instead of failing.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from .elements import (
    NodeInfo,
    assign_refs,
    collapse_chains,
    flatten_interactive,
    format_node_line,
    prune,
    render_tree,
    search,
)
from .win32.defs import ClaudeHandsError
from .win32.windows import (
    WindowInfo,
    WindowNotFoundError,
    describe_window,
    find_window,
    is_window,
)


class RefNotFoundError(ClaudeHandsError):
    """Raised when a ref cannot be resolved even after a refresh."""


class NoSessionError(ClaudeHandsError):
    """Raised when an action is attempted before attaching to a window."""


@dataclass
class SnapshotOptions:
    max_depth: int = 12
    max_children: int = 60
    max_nodes: int = 1200
    max_lines: int = 400
    show_rect: bool = True
    keep_offscreen: bool = False
    interactive_only: bool = False


@dataclass
class WindowSession:
    """State for one attached top-level window."""

    hwnd: int
    info: WindowInfo
    tree: Optional[NodeInfo] = None
    refs: dict[str, NodeInfo] = field(default_factory=dict)
    elements: dict[int, object] = field(default_factory=dict)  # id(node) -> UiaElement
    taken_at: float = 0.0
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # -- lifecycle --------------------------------------------------------
    def refresh_info(self) -> WindowInfo:
        self.info = describe_window(self.hwnd)
        return self.info

    def ensure_alive(self) -> None:
        if not is_window(self.hwnd):
            raise WindowNotFoundError(
                f"hwnd={self.hwnd} 창이 닫혔습니다. list_windows 로 다시 찾아 attach 하세요."
            )

    # -- snapshots --------------------------------------------------------
    def capture_tree(self, options: SnapshotOptions | None = None) -> NodeInfo:
        """Rebuild the UI tree for this window and re-issue refs."""

        from .uia.core import build_tree, element_from_hwnd

        options = options or SnapshotOptions()
        with self._lock:
            self.ensure_alive()
            root_element = element_from_hwnd(self.hwnd)
            tree, index = build_tree(
                root_element,
                max_depth=options.max_depth,
                max_children=options.max_children,
                max_nodes=options.max_nodes,
            )
            pruned = prune(tree, keep_offscreen=options.keep_offscreen) or tree
            pruned = collapse_chains(pruned)
            self.tree = pruned
            self.refs = assign_refs(pruned)
            self.elements = index
            self.taken_at = time.time()
            return pruned

    def render(self, options: SnapshotOptions | None = None) -> str:
        options = options or SnapshotOptions()
        tree = self.capture_tree(options)
        header = self._header()
        if options.interactive_only:
            nodes = flatten_interactive(tree)
            body = "\n".join(
                format_node_line(node, show_rect=options.show_rect) for node in nodes
            )
            if not body:
                body = "(조작 가능한 요소를 찾지 못했습니다. interactive_only=False 로 전체 트리를 보세요.)"
        else:
            body = render_tree(tree, max_lines=options.max_lines, show_rect=options.show_rect)
        return f"{header}\n{body}"

    def _header(self) -> str:
        info = self.refresh_info()
        return (
            f"창: {info.title or '(제목 없음)'} | {info.process} (pid {info.pid}) "
            f"| hwnd={info.hwnd} | 상태={info.state} "
            f"| 위치 {info.rect.left},{info.rect.top} {info.rect.width}x{info.rect.height}"
        )

    # -- refs -------------------------------------------------------------
    def _fingerprint(self, node: NodeInfo) -> tuple:
        return (node.role, node.name, node.automation_id, node.class_name)

    def resolve(self, ref: str, *, auto_refresh: bool = True):
        """Return ``(node, element)`` for a ref, refreshing the tree if needed."""

        with self._lock:
            node = self.refs.get(ref)
            if node is not None:
                element = self.elements.get(id(node))
                if element is not None and self._element_alive(element):
                    return node, element

            if not auto_refresh:
                raise RefNotFoundError(
                    f"{ref} 를 찾을 수 없습니다. snapshot 을 다시 찍어 새 ref 를 받으세요."
                )

            stale = node
            self.capture_tree()
            if stale is None:
                node = self.refs.get(ref)
                if node is None:
                    raise RefNotFoundError(
                        f"{ref} 는 이 창의 ref 가 아닙니다. snapshot 을 먼저 찍으세요."
                    )
                element = self.elements.get(id(node))
                if element is None:
                    raise RefNotFoundError(f"{ref} 에 연결된 UI 요소가 사라졌습니다.")
                return node, element

            match = self._find_equivalent(stale)
            if match is None:
                raise RefNotFoundError(
                    f"{ref} ({stale.role} \"{stale.name}\") 요소가 화면에서 사라졌습니다. "
                    "snapshot 을 다시 찍어 확인하세요."
                )
            element = self.elements.get(id(match))
            if element is None:
                raise RefNotFoundError(f"{ref} 를 다시 연결하지 못했습니다.")
            # Keep the old ref usable so a caller's plan does not break.
            self.refs[ref] = match
            return match, element

    def _find_equivalent(self, stale: NodeInfo) -> Optional[NodeInfo]:
        if self.tree is None:
            return None
        if stale.runtime_id:
            for node in self.tree.walk():
                if node.runtime_id and node.runtime_id == stale.runtime_id:
                    return node
        wanted = self._fingerprint(stale)
        for node in self.tree.walk():
            if self._fingerprint(node) == wanted:
                return node
        # Last resort: same role and name, ignoring class/automation id churn.
        for node in self.tree.walk():
            if node.role == stale.role and node.name and node.name == stale.name:
                return node
        return None

    def _element_alive(self, element) -> bool:
        try:
            return element.rect is not None or bool(element.name) or element.enabled
        except Exception:  # noqa: BLE001 - dead COM pointer
            return False

    # -- lookup -----------------------------------------------------------
    def find(
        self,
        query: str,
        *,
        role: str | None = None,
        limit: int = 10,
        actionable_only: bool = False,
        refresh: bool = True,
    ) -> list[tuple[float, NodeInfo]]:
        with self._lock:
            if refresh or self.tree is None:
                self.capture_tree()
            assert self.tree is not None
            return search(
                self.tree, query, role=role, limit=limit, actionable_only=actionable_only
            )

    def element_for(self, node: NodeInfo):
        element = self.elements.get(id(node))
        if element is None:
            raise RefNotFoundError(
                f"{node.role} \"{node.name}\" 에 연결된 UI 요소를 찾지 못했습니다."
            )
        return element


class SessionManager:
    """Holds every attached window and remembers which one is current."""

    def __init__(self) -> None:
        self._sessions: dict[int, WindowSession] = {}
        self._current: Optional[int] = None
        self._lock = threading.RLock()

    def attach(
        self,
        *,
        hwnd: int | None = None,
        title: str | None = None,
        process: str | None = None,
        pid: int | None = None,
        exact_title: bool = False,
    ) -> WindowSession:
        info = find_window(
            hwnd=hwnd, title=title, process=process, pid=pid, exact_title=exact_title
        )
        with self._lock:
            session = self._sessions.get(info.hwnd)
            if session is None:
                session = WindowSession(hwnd=info.hwnd, info=info)
                self._sessions[info.hwnd] = session
            else:
                session.info = info
            self._current = info.hwnd
            return session

    def current(self) -> WindowSession:
        with self._lock:
            if self._current is None:
                raise NoSessionError(
                    "아직 창에 연결하지 않았습니다. attach(title=...) 를 먼저 호출하세요."
                )
            session = self._sessions[self._current]
        session.ensure_alive()
        return session

    def get(self, hwnd: int | None) -> WindowSession:
        if hwnd is None:
            return self.current()
        with self._lock:
            session = self._sessions.get(hwnd)
            if session is None:
                return self.attach(hwnd=hwnd)
            self._current = hwnd
            return session

    def detach(self, hwnd: int | None = None) -> bool:
        with self._lock:
            target = hwnd if hwnd is not None else self._current
            if target is None:
                return False
            existed = self._sessions.pop(target, None) is not None
            if self._current == target:
                self._current = next(iter(self._sessions), None)
            return existed

    def all(self) -> list[WindowSession]:
        with self._lock:
            return list(self._sessions.values())
