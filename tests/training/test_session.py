import json
import sqlite3
import wave
from pathlib import Path

import numpy as np
import pytest

from speakeasy.training.qc import QCResult
from speakeasy.training.session import (
    FULL_SIZE,
    QUICK_SIZE,
    Manifest,
    ManifestPrompt,
    TrainingSession,
    assign_split,
    build_prompt_set,
)

REAL_CORPUS_PATH = (
    Path(__file__).resolve().parents[2]
    / "speakeasy"
    / "training"
    / "corpora"
    / "harvard_sentences.txt"
)

SR = 16000


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_corpus(tmp_path: Path, n: int = 12) -> Path:
    p = tmp_path / "harvard.txt"
    p.write_text("\n".join(f"Sentence number {i}." for i in range(n)) + "\n", encoding="utf-8")
    return p


def make_history_db(tmp_path: Path, rows: list[tuple[str, str | None, str]], name: str = "history.db") -> Path:
    db_path = tmp_path / name
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


def make_vocab_file(tmp_path: Path, terms: list[str]) -> Path:
    p = tmp_path / "vocab.txt"
    p.write_text("\n".join(terms) + "\n", encoding="utf-8")
    return p


def sine_samples(duration_s: float = 0.5, freq: float = 440.0, amplitude: float = 0.5, sr: int = SR) -> np.ndarray:
    t = np.arange(int(duration_s * sr), dtype=np.float64) / sr
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def passing_qc() -> QCResult:
    return QCResult(clipping=False, silence_ratio=0.9, rms_dbfs=-20.0, snr_db=30.0, passed=True)


def failing_qc() -> QCResult:
    return QCResult(clipping=True, silence_ratio=0.9, rms_dbfs=-20.0, snr_db=30.0, passed=False)


# ---------------------------------------------------------------------------
# Manifest: round-trip + atomicity
# ---------------------------------------------------------------------------


def test_manifest_json_round_trip(tmp_path):
    prompts = [
        ManifestPrompt(
            prompt_id="harvard-0007",
            source="harvard",
            text="The birch canoe slid on the smooth planks.",
            split="train",
            clip_path="clips/harvard-0007.wav",
            status="accepted",
            qc={"clipping": False, "silence_ratio": 0.81, "rms_dbfs": -22.4, "snr_db": 27.1},
            attempts=1,
            recorded_ts=1799000042.1,
        ),
        ManifestPrompt(
            prompt_id="vocab-0000",
            source="vocab",
            text="The word is Karina.",
            split="eval",
            clip_path="clips/vocab-0000.wav",
        ),
    ]
    manifest = Manifest(
        session_id="2026-07-18T14-02-11",
        created_ts=1799000000.0,
        status="in_progress",
        speakeasy_version="0.1.0",
        prompts=prompts,
    )
    path = tmp_path / "sessions" / "2026-07-18T14-02-11" / "manifest.json"
    manifest.save(path)

    assert path.exists()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["session_id"] == "2026-07-18T14-02-11"
    assert on_disk["prompts"][0]["prompt_id"] == "harvard-0007"
    assert on_disk["prompts"][0]["qc"]["silence_ratio"] == 0.81

    loaded = Manifest.load(path)
    assert loaded == manifest


def test_manifest_save_leaves_no_temp_files(tmp_path):
    manifest = Manifest(
        session_id="s1", created_ts=1.0, status="in_progress", speakeasy_version="0.1.0", prompts=[]
    )
    path = tmp_path / "manifest.json"
    manifest.save(path)
    manifest.save(path)  # second save should also clean up its own temp file
    entries = list(tmp_path.iterdir())
    assert entries == [path]


def test_manifest_load_ignores_partial_crash_temp_file(tmp_path):
    """Simulate a crash mid-write: a stray, incompletely-written temp file
    sits next to a valid manifest.json. Loading must return the last
    good manifest, never something derived from the partial temp file."""
    good = Manifest(
        session_id="s1", created_ts=1.0, status="in_progress", speakeasy_version="0.1.0",
        prompts=[ManifestPrompt(prompt_id="p1", source="harvard", text="x", split="train", clip_path="clips/p1.wav")],
    )
    path = tmp_path / "manifest.json"
    good.save(path)

    # Simulate a crash that occurred mid-write of the *next* update: a
    # temp file exists but was never renamed into place.
    stray_tmp = tmp_path / ".manifest.json.tmp-99999-123"
    stray_tmp.write_text('{"session_id": "s1", "status": "CORRUPT', encoding="utf-8")

    loaded = Manifest.load(path)
    assert loaded == good
    assert loaded.prompts[0].status == "pending"
    # The stray temp file is inert -- attempting to parse *it* as JSON fails,
    # proving it was never treated as the manifest.
    with pytest.raises(json.JSONDecodeError):
        json.loads(stray_tmp.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# assign_split: deterministic, reproducible (AC3)
# ---------------------------------------------------------------------------


def test_assign_split_is_reproducible():
    a = assign_split("harvard-0007", "2026-07-18T14-02-11")
    b = assign_split("harvard-0007", "2026-07-18T14-02-11")
    assert a == b
    assert a in ("train", "eval")


def test_assign_split_matches_manual_sha256_computation():
    import hashlib

    prompt_id, session_id = "vocab-0003", "2026-07-18T14-02-11"
    digest = hashlib.sha256(f"{session_id}:{prompt_id}".encode("utf-8")).hexdigest()
    expected_bucket = int(digest[:8], 16) % 100
    expected = "train" if expected_bucket < 80 else "eval"
    assert assign_split(prompt_id, session_id) == expected


def test_assign_split_differs_by_session_id_generally():
    # Same prompt id, different session id -- across many session ids we
    # expect both buckets to appear (guards against a constant-function
    # stub that always returns the same split).
    results = {assign_split("harvard-0001", f"session-{i}") for i in range(50)}
    assert results == {"train", "eval"}


def test_assign_split_roughly_80_20_distribution():
    session_id = "2026-07-18T14-02-11"
    results = [assign_split(f"prompt-{i:05d}", session_id) for i in range(2000)]
    train_frac = results.count("train") / len(results)
    assert 0.7 <= train_frac <= 0.9


# ---------------------------------------------------------------------------
# build_prompt_set: determinism, size, source interleave
# ---------------------------------------------------------------------------


def _rich_history_db(tmp_path):
    return make_history_db(
        tmp_path,
        [
            ("i saw Karina Legal Workspace yesterday", None, "raw"),
            ("call sherpa onnx integration team", None, "raw"),
            ("i gonna call bob", "I am going to call Bob.", "clean"),
            ("schedule with parakeet team", "Schedule with Parakeet team.", "clean"),
            ("fix the widget config", "Fix the Widget configuration.", "clean"),
        ],
    )


def test_build_prompt_set_deterministic_same_seed(tmp_path):
    corpus = make_corpus(tmp_path, n=20)
    history_db = _rich_history_db(tmp_path)
    vocab_path = make_vocab_file(tmp_path, ["Foundry", "Codex"])

    first = build_prompt_set(QUICK_SIZE, corpus, history_db, vocab_path, seed=42)
    second = build_prompt_set(QUICK_SIZE, corpus, history_db, vocab_path, seed=42)

    assert [(p.prompt_id, p.source, p.text) for p in first] == [
        (p.prompt_id, p.source, p.text) for p in second
    ]


def test_build_prompt_set_different_seed_can_differ(tmp_path):
    corpus = make_corpus(tmp_path, n=20)
    history_db = _rich_history_db(tmp_path)
    vocab_path = make_vocab_file(tmp_path, ["Foundry", "Codex"])

    a = build_prompt_set(QUICK_SIZE, corpus, history_db, vocab_path, seed=1)
    b = build_prompt_set(QUICK_SIZE, corpus, history_db, vocab_path, seed=2)

    assert [p.prompt_id for p in a] != [p.prompt_id for p in b]


def test_build_prompt_set_quick_size_and_all_three_sources(tmp_path):
    corpus = make_corpus(tmp_path, n=20)
    history_db = _rich_history_db(tmp_path)
    vocab_path = make_vocab_file(tmp_path, ["Foundry", "Codex", "Acme"])

    prompts = build_prompt_set(QUICK_SIZE, corpus, history_db, vocab_path, seed=7)

    assert len(prompts) == QUICK_SIZE
    sources = {p.source for p in prompts}
    assert sources == {"harvard", "vocab", "correction"}


def test_build_prompt_set_full_size(tmp_path):
    corpus = make_corpus(tmp_path, n=100)
    history_db = _rich_history_db(tmp_path)
    vocab_path = make_vocab_file(tmp_path, ["Foundry", "Codex", "Acme", "Descrybe"])

    prompts = build_prompt_set(FULL_SIZE, corpus, history_db, vocab_path, seed=7)
    assert len(prompts) == FULL_SIZE


def test_build_prompt_set_no_history_backfills_from_harvard(tmp_path):
    corpus = make_corpus(tmp_path, n=20)
    prompts = build_prompt_set(QUICK_SIZE, corpus, history_db=None, vocab_path=None, seed=3)
    assert len(prompts) == QUICK_SIZE
    assert all(p.source == "harvard" for p in prompts)


def test_build_prompt_set_real_vendored_corpus_smoke():
    prompts = build_prompt_set(QUICK_SIZE, REAL_CORPUS_PATH, history_db=None, vocab_path=None, seed=0)
    assert len(prompts) == QUICK_SIZE
    assert all(p.source == "harvard" for p in prompts)


# ---------------------------------------------------------------------------
# TrainingSession: create, resume-after-interrupt (AC1), retry, finish
# ---------------------------------------------------------------------------


def test_create_writes_in_progress_manifest_immediately(tmp_path):
    corpus = make_corpus(tmp_path, n=20)
    sessions_dir = tmp_path / "sessions"

    session = TrainingSession.create(
        sessions_dir, size=QUICK_SIZE, corpus_path=corpus, seed=1, session_id="2026-07-18T10-00-00"
    )

    manifest_path = sessions_dir / "2026-07-18T10-00-00" / "manifest.json"
    assert manifest_path.exists()
    on_disk = Manifest.load(manifest_path)
    assert on_disk.status == "in_progress"
    assert len(on_disk.prompts) == QUICK_SIZE
    assert all(p.split in ("train", "eval") for p in on_disk.prompts)
    assert session.current_prompt().prompt_id == on_disk.prompts[0].prompt_id


def test_resume_after_interrupt_zero_rerecording(tmp_path):
    """AC1: a training session can be started, interrupted (process killed)
    after any prompt, and resumed from manifest.json with zero re-recording
    of already-accepted clips."""
    corpus = make_corpus(tmp_path, n=20)
    sessions_dir = tmp_path / "sessions"

    session = TrainingSession.create(
        sessions_dir, size=5, corpus_path=corpus, seed=1, session_id="2026-07-18T10-00-00"
    )

    first_prompt = session.current_prompt()
    status = session.record_result(first_prompt.prompt_id, sine_samples(), passing_qc())
    assert status == "accepted"

    clip_path = session.session_dir / first_prompt.clip_path
    assert clip_path.exists()
    original_bytes = clip_path.read_bytes()

    # Simulate the process being killed: drop the in-memory `session`
    # object entirely and resume purely from disk.
    del session
    resumed = TrainingSession.resume(sessions_dir)
    assert resumed is not None
    assert resumed.manifest.session_id == "2026-07-18T10-00-00"

    # The next actionable prompt is NOT the already-accepted one.
    next_prompt = resumed.current_prompt()
    assert next_prompt.prompt_id != first_prompt.prompt_id
    assert next_prompt.status != "accepted"

    # The accepted clip was never touched/re-recorded.
    assert resumed._find(first_prompt.prompt_id).status == "accepted"
    assert clip_path.read_bytes() == original_bytes


def test_resume_returns_none_when_no_incomplete_session(tmp_path):
    sessions_dir = tmp_path / "sessions"
    assert TrainingSession.resume(sessions_dir) is None


def test_resume_ignores_complete_sessions(tmp_path):
    corpus = make_corpus(tmp_path, n=20)
    sessions_dir = tmp_path / "sessions"
    session = TrainingSession.create(
        sessions_dir, size=2, corpus_path=corpus, seed=1, session_id="2026-07-18T10-00-00"
    )
    for p in list(session.manifest.prompts):
        session.record_result(p.prompt_id, sine_samples(), passing_qc())
    session.finish()

    assert TrainingSession.resume(sessions_dir) is None


def test_create_or_resume_resumes_existing_in_progress(tmp_path):
    corpus = make_corpus(tmp_path, n=20)
    sessions_dir = tmp_path / "sessions"
    original = TrainingSession.create(
        sessions_dir, size=5, corpus_path=corpus, seed=1, session_id="2026-07-18T10-00-00"
    )

    resumed = TrainingSession.create_or_resume(
        sessions_dir, size=5, corpus_path=corpus, seed=999, session_id="ignored-should-not-be-used"
    )
    assert resumed.manifest.session_id == original.manifest.session_id


def test_create_or_resume_creates_when_none_exists(tmp_path):
    corpus = make_corpus(tmp_path, n=20)
    sessions_dir = tmp_path / "sessions"
    session = TrainingSession.create_or_resume(
        sessions_dir, size=5, corpus_path=corpus, seed=1, session_id="2026-07-18T10-00-00"
    )
    assert session.manifest.session_id == "2026-07-18T10-00-00"
    assert (sessions_dir / "2026-07-18T10-00-00" / "manifest.json").exists()


def test_three_attempt_retry_then_low_quality(tmp_path):
    corpus = make_corpus(tmp_path, n=20)
    sessions_dir = tmp_path / "sessions"
    session = TrainingSession.create(
        sessions_dir, size=3, corpus_path=corpus, seed=1, session_id="2026-07-18T10-00-00"
    )
    prompt_id = session.current_prompt().prompt_id

    status1 = session.record_result(prompt_id, sine_samples(), failing_qc())
    assert status1 == "pending"
    assert session._find(prompt_id).attempts == 1

    status2 = session.record_result(prompt_id, sine_samples(), failing_qc())
    assert status2 == "pending"
    assert session._find(prompt_id).attempts == 2

    status3 = session.record_result(prompt_id, sine_samples(), failing_qc())
    assert status3 == "low_quality"
    assert session._find(prompt_id).attempts == 3

    # No clip should have been written for a never-accepted prompt.
    clip_path = session.session_dir / session._find(prompt_id).clip_path
    assert not clip_path.exists()

    # Reloading from disk preserves the low_quality flag.
    reloaded = Manifest.load(session.manifest_path)
    assert reloaded.prompts[[p.prompt_id for p in reloaded.prompts].index(prompt_id)].status == "low_quality"


def test_record_result_accepted_writes_readable_16khz_mono_wav(tmp_path):
    corpus = make_corpus(tmp_path, n=20)
    sessions_dir = tmp_path / "sessions"
    session = TrainingSession.create(
        sessions_dir, size=3, corpus_path=corpus, seed=1, session_id="2026-07-18T10-00-00"
    )
    prompt_id = session.current_prompt().prompt_id
    samples = sine_samples(duration_s=0.3)

    session.record_result(prompt_id, samples, passing_qc())

    clip_path = session.session_dir / session._find(prompt_id).clip_path
    with wave.open(str(clip_path), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 16000
        raw = w.readframes(w.getnframes())
    read_back = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    assert read_back.shape[0] == samples.shape[0]
    assert np.max(np.abs(read_back - samples)) < 1e-3

    qc_dict = session._find(prompt_id).qc
    assert qc_dict == {"clipping": False, "silence_ratio": 0.9, "rms_dbfs": -20.0, "snr_db": 30.0}


def test_finish_marks_complete_and_returns_summary(tmp_path):
    corpus = make_corpus(tmp_path, n=20)
    sessions_dir = tmp_path / "sessions"
    session = TrainingSession.create(
        sessions_dir, size=3, corpus_path=corpus, seed=1, session_id="2026-07-18T10-00-00"
    )
    prompts = list(session.manifest.prompts)

    session.record_result(prompts[0].prompt_id, sine_samples(duration_s=1.0), passing_qc())
    session.record_result(prompts[1].prompt_id, sine_samples(duration_s=2.0), passing_qc())
    session.record_result(prompts[2].prompt_id, sine_samples(duration_s=0.5), failing_qc())
    session.record_result(prompts[2].prompt_id, sine_samples(duration_s=0.5), failing_qc())
    session.record_result(prompts[2].prompt_id, sine_samples(duration_s=0.5), failing_qc())

    summary = session.finish()

    assert summary["n_clips"] == 2
    assert summary["n_rejected"] == 1
    assert summary["est_minutes"] == pytest.approx(3.0 / 60.0, abs=1e-3)

    reloaded = Manifest.load(session.manifest_path)
    assert reloaded.status == "complete"
