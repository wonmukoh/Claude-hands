"""COM plumbing for UI Automation.

UIA is the reason this package can drive a window that is minimised: a UIA
pattern call (``Invoke``, ``SetValue``, ``Toggle``…) is a cross-process method
call into the target application, not a synthetic mouse event. The app runs the
same handler it would run for a real click, whether or not it is on screen.

All COM work is funnelled through a single multi-threaded-apartment worker
thread so that callers (including an asyncio MCP server) never have to think
about apartments.
"""

from __future__ import annotations

import os
import queue
import re
import sys
import threading
from concurrent.futures import Future
from typing import Any, Callable, Optional, TypeVar

from ..win32.defs import ClaudeHandsError, enable_dpi_awareness, require_windows
from ..win32.windows import Rect

T = TypeVar("T")


class UiaUnavailableError(ClaudeHandsError):
    """Raised when the UI Automation stack cannot be initialised."""


# COM apartment bookkeeping
# -------------------------
# comtypes joins an apartment the moment it is imported, using the *default*
# COINIT_APARTMENTTHREADED unless ``sys.coinit_flags`` was set first. Once a
# thread is in an apartment the choice is final: asking for a different one
# raises RPC_E_CHANGED_MODE. So the preference has to be registered here, at
# import of this module, before anything can pull comtypes in — importing it
# first on a worker thread and asking for the MTA afterwards fails outright.

RPC_E_CHANGED_MODE = -2147417850
COINIT_MULTITHREADED = 0


def prefer_mta() -> bool:
    """Ask that comtypes join the multi-threaded apartment. Call before import.

    Returns False when comtypes is already loaded and the preference can no
    longer be expressed — the process keeps whatever apartment it has, which
    still works for UI Automation.
    """

    if "comtypes" in sys.modules:
        return False
    sys.coinit_flags = COINIT_MULTITHREADED
    return True


prefer_mta()


def init_thread_com(comtypes_module: Any) -> str:
    """Join an apartment on the calling thread; report which one we got.

    An apartment already fixed by an earlier comtypes import is not an error:
    UI Automation is callable from a single-threaded apartment too, it just
    marshals more. Failing here would take the whole tool down for a
    difference that does not stop it working.
    """

    try:
        comtypes_module.CoInitializeEx(
            getattr(comtypes_module, "COINIT_MULTITHREADED", COINIT_MULTITHREADED)
        )
        return "mta"
    except OSError as exc:
        if getattr(exc, "winerror", None) == RPC_E_CHANGED_MODE:
            return "sta"
        raise


# --------------------------------------------------------------------------
# Worker thread (COM MTA)
# --------------------------------------------------------------------------


class UiaWorker:
    """Runs every UIA call on one dedicated MTA thread."""

    def __init__(self, name: str = "claude-hands-uia") -> None:
        self._queue: "queue.Queue[tuple[Callable[..., Any], tuple, dict, Future]]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._started = threading.Event()
        self._error: Optional[BaseException] = None
        self.apartment = "unknown"
        self._stop = object()
        self._thread.start()
        self._started.wait(timeout=15)
        if self._error is not None:
            raise UiaUnavailableError(str(self._error))

    def _run(self) -> None:  # pragma: no cover - Windows only
        try:
            prefer_mta()  # no-op if comtypes is already loaded
            import comtypes

            self.apartment = init_thread_com(comtypes)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the constructor
            self._error = exc
            self._started.set()
            return
        self._started.set()
        while True:
            item = self._queue.get()
            if item is self._stop:
                break
            func, args, kwargs, future = item
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(func(*args, **kwargs))
                except BaseException as exc:  # noqa: BLE001 - relayed to caller
                    future.set_exception(exc)
        try:
            import comtypes

            comtypes.CoUninitialize()
        except Exception:  # noqa: BLE001 - shutdown best effort
            pass

    def submit(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> "Future[T]":
        future: "Future[T]" = Future()
        self._queue.put((func, args, kwargs, future))
        return future

    def call(self, func: Callable[..., T], *args: Any, timeout: float = 60.0, **kwargs: Any) -> T:
        return self.submit(func, *args, **kwargs).result(timeout=timeout)

    def close(self) -> None:
        self._queue.put(self._stop)  # type: ignore[arg-type]


_worker: Optional[UiaWorker] = None
_worker_lock = threading.Lock()


def get_worker() -> UiaWorker:
    global _worker
    with _worker_lock:
        if _worker is None:
            require_windows()
            _worker = UiaWorker()
        return _worker


def shutdown() -> None:
    global _worker
    with _worker_lock:
        if _worker is not None:
            _worker.close()
            _worker = None


# --------------------------------------------------------------------------
# UIA module / instance
# --------------------------------------------------------------------------

_uia_module: Any = None
_uia: Any = None
_init_lock = threading.Lock()

CONTROL_TYPE_NAMES: dict[int, str] = {}
_CONTROL_TYPE_RE = re.compile(r"^UIA_(\w+)ControlTypeId$")

PATTERN_SPECS: dict[str, tuple[str, str]] = {
    "invoke": ("UIA_InvokePatternId", "IUIAutomationInvokePattern"),
    "value": ("UIA_ValuePatternId", "IUIAutomationValuePattern"),
    "toggle": ("UIA_TogglePatternId", "IUIAutomationTogglePattern"),
    "selectionitem": ("UIA_SelectionItemPatternId", "IUIAutomationSelectionItemPattern"),
    "selection": ("UIA_SelectionPatternId", "IUIAutomationSelectionPattern"),
    "expandcollapse": ("UIA_ExpandCollapsePatternId", "IUIAutomationExpandCollapsePattern"),
    "scroll": ("UIA_ScrollPatternId", "IUIAutomationScrollPattern"),
    "scrollitem": ("UIA_ScrollItemPatternId", "IUIAutomationScrollItemPattern"),
    "text": ("UIA_TextPatternId", "IUIAutomationTextPattern"),
    "rangevalue": ("UIA_RangeValuePatternId", "IUIAutomationRangeValuePattern"),
    "window": ("UIA_WindowPatternId", "IUIAutomationWindowPattern"),
    "legacy": ("UIA_LegacyIAccessiblePatternId", "IUIAutomationLegacyIAccessiblePattern"),
    "grid": ("UIA_GridPatternId", "IUIAutomationGridPattern"),
    "griditem": ("UIA_GridItemPatternId", "IUIAutomationGridItemPattern"),
    "table": ("UIA_TablePatternId", "IUIAutomationTablePattern"),
}

# Properties we want the cache to carry so a whole tree costs one round trip.
_CACHED_PROPERTY_NAMES = [
    "UIA_NamePropertyId",
    "UIA_ControlTypePropertyId",
    "UIA_AutomationIdPropertyId",
    "UIA_ClassNamePropertyId",
    "UIA_BoundingRectanglePropertyId",
    "UIA_IsEnabledPropertyId",
    "UIA_IsOffscreenPropertyId",
    "UIA_IsKeyboardFocusablePropertyId",
    "UIA_HasKeyboardFocusPropertyId",
    "UIA_NativeWindowHandlePropertyId",
    "UIA_HelpTextPropertyId",
    "UIA_ValueValuePropertyId",
    "UIA_ToggleToggleStatePropertyId",
    "UIA_ExpandCollapseExpandCollapseStatePropertyId",
    "UIA_SelectionItemIsSelectedPropertyId",
    "UIA_IsInvokePatternAvailablePropertyId",
    "UIA_IsValuePatternAvailablePropertyId",
    "UIA_IsTogglePatternAvailablePropertyId",
    "UIA_IsSelectionItemPatternAvailablePropertyId",
    "UIA_IsExpandCollapsePatternAvailablePropertyId",
    "UIA_IsScrollPatternAvailablePropertyId",
    "UIA_IsTextPatternAvailablePropertyId",
    "UIA_IsRangeValuePatternAvailablePropertyId",
]

TOGGLE_STATES = {0: "off", 1: "on", 2: "indeterminate"}
EXPAND_STATES = {0: "collapsed", 1: "expanded", 2: "partially-expanded", 3: "leaf"}


CLSID_CUIAUTOMATION = "{ff48dba4-60ef-4201-aa87-54103eef594e}"


def _describe_exception(exc: BaseException) -> str:
    """Some COM/codegen failures stringify to nothing; never report an empty reason."""

    text = " ".join(str(exc).split())
    return text or f"{type(exc).__name__} (상세 메시지 없음)"


def _create_by_clsid(module: Any, failures: list[str]) -> Any:  # pragma: no cover - Windows only
    """Create CUIAutomation straight from its CLSID when no coclass is exposed."""

    try:
        import ctypes

        import comtypes

        interface = getattr(module, "IUIAutomation", None)
        if interface is None:
            failures.append("IUIAutomation 인터페이스가 타입 라이브러리에 없음")
            return None
        clsid = comtypes.GUID(CLSID_CUIAUTOMATION)
        unknown = ctypes.POINTER(comtypes.IUnknown)()
        ctypes.oledll.ole32.CoCreateInstance(
            ctypes.byref(clsid),
            None,
            1,  # CLSCTX_INPROC_SERVER
            ctypes.byref(comtypes.IUnknown._iid_),
            ctypes.byref(unknown),
        )
        return unknown.QueryInterface(interface)
    except Exception as exc:  # noqa: BLE001 - last resort
        failures.append(f"CoCreateInstance(CLSID): {_describe_exception(exc)}")
        return None


def _load() -> tuple[Any, Any]:  # pragma: no cover - Windows only
    """Import the generated UIAutomationCore wrapper and create IUIAutomation."""

    global _uia_module, _uia
    if _uia is not None:
        return _uia_module, _uia

    require_windows()
    prefer_mta()  # only effective if nothing has imported comtypes yet

    try:
        import comtypes.client
    except ImportError as exc:
        raise UiaUnavailableError(
            "comtypes 가 설치되어 있지 않습니다. `pip install comtypes` 후 다시 시도하세요."
        ) from exc

    system32 = os.path.join(
        os.environ.get("SystemRoot", r"C:\\Windows"), "System32", "UIAutomationCore.dll"
    )
    module = None
    failures: list[str] = []
    # The bare name is the documented form, but it fails on installs where
    # comtypes cannot resolve it to a path it can stat; the absolute path and
    # the registered typelib GUID are the ways back in.
    for label, source in (
        ("UIAutomationCore.dll", "UIAutomationCore.dll"),
        (system32, system32),
        ("typelib GUID", ("{944DE083-8FB8-45CF-BCB7-C477ACB2F897}", 1, 0)),
    ):
        try:
            module = comtypes.client.GetModule(source)
            break
        except Exception as exc:  # noqa: BLE001 - any COM/codegen failure
            failures.append(f"{label}: {_describe_exception(exc)}")
    if module is None:
        raise UiaUnavailableError(
            "UIAutomationCore 타입 라이브러리를 불러오지 못했습니다.\n  "
            + "\n  ".join(failures)
            + "\ncomtypes 캐시 디렉터리에 쓰기 권한이 있는지 확인하세요. "
            "Wine 처럼 UIA 클라이언트 타입 라이브러리가 없는 환경이라면 "
            "engine='win32' 로 창 메시지 기반 폴백 엔진을 쓰세요."
        )
    if not hasattr(module, "IUIAutomationElement"):
        raise UiaUnavailableError(
            f"불러온 타입 라이브러리({module.__name__})에 UIA 클라이언트 인터페이스가 "
            "없습니다. Wine 은 공급자 측 전용 타입 라이브러리만 제공하므로 UIA 엔진을 쓸 수 "
            "없습니다. engine='win32' 로 창 메시지 기반 폴백 엔진을 쓰세요."
        )

    instance = None
    creation_failures: list[str] = []
    for coclass, interface in (
        ("CUIAutomation8", "IUIAutomation6"),
        ("CUIAutomation8", "IUIAutomation2"),
        ("CUIAutomation", "IUIAutomation"),
    ):
        if not hasattr(module, coclass) or not hasattr(module, interface):
            continue
        try:
            instance = comtypes.client.CreateObject(
                getattr(module, coclass), interface=getattr(module, interface)
            )
            break
        except Exception as exc:  # noqa: BLE001 - try the next combination
            creation_failures.append(f"{coclass}/{interface}: {_describe_exception(exc)}")
    if instance is None:
        # Some type libraries describe the interfaces but omit the coclasses.
        instance = _create_by_clsid(module, creation_failures)
    if instance is None:
        raise UiaUnavailableError(
            "IUIAutomation 인스턴스를 생성하지 못했습니다.\n  "
            + "\n  ".join(creation_failures or ["(원인 불명)"])
        )

    if not CONTROL_TYPE_NAMES:
        for attribute in dir(module):
            match = _CONTROL_TYPE_RE.match(attribute)
            if match:
                CONTROL_TYPE_NAMES[getattr(module, attribute)] = match.group(1).lower()

    _uia_module, _uia = module, instance
    return module, instance


_thread_state = threading.local()


def ensure_com() -> None:
    """Join the multi-threaded apartment on whatever thread is calling.

    In an MTA, interface pointers are free-threaded, so an element built on the
    worker can be read from the caller — but only once that caller has itself
    initialised COM.
    """

    if getattr(_thread_state, "ready", False):
        return
    try:
        prefer_mta()
        import comtypes

        init_thread_com(comtypes)
    except Exception:  # noqa: BLE001 - never block on COM bookkeeping
        pass
    _thread_state.ready = True


def get_automation() -> tuple[Any, Any]:
    """Return ``(generated_module, IUIAutomation)``, initialising on first use."""

    if _uia is not None:
        ensure_com()
        return _uia_module, _uia
    with _init_lock:
        if _uia is not None:
            ensure_com()
            return _uia_module, _uia
        enable_dpi_awareness()
        result = get_worker().call(_load)
        ensure_com()
        return result


def control_type_name(control_type: int) -> str:
    return CONTROL_TYPE_NAMES.get(control_type, f"type{control_type}")


# --------------------------------------------------------------------------
# Element wrapper
# --------------------------------------------------------------------------


def _as_int(value: Any) -> Optional[int]:
    """Coerce a VARIANT-ish property value to int, or None if it isn't one."""

    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe(getter: Callable[[], T], default: T) -> T:
    try:
        return getter()
    except Exception:  # noqa: BLE001 - COM props fail for dying elements
        return default


class UiaElement:
    """A thin, forgiving wrapper around ``IUIAutomationElement``.

    Property reads prefer the cache (filled by :func:`build_tree`) and fall back
    to a live read, because an element may be handed to us either way.
    """

    __slots__ = ("com", "_cached", "_props")

    def __init__(self, com_element: Any, *, cached: bool = False) -> None:
        self.com = com_element
        self._cached = cached
        self._props: dict[str, Any] = {}

    # -- raw property access ---------------------------------------------
    def _prop(self, name: str, default: Any = None) -> Any:
        if name in self._props:
            return self._props[name]
        ensure_com()
        value = default
        if self._cached:
            value = _safe(lambda: getattr(self.com, f"Cached{name}"), None)
        if value is None:
            value = _safe(lambda: getattr(self.com, f"Current{name}"), default)
        if value is None:
            value = default
        self._props[name] = value
        return value

    @property
    def name(self) -> str:
        return (self._prop("Name", "") or "").strip()

    @property
    def control_type(self) -> int:
        return int(self._prop("ControlType", 0) or 0)

    @property
    def role(self) -> str:
        return control_type_name(self.control_type)

    @property
    def automation_id(self) -> str:
        return self._prop("AutomationId", "") or ""

    @property
    def class_name(self) -> str:
        return self._prop("ClassName", "") or ""

    @property
    def help_text(self) -> str:
        return self._prop("HelpText", "") or ""

    @property
    def enabled(self) -> bool:
        return bool(self._prop("IsEnabled", True))

    @property
    def offscreen(self) -> bool:
        return bool(self._prop("IsOffscreen", False))

    @property
    def focusable(self) -> bool:
        return bool(self._prop("IsKeyboardFocusable", False))

    @property
    def focused(self) -> bool:
        return bool(self._prop("HasKeyboardFocus", False))

    @property
    def hwnd(self) -> int:
        return int(self._prop("NativeWindowHandle", 0) or 0)

    @property
    def rect(self) -> Optional[Rect]:
        raw = self._prop("BoundingRectangle", None)
        if raw is None:
            return None
        try:
            left, top, right, bottom = int(raw.left), int(raw.top), int(raw.right), int(raw.bottom)
        except AttributeError:
            try:
                left, top, right, bottom = (int(v) for v in raw)
            except Exception:  # noqa: BLE001
                return None
        if right <= left or bottom <= top:
            return Rect(left, top, left, top)
        return Rect(left, top, right, bottom)

    @property
    def runtime_id(self) -> tuple[int, ...]:
        ensure_com()
        raw = _safe(lambda: self.com.GetRuntimeId(), None)
        if not raw:
            return ()
        return tuple(int(v) for v in raw)

    def refresh(self) -> "UiaElement":
        """Drop memoised property values so the next read hits the live UI."""

        self._props.clear()
        self._cached = False
        return self

    def property_value(self, property_name: str, default: Any = None) -> Any:
        """Read a property by id, preferring the cache filled by ``build_tree``.

        Pattern-backed properties (a value box's text, a checkbox's state) are
        readable this way without instantiating the pattern, which turns a
        cross-process call per node into a cache lookup.
        """

        module, _ = get_automation()
        property_id = getattr(module, property_name, None)
        if property_id is None:
            return default
        if self._cached:
            value = _safe(lambda: self.com.GetCachedPropertyValue(property_id), None)
            if value is not None:
                return value
        value = _safe(lambda: self.com.GetCurrentPropertyValue(property_id), None)
        return default if value is None else value

    def value(self) -> str:
        pattern = self.pattern("value")
        if pattern is not None:
            text = _safe(lambda: pattern.CurrentValue, "")
            if text:
                return str(text)
        pattern = self.pattern("rangevalue")
        if pattern is not None:
            number = _safe(lambda: pattern.CurrentValue, None)
            if number is not None:
                return str(number)
        return ""

    def toggle_state(self) -> Optional[str]:
        pattern = self.pattern("toggle")
        if pattern is None:
            return None
        state = _safe(lambda: int(pattern.CurrentToggleState), None)
        return TOGGLE_STATES.get(state) if state is not None else None

    def expand_state(self) -> Optional[str]:
        pattern = self.pattern("expandcollapse")
        if pattern is None:
            return None
        state = _safe(lambda: int(pattern.CurrentExpandCollapseState), None)
        return EXPAND_STATES.get(state) if state is not None else None

    def selected(self) -> Optional[bool]:
        pattern = self.pattern("selectionitem")
        if pattern is None:
            return None
        return _safe(lambda: bool(pattern.CurrentIsSelected), None)

    # -- patterns ---------------------------------------------------------
    def pattern(self, key: str) -> Any:
        """Return a pattern interface, or ``None`` when unsupported."""

        module, _ = get_automation()
        ensure_com()
        spec = PATTERN_SPECS.get(key)
        if spec is None:
            raise ClaudeHandsError(f"알 수 없는 패턴: {key!r}")
        pattern_id_name, interface_name = spec
        pattern_id = getattr(module, pattern_id_name, None)
        interface = getattr(module, interface_name, None)
        if pattern_id is None or interface is None:
            return None
        raw = _safe(lambda: self.com.GetCurrentPattern(pattern_id), None)
        if not raw:
            return None
        return _safe(lambda: raw.QueryInterface(interface), None)

    def available_patterns(self) -> tuple[str, ...]:
        found: list[str] = []
        for key in (
            "invoke",
            "value",
            "toggle",
            "selectionitem",
            "expandcollapse",
            "scroll",
            "scrollitem",
            "text",
            "rangevalue",
            "legacy",
            "window",
        ):
            if self.pattern(key) is not None:
                found.append(key)
        return tuple(found)

    # -- navigation -------------------------------------------------------
    def children(self, *, cached: bool = False) -> list["UiaElement"]:
        ensure_com()
        if cached:
            array = _safe(lambda: self.com.GetCachedChildren(), None)
            if array is not None:
                return [
                    UiaElement(array.GetElement(i), cached=True)
                    for i in range(_safe(lambda: array.Length, 0))
                ]
        module, automation = get_automation()
        walker = automation.ControlViewWalker
        out: list[UiaElement] = []
        child = _safe(lambda: walker.GetFirstChildElement(self.com), None)
        guard = 0
        while child and guard < 500:
            out.append(UiaElement(child))
            child = _safe(lambda: walker.GetNextSiblingElement(child), None)
            guard += 1
        return out

    def parent(self) -> Optional["UiaElement"]:
        _, automation = get_automation()
        walker = automation.ControlViewWalker
        parent = _safe(lambda: walker.GetParentElement(self.com), None)
        return UiaElement(parent) if parent else None

    def to_node_info(self, depth: int = 0):
        from ..elements import NodeInfo

        patterns: list[str] = []
        for key, prop in (
            ("invoke", "IsInvokePatternAvailable"),
            ("value", "IsValuePatternAvailable"),
            ("toggle", "IsTogglePatternAvailable"),
            ("selectionitem", "IsSelectionItemPatternAvailable"),
            ("expandcollapse", "IsExpandCollapsePatternAvailable"),
            ("scroll", "IsScrollPatternAvailable"),
            ("text", "IsTextPatternAvailable"),
            ("rangevalue", "IsRangeValuePatternAvailable"),
        ):
            if bool(self._prop(prop, False)):
                patterns.append(key)

        value = ""
        toggle = None
        expand = None
        selected = None
        if "value" in patterns or "rangevalue" in patterns:
            raw = self.property_value("UIA_ValueValuePropertyId", "")
            value = "" if raw is None else str(raw)
        if "toggle" in patterns:
            state = _as_int(self.property_value("UIA_ToggleToggleStatePropertyId"))
            toggle = TOGGLE_STATES.get(state) if state is not None else None
        if "expandcollapse" in patterns:
            state = _as_int(
                self.property_value("UIA_ExpandCollapseExpandCollapseStatePropertyId")
            )
            expand = EXPAND_STATES.get(state) if state is not None else None
        if "selectionitem" in patterns:
            raw = self.property_value("UIA_SelectionItemIsSelectedPropertyId")
            selected = bool(raw) if raw is not None else None

        return NodeInfo(
            role=self.role,
            name=self.name,
            value=value,
            automation_id=self.automation_id,
            class_name=self.class_name,
            rect=self.rect,
            enabled=self.enabled,
            offscreen=self.offscreen,
            focusable=self.focusable,
            focused=self.focused,
            hwnd=self.hwnd,
            runtime_id=self.runtime_id,
            patterns=tuple(patterns),
            toggle_state=toggle,
            expand_state=expand,
            selected=selected,
            help_text=self.help_text,
            depth=depth,
        )


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def element_from_hwnd(hwnd: int) -> UiaElement:
    module, automation = get_automation()
    element = get_worker().call(lambda: automation.ElementFromHandle(hwnd))
    if not element:
        raise UiaUnavailableError(f"hwnd={hwnd} 에서 UIA 요소를 얻지 못했습니다.")
    return UiaElement(element)


def focused_element() -> Optional[UiaElement]:
    _, automation = get_automation()
    element = get_worker().call(lambda: automation.GetFocusedElement())
    return UiaElement(element) if element else None


def _make_cache_request() -> Any:  # pragma: no cover - Windows only
    module, automation = get_automation()
    request = automation.CreateCacheRequest()
    request.AutomationElementMode = module.AutomationElementMode_Full
    request.TreeScope = module.TreeScope_Subtree
    request.TreeFilter = automation.ControlViewCondition
    for name in _CACHED_PROPERTY_NAMES:
        property_id = getattr(module, name, None)
        if property_id is not None:
            try:
                request.AddProperty(property_id)
            except Exception:  # noqa: BLE001 - unsupported property on old OS
                continue
    return request


def build_tree(
    root: UiaElement,
    *,
    max_depth: int = 12,
    max_children: int = 60,
    max_nodes: int = 1200,
    use_cache: bool = True,
):
    """Materialise a :class:`~claude_hands.elements.NodeInfo` tree from ``root``.

    One cached subtree request keeps this to a single cross-process round trip
    on well-behaved apps; anything that refuses caching falls back to walking
    the live control view.

    Returns ``(tree, element_index)`` where ``element_index`` maps ``id(node)``
    to the live :class:`UiaElement` behind it, so actions can be dispatched
    against the very element that produced a line of the snapshot.
    """

    from ..elements import NodeInfo

    element_index: dict[int, UiaElement] = {}

    def _work() -> NodeInfo:
        cached_root = root
        cached = False
        if use_cache:
            try:
                request = _make_cache_request()
                updated = root.com.BuildUpdatedCache(request)
                cached_root = UiaElement(updated, cached=True)
                cached = True
            except Exception:  # noqa: BLE001 - fall back to live walking
                cached_root = root
                cached = False

        counter = {"nodes": 0}

        def _descend(element: UiaElement, depth: int) -> NodeInfo:
            node = element.to_node_info(depth)
            element_index[id(node)] = element
            counter["nodes"] += 1
            if depth >= max_depth or counter["nodes"] >= max_nodes:
                return node
            try:
                children = element.children(cached=cached)
            except Exception:  # noqa: BLE001 - element vanished mid-walk
                children = []
            if len(children) > max_children:
                node.truncated_children = len(children) - max_children
                children = children[:max_children]
            for child in children:
                if counter["nodes"] >= max_nodes:
                    node.truncated_children += 1
                    continue
                node.children.append(_descend(child, depth + 1))
            return node

        return _descend(cached_root, 0)

    tree = get_worker().call(_work, timeout=120.0)
    return tree, element_index
