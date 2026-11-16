"""Batch-translate a recorded conversation to English.

Usage:
    python -m speakeasy.translate_file <audio> [--out out.md] [--polish] [--language es]

Decodes a wav/m4a/mp3/mp4 recording, resamples to 16kHz mono, splits it into
silence-aligned chunks (Whisper only handles <=30s windows), runs each chunk
through a local sherpa-onnx Whisper model in translate mode, and writes a
timestamped English markdown transcript. No diarization is performed: this
tool does NOT label speakers (no true diarization available on-device), it
only timestamps segments.

Optionally (--polish) the full raw transcript is sent through headless Claude
for American-English localization; the raw transcript is always written to
disk first so a polish failure never loses work.
"""
from __future__ import annotations

import argparse
import glob as globmod
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

import numpy as np

SAMPLE_RATE = 16000

# Fallback model spec, only used if speakeasy.engine.download has no
# "whisper-translate" entry (it doesn't, as of writing).
MODEL_DIRNAME = "sherpa-onnx-whisper-small"
MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    "sherpa-onnx-whisper-small.tar.bz2"
)
DEFAULT_MODELS_DIR = Path.home() / ".speakeasy" / "models"


class AudioDecodeError(Exception):
    """Raised when the input audio can't be decoded to 16kHz mono float32."""


class TranslateFileError(Exception):
    """Raised for user-facing fatal errors (missing ffmpeg, bad model, etc)."""


# --------------------------------------------------------------------------
# Audio decoding
# --------------------------------------------------------------------------

def decode_audio_ffmpeg(path: Path, ffmpeg_bin: str = "ffmpeg",
                        runner: Callable = subprocess.run) -> np.ndarray:
    """Decode+resample any ffmpeg-readable file to 16kHz mono float32 PCM."""
    cmd = [ffmpeg_bin, "-v", "error", "-i", str(path),
           "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"]
    proc = runner(cmd, capture_output=True)
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace") if proc.stderr else ""
        raise AudioDecodeError(f"ffmpeg failed decoding {path}: {stderr.strip()}")
    return np.frombuffer(proc.stdout, dtype="<f4").copy()


def decode_audio_wave(path: Path) -> np.ndarray:
    """Pure-python decode for a 16kHz mono wav file (no ffmpeg needed)."""
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getframerate() != SAMPLE_RATE:
            raise AudioDecodeError(
                f"{path} is not 16kHz mono wav (got {w.getnchannels()}ch @ "
                f"{w.getframerate()}Hz); install ffmpeg to decode this file "
                "(brew install ffmpeg)")
        raw = w.readframes(w.getnframes())
        width = w.getsampwidth()
        if width == 2:
            return (np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0)
        if width == 4:
            return (np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0)
        raise AudioDecodeError(f"unsupported wav sample width {width} bytes")


def load_audio(path: Path, ffmpeg_bin: str | None = None,
               runner: Callable = subprocess.run,
               which: Callable[[str], str | None] = shutil.which) -> np.ndarray:
    """Load `path` as 16kHz mono float32 samples.

    Uses ffmpeg when available (any container/codec); otherwise falls back
    to a pure-python wav reader that only accepts already-16kHz-mono wav.
    `ffmpeg_bin=None` auto-detects via `which("ffmpeg")` (injectable so tests
    can simulate a missing ffmpeg on a machine that actually has it).
    """
    path = Path(path)
    if not path.exists():
        raise TranslateFileError(f"input file not found: {path}")
    if ffmpeg_bin is None:
        ffmpeg_bin = which("ffmpeg")
    if ffmpeg_bin:
        return decode_audio_ffmpeg(path, ffmpeg_bin, runner=runner)
    if path.suffix.lower() == ".wav":
        return decode_audio_wave(path)
    raise TranslateFileError(
        f"ffmpeg not found and {path.name} isn't a 16kHz mono wav.\n"
        "Install ffmpeg to decode wav/m4a/mp3/mp4 recordings:\n"
        "    brew install ffmpeg")


# --------------------------------------------------------------------------
# Segmentation (simple RMS/silence VAD chunker)
# --------------------------------------------------------------------------

def frame_rms(samples: np.ndarray, frame_size: int) -> list[float]:
    """RMS energy per fixed-size frame (last partial frame included)."""
    n = len(samples)
    if frame_size <= 0:
        raise ValueError("frame_size must be positive")
    out = []
    for i in range(0, n, frame_size):
        frame = samples[i:i + frame_size]
        if len(frame) == 0:
            continue
        out.append(float(np.sqrt(np.mean(np.square(frame.astype(np.float64))))))
    return out


def find_silence_points(samples: np.ndarray, sample_rate: int = SAMPLE_RATE,
                        frame_ms: float = 30.0, silence_rms: float = 0.02) -> list[int]:
    """Sample indices at the midpoint of each below-threshold frame."""
    frame_size = max(1, int(sample_rate * frame_ms / 1000))
    rms = frame_rms(samples, frame_size)
    return [i * frame_size + frame_size // 2
            for i, r in enumerate(rms) if r < silence_rms]


def segment_audio(samples: np.ndarray, sample_rate: int = SAMPLE_RATE,
                  min_dur: float = 5.0, max_dur: float = 28.0,
                  frame_ms: float = 30.0, silence_rms: float = 0.02
                  ) -> list[tuple[int, int]]:
    """Split `samples` into (start, end) sample-index chunks of min_dur..max_dur
    seconds, cutting on silence where possible; hard-cuts at max_dur otherwise.
    Whisper only handles <=30s windows so max_dur must stay under that.
    """
    total = len(samples)
    if total == 0:
        return []
    min_samples = int(min_dur * sample_rate)
    max_samples = int(max_dur * sample_rate)
    if max_samples <= 0:
        raise ValueError("max_dur must be positive")
    if total <= max_samples:
        return [(0, total)]

    silence_points = find_silence_points(samples, sample_rate, frame_ms, silence_rms)
    segments: list[tuple[int, int]] = []
    start = 0
    while start < total:
        remaining = total - start
        if remaining <= max_samples:
            segments.append((start, total))
            break
        window_lo = start + min_samples
        window_hi = start + max_samples
        candidates = [p for p in silence_points if window_lo <= p <= window_hi]
        cut = candidates[-1] if candidates else start + max_samples
        if cut <= start:
            cut = start + max_samples
        segments.append((start, cut))
        start = cut
    return segments


# --------------------------------------------------------------------------
# Timestamp formatting
# --------------------------------------------------------------------------

def format_timestamp(seconds: float) -> str:
    """Format seconds as MM:SS (minutes not capped at 59 for long audio)."""
    total = int(round(seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes:02d}:{secs:02d}"


# --------------------------------------------------------------------------
# Model acquisition + translate engine
# --------------------------------------------------------------------------

def _glob_one(model_dir: Path, *patterns: str) -> Path:
    for pattern in patterns:
        matches = sorted(globmod.glob(str(model_dir / "**" / pattern), recursive=True))
        if matches:
            return Path(matches[0])
    raise TranslateFileError(
        f"no file matching {patterns!r} found under {model_dir}")


def ensure_model(models_dir: Path = DEFAULT_MODELS_DIR,
                 fetch: Callable | None = None) -> Path:
    """Resolve (downloading if needed) the whisper translate model directory.

    Prefers speakeasy.engine.download's registry (if it grows a
    "whisper-translate" entry); otherwise downloads our own known-good
    sherpa-onnx whisper-small archive.
    """
    try:
        from speakeasy.engine.download import ensure_model as _shared_ensure, MODELS
        if "whisper-translate" in MODELS:
            kwargs = {"fetch": fetch} if fetch is not None else {}
            return _shared_ensure("whisper-translate", models_dir, **kwargs)
    except ImportError:
        pass
    return _ensure_local_model(models_dir, fetch=fetch)


def _ensure_local_model(models_dir: Path, fetch: Callable | None = None) -> Path:
    target = models_dir / MODEL_DIRNAME
    if target.exists():
        return target
    models_dir.mkdir(parents=True, exist_ok=True)
    # Mirrors the TLS fix documented in README.md for the bare python.org
    # build shipping without a CA bundle.
    os.environ.setdefault("SSL_CERT_FILE", "/etc/ssl/cert.pem")
    fetch = fetch or urllib.request.urlretrieve
    with tempfile.NamedTemporaryFile(suffix=".tar.bz2", delete=False) as tmp:
        tmp_path = tmp.name
    fetch(MODEL_URL, tmp_path)
    with tarfile.open(tmp_path, "r:bz2") as t:
        t.extractall(models_dir, filter="data")
    if not target.exists():
        raise FileNotFoundError(f"archive did not contain {MODEL_DIRNAME}")
    return target


class TranslateEngine:
    """Wraps a sherpa-onnx OfflineRecognizer built for whisper translate task."""

    def __init__(self, model_dir: Path, language: str = "es", num_threads: int = 4):
        import sherpa_onnx  # local import: heavy, native dependency
        d = Path(model_dir)
        encoder = _glob_one(d, "*encoder*.int8.onnx", "*encoder*.onnx")
        decoder = _glob_one(d, "*decoder*.int8.onnx", "*decoder*.onnx")
        tokens = _glob_one(d, "*tokens.txt")
        self._rec = sherpa_onnx.OfflineRecognizer.from_whisper(
            encoder=str(encoder), decoder=str(decoder), tokens=str(tokens),
            language=language, task="translate", num_threads=num_threads)

    def translate(self, samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> str:
        stream = self._rec.create_stream()
        stream.accept_waveform(sample_rate, samples)
        self._rec.decode_stream(stream)
        return stream.result.text.strip()


# --------------------------------------------------------------------------
# Transcription pipeline (pure-ish: engine + samples in, lines out)
# --------------------------------------------------------------------------

@dataclass
class TranscriptLine:
    start_s: float
    text: str

    def render(self) -> str:
        return f"[{format_timestamp(self.start_s)}] {self.text}"


def transcribe_segments(samples: np.ndarray, engine: TranslateEngine,
                        sample_rate: int = SAMPLE_RATE,
                        min_dur: float = 5.0, max_dur: float = 28.0
                        ) -> list[TranscriptLine]:
    segments = segment_audio(samples, sample_rate=sample_rate,
                             min_dur=min_dur, max_dur=max_dur)
    lines = []
    for start, end in segments:
        chunk = samples[start:end]
        if len(chunk) == 0:
            continue
        text = engine.translate(chunk, sample_rate=sample_rate)
        if not text:
            continue
        lines.append(TranscriptLine(start_s=start / sample_rate, text=text))
    return lines


# --------------------------------------------------------------------------
# Output file
# --------------------------------------------------------------------------

def render_markdown(source: Path, model_name: str, lines: Sequence[TranscriptLine],
                    mode: str = "raw", when: datetime | None = None) -> str:
    when = when or datetime.now()
    header = [
        f"# Translation: {source.name}",
        "",
        f"- Source: `{source}`",
        f"- Date: {when.isoformat(timespec='seconds')}",
        f"- Model: {model_name}",
        f"- Mode: {mode}",
        "- Note: speaker labels not available (timestamped segments only, no diarization)",
        "",
    ]
    body = [line.render() for line in lines]
    return "\n".join(header + body) + "\n"


def write_output(out_path: Path, source: Path, model_name: str,
                 lines: Sequence[TranscriptLine], mode: str = "raw") -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_markdown(source, model_name, lines, mode=mode))
    return out_path


def default_output_path(source: Path) -> Path:
    return source.with_suffix("").with_name(source.stem + ".translated.md")


# --------------------------------------------------------------------------
# --polish: headless Claude localization pass (replicates rewrite.py's
# subprocess + account-rotation pattern locally; rewrite.py itself is
# out of scope to import per work-order file-scope rules).
# --------------------------------------------------------------------------

POLISH_PROMPT = (
    "You are a localization editor. The following is an English transcript "
    "produced by machine translation from Spanish speech, as timestamped "
    "lines in the form `[MM:SS] text`. Localize it to natural American "
    "English. Preserve meaning, facts, names, and numbers exactly. Preserve "
    "the register (formal/informal) of the original. Keep every line's "
    "`[MM:SS]` timestamp prefix intact and in the same order — output the "
    "same timestamped lines back, only the text after each timestamp may "
    "change. Output ONLY the timestamped lines, no commentary, no preamble, "
    "no code fences."
)


class PolishError(Exception):
    pass


def polish_transcript(raw_text: str, pool, model: str = "claude-haiku-4-5-20251001",
                      runner: Callable = subprocess.run, timeout: float = 60.0,
                      max_attempts: int = 4) -> tuple[str, str]:
    """Send the full raw transcript through headless Claude for localization.

    Returns (text, account_name_or_"none"). Raises PolishError if every
    account attempt failed; callers should catch this and fall back to the
    raw transcript rather than losing work.
    """
    prompt = f"{POLISH_PROMPT}\n\n<transcript>\n{raw_text}\n</transcript>"
    last_err = "no eligible account"
    for _ in range(max_attempts):
        acc = pool.next_account()
        if acc is None:
            break
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(acc.dir)}
        cmd = ["claude", "-p", prompt, "--model", model, "--output-format", "text"]
        try:
            proc = runner(cmd, env=env, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            pool.mark_cooling(acc.name)
            last_err = f"{acc.name}: timeout"
            continue
        if proc.returncode != 0:
            pool.mark_cooling(acc.name)
            last_err = f"{acc.name}: exit {proc.returncode}: {(proc.stderr or '')[:200]}"
            continue
        return proc.stdout.strip(), acc.name
    raise PolishError(last_err)


def build_account_pool():
    from speakeasy.accounts import AccountPool
    from speakeasy.config import speakeasy_dir
    from speakeasy.deploy_profile import accounts_path as _accounts_path
    accounts_path = _accounts_path()
    if not accounts_path.exists():
        return None
    return AccountPool(accounts_path, speakeasy_dir() / "state.json")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m speakeasy.translate_file",
        description="Translate a recorded conversation to timestamped English text.")
    parser.add_argument("audio", type=Path, help="input wav/m4a/mp3/mp4 file")
    parser.add_argument("--out", type=Path, default=None,
                        help="output markdown path (default: <audio>.translated.md)")
    parser.add_argument("--polish", action="store_true",
                        help="localize the raw translation via headless Claude")
    parser.add_argument("--language", default="es", help="source spoken language (default: es)")
    parser.add_argument("--models-dir", type=Path, default=DEFAULT_MODELS_DIR)
    args = parser.parse_args(argv)

    try:
        samples = load_audio(args.audio)
    except TranslateFileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"Loading translate model (this may download ~650MB the first time)...")
    try:
        model_dir = ensure_model(args.models_dir)
        engine = TranslateEngine(model_dir, language=args.language)
    except Exception as e:
        print(f"error: could not load translate model: {e}", file=sys.stderr)
        return 1

    print("Transcribing/translating...")
    lines = transcribe_segments(samples, engine)

    out_path = args.out or default_output_path(args.audio)
    write_output(out_path, args.audio, model_dir.name, lines, mode="raw")
    print(f"Wrote raw translation: {out_path}")

    if args.polish:
        pool = build_account_pool()
        if pool is None:
            print("warning: no ~/.claude-accounts.json found; skipping --polish, raw output kept.")
        else:
            raw_text = "\n".join(line.render() for line in lines)
            try:
                polished_text, account = polish_transcript(raw_text, pool)
                out_path.write_text(
                    render_polished_markdown(args.audio, model_dir.name, polished_text, account))
                print(f"Wrote polished translation (account={account}): {out_path}")
            except PolishError as e:
                print(f"warning: polish failed ({e}); raw output kept at {out_path}")
    return 0


def render_polished_markdown(source: Path, model_name: str, polished_text: str,
                             account: str, when: datetime | None = None) -> str:
    when = when or datetime.now()
    header = [
        f"# Translation: {source.name}",
        "",
        f"- Source: `{source}`",
        f"- Date: {when.isoformat(timespec='seconds')}",
        f"- Model: {model_name}",
        f"- Mode: polished (account={account})",
        "- Note: speaker labels not available (timestamped segments only, no diarization)",
        "",
    ]
    return "\n".join(header) + "\n" + polished_text.strip() + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
