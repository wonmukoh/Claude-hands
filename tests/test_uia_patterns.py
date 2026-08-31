"""How an element's available patterns are read.

`to_node_info` asked for them as attributes — `CurrentIsInvokePatternAvailable`
and friends. IUIAutomationElement has no such members, so every lookup raised,
was swallowed into the default, and every element in every tree came back with
no patterns at all. Nothing crashed; the tree just quietly lost the one signal
that says what an element can do, and `"value" in node.patterns` could never be
true. Measured against a live PowerPoint: the search box answered False through
the attribute and True through the property id.
"""

import pytest

from claude_hands.uia import core


class FakeCom:
    def GetRuntimeId(self):
        return ()


class RecordingElement(core.UiaElement):
    """Answers by property id, and refuses attribute reads the way COM does."""

    __slots__ = ("asked", "_available")

    def __init__(self, available):
        super().__init__(FakeCom())
        self.asked = []
        self._available = available
        # Everything `to_node_info` reads through `_prop`, pre-answered, so the
        # test is about pattern discovery and nothing else.
        self._props.update(
            {
                "Name": "Microsoft Search",
                "ControlType": 50004,  # edit
                "AutomationId": "SearchBox",
                "ClassName": "NetUIEdit",
                "HelpText": "",
                "IsEnabled": True,
                "IsOffscreen": False,
                "IsKeyboardFocusable": True,
                "HasKeyboardFocus": False,
                "NativeWindowHandle": 0,
                "BoundingRectangle": None,
            }
        )

    def _prop(self, name, default=None):
        if name not in self._props:
            # This is what COM does for CurrentIsValuePatternAvailable: there
            # is no such member. The old code swallowed it into `default`.
            raise AttributeError(name)
        return self._props[name]

    def property_value(self, property_name, default=None):
        self.asked.append(property_name)
        return self._available.get(property_name, default)


@pytest.fixture(autouse=True)
def no_com(monkeypatch):
    monkeypatch.setattr(core, "ensure_com", lambda: None)


def test_pattern_availability_is_read_by_property_id():
    element = RecordingElement({"UIA_IsValuePatternAvailablePropertyId": True})
    node = element.to_node_info()

    assert "value" in node.patterns
    assert "UIA_IsValuePatternAvailablePropertyId" in element.asked


def test_every_pattern_is_asked_for_by_id_not_by_attribute():
    element = RecordingElement({})
    element.to_node_info()

    for property_id in (
        "UIA_IsInvokePatternAvailablePropertyId",
        "UIA_IsValuePatternAvailablePropertyId",
        "UIA_IsTogglePatternAvailablePropertyId",
        "UIA_IsSelectionItemPatternAvailablePropertyId",
        "UIA_IsExpandCollapsePatternAvailablePropertyId",
        "UIA_IsScrollPatternAvailablePropertyId",
        "UIA_IsTextPatternAvailablePropertyId",
        "UIA_IsRangeValuePatternAvailablePropertyId",
    ):
        assert property_id in element.asked


def test_an_element_with_no_patterns_reports_none():
    node = RecordingElement({}).to_node_info()
    assert node.patterns == ()


def test_pattern_backed_state_follows_availability():
    element = RecordingElement(
        {
            "UIA_IsTogglePatternAvailablePropertyId": True,
            "UIA_ToggleToggleStatePropertyId": 1,
            "UIA_IsValuePatternAvailablePropertyId": True,
            "UIA_ValueValuePropertyId": "감염병",
        }
    )
    node = element.to_node_info()

    assert node.toggle_state == "on"
    assert node.value == "감염병"


def test_state_is_not_read_for_patterns_the_element_lacks():
    element = RecordingElement({})
    node = element.to_node_info()

    assert node.toggle_state is None
    assert node.value == ""
    assert "UIA_ToggleToggleStatePropertyId" not in element.asked


def test_all_pattern_ids_exist_in_the_cache_request():
    """A pattern read that is not cached costs a cross-process call per node."""

    for property_id in (
        "UIA_IsInvokePatternAvailablePropertyId",
        "UIA_IsValuePatternAvailablePropertyId",
        "UIA_IsTogglePatternAvailablePropertyId",
        "UIA_IsSelectionItemPatternAvailablePropertyId",
        "UIA_IsExpandCollapsePatternAvailablePropertyId",
        "UIA_IsScrollPatternAvailablePropertyId",
        "UIA_IsTextPatternAvailablePropertyId",
        "UIA_IsRangeValuePatternAvailablePropertyId",
    ):
        assert property_id in core._CACHED_PROPERTY_NAMES
