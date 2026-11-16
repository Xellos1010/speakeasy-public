"""speakeasy.hotkey_linux tests. Uses a fake pynput-shaped listener class
(constructor captures on_press/on_release, no real X11 grab) so these run
in any environment, not just against a live X session — the live-session
behavior itself (Xlib connects, CLIPBOARD round-trips for real) is
exercised separately, this file is about the state-machine wiring."""

import sys

import pytest

# P1-5: Linux-only module (pynput/Xlib/X11). Skip cleanly elsewhere so
# `pytest -q` and the nx test target are green on macOS.
if not sys.platform.startswith("linux"):
    pytest.skip("Linux-only (X11) tests", allow_module_level=True)
pytest.importorskip("pynput")

import importlib

from pynput import keyboard

from speakeasy.hotkey_linux import HotkeyListener, DEFAULT_TARGET_KEY


class FakeListener:
    """Records on_press/on_release so a test can call them directly,
    standing in for pynput.keyboard.Listener without touching a real
    display connection."""
    instances = []

    def __init__(self, on_press=None, on_release=None):
        self.on_press = on_press
        self.on_release = on_release
        self.started = False
        FakeListener.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.started = False


def make_listener(**kwargs):
    FakeListener.instances.clear()
    l = HotkeyListener(listener_cls=FakeListener, **kwargs)
    l.start()
    fake = FakeListener.instances[-1]
    return l, fake


def test_tap_toggles_on_target_key():
    calls = []
    l, fake = make_listener(on_toggle=lambda: calls.append("toggle"))
    l._time = lambda: 0.0
    fake.on_press(DEFAULT_TARGET_KEY)
    l._time = lambda: 0.05
    fake.on_release(DEFAULT_TARGET_KEY)
    assert calls == ["toggle"]

def test_other_modifier_does_not_chord():
    calls = []
    l, fake = make_listener(on_toggle=lambda: calls.append("toggle"))
    l._time = lambda: 0.0
    fake.on_press(DEFAULT_TARGET_KEY)
    fake.on_press(keyboard.Key.shift)     # modifier noise, not a chord
    fake.on_release(keyboard.Key.shift)
    l._time = lambda: 0.05
    fake.on_release(DEFAULT_TARGET_KEY)
    assert calls == ["toggle"]

def test_character_key_chords_and_suppresses_tap():
    calls = []
    l, fake = make_listener(on_toggle=lambda: calls.append("toggle"))
    l._time = lambda: 0.0
    fake.on_press(DEFAULT_TARGET_KEY)
    fake.on_press(keyboard.KeyCode.from_char("v"))   # real chord
    l._time = lambda: 0.05
    fake.on_release(DEFAULT_TARGET_KEY)
    assert calls == []

def test_hold_start_and_end():
    starts, ends = [], []
    l, fake = make_listener(on_hold_start=lambda: starts.append(1),
                             on_hold_end=lambda: ends.append(1))
    l._time = lambda: 0.0
    fake.on_press(DEFAULT_TARGET_KEY)
    l._time = lambda: 0.36
    l._check_hold()          # simulates the threading.Timer firing at tap_max_s
    assert starts == [1]
    l._time = lambda: 0.50
    fake.on_release(DEFAULT_TARGET_KEY)
    assert ends == [1]

def test_release_of_non_target_modifier_is_ignored():
    calls = []
    l, fake = make_listener(on_toggle=lambda: calls.append("toggle"))
    fake.on_release(keyboard.Key.ctrl)   # never pressed, never armed — no-op
    assert calls == []

def test_start_and_stop_do_not_raise():
    l, fake = make_listener()
    assert fake.started is True
    l.stop()
    assert fake.started is False


def test_module_imports_cleanly_even_missing_darwin_absent_key_attrs(monkeypatch):
    """HF1 (ADJ-SPK-023): pynput's Darwin backend doesn't define Key.num_lock /
    Key.scroll_lock (no such physical keys on a standard Mac keyboard). Simulate that
    by swapping in a stand-in for keyboard.Key missing exactly those two names (real
    enum members can't be deleted, hence the stand-in) and reloading the module -- it
    must not raise AttributeError at import time, and the two missing names must
    simply be absent from the built set, not silently replaced by something else.

    HF3 (ADJ-SPK-027) fix: the stand-in is built from the module's own declared
    _MODIFIER_KEY_NAMES with a synthetic fallback (object()) for any name the real
    platform doesn't provide -- NOT from dir(keyboard.Key) -- so this simulated
    baseline is always "all 12 names present" regardless of what the real platform's
    baseline happens to already be missing. The earlier version derived its baseline
    from the real dir(), which made the test itself non-portable: on macOS, where
    num_lock/scroll_lock are ALREADY absent from the real baseline (and, it turned
    out, so is at least one more name), "removing" already-absent names was a no-op,
    so the expected-count assertion (hardcoded as 10) didn't match the platform's
    actual post-removal count (9) -- a portability regression test that itself
    wasn't portable. This version's expected count is always
    len(_MODIFIER_KEY_NAMES) - 2, independent of any platform's real gaps."""
    import types
    import speakeasy.hotkey_linux as hotkey_linux_mod
    all_names = hotkey_linux_mod._MODIFIER_KEY_NAMES
    synthetic_full = {n: getattr(keyboard.Key, n, object()) for n in all_names}
    fake_key = types.SimpleNamespace(**{
        n: v for n, v in synthetic_full.items() if n not in ("num_lock", "scroll_lock")
    })
    monkeypatch.setattr(keyboard, "Key", fake_key)

    try:
        reloaded = importlib.reload(hotkey_linux_mod)
        assert reloaded.DEFAULT_TARGET_KEY in reloaded._MODIFIER_KEYS  # cmd_r still present
        assert len(reloaded._MODIFIER_KEYS) == len(all_names) - 2  # baseline-independent
    finally:
        importlib.reload(hotkey_linux_mod)  # restore real state for later tests
