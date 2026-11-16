"""Genuine synthetic-Cmd-V proof into a real, active, external app (Stickies).

Terminal-launched python HAS Accessibility permission (inherited from the
terminal — verified: it can create event taps and post events) but CANNOT become
the active application. So instead of pasting into our own inactive window, we
target Stickies: `open -a` makes it the active app with a note's text area
focused, exactly like a user's real focused field. We post the injector's real
Cmd-V, then read the focused field's value back through the Accessibility API
(AXUIElement) — no dependency on the target's AppleScript dictionary.

Cleanup: the note we created is closed at the end.
"""
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np
import Quartz
from ApplicationServices import (AXUIElementCopyAttributeValue,
                                 AXUIElementCreateApplication)
from AppKit import NSWorkspace

from speakeasy import injector
from speakeasy.accounts import AccountPool
from speakeasy.app import Controller
from speakeasy.config import Settings, speakeasy_dir
from speakeasy.engine import create_engine
from speakeasy.history import History
from speakeasy.modes import load_modes


def key(keycode, flags=0):
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    for down in (True, False):
        e = Quartz.CGEventCreateKeyboardEvent(src, keycode, down)
        if flags:
            Quartz.CGEventSetFlags(e, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
        time.sleep(0.03)


def stickies_pid():
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if app.localizedName() == "Stickies":
            return app.processIdentifier()
    return None


def read_focused_value(pid):
    ax = AXUIElementCreateApplication(pid)
    err, focused = AXUIElementCopyAttributeValue(ax, "AXFocusedUIElement", None)
    if err or focused is None:
        return None
    err, val = AXUIElementCopyAttributeValue(focused, "AXValue", None)
    return None if err else (str(val) if val is not None else None)


class WavRecorder:
    def __init__(self, wav):
        with wave.open(str(wav), "rb") as w:
            raw = w.readframes(w.getnframes())
        self._s = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    def start(self):
        pass

    def stop(self):
        return self._s

    def snapshot(self):
        return self._s

    level = 0.1


def run(mode_name: str, wav: Path, tag: str) -> int:
    base = speakeasy_dir()
    engine = create_engine("parakeet", base / "models")   # preload before focus

    controller = Controller(
        engine=engine, recorder=WavRecorder(wav),
        history=History(base / "history.db"),
        modes=load_modes(base / "modes.yaml"),
        pool=AccountPool(Path.home() / ".claude-accounts.json", base / "state.json"),
        settings=Settings(active_mode=mode_name),
        inject_fn=injector.inject,
        frontmost=injector.frontmost_app_name,
        run_async=False)

    subprocess.run(["killall", "-9", "Stickies"], capture_output=True)  # clean slate
    time.sleep(1.0)
    subprocess.run(["open", "-a", "Stickies"], check=True, timeout=12)
    time.sleep(2.5)
    key(45, Quartz.kCGEventFlagMaskCommand)   # Cmd-N: fresh empty note, focused
    time.sleep(1.2)

    pid = stickies_pid()
    t0 = time.monotonic()
    controller.toggle()
    controller.toggle()   # transcribe -> real Cmd-V into the focused Stickies note
    elapsed = time.monotonic() - t0
    time.sleep(0.8)

    pasted = read_focused_value(pid) if pid else None

    ev = Path(__file__).resolve().parent.parent / "evidence"
    ev.mkdir(parents=True, exist_ok=True)
    subprocess.run(["screencapture", "-x", str(ev / f"e2e-{tag}.png")], timeout=15)
    (ev / f"e2e-{tag}-focused-field.txt").write_text(pasted or "")

    # Cleanup: hard-quit Stickies (no save dialog, no leftover modal).
    subprocess.run(["killall", "-9", "Stickies"], capture_output=True)

    row = History(base / "history.db").recent(1)[0]
    print(f"MODE={mode_name} ELAPSED={elapsed:.2f}s STICKIES_PID={pid}")
    print(f"FOCUSED_FIELD={pasted!r}")
    print(f"HISTORY status={row['status']} account={row['account']} "
          f"latency_ms={row['latency_ms']} raw={row['raw']!r} "
          f"rewritten={row['rewritten']!r}")
    ok = bool(pasted and pasted.strip()) and row["status"] == "injected"
    print("RESULT=", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "raw"
    wav_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("tests/assets/sample.wav")
    tag = sys.argv[3] if len(sys.argv) > 3 else mode
    sys.exit(run(mode, wav_path, tag))
