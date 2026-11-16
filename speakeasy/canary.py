"""Production-readiness canaries (TSK-SPK-HOTFIX-LAUNCH).

Two probes:

1. `run_selftest()` — the in-app canary behind `speakeasy --canary`:
   exercises the frozen bundle's real dependency chain (settings, model
   files, engine init + a real transcription of the bundled sample,
   history SQLite round-trip, profile store) and reports permission state.
   Critical failures => ok=false / exit 1. Missing Accessibility is a
   loud WARNING but not a failure: since the degraded-startup fix the app
   still reaches the menu bar without it.

2. `write_heartbeat()` — written by __main__ once the rumps run loop is
   up (and refreshed periodically). scripts/canary.sh launches the .app
   through LaunchServices exactly like the Dock does and passes only when
   a fresh heartbeat appears — the probe that would have caught the
   original first-Dock-launch death.
"""
import json
import os
import time
import wave
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from speakeasy.deploy_profile import accounts_path as _deploy_accounts_path

_EXPECTED_WORDS = ("quick", "brown", "fox", "dog")


def write_heartbeat(path: Path, status: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(status, ts=time.time())
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)


def _load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def bundled_sample_wav():
    """Context manager yielding a real filesystem path to the bundled
    sample, zip-safe: in the py2app bundle `speakeasy` is imported from
    python3XY.zip, so `Path(__file__)` is not an openable path —
    importlib.resources extracts to a temp file when needed."""
    from importlib import resources

    return resources.as_file(resources.files("speakeasy.assets").joinpath("sample.wav"))


def run_selftest(
    base_dir: Optional[Path] = None,
    engine_factory: Optional[Callable] = None,
    sample_wav: Optional[Path] = None,
    accessibility_check: Optional[Callable[[], bool]] = None,
) -> dict:
    from speakeasy.config import load_settings, speakeasy_dir
    from speakeasy.training.profile import ProfileStore

    if base_dir is None:
        base_dir = speakeasy_dir()
    if engine_factory is None:
        from speakeasy.engine import create_engine
        engine_factory = create_engine
    if accessibility_check is None:
        from speakeasy.permissions import accessibility_trusted
        accessibility_check = accessibility_trusted

    checks: list[dict] = []

    def check(name: str, critical: bool, fn: Callable[[], str]) -> None:
        try:
            detail = fn() or "ok"
            checks.append({"name": name, "passed": True,
                           "critical": critical, "detail": detail})
        except Exception as exc:  # noqa: BLE001 — canary reports, never raises
            checks.append({"name": name, "passed": False,
                           "critical": critical, "detail": f"{type(exc).__name__}: {exc}"})

    engine_name = "parakeet"

    def _settings() -> str:
        nonlocal engine_name
        s = load_settings()
        engine_name = s.engine
        return f"engine={s.engine} hotkey={s.hotkey!r}"

    check("settings", True, _settings)

    model_dir = Path(base_dir) / "models"

    def _models() -> str:
        files = list(model_dir.rglob("*")) if model_dir.exists() else []
        if not files:
            raise FileNotFoundError(f"no model files under {model_dir}")
        return f"{len(files)} files under {model_dir}"

    check("models", True, _models)

    def _engine() -> str:
        eng = engine_factory(engine_name, model_dir)
        if sample_wav is not None:
            samples = _load_wav(Path(sample_wav))
        else:
            with bundled_sample_wav() as p:
                samples = _load_wav(Path(p))
        t0 = time.perf_counter()
        text = eng.transcribe(samples)
        ms = (time.perf_counter() - t0) * 1000.0
        got = sum(1 for w in _EXPECTED_WORDS if w in text.lower())
        if got < 3:
            raise AssertionError(
                f"transcript {text!r} matched only {got}/{len(_EXPECTED_WORDS)} expected words")
        return f"{text!r} in {ms:.0f}ms"

    check("engine_transcribe", True, _engine)

    def _history() -> str:
        from speakeasy.history import History
        db = Path(base_dir) / "run" / "canary-history.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        h = History(db)
        row_id = h.record("canary probe", "raw", "canary")
        h.update(row_id, status="canary")
        rows = h.recent(limit=1)
        if not rows:
            raise AssertionError("history round-trip returned no rows")
        return f"round-trip ok (row {row_id})"

    check("history_db", True, _history)

    def _profile() -> str:
        store = ProfileStore(Path(base_dir) / "profile")
        active = store.load_active()
        return f"active={active.version}" if active else "no active profile (ok)"

    check("profile_store", True, _profile)

    def _accessibility() -> str:
        if not accessibility_check():
            raise PermissionError(
                "Accessibility not granted — hotkey runs degraded until "
                "granted in System Settings > Privacy & Security > Accessibility")
        return "granted"

    check("accessibility", False, _accessibility)

    def _claude_cli() -> str:
        # P2-10: rewrite modes need the claude CLI resolvable from the APP's env
        # (Dock/LaunchServices PATH has no ~/.local/bin). Non-critical: raw works.
        from speakeasy.claude_cli import resolve_claude
        s = load_settings()
        exe = resolve_claude(s.claude_bin or None)
        if exe is None:
            raise FileNotFoundError("claude CLI not resolvable; rewrite modes fall back to raw")
        return exe

    check("claude_cli_resolvable", False, _claude_cli)

    warnings = [f"{c['name']}: {c['detail']}"
                for c in checks if not c["passed"] and not c["critical"]]
    ok = all(c["passed"] for c in checks if c["critical"])
    return {"ok": ok, "checks": checks, "warnings": warnings, "ts": time.time()}


def main() -> int:
    os.umask(0o077)
    report = run_selftest()
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


def pipeline_selftest(modes_to_run=("raw", "clean"), wav: Optional[Path] = None,
                      history_db: Optional[Path] = None) -> dict:
    """Non-interactive end-to-end run of the REAL dictation pipeline (Controller ->
    engine -> postprocess -> rewrite via the account pool -> history) on a WAV file,
    with injection replaced by a recorder (nothing is pasted, nothing takes focus).
    History goes to ~/.speakeasy/run/selftest-history.db, not the operator's history."""
    import os as _os
    from speakeasy.__main__ import _configured_rewrite
    from speakeasy.accounts import AccountPool
    from speakeasy.app import Controller
    from speakeasy.claude_cli import resolve_claude
    from speakeasy.config import load_settings, speakeasy_dir
    from speakeasy.engine import create_engine
    from speakeasy.history import History
    from speakeasy.modes import load_modes

    base = speakeasy_dir()
    settings = load_settings()
    if wav is not None:
        samples = _load_wav(Path(wav))
    else:
        with bundled_sample_wav() as p:
            samples = _load_wav(Path(p))
    engine = create_engine(settings.engine, base / "models")
    modes = load_modes(base / "modes.yaml")
    pool = AccountPool(_deploy_accounts_path(), base / "state.json",
                       allowlist=settings.account_allowlist or None)
    db = Path(history_db) if history_db else base / "run" / "selftest-history.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    history = History(db)

    class _Rec:
        def __init__(self, s): self._s = s
        def start(self): pass
        def stop(self): return self._s
        def snapshot(self): return self._s
        level = 0.0

    report = {"claude_bin": resolve_claude(settings.claude_bin or None),
              "path_env": _os.environ.get("PATH", ""),
              "rewrite_timeout_s": settings.rewrite_timeout_s,
              "history_db": str(db), "runs": []}
    for name in modes_to_run:
        injected: list[str] = []
        renders: list[tuple] = []
        run_settings = load_settings()
        run_settings.active_mode = name
        c = Controller(engine=engine, recorder=_Rec(samples), history=history,
                       modes=modes, pool=pool, settings=run_settings,
                       overlay_render=lambda m, _r=renders: _r.append((m.state, m.message)),
                       inject_fn=lambda t, _i=injected: _i.append(t) or "injected",
                       rewrite_fn=_configured_rewrite(run_settings),
                       audio_dir=base / "run" / "selftest-audio",
                       run_async=False, frontmost=lambda: "selftest")
        t0 = time.perf_counter()
        c.toggle()
        c.toggle()
        row = history.recent(1)[0]
        report["runs"].append({
            "mode": name, "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "injected": injected, "renders": renders,
            "row": {k: row.get(k) for k in ("id", "raw", "mode", "rewritten", "account",
                                             "latency_ms", "status", "error", "audio_path")}})
    report["ok"] = all(r["injected"] and r["row"]["status"] not in ("pending", "transcribing")
                       for r in report["runs"])
    return report


def pipeline_main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="speakeasy --pipeline-selftest")
    ap.add_argument("--mode", action="append", dest="modes")
    ap.add_argument("--wav", type=Path)
    a = ap.parse_args(argv)
    report = pipeline_selftest(tuple(a.modes or ("raw", "clean")), wav=a.wav)
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1
