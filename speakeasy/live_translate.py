"""Live near-real-time speech translation / transcription.

Usage:
    python -m speakeasy.live_translate [--mode translate|transcribe]
        [--out session.md] [--polish-at-end] [--device N]
        [--input-wav file.wav] [--stats]

Continuously captures microphone audio (via speakeasy.audio.Recorder),
splits it into speech segments using a streaming RMS/silence endpoint
detector (adapted from speakeasy.translate_file's batch VAD chunker for
online use: closes a segment after ~0.6-0.9s of trailing silence once a
minimum length is met, or force-closes at a hard maximum), and feeds each
closed segment through a single ordered background worker thread that
calls the engine's `transcribe_ex`. Finalized lines are printed to stdout
immediately and appended (flushed) to a markdown file as they arrive, so a
crash loses nothing already spoken.

`--mode translate` uses the whisper-translate engine (Spanish speech -> English
text). `--mode transcribe` uses the configured default engine (Parakeet by
default) for live English voice-to-text. No diarization is performed: no
speaker labels are produced, only timestamped segments (documented in the
output header, mirroring translate_file.py).

`--input-wav` feeds a wav file through the exact same streaming pipeline at
real-time pace (chunked, not one-shot) instead of the microphone -- this is
the E2E proof path used because mic loopback is silent in this sandbox, and
it is also handy for repeatable testing against a fixed recording.

On Ctrl-C, the pipeline drains gracefully: capture stops, any pending
partial segment is flushed, the worker finishes whatever's already queued,
and only then (if --polish-at-end was given) is the full transcript sent
through the same headless-Haiku localization pattern translate_file.py
uses, writing a *separate* `<out>.polished.md` file -- the raw transcript
is never overwritten.
"""
from __future__ import annotations

import argparse
import queue
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator, Sequence

import numpy as np

from speakeasy.audio import Recorder
from speakeasy.config import load_settings, speakeasy_dir
from speakeasy.engine import TranscriptionResult, create_engine
from speakeasy.translate_file import (
    DEFAULT_MODELS_DIR,
    PolishError,
    TranslateFileError,
    build_account_pool,
    load_audio,
    polish_transcript,
)

SAMPLE_RATE = 16000


# --------------------------------------------------------------------------
# Streaming endpoint detector (state machine)
# --------------------------------------------------------------------------

@dataclass
class SegmenterConfig:
    sample_rate: int = SAMPLE_RATE
    silence_rms: float = 0.02
    min_dur: float = 1.0        # never silence-close a segment shorter than this
    max_dur: float = 15.0       # always force-close at this length
    silence_hold_s: float = 0.75  # trailing silence needed to close (0.6-0.9s target)


def _rms(frame: np.ndarray) -> float:
    if len(frame) == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(frame.astype(np.float64)))))


class StreamingSegmenter:
    """Online endpoint detector: feed audio frame-by-frame, get back a
    closed segment (concatenated samples) whenever one is ready.

    Closes on trailing silence only after both `min_dur` has been reached
    AND at least one non-silent frame has occurred since the last close
    (so a lull with no speech at all doesn't get chopped into empty
    segments). Always force-closes at `max_dur` regardless, to bound
    memory even through total silence.
    """

    def __init__(self, config: SegmenterConfig | None = None):
        self.config = config or SegmenterConfig()
        self._buffer: list[np.ndarray] = []
        self._buffer_len = 0
        self._silence_run_s = 0.0
        self._has_speech = False

    def feed(self, frame: np.ndarray) -> np.ndarray | None:
        if len(frame) == 0:
            return None
        cfg = self.config
        self._buffer.append(frame)
        self._buffer_len += len(frame)
        frame_dur = len(frame) / cfg.sample_rate
        if _rms(frame) < cfg.silence_rms:
            self._silence_run_s += frame_dur
        else:
            self._silence_run_s = 0.0
            self._has_speech = True
        seg_dur = self._buffer_len / cfg.sample_rate
        if seg_dur >= cfg.max_dur:
            return self._close()
        if (self._has_speech and seg_dur >= cfg.min_dur
                and self._silence_run_s >= cfg.silence_hold_s):
            return self._close()
        return None

    def flush(self) -> np.ndarray | None:
        """Force-close whatever is pending (used at shutdown). Discards a
        buffer that never had any speech in it (pure silence tail)."""
        if self._buffer_len == 0:
            return None
        if not self._has_speech:
            self._reset()
            return None
        return self._close()

    def _close(self) -> np.ndarray:
        out = np.concatenate(self._buffer) if self._buffer else np.zeros(0, dtype=np.float32)
        self._reset()
        return out

    def _reset(self) -> None:
        self._buffer = []
        self._buffer_len = 0
        self._silence_run_s = 0.0
        self._has_speech = False


def frames_from_samples(samples: np.ndarray, frame_size: int) -> Iterator[np.ndarray]:
    """Split a full array into fixed-size chunks (last one may be shorter).
    Used to feed a wav file through the streaming pipeline chunk-by-chunk."""
    n = len(samples)
    for i in range(0, n, frame_size):
        yield samples[i:i + frame_size]


# --------------------------------------------------------------------------
# Ordered background worker
# --------------------------------------------------------------------------

class SegmentWorker:
    """Single background thread draining a FIFO queue of closed segments
    through `transcribe_fn`, in submission order, no matter how long any
    individual call takes (a single consumer thread can't reorder its own
    queue). Calls `on_result(seq, result, close_wall, engine_latency_s,
    lag_s)` for each success, `on_error(seq, exc)` for each failure -- a
    failure never kills the thread or skips subsequent items.
    """

    _STOP = object()

    def __init__(self, transcribe_fn: Callable[[np.ndarray], object],
                 on_result: Callable, on_error: Callable | None = None):
        self._transcribe_fn = transcribe_fn
        self._on_result = on_result
        self._on_error = on_error
        self._queue: "queue.Queue" = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def submit(self, seq: int, samples: np.ndarray, close_monotonic: float,
              close_wall: datetime) -> None:
        self._queue.put((seq, samples, close_monotonic, close_wall))

    def pending_count(self) -> int:
        return self._queue.qsize()

    def drain_and_stop(self, timeout: float | None = None) -> None:
        """Block until every currently-queued segment has been processed,
        then stop the worker thread. Used for the SIGINT graceful shutdown:
        capture stops first, any final segment is submitted, THEN this is
        called so nothing already spoken is lost."""
        self._queue.put(self._STOP)
        self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is self._STOP:
                self._queue.task_done()
                break
            seq, samples, close_monotonic, close_wall = item
            t0 = time.monotonic()
            try:
                result = self._transcribe_fn(samples)
            except Exception as exc:  # noqa: BLE001 - report, never crash the drain
                if self._on_error:
                    self._on_error(seq, exc)
                self._queue.task_done()
                continue
            engine_latency_s = time.monotonic() - t0
            lag_s = time.monotonic() - close_monotonic
            self._on_result(seq, result, close_wall, engine_latency_s, lag_s)
            self._queue.task_done()


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def format_hms(when: datetime) -> str:
    return when.strftime("%H:%M:%S")


@dataclass
class TranscriptLine:
    start_time: datetime
    text: str
    engine_latency_s: float
    lag_s: float

    def render(self) -> str:
        return f"[{format_hms(self.start_time)}] {self.text}"


def render_header(mode: str, model_name: str, source_label: str,
                  when: datetime | None = None) -> str:
    when = when or datetime.now()
    lines = [
        f"# Live session: {source_label}",
        "",
        f"- Date: {when.isoformat(timespec='seconds')}",
        f"- Mode: {mode}",
        f"- Model: {model_name}",
        "- Note: speaker labels not available (timestamped segments only, no diarization)",
        "",
    ]
    return "\n".join(lines) + "\n"


def init_output(out_path: Path, mode: str, model_name: str, source_label: str,
                when: datetime | None = None) -> None:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_header(mode, model_name, source_label, when=when))


def append_line(out_path: Path, line: str) -> None:
    """Append one line + flush + close -- each call is a fully independent
    open/write/close so a crash between calls loses nothing already
    written (mirrors the history-first durability principle)."""
    with open(out_path, "a") as f:
        f.write(line + "\n")
        f.flush()


def append_footer(out_path: Path, stats: dict) -> None:
    count = stats.get("count", 0)
    latencies = stats.get("engine_latencies", [])
    lags = stats.get("lags", [])
    lines = ["", "## Session stats", "", f"- segments: {count}"]
    if latencies:
        lines.append(
            f"- engine latency (s): avg={sum(latencies)/len(latencies):.2f} "
            f"max={max(latencies):.2f}")
    if lags:
        lines.append(
            f"- end-to-end lag (s): avg={sum(lags)/len(lags):.2f} max={max(lags):.2f}")
    with open(out_path, "a") as f:
        f.write("\n".join(lines) + "\n")
        f.flush()


def print_stats(stats: dict) -> None:
    count = stats.get("count", 0)
    latencies = stats.get("engine_latencies", [])
    lags = stats.get("lags", [])
    print(f"\n-- stats: {count} segment(s) --")
    if latencies:
        print(f"engine latency: avg={sum(latencies)/len(latencies):.2f}s "
              f"max={max(latencies):.2f}s")
    if lags:
        print(f"end-to-end lag: avg={sum(lags)/len(lags):.2f}s max={max(lags):.2f}s")


# --------------------------------------------------------------------------
# --polish-at-end
# --------------------------------------------------------------------------

def run_polish_at_end(lines: Sequence[TranscriptLine], out_path: Path, pool,
                      polish_fn: Callable = polish_transcript) -> Path | None:
    """Send the full raw transcript through the same headless-Haiku
    localization pattern translate_file.py uses. Writes `<out>.polished.md`
    SEPARATELY; never touches/overwrites the raw `out_path`. Returns the
    polished path, or None (with a printed warning) if polish wasn't
    attempted/failed."""
    out_path = Path(out_path)
    if pool is None:
        print("warning: no ~/.claude-accounts.json found; skipping --polish-at-end, "
              "raw output kept.")
        return None
    if not lines:
        print("warning: nothing transcribed; skipping --polish-at-end.")
        return None
    raw_text = "\n".join(line.render() for line in lines)
    try:
        polished_text, account = polish_fn(raw_text, pool)
    except PolishError as e:
        print(f"warning: polish failed ({e}); raw output kept at {out_path}")
        return None
    polished_path = out_path.with_suffix(".polished.md")
    header = render_header("polished", f"account={account}", str(out_path))
    polished_path.write_text(header + "\n" + polished_text.strip() + "\n")
    print(f"Wrote polished translation (account={account}): {polished_path}")
    return polished_path


# --------------------------------------------------------------------------
# Engine selection
# --------------------------------------------------------------------------

def build_engine(mode: str, models_dir: Path, settings) -> tuple[object, str]:
    if mode == "translate":
        return create_engine("whisper-translate", models_dir), "whisper-translate"
    engine_name = settings.engine or "parakeet"
    return create_engine(engine_name, models_dir), engine_name


def make_transcribe_fn(engine) -> Callable[[np.ndarray], TranscriptionResult]:
    ex = getattr(engine, "transcribe_ex", None)
    if ex is not None:
        return lambda samples: ex(samples, SAMPLE_RATE)
    return lambda samples: TranscriptionResult(
        text=engine.transcribe(samples, SAMPLE_RATE), avg_logprob=None)


# --------------------------------------------------------------------------
# Session runner (shared by mic + --input-wav paths)
# --------------------------------------------------------------------------

class LiveSession:
    """Wires segmenter + worker + writer together. `frame_iter` yields raw
    audio frames (np.ndarray); the caller drives real-time pacing (mic
    polling, or a wav sleep loop) -- this class doesn't know or care where
    frames come from."""

    def __init__(self, engine, mode: str, model_name: str, out_path: Path,
                source_label: str, stats_enabled: bool = False):
        self.out_path = Path(out_path)
        self.stats_enabled = stats_enabled
        self._segmenter = StreamingSegmenter()
        self._seq = 0
        self.lines: list[TranscriptLine] = []
        self._stats = {"count": 0, "engine_latencies": [], "lags": []}
        init_output(self.out_path, mode, model_name, source_label)
        self._worker = SegmentWorker(make_transcribe_fn(engine), self._on_result,
                                     on_error=self._on_error)
        self._worker.start()

    def _on_result(self, seq, result, close_wall, engine_latency_s, lag_s) -> None:
        text = getattr(result, "text", result)
        if not text or not str(text).strip():
            return
        line = TranscriptLine(start_time=close_wall, text=str(text).strip(),
                              engine_latency_s=engine_latency_s, lag_s=lag_s)
        self.lines.append(line)
        rendered = line.render()
        print(rendered)
        append_line(self.out_path, rendered)
        self._stats["count"] += 1
        self._stats["engine_latencies"].append(engine_latency_s)
        self._stats["lags"].append(lag_s)

    def _on_error(self, seq, exc) -> None:
        print(f"warning: segment {seq} failed to transcribe: {exc}", file=sys.stderr)

    def feed(self, frame: np.ndarray) -> None:
        closed = self._segmenter.feed(frame)
        if closed is not None:
            self._submit(closed)

    def _submit(self, samples: np.ndarray) -> None:
        self._worker.submit(self._seq, samples, time.monotonic(), datetime.now())
        self._seq += 1

    def status_line(self, elapsed_s: float) -> str:
        return f"listening... {elapsed_s:.1f}s ({self._worker.pending_count()} queued)"

    def shutdown(self, polish_at_end: bool = False, pool=None) -> None:
        """Graceful drain: flush any pending partial segment, wait for the
        worker to finish everything already queued, THEN (optionally) run
        polish. Mockable / directly callable from tests without real
        signals."""
        pending = self._segmenter.flush()
        if pending is not None:
            self._submit(pending)
        self._worker.drain_and_stop(timeout=60)
        if polish_at_end:
            run_polish_at_end(self.lines, self.out_path, pool)
        if self.stats_enabled:
            print_stats(self._stats)
        append_footer(self.out_path, self._stats)


# --------------------------------------------------------------------------
# Mic + wav frame sources
# --------------------------------------------------------------------------

def run_mic_session(session: LiveSession, recorder: Recorder, poll_interval: float = 0.2,
                    stop_event: threading.Event | None = None) -> None:
    stop_event = stop_event or threading.Event()
    recorder.start()
    start = time.monotonic()
    last_len = 0
    is_tty = sys.stdout.isatty()
    try:
        while not stop_event.is_set():
            snap = recorder.snapshot()
            new = snap[last_len:]
            last_len = len(snap)
            if len(new) > 0:
                session.feed(new)
            if is_tty:
                print(f"\r{session.status_line(time.monotonic() - start)}   ",
                     end="", flush=True)
            time.sleep(poll_interval)
    finally:
        recorder.stop()
        if is_tty:
            print()


def run_wav_session(session: LiveSession, samples: np.ndarray, sample_rate: int = SAMPLE_RATE,
                    chunk_ms: float = 200.0, speed: float = 1.0,
                    stop_event: threading.Event | None = None) -> None:
    """Feeds `samples` through the pipeline in chunk_ms-sized chunks at
    real-time pace (sleeping between chunks), the E2E proof path: mic
    loopback is silent on this machine, so this is how the streaming
    pipeline is actually exercised end-to-end."""
    stop_event = stop_event or threading.Event()
    frame_size = max(1, int(sample_rate * chunk_ms / 1000))
    is_tty = sys.stdout.isatty()
    start = time.monotonic()
    for frame in frames_from_samples(samples, frame_size):
        if stop_event.is_set():
            break
        session.feed(frame)
        if is_tty:
            print(f"\r{session.status_line(time.monotonic() - start)}   ",
                 end="", flush=True)
        time.sleep((len(frame) / sample_rate) / max(speed, 1e-6))
    if is_tty:
        print()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m speakeasy.live_translate",
        description="Live near-real-time speech translation/transcription.")
    parser.add_argument("--mode", choices=["translate", "transcribe"], default="translate",
                        help="translate: ES speech -> EN text. transcribe: live V2T "
                             "in the configured default engine.")
    parser.add_argument("--out", type=Path, default=None,
                        help="output markdown path (default: live_<timestamp>.md)")
    parser.add_argument("--polish-at-end", action="store_true",
                        help="on shutdown, localize the full transcript via headless "
                             "Claude; written to a separate <out>.polished.md")
    parser.add_argument("--device", type=int, default=None, help="input device index")
    parser.add_argument("--input-wav", type=Path, default=None,
                        help="feed this wav through the streaming pipeline at real-time "
                             "pace instead of the microphone (proof/test path)")
    parser.add_argument("--stats", action="store_true",
                        help="print a latency summary to stdout at exit")
    parser.add_argument("--models-dir", type=Path, default=DEFAULT_MODELS_DIR)
    return parser


def default_out_path() -> Path:
    return Path(f"live_{datetime.now():%Y%m%d_%H%M%S}.md")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    out_path = args.out or default_out_path()

    settings = load_settings()
    print("Loading model (this may download several hundred MB the first time)...")
    try:
        engine, model_name = build_engine(args.mode, args.models_dir, settings)
    except Exception as e:
        print(f"error: could not load engine: {e}", file=sys.stderr)
        return 1

    pool = build_account_pool() if args.polish_at_end else None

    stop_event = threading.Event()

    def _sigint_handler(signum, frame):
        stop_event.set()

    previous_handler = signal.signal(signal.SIGINT, _sigint_handler)

    try:
        if args.input_wav:
            try:
                samples = load_audio(args.input_wav)
            except TranslateFileError as e:
                print(f"error: {e}", file=sys.stderr)
                return 1
            session = LiveSession(engine, args.mode, model_name, out_path,
                                  source_label=f"wav:{args.input_wav}",
                                  stats_enabled=args.stats)
            run_wav_session(session, samples, stop_event=stop_event)
        else:
            session = LiveSession(engine, args.mode, model_name, out_path,
                                  source_label=f"microphone (device {args.device})",
                                  stats_enabled=args.stats)
            recorder = Recorder(device=args.device if args.device is not None
                                else settings.input_device)
            run_mic_session(session, recorder, stop_event=stop_event)
        session.shutdown(polish_at_end=args.polish_at_end, pool=pool)
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    print(f"Wrote live transcript: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
