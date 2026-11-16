"""Benchmark harness: WER/CER + latency scoring and the regression gate
(TSK-SPK-TRAIN-T7).

Governing spec: docs/superpowers/specs/2026-07-18-voice-training-environment.md
- §6 (benchmark harness + regression gate -- the 4 gate conditions matched
  verbatim in `gate()` below).
- §8 acceptance criteria 4, 5, 6 (a no-op profile must not activate; a
  correction that measurably helps must activate; a hotwords_enabled
  profile that hallucinates an empty/single-word output must be downgraded,
  not rejected, before being written as active).
- §9 T7 row: this is the integration point for T3 (session manifests), T5
  (profile artifact + `apply_corrections`), and T6 (hotwords-capable engine)
  -- it proves those outputs are actually safe to activate.

This module never imports a concrete engine class: `engine` is injected
throughout (constructor-injected in production, a scripted stub in unit
tests) so no model download or real audio hardware is required to exercise
any of this logic. The engine's `transcribe(samples, sample_rate=16000) ->
str` interface is matched to `speakeasy.engine.parakeet.ParakeetEngine`.
"""

from __future__ import annotations

import re
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Sequence, Union

import jiwer
import numpy as np

from speakeasy.training.profile import Correction, Profile, apply_corrections
from speakeasy.training.session import Manifest

PathLike = Union[str, Path]

# spec §6: the operator's explicit latency budgets.
P50_BUDGET_MULTIPLIER = 1.10
P95_BUDGET_MULTIPLIER = 1.15


# ---------------------------------------------------------------------------
# Scoring normalization
# ---------------------------------------------------------------------------


def normalize_for_scoring(text: str) -> str:
    """Normalizes `text` before WER/CER scoring so that case and punctuation
    differences (which are not real transcription errors for this harness's
    purposes) never contribute to the error count.

    Normalization applied, in order:
    1. Lowercase.
    2. Strip everything except word characters (``\\w``, i.e. letters,
       digits, underscore) and whitespace -- this removes punctuation like
       commas, periods, apostrophes, and exclamation marks.
    3. Collapse runs of whitespace to a single space and strip leading/
       trailing whitespace.

    Both the reference (prompt `text`) and the hypothesis (engine output,
    post-corrections if a profile is given) are put through this exact same
    function before being handed to `jiwer.wer`/`jiwer.cer`, so scoring is
    symmetric and never penalizes a candidate for spelling/punctuating
    differently than the prompt was originally written.
    """
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# Audio loading (matches tests/test_engine_integration.py::load_wav)
# ---------------------------------------------------------------------------


def _load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _eval_clips(manifests: Sequence[PathLike]) -> list:
    """Union, in manifest-then-prompt order, of `(abs_clip_path, prompt_text)`
    for every eval-split, `status == "accepted"` clip across `manifests`
    (each a path to a `manifest.json` file written by
    `speakeasy.training.session.TrainingSession`; the clip's session_dir is
    the manifest's parent directory, per that module's on-disk layout)."""
    clips = []
    for manifest_path in manifests:
        manifest_path = Path(manifest_path)
        manifest = Manifest.load(manifest_path)
        session_dir = manifest_path.parent
        for p in manifest.prompts:
            if p.split == "eval" and p.status == "accepted":
                clips.append((session_dir / p.clip_path, p.text))
    return clips


# ---------------------------------------------------------------------------
# BenchmarkResult + run_benchmark
# ---------------------------------------------------------------------------


@dataclass
class BenchmarkResult:
    wer: float
    cer: float
    p50_ms: float
    p95_ms: float
    n_clips: int
    outputs: list = field(default_factory=list)  # post-correction transcript per clip, clip order
    references: list = field(default_factory=list)  # normalized reference text per clip, clip order


def run_benchmark(
    engine,
    manifests: Sequence[PathLike],
    profile: Optional[Profile] = None,
    *,
    clock: Callable[[], float] = time.perf_counter,
) -> BenchmarkResult:
    """Transcribes every eval-split accepted clip across `manifests` with
    `engine`, applies `profile.corrections` (if `profile` is given) as a
    post-processing pass, and scores WER/CER (jiwer, both sides normalized
    via `normalize_for_scoring`) plus p50/p95 wall-clock transcribe latency.

    `clock` defaults to `time.perf_counter` but is injectable so unit tests
    can control latency deterministically without real sleeping -- it is
    called once immediately before and once immediately after each
    `engine.transcribe()` call, matching the measurement approach already
    used in `tests/test_engine_integration.py` (load wav -> `transcribe`).

    Determinism: identical `engine` behavior + identical `manifests` +
    identical `clock` sequence always yields an identical `BenchmarkResult`
    (no randomness anywhere in this function).
    """
    clips = _eval_clips(manifests)
    corrections: Sequence[Correction] = profile.corrections if profile is not None else []

    outputs = []
    references = []
    latencies_ms = []

    for wav_path, prompt_text in clips:
        samples = _load_wav(wav_path)
        start = clock()
        raw_text = engine.transcribe(samples, sample_rate=16000)
        end = clock()
        latencies_ms.append((end - start) * 1000.0)

        text = apply_corrections(raw_text, corrections) if corrections else raw_text
        outputs.append(text)
        references.append(prompt_text)

    if not clips:
        return BenchmarkResult(wer=0.0, cer=0.0, p50_ms=0.0, p95_ms=0.0, n_clips=0)

    normalized_refs = [normalize_for_scoring(r) for r in references]
    normalized_hyps = [normalize_for_scoring(o) for o in outputs]

    wer = float(jiwer.wer(normalized_refs, normalized_hyps))
    cer = float(jiwer.cer(normalized_refs, normalized_hyps))
    p50_ms = float(np.percentile(latencies_ms, 50))
    p95_ms = float(np.percentile(latencies_ms, 95))

    return BenchmarkResult(
        wer=wer,
        cer=cer,
        p50_ms=p50_ms,
        p95_ms=p95_ms,
        n_clips=len(clips),
        outputs=outputs,
        references=normalized_refs,
    )


# ---------------------------------------------------------------------------
# gate(): spec §6's 4 conditions, verbatim
# ---------------------------------------------------------------------------


@dataclass
class ConditionResult:
    name: str
    passed: bool
    detail: str


@dataclass
class GateVerdict:
    passed: bool  # overall activation verdict (conditions 1-3; see hotwords note below)
    hotwords_enabled: bool  # resolved decoding.hotwords_enabled to write into the profile
    conditions: list  # list[ConditionResult], all 4 conditions, in spec §6 order

    def failed_conditions(self) -> list:
        return [c for c in self.conditions if not c.passed]


def gate(baseline: BenchmarkResult, candidate: BenchmarkResult, profile: Profile) -> GateVerdict:
    """Spec §6's 4 gate conditions, verbatim:

    1. `profile_wer < baseline_wer` (strict improvement, not a tie).
    2. `profile_p50_ms <= baseline_p50_ms * 1.10`.
    3. `profile_p95_ms <= baseline_p95_ms * 1.15`.
    4. If `profile.decoding.hotwords_enabled`: require zero empty-string or
       single-word outputs on the eval split (guard against issue #3267,
       §1.1). A violation force-downgrades the *written* `hotwords_enabled`
       to `False` -- this is a downgrade, not a rejection (spec §8 AC6):
       it does not by itself flip the overall `passed` verdict, which is
       conditions 1-3 only. If `hotwords_enabled` is already `False`, this
       condition is trivially satisfied.

    Returns a `GateVerdict` carrying per-condition pass/fail + a
    human-readable reason for each, plus the resolved `hotwords_enabled`
    value the caller should write into the candidate profile before
    persisting it (whether or not the gate as a whole passes).
    """
    conditions = []

    wer_improved = candidate.wer < baseline.wer
    conditions.append(
        ConditionResult(
            name="wer_improvement",
            passed=wer_improved,
            detail=(
                f"candidate WER {candidate.wer:.4f} vs baseline WER {baseline.wer:.4f} "
                f"({'strict improvement' if wer_improved else 'not strictly improved'})"
            ),
        )
    )

    p50_budget = baseline.p50_ms * P50_BUDGET_MULTIPLIER
    p50_ok = candidate.p50_ms <= p50_budget
    conditions.append(
        ConditionResult(
            name="p50_latency_budget",
            passed=p50_ok,
            detail=(
                f"candidate p50 {candidate.p50_ms:.1f}ms vs budget {p50_budget:.1f}ms "
                f"(baseline {baseline.p50_ms:.1f}ms * {P50_BUDGET_MULTIPLIER})"
            ),
        )
    )

    p95_budget = baseline.p95_ms * P95_BUDGET_MULTIPLIER
    p95_ok = candidate.p95_ms <= p95_budget
    conditions.append(
        ConditionResult(
            name="p95_latency_budget",
            passed=p95_ok,
            detail=(
                f"candidate p95 {candidate.p95_ms:.1f}ms vs budget {p95_budget:.1f}ms "
                f"(baseline {baseline.p95_ms:.1f}ms * {P95_BUDGET_MULTIPLIER})"
            ),
        )
    )

    hotwords_enabled = profile.decoding.hotwords_enabled
    if hotwords_enabled:
        violations = sum(1 for o in candidate.outputs if len(o.strip().split()) <= 1)
        hotwords_ok = violations == 0
        if not hotwords_ok:
            hotwords_enabled = False
        conditions.append(
            ConditionResult(
                name="hotwords_safety",
                passed=hotwords_ok,
                detail=(
                    f"{violations} empty-string/single-word output(s) on the eval split "
                    "with hotwords_enabled=true"
                    + ("" if hotwords_ok else " -- downgraded hotwords_enabled to false")
                ),
            )
        )
    else:
        conditions.append(
            ConditionResult(
                name="hotwords_safety",
                passed=True,
                detail="decoding.hotwords_enabled=false; no hallucination check required",
            )
        )

    passed = wer_improved and p50_ok and p95_ok
    return GateVerdict(passed=passed, hotwords_enabled=hotwords_enabled, conditions=conditions)


# ---------------------------------------------------------------------------
# run_and_activate(): baseline vs candidate -> gate -> write -> report
# ---------------------------------------------------------------------------


def _write_report(
    evidence_dir: Path,
    candidate: Profile,
    baseline_result: BenchmarkResult,
    candidate_result: BenchmarkResult,
    verdict: GateVerdict,
    now: datetime,
) -> Path:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    report_path = evidence_dir / f"training-report-{now.strftime('%Y-%m-%d')}.md"

    lines = [
        f"# Training report — profile v{candidate.version}",
        "",
        f"- Session: {candidate.built_from_session}",
        f"- Profile version under test: {candidate.version}",
        f"- Verdict: {'ACTIVATED' if verdict.passed else 'REFUSED'}",
        "",
        "## Metrics",
        "",
        "| metric | baseline | candidate |",
        "| --- | --- | --- |",
        f"| WER | {baseline_result.wer:.4f} | {candidate_result.wer:.4f} |",
        f"| CER | {baseline_result.cer:.4f} | {candidate_result.cer:.4f} |",
        f"| p50 latency (ms) | {baseline_result.p50_ms:.1f} | {candidate_result.p50_ms:.1f} |",
        f"| p95 latency (ms) | {baseline_result.p95_ms:.1f} | {candidate_result.p95_ms:.1f} |",
        "",
        "## Gate conditions (spec §6)",
        "",
    ]
    for c in verdict.conditions:
        lines.append(f"- [{'PASS' if c.passed else 'FAIL'}] {c.name}: {c.detail}")

    failed = [c.name for c in verdict.failed_conditions()]
    lines.append("")
    lines.append(f"Failed conditions: {', '.join(failed) if failed else 'none'}")
    lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def run_and_activate(
    engine,
    manifests: Sequence[PathLike],
    store,
    candidate: Profile,
    *,
    clock: Callable[[], float] = time.perf_counter,
    evidence_dir: PathLike = "evidence",
    now: Optional[datetime] = None,
) -> Path:
    """Runs baseline (no profile) and candidate benchmarks over `manifests`,
    applies `gate()`, fills `candidate.metrics`, writes `candidate` to
    `store` unconditionally, activates it (`store.set_active`) only on gate
    pass, and writes an evidence report. Returns the report path.

    `store.write()` is always called (spec §6: "Gate failure -> profile is
    still written to profile-v<N>.json for inspection/debugging"), so a
    rejected or downgraded candidate is never lost, only kept off the
    active pointer.
    """
    baseline_result = run_benchmark(engine, manifests, profile=None, clock=clock)
    candidate_result = run_benchmark(engine, manifests, profile=candidate, clock=clock)

    verdict = gate(baseline_result, candidate_result, candidate)

    candidate.decoding.hotwords_enabled = verdict.hotwords_enabled
    candidate.metrics = {
        "baseline_wer": baseline_result.wer,
        "profile_wer": candidate_result.wer,
        "baseline_cer": baseline_result.cer,
        "profile_cer": candidate_result.cer,
        "baseline_p50_ms": baseline_result.p50_ms,
        "profile_p50_ms": candidate_result.p50_ms,
        "baseline_p95_ms": baseline_result.p95_ms,
        "profile_p95_ms": candidate_result.p95_ms,
    }

    store.write(candidate)

    if verdict.passed:
        store.set_active(candidate.version)

    report_path = _write_report(
        Path(evidence_dir), candidate, baseline_result, candidate_result, verdict, now or datetime.now()
    )
    return report_path
