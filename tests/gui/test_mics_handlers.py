"""Tests for speakeasy/gui/handlers/mics.py.

No real speaker-diarization backend exists in this codebase -- these tests
only cover real device enumeration (mics_list_devices), with a fake
query_devices() so hardware isn't required to run the suite.
"""
from speakeasy.gui.handlers.mics import mics_list_devices


def _fake_devices():
    return [
        {"name": "MacBook Pro Microphone", "max_input_channels": 1, "max_output_channels": 0},
        {"name": "MacBook Pro Speakers", "max_input_channels": 0, "max_output_channels": 2},
        {"name": "External USB Mic", "max_input_channels": 2, "max_output_channels": 0},
    ]


def test_lists_only_input_capable_devices_with_index_and_name():
    result = mics_list_devices({}, None, query_devices=_fake_devices,
                                default_device_fn=lambda: 0)

    assert result == {
        "devices": [
            {"index": 0, "name": "MacBook Pro Microphone", "is_default": True},
            {"index": 2, "name": "External USB Mic", "is_default": False},
        ]
    }


def test_no_input_devices_returns_empty_list():
    result = mics_list_devices({}, None, query_devices=lambda: [
        {"name": "MacBook Pro Speakers", "max_input_channels": 0, "max_output_channels": 2},
    ], default_device_fn=lambda: 0)

    assert result == {"devices": []}


def test_empty_device_list():
    result = mics_list_devices({}, None, query_devices=lambda: [],
                                default_device_fn=lambda: 0)

    assert result == {"devices": []}


def test_query_devices_exception_is_caught_and_reported():
    def _raise():
        raise OSError("no audio subsystem available")

    result = mics_list_devices({}, None, query_devices=_raise, default_device_fn=lambda: 0)

    assert result["devices"] == []
    assert "no audio subsystem available" in result["error"]


def test_default_lookup_raising_marks_no_device_as_default():
    def _raise_default():
        raise OSError("no default device")

    result = mics_list_devices({}, None, query_devices=_fake_devices,
                                default_device_fn=_raise_default)

    assert result == {
        "devices": [
            {"index": 0, "name": "MacBook Pro Microphone", "is_default": False},
            {"index": 2, "name": "External USB Mic", "is_default": False},
        ]
    }


def test_default_index_not_among_enumerated_devices_marks_none_as_default():
    # e.g. the system default is a speaker/output-only device, or an index
    # that doesn't correspond to any input-capable device -- never guess.
    result = mics_list_devices({}, None, query_devices=_fake_devices,
                                default_device_fn=lambda: 1)

    assert result == {
        "devices": [
            {"index": 0, "name": "MacBook Pro Microphone", "is_default": False},
            {"index": 2, "name": "External USB Mic", "is_default": False},
        ]
    }


def test_registered_on_bridge():
    from speakeasy.gui.bridge import ACTIONS

    assert ACTIONS["mics_list_devices"] is mics_list_devices
