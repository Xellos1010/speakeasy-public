"""Tests for `speakeasy/engine/postprocess.py` (TSK-SPK-TRAIN-T8).

Governing spec: docs/superpowers/specs/2026-07-18-voice-training-environment.md
- §5 note: corrections are applied on engine raw output, ordered, before the
  mode/rewrite pipeline sees the text.
- §8 AC7: `active.json`'s pointed-to profile is the only profile the runtime
  engine consults; rolling `active_version` back fully reverts behavior with
  no cached state elsewhere.
"""

import warnings
from pathlib import Path

from speakeasy.engine.postprocess import Postprocessor
from speakeasy.training.profile import Correction, Hotword, Profile, ProfileStore, Provenance


def make_profile(version: int, corrections: list[Correction], hotwords: list[Hotword] = None) -> Profile:
    return Profile(
        version=version,
        built_ts=1799000500.0,
        built_from_session="2026-07-18T14-02-11",
        provenance=Provenance(n_train_clips=1, n_eval_clips=1, sherpa_onnx_version="1.13.4", engine="parakeet"),
        hotwords=hotwords or [],
        corrections=corrections,
    )


def test_no_active_profile_is_identity_passthrough(tmp_path):
    pp = Postprocessor(profile_dir=tmp_path / "profile")
    assert pp.process("pair a keet is fast") == "pair a keet is fast"


def test_active_profile_corrections_applied(tmp_path):
    profile_dir = tmp_path / "profile"
    store = ProfileStore(profile_dir)
    store.write(make_profile(1, [Correction(pattern=r"\bpair a keet\b", replacement="Parakeet", kind="regex")]))
    store.set_active(1)

    pp = Postprocessor(profile_dir=profile_dir)
    assert pp.process("pair a keet is fast") == "Parakeet is fast"


def test_active_profile_hotwords_fuzzy_corrected_before_regular_corrections(tmp_path):
    """TSK-SPK-TRANSFER: process() applies the fuzzy hotword fallback (the
    only mechanism that works regardless of which STT engine is active,
    given native hotwords are transducer-only and unavailable for the
    bundled Parakeet export -- see profile.py's module docstring) ahead of
    the regular corrections pass."""
    profile_dir = tmp_path / "profile"
    store = ProfileStore(profile_dir)
    store.write(make_profile(
        1,
        corrections=[Correction(pattern="workspace", replacement="Workspace", kind="literal")],
        hotwords=[Hotword(phrase="Karina Legal Workspace", score=2.5)],
    ))
    store.set_active(1)

    pp = Postprocessor(profile_dir=profile_dir)
    result = pp.process("please open carina legal workspace for me")
    assert "Karina Legal Workspace" in result


def test_corrupt_active_json_passthrough_and_warns(tmp_path):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True)
    (profile_dir / "active.json").write_text("{ not valid json", encoding="utf-8")

    pp = Postprocessor(profile_dir=profile_dir)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = pp.process("hello world")
    assert result == "hello world"
    assert any("active.json" in str(w.message) or "profile" in str(w.message) for w in caught)


def test_missing_profile_file_passthrough_and_warns(tmp_path):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True)
    # active.json points at a version whose profile file was never written.
    (profile_dir / "active.json").write_text('{"active_version": 7}', encoding="utf-8")

    pp = Postprocessor(profile_dir=profile_dir)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = pp.process("hello world")
    assert result == "hello world"
    assert len(caught) >= 1


def test_corrupt_profile_json_passthrough_and_warns(tmp_path):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True)
    (profile_dir / "profile-v1.json").write_text("{ not valid json", encoding="utf-8")
    (profile_dir / "active.json").write_text('{"active_version": 1}', encoding="utf-8")

    pp = Postprocessor(profile_dir=profile_dir)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = pp.process("hello world")
    assert result == "hello world"
    assert len(caught) >= 1


def test_version_flip_picked_up_without_restart(tmp_path):
    profile_dir = tmp_path / "profile"
    store = ProfileStore(profile_dir)
    store.write(make_profile(1, [Correction(pattern="gonna", replacement="going to", kind="literal")]))
    store.write(make_profile(2, [Correction(pattern="wanna", replacement="want to", kind="literal")]))
    store.set_active(1)

    pp = Postprocessor(profile_dir=profile_dir)
    assert pp.process("i am gonna go") == "i am going to go"
    assert pp.process("i wanna go") == "i wanna go"   # v1 doesn't have this rule

    # Flip the pointer -- same Postprocessor instance, no restart.
    store.set_active(2)
    assert pp.process("i wanna go") == "i want to go"
    assert pp.process("i am gonna go") == "i am gonna go"  # v2 doesn't have this rule


def test_rollback_fully_reverts(tmp_path):
    """AC7: rolling active_version back to N-1 fully reverts behavior --
    no cached state elsewhere makes v1's rules leak into v2 or vice versa
    once we've rolled back."""
    profile_dir = tmp_path / "profile"
    store = ProfileStore(profile_dir)
    store.write(make_profile(1, [Correction(pattern="foo", replacement="bar", kind="literal")]))
    store.write(make_profile(2, [Correction(pattern="foo", replacement="baz", kind="literal")]))
    store.set_active(2)

    pp = Postprocessor(profile_dir=profile_dir)
    assert pp.process("foo") == "baz"

    store.set_active(1)  # rollback
    assert pp.process("foo") == "bar"


def test_cache_avoids_reload_when_pointer_unchanged(tmp_path, monkeypatch):
    profile_dir = tmp_path / "profile"
    store = ProfileStore(profile_dir)
    store.write(make_profile(1, [Correction(pattern="foo", replacement="bar", kind="literal")]))
    store.set_active(1)

    pp = Postprocessor(profile_dir=profile_dir)
    pp.process("foo")  # first call: loads + caches

    read_calls = []
    orig_read = ProfileStore.read

    def counting_read(self, version):
        read_calls.append(version)
        return orig_read(self, version)

    monkeypatch.setattr(ProfileStore, "read", counting_read)

    for _ in range(5):
        assert pp.process("foo") == "bar"

    assert read_calls == []  # pointer unchanged -> no re-read of the profile file
