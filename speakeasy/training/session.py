"""Resumable training-session controller (TSK-SPK-TRAIN-T3).

Governing spec: docs/superpowers/specs/2026-07-18-voice-training-environment.md
- §3 (full UX flow contract: prompt sets, per-clip QC gate, held-out split,
  resumability, session end).
- §4 (corpus layout + manifest.json schema).
- §8 acceptance criteria 1-3 (resumability, QC enforcement, split
  reproducibility).

This module is pure logic + filesystem: no UI, no rumps/Quartz imports.
T4 wires this controller into the menu-bar UI. It consumes T1's
`speakeasy/training/vocab_mining.py` (carrier_sentences/mine_history) and
T2's `speakeasy/training/qc.py` (QCResult) rather than reimplementing either.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import sqlite3
import time
import wave
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

import numpy as np

from speakeasy.training.qc import QCResult
from speakeasy.training.vocab_mining import Prompt, carrier_sentences, load_user_vocab, mine_history

SAMPLE_RATE = 16000
QUICK_SIZE = 20
FULL_SIZE = 80
TRAIN_SPLIT_PCT = 80  # ~80/20 train/eval, spec §3.5
MAX_ATTEMPTS = 3  # spec §3.4 retry contract

PathLike = Union[str, Path]


def _speakeasy_version() -> str:
    """Best-effort package version for the manifest's `speakeasy_version`
    field (spec §4: "<git sha or package version>"). Never raises."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("speakeasy")
        except PackageNotFoundError:
            return "unknown"
    except Exception:
        return "unknown"


def new_session_id(now: Optional[datetime] = None) -> str:
    """ISO8601 timestamp session id with dashes for time separators, e.g.
    `2026-07-18T14-02-11` -- matches spec §4's example verbatim."""
    now = now or datetime.now()
    return now.strftime("%Y-%m-%dT%H-%M-%S")


# ---------------------------------------------------------------------------
# Manifest data model (spec §4) -- JSON round-trip, atomic writes.
# ---------------------------------------------------------------------------


@dataclass
class ManifestPrompt:
    prompt_id: str
    source: str  # harvard | vocab | correction
    text: str
    split: str  # train | eval
    clip_path: str
    status: str = "pending"  # pending | accepted | low_quality
    qc: Optional[dict] = None
    attempts: int = 0
    recorded_ts: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ManifestPrompt":
        return cls(
            prompt_id=d["prompt_id"],
            source=d["source"],
            text=d["text"],
            split=d["split"],
            clip_path=d["clip_path"],
            status=d.get("status", "pending"),
            qc=d.get("qc"),
            attempts=d.get("attempts", 0),
            recorded_ts=d.get("recorded_ts"),
        )


@dataclass
class Manifest:
    session_id: str
    created_ts: float
    status: str  # in_progress | complete
    speakeasy_version: str
    prompts: list

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "created_ts": self.created_ts,
            "status": self.status,
            "speakeasy_version": self.speakeasy_version,
            "prompts": [p.to_dict() for p in self.prompts],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Manifest":
        return cls(
            session_id=d["session_id"],
            created_ts=d["created_ts"],
            status=d["status"],
            speakeasy_version=d.get("speakeasy_version", "unknown"),
            prompts=[ManifestPrompt.from_dict(p) for p in d.get("prompts", [])],
        )

    def save(self, path: PathLike) -> None:
        """Atomic write: serialize to a temp file in the same directory,
        fsync, then `os.replace` onto `path`. A crash mid-write leaves only
        the stray (unreferenced) temp file behind -- `path` itself always
        holds either the prior good manifest or the fully-written new one,
        never a partial/corrupt one."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".{path.name}.tmp-{os.getpid()}-{time.monotonic_ns()}"
        data = json.dumps(self.to_dict(), indent=2)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: PathLike) -> "Manifest":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


# ---------------------------------------------------------------------------
# Deterministic train/eval split (spec §3.5, §8 AC3)
# ---------------------------------------------------------------------------


def assign_split(prompt_id: str, session_id: str) -> str:
    """Stable ~80/20 train/eval assignment via sha256 of
    `f"{session_id}:{prompt_id}"` -- pure and reproducible (AC3): rerunning
    with the same inputs always yields the same result. Deliberately does
    NOT use Python's `hash()`, which is randomized per-process
    (PYTHONHASHSEED) and would break reproducibility across runs."""
    digest = hashlib.sha256(f"{session_id}:{prompt_id}".encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) % 100
    return "train" if bucket < TRAIN_SPLIT_PCT else "eval"


# ---------------------------------------------------------------------------
# Prompt-set construction (spec §3.2) -- seeded-random fixed interleave.
# ---------------------------------------------------------------------------


def _load_harvard_prompts(corpus_path: PathLike, count: int, rng: random.Random) -> list:
    if count <= 0:
        return []
    lines = [l for l in Path(corpus_path).read_text(encoding="utf-8").splitlines() if l.strip()]
    n = min(count, len(lines))
    idxs = rng.sample(range(len(lines)), k=n)
    return [Prompt(prompt_id=f"harvard-{i:04d}", source="harvard", text=lines[i]) for i in idxs]


def _load_vocab_prompts(
    history_db: Optional[PathLike], vocab_path: Optional[PathLike], count: int, rng: random.Random
) -> list:
    if count <= 0:
        return []
    terms: list = []
    if history_db is not None and Path(history_db).exists():
        terms.extend(mine_history(history_db))
    if vocab_path is not None:
        terms.extend(load_user_vocab(vocab_path))
    if not terms:
        return []
    n = min(count, len(terms))
    sampled = rng.sample(terms, k=n)
    return carrier_sentences(sampled)


def _load_correction_prompts(history_db: Optional[PathLike], count: int, rng: random.Random) -> list:
    """Correction-pair replay prompts (spec §3.2(c)): rows where `rewritten
    != raw` under `clean` mode are the closest available signal for "the
    operator corrected/rewrote this"; the *raw* text is replayed as the
    prompt so the new recording can be compared against both the old wrong
    transcript and the desired final form."""
    if count <= 0 or history_db is None or not Path(history_db).exists():
        return []
    conn = sqlite3.connect(str(history_db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT raw FROM dictations WHERE mode = 'clean' AND rewritten IS NOT NULL"
            " AND rewritten != raw ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    raws = [r["raw"] for r in rows]
    if not raws:
        return []
    n = min(count, len(raws))
    sampled_idxs = rng.sample(range(len(raws)), k=n)
    return [
        Prompt(prompt_id=f"correction-{i:04d}", source="correction", text=raws[idx])
        for i, idx in enumerate(sampled_idxs)
    ]


def build_prompt_set(
    size: int,
    corpus_path: PathLike,
    history_db: Optional[PathLike],
    vocab_path: Optional[PathLike],
    seed: int,
) -> list:
    """Deterministic, seeded-random fixed interleave of the 3 prompt sources
    (spec §3.2): (a) harvard sentences from `corpus_path`, (b) mined
    vocab/carrier sentences (`vocab_mining.mine_history` + user vocab list),
    (c) correction-pair replay prompts from `history_db`. Quick sessions use
    `size=20`, full sessions `size=80`.

    Determinism: given the same `seed` and the same corpus/history/vocab
    content, returns an identical list (same prompt ids, same order) --
    uses a private `random.Random(seed)` instance, never the global
    `random` module or Python's randomized `hash()`.

    Quota is split as evenly as possible across the three sources in a
    fixed order (harvard, vocab, correction) so even a 20-prompt quick
    session samples all three; if a source's pool is smaller than its
    quota (e.g. a fresh install with no history yet), the shortfall is
    backfilled from the harvard corpus (always available, vendored
    locally) so the requested `size` is still met whenever enough material
    exists overall.
    """
    rng = random.Random(seed)

    base = size // 3
    remainder = size - base * 3
    q_harvard, q_vocab, q_correction = (base + (1 if i < remainder else 0) for i in range(3))

    harvard = _load_harvard_prompts(corpus_path, q_harvard, rng)
    vocab = _load_vocab_prompts(history_db, vocab_path, q_vocab, rng)
    correction = _load_correction_prompts(history_db, q_correction, rng)

    shortfall = size - (len(harvard) + len(vocab) + len(correction))
    if shortfall > 0:
        topped_up = _load_harvard_prompts(corpus_path, q_harvard + shortfall, rng)
        if len(topped_up) > len(harvard):
            harvard = topped_up

    sources = [harvard, vocab, correction]
    interleaved: list = []
    idxs = [0, 0, 0]
    while len(interleaved) < size:
        progressed = False
        for s in range(3):
            if len(interleaved) >= size:
                break
            if idxs[s] < len(sources[s]):
                interleaved.append(sources[s][idxs[s]])
                idxs[s] += 1
                progressed = True
        if not progressed:
            break
    return interleaved


# ---------------------------------------------------------------------------
# TrainingSession controller (spec §3.1, §3.3-§3.7)
# ---------------------------------------------------------------------------


class TrainingSession:
    """Create-or-resume session controller. The manifest is the single
    source of truth for "what's done" (spec §3.6) -- no in-memory-only
    state; every mutation is saved atomically before returning."""

    def __init__(self, manifest: Manifest, session_dir: Path):
        self.manifest = manifest
        self.session_dir = Path(session_dir)

    # -- construction ------------------------------------------------------

    @classmethod
    def create(
        cls,
        sessions_dir: PathLike,
        *,
        size: int = QUICK_SIZE,
        corpus_path: PathLike,
        history_db: Optional[PathLike] = None,
        vocab_path: Optional[PathLike] = None,
        seed: int = 0,
        session_id: Optional[str] = None,
        now_ts: Optional[float] = None,
    ) -> "TrainingSession":
        """Create a brand-new session: builds the prompt set, assigns each
        prompt's train/eval split, and writes the initial manifest
        (status `in_progress`) immediately -- spec §3 step 1: "a session
        manifest is created immediately so a crash or quit mid-session
        loses at most the current prompt, not the whole session."
        """
        sessions_dir = Path(sessions_dir)
        session_id = session_id or new_session_id()
        now_ts = time.time() if now_ts is None else now_ts

        raw_prompts = build_prompt_set(size, corpus_path, history_db, vocab_path, seed)
        prompts = [
            ManifestPrompt(
                prompt_id=p.prompt_id,
                source=p.source,
                text=p.text,
                split=assign_split(p.prompt_id, session_id),
                clip_path=f"clips/{p.prompt_id}.wav",
            )
            for p in raw_prompts
        ]

        manifest = Manifest(
            session_id=session_id,
            created_ts=now_ts,
            status="in_progress",
            speakeasy_version=_speakeasy_version(),
            prompts=prompts,
        )

        session_dir = sessions_dir / session_id
        (session_dir / "clips").mkdir(parents=True, exist_ok=True)
        session = cls(manifest, session_dir)
        session._save()
        return session

    @classmethod
    def resume(cls, sessions_dir: PathLike) -> Optional["TrainingSession"]:
        """Resume the most recent `in_progress` session under
        `sessions_dir`, reading the manifest fresh from disk (no reliance
        on any prior in-memory controller instance). Returns `None` if no
        incomplete session exists."""
        sessions_dir = Path(sessions_dir)
        if not sessions_dir.exists():
            return None
        candidates = []
        for d in sorted(p for p in sessions_dir.iterdir() if p.is_dir()):
            manifest_path = d / "manifest.json"
            if not manifest_path.exists():
                continue
            manifest = Manifest.load(manifest_path)
            if manifest.status == "in_progress":
                candidates.append((d, manifest))
        if not candidates:
            return None
        # Session dirs are named by ISO8601 session_id, so lexicographic
        # sort order (from the `sorted()` above) is chronological order;
        # the last candidate is the most recent.
        session_dir, manifest = candidates[-1]
        return cls(manifest, session_dir)

    @classmethod
    def create_or_resume(cls, sessions_dir: PathLike, **create_kwargs) -> "TrainingSession":
        """Resume an incomplete session if one exists, otherwise create a
        new one -- the entry point matching spec §3 step 1 / §3.6."""
        existing = cls.resume(sessions_dir)
        if existing is not None:
            return existing
        return cls.create(sessions_dir, **create_kwargs)

    # -- paths ---------------------------------------------------------------

    @property
    def manifest_path(self) -> Path:
        return self.session_dir / "manifest.json"

    def _save(self) -> None:
        self.manifest.save(self.manifest_path)

    # -- prompt sequencing (spec §3.6) ---------------------------------------

    def current_prompt(self) -> Optional[ManifestPrompt]:
        """First prompt not yet `accepted` -- includes both `pending` and
        `low_quality` prompts, so a resumed session offers a previously
        flagged `low_quality` prompt for retry, per spec §3.6, while never
        re-recording an already-`accepted` clip (AC1)."""
        for p in self.manifest.prompts:
            if p.status != "accepted":
                return p
        return None

    def _find(self, prompt_id: str) -> ManifestPrompt:
        for p in self.manifest.prompts:
            if p.prompt_id == prompt_id:
                return p
        raise KeyError(f"unknown prompt_id: {prompt_id!r}")

    # -- recording (spec §3.3-§3.4) -------------------------------------------

    def record_result(self, prompt_id: str, samples: np.ndarray, qc_result: QCResult) -> str:
        """Record one recording attempt for `prompt_id`. Consumes the
        already-computed T2 `QCResult` (does not recompute QC). On QC pass:
        status -> `accepted`, clip written to disk. On QC fail: status stays
        `pending` (retry) until `MAX_ATTEMPTS` (3, spec §3.4) is reached,
        then `low_quality`. Returns the resulting status string."""
        prompt = self._find(prompt_id)
        prompt.attempts += 1
        prompt.recorded_ts = time.time()
        prompt.qc = {
            "clipping": qc_result.clipping,
            "silence_ratio": qc_result.silence_ratio,
            "rms_dbfs": qc_result.rms_dbfs,
            "snr_db": qc_result.snr_db,
        }

        if qc_result.passed:
            prompt.status = "accepted"
            self._write_clip(prompt.clip_path, samples)
        elif prompt.attempts >= MAX_ATTEMPTS:
            prompt.status = "low_quality"
        else:
            prompt.status = "pending"

        self._save()
        return prompt.status

    def _write_clip(self, relative_clip_path: str, samples: np.ndarray) -> None:
        """16 kHz mono int16 PCM wav, matching `tests/assets/sample.wav`
        convention (see `tests/test_engine_integration.py::load_wav`)."""
        path = self.session_dir / relative_clip_path
        path.parent.mkdir(parents=True, exist_ok=True)
        samples = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
        pcm16 = (samples * 32767.0).astype(np.int16)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm16.tobytes())

    # -- completion (spec §3.7) -----------------------------------------------

    def summary(self) -> dict:
        n_accepted = sum(1 for p in self.manifest.prompts if p.status == "accepted")
        n_rejected = sum(1 for p in self.manifest.prompts if p.status == "low_quality")
        total_seconds = 0.0
        for p in self.manifest.prompts:
            if p.status != "accepted":
                continue
            clip_path = self.session_dir / p.clip_path
            if not clip_path.exists():
                continue
            with wave.open(str(clip_path), "rb") as w:
                total_seconds += w.getnframes() / float(w.getframerate())
        return {
            "n_clips": n_accepted,
            "n_rejected": n_rejected,
            "est_minutes": round(total_seconds / 60.0, 2),
        }

    def finish(self) -> dict:
        """Mark the session `complete` and return the end-of-session
        summary (spec §3 step 7: "# clips, # rejected, est. minutes")."""
        self.manifest.status = "complete"
        self._save()
        return self.summary()
