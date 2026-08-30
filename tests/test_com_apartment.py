"""COM apartment selection.

comtypes joins an apartment when it is imported, and the choice is final. The
whole tool died on real Windows because the worker imported comtypes first and
asked for the multi-threaded apartment afterwards, getting RPC_E_CHANGED_MODE.
`doctor` happened to import comtypes on the main thread first, which hid it.
"""

import sys

import pytest

from claude_hands.uia.core import (
    COINIT_MULTITHREADED,
    RPC_E_CHANGED_MODE,
    init_thread_com,
    prefer_mta,
)


class FakeComtypes:
    """Stands in for comtypes without needing Windows."""

    COINIT_MULTITHREADED = 0

    def __init__(self, raises=None):
        self.calls = []
        self._raises = raises

    def CoInitializeEx(self, flags):
        self.calls.append(flags)
        if self._raises is not None:
            raise self._raises


def windows_error(code: int) -> OSError:
    exc = OSError(f"apartment already set ({code})")
    exc.winerror = code
    return exc


def test_prefer_mta_sets_the_flag_before_comtypes_is_imported(monkeypatch):
    monkeypatch.delitem(sys.modules, "comtypes", raising=False)
    monkeypatch.delattr(sys, "coinit_flags", raising=False)

    assert prefer_mta() is True
    assert sys.coinit_flags == COINIT_MULTITHREADED


def test_prefer_mta_declines_once_comtypes_is_loaded(monkeypatch):
    monkeypatch.setitem(sys.modules, "comtypes", object())
    monkeypatch.setattr(sys, "coinit_flags", 2, raising=False)

    assert prefer_mta() is False
    assert sys.coinit_flags == 2  # left exactly as the host set it


def test_init_thread_com_joins_the_mta_when_it_can():
    fake = FakeComtypes()
    assert init_thread_com(fake) == "mta"
    assert fake.calls == [0]


def test_an_already_fixed_apartment_is_not_a_failure():
    """UIA works from an STA too — dying here would be worse than adapting."""

    fake = FakeComtypes(raises=windows_error(RPC_E_CHANGED_MODE))
    assert init_thread_com(fake) == "sta"


def test_other_com_errors_still_propagate():
    fake = FakeComtypes(raises=windows_error(-2147024891))  # E_ACCESSDENIED
    with pytest.raises(OSError):
        init_thread_com(fake)


def test_module_import_registers_the_preference():
    """Importing claude_hands must express the MTA preference by itself.

    Nothing else may have to import comtypes first for the worker to start.
    """

    import claude_hands.uia.core  # noqa: F401

    if "comtypes" not in sys.modules:
        assert getattr(sys, "coinit_flags", None) == COINIT_MULTITHREADED
