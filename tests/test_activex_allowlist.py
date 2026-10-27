"""What may be called on a page's ActiveX controls, and with what.

Nothing here touches COM: the allowlist and every check on the arguments run
before the page sees a call, so a refusal here is a refusal on Windows too.
"""

from __future__ import annotations

import pytest

from claude_hands.mshtml.activex import (
    CONTROLS,
    HSATTACH_CONTROL,
    HWP_CONTROL,
    READ,
    ControlCallError,
    call_script,
    check_call,
    control_for,
    decode,
    js_literal,
    local_path,
    lookup,
)
from claude_hands.win32.defs import ClaudeHandsError


def test_controls_are_known_by_class_id_however_the_page_spells_it():
    assert control_for("clsid:BD9C32E4-3155-4691-8972-097D53B10052") is HWP_CONTROL
    assert control_for("{205D1F4F-DB85-4393-AC6D-D3FF2434E37E}") is HSATTACH_CONTROL
    assert control_for("clsid:00000000-0000-0000-0000-000000000000") is None


# Each of these does something the user did not hand over: replaces the draft
# with another file, prints, runs a script, touches the security module, or
# posts the attachments to the server — which is what 결재올림 does.
NEVER = {
    "Open", "OpenDocument", "PrintDocument", "Clear", "RunScriptMacro", "GetScriptSource",
    "RegisterModule", "SetPrivateInfoPassword", "LockCommand", "ReplaceAction",
    "SetAutoSave", "VersionSave", "VersionDelete", "SetMetaTag", "RemoveMetaTag",
    "CreateAction", "AboutBox", "DoPost", "Attach", "RemoveAll",
}


def test_nothing_that_sends_prints_or_replaces_the_draft_is_callable():
    for control in CONTROLS.values():
        assert not NEVER & set(control.methods), control.label


@pytest.mark.parametrize("name", ["Open", "PrintDocument", "RunScriptMacro"])
def test_an_unlisted_method_is_refused_with_what_is_allowed(name):
    with pytest.raises(ClaudeHandsError) as caught:
        lookup(HWP_CONTROL, name)
    assert "GetTextFile" in str(caught.value) and "SaveAs" in str(caught.value)


def test_a_wrong_case_points_at_the_right_name():
    with pytest.raises(ClaudeHandsError, match="'GetTextFile'"):
        lookup(HWP_CONTROL, "gettextfile")


def test_the_user_asked_for_saving_so_it_is_there():
    for name in ("Save", "SaveAs", "CreatePageImage"):
        assert lookup(HWP_CONTROL, name).kind == "save"
    assert lookup(HWP_CONTROL, "GetTextFile").kind == READ


# --------------------------------------------------------------------------
# Arguments
# --------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["MoveDocEnd", "MoveSelParaEnd", "SelectAll", "BreakPara", "Undo"])
def test_editor_actions_that_stay_in_the_text_may_run(action):
    check_call(lookup(HWP_CONTROL, "Run"), "Run", [action])


@pytest.mark.parametrize("action", ["FileSaveAs", "FilePrint", "Print", "FileOpen", "ScriptMacro"])
def test_editor_actions_that_open_dialogs_or_files_may_not(action):
    with pytest.raises(ClaudeHandsError, match="허용하지 않습니다"):
        check_call(lookup(HWP_CONTROL, "Run"), "Run", [action])


def test_a_property_takes_no_arguments():
    with pytest.raises(ClaudeHandsError, match="속성"):
        check_call(lookup(HWP_CONTROL, "PageCount"), "PageCount", [1])


def test_too_many_arguments_are_refused_before_the_page_sees_them():
    with pytest.raises(ClaudeHandsError, match="2개까지"):
        check_call(lookup(HWP_CONTROL, "GetTextFile"), "GetTextFile", ["TEXT", "", "extra"])


def test_only_plain_values_cross_into_the_page():
    with pytest.raises(ClaudeHandsError, match="문자열"):
        check_call(lookup(HWP_CONTROL, "GetFieldText"), "GetFieldText", [["제목"]])
    with pytest.raises(ClaudeHandsError, match="NaN"):
        check_call(lookup(HWP_CONTROL, "GetPageText"), "GetPageText", [float("nan")])


@pytest.mark.parametrize(
    "path",
    [r"\\server\share\a.hwp", "//server/share/a.hwp", "http://example.com/a.hwp",
     "file:///C:/a.hwp", r"a.hwp", r"..\a.hwp", ""],
)
def test_a_path_must_be_a_file_on_this_pc(path):
    # A network path or URL would carry the document off the machine.
    with pytest.raises(ClaudeHandsError):
        local_path(path)


def test_saving_over_an_existing_file_needs_to_be_asked_for(tmp_path):
    existing = tmp_path / "기안.hwp"
    existing.write_bytes(b"old")
    spec = lookup(HWP_CONTROL, "SaveAs")

    with pytest.raises(ClaudeHandsError, match="overwrite=True"):
        check_call(spec, "SaveAs", [str(existing), "HWP", ""])
    assert check_call(spec, "SaveAs", [str(existing), "HWP", ""], overwrite=True)[0] == str(existing)


def test_saving_into_a_folder_that_is_not_there_is_refused(tmp_path):
    with pytest.raises(ClaudeHandsError, match="폴더가 없습니다"):
        check_call(lookup(HWP_CONTROL, "SaveAs"), "SaveAs", [str(tmp_path / "없음" / "a.hwp"), "HWP", ""])


def test_saving_needs_a_path():
    with pytest.raises(ClaudeHandsError, match="경로가 필요합니다"):
        check_call(lookup(HWP_CONTROL, "SaveAs"), "SaveAs", [])


def test_a_file_to_insert_or_attach_must_exist(tmp_path):
    with pytest.raises(ClaudeHandsError, match="파일이 없습니다"):
        check_call(lookup(HSATTACH_CONTROL, "AddFile"), "AddFile", [str(tmp_path / "없음.pdf")])
    present = tmp_path / "붙임.pdf"
    present.write_bytes(b"%PDF")
    assert check_call(lookup(HSATTACH_CONTROL, "AddFile"), "AddFile", [str(present)]) == [str(present)]


# --------------------------------------------------------------------------
# The page-side script
# --------------------------------------------------------------------------


def test_text_cannot_end_the_string_it_is_carried_in():
    assert js_literal('a"b\\c\n가') == '"a\\"b\\\\c\\n\\uac00"'
    script = call_script("document.all[40]", "PutFieldText", lookup(HWP_CONTROL, "PutFieldText"),
                         ['제목"); alert(1); ("', "본문"], "claudeHandsCall1")
    assert script.isascii()
    assert 'c.PutFieldText("\\uc81c\\ubaa9\\"); alert(1); (\\"", "\\ubcf8\\ubb38")' in script


def test_a_property_is_read_not_called():
    script = call_script("document.all[40]", "PageCount", lookup(HWP_CONTROL, "PageCount"), [], "s")
    assert "r = c.PageCount;" in script


@pytest.mark.parametrize(
    "raw, value",
    [("string\x1f가나", "가나"), ("boolean\x1ftrue", True), ("boolean\x1ffalse", False),
     ("number\x1f3", 3), ("number\x1f1.5", 1.5), ("undefined\x1f", None), ("object\x1f", None)],
)
def test_answers_come_back_as_their_own_types(raw, value):
    assert decode(raw) == value


def test_an_object_answer_is_named_not_mistaken_for_text():
    assert "돌려줄 수 없는" in decode("object\x1f[object]")


def test_the_control_refusing_is_an_error():
    with pytest.raises(ControlCallError, match="형식이 올바르지"):
        decode("error\x1f형식이 올바르지 않습니다")
