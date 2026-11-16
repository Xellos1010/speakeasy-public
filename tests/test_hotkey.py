import pytest

Quartz = pytest.importorskip("Quartz")  # macOS-only module; self-skip elsewhere (ADJ-SPK-009 C2)
from speakeasy.hotkey import (
    should_toggle,
    ModifierHotkeyStateMachine,
    HotkeyListener,
    RIGHT_COMMAND_KEYCODE,
    TAP_MAX_S,
    EVENT_FLAGS_CHANGED,
    EVENT_KEY_DOWN,
    ACTION_TAP_TOGGLE,
    ACTION_HOLD_START,
    ACTION_HOLD_END,
)

# --- legacy F5 / int-keycode path -------------------------------------------

def test_matches_only_keydown_of_target():
    assert should_toggle(Quartz.kCGEventKeyDown, 96, 96) is True
    assert should_toggle(Quartz.kCGEventKeyUp, 96, 96) is False
    assert should_toggle(Quartz.kCGEventKeyDown, 49, 96) is False


# --- pure state machine: tap / hold / chord ---------------------------------

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
    # duration == tap_max_s: not < tap_max_s, and hold was never polled -> no action
    assert release(sm, TAP_MAX_S) is None

def test_hold_threshold_crossing_fires_start_then_end_on_release():
    sm = ModifierHotkeyStateMachine()
    press(sm, 0.0)
    assert sm.check_hold(0.20) is None            # not yet at threshold
    assert sm.check_hold(0.36) == ACTION_HOLD_START
    assert sm.check_hold(0.40) is None             # already fired, no repeat
    assert release(sm, 0.50) == ACTION_HOLD_END

def test_hold_shorter_than_threshold_is_just_a_tap():
    sm = ModifierHotkeyStateMachine()
    press(sm, 0.0)
    assert sm.check_hold(0.10) is None             # released before threshold
    assert release(sm, 0.15) == ACTION_TAP_TOGGLE

def test_chord_guard_fires_nothing_on_release():
    sm = ModifierHotkeyStateMachine()
    press(sm, 0.0)
    key_down(sm, 9, 0.05)                          # 'v' keyDown while cmd held
    assert release(sm, 0.10) is None               # chorded -> no tap

def test_chord_guard_also_suppresses_hold():
    sm = ModifierHotkeyStateMachine()
    press(sm, 0.0)
    key_down(sm, 9, 0.05)
    assert sm.check_hold(0.40) is None             # chorded -> no hold either
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
    # left-shift flagsChanged (keycode 56) while right-cmd held: ignored noise
    sm.feed(EVENT_FLAGS_CHANGED, 56, True, 0.05)
    sm.feed(EVENT_FLAGS_CHANGED, 56, False, 0.08)
    assert release(sm, 0.10) == ACTION_TAP_TOGGLE

def test_other_modifier_noise_does_not_break_hold():
    sm = ModifierHotkeyStateMachine()
    press(sm, 0.0)
    sm.feed(EVENT_FLAGS_CHANGED, 58, True, 0.05)   # left-option noise
    assert sm.check_hold(0.36) == ACTION_HOLD_START
    assert release(sm, 0.50) == ACTION_HOLD_END

def test_redundant_flagschanged_is_a_no_op():
    sm = ModifierHotkeyStateMachine()
    press(sm, 0.0)
    # duplicate "down" event (cmd already down) must not reset the press timer
    assert sm.feed(EVENT_FLAGS_CHANGED, RIGHT_COMMAND_KEYCODE, True, 0.30) is None
    assert release(sm, 0.34) == ACTION_TAP_TOGGLE  # measured from original press at t=0

def test_release_without_prior_press_is_a_no_op():
    sm = ModifierHotkeyStateMachine()
    assert release(sm, 1.0) is None

def test_keydown_without_cmd_down_does_not_arm_chord():
    sm = ModifierHotkeyStateMachine()
    key_down(sm, 9, 0.0)                            # unrelated typing, cmd never pressed
    press(sm, 1.0)
    assert release(sm, 1.05) == ACTION_TAP_TOGGLE


# --- HotkeyListener wiring: modifier events are NEVER swallowed -------------

class FakeEvent:
    def __init__(self, keycode, cmd_set):
        self.keycode = keycode
        self.cmd_set = cmd_set

def _patch_quartz(monkeypatch):
    monkeypatch.setattr(Quartz, "CGEventGetIntegerValueField",
                         lambda event, field: event.keycode)
    monkeypatch.setattr(Quartz, "CGEventGetFlags",
                         lambda event: Quartz.kCGEventFlagMaskCommand if event.cmd_set else 0)

def test_flagschanged_event_is_never_swallowed(monkeypatch):
    _patch_quartz(monkeypatch)
    calls = []
    listener = HotkeyListener("right_command", on_toggle=lambda: calls.append("toggle"))
    listener._time = lambda: 0.0
    ev_down = FakeEvent(RIGHT_COMMAND_KEYCODE, True)
    result = listener._handler(None, Quartz.kCGEventFlagsChanged, ev_down, None)
    assert result is ev_down                        # unchanged, never None
    listener._time = lambda: 0.05
    ev_up = FakeEvent(RIGHT_COMMAND_KEYCODE, False)
    result = listener._handler(None, Quartz.kCGEventFlagsChanged, ev_up, None)
    assert result is ev_up
    assert calls == ["toggle"]

def test_legacy_int_hotkey_still_swallows_its_keydown(monkeypatch):
    _patch_quartz(monkeypatch)
    calls = []
    listener = HotkeyListener(96, on_toggle=lambda: calls.append("toggle"))
    ev = FakeEvent(96, False)
    result = listener._handler(None, Quartz.kCGEventKeyDown, ev, None)
    assert result is None                            # legacy path still swallows
    assert calls == ["toggle"]
