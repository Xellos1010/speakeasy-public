"""Pure state-machine tests for speakeasy.hotkey_core (ADJ-SPK-007).

Deliberately imports nothing platform-specific (no Quartz, no pynput, no
Xlib) — this is the test that proves the core module is actually importable
on Linux, unlike speakeasy.hotkey (which still requires Quartz and is
covered by tests/test_hotkey.py, macOS-only). These cases mirror the
pure-state-machine section of test_hotkey.py exactly, since the logic
itself was moved, not rewritten.
"""

from speakeasy.hotkey_core import (
    ModifierHotkeyStateMachine,
    RIGHT_COMMAND_KEYCODE,
    TAP_MAX_S,
    EVENT_FLAGS_CHANGED,
    EVENT_KEY_DOWN,
    ACTION_TAP_TOGGLE,
    ACTION_HOLD_START,
    ACTION_HOLD_END,
)


def press(sm, t):
    return sm.feed(EVENT_FLAGS_CHANGED, RIGHT_COMMAND_KEYCODE, True, t)

def release(sm, t):
    return sm.feed(EVENT_FLAGS_CHANGED, RIGHT_COMMAND_KEYCODE, False, t)

def key_down(sm, keycode, t):
    return sm.feed(EVENT_KEY_DOWN, keycode, sm.cmd_down, t)


def test_tap_within_threshold_fires_toggle_on_release():
    sm = ModifierHotkeyStateMachine()
    assert press(sm, 0.0) is None
    assert release(sm, 0.10) == ACTION_TAP_TOGGLE

def test_tap_exactly_at_threshold_boundary_does_not_toggle():
    sm = ModifierHotkeyStateMachine()
    press(sm, 0.0)
    assert release(sm, TAP_MAX_S) is None

def test_hold_threshold_crossing_fires_start_then_end_on_release():
    sm = ModifierHotkeyStateMachine()
    press(sm, 0.0)
    assert sm.check_hold(0.20) is None
    assert sm.check_hold(0.36) == ACTION_HOLD_START
    assert sm.check_hold(0.40) is None
    assert release(sm, 0.50) == ACTION_HOLD_END

def test_hold_shorter_than_threshold_is_just_a_tap():
    sm = ModifierHotkeyStateMachine()
    press(sm, 0.0)
    assert sm.check_hold(0.10) is None
    assert release(sm, 0.15) == ACTION_TAP_TOGGLE

def test_chord_guard_fires_nothing_on_release():
    sm = ModifierHotkeyStateMachine()
    press(sm, 0.0)
    key_down(sm, 9, 0.05)
    assert release(sm, 0.10) is None

def test_chord_guard_also_suppresses_hold():
    sm = ModifierHotkeyStateMachine()
    press(sm, 0.0)
    key_down(sm, 9, 0.05)
    assert sm.check_hold(0.40) is None
    assert release(sm, 0.50) is None

def test_double_tap_each_fires_independently():
    sm = ModifierHotkeyStateMachine()
    press(sm, 0.0)
    assert release(sm, 0.05) == ACTION_TAP_TOGGLE
    press(sm, 0.10)
    assert release(sm, 0.15) == ACTION_TAP_TOGGLE

def test_other_modifier_noise_does_not_break_tap():
    sm = ModifierHotkeyStateMachine()
    press(sm, 0.0)
    sm.feed(EVENT_FLAGS_CHANGED, 56, True, 0.05)
    sm.feed(EVENT_FLAGS_CHANGED, 56, False, 0.08)
    assert release(sm, 0.10) == ACTION_TAP_TOGGLE

def test_other_modifier_noise_does_not_break_hold():
    sm = ModifierHotkeyStateMachine()
    press(sm, 0.0)
    sm.feed(EVENT_FLAGS_CHANGED, 58, True, 0.05)
    assert sm.check_hold(0.36) == ACTION_HOLD_START
    assert release(sm, 0.50) == ACTION_HOLD_END

def test_redundant_flagschanged_is_a_no_op():
    sm = ModifierHotkeyStateMachine()
    press(sm, 0.0)
    assert sm.feed(EVENT_FLAGS_CHANGED, RIGHT_COMMAND_KEYCODE, True, 0.30) is None
    assert release(sm, 0.34) == ACTION_TAP_TOGGLE

def test_release_without_prior_press_is_a_no_op():
    sm = ModifierHotkeyStateMachine()
    assert release(sm, 1.0) is None

def test_keydown_without_cmd_down_does_not_arm_chord():
    sm = ModifierHotkeyStateMachine()
    key_down(sm, 9, 0.0)
    press(sm, 1.0)
    assert release(sm, 1.05) == ACTION_TAP_TOGGLE


# --- non-int keycode identity (Linux adapter uses pynput Key enum members) --

def test_target_keycode_accepts_non_int_identity():
    """The Linux adapter feeds pynput Key enum members (e.g. Key.cmd_r), not
    raw ints, as the keycode/target_keycode identity — the state machine
    only ever compares them for equality, so any hashable works."""
    TARGET = object()
    OTHER = object()
    sm = ModifierHotkeyStateMachine(target_keycode=TARGET)
    assert sm.feed(EVENT_FLAGS_CHANGED, TARGET, True, 0.0) is None
    assert sm.feed(EVENT_FLAGS_CHANGED, OTHER, True, 0.05) is None   # noise, ignored
    assert sm.feed(EVENT_FLAGS_CHANGED, TARGET, False, 0.10) == ACTION_TAP_TOGGLE
