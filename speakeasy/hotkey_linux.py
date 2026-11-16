"""X11 hotkey adapter (Linux) around the platform-neutral core in
speakeasy.hotkey_core — see docs/superpowers/specs/2026-08-17-linux-client-port.md
§1. Only this module (and hotkey.py's mac equivalent) may import a real
input backend; hotkey_core.py stays free of both.

Verified target environment: X11 session (XDG_SESSION_TYPE=x11), not
Wayland — pynput's global listener uses python-xlib on X11 and needs no
root/uinput access under a regular user session.
"""

import threading
import time
from typing import Callable

from pynput import keyboard

from speakeasy.hotkey_core import (
    TAP_MAX_S,
    EVENT_FLAGS_CHANGED,
    EVENT_KEY_DOWN,
    ACTION_TAP_TOGGLE,
    ACTION_HOLD_START,
    ACTION_HOLD_END,
    ModifierHotkeyStateMachine,
)

# Right Super/Meta: the closest hardware analog to macOS's Right Command on
# a typical PC keyboard, and unclaimed by GNOME's default keybindings on the
# reference box (Ubuntu 24.04 + GNOME). Right Ctrl and Right Alt were both
# rejected in the spec — Ctrl is overloaded in terminal apps, Alt is often
# remapped to AltGr by the active keyboard layout.
DEFAULT_TARGET_KEY = keyboard.Key.cmd_r

# pynput reports every key (including modifiers) through one unified
# press/release stream, unlike Quartz's separate kCGEventFlagsChanged vs
# kCGEventKeyDown event types. This set is how the adapter tells them
# apart before calling the state machine's feed(): any key in here is fed
# as EVENT_FLAGS_CHANGED (ignored unless it's the target — see
# ModifierHotkeyStateMachine's "other-modifier noise" handling); everything
# else (character keys, function keys, arrows, ...) is fed as
# EVENT_KEY_DOWN and will chord-guard the hotkey if held during a press.
# Built via getattr rather than direct attribute access (HF1, ADJ-SPK-023): pynput's
# Key enum is NOT identical across backends -- e.g. num_lock/scroll_lock exist on the
# X11 backend but not on pynput's Darwin backend, since standard Mac keyboards have no
# such physical keys. A direct `keyboard.Key.num_lock` reference at module level would
# raise AttributeError at IMPORT time on macOS, breaking `pytest tests/` collection
# there the same way test_hotkey.py's top-level `import Quartz` broke collection on
# Linux (ADJ-SPK-009 C2) -- don't "simplify" this back to a literal set.
_MODIFIER_KEY_NAMES = (
    "alt", "alt_gr", "alt_r", "caps_lock", "cmd", "cmd_r",
    "ctrl", "ctrl_r", "shift", "shift_r", "num_lock", "scroll_lock",
)
_MODIFIER_KEYS = frozenset(
    key for key in (getattr(keyboard.Key, name, None) for name in _MODIFIER_KEY_NAMES)
    if key is not None
)


class HotkeyListener:
    """X11 sibling of speakeasy.hotkey.HotkeyListener. Same public surface
    (start/stop, on_toggle/on_hold_start/on_hold_end callbacks) and the same
    tap/hold-PTT/chord-guard semantics, driven by the identical core state
    machine — only the event source differs.

    `hotkey` currently only supports the modifier-hotkey mode (unlike the
    mac adapter's legacy int-keycode/F5 path, which has no clean pynput
    equivalent and isn't needed on this platform yet).
    """

    def __init__(self, hotkey: str = "right_command",
                 target_key=DEFAULT_TARGET_KEY,
                 on_toggle: Callable[[], None] | None = None,
                 on_hold_start: Callable[[], None] | None = None,
                 on_hold_end: Callable[[], None] | None = None,
                 tap_max_s: float = TAP_MAX_S,
                 time_fn: Callable[[], float] = time.monotonic,
                 listener_cls=keyboard.Listener):
        self._on_toggle = on_toggle or (lambda: None)
        self._on_hold_start = on_hold_start or (lambda: None)
        self._on_hold_end = on_hold_end or (lambda: None)
        self._time = time_fn
        self._listener_cls = listener_cls
        self._sm = ModifierHotkeyStateMachine(tap_max_s, target_keycode=target_key)
        self._hold_timer: threading.Timer | None = None
        self._listener = None

    def _arm_hold_timer(self) -> None:
        self._cancel_hold_timer()
        self._hold_timer = threading.Timer(self._sm.tap_max_s, self._check_hold)
        self._hold_timer.daemon = True
        self._hold_timer.start()

    def _cancel_hold_timer(self) -> None:
        if self._hold_timer:
            self._hold_timer.cancel()
            self._hold_timer = None

    def _check_hold(self) -> None:
        if self._sm.check_hold(self._time()) == ACTION_HOLD_START:
            self._on_hold_start()

    def _on_press(self, key) -> None:
        if key in _MODIFIER_KEYS:
            was_down = self._sm.cmd_down
            action = self._sm.feed(EVENT_FLAGS_CHANGED, key, True, self._time())
            if not was_down and self._sm.cmd_down:
                self._arm_hold_timer()
            if action == ACTION_TAP_TOGGLE:      # a redundant "down" never fires this,
                self._on_toggle()                 # kept only for symmetry with feed()'s contract
        else:
            self._sm.feed(EVENT_KEY_DOWN, key, self._sm.cmd_down, self._time())
        # pynput's global listener cannot selectively swallow keys (that
        # needs a separate low-level grab); every key always reaches the
        # focused application, which matches the mac adapter's explicit
        # "modifier events are NEVER swallowed" contract for flagsChanged
        # and only diverges from it for the (unsupported here) legacy
        # int-keycode path, which used to swallow its own keypress.

    def _on_release(self, key) -> None:
        if key not in _MODIFIER_KEYS:
            return
        was_down = self._sm.cmd_down
        action = self._sm.feed(EVENT_FLAGS_CHANGED, key, False, self._time())
        if was_down and not self._sm.cmd_down:
            self._cancel_hold_timer()
        if action == ACTION_TAP_TOGGLE:
            self._on_toggle()
        elif action == ACTION_HOLD_END:
            self._on_hold_end()

    def start(self) -> None:
        self._listener = self._listener_cls(on_press=self._on_press,
                                             on_release=self._on_release)
        self._listener.start()

    def stop(self) -> None:
        self._cancel_hold_timer()
        if self._listener:
            self._listener.stop()
            self._listener = None
