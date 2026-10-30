"""Which methods of a page's ActiveX controls may be called, and how.

K-에듀파인's 기안 page embeds two controls that do the real work: the 한글 web
control holding the document body, and HSAttach holding the attachments. Their
methods reach much further than any form field — a 한글 control can run
arbitrary editor actions and macros, HSAttach can post files to the server —
so nothing is callable unless it is listed here, per control, by class id.

Each method is filed under what it does to the user's world:

* ``read``  — answers a question; allowed even on a read-only server.
* ``write`` — changes the draft on screen, which the user still has to send.
* ``save``  — writes a file on this PC. The user asked for this explicitly:
  "발송은 내가 하더라도 임시저장은 하게 해줘".

What is left out is left out on purpose: opening another file over the draft
(``Open``), printing, macros and scripts, security modules and passwords, and
anything that posts to a server (``DoPost``). A method missing here should be
added deliberately, with the reason, not reached through a generic escape.

Kept free of COM so the allowlist and argument checks are testable anywhere.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..win32.defs import ClaudeHandsError

READ, WRITE, SAVE = "read", "write", "save"
KIND_LABELS = {READ: "읽기", WRITE: "쓰기", SAVE: "파일 저장"}


@dataclass(frozen=True)
class Method:
    kind: str
    params: tuple[str, ...] = ()
    property: bool = False
    """Read as ``control.Name`` rather than called as ``control.Name()``."""
    path_in: Optional[int] = None
    """Index of an argument naming a local file the control will read."""
    path_out: Optional[int] = None
    """Index of an argument naming a local file the control will write."""
    check: Optional[Callable[[list], None]] = None

    def signature(self, name: str) -> str:
        return name if self.property else f"{name}({', '.join(self.params)})"


@dataclass(frozen=True)
class Control:
    label: str
    methods: dict[str, Method] = field(default_factory=dict)

    def describe(self) -> str:
        lines = []
        for kind in (READ, WRITE, SAVE):
            names = [m.signature(n) for n, m in self.methods.items() if m.kind == kind]
            if names:
                lines.append(f"  {KIND_LABELS[kind]}: " + ", ".join(names))
        return "\n".join(lines)


# --------------------------------------------------------------------------
# 한글 web control (HwpODTCtrl)
# --------------------------------------------------------------------------

# Editor actions that only move the caret, select, or edit text in place. Any
# other action id can open a dialog, print, or save somewhere unasked.
HWP_RUN_ACTIONS = {
    "Select", "SelectAll", "Cancel", "BreakPara", "BreakLine", "Delete",
    "DeleteBack", "DeleteWord", "DeleteLine", "Undo", "Redo",
}


def _check_run(args: list) -> None:
    action = str(args[0]) if args else ""
    if action.startswith("Move") or action in HWP_RUN_ACTIONS:
        return
    raise ClaudeHandsError(
        f"Run({action!r}) 은 허용하지 않습니다. 커서 이동(Move…)과 "
        + ", ".join(sorted(HWP_RUN_ACTIONS))
        + " 만 됩니다. 다른 동작은 대화상자를 띄우거나 인쇄·저장을 할 수 있습니다."
    )


HWP_CONTROL = Control(
    label="한글 컨트롤",
    methods={
        # read
        "GetTextFile": Method(READ, ("format", "option")),
        "GetPageText": Method(READ, ("pgno", "option")),
        "GetFieldList": Method(READ, ("number", "option")),
        "GetFieldText": Method(READ, ("fieldlist",)),
        "FieldExist": Method(READ, ("field",)),
        "GetCurFieldName": Method(READ, ("option",)),
        "GetHeadingString": Method(READ),
        "GetMetaTag": Method(READ, ("name",)),
        "GetMetaTagAll": Method(READ),
        "GetFontList": Method(READ, ("langid",)),
        "GetTableCellAddr": Method(READ, ("type",)),
        "GetViewStatus": Method(READ, ("nType",)),
        "IsSpellCheckCompleted": Method(READ),
        "PageCount": Method(READ, property=True),
        "IsModified": Method(READ, property=True),
        "IsEmpty": Method(READ, property=True),
        "Path": Method(READ, property=True),
        "Version": Method(READ, property=True),
        "EditMode": Method(READ, property=True),
        "ReadOnlyMode": Method(READ, property=True),
        "CurFieldState": Method(READ, property=True),
        # write
        "PutFieldText": Method(WRITE, ("fieldlist", "textlist")),
        "MoveToField": Method(WRITE, ("field", "text", "start", "select")),
        "MoveToFieldEx": Method(WRITE, ("field", "text", "start", "select")),
        "MovePos": Method(WRITE, ("moveID", "para", "pos")),
        "SetPos": Method(WRITE, ("list", "para", "pos")),
        "SelectText": Method(WRITE, ("spara", "spos", "epara", "epos")),
        "SetTextFile": Method(WRITE, ("data", "format", "option")),
        "Insert": Method(WRITE, ("Path", "format", "arg"), path_in=0),
        "InsertDocument": Method(WRITE, ("szFileName",), path_in=0),
        "InsertPicture": Method(
            WRITE,
            ("Path", "embedded", "sizeOption", "reverse", "watermark", "effect", "width", "height"),
            path_in=0,
        ),
        "CreateField": Method(WRITE, ("direction", "memo", "name")),
        "RenameField": Method(WRITE, ("oldname", "newname")),
        "SetCurFieldName": Method(WRITE, ("fieldname", "option", "direction", "memo")),
        "SetFieldViewOption": Method(WRITE, ("option",)),
        "Run": Method(WRITE, ("actid",), check=_check_run),
        # save
        "Save": Method(SAVE, ("save_if_dirty",)),
        "SaveAs": Method(SAVE, ("Path", "format", "arg"), path_out=0),
        "SaveDocument": Method(SAVE, ("szFileName", "szFileType"), path_out=0),
        "CreatePageImage": Method(SAVE, ("Path", "pgno", "resolution", "depth", "format"), path_out=0),
        "CreatePageImageEx": Method(
            SAVE, ("Path", "pgno", "resolution", "depth", "format", "options"), path_out=0
        ),
    },
)


# --------------------------------------------------------------------------
# HSAttach (핸디소프트 첨부 부품)
# --------------------------------------------------------------------------

# Adding to and taking from the list is editing the draft. Posting the list
# (DoPost) is the upload the user's own 결재올림 does, and Attach() opens a
# file picker that would block the page until a person closed it.
HSATTACH_CONTROL = Control(
    label="첨부 부품",
    methods={
        "getFilesCount": Method(READ),
        "getFileName": Method(READ, ("idx",)),
        "getFilePath": Method(READ, ("idx",)),
        "getFileSize": Method(READ, ("idx",)),
        "getTotalSize": Method(READ),
        "AttachCount": Method(READ, property=True),
        "MaxAttachSize": Method(READ, property=True),
        "MaxAttachCount": Method(READ, property=True),
        "DisableFileExt": Method(READ, property=True),
        "AddFile": Method(WRITE, ("sUrl",), path_in=0),
        "RemoveAt": Method(WRITE, ("idx",)),
    },
)


CONTROLS: dict[str, Control] = {
    "bd9c32e4-3155-4691-8972-097d53b10052": HWP_CONTROL,
    "205d1f4f-db85-4393-ac6d-d3ff2434e37e": HSATTACH_CONTROL,
}


def normalise_clsid(classid: str) -> str:
    return classid.lower().replace("clsid:", "").strip().strip("{}")


def control_for(classid: str) -> Optional[Control]:
    return CONTROLS.get(normalise_clsid(classid))


# --------------------------------------------------------------------------
# Checking a call before it reaches the page
# --------------------------------------------------------------------------


def lookup(control: Control, method: str) -> Method:
    spec = control.methods.get(method)
    if spec is not None:
        return spec
    # Method names are case-sensitive to the control. A near miss is far more
    # likely a typo than a request for something else.
    for name, candidate in control.methods.items():
        if name.lower() == method.lower():
            raise ClaudeHandsError(f"{method!r} 가 아니라 {name!r} 입니다 (대소문자 구분).")
    raise ClaudeHandsError(
        f"{control.label}의 {method!r} 는 부를 수 없습니다. 허용된 것:\n{control.describe()}\n"
        "목록에 없는 것은 일부러 뺀 것입니다(다른 파일 열기·인쇄·매크로·서버 전송). "
        "필요하면 claude-hands 담당에게 이유와 함께 요청하세요."
    )


def check_call(spec: Method, name: str, args: list, *, overwrite: bool = False) -> list:
    """Validate ``args`` for ``spec`` and return them with paths normalised."""

    args = list(args)
    if spec.property and args:
        raise ClaudeHandsError(f"{name} 은(는) 속성이라 인자를 받지 않습니다.")
    if len(args) > len(spec.params):
        raise ClaudeHandsError(
            f"인자가 너무 많습니다: {spec.signature(name)} 는 {len(spec.params)}개까지 받습니다."
        )
    for value in args:
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise ClaudeHandsError(
                f"인자는 문자열·숫자·참거짓·null 만 됩니다: {type(value).__name__}"
            )
        if isinstance(value, float) and not math.isfinite(value):
            raise ClaudeHandsError("인자에 무한대나 NaN 은 쓸 수 없습니다.")
    if spec.path_in is not None and spec.path_in < len(args):
        args[spec.path_in] = local_path(args[spec.path_in], must_exist=True)
    if spec.path_out is not None:
        if spec.path_out >= len(args):
            raise ClaudeHandsError(f"{spec.signature(name)} 에는 저장할 경로가 필요합니다.")
        args[spec.path_out] = local_path(args[spec.path_out], overwrite=overwrite)
    if spec.check is not None:
        spec.check(args)
    return args


_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def local_path(value: Any, *, must_exist: bool = False, overwrite: bool = True) -> str:
    """A file on this PC, given as an absolute path.

    The 한글 control and HSAttach both accept URLs and network paths where a
    file name goes. Neither is what anyone means by "save it" or "attach this",
    and a network path would carry the document off the machine.
    """

    if not isinstance(value, str) or not value.strip():
        raise ClaudeHandsError("파일 경로는 비어 있지 않은 문자열이어야 합니다.")
    text = value.strip()
    if not _DRIVE_PATH.match(text):
        raise ClaudeHandsError(
            f"{text!r} 는 이 PC 의 절대 경로가 아닙니다. C:\\... 처럼 드라이브 문자로 시작하는 "
            "경로만 됩니다(네트워크 경로·URL 은 받지 않습니다)."
        )
    path = os.path.normpath(text)
    if must_exist and not os.path.isfile(path):
        raise ClaudeHandsError(f"파일이 없습니다: {path}")
    if not must_exist:
        folder = os.path.dirname(path)
        if not os.path.isdir(folder):
            raise ClaudeHandsError(f"저장할 폴더가 없습니다: {folder}")
        if os.path.exists(path) and not overwrite:
            raise ClaudeHandsError(
                f"이미 있는 파일입니다: {path}\n"
                "덮어쓰면 그 파일의 원래 내용은 사라집니다. 다른 이름을 쓰거나, 덮어써도 되는지 "
                "사용자에게 확인한 뒤 overwrite=True 로 부르세요."
            )
    return path


# --------------------------------------------------------------------------
# The call as the page runs it
# --------------------------------------------------------------------------


def js_literal(value: Any) -> str:
    """``value`` as a JScript literal that IE5 quirks mode parses.

    JSON with ``ensure_ascii`` is exactly that for these types: every non-ASCII
    character, quote and control character comes out as an escape, so nothing
    a caller passes can end the string early.
    """

    return json.dumps(value, ensure_ascii=True)


MISSING_CONTROL = "페이지가 바뀌어 컨트롤을 다시 찾지 못했습니다. snapshot 을 다시 찍으세요."


def call_script(element_ref: str, name: str, spec: Method, args: list, slot: str) -> str:
    """The page-side statement that makes the call and leaves its result in ``slot``."""

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):  # pragma: no cover - allowlist names
        raise ClaudeHandsError(f"메서드 이름이 올바르지 않습니다: {name!r}")
    target = f"c.{name}" if spec.property else f"c.{name}({', '.join(js_literal(a) for a in args)})"
    return (
        "(function () { var c, r, t, s = ''; "
        f"try {{ c = {element_ref}; "
        f"if (!c) {{ throw new Error({js_literal(MISSING_CONTROL)}); }} "
        f"r = {target}; t = typeof r; "
        "try { s = (r === null || r === undefined) ? '' : String(r); } catch (e2) { t = 'unknown'; } "
        f"document.{slot} = t + '\\x1f' + s; "
        f"}} catch (e) {{ document.{slot} = 'error\\x1f' + (e.message || e.description || e); }} "
        "})();"
    )


class ControlCallError(ClaudeHandsError):
    """The control itself refused the call."""


def decode(raw: str) -> Any:
    kind, _, text = raw.partition("\x1f")
    if kind == "error":
        raise ControlCallError(f"컨트롤이 거절했습니다: {text}")
    if kind == "string":
        return text
    if kind == "boolean":
        return text == "true"
    if kind == "number":
        number = float(text)
        return int(number) if number.is_integer() else number
    if kind in {"undefined", "object"} and text in {"", "null"}:
        return None
    # A COM object or array came back. The call happened; its answer simply
    # cannot travel as text.
    return f"({kind} — 글자로 돌려줄 수 없는 값)"
