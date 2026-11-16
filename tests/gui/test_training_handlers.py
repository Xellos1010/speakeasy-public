import time
from dataclasses import dataclass, field

import pytest

from speakeasy.gui.handlers import training
from speakeasy.training.profile import (
    Correction,
    Hotword,
    Profile,
    ProfileStore,
    Provenance,
)

DAY = 86400.0


def _make_profile(version, built_ts, metrics=None, n_train=20, n_eval=5,
                   hotwords=None, corrections=None):
    return Profile(
        version=version,
        built_ts=built_ts,
        built_from_session=f"session-{version}",
        provenance=Provenance(
            n_train_clips=n_train,
            n_eval_clips=n_eval,
            sherpa_onnx_version="1.13.4",
            engine="parakeet",
        ),
        hotwords=hotwords or [],
        corrections=corrections or [],
        metrics=metrics,
    )


@dataclass
class _FakeCtx:
    profile_store: ProfileStore
    _resumable: bool = False
    _start_result: dict = field(default_factory=lambda: {"started": True})
    _resume_result: dict = field(default_factory=lambda: {"resumed": True})
    last_start_size: str = None
    resume_called: bool = False

    def training_resumable(self):
        return self._resumable

    def start_training(self, size_name):
        self.last_start_size = size_name
        return self._start_result

    def resume_training(self):
        self.resume_called = True
        return self._resume_result


@pytest.fixture
def store(tmp_path):
    return ProfileStore(tmp_path / "profile")


# ---------------------------------------------------------------------------
# _days_ago -- pure, injectable-now helper
# ---------------------------------------------------------------------------


def test_days_ago_zero_for_just_built():
    now = 1_000_000.0
    assert training._days_ago(now, now) == 0


def test_days_ago_rounds_down_to_whole_days():
    now = 1_000_000.0
    built = now - (2.5 * DAY)
    assert training._days_ago(built, now) == 2


def test_days_ago_never_negative_for_future_timestamp():
    now = 1_000_000.0
    built = now + DAY  # clock skew: "built" is after "now"
    assert training._days_ago(built, now) == 0


# ---------------------------------------------------------------------------
# training_get_state
# ---------------------------------------------------------------------------


def test_get_state_no_active_profile_reports_nothing_trained(store):
    ctx = _FakeCtx(profile_store=store, _resumable=False)

    result = training.training_get_state({}, ctx)

    assert result["has_active"] is False
    assert result["active"] is None
    assert result["versions"] == []
    assert result["resumable"] is False


def test_get_state_active_profile_with_real_metrics(store):
    # training_get_state stamps days_ago against the real wall clock
    # (time.time()), so anchor built_ts to it rather than a fixed instant.
    now = time.time()
    profile = _make_profile(
        1, built_ts=now - (3 * DAY),
        metrics={
            "baseline_wer": 0.041, "profile_wer": 0.028,
            "baseline_p50_ms": 231, "profile_p50_ms": 238,
            "baseline_p95_ms": 310, "profile_p95_ms": 322,
        },
        hotwords=[Hotword(phrase="Karina", score=2.0)],
        corrections=[Correction(pattern="gonna", replacement="going to")],
    )
    store.write(profile)
    store.set_active(1)
    ctx = _FakeCtx(profile_store=store)

    result = training.training_get_state({}, ctx)

    assert result["has_active"] is True
    active = result["active"]
    assert active["version"] == 1
    assert active["days_ago"] == 3
    assert active["n_train_clips"] == 20
    assert active["n_eval_clips"] == 5
    assert active["engine"] == "parakeet"
    assert active["metrics"]["baseline_wer"] == 0.041
    assert active["metrics"]["profile_wer"] == 0.028
    assert active["hotwords"] == [{"phrase": "Karina", "score": 2.0}]
    assert active["corrections"][0]["pattern"] == "gonna"


def test_get_state_active_profile_without_metrics_is_none_not_missing(store):
    profile = _make_profile(1, built_ts=1_000_000.0, metrics=None)
    store.write(profile)
    store.set_active(1)
    ctx = _FakeCtx(profile_store=store)

    result = training.training_get_state({}, ctx)

    assert result["active"]["metrics"] is None


def test_get_state_versions_ordered_newest_first_with_active_flag(store):
    store.write(_make_profile(1, built_ts=1_000_000.0))
    store.write(_make_profile(2, built_ts=1_100_000.0))
    store.write(_make_profile(3, built_ts=1_200_000.0))
    store.set_active(2)  # e.g. v3 got rejected by the benchmark gate
    ctx = _FakeCtx(profile_store=store)

    result = training.training_get_state({}, ctx)

    versions = result["versions"]
    assert [v["version"] for v in versions] == [3, 2, 1]
    by_version = {v["version"]: v for v in versions}
    assert by_version[2]["is_active"] is True
    assert by_version[1]["is_active"] is False
    assert by_version[3]["is_active"] is False


def test_get_state_resumable_flag_passed_through(store):
    ctx = _FakeCtx(profile_store=store, _resumable=True)
    result = training.training_get_state({}, ctx)
    assert result["resumable"] is True


# ---------------------------------------------------------------------------
# training_start / training_resume
# ---------------------------------------------------------------------------


def test_start_passes_size_through_to_ctx(store):
    ctx = _FakeCtx(profile_store=store)
    result = training.training_start({"size": "full"}, ctx)

    assert ctx.last_start_size == "full"
    assert result == {"started": True}


def test_start_defaults_to_quick_if_size_omitted(store):
    ctx = _FakeCtx(profile_store=store)
    training.training_start({}, ctx)

    assert ctx.last_start_size == "quick"


def test_resume_calls_ctx_resume_training(store):
    ctx = _FakeCtx(profile_store=store)
    result = training.training_resume({}, ctx)

    assert ctx.resume_called is True
    assert result == {"resumed": True}


# ---------------------------------------------------------------------------
# training_restore_version
# ---------------------------------------------------------------------------


def test_restore_version_success_flips_active_pointer(store):
    store.write(_make_profile(1, built_ts=1_000_000.0))
    store.write(_make_profile(2, built_ts=1_100_000.0))
    store.set_active(1)
    ctx = _FakeCtx(profile_store=store)

    result = training.training_restore_version({"version": 2}, ctx)

    assert result == {"active_version": 2}
    assert store.active_version() == 2


def test_restore_nonexistent_version_raises_not_crashes(store):
    store.write(_make_profile(1, built_ts=1_000_000.0))
    store.set_active(1)
    ctx = _FakeCtx(profile_store=store)

    with pytest.raises(FileNotFoundError):
        training.training_restore_version({"version": 99}, ctx)

    # the bad restore attempt must never have moved the active pointer
    assert store.active_version() == 1


def test_restore_nonexistent_version_surfaces_as_bridge_error(store):
    """End-to-end through bridge.dispatch (not just the raw handler) --
    confirms the bridge's catch-all turns the raised FileNotFoundError into
    a normal {"ok": false, "error": ...} response instead of propagating."""
    from speakeasy.gui import bridge

    store.write(_make_profile(1, built_ts=1_000_000.0))
    store.set_active(1)
    ctx = _FakeCtx(profile_store=store)

    response = bridge.dispatch("training_restore_version", {"version": 99}, ctx)

    assert response["ok"] is False
    assert "99" in response["error"]
