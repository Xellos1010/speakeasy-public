"""TSK-SPK-HOTFIX-LAUNCH: the packaged app must never die before the menu
bar loads because a permission is missing (root cause: PermissionError from
HotkeyListener.start() at __main__ line ~114, raised before app.run(), left
the user staring at py2app's "Terminate" error dialog on first Dock launch).

Covers: degraded hotkey startup, the run-loop heartbeat the launch canary
probes, and the --canary self-test used to prove production readiness.
"""
import json
import time
from pathlib import Path

import numpy as np
import pytest

from speakeasy.canary import run_selftest, write_heartbeat
from speakeasy.permissions import start_hotkey_with_degradation


class FakeListener:
    def __init__(self, exc=None):
        self.exc = exc
        self.started = False

    def start(self):
        if self.exc is not None:
            raise self.exc
        self.started = True


class TestStartHotkeyWithDegradation:
    def test_ok_path_starts_and_returns_true(self):
        listener = FakeListener()
        calls = []
        assert start_hotkey_with_degradation(listener, calls.append) is True
        assert listener.started
        assert calls == []

    def test_permission_error_degrades_instead_of_raising(self):
        listener = FakeListener(PermissionError("grant Accessibility"))
        calls = []
        assert start_hotkey_with_degradation(listener, calls.append) is False
        assert not listener.started
        assert len(calls) == 1 and "Accessibility" in calls[0]

    def test_unexpected_errors_still_propagate(self):
        # Only the known permission failure is degraded; real bugs must not
        # be silently swallowed into "degraded mode".
        listener = FakeListener(RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            start_hotkey_with_degradation(listener, lambda m: None)


class TestHeartbeat:
    def test_writes_json_with_ts_and_status(self, tmp_path):
        p = tmp_path / "run" / "heartbeat.json"
        write_heartbeat(p, {"hotkey_active": False, "version": "test"})
        d = json.loads(p.read_text())
        assert d["hotkey_active"] is False
        assert d["version"] == "test"
        assert isinstance(d["ts"], float) and d["ts"] <= time.time()

    def test_rewrite_updates_content(self, tmp_path):
        p = tmp_path / "heartbeat.json"
        write_heartbeat(p, {"hotkey_active": False})
        write_heartbeat(p, {"hotkey_active": True})
        assert json.loads(p.read_text())["hotkey_active"] is True


class _StubEngine:
    def __init__(self, text="the quick brown fox jumps over the lazy dog"):
        self.text = text

    def transcribe(self, samples):
        return self.text


def _selftest_kwargs(tmp_path, engine_text="the quick brown fox jumps over the lazy dog"):
    wav = tmp_path / "sample.wav"
    import wave

    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((np.zeros(1600, dtype=np.int16)).tobytes())
    models = tmp_path / "models"
    models.mkdir()
    (models / "tokens.txt").write_text("x")
    return dict(
        base_dir=tmp_path,
        engine_factory=lambda name, model_dir: _StubEngine(engine_text),
        sample_wav=wav,
        accessibility_check=lambda: True,
    )


class TestRunSelftest:
    def test_all_green(self, tmp_path):
        report = run_selftest(**_selftest_kwargs(tmp_path))
        assert report["ok"] is True
        names = {c["name"] for c in report["checks"]}
        assert {"settings", "models", "engine_transcribe", "history_db",
                "profile_store", "accessibility"} <= names
        assert all(c["passed"] for c in report["checks"] if c["critical"])

    def test_engine_failure_fails_canary(self, tmp_path):
        kw = _selftest_kwargs(tmp_path)

        def broken(name, model_dir):
            raise RuntimeError("engine init failed")

        kw["engine_factory"] = broken
        report = run_selftest(**kw)
        assert report["ok"] is False
        eng = next(c for c in report["checks"] if c["name"] == "engine_transcribe")
        assert eng["passed"] is False and "engine init failed" in eng["detail"]

    def test_wrong_transcript_fails(self, tmp_path):
        report = run_selftest(**_selftest_kwargs(tmp_path, engine_text="total nonsense"))
        assert report["ok"] is False

    def test_missing_accessibility_is_warning_not_failure(self, tmp_path):
        kw = _selftest_kwargs(tmp_path)
        kw["accessibility_check"] = lambda: False
        report = run_selftest(**kw)
        acc = next(c for c in report["checks"] if c["name"] == "accessibility")
        assert acc["passed"] is False and acc["critical"] is False
        # ok reflects CRITICAL checks only: the app now degrades gracefully
        # without Accessibility, so the canary flags it loudly but does not
        # call the build unshippable.
        assert report["ok"] is True
        assert any("accessibility" in w.lower() for w in report["warnings"])


class TestBundledSample:
    def test_bundled_sample_resolves_zip_safely(self):
        # importlib.resources path: must work whether speakeasy is a real
        # directory (dev) or lives inside py2app's python3XY.zip (frozen).
        from speakeasy.canary import bundled_sample_wav
        with bundled_sample_wav() as p:
            assert Path(p).exists() and Path(p).stat().st_size > 1000
