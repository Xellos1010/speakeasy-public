"""Vocabulary mining for the voice-training environment (TSK-SPK-TRAIN-T1).

Implements prompt-set (b) from
docs/superpowers/specs/2026-07-18-voice-training-environment.md §3:
personal/domain vocabulary mined from `history.db` (proper nouns / capitalized
multi-word terms in `raw`, and terms present in `rewritten` but not `raw`),
plus a user-supplied term list, wrapped into carrier sentences.

Open question 2 (§10): `rewritten != raw` is a noisy proxy for "the model got
this wrong" — `clean` mode also fixes casing/punctuation/whitespace and filler
words unrelated to STT accuracy. `mine_history` applies a heuristic to split
rewritten-only tokens into `kind="vocab"` (likely a real STT miss — a genuine
new word/term) vs `kind="normalization"` (a punctuation/casing variant of an
existing token, or a filler-word/contraction expansion such as
"gonna" -> "going to") so downstream consumers (T5's profile builder) can
avoid mining false corrections as hotwords.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence, Union

# --- data model --------------------------------------------------------


@dataclass(frozen=True)
class VocabTerm:
    term: str
    kind: str  # "vocab" | "normalization"
    source: str  # "proper_noun" | "rewritten_diff"
    count: int = 1


@dataclass(frozen=True)
class Prompt:
    prompt_id: str
    source: str  # "vocab"
    text: str


CARRIER_TEMPLATES = (
    "The word is {term}.",
    "Please schedule a call with {term}.",
)

# Common contraction/filler expansions a "clean" rewrite performs that are not
# a real vocabulary signal -- e.g. raw "gonna" -> rewritten "going to".
_FILLER_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "gonna": ("going", "to"),
    "wanna": ("want", "to"),
    "gotta": ("got", "to"),
    "kinda": ("kind", "of"),
    "sorta": ("sort", "of"),
    "lemme": ("let", "me"),
    "gimme": ("give", "me"),
}

# Filler words that a "clean" rewrite removes; if one somehow still shows up
# as a rewritten-only token, treat it as normalization noise, not vocab.
_FILLER_WORDS = {"um", "uh", "umm", "uhh", "er", "erm", "ah"}

_WORD_RE = re.compile(r"[A-Za-z']+")
_CAP_RUN_RE = re.compile(r"\b[A-Z][A-Za-z']*(?:\s+[A-Z][A-Za-z']*)*\b")


def _strip_punct(token: str) -> str:
    return re.sub(r"[^a-z0-9]", "", token.lower())


def _tokenize(text: str) -> list[str]:
    return [m.group(0) for m in _WORD_RE.finditer(text)]


def _proper_nouns(raw: str) -> list[str]:
    """Extract capitalized multi-word runs (kept regardless of position), plus
    single capitalized words that are NOT the first token of the text (a
    cheap guard against ordinary sentence-initial capitalization)."""
    terms: list[str] = []
    for m in _CAP_RUN_RE.finditer(raw):
        candidate = m.group(0)
        is_multiword = " " in candidate
        if is_multiword or m.start() != 0:
            terms.append(candidate)
    return terms


def _classify_diff_token(
    token: str, raw_tokens_stripped: set[str], raw_lower_tokens: set[str]
) -> str:
    """Return "normalization" if `token` (present in rewritten, absent from
    raw) is explainable as a pure casing/punctuation variant of a raw token,
    or a filler/contraction expansion -- else "vocab"."""
    if _strip_punct(token) in raw_tokens_stripped:
        return "normalization"
    for filler, expansion in _FILLER_EXPANSIONS.items():
        if filler in raw_lower_tokens and token.lower() in expansion:
            return "normalization"
    if token.lower() in _FILLER_WORDS:
        return "normalization"
    return "vocab"


def mine_history(db_path: Union[str, Path]) -> list[VocabTerm]:
    """Mine speakeasy `history.db` for candidate vocabulary terms.

    Reads the `dictations` table (see speakeasy/history.py for schema) and
    extracts:
      (a) proper nouns / capitalized multi-word terms from `raw`.
      (b) terms present in `rewritten` but not `raw`, tagged `vocab` unless
          the noise heuristic (open question 2) identifies the diff as pure
          casing/punctuation/whitespace or a filler-word/contraction
          normalization, in which case it's tagged `normalization`.

    Returns a deduplicated (case-insensitive on term+kind), deterministically
    ordered list -- sorted by (kind, term.lower()).
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT raw, rewritten FROM dictations ORDER BY id").fetchall()
    finally:
        conn.close()

    counts: dict[tuple[str, str], dict] = {}

    def _add(term: str, kind: str, source: str) -> None:
        key = (term.lower(), kind)
        entry = counts.setdefault(
            key, {"term": term, "kind": kind, "source": source, "count": 0}
        )
        entry["count"] += 1

    for row in rows:
        raw = row["raw"] or ""
        rewritten = row["rewritten"]

        for term in _proper_nouns(raw):
            _add(term, "vocab", "proper_noun")

        if not rewritten or rewritten == raw:
            continue

        raw_tokens = _tokenize(raw)
        raw_lower_tokens = {t.lower() for t in raw_tokens}
        raw_tokens_stripped = {_strip_punct(t) for t in raw_tokens}

        seen_in_row: set[str] = set()
        for token in _tokenize(rewritten):
            lower = token.lower()
            if lower in raw_lower_tokens or lower in seen_in_row:
                continue
            seen_in_row.add(lower)
            kind = _classify_diff_token(token, raw_tokens_stripped, raw_lower_tokens)
            _add(token, kind, "rewritten_diff")

    terms = [VocabTerm(**v) for v in counts.values()]
    terms.sort(key=lambda t: (t.kind, t.term.lower()))
    return terms


def load_user_vocab(path: Union[str, Path]) -> list[str]:
    """Load user-supplied vocab terms from `~/.speakeasy/training/vocab.txt`
    convention: one term per line, blank lines and `#`-prefixed comment lines
    skipped. Missing file returns an empty list (the list is optional)."""
    p = Path(path)
    if not p.exists():
        return []
    terms = []
    for line in p.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        terms.append(stripped)
    return terms


def carrier_sentences(terms: Sequence[Union[str, VocabTerm]]) -> list[Prompt]:
    """Wrap `terms` (plain strings or VocabTerm instances) into short carrier
    sentences (spec §3(b)) so they're spoken in natural phonetic context
    rather than read as bare words. Deterministically cycles through
    CARRIER_TEMPLATES and assigns sequential, zero-padded prompt ids."""
    prompts: list[Prompt] = []
    for i, term in enumerate(terms):
        text_term = term.term if isinstance(term, VocabTerm) else term
        template = CARRIER_TEMPLATES[i % len(CARRIER_TEMPLATES)]
        prompts.append(
            Prompt(
                prompt_id=f"vocab-{i:04d}",
                source="vocab",
                text=template.format(term=text_term),
            )
        )
    return prompts
