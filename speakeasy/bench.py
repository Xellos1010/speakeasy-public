"""Cross-platform per-host STT benchmark harness (ADJ-SPK-024).

Writes ~/.speakeasy/host_settings.json (docs/superpowers/specs/2026-08-18-linux-client-port.md
§4 shape) as a repeatable tool -- the original numbers in that shape came from an ad-hoc,
uncommitted script, and nothing in the repo wrote that file at all (config.py only reads it).
This is the one real implementation; each host runs it to produce its own record per R-06.

Engine-only, deliberately: no Quartz, no X11/pynput/Xlib imports anywhere in this module, so
it runs identically on macOS and Linux. Synthetic non-personal audio only (R-07) -- the same
speech-shaped-but-not-real-speech recipe used in the original ad-hoc benchmarking, formalized
here rather than living in a throwaway script.
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

from speakeasy.config import load_settings, speakeasy_dir
from speakeasy.engine import create_engine
from speakeasy.engine.download import ensure_model

CLIP_DURATIONS_S = (3, 5, 8, 15)  # ADJ-SPK-024 c: adds the ~15s band beyond the original 3/5/8
N_RUNS = 5


def synth_clip(duration_s: float, seed: int = 42) -> np.ndarray:
    """Non-personal synthetic 'speech-shaped' signal: a few formant-like sine bands
    amplitude-modulated at a syllable-ish rate, plus light noise. Not real speech, but
    exercises the same audio-frame-count-driven compute path as real dictation audio of
    the same duration."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(duration_s * 16000)) / 16000
    formants = [300, 900, 2200]
    sig = sum(np.sin(2 * np.pi * f * t + rng.uniform(0, 2 * np.pi)) for f in formants)
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 3.5 * t)
    sig = sig * envelope
    sig += rng.normal(0, 0.05, size=sig.shape)
    sig = sig / np.max(np.abs(sig)) * 0.6
    return sig.astype(np.float32)


def bench_engine(engine, durations=CLIP_DURATIONS_S, n_runs: int = N_RUNS,
                  clock=time.perf_counter) -> tuple[dict, dict]:
    """Runs `n_runs` timed transcriptions per clip-length band (after one discarded
    warmup run), returns ({band_key: p50_ms}, {band_key: p95_ms})."""
    p50, p95 = {}, {}
    for dur in durations:
        clip = synth_clip(dur)
        engine.transcribe(clip)  # warmup, discarded (model/session warmup cost)
        latencies = []
        for _ in range(n_runs):
            start = clock()
            engine.transcribe(clip)
            latencies.append((clock() - start) * 1000.0)
        key = f"{dur}s_clip"
        p50[key] = float(np.percentile(latencies, 50))
        p95[key] = float(np.percentile(latencies, 95))
    return p50, p95


def try_gpu_bench(model_dir: Path, durations=CLIP_DURATIONS_S,
                   n_runs: int = N_RUNS) -> tuple[bool, dict, dict]:
    """Best-effort CUDA comparison for the Parakeet model, bypassing ParakeetEngine
    (which has no provider option by design -- production dictation is CPU-only, see
    README's "no GPU required"). Returns (available, p50_ms, p95_ms); available=False
    on any failure (no GPU, no CUDA-enabled sherpa-onnx build, etc.) -- never raises."""
    try:
        import sherpa_onnx
        rec = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(next(model_dir.glob("encoder*.onnx"))),
            decoder=str(next(model_dir.glob("decoder*.onnx"))),
            joiner=str(next(model_dir.glob("joiner*.onnx"))),
            tokens=str(model_dir / "tokens.txt"),
            num_threads=4, model_type="nemo_transducer", provider="cuda",
        )
    except Exception:
        return False, {}, {}

    class _RawEngine:
        def transcribe(self, samples, sample_rate=16000):
            s = rec.create_stream()
            s.accept_waveform(sample_rate, samples)
            rec.decode_stream(s)
            return s.result.text

    p50, p95 = bench_engine(_RawEngine(), durations, n_runs)
    return True, p50, p95


def resolve_tailscale_ip(run=subprocess.run) -> str | None:
    try:
        result = run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5)
        ip = result.stdout.strip()
        return ip or None
    except Exception:
        return None


def write_record_atomically(record: dict, path: Path) -> None:
    """Writes via a temp file + os.replace (atomic on POSIX) so a crash or a
    concurrent read mid-write can never observe a half-written record."""
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".host_settings.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(record, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def build_record(engine_name: str, p50: dict, p95: dict,
                  gpu_available: bool, gpu_p50: dict, gpu_p95: dict,
                  host_id: str | None = None,
                  tailscale_ip: str | None = None,
                  updated: str | None = None) -> dict:
    gpu_slower = bool(
        gpu_available and gpu_p50 and p50
        and all(gpu_p50[k] > p50[k] for k in p50 if k in gpu_p50)
    )
    return {
        "host_id": host_id or socket.gethostname(),
        "tailscale_ip": tailscale_ip,
        "updated": updated or time.strftime("%Y-%m-%d"),
        "engine": {
            "name": engine_name,
            "provider": "cpu",
            "measured_p50_ms": p50,
            "measured_p95_ms": p95,
            "gpu_available": gpu_available,
            "gpu_measured_p50_ms": gpu_p50,
            "gpu_measured_p95_ms": gpu_p95,
            "gpu_provider_measured_slower": gpu_slower,
        },
    }


def main(argv=None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(description="speakeasy per-host STT benchmark (ADJ-SPK-024)")
    parser.add_argument("--force", action="store_true",
                         help="overwrite an existing host_settings.json")
    parser.add_argument("--engine", default=None,
                         help="engine name to benchmark (default: settings.engine)")
    args = parser.parse_args(argv)

    out_path = speakeasy_dir() / "host_settings.json"
    if out_path.exists() and not args.force:
        # ADJ-SPK-024 d: never silently clobber a stale-but-real record.
        print(f"speakeasy-bench: {out_path} already exists -- pass --force to overwrite.",
              file=sys.stderr)
        raise SystemExit(1)

    settings = load_settings()
    engine_name = args.engine or settings.engine
    models_dir = speakeasy_dir() / "models"
    engine = create_engine(engine_name, models_dir)

    print(f"speakeasy-bench: benchmarking {engine_name!r} on {socket.gethostname()}...")
    p50, p95 = bench_engine(engine)
    for dur in CLIP_DURATIONS_S:
        key = f"{dur}s_clip"
        print(f"  {key}: p50={p50[key]:.1f}ms  p95={p95[key]:.1f}ms")

    gpu_available, gpu_p50, gpu_p95 = False, {}, {}
    if engine_name == "parakeet":
        model_dir = ensure_model(engine_name, models_dir)
        gpu_available, gpu_p50, gpu_p95 = try_gpu_bench(model_dir)
        if gpu_available:
            print("speakeasy-bench: GPU (cuda) comparison:")
            for dur in CLIP_DURATIONS_S:
                key = f"{dur}s_clip"
                print(f"  {key}: p50={gpu_p50[key]:.1f}ms  p95={gpu_p95[key]:.1f}ms")
        else:
            print("speakeasy-bench: no CUDA-enabled path available, skipping GPU comparison.")

    record = build_record(engine_name, p50, p95, gpu_available, gpu_p50, gpu_p95,
                           tailscale_ip=resolve_tailscale_ip())
    write_record_atomically(record, out_path)
    print(f"speakeasy-bench: wrote {out_path}")


if __name__ == "__main__":
    main()
