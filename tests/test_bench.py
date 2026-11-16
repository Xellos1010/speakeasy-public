import json
from pathlib import Path

import numpy as np
import pytest

from speakeasy.bench import (
    CLIP_DURATIONS_S,
    bench_engine,
    build_record,
    resolve_tailscale_ip,
    synth_clip,
    write_record_atomically,
)


def test_no_platform_specific_imports():
    """ADJ-SPK-024 (a): engine-only, must run identically on macOS and Linux."""
    import sys
    import speakeasy.bench  # noqa: F401 -- import itself must not raise
    forbidden = {"Quartz", "Xlib", "pynput"}
    loaded = forbidden & set(sys.modules)
    # Some other test in the same process may have imported these already;
    # what actually matters is that speakeasy.bench's own source never
    # references them -- check the source, not just sys.modules.
    src = Path(speakeasy.bench.__file__).read_text()
    for name in forbidden:
        assert f"import {name}" not in src and f"from {name}" not in src, \
            f"speakeasy/bench.py must not import {name}"


def test_synth_clip_is_deterministic_and_right_shape():
    a = synth_clip(3, seed=42)
    b = synth_clip(3, seed=42)
    np.testing.assert_array_equal(a, b)
    assert len(a) == 3 * 16000
    assert a.dtype == np.float32
    assert np.max(np.abs(a)) <= 0.61  # normalized to ~0.6 peak

def test_synth_clip_different_durations_scale_length():
    assert len(synth_clip(15)) == 15 * 16000


class StubEngine:
    def __init__(self, latency_ms=50.0):
        self._latency_ms = latency_ms
        self.calls = 0

    def transcribe(self, samples, sample_rate=16000):
        self.calls += 1
        return "stub transcription"


def test_bench_engine_runs_warmup_plus_n_runs_per_band():
    engine = StubEngine()
    durations = (3, 5)
    bench_engine(engine, durations=durations, n_runs=4, clock=lambda: 0.0)
    # 1 warmup + 4 timed runs per band, 2 bands
    assert engine.calls == (1 + 4) * 2

def test_bench_engine_returns_p50_p95_per_band():
    # clock substitute that returns increasing values so each "transcribe" measures
    # a fixed 10ms step regardless of which call (warmup or timed) it's wrapping.
    calls = {"n": 0}
    def clock():
        calls["n"] += 1
        return calls["n"] * 0.01  # 10ms increments -> each transcribe "takes" 10ms

    engine = StubEngine()
    p50, p95 = bench_engine(engine, durations=(3,), n_runs=5, clock=clock)
    assert "3s_clip" in p50 and "3s_clip" in p95
    assert p50["3s_clip"] == pytest.approx(10.0, abs=0.01)
    assert p95["3s_clip"] == pytest.approx(10.0, abs=0.01)


def test_write_record_atomically_and_read_back(tmp_path: Path):
    record = {"host_id": "test-host", "engine": {"name": "parakeet"}}
    out = tmp_path / "host_settings.json"
    write_record_atomically(record, out)
    assert json.loads(out.read_text()) == record

def test_write_record_atomically_no_tmp_file_left_behind(tmp_path: Path):
    record = {"a": 1}
    out = tmp_path / "host_settings.json"
    write_record_atomically(record, out)
    leftovers = list(tmp_path.glob(".host_settings.*.tmp"))
    assert leftovers == []

def test_write_record_atomically_cleans_up_tmp_on_failure(tmp_path: Path, monkeypatch):
    out = tmp_path / "host_settings.json"

    class Unserializable:
        pass

    with pytest.raises(TypeError):
        write_record_atomically({"bad": Unserializable()}, out)
    assert not out.exists()
    assert list(tmp_path.glob(".host_settings.*.tmp")) == []


def test_resolve_tailscale_ip_returns_stripped_output():
    class FakeResult:
        stdout = "192.0.2.3\n"
    assert resolve_tailscale_ip(run=lambda *a, **k: FakeResult()) == "192.0.2.3"

def test_resolve_tailscale_ip_none_on_failure():
    def failing_run(*a, **k):
        raise FileNotFoundError("no tailscale")
    assert resolve_tailscale_ip(run=failing_run) is None

def test_resolve_tailscale_ip_none_on_empty_output():
    class FakeResult:
        stdout = ""
    assert resolve_tailscale_ip(run=lambda *a, **k: FakeResult()) is None


def test_build_record_shape_matches_port_spec_4():
    record = build_record("parakeet", {"3s_clip": 212.0}, {"3s_clip": 216.0},
                           gpu_available=False, gpu_p50={}, gpu_p95={},
                           host_id="linux-host", tailscale_ip="192.0.2.3",
                           updated="2026-08-18")
    assert record["host_id"] == "linux-host"
    assert record["tailscale_ip"] == "192.0.2.3"
    assert record["updated"] == "2026-08-18"
    assert record["engine"]["name"] == "parakeet"
    assert record["engine"]["measured_p50_ms"] == {"3s_clip": 212.0}
    assert record["engine"]["gpu_available"] is False

def test_build_record_gpu_slower_flag_true_when_all_bands_slower():
    record = build_record("parakeet", {"3s_clip": 212.0, "5s_clip": 320.0},
                           {"3s_clip": 216.0, "5s_clip": 324.0},
                           gpu_available=True,
                           gpu_p50={"3s_clip": 245.0, "5s_clip": 370.0},
                           gpu_p95={"3s_clip": 247.0, "5s_clip": 378.0})
    assert record["engine"]["gpu_provider_measured_slower"] is True

def test_build_record_gpu_slower_flag_false_when_gpu_faster():
    record = build_record("parakeet", {"3s_clip": 300.0}, {"3s_clip": 310.0},
                           gpu_available=True, gpu_p50={"3s_clip": 100.0},
                           gpu_p95={"3s_clip": 110.0})
    assert record["engine"]["gpu_provider_measured_slower"] is False

def test_build_record_default_durations_cover_all_bands():
    assert CLIP_DURATIONS_S == (3, 5, 8, 15)


@pytest.mark.integration
def test_main_refuses_to_overwrite_without_force(tmp_path, monkeypatch):
    """Integration-marked: exercises main()'s real load_settings()/speakeasy_dir(),
    which touch the real ~/.speakeasy -- kept out of the default fast run same as
    the existing `pytest -m integration` convention (needs no model download since
    it exits before ever constructing an engine)."""
    monkeypatch.setattr("speakeasy.bench.speakeasy_dir", lambda: tmp_path)
    (tmp_path / "host_settings.json").write_text('{"existing": true}')

    from speakeasy.bench import main
    with pytest.raises(SystemExit):
        main([])
    # untouched -- refused before doing anything
    assert json.loads((tmp_path / "host_settings.json").read_text()) == {"existing": True}
