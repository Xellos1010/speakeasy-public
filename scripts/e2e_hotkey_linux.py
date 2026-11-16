"""Genuine synthetic-key proof of speakeasy.hotkey_linux against a real X11
session (ADJ-SPK-009 evidence-gap closure): every other hotkey test uses a
fake listener_cls so it can run anywhere, including CI without a display.
This script is the one place that drives an actual pynput.keyboard.Listener
with real X11 events, synthesized via `xdotool keydown`/`keyup`/`key` --
the same tool the reference session's operator explicitly approved for this
purpose (it visibly pops the desktop's Activities-overview / equivalent
global-shortcut UI each time Right Super is pressed, and sends one real
keypress to whatever window has focus during the chord case).

Not run by default `pytest` — needs a live X11 session and will visibly
interrupt it. Run directly: `python scripts/e2e_hotkey_linux.py`.

Expected output ends with "ALL SMOKE TESTS PASSED"; any AssertionError
means real behavior diverged from the fake-listener unit tests.
"""

import subprocess
import time

from speakeasy.hotkey_linux import HotkeyListener


def xdo(*args):
    subprocess.run(["xdotool", *args], check=True)


def main():
    events = []
    listener = HotkeyListener(
        on_toggle=lambda: events.append("toggle"),
        on_hold_start=lambda: events.append("hold_start"),
        on_hold_end=lambda: events.append("hold_end"),
    )
    listener.start()
    time.sleep(0.3)  # let the real X11 listener thread actually attach

    try:
        print("--- TAP test ---")
        events.clear()
        xdo("keydown", "Super_R")
        time.sleep(0.10)
        xdo("keyup", "Super_R")
        time.sleep(0.15)
        print("events:", events)
        assert events == ["toggle"], f"expected tap toggle, got {events}"

        print("--- HOLD test ---")
        events.clear()
        xdo("keydown", "Super_R")
        time.sleep(0.50)  # exceed tap_max_s (0.35s) so hold fires
        xdo("keyup", "Super_R")
        time.sleep(0.15)
        print("events:", events)
        assert events == ["hold_start", "hold_end"], f"expected hold start+end, got {events}"

        print("--- CHORD test ---")
        events.clear()
        xdo("keydown", "Super_R")
        time.sleep(0.05)
        xdo("key", "a")  # a full press+release while Super_R is held
        time.sleep(0.05)
        xdo("keyup", "Super_R")
        time.sleep(0.15)
        print("events:", events)
        assert events == [], f"expected chord to suppress everything, got {events}"
    finally:
        listener.stop()

    print("ALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
