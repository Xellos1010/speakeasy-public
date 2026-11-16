"""Tests for the T7 benchmark harness (jiwer WER/CER + latency + gate).

Governing spec: docs/superpowers/specs/2026-07-18-voice-training-environment.md
- §6 (benchmark harness + regression gate -- the 4 gate conditions).
- §8 acceptance criteria 4, 5, 6.
- §9 T7 row.

All engine interaction goes through a scripted stub (`ScriptedEngine` below)
so these are pure unit tests: no model download, no real audio decode, no
real wall-clock sleeping (latency is controlled via an injected fake
`clock` callable so percentile math is exactly reproducible).
"""

from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np
import pytest

from speakeasy.training.benchmark import (
    BenchmarkResult,
    GateVerdict,
    gate,
    normalize_for_scoring,
    run_and_activate,
    run_benchmark,
)
from speakeasy.training.profile import Correction, Decoding, Profile, ProfileStore, Provenance
from speakeasy.training.session import Manifest, ManifestPrompt

SAMPLE_RATE = 16000


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def write_marker_wav(path: Path, marker: int, seconds: float = 0.2) -> None:
    """Writes a wav whose first PCM16 sample encodes `marker` -- lets
    `ScriptedEngine` identify which clip it was handed regardless of call
    order (transcribe() only receives the raw samples, not a clip id)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(seconds * SAMPLE_RATE)
    samples = np.zeros(n, dtype=np.int16)
    samples[0] = marker
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(samples.tobytes())


class ScriptedEngine:
    """Stub matching `speakeasy.engine.parakeet.ParakeetEngine.transcribe`'s
    interface: `transcribe(samples, sample_rate=16000) -> str`. Looks up the
    scripted transcript by the marker embedded in `samples[0]`, so the same
    clip always yields the same transcript regardless of whether it's being
    benchmarked as part of a baseline or a candidate run."""

    def __init__(self, transcripts_by_marker: dict):
        self._transcripts = transcripts_by_marker
        self.call_count = 0

    def transcribe(self, samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> str:
        self.call_count += 1
        # `_load_wav` divides raw PCM16 by 32768.0, so recover the original
        # marker by multiplying back before rounding.
        marker = int(round(float(samples[0]) * 32768.0))
        return self._transcripts[marker]


def make_clock(deltas_ms: list):
    """Deterministic fake clock: `run_benchmark` calls `clock()` once before
    and once after each `engine.transcribe()` call. Given the intended
    per-clip latency (in ms) in clip order, returns a callable that replays
    exactly those elapsed times with no dependency on real wall-clock time."""
    seq = []
    t = 0.0
    for d in deltas_ms:
        seq.append(t)
        t += d / 1000.0
        seq.append(t)
    it = iter(seq)
    return lambda: next(it)


def make_session(
    tmp_path: Path,
    clips: list,
    session_id: str = "2026-07-18T14-02-11",
    dirname: str = "sess",
) -> Path:
    """Builds a session_dir/manifest.json + session_dir/clips/*.wav fixture
    matching the real `TrainingSession` on-disk layout. `clips` is a list of
    (prompt_id, marker, reference_text, split, status) tuples."""
    session_dir = tmp_path / dirname
    prompts = []
    for prompt_id, marker, text, split, status in clips:
        clip_rel = f"clips/{prompt_id}.wav"
        write_marker_wav(session_dir / clip_rel, marker)
        prompts.append(
            ManifestPrompt(
                prompt_id=prompt_id,
                source="harvard",
                text=text,
                split=split,
                clip_path=clip_rel,
                status=status,
            )
        )
    manifest = Manifest(
        session_id=session_id,
        created_ts=1799000000.0,
        status="complete",
        speakeasy_version="0.0.0-test",
        prompts=prompts,
    )
    manifest_path = session_dir / "manifest.json"
    manifest.save(manifest_path)
    return manifest_path


def make_profile(corrections=None, hotwords_enabled: bool = False, version: int = 1) -> Profile:
    return Profile(
        version=version,
        built_ts=1799000500.0,
        built_from_session="2026-07-18T14-02-11",
        provenance=Provenance(n_train_clips=10, n_eval_clips=3, sherpa_onnx_version="1.13.4", engine="parakeet"),
        hotwords=[],
        corrections=corrections or [],
        decoding=Decoding(decoding_method="greedy_search", hotwords_enabled=hotwords_enabled),
        metrics=None,
    )


# ---------------------------------------------------------------------------
# normalize_for_scoring
# ---------------------------------------------------------------------------


def test_normalize_for_scoring_lowercases_and_strips_punctuation():
    assert normalize_for_scoring("The Quick, Brown Fox!") == "the quick brown fox"


def test_normalize_for_scoring_collapses_whitespace():
    assert normalize_for_scoring("hello   world\n") == "hello world"


def test_normalize_for_scoring_is_idempotent():
    s = "Parakeet's  hotword-guard: v2!!"
    once = normalize_for_scoring(s)
    twice = normalize_for_scoring(once)
    assert once == twice


# ---------------------------------------------------------------------------
# run_benchmark: WER/CER + latency over eval-split accepted clips only
# ---------------------------------------------------------------------------


def test_run_benchmark_scores_only_eval_split_accepted_clips(tmp_path):
    manifest_path = make_session(
        tmp_path,
        [
            ("p-eval-1", 1, "hello world", "eval", "accepted"),
            ("p-train-1", 2, "should not be scored", "train", "accepted"),
            ("p-eval-2", 3, "not accepted yet", "eval", "pending"),
        ],
    )
    engine = ScriptedEngine({1: "hello world"})
    result = run_benchmark(engine, [manifest_path])

    assert result.n_clips == 1
    assert engine.call_count == 1
    assert result.wer == 0.0
    assert result.cer == 0.0


def test_run_benchmark_computes_wer_against_normalized_reference(tmp_path):
    manifest_path = make_session(
        tmp_path,
        [("p1", 1, "The Quick, Brown Fox.", "eval", "accepted")],
    )
    # engine's raw transcript differs from reference only by casing/punctuation
    # -- normalization (documented on normalize_for_scoring) must treat these
    # as equal, i.e. WER == 0, not penalize case/punctuation mismatches.
    engine = ScriptedEngine({1: "the quick brown fox"})
    result = run_benchmark(engine, [manifest_path])
    assert result.wer == 0.0
    assert result.cer == 0.0


def test_run_benchmark_unions_multiple_manifests(tmp_path):
    m1 = make_session(tmp_path, [("p1", 1, "hello world", "eval", "accepted")], dirname="sess1")
    m2 = make_session(tmp_path, [("p2", 2, "goodnight moon", "eval", "accepted")], dirname="sess2")
    engine = ScriptedEngine({1: "hello world", 2: "goodnight moon"})
    result = run_benchmark(engine, [m1, m2])
    assert result.n_clips == 2
    assert result.wer == 0.0


def test_run_benchmark_applies_profile_corrections_before_scoring(tmp_path):
    manifest_path = make_session(
        tmp_path,
        [("p1", 1, "Parakeet is fast", "eval", "accepted")],
    )
    engine = ScriptedEngine({1: "pair a keet is fast"})
    profile = make_profile(corrections=[Correction(pattern="pair a keet", replacement="Parakeet", kind="literal")])

    baseline = run_benchmark(engine, [manifest_path], profile=None)
    candidate = run_benchmark(engine, [manifest_path], profile=profile)

    assert baseline.wer > 0.0
    assert candidate.wer == 0.0


def test_run_benchmark_measures_latency_percentiles_via_injected_clock(tmp_path):
    manifest_path = make_session(
        tmp_path,
        [
            ("p1", 1, "one", "eval", "accepted"),
            ("p2", 2, "two", "eval", "accepted"),
            ("p3", 3, "three", "eval", "accepted"),
            ("p4", 4, "four", "eval", "accepted"),
        ],
    )
    engine = ScriptedEngine({1: "one", 2: "two", 3: "three", 4: "four"})
    latencies_ms = [100.0, 200.0, 300.0, 400.0]
    clock = make_clock(latencies_ms)

    result = run_benchmark(engine, [manifest_path], clock=clock)

    expected_p50 = float(np.percentile(latencies_ms, 50))
    expected_p95 = float(np.percentile(latencies_ms, 95))
    assert result.p50_ms == pytest.approx(expected_p50)
    assert result.p95_ms == pytest.approx(expected_p95)


def test_run_benchmark_is_deterministic_for_identical_inputs(tmp_path):
    manifest_path = make_session(
        tmp_path,
        [
            ("p1", 1, "hello world", "eval", "accepted"),
            ("p2", 2, "goodnight moon", "eval", "accepted"),
        ],
    )
    engine = ScriptedEngine({1: "hello world", 2: "goodnight moom"})
    latencies_ms = [50.0, 75.0]

    r1 = run_benchmark(engine, [manifest_path], clock=make_clock(latencies_ms))
    r2 = run_benchmark(engine, [manifest_path], clock=make_clock(latencies_ms))

    assert r1.wer == r2.wer
    assert r1.cer == r2.cer
    assert r1.p50_ms == r2.p50_ms
    assert r1.p95_ms == r2.p95_ms
    assert r1.outputs == r2.outputs


# ---------------------------------------------------------------------------
# gate(): the 4 conditions, spec §6
# ---------------------------------------------------------------------------


def test_gate_refuses_when_wer_not_strictly_improved_ac4():
    baseline = BenchmarkResult(wer=0.10, cer=0.05, p50_ms=200.0, p95_ms=300.0, n_clips=3, outputs=["a b", "c d", "e f"])
    candidate = BenchmarkResult(wer=0.10, cer=0.05, p50_ms=200.0, p95_ms=300.0, n_clips=3, outputs=["a b", "c d", "e f"])
    profile = make_profile()

    verdict = gate(baseline, candidate, profile)

    assert verdict.passed is False
    wer_cond = next(c for c in verdict.conditions if c.name == "wer_improvement")
    assert wer_cond.passed is False


def test_gate_activates_when_wer_improves_and_latency_in_budget_ac5():
    baseline = BenchmarkResult(wer=0.20, cer=0.10, p50_ms=200.0, p95_ms=300.0, n_clips=3, outputs=["a b", "c d", "e f"])
    candidate = BenchmarkResult(wer=0.05, cer=0.02, p50_ms=210.0, p95_ms=340.0, n_clips=3, outputs=["a b", "c d", "e f"])
    profile = make_profile()

    verdict = gate(baseline, candidate, profile)

    assert verdict.passed is True
    assert all(c.passed for c in verdict.conditions)


def test_gate_refuses_on_p50_budget_violation():
    baseline = BenchmarkResult(wer=0.20, cer=0.10, p50_ms=200.0, p95_ms=300.0, n_clips=1, outputs=["x"])
    # 220 > 200 * 1.10 == 220.0 exactly at boundary -> use 221 to force violation
    candidate = BenchmarkResult(wer=0.05, cer=0.02, p50_ms=221.0, p95_ms=300.0, n_clips=1, outputs=["x"])
    profile = make_profile()

    verdict = gate(baseline, candidate, profile)

    assert verdict.passed is False
    p50_cond = next(c for c in verdict.conditions if c.name == "p50_latency_budget")
    assert p50_cond.passed is False


def test_gate_refuses_on_p95_budget_violation():
    baseline = BenchmarkResult(wer=0.20, cer=0.10, p50_ms=200.0, p95_ms=300.0, n_clips=1, outputs=["x"])
    # 346 > 300 * 1.15 == 345.0 -> forces violation
    candidate = BenchmarkResult(wer=0.05, cer=0.02, p50_ms=200.0, p95_ms=346.0, n_clips=1, outputs=["x"])
    profile = make_profile()

    verdict = gate(baseline, candidate, profile)

    assert verdict.passed is False
    p95_cond = next(c for c in verdict.conditions if c.name == "p95_latency_budget")
    assert p95_cond.passed is False


def test_gate_p50_boundary_at_exactly_110_percent_passes():
    baseline = BenchmarkResult(wer=0.20, cer=0.10, p50_ms=200.0, p95_ms=300.0, n_clips=1, outputs=["x"])
    candidate = BenchmarkResult(wer=0.05, cer=0.02, p50_ms=220.0, p95_ms=300.0, n_clips=1, outputs=["x"])
    profile = make_profile()
    verdict = gate(baseline, candidate, profile)
    p50_cond = next(c for c in verdict.conditions if c.name == "p50_latency_budget")
    assert p50_cond.passed is True


def test_gate_downgrades_hotwords_on_empty_output_ac6():
    baseline = BenchmarkResult(wer=0.20, cer=0.10, p50_ms=200.0, p95_ms=300.0, n_clips=2, outputs=["a b", "c d"])
    candidate = BenchmarkResult(wer=0.05, cer=0.02, p50_ms=200.0, p95_ms=300.0, n_clips=2, outputs=["a b", ""])
    profile = make_profile(hotwords_enabled=True)

    verdict = gate(baseline, candidate, profile)

    assert verdict.hotwords_enabled is False
    hotwords_cond = next(c for c in verdict.conditions if c.name == "hotwords_safety")
    assert hotwords_cond.passed is False
    # downgrade, not rejection (AC6): the other 3 conditions still pass so
    # the overall gate still activates.
    assert verdict.passed is True


def test_gate_downgrades_hotwords_on_single_word_output():
    baseline = BenchmarkResult(wer=0.20, cer=0.10, p50_ms=200.0, p95_ms=300.0, n_clips=1, outputs=["a b c"])
    candidate = BenchmarkResult(wer=0.05, cer=0.02, p50_ms=200.0, p95_ms=300.0, n_clips=1, outputs=["oops"])
    profile = make_profile(hotwords_enabled=True)

    verdict = gate(baseline, candidate, profile)

    assert verdict.hotwords_enabled is False


def test_gate_leaves_hotwords_untouched_when_no_violation():
    baseline = BenchmarkResult(wer=0.20, cer=0.10, p50_ms=200.0, p95_ms=300.0, n_clips=1, outputs=["a b c"])
    candidate = BenchmarkResult(wer=0.05, cer=0.02, p50_ms=200.0, p95_ms=300.0, n_clips=1, outputs=["a b fixed"])
    profile = make_profile(hotwords_enabled=True)

    verdict = gate(baseline, candidate, profile)

    assert verdict.hotwords_enabled is True
    hotwords_cond = next(c for c in verdict.conditions if c.name == "hotwords_safety")
    assert hotwords_cond.passed is True


def test_gate_hotwords_condition_trivially_passes_when_disabled():
    baseline = BenchmarkResult(wer=0.20, cer=0.10, p50_ms=200.0, p95_ms=300.0, n_clips=1, outputs=["a b c"])
    candidate = BenchmarkResult(wer=0.05, cer=0.02, p50_ms=200.0, p95_ms=300.0, n_clips=1, outputs=[""])
    profile = make_profile(hotwords_enabled=False)

    verdict = gate(baseline, candidate, profile)

    assert verdict.hotwords_enabled is False
    hotwords_cond = next(c for c in verdict.conditions if c.name == "hotwords_safety")
    assert hotwords_cond.passed is True
    assert verdict.passed is True


# ---------------------------------------------------------------------------
# run_and_activate(): full pipeline (benchmark x2 -> gate -> write -> report)
# ---------------------------------------------------------------------------


def test_run_and_activate_activates_on_pass_and_bumps_active_version(tmp_path):
    manifest_path = make_session(
        tmp_path,
        [
            ("p1", 1, "Parakeet is fast", "eval", "accepted"),
            ("p2", 2, "hello world", "eval", "accepted"),
        ],
    )
    engine = ScriptedEngine({1: "pair a keet is fast", 2: "hello world"})
    store = ProfileStore(tmp_path / "profile")
    candidate = make_profile(
        corrections=[Correction(pattern="pair a keet", replacement="Parakeet", kind="literal")],
        version=0,
    )
    evidence_dir = tmp_path / "evidence"

    # Deterministic clock: baseline (2 clips) + candidate (2 clips) = 4
    # transcribe timings. Without this, the latency gate compares real
    # microsecond-scale stub-engine noise and the test flakes.
    report_path = run_and_activate(engine, [manifest_path], store, candidate,
                                   evidence_dir=evidence_dir, clock=make_clock([10.0] * 4))

    assert store.active_version() == candidate.version
    written = store.read(candidate.version)
    assert written.metrics is not None
    assert written.metrics["baseline_wer"] > written.metrics["profile_wer"]
    assert report_path.exists()
    assert report_path.parent == evidence_dir


def test_run_and_activate_refuses_leaves_active_untouched_ac4(tmp_path):
    manifest_path = make_session(
        tmp_path,
        [("p1", 1, "hello world", "eval", "accepted")],
    )
    engine = ScriptedEngine({1: "hello world"})
    store = ProfileStore(tmp_path / "profile")
    # seed an existing active version so we can prove it's untouched
    seed = make_profile(version=0)
    store.write(seed)
    store.set_active(seed.version)

    noop_candidate = make_profile(corrections=[], version=0)  # no-op: same output as baseline
    evidence_dir = tmp_path / "evidence"

    run_and_activate(engine, [manifest_path], store, noop_candidate, evidence_dir=evidence_dir)

    assert store.active_version() == seed.version
    # gate-failed profile is still written for inspection (§6)
    assert store.read(noop_candidate.version) is not None


def test_run_and_activate_downgrades_hotwords_before_writing_ac6(tmp_path):
    manifest_path = make_session(
        tmp_path,
        [
            ("p1", 1, "Parakeet one", "eval", "accepted"),
            ("p2", 2, "Parakeet two", "eval", "accepted"),
            ("p3", 3, "hello world", "eval", "accepted"),
        ],
    )
    engine = ScriptedEngine(
        {
            1: "pair a keet one",
            2: "pair a keet two",
            3: "hello world",
        }
    )
    store = ProfileStore(tmp_path / "profile")
    candidate = make_profile(
        corrections=[
            Correction(pattern="pair a keet", replacement="Parakeet", kind="literal"),
            # simulates a hotwords hallucination artifact (§1.1 issue #3267):
            # the third clip's corrected output collapses to empty.
            Correction(pattern="hello world", replacement="", kind="literal"),
        ],
        hotwords_enabled=True,
        version=0,
    )
    evidence_dir = tmp_path / "evidence"

    # Deterministic clock (3 baseline + 3 candidate timings): see the
    # activation test above — real stub-engine timing noise flakes the gate.
    run_and_activate(engine, [manifest_path], store, candidate,
                     evidence_dir=evidence_dir, clock=make_clock([10.0] * 6))

    written = store.read(candidate.version)
    assert written.decoding.hotwords_enabled is False
    # still activated despite the downgrade -- downgrade, not rejection.
    assert store.active_version() == candidate.version


def test_run_and_activate_report_contains_required_fields(tmp_path):
    manifest_path = make_session(
        tmp_path,
        [("p1", 1, "hello world", "eval", "accepted")],
    )
    engine = ScriptedEngine({1: "hello world"})
    store = ProfileStore(tmp_path / "profile")
    candidate = make_profile(version=0)
    evidence_dir = tmp_path / "evidence"

    report_path = run_and_activate(engine, [manifest_path], store, candidate, evidence_dir=evidence_dir)
    text = report_path.read_text(encoding="utf-8")

    assert "WER" in text
    assert "CER" in text
    assert "p50" in text
    assert "p95" in text
    assert str(candidate.version) in text
    assert ("REFUSED" in text or "ACTIVATED" in text)


def test_run_and_activate_evidence_report_filename_matches_date_convention(tmp_path):
    from datetime import datetime

    manifest_path = make_session(tmp_path, [("p1", 1, "hi", "eval", "accepted")])
    engine = ScriptedEngine({1: "hi"})
    store = ProfileStore(tmp_path / "profile")
    candidate = make_profile(version=0)
    evidence_dir = tmp_path / "evidence"
    fixed_now = datetime(2026, 7, 18, 12, 0, 0)

    report_path = run_and_activate(
        engine, [manifest_path], store, candidate, evidence_dir=evidence_dir, now=fixed_now
    )

    assert report_path.name == "training-report-2026-07-18.md"
