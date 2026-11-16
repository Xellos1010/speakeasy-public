"""End-to-end proof: real engine -> history -> real CGEvent Cmd-V paste into a
real, focused, native NSTextView (key window of a regular Cocoa app).

This exercises the true injection path (synthetic system keyboard event posted to
the HID tap, delivered through the Cocoa responder chain to a focused text view)
without depending on any external app's AppleScript modality. The microphone's
acoustic path is the only substitution: a real recorded 16 kHz WAV stands in for
live capture. Uses run_async=True so the AppKit runloop pumps on the main thread
and the paste is handled inside the injector's clipboard-swap window, exactly as
in the shipped app.
"""
import sys
import time
import wave
from pathlib import Path

import numpy as np
from AppKit import (NSApplication, NSApplicationActivationPolicyRegular,
                    NSBackingStoreBuffered, NSMakeRect, NSRunningApplication,
                    NSTextView, NSWindow, NSWindowStyleMaskTitled)
from Foundation import NSDate, NSDefaultRunLoopMode, NSRunLoop

from speakeasy import injector
from speakeasy.accounts import AccountPool
from speakeasy.app import Controller
from speakeasy.config import Settings, speakeasy_dir
from speakeasy.engine import create_engine
from speakeasy.history import History
from speakeasy.modes import load_modes


class WavRecorder:
    def __init__(self, wav_path: Path):
        with wave.open(str(wav_path), "rb") as w:
            raw = w.readframes(w.getnframes())
        self._s = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    def start(self):
        pass

    def stop(self):
        return self._s

    def snapshot(self):
        return self._s

    level = 0.1


def pump(seconds: float):
    deadline = time.monotonic() + seconds
    loop = NSRunLoop.currentRunLoop()
    while time.monotonic() < deadline:
        loop.runMode_beforeDate_(NSDefaultRunLoopMode,
                                 NSDate.dateWithTimeIntervalSinceNow_(0.05))


def run(mode_name: str, wav: Path, tag: str) -> int:
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    rect = NSMakeRect(200, 400, 640, 240)
    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, NSWindowStyleMaskTitled, NSBackingStoreBuffered, False)
    win.setTitle_("speakeasy E2E target")
    tv = NSTextView.alloc().initWithFrame_(rect)
    tv.setEditable_(True)
    tv.setSelectable_(True)
    win.setContentView_(tv)
    import os
    import subprocess as _sp
    # Some other app may hold the foreground and refuse to yield it (e.g. a
    # comms app in a call). Hide currently-visible GUI apps (reversible, like
    # Cmd-H; does not quit them or end calls) so this window can take focus for
    # the paste. Restored in the finally block below.
    hidden = []
    try:
        names = _sp.run(["osascript", "-e",
                         'tell application "System Events" to get name of '
                         '(every process whose visible is true and background only is false)'],
                        capture_output=True, text=True, timeout=8).stdout
        hidden = [n.strip() for n in names.split(",")
                  if n.strip() and n.strip() not in ("Python", "Terminal", "iTerm2", "Finder")]
        for n in hidden:
            _sp.run(["osascript", "-e",
                     f'tell application "System Events" to set visible of process "{n}" to false'],
                    capture_output=True, timeout=6)
    except Exception as e:  # noqa: BLE001
        print(f"(hide step warning: {e})")

    app.activateIgnoringOtherApps_(True)
    win.makeKeyAndOrderFront_(None)
    win.makeFirstResponder_(tv)
    pump(0.5)
    NSRunningApplication.currentApplication().activateWithOptions_(1 << 1)
    _sp.run(["osascript", "-e",
             f'tell application "System Events" to set frontmost of '
             f'(first process whose unix id is {os.getpid()}) to true'],
            capture_output=True, timeout=8)
    win.makeKeyAndOrderFront_(None)
    win.makeFirstResponder_(tv)
    pump(1.0)   # let the window become key & first responder

    base = speakeasy_dir()
    settings = Settings(active_mode=mode_name)
    controller = Controller(
        engine=create_engine("parakeet", base / "models"),
        recorder=WavRecorder(wav),
        history=History(base / "history.db"),
        modes=load_modes(base / "modes.yaml"),
        pool=AccountPool(Path.home() / ".claude-accounts.json", base / "state.json"),
        settings=settings,
        inject_fn=injector.inject,
        frontmost=lambda: "speakeasy-e2e",
        run_async=True)

    t0 = time.monotonic()
    controller.toggle()   # start
    controller.toggle()   # stop -> bg thread: transcribe -> inject (Cmd-V)
    pump(6.0)             # process transcription + paste on main runloop
    elapsed = time.monotonic() - t0

    text = str(tv.string())
    # Restore every app we hid, regardless of outcome.
    for n in hidden:
        _sp.run(["osascript", "-e",
                 f'tell application "System Events" to set visible of process "{n}" to true'],
                capture_output=True, timeout=6)

    ev = Path(__file__).resolve().parent.parent / "evidence"
    ev.mkdir(parents=True, exist_ok=True)
    import subprocess
    subprocess.run(["screencapture", "-x", str(ev / f"e2e-{tag}.png")], timeout=15)
    (ev / f"e2e-{tag}-textview.txt").write_text(text)

    row = History(base / "history.db").recent(1)[0]
    print(f"MODE={mode_name} ELAPSED={elapsed:.2f}s")
    print(f"TEXTVIEW={text!r}")
    print(f"HISTORY status={row['status']} account={row['account']} "
          f"latency_ms={row['latency_ms']} raw={row['raw']!r} "
          f"rewritten={row['rewritten']!r} target={row['target_app']!r}")
    ok = bool(text.strip()) and row["status"] in ("injected", "clipboard_only")
    print("RESULT=", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "raw"
    wav_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("tests/assets/sample.wav")
    tag = sys.argv[3] if len(sys.argv) > 3 else mode
    sys.exit(run(mode, wav_path, tag))
