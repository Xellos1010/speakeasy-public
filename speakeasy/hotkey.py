import threading
import time
from typing import Callable
import Quartz

from speakeasy.hotkey_core import (
    RIGHT_COMMAND_KEYCODE,
    TAP_MAX_S,
    EVENT_FLAGS_CHANGED,
    EVENT_KEY_DOWN,
    ACTION_TAP_TOGGLE,
    ACTION_HOLD_START,
    ACTION_HOLD_END,
    ModifierHotkeyStateMachine,
)


def should_toggle(event_type: int, keycode: int, target_keycode: int) -> bool:
    """Legacy keyDown-hotkey match (e.g. F5). Used for the int-keycode config path."""
    return event_type == Quartz.kCGEventKeyDown and keycode == target_keycode


class HotkeyListener:
    """Global CGEventTap-backed hotkey listener.

    `hotkey` is either the string "right_command" (tap-to-toggle / hold-to-talk
    on Right Command, keycode 54, via kCGEventFlagsChanged) or a legacy int
    keycode (e.g. 96 = F5, matched on kCGEventKeyDown as before).
    """

    def __init__(self, hotkey: str | int,
                 on_toggle: Callable[[], None] | None = None,
                 on_hold_start: Callable[[], None] | None = None,
                 on_hold_end: Callable[[], None] | None = None,
                 tap_max_s: float = TAP_MAX_S,
                 time_fn: Callable[[], float] = time.monotonic):
        self._legacy_keycode = hotkey if isinstance(hotkey, int) else None
        self._on_toggle = on_toggle or (lambda: None)
        self._on_hold_start = on_hold_start or (lambda: None)
        self._on_hold_end = on_hold_end or (lambda: None)
        self._time = time_fn
        self._sm = (None if self._legacy_keycode is not None
                    else ModifierHotkeyStateMachine(tap_max_s))
        self._hold_timer: threading.Timer | None = None
        self._tap = None
        self._source = None

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

    def _handler(self, proxy, event_type, event, refcon):
        if event_type == Quartz.kCGEventTapDisabledByTimeout and self._tap:
            Quartz.CGEventTapEnable(self._tap, True)
            return event

        if event_type == Quartz.kCGEventFlagsChanged:
            if self._sm is not None:
                keycode = Quartz.CGEventGetIntegerValueField(
                    event, Quartz.kCGKeyboardEventKeycode)
                cmd_set = bool(Quartz.CGEventGetFlags(event)
                               & Quartz.kCGEventFlagMaskCommand)
                was_down = self._sm.cmd_down
                action = self._sm.feed(EVENT_FLAGS_CHANGED, keycode, cmd_set,
                                        self._time())
                if not was_down and self._sm.cmd_down:
                    self._arm_hold_timer()
                elif was_down and not self._sm.cmd_down:
                    self._cancel_hold_timer()
                if action == ACTION_TAP_TOGGLE:
                    self._on_toggle()
                elif action == ACTION_HOLD_END:
                    self._on_hold_end()
            return event          # modifier events are NEVER swallowed

        if event_type == Quartz.kCGEventKeyDown:
            keycode = Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGKeyboardEventKeycode)
            if self._sm is not None:
                self._sm.feed(EVENT_KEY_DOWN, keycode, self._sm.cmd_down, self._time())
                return event      # never swallow the user's own keystrokes
            if should_toggle(event_type, keycode, self._legacy_keycode):
                self._on_toggle()
                return None       # swallow the legacy hotkey's own keypress
            return event
        return event

    def start(self) -> None:
        mask = (Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
                | Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged))
        self._tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionDefault, mask, self._handler, None)
        if self._tap is None:
            raise PermissionError(
                "CGEventTap failed — grant Accessibility permission to this app "
                "(System Settings > Privacy & Security > Accessibility)")
        self._source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), self._source,
                                  Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(self._tap, True)

    def stop(self) -> None:
        self._cancel_hold_timer()
        if self._tap:
            Quartz.CGEventTapEnable(self._tap, False)
            self._tap = None
