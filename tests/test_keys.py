import pytest

from claude_hands.win32.input import KeyParseError, parse_keys


def test_single_key():
    (stroke,) = parse_keys("enter")
    assert stroke.vk == 0x0D
    assert stroke.modifiers == ()


def test_modifier_chord():
    (stroke,) = parse_keys("ctrl+s")
    assert stroke.modifiers == ("ctrl",)
    assert stroke.vk == ord("S")
    assert stroke.describe() == "ctrl+s"


def test_multiple_modifiers_preserve_order_and_dedupe():
    (stroke,) = parse_keys("ctrl+shift+ctrl+n")
    assert stroke.modifiers == ("ctrl", "shift")
    assert stroke.vk == ord("N")


def test_alias_normalisation():
    (stroke,) = parse_keys("control+alt+delete")
    assert stroke.modifiers == ("ctrl", "alt")
    assert stroke.vk == 0x2E


def test_sequence_of_chords():
    strokes = parse_keys("ctrl+shift+n enter")
    assert [s.describe() for s in strokes] == ["ctrl+shift+n", "enter"]


def test_comma_separated_sequence():
    strokes = parse_keys("tab, tab, enter")
    assert len(strokes) == 3
    assert strokes[-1].vk == 0x0D


def test_function_keys():
    assert parse_keys("f5")[0].vk == 0x74
    assert parse_keys("alt+f4")[0].vk == 0x73


def test_literal_plus_is_not_a_separator():
    (stroke,) = parse_keys("ctrl++")
    assert stroke.modifiers == ("ctrl",)
    assert stroke.key == "+"


def test_modifier_alone_is_treated_as_the_key():
    (stroke,) = parse_keys("shift")
    assert stroke.modifiers == ()
    assert stroke.vk == 0x10


def test_unknown_key_name_rejected():
    with pytest.raises(KeyParseError):
        parse_keys("ctrl+frobnicate")


def test_empty_spec_rejected():
    with pytest.raises(KeyParseError):
        parse_keys("   ")
