import re
import sqlite3
from pathlib import Path

import pytest

from speakeasy.training.vocab_mining import (
    Prompt,
    VocabTerm,
    carrier_sentences,
    load_user_vocab,
    mine_history,
)

CORPUS_PATH = (
    Path(__file__).resolve().parents[2]
    / "speakeasy"
    / "training"
    / "corpora"
    / "harvard_sentences.txt"
)


def make_history_db(tmp_path: Path, rows: list[tuple[str, str | None, str]]) -> Path:
    db_path = tmp_path / "history.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE dictations (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,"
        " raw TEXT NOT NULL, mode TEXT NOT NULL, target_app TEXT, rewritten TEXT,"
        " account TEXT, latency_ms INTEGER, status TEXT)"
    )
    for raw, rewritten, mode in rows:
        conn.execute(
            "INSERT INTO dictations (ts, raw, mode, rewritten) VALUES (0, ?, ?, ?)",
            (raw, mode, rewritten),
        )
    conn.commit()
    conn.close()
    return db_path


# --- corpus -----------------------------------------------------------------


def test_harvard_corpus_line_count_and_ascii():
    lines = CORPUS_PATH.read_text(encoding="ascii").splitlines()
    assert len(lines) == 720
    assert all(line.strip() for line in lines), "no blank lines"
    for line in lines:
        assert all(ord(c) < 128 for c in line), f"non-ascii in {line!r}"
        # numbering stripped: no leading "123." / "123)" style prefix
        assert not re.match(r"^\s*\d+[\.\)]", line), f"leading numbering in {line!r}"


def test_harvard_corpus_sentences_are_unique():
    lines = CORPUS_PATH.read_text(encoding="ascii").splitlines()
    assert len(set(lines)) == len(lines)


# --- mine_history: proper nouns (a) -----------------------------------------


def test_mine_history_extracts_multiword_proper_noun(tmp_path):
    db = make_history_db(
        tmp_path,
        [("open the Karina Legal Workspace please", None, "raw")],
    )
    terms = mine_history(db)
    assert any(
        t.term == "Karina Legal Workspace" and t.kind == "vocab" and t.source == "proper_noun"
        for t in terms
    )


def test_mine_history_extracts_single_capitalized_noun_not_sentence_initial(tmp_path):
    db = make_history_db(
        tmp_path,
        [("please call Evan tomorrow", None, "raw")],
    )
    terms = mine_history(db)
    assert any(t.term == "Evan" and t.kind == "vocab" for t in terms)


def test_mine_history_ignores_sentence_initial_capital(tmp_path):
    db = make_history_db(
        tmp_path,
        [("The meeting is scheduled", None, "raw")],
    )
    terms = mine_history(db)
    assert not any(t.term == "The" for t in terms)


# --- mine_history: rewritten-vs-raw diff (b) + noise heuristic (open Q2) ----


def test_mine_history_flags_new_word_in_rewritten_as_vocab(tmp_path):
    db = make_history_db(
        tmp_path,
        [("the pair a keet engine is slow", "the Parakeet engine is slow", "clean")],
    )
    terms = mine_history(db)
    vocab_terms = {t.term.lower() for t in terms if t.kind == "vocab" and t.source == "rewritten_diff"}
    assert "parakeet" in vocab_terms


def test_mine_history_tags_filler_expansion_as_normalization(tmp_path):
    db = make_history_db(
        tmp_path,
        [("I'm gonna call you", "I'm going to call you", "clean")],
    )
    terms = mine_history(db)
    normalization_terms = {t.term.lower() for t in terms if t.kind == "normalization"}
    assert "going" in normalization_terms
    assert "to" not in {t.term.lower() for t in terms if t.kind == "vocab"}


def test_mine_history_tags_punctuation_diff_as_normalization(tmp_path):
    db = make_history_db(
        tmp_path,
        [("dont forget the call", "don't forget the call", "clean")],
    )
    terms = mine_history(db)
    assert any(t.term.lower() == "don't" and t.kind == "normalization" for t in terms)
    assert not any(t.term.lower() == "don't" and t.kind == "vocab" for t in terms)


def test_mine_history_skips_identical_raw_rewritten(tmp_path):
    db = make_history_db(
        tmp_path,
        [("hello world", "hello world", "clean")],
    )
    terms = mine_history(db)
    assert terms == []


def test_mine_history_skips_null_rewritten(tmp_path):
    db = make_history_db(
        tmp_path,
        [("just some plain text", None, "raw")],
    )
    terms = mine_history(db)
    # no proper nouns, no rewritten diff -> empty
    assert terms == []


# --- determinism -------------------------------------------------------------


def test_mine_history_is_deterministic(tmp_path):
    db = make_history_db(
        tmp_path,
        [
            ("call Karina Legal Workspace", None, "raw"),
            ("the pair a keet is slow", "the Parakeet is slow", "clean"),
            ("I'm gonna go", "I'm going to go", "clean"),
        ],
    )
    first = mine_history(db)
    second = mine_history(db)
    assert first == second
    # sorted by (kind, term.lower())
    keys = [(t.kind, t.term.lower()) for t in first]
    assert keys == sorted(keys)


def test_mine_history_dedupes_repeated_terms(tmp_path):
    db = make_history_db(
        tmp_path,
        [
            ("call Karina Legal Workspace", None, "raw"),
            ("email Karina Legal Workspace now", None, "raw"),
        ],
    )
    terms = mine_history(db)
    matches = [t for t in terms if t.term == "Karina Legal Workspace"]
    assert len(matches) == 1
    assert matches[0].count == 2


# --- load_user_vocab ----------------------------------------------------------


def test_load_user_vocab_reads_terms_skips_comments_and_blanks(tmp_path):
    vocab_path = tmp_path / "vocab.txt"
    vocab_path.write_text(
        "# personal vocab\n\nsherpa onnx\n  Parakeet-TDT  \n# another comment\nAcme Corp\n"
    )
    terms = load_user_vocab(vocab_path)
    assert terms == ["sherpa onnx", "Parakeet-TDT", "Acme Corp"]


def test_load_user_vocab_missing_file_returns_empty(tmp_path):
    assert load_user_vocab(tmp_path / "does_not_exist.txt") == []


# --- carrier_sentences ---------------------------------------------------------


def test_carrier_sentences_wraps_terms_in_templates():
    prompts = carrier_sentences(["Parakeet", "sherpa onnx"])
    assert len(prompts) == 2
    assert all(isinstance(p, Prompt) for p in prompts)
    assert "Parakeet" in prompts[0].text
    assert "sherpa onnx" in prompts[1].text
    assert prompts[0].source == "vocab"


def test_carrier_sentences_deterministic_prompt_ids():
    prompts = carrier_sentences(["a", "b", "c"])
    assert [p.prompt_id for p in prompts] == ["vocab-0000", "vocab-0001", "vocab-0002"]


def test_carrier_sentences_accepts_vocab_terms():
    terms = [VocabTerm(term="Evan", kind="vocab", source="proper_noun", count=1)]
    prompts = carrier_sentences(terms)
    assert "Evan" in prompts[0].text
