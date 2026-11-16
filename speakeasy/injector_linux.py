"""X11 text injection (Linux), the sibling of speakeasy.injector — see
docs/superpowers/specs/2026-08-17-linux-client-port.md §2.

Clipboard access is implemented directly against Xlib's CLIPBOARD selection
rather than shelling out to xclip/xsel: neither is installed on the
reference box, and adding one would be a new system-package dependency the
README doesn't currently have (and this box has no working sudo/apt path in
this session anyway). Paste itself still shells out to `xdotool`, which
*is* already present, rather than hand-rolling XTest key synthesis.

Known gap, by design (ADJ-SPK-007 ratified this): unlike macOS's
`secure_input_active()` (a real Carbon API call), X11 has no equivalent
concept or API to detect a focused secure/password field. This module does
not fake that check by always returning False — it simply has no
`secure_check` branch at all, and always performs the real paste. Document
this in any user-facing text about the Linux client; do not represent it as
having macOS's guarantee.
"""

import subprocess
import threading
import time
from typing import Callable

from Xlib import X, Xatom, display


class XClipboard:
    """CLIPBOARD selection get/set via raw Xlib. Handles ordinary
    small-to-medium text (dictation transcripts) — does not implement the
    INCR protocol for very large transfers, which is not expected to matter
    for this use case.

    Must run its own event-serving loop while it holds selection ownership
    (an X11 selection owner is responsible for answering other clients'
    SelectionRequest events itself; there is no passive "clipboard" storage
    the server keeps for you) — `set()` starts a daemon thread for exactly
    that, alive until ownership is lost (SelectionClear) or `close()` runs.
    """

    def __init__(self, disp: display.Display | None = None):
        self._disp = disp or display.Display()
        self._win = self._disp.screen().root.create_window(
            0, 0, 1, 1, 0, X.CopyFromParent,
            X.InputOutput, X.CopyFromParent,
        )
        self._clipboard = self._disp.intern_atom("CLIPBOARD")
        self._utf8 = self._disp.intern_atom("UTF8_STRING")
        self._targets = self._disp.intern_atom("TARGETS")
        self._supported_targets = [self._targets, self._utf8, Xatom.STRING]
        self._pending_text: str | None = None
        self._serve_thread: threading.Thread | None = None
        self._stop_serving = threading.Event()

    def set(self, text: str) -> None:
        self.stop_serving()
        self._pending_text = text
        self._win.set_selection_owner(self._clipboard, X.CurrentTime)
        self._disp.flush()
        self._stop_serving.clear()
        self._serve_thread = threading.Thread(target=self._serve_loop, daemon=True)
        self._serve_thread.start()

    def _serve_loop(self) -> None:
        while not self._stop_serving.is_set():
            if self._disp.pending_events() == 0:
                time.sleep(0.01)
                continue
            ev = self._disp.next_event()
            if ev.type == X.SelectionClear:
                return
            if ev.type == X.SelectionRequest:
                self._answer_request(ev)

    def _answer_request(self, ev) -> None:
        """ICCCM-conformant reply. Real toolkits (GTK, Qt) ask TARGETS
        first, expecting a format-32 ATOM list, before ever asking for the
        actual text — replying to every request with the raw text payload
        (as an earlier version of this method did) sends a malformed
        TARGETS reply and can make paste silently fail against exactly the
        apps that matter. An unsupported target gets a refusal (property=
        None in the notify), per ICCCM, rather than a wrong-shaped payload."""
        req = ev.requestor
        reply_property = ev.property

        if ev.target == self._targets:
            req.change_property(ev.property, Xatom.ATOM, 32,
                                 self._supported_targets, mode=X.PropModeReplace)
        elif ev.target in (self._utf8, Xatom.STRING):
            payload = (self._pending_text or "").encode("utf-8")
            req.change_property(ev.property, ev.target, 8, list(payload),
                                 mode=X.PropModeReplace)
        else:
            reply_property = X.NONE   # refusal: unsupported target

        from Xlib.protocol.event import SelectionNotify
        req.send_event(SelectionNotify(
            time=ev.time, requestor=req, selection=ev.selection,
            target=ev.target, property=reply_property,
        ))
        self._disp.flush()

    def get(self, timeout: float = 1.0) -> str | None:
        owner = self._disp.get_selection_owner(self._clipboard)
        if owner == self._win:
            return self._pending_text
        # H1 (ADJ-SPK-009): Display objects aren't thread-safe. If we still
        # think we're the owner's serve thread hasn't processed a
        # SelectionClear yet, stop it before this method starts reading
        # events on the same connection too, to avoid two threads calling
        # next_event() concurrently.
        self.stop_serving()
        if owner == X.NONE:
            return None
        prop_atom = self._disp.intern_atom("SPEAKEASY_CLIPBOARD_XFER")
        self._win.convert_selection(self._clipboard, self._utf8, prop_atom, X.CurrentTime)
        self._disp.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._disp.pending_events() == 0:
                time.sleep(0.01)
                continue
            ev = self._disp.next_event()
            if ev.type == X.SelectionNotify:
                if ev.property == X.NONE:
                    return None
                prop = self._win.get_full_property(prop_atom, X.AnyPropertyType)
                self._win.delete_property(prop_atom)
                return bytes(prop.value).decode("utf-8") if prop else None
        return None

    def stop_serving(self) -> None:
        self._stop_serving.set()
        if self._serve_thread and self._serve_thread.is_alive():
            self._serve_thread.join(timeout=0.5)
        self._serve_thread = None

    def close(self) -> None:
        self.stop_serving()
        self._win.destroy()
        self._disp.flush()


def _post_ctrl_v(run=subprocess.run) -> None:
    run(["xdotool", "key", "--clearmodifiers", "ctrl+v"], check=True)


def _post_type(text: str, run=subprocess.run) -> None:
    run(["xdotool", "type", "--clearmodifiers", "--", text], check=True)


def inject(text: str, clipboard: XClipboard | None = None,
           post_paste: Callable[[], None] | None = None,
           post_type: Callable[[str], None] | None = None,
           injection_mode: str = "paste",
           restore_delay: float = 0.6, sleep=time.sleep) -> str:
    """Linux sibling of speakeasy.injector.inject(). No secure-input check
    (see module docstring) — always performs the real injection.

    injection_mode="type" bypasses the clipboard entirely (xdotool types
    the text directly), trading clipboard-safety concerns for being slower
    on long dictations and occasionally fighting an app's own IME/autocomplete.
    """
    if injection_mode == "type":
        (post_type or _post_type)(text)
        return "injected"

    cb = clipboard or XClipboard()
    paste = post_paste or _post_ctrl_v
    old = cb.get()
    cb.set(text)
    try:
        paste()
        sleep(restore_delay)
    finally:
        cb.set(old if old is not None else "")
    return "injected"
