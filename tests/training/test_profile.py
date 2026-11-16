import json
import sqlite3
import warnings
from pathlib import Path

import pytest

from speakeasy.training.profile import (
    Correction,
    Decoding,
    Hotword,
    Profile,
    ProfileStore,
    Provenance,
    apply_corrections,
    apply_hotword_fuzzy_correction,
    build_profile,
)
from speakeasy.training.session import Manifest, ManifestPrompt

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


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


def make_manifest(tmp_path: Path, prompts: list[ManifestPrompt], session_id: str = "2026-07-18T14-02-11") -> Path:
    manifest = Manifest(
        session_id=session_id,
        created_ts=1799000000.0,
        status="complete",
        speakeasy_version="0.0.0-test",
        prompts=prompts,
    )
    path = tmp_path / "manifest.json"
    manifest.save(path)
    return path


def sample_profile(version: int = 1) -> Profile:
    return Profile(
        version=version,
        built_ts=1799000500.0,
        built_from_session="2026-07-18T14-02-11",
        provenance=Provenance(
            n_train_clips=64, n_eval_clips=16, sherpa_onnx_version="1.13.4", engine="parakeet"
        ),
        hotwords=[
            Hotword(phrase="Karina Legal Workspace", score=2.5),
            Hotword(phrase="sherpa onnx", score=2.0),
        ],
        corrections=[
            Correction(pattern=r"\bpair a keet\b", replacement="Parakeet", kind="regex", case_sensitive=False),
            Correction(pattern="gonna", replacement="going to", kind="literal"),
        ],
        decoding=Decoding(decoding_method="greedy_search", hotwords_enabled=False),
        metrics={
            "baseline_wer": 0.041,
            "profile_wer": 0.028,
            "baseline_p50_ms": 231,
            "profile_p50_ms": 238,
            "baseline_p95_ms": 310,
            "profile_p95_ms": 322,
        },
    )


# ---------------------------------------------------------------------------
# schema round-trip (spec §5)
# ---------------------------------------------------------------------------


def test_profile_to_dict_matches_spec_schema_keys():
    profile = sample_profile()
    d = profile.to_dict()
    assert set(d.keys()) == {
        "version", "built_ts", "built_from_session", "provenance",
        "hotwords", "corrections", "decoding", "metrics",
    }
    assert d["provenance"] == {
        "n_train_clips": 64, "n_eval_clips": 16,
        "sherpa_onnx_version": "1.13.4", "engine": "parakeet",
    }
    assert d["hotwords"] == [
        {"phrase": "Karina Legal Workspace", "score": 2.5},
        {"phrase": "sherpa onnx", "score": 2.0},
    ]
    assert d["corrections"] == [
        {"pattern": r"\bpair a keet\b", "replacement": "Parakeet", "kind": "regex", "case_sensitive": False},
        {"pattern": "gonna", "replacement": "going to", "kind": "literal", "case_sensitive": False},
    ]
    assert d["decoding"] == {"decoding_method": "greedy_search", "hotwords_enabled": False}


def test_profile_json_round_trip():
    profile = sample_profile()
    restored = Profile.from_json(profile.to_json())
    assert restored.to_dict() == profile.to_dict()
    # JSON is actually parseable and matches the dict form.
    assert json.loads(profile.to_json()) == profile.to_dict()


def test_profile_from_dict_defaults_hotwords_enabled_false_when_absent():
    d = sample_profile().to_dict()
    d["decoding"] = {"decoding_method": "greedy_search"}
    restored = Profile.from_dict(d)
    assert restored.decoding.hotwords_enabled is False


# ---------------------------------------------------------------------------
# ProfileStore: versioning / append-only (spec §5, §8 AC7)
# ---------------------------------------------------------------------------


def test_store_write_twice_yields_v1_v2(tmp_path):
    store = ProfileStore(tmp_path / "profile")
    p1 = store.write(sample_profile(version=0))
    p2 = store.write(sample_profile(version=0))
    assert p1.name == "profile-v1.json"
    assert p2.name == "profile-v2.json"
    assert store.next_version() == 3

def test_list_versions_returns_all_written_versions_oldest_first(tmp_path):
    store = ProfileStore(tmp_path / "profile")
    assert store.list_versions() == []
    store.write(sample_profile(version=0))
    store.write(sample_profile(version=0))
    assert store.list_versions() == [1, 2]


def test_store_write_stamps_profile_version(tmp_path):
    store = ProfileStore(tmp_path / "profile")
    profile = sample_profile(version=0)
    store.write(profile)
    assert profile.version == 1


def test_store_write_never_overwrites_existing_version(tmp_path):
    store = ProfileStore(tmp_path / "profile")
    store.write(sample_profile(version=0))  # v1
    store.write(sample_profile(version=0))  # v2
    with pytest.raises(FileExistsError):
        store.write(sample_profile(version=0), version=1)


def test_store_read_round_trips_written_profile(tmp_path):
    store = ProfileStore(tmp_path / "profile")
    original = sample_profile(version=0)
    store.write(original)
    reread = store.read(1)
    assert reread.to_dict() == original.to_dict()


# ---------------------------------------------------------------------------
# ProfileStore: active pointer + rollback (spec §5, §8 AC7)
# ---------------------------------------------------------------------------


def test_active_version_none_when_no_pointer(tmp_path):
    store = ProfileStore(tmp_path / "profile")
    assert store.active_version() is None
    assert store.load_active() is None


def test_set_active_and_load_active(tmp_path):
    store = ProfileStore(tmp_path / "profile")
    store.write(sample_profile(version=0))  # v1
    store.set_active(1)
    assert store.active_version() == 1
    assert store.load_active().to_dict() == store.read(1).to_dict()


def test_set_active_unknown_version_raises(tmp_path):
    store = ProfileStore(tmp_path / "profile")
    with pytest.raises(FileNotFoundError):
        store.set_active(1)


def test_rollback_is_only_pointer_flip_no_other_state(tmp_path):
    store = ProfileStore(tmp_path / "profile")
    v1 = sample_profile(version=0)
    v1.corrections = [Correction(pattern="a", replacement="b")]
    store.write(v1)  # v1
    v2 = sample_profile(version=0)
    v2.corrections = [Correction(pattern="c", replacement="d")]
    store.write(v2)  # v2

    store.set_active(2)
    assert store.active_version() == 2

    # Rollback: N-1, nothing else changes.
    store.set_active(1)
    assert store.active_version() == 1
    assert store.load_active().corrections[0].pattern == "a"
    # v2 profile file itself is untouched -- still readable, unchanged.
    assert store.read(2).corrections[0].pattern == "c"


# ---------------------------------------------------------------------------
# build_profile: fixture manifest + fixture history db
# ---------------------------------------------------------------------------


CORRECTION_RAW = "I said pair a keet is fast"
CORRECTION_REWRITTEN = "I said Parakeet is fast"


def _fixture_manifest_prompts():
    return [
        ManifestPrompt(
            prompt_id="harvard-0001",
            source="harvard",
            text="The birch canoe slid on the smooth planks.",
            split="train",
            clip_path="clips/harvard-0001.wav",
            status="accepted",
        ),
        ManifestPrompt(
            prompt_id="vocab-0001",
            source="vocab",
            text="The word is Karina Legal Workspace.",
            split="eval",
            clip_path="clips/vocab-0001.wav",
            status="accepted",
        ),
        ManifestPrompt(
            prompt_id="correction-0001",
            source="correction",
            text=CORRECTION_RAW,
            split="train",
            clip_path="clips/correction-0001.wav",
            status="accepted",
        ),
        ManifestPrompt(
            prompt_id="correction-0002",
            source="correction",
            text="unused low quality prompt",
            split="eval",
            clip_path="clips/correction-0002.wav",
            status="low_quality",
        ),
    ]


def _fixture_history_rows():
    return [
        # (a) real correction, mined via the manifest correction-pair prompt.
        (CORRECTION_RAW, CORRECTION_REWRITTEN, "clean"),
        # filler/contraction expansion -- real signal, never noise-dropped.
        ("I am gonna go home", "I am going to go home", "clean"),
        # casing-only noise diff, occurs ONCE -- should be dropped (default threshold 2).
        ("the Cat sat down", "the cat sat down", "clean"),
        # casing-only noise diff, same pair occurs TWICE -- should be kept.
        ("This is Foo here", "This is foo here", "clean"),
        ("Foo is here again", "foo is here again", "clean"),
        # new vocabulary introduced only in rewritten -- real signal, source (b) for hotwords.
        ("check the tool", "check the sherpa onnx tool", "clean"),
        # proper noun in raw, for hotwords mining.
        ("Karina Legal Workspace project starts Monday", None, "raw"),
    ]


@pytest.fixture()
def fixture_paths(tmp_path):
    manifest_path = make_manifest(tmp_path, _fixture_manifest_prompts())
    history_db = make_history_db(tmp_path, _fixture_history_rows())
    return manifest_path, history_db


def test_build_profile_provenance_from_manifest_split_counts(fixture_paths):
    manifest_path, history_db = fixture_paths
    profile = build_profile(manifest_path, history_db)
    # 2 accepted train (harvard, correction-0001), 1 accepted eval (vocab-0001);
    # correction-0002 is low_quality and excluded.
    assert profile.provenance.n_train_clips == 2
    assert profile.provenance.n_eval_clips == 1
    assert profile.provenance.engine == "parakeet"
    assert profile.built_from_session == "2026-07-18T14-02-11"


def test_build_profile_decoding_defaults(fixture_paths):
    manifest_path, history_db = fixture_paths
    profile = build_profile(manifest_path, history_db)
    assert profile.decoding.decoding_method == "greedy_search"
    assert profile.decoding.hotwords_enabled is False


def test_build_profile_metrics_none_at_build_time(fixture_paths):
    manifest_path, history_db = fixture_paths
    profile = build_profile(manifest_path, history_db)
    assert profile.metrics is None


def test_build_profile_hotwords_from_vocab_mining(fixture_paths):
    manifest_path, history_db = fixture_paths
    profile = build_profile(manifest_path, history_db)
    phrases = {h.phrase for h in profile.hotwords}
    # multi-word proper-noun phrase preserved as a single hotword.
    assert "Karina Legal Workspace" in phrases
    # new single-token vocab introduced only in rewritten.
    assert "sherpa" in phrases
    assert "onnx" in phrases
    for h in profile.hotwords:
        assert h.score == 2.0
    # filler-expansion tokens ("going", "to") are normalization, not vocab --
    # must not leak into hotwords.
    assert "going" not in phrases


def test_build_profile_real_correction_from_manifest_correction_prompt(fixture_paths):
    manifest_path, history_db = fixture_paths
    profile = build_profile(manifest_path, history_db)
    patterns = {(c.pattern, c.replacement) for c in profile.corrections}
    assert ("pair a keet", "Parakeet") in patterns


def test_build_profile_filler_expansion_correction_always_kept(fixture_paths):
    manifest_path, history_db = fixture_paths
    profile = build_profile(manifest_path, history_db)
    patterns = {(c.pattern, c.replacement) for c in profile.corrections}
    assert ("gonna", "going to") in patterns


def test_build_profile_noise_threshold_drops_single_occurrence(fixture_paths):
    manifest_path, history_db = fixture_paths
    profile = build_profile(manifest_path, history_db)
    patterns = {(c.pattern.lower(), c.replacement.lower()) for c in profile.corrections}
    # "Cat" -> "cat" occurs once: pure casing noise, dropped at default threshold 2.
    assert ("cat", "cat") not in patterns


def test_build_profile_noise_threshold_keeps_repeated_occurrence(fixture_paths):
    manifest_path, history_db = fixture_paths
    profile = build_profile(manifest_path, history_db)
    patterns = {(c.pattern.lower(), c.replacement.lower()) for c in profile.corrections}
    # "Foo" -> "foo" occurs twice: still casing-only, but meets the threshold.
    assert ("foo", "foo") in patterns


def test_build_profile_noise_threshold_is_configurable(fixture_paths):
    manifest_path, history_db = fixture_paths
    lenient = build_profile(manifest_path, history_db, min_correction_occurrences=1)
    patterns = {(c.pattern.lower(), c.replacement.lower()) for c in lenient.corrections}
    assert ("cat", "cat") in patterns


def test_build_profile_corrections_ordered_deterministically(fixture_paths):
    manifest_path, history_db = fixture_paths
    p1 = build_profile(manifest_path, history_db)
    p2 = build_profile(manifest_path, history_db)
    assert [c.pattern for c in p1.corrections] == [c.pattern for c in p2.corrections]
    lower_patterns = [c.pattern.lower() for c in p1.corrections]
    assert lower_patterns == sorted(lower_patterns)


def test_build_profile_no_history_db_yields_empty_hotwords_and_corrections(tmp_path):
    manifest_path = make_manifest(tmp_path, _fixture_manifest_prompts())
    profile = build_profile(manifest_path, history_db=None)
    assert profile.hotwords == []
    assert profile.corrections == []


def test_build_profile_carries_forward_prior_rules_unless_contradicted(fixture_paths):
    manifest_path, history_db = fixture_paths
    prior = sample_profile(version=1)
    prior.corrections = [
        Correction(pattern="unrelated old rule", replacement="kept as-is", kind="literal"),
        Correction(pattern="gonna", replacement="going 2", kind="literal"),  # contradicted by new mining
    ]
    profile = build_profile(manifest_path, history_db, prior_profile=prior)
    by_pattern = {c.pattern: c.replacement for c in profile.corrections}
    assert by_pattern["unrelated old rule"] == "kept as-is"
    assert by_pattern["gonna"] == "going to"  # new mined value wins over contradicted prior


# ---------------------------------------------------------------------------
# apply_corrections
# ---------------------------------------------------------------------------


def test_apply_corrections_literal_word_boundary_aware():
    corrections = [Correction(pattern="cat", replacement="dog", kind="literal")]
    assert apply_corrections("the cat sat down", corrections) == "the dog sat down"
    # "category" must NOT match "cat" due to word-boundary anchoring.
    assert apply_corrections("the category is great", corrections) == "the category is great"


def test_apply_corrections_regex_kind():
    corrections = [
        Correction(pattern=r"\bpair a keet\b", replacement="Parakeet", kind="regex", case_sensitive=False)
    ]
    assert apply_corrections("I use pair a keet daily", corrections) == "I use Parakeet daily"


def test_apply_corrections_ordering_is_sequential():
    corrections = [
        Correction(pattern="foo", replacement="bar", kind="literal"),
        Correction(pattern="bar", replacement="baz", kind="literal"),
    ]
    # rule 1 turns foo->bar, rule 2 then turns that bar->baz: order matters.
    assert apply_corrections("foo", corrections) == "baz"


def test_apply_corrections_case_insensitive_by_default():
    corrections = [Correction(pattern="Karina", replacement="KARINA-CORP", kind="literal")]
    assert apply_corrections("please book karina for tomorrow", corrections) == "please book KARINA-CORP for tomorrow"


def test_apply_corrections_case_sensitive_flag_respected():
    corrections = [
        Correction(pattern="Karina", replacement="KARINA-CORP", kind="literal", case_sensitive=True)
    ]
    assert apply_corrections("please book karina for tomorrow", corrections) == "please book karina for tomorrow"
    assert apply_corrections("please book Karina for tomorrow", corrections) == "please book KARINA-CORP for tomorrow"


def test_apply_corrections_invalid_regex_is_skipped_not_raised():
    corrections = [
        Correction(pattern="(unclosed", replacement="oops", kind="regex"),
        Correction(pattern="cat", replacement="dog", kind="literal"),
    ]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = apply_corrections("the cat sat down", corrections)
    assert result == "the dog sat down"
    assert any(issubclass(w.category, RuntimeWarning) for w in caught)


def test_apply_corrections_accepts_plain_dicts_too():
    corrections = [{"pattern": "cat", "replacement": "dog", "kind": "literal", "case_sensitive": False}]
    assert apply_corrections("the cat sat", corrections) == "the dog sat"


def test_apply_corrections_empty_list_returns_text_unchanged():
    assert apply_corrections("hello world", []) == "hello world"


# --- apply_hotword_fuzzy_correction (TSK-SPK-TRANSFER) ----------------------

def test_fuzzy_hotword_corrects_a_close_misrecognition():
    hotwords = [Hotword(phrase="Karina Legal Workspace", score=2.5)]
    text = "please open carina legal workspace for me"
    result = apply_hotword_fuzzy_correction(text, hotwords)
    assert "Karina Legal Workspace" in result
    assert "carina legal workspace" not in result.lower()

def test_fuzzy_hotword_leaves_already_correct_text_alone():
    hotwords = [Hotword(phrase="sherpa onnx")]
    text = "we are using sherpa onnx for transcription"
    assert apply_hotword_fuzzy_correction(text, hotwords) == text

def test_fuzzy_hotword_does_not_touch_unrelated_text():
    hotwords = [Hotword(phrase="Karina Legal Workspace")]
    text = "the weather today is sunny and warm"
    assert apply_hotword_fuzzy_correction(text, hotwords) == text

def test_fuzzy_hotword_respects_threshold():
    hotwords = [Hotword(phrase="Karina Legal Workspace")]
    text = "completely different words with no resemblance at all"
    # a very high threshold should refuse to match anything not genuinely close
    assert apply_hotword_fuzzy_correction(text, hotwords, threshold=99.0) == text

def test_fuzzy_hotword_accepts_plain_dicts_too():
    hotwords = [{"phrase": "sherpa onnx", "score": 2.0}]
    text = "we use sherpa onix daily"
    result = apply_hotword_fuzzy_correction(text, hotwords)
    assert "sherpa onnx" in result.lower()

def test_fuzzy_hotword_empty_list_returns_text_unchanged():
    assert apply_hotword_fuzzy_correction("hello world", []) == "hello world"

def test_fuzzy_hotword_skips_blank_phrase():
    hotwords = [Hotword(phrase="   ")]
    text = "hello world"
    assert apply_hotword_fuzzy_correction(text, hotwords) == text
