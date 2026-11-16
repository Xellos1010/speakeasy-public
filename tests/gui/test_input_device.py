"""Tests for speakeasy/gui/input_device.py::resolve_input_device -- the
pure validate-and-fall-back logic behind speakeasy/__main__.py's
_set_input_device (not itself independently testable without constructing
the whole app, same reason speakeasy/gui/wizard_start.py's
open_wizard_or_fallback was extracted)."""
from speakeasy.gui.input_device import resolve_input_device


def test_none_always_resolves_to_none_no_fallback():
    assert resolve_input_device(None, {0, 2}) == (None, False)


def test_real_available_index_resolves_unchanged():
    assert resolve_input_device(2, {0, 2}) == (2, False)


def test_stale_index_falls_back_to_none():
    # e.g. an unplugged USB mic that used to be index 2
    assert resolve_input_device(2, {0}) == (None, True)


def test_stale_index_against_empty_available_set_falls_back_to_none():
    assert resolve_input_device(0, set()) == (None, True)
