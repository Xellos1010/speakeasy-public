"""Autonomous E2E proof of the real speakeasy pipeline into a real external app.

Substitutes ONLY the microphone's acoustic path with a real recorded WAV
(acoustic speaker->mic loopback is silent on this machine). Everything else is
real: the Parakeet engine transcribes, History persists to SQLite, and the
injector posts a real CGEvent Cmd-V into the frontmost TextEdit document — which,
being a separate process with its own runloop, handles the paste during the
injector's clipboard-swap window (so run_async=False is correct here). If a
rewrite mode is given, the real `claude -p` round-robin runs.

TextEdit is the target because it is the canonical "any text field" and, unlike
the in-process NSTextView attempt, an already-active external app keeps focus
while this headless script runs (a plain python process does not steal focus).
"""
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

from speakeasy import injector
from speakeasy.accounts import AccountPool
from speakeasy.app import Controller
from speakeasy.config import Settings, speakeasy_dir
from speakeasy.engine import create_engine
from speakeasy.history import History
from speakeasy.modes import load_modes

TARGET = Path("/tmp/e2e_target.txt")


def osa(script: str, timeout: float = 12) -> str:
    r = subprocess.run(["osascript", "-e", script], capture_output=True,
                       text=True, timeout=timeout)
    return r.stdout.strip()


def prep_textedit():
    TARGET.write_text("")
    subprocess.run(["open", "-a", "TextEdit", str(TARGET)], check=True, timeout=12)
    time.sleep(2.0)
    osa('tell application "TextEdit" to set text of document 1 to ""')
    osa('tell application "TextEdit" to activate')
    time.sleep(1.5)


def load_wav(wav: Path) -> np.ndarray:
    with wave.open(str(wav), "rb") as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


class WavRecorder:
    def __init__(self, samples):
        self._s = samples

    def start(self):
        pass

    def stop(self):
        return self._s

    def snapshot(self):
        return self._s

    level = 0.1


def run(mode_name: str, wav: Path, tag: str) -> int:
    base = speakeasy_dir()
    # Preload the slow engine BEFORE we touch focus, so the activate->paste gap is tiny.
    engine = create_engine("parakeet", base / "models")
    samples = load_wav(wav)

    controller = Controller(
        engine=engine, recorder=WavRecorder(samples),
        history=History(base / "history.db"),
        modes=load_modes(base / "modes.yaml"),
        pool=AccountPool(Path.home() / ".claude-accounts.json", base / "state.json"),
        settings=Settings(active_mode=mode_name),
        inject_fn=injector.inject,
        frontmost=injector.frontmost_app_name,
        run_async=False)

    prep_textedit()
    t0 = time.monotonic()
    controller.toggle()   # start (samples ready)
    controller.toggle()   # stop -> transcribe -> (rewrite) -> real Cmd-V into TextEdit
    elapsed = time.monotonic() - t0
    time.sleep(0.8)

    text = osa('tell application "TextEdit" to get text of document 1')
    ev = Path(__file__).resolve().parent.parent / "evidence"
    ev.mkdir(parents=True, exist_ok=True)
    subprocess.run(["screencapture", "-x", str(ev / f"e2e-{tag}.png")], timeout=15)
    (ev / f"e2e-{tag}-textedit.txt").write_text(text)

    row = History(base / "history.db").recent(1)[0]
    print(f"MODE={mode_name} ELAPSED={elapsed:.2f}s")
    print(f"TEXTEDIT={text!r}")
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
