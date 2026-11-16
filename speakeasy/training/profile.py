"""Voice profile artifact: schema, versioned store, builder, and the
`apply_corrections` post-processing function (TSK-SPK-TRAIN-T5).

Governing spec: docs/superpowers/specs/2026-07-18-voice-training-environment.md
- §5 (profile artifact schema -- matched exactly here, including the
  `active.json` pointer semantics and `decoding.hotwords_enabled` defaulting
  `false` at build time).
- §1.2 (personalization levers -- corrections are lever 1 "primary", hotwords
  are lever 3 "third", gated; this module builds the artifact for both but
  never flips `hotwords_enabled` -- that is exclusively T7's gate decision,
  per the note under the §5 JSON example and §6 condition 4).
- §9 T5 row (this task) and §10 open question 2 (correction-mining noise
  heuristic, addressed by `_is_noise_diff` / `min_occurrences` below).

This module deliberately does not reimplement `speakeasy/training/vocab_mining.py`'s
history-mining logic: hotword candidates and the filler/contraction expansion
table are imported and reused from there.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import sqlite3
import time
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional, Sequence, Union

from speakeasy.training.session import Manifest
from speakeasy.training.vocab_mining import _FILLER_EXPANSIONS, mine_history

PathLike = Union[str, Path]

DEFAULT_HOTWORD_SCORE = 2.0
DEFAULT_MIN_CORRECTION_OCCURRENCES = 2  # spec §10 open question 2

_WORD_RE = re.compile(r"[A-Za-z']+")


def _strip_punct_lower(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _sherpa_onnx_version() -> str:
    """Best-effort `sherpa-onnx` package version for `provenance.sherpa_onnx_version`
    (spec §5). Never raises -- returns "unknown" if the package metadata can't
    be read (e.g. not installed in the current environment)."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("sherpa-onnx")
        except PackageNotFoundError:
            return "unknown"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Data model (spec §5) -- JSON round-trip.
# ---------------------------------------------------------------------------


@dataclass
class Provenance:
    n_train_clips: int
    n_eval_clips: int
    sherpa_onnx_version: str
    engine: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Provenance":
        return cls(
            n_train_clips=d["n_train_clips"],
            n_eval_clips=d["n_eval_clips"],
            sherpa_onnx_version=d["sherpa_onnx_version"],
            engine=d["engine"],
        )


@dataclass
class Hotword:
    phrase: str
    score: float = DEFAULT_HOTWORD_SCORE

    def to_dict(self) -> dict:
        return {"phrase": self.phrase, "score": self.score}

    @classmethod
    def from_dict(cls, d: dict) -> "Hotword":
        return cls(phrase=d["phrase"], score=d.get("score", DEFAULT_HOTWORD_SCORE))


@dataclass
class Correction:
    pattern: str
    replacement: str
    kind: str = "literal"  # "literal" | "regex"
    case_sensitive: bool = False

    def to_dict(self) -> dict:
        return {
            "pattern": self.pattern,
            "replacement": self.replacement,
            "kind": self.kind,
            "case_sensitive": self.case_sensitive,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Correction":
        return cls(
            pattern=d["pattern"],
            replacement=d["replacement"],
            kind=d.get("kind", "literal"),
            case_sensitive=d.get("case_sensitive", False),
        )


@dataclass
class Decoding:
    decoding_method: str = "greedy_search"
    hotwords_enabled: bool = False  # spec §5 note: default false even if hotwords exist

    def to_dict(self) -> dict:
        return {"decoding_method": self.decoding_method, "hotwords_enabled": self.hotwords_enabled}

    @classmethod
    def from_dict(cls, d: dict) -> "Decoding":
        return cls(
            decoding_method=d.get("decoding_method", "greedy_search"),
            hotwords_enabled=d.get("hotwords_enabled", False),
        )


@dataclass
class Profile:
    """Mirrors the spec §5 JSON schema exactly. `version` is a placeholder
    (0) until `ProfileStore.write()` stamps the real, store-assigned value --
    the store, not the builder, owns versioning."""

    version: int
    built_ts: float
    built_from_session: str
    provenance: Provenance
    hotwords: list = field(default_factory=list)  # list[Hotword]
    corrections: list = field(default_factory=list)  # list[Correction]
    decoding: Decoding = field(default_factory=Decoding)
    metrics: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "built_ts": self.built_ts,
            "built_from_session": self.built_from_session,
            "provenance": self.provenance.to_dict(),
            "hotwords": [h.to_dict() for h in self.hotwords],
            "corrections": [c.to_dict() for c in self.corrections],
            "decoding": self.decoding.to_dict(),
            "metrics": self.metrics,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        return cls(
            version=d["version"],
            built_ts=d["built_ts"],
            built_from_session=d["built_from_session"],
            provenance=Provenance.from_dict(d["provenance"]),
            hotwords=[Hotword.from_dict(h) for h in d.get("hotwords", [])],
            corrections=[Correction.from_dict(c) for c in d.get("corrections", [])],
            decoding=Decoding.from_dict(d.get("decoding", {})),
            metrics=d.get("metrics"),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_json(cls, s: str) -> "Profile":
        return cls.from_dict(json.loads(s))


# ---------------------------------------------------------------------------
# ProfileStore -- versioned, append-only (spec §5, §8 AC7).
# ---------------------------------------------------------------------------


_PROFILE_RE = re.compile(r"^profile-v(\d+)\.json$")


class ProfileStore:
    """Versioned, append-only profile store rooted at `dir`
    (`~/.speakeasy/profile/` per spec §5). The store is otherwise immutable
    once a version is written -- `active.json` is the only mutable pointer,
    so a bad build never corrupts a good one and rollback is "flip the
    pointer back" (§8 AC7)."""

    def __init__(self, dir: PathLike):
        self.dir = Path(dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    # -- paths ---------------------------------------------------------------

    def _profile_path(self, version: int) -> Path:
        return self.dir / f"profile-v{version}.json"

    @property
    def _active_path(self) -> Path:
        return self.dir / "active.json"

    def _existing_versions(self) -> list:
        versions = []
        for p in self.dir.iterdir():
            m = _PROFILE_RE.match(p.name)
            if m:
                versions.append(int(m.group(1)))
        return sorted(versions)

    # -- atomic write helper ---------------------------------------------------

    def _atomic_write(self, path: Path, data: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".{path.name}.tmp-{os.getpid()}-{time.monotonic_ns()}"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    # -- versioning ------------------------------------------------------------

    def next_version(self) -> int:
        existing = self._existing_versions()
        return (existing[-1] + 1) if existing else 1

    def list_versions(self) -> list:
        """All written profile versions, oldest first. Public wrapper over
        `_existing_versions()` -- for consumers (e.g. the GUI's version
        history list) that need to enumerate every version, not just the
        active one."""
        return self._existing_versions()

    def write(self, profile: Profile, version: Optional[int] = None) -> Path:
        """Write `profile` as the next version (or an explicit `version`,
        primarily for tests exercising the never-overwrite guard). Mutates
        `profile.version` in place to the assigned version. Never overwrites
        an existing `profile-v<N>.json` -- raises `FileExistsError` instead.
        Atomic temp-file-then-rename write."""
        target_version = version if version is not None else self.next_version()
        path = self._profile_path(target_version)
        if path.exists():
            raise FileExistsError(
                f"profile version {target_version} already exists at {path}; "
                "the profile store is append-only and never overwrites a version"
            )
        profile.version = target_version
        self._atomic_write(path, profile.to_json())
        return path

    def read(self, version: int) -> Profile:
        path = self._profile_path(version)
        with open(path, "r", encoding="utf-8") as f:
            return Profile.from_dict(json.load(f))

    # -- active pointer (spec §5, §8 AC7) ---------------------------------------

    def active_version(self) -> Optional[int]:
        if not self._active_path.exists():
            return None
        with open(self._active_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("active_version")

    def set_active(self, version: int) -> None:
        """Flip the `active.json` pointer to `version`. This is the *only*
        state rollback touches (§8 AC7: `set_active(N-1)` with no other
        state) -- the profile files themselves are never rewritten."""
        path = self._profile_path(version)
        if not path.exists():
            raise FileNotFoundError(f"cannot activate profile version {version}: {path} does not exist")
        self._atomic_write(self._active_path, json.dumps({"active_version": version}, indent=2))

    def load_active(self) -> Optional[Profile]:
        version = self.active_version()
        if version is None:
            return None
        return self.read(version)


# ---------------------------------------------------------------------------
# Correction mining (spec §10 open question 2) -- noise heuristic.
# ---------------------------------------------------------------------------


def _word_diff_rules(raw: str, rewritten: str) -> list:
    """Word-level diff between `raw` and `rewritten`, returning
    `(pattern, replacement)` candidate pairs for each substituted span
    (`difflib.SequenceMatcher` 'replace' opcode). Pure insertions/deletions
    (filler words added/removed with no substitution) are not treated as
    correction rules here -- only substitutions are a "the model heard X,
    should have been Y" signal."""
    raw_tokens = _WORD_RE.findall(raw)
    rewritten_tokens = _WORD_RE.findall(rewritten)
    matcher = difflib.SequenceMatcher(a=raw_tokens, b=rewritten_tokens, autojunk=False)
    rules = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "replace":
            continue
        pattern = " ".join(raw_tokens[i1:i2])
        replacement = " ".join(rewritten_tokens[j1:j2])
        if pattern and replacement:
            rules.append((pattern, replacement))
    return rules


def _is_noise_diff(pattern: str, replacement: str) -> bool:
    """True if `pattern` -> `replacement` is a pure casing/punctuation diff
    (same word(s) once lowercased and stripped of punctuation) -- spec §10
    open question 2's noise heuristic target."""
    return _strip_punct_lower(pattern) == _strip_punct_lower(replacement)


def _run_history_query(history_db: PathLike, sql: str) -> list[sqlite3.Row]:
    """Execute a SELECT query against the history database and return all rows.

    Opens the connection, sets row_factory to sqlite3.Row, executes the query,
    fetches all rows, and closes the connection (even on error)."""
    conn = sqlite3.connect(str(history_db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql).fetchall()
    finally:
        conn.close()
    return rows


def _mine_correction_pairs(history_db: PathLike) -> list:
    """All `(raw, rewritten)` pairs from `history.db` matching the same
    signal `speakeasy/training/session.py::_load_correction_prompts` uses:
    `mode == 'clean' AND rewritten != raw`."""
    rows = _run_history_query(
        history_db,
        "SELECT raw, rewritten FROM dictations WHERE mode = 'clean' AND rewritten IS NOT NULL"
        " AND rewritten != raw ORDER BY id"
    )
    return [(r["raw"], r["rewritten"]) for r in rows]


def _filler_expansion_rules(history_db: PathLike) -> list:
    """Filler/contraction-expansion corrections (e.g. "gonna" -> "going to"),
    reusing `vocab_mining._FILLER_EXPANSIONS` rather than reimplementing the
    table. Only emitted for fillers that actually appear as a raw token
    somewhere in history -- not unconditionally for every known filler."""
    rows = _run_history_query(history_db, "SELECT raw FROM dictations ORDER BY id")
    raw_text = " ".join((r["raw"] or "") for r in rows).lower()
    raw_tokens = set(_WORD_RE.findall(raw_text.lower()))

    rules = []
    for filler, expansion in _FILLER_EXPANSIONS.items():
        if filler in raw_tokens:
            rules.append((filler, " ".join(expansion)))
    return rules


def _build_correction_rules(
    manifest: Manifest,
    history_db: Optional[PathLike],
    min_occurrences: int,
) -> list:
    """Corrections list (spec §5 `corrections`), derived from:
    (a) correction-pair prompts in the manifest (`source == 'correction'`,
        `status == 'accepted'`) matched back to their `history.db` row to
        recover the `rewritten` (desired) form, word-diffed against the
        replayed `raw` (prompt) text; and
    (b) `vocab_mining`'s filler/contraction-expansion table, reused (not
        reimplemented) against the full history.

    Applies the §10 open-question-2 noise heuristic: a candidate rule that
    is a pure casing/punctuation diff is dropped unless the identical
    (pattern, replacement) pair recurs at least `min_occurrences` times
    across the mined correction pairs. Returns a deterministically ordered,
    deduplicated list of `Correction`.
    """
    if history_db is None or not Path(history_db).exists():
        return []

    all_pairs = _mine_correction_pairs(history_db)
    by_raw: dict = {}
    for raw, rewritten in all_pairs:
        by_raw.setdefault(raw, rewritten)  # first (lowest id) wins, deterministic

    candidates: list = []  # (pattern, replacement) in discovery order

    # (a) manifest-scoped correction prompts, matched back to history.db.
    for p in manifest.prompts:
        if p.source != "correction" or p.status != "accepted":
            continue
        rewritten = by_raw.get(p.text)
        if rewritten is None:
            continue
        candidates.extend(_word_diff_rules(p.text, rewritten))

    # Also mine every correction pair in history (not just this session's),
    # so the noise-occurrence count reflects the operator's real repeat-error
    # rate rather than just what happened to be re-recorded this session.
    for raw, rewritten in all_pairs:
        candidates.extend(_word_diff_rules(raw, rewritten))

    # (b) filler/contraction-expansion rules (always real signal, not noise).
    filler_rules = _filler_expansion_rules(history_db)

    occurrence_counts: dict = {}
    for pattern, replacement in candidates:
        key = (_strip_punct_lower(pattern), _strip_punct_lower(replacement))
        occurrence_counts[key] = occurrence_counts.get(key, 0) + 1

    kept: dict = {}  # (pattern.lower(), replacement.lower()) -> Correction, first-seen order
    order: list = []

    def _consider(pattern: str, replacement: str) -> None:
        dedup_key = (pattern.lower(), replacement.lower())
        if dedup_key in kept:
            return
        if _is_noise_diff(pattern, replacement):
            count = occurrence_counts.get((_strip_punct_lower(pattern), _strip_punct_lower(replacement)), 0)
            if count < min_occurrences:
                return
        kept[dedup_key] = Correction(pattern=pattern, replacement=replacement, kind="literal", case_sensitive=False)
        order.append(dedup_key)

    for pattern, replacement in candidates:
        _consider(pattern, replacement)
    for pattern, replacement in filler_rules:
        _consider(pattern, replacement)

    order.sort(key=lambda k: k[0])
    return [kept[k] for k in order]


def _merge_with_prior(new_rules: list, prior_profile: Optional[Profile]) -> list:
    """Carry forward `prior_profile`'s correction rules unless contradicted
    (spec T5 requirement): a new rule "contradicts" a prior rule when they
    share the same `(pattern.lower(), kind)` but a different `replacement`
    -- the new rule wins in that case. Prior rules are otherwise kept
    verbatim (in prior order); brand-new, non-conflicting rules are
    appended, sorted deterministically by pattern."""
    if prior_profile is None:
        return new_rules

    new_by_key = {(c.pattern.lower(), c.kind): c for c in new_rules}

    merged: list = []
    seen_keys = set()
    for prior in prior_profile.corrections:
        key = (prior.pattern.lower(), prior.kind)
        seen_keys.add(key)
        merged.append(new_by_key.get(key, prior))

    for c in new_rules:
        key = (c.pattern.lower(), c.kind)
        if key not in seen_keys:
            merged.append(c)
            seen_keys.add(key)

    return merged


# ---------------------------------------------------------------------------
# Profile builder (spec §5, §9 T5).
# ---------------------------------------------------------------------------


def build_profile(
    session_manifest_path: PathLike,
    history_db: Optional[PathLike] = None,
    prior_profile: Optional[Profile] = None,
    *,
    engine: str = "parakeet",
    min_correction_occurrences: int = DEFAULT_MIN_CORRECTION_OCCURRENCES,
    now_ts: Optional[float] = None,
) -> Profile:
    """Build a new (unversioned -- see `Profile.version`) profile from a
    completed training session manifest plus `history.db`.

    - `hotwords`: vocab terms (`kind == 'vocab'`) from
      `vocab_mining.mine_history`, default score `DEFAULT_HOTWORD_SCORE`;
      multi-word proper-noun phrases are preserved as-is.
    - `corrections`: see `_build_correction_rules` (manifest correction-pair
      prompts + reused filler-expansion table, noise heuristic applied),
      merged with `prior_profile.corrections` per `_merge_with_prior`.
    - `decoding`: always `{"decoding_method": "greedy_search",
      "hotwords_enabled": false}` -- activation is exclusively T7's gate
      decision (spec §5 note, §6 condition 4).
    - `metrics`: left `None` at build time -- T7 fills them post-benchmark.
    - `provenance`: clip counts from the manifest's accepted train/eval
      split, `sherpa_onnx_version` via `importlib.metadata`, `engine` name.
    """
    manifest = Manifest.load(session_manifest_path)

    n_train = sum(1 for p in manifest.prompts if p.split == "train" and p.status == "accepted")
    n_eval = sum(1 for p in manifest.prompts if p.split == "eval" and p.status == "accepted")

    hotwords: list = []
    if history_db is not None and Path(history_db).exists():
        for term in mine_history(history_db):
            if term.kind == "vocab":
                hotwords.append(Hotword(phrase=term.term, score=DEFAULT_HOTWORD_SCORE))

    correction_rules = _build_correction_rules(manifest, history_db, min_correction_occurrences)
    corrections = _merge_with_prior(correction_rules, prior_profile)

    return Profile(
        version=0,
        built_ts=time.time() if now_ts is None else now_ts,
        built_from_session=manifest.session_id,
        provenance=Provenance(
            n_train_clips=n_train,
            n_eval_clips=n_eval,
            sherpa_onnx_version=_sherpa_onnx_version(),
            engine=engine,
        ),
        hotwords=hotwords,
        corrections=corrections,
        decoding=Decoding(decoding_method="greedy_search", hotwords_enabled=False),
        metrics=None,
    )


# ---------------------------------------------------------------------------
# apply_corrections -- what T8 wires into the runtime path.
# ---------------------------------------------------------------------------


def apply_corrections(text: str, corrections: Sequence[Union[Correction, dict]]) -> str:
    """Apply `corrections` to `text`, in order, as a post-processing pass
    (spec §5 note: "applied ... ordered as listed, before the existing
    mode/rewrite pipeline ... sees the text").

    - `kind == "literal"`: word-boundary-aware (`\\b...\\b`) substring
      match; the replacement is inserted verbatim (never treated as a regex
      backreference), so replacement text containing e.g. backslashes is
      always literal.
    - `kind == "regex"`: `pattern` is compiled and used directly with
      `re.sub`, so a `replacement` may use backreferences (`\\1`, etc.) if
      the rule author intends that.
    - Case-insensitive unless `case_sensitive=True` on the rule.
    - An invalid regex `pattern` in a stored profile is skipped (with a
      `warnings.warn`), never raises -- a malformed rule must never crash
      the runtime transcription path.
    """
    result = text
    for raw_rule in corrections:
        rule = raw_rule if isinstance(raw_rule, Correction) else Correction.from_dict(raw_rule)
        flags = 0 if rule.case_sensitive else re.IGNORECASE

        if rule.kind == "regex":
            try:
                compiled = re.compile(rule.pattern, flags)
            except re.error as exc:
                warnings.warn(
                    f"skipping invalid regex correction rule {rule.pattern!r}: {exc}",
                    RuntimeWarning,
                )
                continue
            result = compiled.sub(rule.replacement, result)
        else:  # "literal"
            try:
                compiled = re.compile(rf"\b{re.escape(rule.pattern)}\b", flags)
            except re.error as exc:
                warnings.warn(
                    f"skipping invalid literal correction rule {rule.pattern!r}: {exc}",
                    RuntimeWarning,
                )
                continue
            replacement = rule.replacement
            result = compiled.sub(lambda _m, _r=replacement: _r, result)

    return result


# ---------------------------------------------------------------------------
# apply_hotword_fuzzy_correction -- engine-agnostic hotword fallback
# (TSK-SPK-TRANSFER).
# ---------------------------------------------------------------------------

DEFAULT_HOTWORD_FUZZY_THRESHOLD = 90.0  # rapidfuzz.fuzz.ratio is 0-100


def apply_hotword_fuzzy_correction(
    text: str,
    hotwords: Sequence[Union["Hotword", dict]],
    threshold: float = DEFAULT_HOTWORD_FUZZY_THRESHOLD,
) -> str:
    """Best-effort, engine-agnostic approximation of the hotwords/contextual-
    biasing boost, for whenever native decode-time hotwords aren't in play.
    Two real reasons that's the common case, not an edge case:

    1. sherpa-onnx's hotwords mechanism is transducer-only (confirmed against
       the official docs) -- Whisper's encoder-decoder architecture cannot
       use it at all, full stop.
    2. Even on the Parakeet engine that DOES support it, the bundled
       ``sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8`` export lacks the
       ``bpe_vocab`` PR #3077's mechanism needs (see
       ``speakeasy.engine.parakeet.HotwordsUnavailable``'s docstring) --
       deriving one needs NeMo's original checkpoint + the nemo_toolkit
       dependency, out of scope. So native hotwords are dead on arrival
       for the model this app actually ships today, on either engine.

    This is a post-hoc TEXT correction (fuzzy-matching each hotword phrase
    against same-length windows of already-decoded words, replacing a
    close-enough match), not a decode-time acoustic boost -- it can only
    help when the engine transcribed a hotword as something that *sounds*
    close, not something unrelated. That's a real but strictly smaller win
    than true contextual biasing would be; it is however the only mechanism
    available that works identically regardless of which engine is active,
    which is what makes a trained profile's hotword list actually "transfer"
    across a model switch rather than silently doing nothing.

    A hotword already present verbatim (case-insensitive, word-boundary
    match) is left alone -- no wasted fuzzy search once it's already right.
    """
    from rapidfuzz import fuzz

    result = text
    for raw_hotword in hotwords:
        hotword = raw_hotword if isinstance(raw_hotword, Hotword) else Hotword.from_dict(raw_hotword)
        phrase = hotword.phrase.strip()
        phrase_len = len(phrase.split())
        if not phrase or phrase_len == 0:
            continue
        if re.search(rf"\b{re.escape(phrase)}\b", result, re.IGNORECASE):
            continue  # already correct -- nothing to fix

        words = result.split()
        best_idx, best_score = None, 0.0
        for i in range(len(words) - phrase_len + 1):
            window = " ".join(words[i:i + phrase_len])
            score = fuzz.ratio(window.lower(), phrase.lower())
            if score > best_score:
                best_score, best_idx = score, i
        if best_idx is not None and best_score >= threshold:
            words[best_idx:best_idx + phrase_len] = [phrase]
            result = " ".join(words)

    return result
