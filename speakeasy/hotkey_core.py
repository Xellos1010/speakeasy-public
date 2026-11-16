"""Platform-neutral hotkey state machine, extracted from speakeasy.hotkey
(ADJ-SPK-007): the mac-specific module imports Quartz at the top level and
therefore cannot be imported at all on Linux. Nothing in this module may
import Quartz, pynput, Xlib, or any other platform-specific input library —
it is fed primitives by a platform adapter (speakeasy.hotkey on macOS,
speakeasy.hotkey_linux on Linux) and never talks to a real event source
itself.
"""

RIGHT_COMMAND_KEYCODE = 54
TAP_MAX_S = 0.35

# Event kinds fed to the pure state machine, decoupled from any platform's
# own event-type representation so the state machine is unit-testable
# without any real keyboard-event objects.
EVENT_FLAGS_CHANGED = "flags_changed"
EVENT_KEY_DOWN = "key_down"

# Actions the state machine asks the caller to perform.
ACTION_TAP_TOGGLE = "tap_toggle"
ACTION_HOLD_START = "hold_start"
ACTION_HOLD_END = "hold_end"


class ModifierHotkeyStateMachine:
    """Pure tap/hold/chord-guard state machine for a modifier-only hotkey
    (Right Command on macOS, keycode 54, via kCGEventFlagsChanged; Right
    Super/Meta on Linux X11, via pynput's Key.cmd_r) that only ever emits
    flags-changed-style events for the target key.

    Feed it primitives, never platform event objects:

        feed(event_type, keycode, cmd_flag_set, timestamp)
        check_hold(timestamp)

    Rules:
      TAP   - target key down then up within `tap_max_s`, no other key
              pressed in between -> ACTION_TAP_TOGGLE on release.
      HOLD  - target key held >= tap_max_s with no other key pressed ->
              ACTION_HOLD_START once the threshold is crossed (via
              check_hold, polled by a caller-side timer), ACTION_HOLD_END
              on release.
      CHORD - any keyDown while the target key is held marks the press as
              a chord: nothing fires on release (neither tap nor hold).

    Modifier flagsChanged noise for keys other than the target (e.g. the
    user also tapping shift/option) is ignored and never chords the press;
    only an actual EVENT_KEY_DOWN does. The platform adapter is responsible
    for classifying its own events into EVENT_FLAGS_CHANGED (modifier keys)
    vs EVENT_KEY_DOWN (everything else) before calling feed().
    """

    def __init__(self, tap_max_s: float = TAP_MAX_S,
                 target_keycode=RIGHT_COMMAND_KEYCODE):
        self.tap_max_s = tap_max_s
        self._target_keycode = target_keycode
        self.cmd_down = False
        self._press_time: float | None = None
        self._chorded = False
        self._hold_fired = False

    def feed(self, event_type: str, keycode, cmd_flag_set: bool,
              timestamp: float) -> str | None:
        if event_type == EVENT_FLAGS_CHANGED:
            if keycode != self._target_keycode:
                return None                       # other-modifier noise
            if cmd_flag_set and not self.cmd_down:
                self.cmd_down = True
                self._press_time = timestamp
                self._chorded = False
                self._hold_fired = False
                return None
            if not cmd_flag_set and self.cmd_down:
                return self._release(timestamp)
            return None                           # redundant flagsChanged
        if event_type == EVENT_KEY_DOWN:
            if self.cmd_down:
                self._chorded = True
            return None
        return None

    def check_hold(self, timestamp: float) -> str | None:
        if (self.cmd_down and not self._hold_fired and not self._chorded
                and (timestamp - self._press_time) >= self.tap_max_s):
            self._hold_fired = True
            return ACTION_HOLD_START
        return None

    def _release(self, timestamp: float) -> str | None:
        duration = timestamp - self._press_time
        chorded, hold_fired = self._chorded, self._hold_fired
        self.cmd_down = False
        self._press_time = None
        self._chorded = False
        self._hold_fired = False
        if chorded:
            return None
        if hold_fired:
            return ACTION_HOLD_END
        if duration < self.tap_max_s:
            return ACTION_TAP_TOGGLE
        return None       # threshold crossed but check_hold was never polled in time
