"""speakeasy.injector_linux tests. The clipboard piece uses the real
XClipboard against the live X session (this is exactly the code path
manually verified to round-trip correctly before this test file was
written) — no fake/mock clipboard, since the whole point of this module is
the real Xlib selection-ownership protocol. Only the paste keystroke
(`xdotool`) and typing path are mocked, since those are just a subprocess
call this project doesn't own."""

import sys

import pytest

# P1-5: Linux-only module (pynput/Xlib/X11). Skip cleanly elsewhere so
# `pytest -q` and the nx test target are green on macOS.
if not sys.platform.startswith("linux"):
    pytest.skip("Linux-only (X11) tests", allow_module_level=True)
pytest.importorskip("Xlib")

import time

import pytest
from Xlib import X, Xatom, display
from Xlib.protocol import request

from speakeasy.injector_linux import XClipboard, inject


def _clear_clipboard_selection() -> None:
    """Explicitly disowns the CLIPBOARD selection on the real X server (owner=NONE),
    regardless of who currently owns it -- ICCCM/X11 allow any client to do this, it
    doesn't require being the current owner. Needed because these tests exercise the
    real, live, process-wide CLIPBOARD selection (that's the whole point of this file
    -- see module docstring), so without an explicit reset, test order determines
    whether a given test starts from a clean slate (found via randomized ordering,
    ADJ-SPK-023 HF2 investigation: not the exact test the advisor's Mac run hit, but
    the same underlying "shared live state, no isolation between tests" root cause)."""
    d = display.Display()
    try:
        clipboard_atom = d.intern_atom("CLIPBOARD")
        request.SetSelectionOwner(display=d.display, window=X.NONE,
                                   selection=clipboard_atom, time=X.CurrentTime)
        d.sync()
    finally:
        d.close()


@pytest.fixture(autouse=True)
def _isolated_clipboard():
    """Every test in this file starts from -- and leaves -- a known-empty CLIPBOARD
    selection, so test order never determines what `XClipboard().get()` sees."""
    _clear_clipboard_selection()
    yield
    _clear_clipboard_selection()


@pytest.fixture
def clipboard():
    cb = XClipboard()
    yield cb
    cb.close()


def test_inject_paste_mode_restores_old_clipboard(clipboard):
    clipboard.set("old contents")
    pastes = []
    result = inject("new dictated text", clipboard=clipboard,
                     post_paste=lambda: pastes.append(1),
                     restore_delay=0.0)
    assert result == "injected"
    assert pastes == [1]
    assert clipboard.get() == "old contents"

def test_inject_paste_mode_sets_clipboard_during_the_paste_call():
    cb = XClipboard()
    cb.set("old contents")
    seen_during_paste = []

    def fake_paste():
        seen_during_paste.append(cb.get())

    inject("new dictated text", clipboard=cb, post_paste=fake_paste, restore_delay=0.0)
    assert seen_during_paste == ["new dictated text"]
    cb.close()

def test_inject_restores_clipboard_even_if_paste_raises():
    cb = XClipboard()
    cb.set("old contents")

    def raising_paste():
        raise RuntimeError("xdotool not found")

    with pytest.raises(RuntimeError):
        inject("new dictated text", clipboard=cb, post_paste=raising_paste, restore_delay=0.0)
    assert cb.get() == "old contents"
    cb.close()

def test_inject_paste_mode_with_no_prior_clipboard_restores_empty(clipboard):
    # get() on a selection nobody owns yet returns None -> restore to ""
    result = inject("text", clipboard=clipboard, post_paste=lambda: None, restore_delay=0.0)
    assert result == "injected"
    assert clipboard.get() == ""

def test_targets_negotiation_like_a_real_toolkit():
    """Real GTK/Qt paste targets ask TARGETS first (expecting a format-32
    ATOM list) before ever asking for the text itself — this is the exact
    negotiation the earlier, buggy _answer_request (ADJ-SPK-009 C1) got
    wrong, since it replied to every request with the raw text payload
    regardless of what was actually asked for. XClipboard.get() only ever
    asks for UTF8_STRING directly, so this test drives the raw protocol
    itself to actually exercise the TARGETS path."""
    owner = XClipboard()
    owner.set("targets negotiation test")
    time.sleep(0.05)

    requestor_disp = display.Display()
    win = requestor_disp.screen().root.create_window(
        0, 0, 1, 1, 0, X.CopyFromParent, X.InputOutput, X.CopyFromParent)
    clipboard_atom = requestor_disp.intern_atom("CLIPBOARD")
    targets_atom = requestor_disp.intern_atom("TARGETS")
    utf8_atom = requestor_disp.intern_atom("UTF8_STRING")
    prop_atom = requestor_disp.intern_atom("SPEAKEASY_TEST_XFER")

    def convert_and_wait(target_atom):
        win.convert_selection(clipboard_atom, target_atom, prop_atom, X.CurrentTime)
        requestor_disp.flush()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if requestor_disp.pending_events():
                ev = requestor_disp.next_event()
                if ev.type == X.SelectionNotify:
                    if ev.property == X.NONE:
                        return None, None
                    prop = win.get_full_property(prop_atom, X.AnyPropertyType)
                    win.delete_property(prop_atom)
                    return prop.property_type, prop.value
            else:
                time.sleep(0.01)
        return None, None

    # 1. TARGETS request first, exactly like a real toolkit.
    reply_type, reply_value = convert_and_wait(targets_atom)
    assert reply_type == Xatom.ATOM
    returned_atoms = list(reply_value)
    assert utf8_atom in returned_atoms
    assert targets_atom in returned_atoms

    # 2. Only now request the actual text, as UTF8_STRING.
    reply_type, reply_value = convert_and_wait(utf8_atom)
    assert reply_type == utf8_atom
    assert bytes(reply_value).decode("utf-8") == "targets negotiation test"

    win.destroy()
    requestor_disp.flush()
    owner.close()


def test_inject_type_mode_bypasses_clipboard_entirely(clipboard):
    clipboard.set("untouched")
    typed = []
    result = inject("direct typed text", clipboard=clipboard,
                     injection_mode="type", post_type=lambda t: typed.append(t))
    assert result == "injected"
    assert typed == ["direct typed text"]
    assert clipboard.get() == "untouched"     # type mode never touches it
