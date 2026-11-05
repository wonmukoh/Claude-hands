"""Classic Win32 lists to measure against: ListBox, ComboBox, SysTreeView32, SysListView32.

    py examples/list_target.py [log file]     # prints its HWND, then runs

verify_lists.py starts one of these per case. The window opens far off screen
without being activated, so a run never covers what the user is working on.
Every notification the controls send their parent is appended to the log file,
which is how a run tells "the control did it" from "the call returned".

The tree's last branch, 늦게, has no children until it is first expanded — the
way Explorer-style trees fill themselves — so TVM_EXPAND alone cannot open it.
"""

import ctypes
import sys
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
comctl32 = ctypes.WinDLL("comctl32")

TITLE = "claude-hands 목록 시험 대상"
LOG = sys.argv[1] if len(sys.argv) > 1 else "list_target.log"

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = LRESULT
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]


class INITCOMMONCONTROLSEX(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("dwICC", wintypes.DWORD)]


class NMHDR(ctypes.Structure):
    _fields_ = [("hwndFrom", wintypes.HWND), ("idFrom", ctypes.c_size_t), ("code", ctypes.c_int)]


class TVITEMW(ctypes.Structure):
    _fields_ = [
        ("mask", wintypes.UINT), ("hItem", ctypes.c_void_p), ("state", wintypes.UINT),
        ("stateMask", wintypes.UINT), ("pszText", wintypes.LPWSTR), ("cchTextMax", ctypes.c_int),
        ("iImage", ctypes.c_int), ("iSelectedImage", ctypes.c_int), ("cChildren", ctypes.c_int),
        ("lParam", wintypes.LPARAM),
    ]


class NMTREEVIEWW(ctypes.Structure):
    _fields_ = [("hdr", NMHDR), ("action", wintypes.UINT), ("itemOld", TVITEMW), ("itemNew", TVITEMW),
                ("ptDrag", wintypes.POINT)]


class TVINSERTSTRUCTW(ctypes.Structure):
    _fields_ = [("hParent", ctypes.c_void_p), ("hInsertAfter", ctypes.c_void_p), ("item", TVITEMW)]


class LVCOLUMNW(ctypes.Structure):
    _fields_ = [
        ("mask", wintypes.UINT), ("fmt", ctypes.c_int), ("cx", ctypes.c_int), ("pszText", wintypes.LPWSTR),
        ("cchTextMax", ctypes.c_int), ("iSubItem", ctypes.c_int), ("iImage", ctypes.c_int),
        ("iOrder", ctypes.c_int), ("cxMin", ctypes.c_int), ("cxDefault", ctypes.c_int), ("cxIdeal", ctypes.c_int),
    ]


class LVITEMW(ctypes.Structure):
    _fields_ = [
        ("mask", wintypes.UINT), ("iItem", ctypes.c_int), ("iSubItem", ctypes.c_int), ("state", wintypes.UINT),
        ("stateMask", wintypes.UINT), ("pszText", wintypes.LPWSTR), ("cchTextMax", ctypes.c_int),
        ("iImage", ctypes.c_int), ("lParam", wintypes.LPARAM), ("iIndent", ctypes.c_int),
        ("iGroupId", ctypes.c_int), ("cColumns", wintypes.UINT), ("puColumns", ctypes.c_void_p),
        ("piColFmt", ctypes.c_void_p), ("iGroup", ctypes.c_int),
    ]


WS_CHILD, WS_VISIBLE, WS_BORDER, WS_VSCROLL, WS_TABSTOP = 0x40000000, 0x10000000, 0x00800000, 0x00200000, 0x00010000
WS_OVERLAPPEDWINDOW = 0x00CF0000
LBS_NOTIFY = 0x0001
CBS_DROPDOWNLIST = 0x0003
TVS_HASBUTTONS, TVS_HASLINES, TVS_LINESATROOT, TVS_SHOWSELALWAYS = 0x1, 0x2, 0x4, 0x20
LVS_REPORT, LVS_SHOWSELALWAYS, LVS_SINGLESEL = 0x1, 0x8, 0x4
WM_COMMAND, WM_NOTIFY, WM_DESTROY = 0x0111, 0x004E, 0x0002
LB_ADDSTRING, CB_ADDSTRING = 0x0180, 0x0143
TVM_INSERTITEMW, LVM_INSERTCOLUMNW, LVM_INSERTITEMW = 0x1132, 0x1061, 0x104D
TVM_GETNEXTITEM, TVGN_CHILD = 0x110A, 4
TVIF_TEXT, TVIF_CHILDREN, LVIF_TEXT, LVCF_TEXT, LVCF_WIDTH = 0x1, 0x40, 0x1, 0x4, 0x2
TVI_LAST = ctypes.c_void_p(-0xFFFE & 0xFFFFFFFFFFFFFFFF)
TVN_SELCHANGEDW, TVN_ITEMEXPANDEDW, LVN_ITEMCHANGED, TVN_ITEMEXPANDINGW = -451, -455, -101, -454
LAZY = [0]  # the branch that is filled on first expansion
SW_SHOWNOACTIVATE = 4

ID_LIST, ID_COMBO, ID_TREE, ID_LV = 101, 102, 103, 104
_keep = []


def log(text):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(text + "\n")


def on_message(hwnd, msg, wparam, lparam):
    if msg == WM_COMMAND:
        code, cid = (wparam >> 16) & 0xFFFF, wparam & 0xFFFF
        if cid in (ID_LIST, ID_COMBO):
            log(f"WM_COMMAND id={cid} code={code}")
    elif msg == WM_NOTIFY:
        hdr = NMHDR.from_address(lparam)
        if hdr.code in (TVN_SELCHANGEDW, TVN_ITEMEXPANDEDW, LVN_ITEMCHANGED, TVN_ITEMEXPANDINGW):
            log(f"WM_NOTIFY id={hdr.idFrom} code={hdr.code}")
        if hdr.code == TVN_ITEMEXPANDINGW:
            note = NMTREEVIEWW.from_address(lparam)
            h = note.itemNew.hItem
            if h == LAZY[0] and not user32.SendMessageW(hdr.hwndFrom, TVM_GETNEXTITEM, TVGN_CHILD, h):
                ins = TVINSERTSTRUCTW(ctypes.c_void_p(h), TVI_LAST, TVITEMW(mask=TVIF_TEXT, pszText="늦은1"))
                user32.SendMessageW(hdr.hwndFrom, TVM_INSERTITEMW, 0, ctypes.addressof(ins))
    elif msg == WM_DESTROY:
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def child(parent, cls, style, x, y, w, h, cid, hinst):
    return user32.CreateWindowExW(0x200, cls, "", WS_CHILD | WS_VISIBLE | WS_TABSTOP | style,
                                  x, y, w, h, parent, wintypes.HMENU(cid), hinst, None)


def main():
    icc = INITCOMMONCONTROLSEX(ctypes.sizeof(INITCOMMONCONTROLSEX), 0x1 | 0x2)  # LISTVIEW | TREEVIEW
    comctl32.InitCommonControlsEx(ctypes.byref(icc))
    hinst = kernel32.GetModuleHandleW(None)
    proc = WNDPROC(on_message)
    _keep.append(proc)
    wc = WNDCLASSW(lpfnWndProc=proc, hInstance=hinst, lpszClassName="ClaudeHandsListTarget", hbrBackground=16)
    wc.hCursor = user32.LoadCursorW(None, ctypes.c_wchar_p(32512))
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(0, "ClaudeHandsListTarget", TITLE, WS_OVERLAPPEDWINDOW,
                                  9000, 9000, 640, 300, None, None, hinst, None)

    lb = child(hwnd, "LISTBOX", WS_BORDER | WS_VSCROLL | LBS_NOTIFY, 10, 10, 140, 120, ID_LIST, hinst)
    for s in ("사과", "배", "포도"):
        buf = ctypes.create_unicode_buffer(s); _keep.append(buf); user32.SendMessageW(lb, LB_ADDSTRING, 0, ctypes.addressof(buf))
    cb = child(hwnd, "COMBOBOX", CBS_DROPDOWNLIST | WS_VSCROLL, 160, 10, 140, 200, ID_COMBO, hinst)
    for s in ("하나", "둘", "셋"):
        buf = ctypes.create_unicode_buffer(s); _keep.append(buf); user32.SendMessageW(cb, CB_ADDSTRING, 0, ctypes.addressof(buf))

    tv = child(hwnd, "SysTreeView32", WS_BORDER | TVS_HASBUTTONS | TVS_HASLINES | TVS_LINESATROOT | TVS_SHOWSELALWAYS,
               310, 10, 150, 240, ID_TREE, hinst)
    parents = []
    for p in ("과일", "채소"):
        ins = TVINSERTSTRUCTW(None, TVI_LAST, TVITEMW(mask=TVIF_TEXT, pszText=p))
        parents.append(user32.SendMessageW(tv, TVM_INSERTITEMW, 0, ctypes.addressof(ins)))
    for parent, kids in zip(parents, (("귤", "감"), ("무", "파"))):
        for k in kids:
            ins = TVINSERTSTRUCTW(ctypes.c_void_p(parent), TVI_LAST, TVITEMW(mask=TVIF_TEXT, pszText=k))
            user32.SendMessageW(tv, TVM_INSERTITEMW, 0, ctypes.addressof(ins))

    ins = TVINSERTSTRUCTW(None, TVI_LAST, TVITEMW(mask=TVIF_TEXT | TVIF_CHILDREN, pszText="늦게", cChildren=1))
    LAZY[0] = user32.SendMessageW(tv, TVM_INSERTITEMW, 0, ctypes.addressof(ins))

    lv = child(hwnd, "SysListView32", WS_BORDER | LVS_REPORT | LVS_SHOWSELALWAYS | LVS_SINGLESEL,
               470, 10, 150, 240, ID_LV, hinst)
    col = LVCOLUMNW(mask=LVCF_TEXT | LVCF_WIDTH, cx=120, pszText="이름")
    user32.SendMessageW(lv, LVM_INSERTCOLUMNW, 0, ctypes.addressof(col))
    for i, s in enumerate(("가람", "나래", "다솜")):
        it = LVITEMW(mask=LVIF_TEXT, iItem=i, pszText=s)
        user32.SendMessageW(lv, LVM_INSERTITEMW, 0, ctypes.addressof(it))

    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    user32.UpdateWindow(hwnd)
    print(hwnd, flush=True)
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    main()
