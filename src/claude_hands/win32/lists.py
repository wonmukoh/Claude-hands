"""Choosing in classic Win32 lists without handing them the keyboard.

UI Automation selects a list item by focusing the list first. Measured on
examples/list_target.py (2026-11-05) with another program in front: Select,
Expand and Invoke on a ListBox, ComboBox, SysTreeView32 and SysListView32
item each moved the foreground to the target's process — while the window was
visible, and still while it was **minimised**, where the window stayed down
but the keyboard went to a list nobody could see. A person typing elsewhere
would have typed into it. Lesson 29 found the same for buttons; these are the
classes it had not measured.

So these controls are driven the way they drive themselves: the selection
message the control understands, then the notification it would have sent its
parent. No focus is involved. ListBox and ComboBox messages sit below
WM_USER, so Windows copies their strings between processes for us; the tree
and list view messages take structures, which have to live in the target
process — `_Remote` borrows a page there, and lays the structure out for a
32-bit program when the target is one (K-에듀파인's WXSClient is).
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Optional

from .defs import GWL_STYLE, SMTO_ABORTIFHUNG, ClaudeHandsError, require_windows

WM_COMMAND = 0x0111

LB_SETSEL, LB_SETCURSEL, LB_GETSEL, LB_GETCURSEL = 0x0185, 0x0186, 0x0187, 0x0188
LB_GETTEXT, LB_GETTEXTLEN, LB_GETCOUNT = 0x0189, 0x018A, 0x018B
LBS_NOTIFY, LBS_MULTIPLESEL, LBS_HASSTRINGS, LBS_EXTENDEDSEL = 0x0001, 0x0008, 0x0040, 0x0800
LBS_OWNERDRAWFIXED, LBS_OWNERDRAWVARIABLE = 0x0010, 0x0020
LBN_SELCHANGE = 1

CB_GETCOUNT, CB_GETCURSEL, CB_GETLBTEXT, CB_GETLBTEXTLEN = 0x0146, 0x0147, 0x0148, 0x0149
CB_SETCURSEL, CB_SHOWDROPDOWN, CB_GETDROPPEDSTATE = 0x014E, 0x014F, 0x0157
CBS_TYPEMASK, CBS_SIMPLE, CBS_DROPDOWN, CBS_DROPDOWNLIST = 0x0003, 0x0001, 0x0002, 0x0003
CBN_SELCHANGE, CBN_EDITCHANGE, CBN_SELENDOK = 1, 5, 9

TVM_EXPAND, TVM_GETNEXTITEM, TVM_SELECTITEM = 0x1102, 0x110A, 0x110B
TVM_GETITEMSTATE, TVM_GETITEMW, TVM_ENSUREVISIBLE = 0x1127, 0x113E, 0x1114
WM_KEYDOWN, WM_KEYUP, VK_ADD = 0x0100, 0x0101, 0x6B
TVGN_ROOT, TVGN_NEXT, TVGN_CHILD, TVGN_CARET = 0x0, 0x1, 0x4, 0x9
TVE_COLLAPSE, TVE_EXPAND = 0x1, 0x2
TVIF_TEXT, TVIF_PARAM, TVIF_STATE, TVIF_HANDLE, TVIF_CHILDREN = 0x1, 0x4, 0x8, 0x10, 0x40
TVIS_SELECTED, TVIS_EXPANDED = 0x02, 0x20

LVM_GETITEMCOUNT, LVM_ENSUREVISIBLE, LVM_SETITEMSTATE = 0x1004, 0x1013, 0x102B
LVM_GETITEMSTATE, LVM_SETSELECTIONMARK, LVM_GETITEMTEXTW = 0x102C, 0x1043, 0x1073
LVIS_FOCUSED, LVIS_SELECTED = 0x1, 0x2
LVS_SINGLESEL = 0x0004

PROCESS_VM_OPERATION, PROCESS_VM_READ, PROCESS_VM_WRITE = 0x0008, 0x0010, 0x0020
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MEM_COMMIT_RESERVE, MEM_RELEASE, PAGE_READWRITE = 0x3000, 0x8000, 0x04
TEXT_CHARS = 260

LIST_CLASSES = {
    "listbox": "listbox",
    "combolbox": "combolbox",
    "combobox": "combobox",
    "systreeview32": "treeview",
    "syslistview32": "listview",
}


class NativeListError(ClaudeHandsError):
    """A classic list could not be driven by its own messages."""


def _user32():
    from .defs import user32

    return user32


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32().GetClassNameW(hwnd, buf, 256)
    return buf.value


def list_kind(hwnd: int) -> str:
    """Which of the classic list controls ``hwnd`` is, or ``""``."""

    if not hwnd:
        return ""
    return LIST_CLASSES.get(_class_name(hwnd).lower(), "")


def _signed(value: int) -> int:
    # SendMessageTimeout hands LRESULT back through a size_t, so LB_ERR (-1)
    # arrives as 2**64 - 1.
    bits = ctypes.sizeof(ctypes.c_size_t) * 8
    return value - (1 << bits) if value >= 1 << (bits - 1) else value


def _send(hwnd: int, message: int, wparam: int = 0, lparam: int = 0, timeout_ms: int = 2000) -> int:
    result = ctypes.c_size_t(0)
    ok = _user32().SendMessageTimeoutW(
        hwnd, message, wparam & ((1 << 64) - 1), lparam, SMTO_ABORTIFHUNG, timeout_ms,
        ctypes.byref(result),
    )
    if not ok:
        raise NativeListError(
            "목록 컨트롤이 응답하지 않습니다. 프로그램이 바쁘거나 대화상자가 떠 있을 수 있습니다. "
            "list_windows 로 떠 있는 창을 확인하세요."
        )
    return _signed(int(result.value))


def _style(hwnd: int) -> int:
    return _user32().GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF


def _ensure_enabled(hwnd: int) -> None:
    # The notifications below reach the parent whatever state the control is
    # in; a person could not choose in a disabled list, or in a dialog that a
    # modal box has disabled.
    user32 = _user32()
    root = user32.GetAncestor(hwnd, 2) or hwnd  # GA_ROOT
    if not user32.IsWindowEnabled(hwnd) or not user32.IsWindowEnabled(root):
        raise NativeListError(
            "비활성 목록이거나 창이 다른 대화상자에 막혀 있어 고르지 않았습니다. "
            "떠 있는 대화상자를 먼저 처리하세요."
        )


def _notify_command(hwnd: int, code: int) -> None:
    user32 = _user32()
    parent = user32.GetParent(hwnd)
    if not parent:
        return
    control_id = user32.GetDlgCtrlID(hwnd) & 0xFFFF
    _send(parent, WM_COMMAND, ((code & 0xFFFF) << 16) | control_id, hwnd)


# --------------------------------------------------------------------------
# ListBox
# --------------------------------------------------------------------------


def _string_items(hwnd: int, count_msg: int, len_msg: int, text_msg: int) -> list[str]:
    count = _send(hwnd, count_msg)
    out: list[str] = []
    for index in range(max(count, 0)):
        length = _send(hwnd, len_msg, index)
        if length < 0:
            out.append("")
            continue
        buf = ctypes.create_unicode_buffer(length + 1)
        _send(hwnd, text_msg, index, ctypes.addressof(buf))
        out.append(buf.value)
    return out


def listbox_texts(hwnd: int) -> list[str]:
    style = _style(hwnd)
    if style & (LBS_OWNERDRAWFIXED | LBS_OWNERDRAWVARIABLE) and not style & LBS_HASSTRINGS:
        # An owner-drawn list keeps item data, not words; LB_GETTEXT would
        # copy a pointer's bytes out as if they were text.
        return [""] * max(_send(hwnd, LB_GETCOUNT), 0)
    return _string_items(hwnd, LB_GETCOUNT, LB_GETTEXTLEN, LB_GETTEXT)


def listbox_select(hwnd: int, index: int, *, add: bool = False) -> None:
    """Select item ``index`` as a click would, and say so to the parent."""

    _ensure_enabled(hwnd)
    style = _style(hwnd)
    if style & (LBS_MULTIPLESEL | LBS_EXTENDEDSEL):
        if not add:
            _send(hwnd, LB_SETSEL, 0, -1)
        _send(hwnd, LB_SETSEL, 1, index)
        landed = _send(hwnd, LB_GETSEL, index) > 0
    else:
        _send(hwnd, LB_SETCURSEL, index)
        landed = _send(hwnd, LB_GETCURSEL) == index
    if not landed:
        raise NativeListError("목록에서 항목을 골랐지만 선택이 반영되지 않았습니다.")
    # A list without LBS_NOTIFY says nothing when clicked either; its program
    # reads the selection when it needs it.
    if style & LBS_NOTIFY:
        _notify_command(hwnd, LBN_SELCHANGE)


# --------------------------------------------------------------------------
# ComboBox
# --------------------------------------------------------------------------


def combo_texts(hwnd: int) -> list[str]:
    return _string_items(hwnd, CB_GETCOUNT, CB_GETLBTEXTLEN, CB_GETLBTEXT)


def combo_editable(hwnd: int) -> bool:
    return _style(hwnd) & CBS_TYPEMASK in (CBS_SIMPLE, CBS_DROPDOWN)


def combo_select(hwnd: int, index: int) -> None:
    _ensure_enabled(hwnd)
    _send(hwnd, CB_SETCURSEL, index)
    if _send(hwnd, CB_GETCURSEL) != index:
        raise NativeListError("콤보 상자에서 항목을 골랐지만 선택이 반영되지 않았습니다.")
    # What choosing from the list tells the parent: the selection moved, and
    # the choice was accepted. CB_SETCURSEL alone tells it nothing.
    _notify_command(hwnd, CBN_SELCHANGE)
    if _send(hwnd, CB_GETDROPPEDSTATE):
        _send(hwnd, CB_SHOWDROPDOWN, 0)  # sends CBN_CLOSEUP itself
    _notify_command(hwnd, CBN_SELENDOK)


def combo_show_dropdown(hwnd: int, show: bool) -> None:
    # Measured: CB_SHOWDROPDOWN opens the list with the foreground untouched,
    # where UIA's Expand on the same combo handed it the foreground.
    _ensure_enabled(hwnd)
    _send(hwnd, CB_SHOWDROPDOWN, 1 if show else 0)


def combo_dropped(hwnd: int) -> bool:
    return bool(_send(hwnd, CB_GETDROPPEDSTATE))


def combo_set_edit_text(hwnd: int, text: str) -> None:
    from .input import set_window_text

    _ensure_enabled(hwnd)
    if not set_window_text(hwnd, text):
        raise NativeListError("콤보 상자의 입력칸에 글자를 넣지 못했습니다.")
    _notify_command(hwnd, CBN_EDITCHANGE)


class COMBOBOXINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcItem", wintypes.RECT),
        ("rcButton", wintypes.RECT),
        ("stateButton", wintypes.DWORD),
        ("hwndCombo", wintypes.HWND),
        ("hwndItem", wintypes.HWND),
        ("hwndList", wintypes.HWND),
    ]


def combo_for_dropdown(list_hwnd: int, top_hwnd: int) -> int:
    """The combo box whose open list ``list_hwnd`` (a ComboLBox) is, or 0.

    The dropped list is a top-level window of its own, so its parent says
    nothing; the combo is found by asking each combo for its list.
    """

    from .windows import enum_child_windows

    get_info = _user32().GetComboBoxInfo
    # Without argtypes ctypes passes an HWND as a C int, which a handle past
    # 0x7FFFFFFF overflows.
    get_info.argtypes = [wintypes.HWND, ctypes.POINTER(COMBOBOXINFO)]
    get_info.restype = wintypes.BOOL
    for combo in enum_child_windows(top_hwnd, lambda h: _class_name(h).lower() == "combobox"):
        info = COMBOBOXINFO()
        info.cbSize = ctypes.sizeof(COMBOBOXINFO)
        if get_info(combo, ctypes.byref(info)) and info.hwndList == list_hwnd:
            return combo
    return 0


# --------------------------------------------------------------------------
# Memory in the target process (tree view, list view)
# --------------------------------------------------------------------------


def _kernel32():
    # A private handle, so these argtypes never clash with defs.kernel32's.
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
    k.VirtualAllocEx.restype = ctypes.c_void_p
    k.VirtualFreeEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
    k.WriteProcessMemory.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)
    ]
    k.ReadProcessMemory.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)
    ]
    k.IsWow64Process.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    k.GetCurrentProcess.restype = wintypes.HANDLE
    return k


def _structs(ptr):
    """Structure layouts for a process whose pointers are ``ptr`` wide."""

    class TVITEMW(ctypes.Structure):
        _fields_ = [
            ("mask", ctypes.c_uint32), ("hItem", ptr), ("state", ctypes.c_uint32),
            ("stateMask", ctypes.c_uint32), ("pszText", ptr), ("cchTextMax", ctypes.c_int32),
            ("iImage", ctypes.c_int32), ("iSelectedImage", ctypes.c_int32),
            ("cChildren", ctypes.c_int32), ("lParam", ptr),
        ]

    class LVITEMW(ctypes.Structure):
        _fields_ = [
            ("mask", ctypes.c_uint32), ("iItem", ctypes.c_int32), ("iSubItem", ctypes.c_int32),
            ("state", ctypes.c_uint32), ("stateMask", ctypes.c_uint32), ("pszText", ptr),
            ("cchTextMax", ctypes.c_int32), ("iImage", ctypes.c_int32), ("lParam", ptr),
            ("iIndent", ctypes.c_int32), ("iGroupId", ctypes.c_int32), ("cColumns", ctypes.c_uint32),
            ("puColumns", ptr), ("piColFmt", ptr), ("iGroup", ctypes.c_int32),
        ]

    return TVITEMW, LVITEMW


class _Remote:
    """One page of memory inside the control's process, freed on exit."""

    SIZE = 4096

    def __init__(self, hwnd: int):
        require_windows()
        self.k = _kernel32()
        pid = wintypes.DWORD()
        _user32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        access = PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_QUERY_LIMITED_INFORMATION
        self.process = self.k.OpenProcess(access, False, pid.value)
        if not self.process:
            raise NativeListError(
                "목록이 있는 프로그램의 메모리를 열 수 없습니다. 그 프로그램이 관리자 권한으로 "
                "실행 중이면 이 도구도 관리자 권한이어야 합니다."
            )
        wow = wintypes.BOOL(False)
        self.k.IsWow64Process(self.process, ctypes.byref(wow))
        ours = ctypes.sizeof(ctypes.c_void_p)
        if ours == 4 and not wow.value and _is_64bit_os(self.k):
            self.close()
            raise NativeListError("32비트 파이썬으로는 64비트 프로그램의 목록을 다룰 수 없습니다.")
        self.ptr = ctypes.c_uint32 if (wow.value or ours == 4) else ctypes.c_uint64
        self.TVITEMW, self.LVITEMW = _structs(self.ptr)
        self.base = self.k.VirtualAllocEx(self.process, None, self.SIZE, MEM_COMMIT_RESERVE, PAGE_READWRITE)
        if not self.base:
            self.close()
            raise NativeListError("목록이 있는 프로그램 안에 작업 공간을 만들지 못했습니다.")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        if getattr(self, "base", None):
            self.k.VirtualFreeEx(self.process, self.base, 0, MEM_RELEASE)
            self.base = None
        if getattr(self, "process", None):
            self.k.CloseHandle(self.process)
            self.process = None

    def write(self, offset: int, obj) -> int:
        done = ctypes.c_size_t()
        if not self.k.WriteProcessMemory(
            self.process, self.base + offset, ctypes.byref(obj), ctypes.sizeof(obj), ctypes.byref(done)
        ):
            raise NativeListError("목록이 있는 프로그램의 메모리에 쓰지 못했습니다.")
        return self.base + offset

    def read(self, offset: int, obj):
        done = ctypes.c_size_t()
        if not self.k.ReadProcessMemory(
            self.process, self.base + offset, ctypes.byref(obj), ctypes.sizeof(obj), ctypes.byref(done)
        ):
            raise NativeListError("목록이 있는 프로그램의 메모리를 읽지 못했습니다.")
        return obj

    def read_text(self, offset: int, chars: int) -> str:
        buf = ctypes.create_unicode_buffer(chars)
        self.read(offset, buf)
        return buf.value


def _is_64bit_os(k) -> bool:
    wow = wintypes.BOOL(False)
    k.IsWow64Process(k.GetCurrentProcess(), ctypes.byref(wow))
    return bool(wow.value)


# --------------------------------------------------------------------------
# Tree view
# --------------------------------------------------------------------------

TEXT_AT = 1024  # the text buffer sits after the structures in the page


def _tree_text(remote: _Remote, hwnd: int, hitem: int) -> str:
    item = remote.TVITEMW(mask=TVIF_TEXT | TVIF_HANDLE, hItem=hitem,
                          pszText=remote.base + TEXT_AT, cchTextMax=TEXT_CHARS)
    address = remote.write(0, item)
    if not _send(hwnd, TVM_GETITEMW, 0, address):
        return ""
    return remote.read_text(TEXT_AT, TEXT_CHARS)


def _tree_children(hwnd: int, parent: int) -> list[int]:
    out: list[int] = []
    item = _send(hwnd, TVM_GETNEXTITEM, TVGN_CHILD if parent else TVGN_ROOT, parent)
    while item and len(out) < 100_000:
        out.append(item)
        item = _send(hwnd, TVM_GETNEXTITEM, TVGN_NEXT, item)
    return out


def tree_find(hwnd: int, path: list[tuple[str, int]]) -> int:
    """The HTREEITEM at ``path`` — (name, nth of that name among siblings) per level.

    Names, not positions: a tree that gained an item since the snapshot would
    make a position point at the wrong row.
    """

    with _Remote(hwnd) as remote:
        parent = 0
        for name, twin in path:
            matches = [h for h in _tree_children(hwnd, parent) if _tree_text(remote, hwnd, h) == name]
            if twin >= len(matches):
                raise NativeListError(
                    f"트리에서 「{name}」 항목을 찾지 못했습니다. 트리가 바뀌었을 수 있으니 snapshot 을 다시 찍으세요."
                )
            parent = matches[twin]
        return parent


def tree_state(hwnd: int, hitem: int) -> int:
    return _send(hwnd, TVM_GETITEMSTATE, hitem, TVIS_SELECTED | TVIS_EXPANDED)


def tree_select(hwnd: int, hitem: int) -> None:
    # TVM_SELECTITEM sends TVN_SELCHANGING/TVN_SELCHANGED itself; the parent
    # may veto, which is then reported as not landing.
    _ensure_enabled(hwnd)
    _send(hwnd, TVM_ENSUREVISIBLE, 0, hitem)
    _send(hwnd, TVM_SELECTITEM, TVGN_CARET, hitem)
    if _send(hwnd, TVM_GETNEXTITEM, TVGN_CARET, 0) != hitem:
        raise NativeListError("트리에서 항목을 골랐지만 선택이 반영되지 않았습니다(프로그램이 막았을 수 있습니다).")


def _tree_may_have_children(remote: _Remote, hwnd: int, hitem: int) -> bool:
    item = remote.TVITEMW(mask=TVIF_HANDLE | TVIF_CHILDREN, hItem=hitem)
    _send(hwnd, TVM_GETITEMW, 0, remote.write(0, item))
    # 1, or I_CHILDRENCALLBACK: the program will say when asked.
    return remote.read(0, remote.TVITEMW()).cChildren != 0


def tree_expand(hwnd: int, hitem: int, *, collapse: bool = False) -> str:
    """Expand or collapse one item; returns what happened, in words.

    A branch that already holds its items is opened with TVM_EXPAND, which
    touches nothing else. It does not tell the parent (documented), and that
    matters for a branch that is filled only as it opens — on
    TVN_ITEMEXPANDING, as folder pickers do — which would open empty. That
    notification cannot be sent from here: Windows refuses WM_NOTIFY between
    processes (measured). Two ways make the tree send it itself, and they were
    measured on a freshly started target, which may take the foreground
    (lesson 31): a click posted on the [+] made the tree take the focus and
    the window came forward; the + key posted to it did not, visible or
    minimised. The key acts on the selected item, so such a branch is selected
    first, and the result says so.
    """

    _ensure_enabled(hwnd)
    want = not collapse
    if bool(tree_state(hwnd, hitem) & TVIS_EXPANDED) == want:
        return "이미 " + ("펼쳐져" if want else "접혀") + " 있음"
    if collapse or _send(hwnd, TVM_GETNEXTITEM, TVGN_CHILD, hitem):
        _send(hwnd, TVM_EXPAND, TVE_COLLAPSE if collapse else TVE_EXPAND, hitem)
        done = "접음" if collapse else "펼침"
    else:
        with _Remote(hwnd) as remote:
            expandable = _tree_may_have_children(remote, hwnd, hitem)
        if not expandable:
            raise NativeListError("이 트리 항목에는 하위 항목이 없어 펼칠 수 없습니다.")
        tree_select(hwnd, hitem)
        user32 = _user32()
        user32.PostMessageW(hwnd, WM_KEYDOWN, VK_ADD, 0x004E0001)
        user32.PostMessageW(hwnd, WM_KEYUP, VK_ADD, 0xC04E0001)
        done = "펼침 — 펼칠 때 내용을 채우는 가지라 이 항목을 고른 뒤 + 키로 펼쳤습니다"
    deadline = time.monotonic() + 2.0
    while bool(tree_state(hwnd, hitem) & TVIS_EXPANDED) != want:
        if time.monotonic() > deadline:
            raise NativeListError(
                "트리 항목이 " + ("펼쳐지지" if want else "접히지") + " 않았습니다. "
                "프로그램이 막았거나 하위 항목이 없을 수 있습니다."
            )
        time.sleep(0.05)
    return done


# --------------------------------------------------------------------------
# List view
# --------------------------------------------------------------------------


def listview_count(hwnd: int) -> int:
    return _send(hwnd, LVM_GETITEMCOUNT)


def listview_texts(hwnd: int, *, limit: int = 20_000) -> list[str]:
    """The first column of every row."""

    with _Remote(hwnd) as remote:
        out: list[str] = []
        for index in range(min(listview_count(hwnd), limit)):
            item = remote.LVITEMW(iSubItem=0, pszText=remote.base + TEXT_AT, cchTextMax=TEXT_CHARS)
            _send(hwnd, LVM_GETITEMTEXTW, index, remote.write(0, item))
            out.append(remote.read_text(TEXT_AT, TEXT_CHARS))
        return out


def listview_selected(hwnd: int, index: int) -> bool:
    return bool(_send(hwnd, LVM_GETITEMSTATE, index, LVIS_SELECTED) & LVIS_SELECTED)


def listview_select(hwnd: int, index: int, *, add: bool = False) -> None:
    # LVM_SETITEMSTATE makes the control send LVN_ITEMCHANGED itself, as a
    # click does.
    _ensure_enabled(hwnd)
    with _Remote(hwnd) as remote:
        if not add or _style(hwnd) & LVS_SINGLESEL:
            clear = remote.LVITEMW(stateMask=LVIS_SELECTED, state=0)
            _send(hwnd, LVM_SETITEMSTATE, -1, remote.write(0, clear))
        mark = LVIS_SELECTED | LVIS_FOCUSED
        chosen = remote.LVITEMW(stateMask=mark, state=mark)
        _send(hwnd, LVM_SETITEMSTATE, index, remote.write(0, chosen))
    _send(hwnd, LVM_SETSELECTIONMARK, 0, index)
    _send(hwnd, LVM_ENSUREVISIBLE, index, 0)
    if not listview_selected(hwnd, index):
        raise NativeListError("목록에서 항목을 골랐지만 선택이 반영되지 않았습니다.")


def find_index(texts: list[str], name: str, hint: Optional[int]) -> int:
    """Where ``name`` is in ``texts``: at ``hint`` if it is there, else the only place it is."""

    if hint is not None and 0 <= hint < len(texts) and texts[hint] == name:
        return hint
    places = [i for i, text in enumerate(texts) if text == name]
    if len(places) == 1:
        return places[0]
    if not places:
        raise NativeListError(
            f"목록에 「{name}」 항목이 없습니다. 목록이 바뀌었을 수 있으니 snapshot 을 다시 찍으세요."
        )
    raise NativeListError(
        f"목록에 「{name}」 항목이 {len(places)}개 있어 어느 것인지 확실하지 않아 고르지 않았습니다. "
        "snapshot 을 다시 찍어 새 ref 로 고르세요."
    )
