# speakeasy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fully-local menu-bar dictation app: toggle hotkey → record → Parakeet STT → optional claude-headless rewrite → inject text into the focused field of any macOS app.

**Architecture:** Single Python process. Pure-logic modules (history, modes, accounts, rewrite, controller state machine) are unit-tested with fakes; macOS-bound modules (audio, hotkey, overlay, injector) keep their decision logic in testable functions and their OS calls thin. AppKit runs on the main thread; recording/transcription on worker threads.

**Tech Stack:** Python 3.11+, sherpa-onnx (Parakeet-TDT 0.6B int8 + whisper small.en fallback), sounddevice/PortAudio, pyobjc (Cocoa+Quartz), rumps, PyYAML, SQLite, pytest.

## Global Constraints

- Target machine: Intel i9 Mac, 16 GB RAM, macOS 26 — CPU inference only, idle RSS < 500 MB.
- All user state under `~/.speakeasy/` (db, models, state.json, modes.yaml). Never hardcode a home-directory path — use `Path.home()`.
- History row is written **before** any injection attempt; a dictation must never be lost (reversible/observable rule).
- Rewrite backend is headless `claude -p` with `CLAUDE_CONFIG_DIR` account rotation; accounts from `~/.claude-accounts.json`; skip `*-elevated` accounts unless allowlisted; default model `claude-haiku-4-5-20251001`.
- Raw mode must work with zero network.
- Conventional Commits; commit after every green task.
- Repo root: the checkout directory. Run everything from repo root with `.venv` activated.

---

### Task 1: Scaffold + toolchain

**Files:**
- Create: `pyproject.toml`, `speakeasy/__init__.py`, `tests/test_smoke.py`

**Interfaces:**
- Produces: importable `speakeasy` package; `.venv` with all deps; `pytest` green.

- [ ] **Step 1: System deps + venv**

```bash
brew list portaudio >/dev/null 2>&1 || brew install portaudio
python3 --version   # need 3.11+; if not, brew install python@3.12 and use python3.12
python3 -m venv .venv && source .venv/bin/activate
```

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "speakeasy"
version = "0.1.0"
description = "Local dictation with universal text injection"
requires-python = ">=3.11"
dependencies = [
  "sherpa-onnx>=1.10",
  "sounddevice>=0.4.6",
  "numpy>=1.26",
  "rumps>=0.4",
  "pyobjc-framework-Cocoa>=10.0",
  "pyobjc-framework-Quartz>=10.0",
  "PyYAML>=6.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[tool.pytest.ini_options]
markers = ["integration: needs model files / audio hardware"]
addopts = "-m 'not integration'"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools]
packages = ["speakeasy", "speakeasy.engine"]
```

- [ ] **Step 3: Create package + smoke test**

`speakeasy/__init__.py`: `__version__ = "0.1.0"`

`tests/test_smoke.py`:
```python
import speakeasy

def test_import():
    assert speakeasy.__version__ == "0.1.0"
```

- [ ] **Step 4: Install and verify**

Run: `pip install -e '.[dev]' && python -c "import sherpa_onnx, sounddevice, rumps, Quartz; print('ok')" && pytest -q`
Expected: `ok`, `1 passed`. If sherpa-onnx has no macOS x86_64 wheel for the pinned version, relax the floor (`sherpa-onnx>=1.9`) until one installs, and note the chosen version in the commit message.

- [ ] **Step 5: Commit** — `chore: scaffold python package + deps`

---

### Task 2: config.py — settings

**Files:**
- Create: `speakeasy/config.py`, `tests/test_config.py`

**Interfaces:**
- Produces: `Settings` dataclass; `load_settings(dir: Path|None=None) -> Settings`; `save_settings(s: Settings, dir: Path|None=None) -> None`; `speakeasy_dir() -> Path` (`~/.speakeasy`, created on demand).

- [ ] **Step 1: Failing test**

`tests/test_config.py`:
```python
from pathlib import Path
from speakeasy.config import Settings, load_settings, save_settings

def test_defaults(tmp_path: Path):
    s = load_settings(tmp_path)          # no file yet -> defaults
    assert s.hotkey_keycode == 96        # F5
    assert s.engine == "parakeet"
    assert s.active_mode == "raw"
    assert s.account_allowlist == []

def test_roundtrip(tmp_path: Path):
    s = load_settings(tmp_path)
    s.active_mode = "clean"
    save_settings(s, tmp_path)
    assert load_settings(tmp_path).active_mode == "clean"
```

- [ ] **Step 2: Run** `pytest tests/test_config.py -q` — expect ImportError FAIL.

- [ ] **Step 3: Implement `speakeasy/config.py`**

```python
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

@dataclass
class Settings:
    hotkey_keycode: int = 96          # F5
    engine: str = "parakeet"
    input_device: int | None = None
    active_mode: str = "raw"
    account_allowlist: list[str] = field(default_factory=list)

def speakeasy_dir() -> Path:
    d = Path.home() / ".speakeasy"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _settings_path(dir: Path | None) -> Path:
    return (dir or speakeasy_dir()) / "settings.json"

def load_settings(dir: Path | None = None) -> Settings:
    p = _settings_path(dir)
    if not p.exists():
        return Settings()
    data = json.loads(p.read_text())
    known = {f for f in Settings.__dataclass_fields__}
    return Settings(**{k: v for k, v in data.items() if k in known})

def save_settings(s: Settings, dir: Path | None = None) -> None:
    _settings_path(dir).write_text(json.dumps(asdict(s), indent=2))
```

- [ ] **Step 4: Run** `pytest tests/test_config.py -q` — expect PASS.
- [ ] **Step 5: Commit** — `feat: settings load/save`

---

### Task 3: history.py — SQLite store

**Files:**
- Create: `speakeasy/history.py`, `tests/test_history.py`

**Interfaces:**
- Produces: `class History(db_path: Path)` with `record(raw: str, mode: str, target_app: str | None) -> int` (status `'pending'`), `update(row_id: int, *, status: str, rewritten: str | None = None, account: str | None = None, latency_ms: int | None = None) -> None`, `recent(limit: int = 10) -> list[dict]` (newest first, dict keys = column names). Valid statuses: `pending, injected, clipboard_only, rewrite_failed, discarded`.

- [ ] **Step 1: Failing test**

`tests/test_history.py`:
```python
from speakeasy.history import History

def test_record_before_injection(tmp_path):
    h = History(tmp_path / "h.db")
    rid = h.record("hello world", "raw", "TextEdit")
    rows = h.recent()
    assert rows[0]["id"] == rid and rows[0]["status"] == "pending"
    assert rows[0]["raw"] == "hello world"

def test_update_and_order(tmp_path):
    h = History(tmp_path / "h.db")
    a = h.record("first", "raw", None)
    b = h.record("second", "email", None)
    h.update(b, status="injected", rewritten="Dear team, second", account="acct-c", latency_ms=900)
    rows = h.recent(limit=2)
    assert [r["id"] for r in rows] == [b, a]
    assert rows[0]["rewritten"] == "Dear team, second"
    assert rows[0]["account"] == "acct-c"
```

- [ ] **Step 2: Run** `pytest tests/test_history.py -q` — FAIL.

- [ ] **Step 3: Implement `speakeasy/history.py`**

```python
import sqlite3, time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dictations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  raw TEXT NOT NULL,
  mode TEXT NOT NULL,
  target_app TEXT,
  rewritten TEXT,
  account TEXT,
  latency_ms INTEGER,
  status TEXT NOT NULL DEFAULT 'pending'
);
"""

class History:
    def __init__(self, db_path: Path):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def record(self, raw: str, mode: str, target_app: str | None) -> int:
        cur = self._conn.execute(
            "INSERT INTO dictations (ts, raw, mode, target_app) VALUES (?,?,?,?)",
            (time.time(), raw, mode, target_app))
        self._conn.commit()
        return cur.lastrowid

    def update(self, row_id: int, *, status: str, rewritten: str | None = None,
               account: str | None = None, latency_ms: int | None = None) -> None:
        self._conn.execute(
            "UPDATE dictations SET status=?, rewritten=COALESCE(?,rewritten),"
            " account=COALESCE(?,account), latency_ms=COALESCE(?,latency_ms) WHERE id=?",
            (status, rewritten, account, latency_ms, row_id))
        self._conn.commit()

    def recent(self, limit: int = 10) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM dictations ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 4: Run** `pytest tests/test_history.py -q` — PASS.
- [ ] **Step 5: Commit** — `feat: sqlite dictation history`

---

### Task 4: modes.py — mode config

**Files:**
- Create: `speakeasy/modes.py`, `tests/test_modes.py`

**Interfaces:**
- Produces: `@dataclass Mode(name: str, system_prompt: str | None, model: str = "claude-haiku-4-5-20251001")` (`system_prompt is None` ⇒ raw/no-LLM); `load_modes(path: Path | None = None) -> dict[str, Mode]` — merges built-in defaults (`raw, clean, email, prompt-polish`) with user YAML overrides; `'raw'` always present and always `system_prompt=None`.

- [ ] **Step 1: Failing test**

`tests/test_modes.py`:
```python
from speakeasy.modes import load_modes, Mode

def test_builtins_present_without_file():
    m = load_modes(None)
    assert set(m) >= {"raw", "clean", "email", "prompt-polish"}
    assert m["raw"].system_prompt is None
    assert m["clean"].model.startswith("claude-haiku")

def test_user_yaml_overrides_and_adds(tmp_path):
    p = tmp_path / "modes.yaml"
    p.write_text(
        "clean:\n  model: claude-sonnet-5\n"
        "jira:\n  system_prompt: Rewrite as a JIRA comment.\n")
    m = load_modes(p)
    assert m["clean"].model == "claude-sonnet-5"
    assert m["jira"].system_prompt.startswith("Rewrite")
    assert m["raw"].system_prompt is None   # raw not overridable
```

- [ ] **Step 2: Run** `pytest tests/test_modes.py -q` — FAIL.

- [ ] **Step 3: Implement `speakeasy/modes.py`**

```python
from dataclasses import dataclass, replace
from pathlib import Path
import yaml

DEFAULT_MODEL = "claude-haiku-4-5-20251001"

@dataclass
class Mode:
    name: str
    system_prompt: str | None
    model: str = DEFAULT_MODEL

_BUILTINS: dict[str, str | None] = {
    "raw": None,
    "clean": ("You clean up dictated text. Fix punctuation, casing, and obvious "
              "speech artifacts (um, uh, repeated words). Preserve the speaker's words "
              "and meaning. Output only the cleaned text."),
    "email": ("Rewrite the dictated text as a clear, professional email body. "
              "Keep it concise and friendly. Output only the email text."),
    "prompt-polish": ("Rewrite the dictated text as a precise, well-structured prompt "
                      "for an AI coding assistant. Output only the prompt."),
}

def load_modes(path: Path | None = None) -> dict[str, Mode]:
    modes = {n: Mode(n, sp) for n, sp in _BUILTINS.items()}
    if path is not None and Path(path).exists():
        data = yaml.safe_load(Path(path).read_text()) or {}
        for name, cfg in data.items():
            if name == "raw" or not isinstance(cfg, dict):
                continue
            base = modes.get(name, Mode(name, None))
            modes[name] = replace(
                base,
                system_prompt=cfg.get("system_prompt", base.system_prompt),
                model=cfg.get("model", base.model))
    return modes
```

- [ ] **Step 4: Run** `pytest tests/test_modes.py -q` — PASS.
- [ ] **Step 5: Commit** — `feat: rewrite modes config with builtin defaults`

---

### Task 5: accounts.py — round-robin pool

**Files:**
- Create: `speakeasy/accounts.py`, `tests/test_accounts.py`

**Interfaces:**
- Consumes: `~/.claude-accounts.json` shape `{"accounts": {"<name>": {"dir": "<path>", "alias": "..."}}}`.
- Produces: `@dataclass Account(name: str, dir: Path)`; `class AccountPool(accounts_path: Path, state_path: Path, allowlist: list[str] | None = None, now: Callable[[], float] = time.time)` with `next_account() -> Account | None` (round-robin, skips cooling + `*-elevated` unless allowlisted + dirs that don't exist; `None` if none eligible) and `mark_cooling(name: str, seconds: float = 3600.0) -> None`. Cursor + cooling persisted as JSON at `state_path`.

- [ ] **Step 1: Failing test**

`tests/test_accounts.py`:
```python
import json
from speakeasy.accounts import AccountPool

def make_accounts(tmp_path, names):
    accs = {}
    for n in names:
        d = tmp_path / n
        d.mkdir()
        accs[n] = {"dir": str(d), "alias": f"claude-{n}"}
    p = tmp_path / "accounts.json"
    p.write_text(json.dumps({"accounts": accs}))
    return p

def test_round_robin_skips_elevated(tmp_path):
    p = make_accounts(tmp_path, ["acct-a", "acct-b", "acct-c-elevated"])
    pool = AccountPool(p, tmp_path / "state.json")
    names = [pool.next_account().name for _ in range(4)]
    assert names == ["acct-a", "acct-b", "acct-a", "acct-b"]

def test_cooling_and_exhaustion(tmp_path):
    p = make_accounts(tmp_path, ["acct-a", "acct-b"])
    t = [1000.0]
    pool = AccountPool(p, tmp_path / "state.json", now=lambda: t[0])
    pool.mark_cooling("acct-a")
    assert pool.next_account().name == "acct-b"
    pool.mark_cooling("acct-b")
    assert pool.next_account() is None
    t[0] += 3601
    assert pool.next_account() is not None

def test_cursor_persists(tmp_path):
    p = make_accounts(tmp_path, ["acct-a", "acct-b"])
    s = tmp_path / "state.json"
    assert AccountPool(p, s).next_account().name == "acct-a"
    assert AccountPool(p, s).next_account().name == "acct-b"
```

- [ ] **Step 2: Run** `pytest tests/test_accounts.py -q` — FAIL.

- [ ] **Step 3: Implement `speakeasy/accounts.py`**

```python
import json, time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

@dataclass
class Account:
    name: str
    dir: Path

class AccountPool:
    def __init__(self, accounts_path: Path, state_path: Path,
                 allowlist: list[str] | None = None,
                 now: Callable[[], float] = time.time):
        self._state_path = state_path
        self._now = now
        data = json.loads(Path(accounts_path).read_text())["accounts"]
        allow = set(allowlist or [])
        self._accounts = [
            Account(name, Path(cfg["dir"]))
            for name, cfg in data.items()
            if Path(cfg["dir"]).exists()
            and (not name.endswith("-elevated") or name in allow)
            and (not allow or name in allow)
        ]
        self._state = {"cursor": 0, "cooling": {}}
        if state_path.exists():
            self._state.update(json.loads(state_path.read_text()))

    def _save(self) -> None:
        self._state_path.write_text(json.dumps(self._state))

    def next_account(self) -> Account | None:
        n = len(self._accounts)
        if n == 0:
            return None
        now = self._now()
        cooling = self._state["cooling"]
        for i in range(n):
            idx = (self._state["cursor"] + i) % n
            acc = self._accounts[idx]
            if cooling.get(acc.name, 0) > now:
                continue
            self._state["cursor"] = (idx + 1) % n
            self._save()
            return acc
        return None

    def mark_cooling(self, name: str, seconds: float = 3600.0) -> None:
        self._state["cooling"][name] = self._now() + seconds
        self._save()
```

- [ ] **Step 4: Run** `pytest tests/test_accounts.py -q` — PASS.
- [ ] **Step 5: Commit** — `feat: account round-robin pool with cooling`

---

### Task 6: rewrite.py — claude headless service

**Files:**
- Create: `speakeasy/rewrite.py`, `tests/test_rewrite.py`

**Interfaces:**
- Consumes: `Mode` (Task 4), `AccountPool`/`Account` (Task 5).
- Produces: `class RewriteError(Exception)`; `@dataclass RewriteResult(text: str, account: str, latency_ms: int)`; `rewrite(transcript: str, mode: Mode, pool: AccountPool, runner=subprocess.run, timeout: float = 20.0, max_attempts: int = 4) -> RewriteResult`.
- Behavior: builds `claude -p <full prompt> --model <mode.model> --output-format text` with `env={**os.environ, "CLAUDE_CONFIG_DIR": str(acc.dir)}`; prompt = `mode.system_prompt + "\n\n<dictation>\n" + transcript + "\n</dictation>"`. Non-zero exit or `subprocess.TimeoutExpired` ⇒ `pool.mark_cooling(acc.name)` and try next account, up to `max_attempts`; no account / attempts exhausted ⇒ `RewriteError`.

- [ ] **Step 1: Failing test**

`tests/test_rewrite.py`:
```python
import subprocess
import pytest
from speakeasy.modes import Mode
from speakeasy.rewrite import rewrite, RewriteError

class FakePool:
    def __init__(self, names):
        self.names = list(names); self.cooled = []
    def next_account(self):
        from speakeasy.accounts import Account
        from pathlib import Path
        if not self.names: return None
        return Account(self.names.pop(0), Path("/tmp"))
    def mark_cooling(self, name, seconds=3600.0):
        self.cooled.append(name)

MODE = Mode("clean", "Clean this up.", "claude-haiku-4-5-20251001")

def ok_runner(cmd, **kw):
    assert "--model" in cmd and "claude-haiku-4-5-20251001" in cmd
    assert kw["env"]["CLAUDE_CONFIG_DIR"].endswith("/tmp")
    return subprocess.CompletedProcess(cmd, 0, stdout="Cleaned text.", stderr="")

def test_success_first_account():
    r = rewrite("um hello", MODE, FakePool(["acct-a"]), runner=ok_runner)
    assert r.text == "Cleaned text." and r.account == "acct-a"

def test_rotates_on_failure():
    calls = []
    def flaky(cmd, **kw):
        calls.append(kw["env"]["CLAUDE_CONFIG_DIR"])
        code = 1 if len(calls) == 1 else 0
        return subprocess.CompletedProcess(cmd, code, stdout="ok", stderr="usage limit")
    pool = FakePool(["acct-a", "acct-b"])
    r = rewrite("hi", MODE, pool, runner=flaky)
    assert r.account == "acct-b" and pool.cooled == ["acct-a"]

def test_exhaustion_raises():
    def dead(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="limit")
    with pytest.raises(RewriteError):
        rewrite("hi", MODE, FakePool(["acct-a"]), runner=dead)
```

- [ ] **Step 2: Run** `pytest tests/test_rewrite.py -q` — FAIL.

- [ ] **Step 3: Implement `speakeasy/rewrite.py`**

```python
import os, subprocess, time
from dataclasses import dataclass
from speakeasy.modes import Mode

class RewriteError(Exception):
    pass

@dataclass
class RewriteResult:
    text: str
    account: str
    latency_ms: int

def rewrite(transcript: str, mode: Mode, pool, runner=subprocess.run,
            timeout: float = 20.0, max_attempts: int = 4) -> RewriteResult:
    if mode.system_prompt is None:
        return RewriteResult(transcript, "none", 0)
    prompt = f"{mode.system_prompt}\n\n<dictation>\n{transcript}\n</dictation>"
    last_err = "no eligible account"
    for _ in range(max_attempts):
        acc = pool.next_account()
        if acc is None:
            break
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(acc.dir)}
        cmd = ["claude", "-p", prompt, "--model", mode.model, "--output-format", "text"]
        t0 = time.monotonic()
        try:
            proc = runner(cmd, env=env, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            pool.mark_cooling(acc.name)
            last_err = f"{acc.name}: timeout"
            continue
        if proc.returncode != 0:
            pool.mark_cooling(acc.name)
            last_err = f"{acc.name}: exit {proc.returncode}: {proc.stderr[:200]}"
            continue
        return RewriteResult(proc.stdout.strip(), acc.name,
                             int((time.monotonic() - t0) * 1000))
    raise RewriteError(last_err)
```

- [ ] **Step 4: Run** `pytest tests/test_rewrite.py -q` — PASS.
- [ ] **Step 5: Commit** — `feat: claude headless rewrite with account rotation`

---

### Task 7: engine package — downloader + Parakeet + whisper fallback

**Files:**
- Create: `speakeasy/engine/__init__.py`, `speakeasy/engine/download.py`, `speakeasy/engine/parakeet.py`, `speakeasy/engine/whisper_onnx.py`, `tests/test_download.py`, `tests/test_engine_integration.py`

**Interfaces:**
- Produces: `Engine` protocol with `transcribe(samples: np.ndarray, sample_rate: int = 16000) -> str` (samples float32 mono −1..1); `create_engine(name: str, models_dir: Path) -> Engine` for `"parakeet" | "whisper"`; `ensure_model(name: str, models_dir: Path, fetch=urllib.request.urlretrieve) -> Path` (download tar.bz2 → extract → return model dir; skip if present).

- [ ] **Step 1: Verify model asset URLs (adjust registry if 404)**

```bash
curl -sIL -o /dev/null -w '%{http_code}\n' https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2
curl -sIL -o /dev/null -w '%{http_code}\n' https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-whisper-small.en.tar.bz2
```
Both verified `200` on 2026-07-18 by the coordinator (whisper asset carries int8 onnx files inside the plain archive; use `*int8*.onnx` glob preferentially in WhisperEngine, falling back to non-int8).

- [ ] **Step 2: Failing downloader test (uses a local fixture tarball, no network)**

`tests/test_download.py`:
```python
import tarfile
from pathlib import Path
from speakeasy.engine.download import ensure_model, MODELS

def make_tarball(tmp_path, dirname):
    src = tmp_path / dirname
    src.mkdir()
    (src / "tokens.txt").write_text("a 1\n")
    tb = tmp_path / f"{dirname}.tar.bz2"
    with tarfile.open(tb, "w:bz2") as t:
        t.add(src, arcname=dirname)
    return tb

def test_ensure_model_extracts_and_is_idempotent(tmp_path):
    dirname = MODELS["parakeet"]["dirname"]
    tb = make_tarball(tmp_path, dirname)
    fetched = []
    def fake_fetch(url, dest):
        fetched.append(url)
        Path(dest).write_bytes(tb.read_bytes())
    models = tmp_path / "models"
    d1 = ensure_model("parakeet", models, fetch=fake_fetch)
    d2 = ensure_model("parakeet", models, fetch=fake_fetch)
    assert d1 == d2 == models / dirname
    assert (d1 / "tokens.txt").exists()
    assert len(fetched) == 1
```

- [ ] **Step 3: Run** `pytest tests/test_download.py -q` — FAIL.

- [ ] **Step 4: Implement `speakeasy/engine/download.py`**

```python
import tarfile, tempfile, urllib.request
from pathlib import Path

_BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"

MODELS = {
    "parakeet": {
        "dirname": "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8",
        "url": _BASE + "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2",
    },
    "whisper": {
        "dirname": "sherpa-onnx-whisper-small.en",
        "url": _BASE + "sherpa-onnx-whisper-small.en.tar.bz2",
    },
}

def ensure_model(name: str, models_dir: Path,
                 fetch=urllib.request.urlretrieve) -> Path:
    spec = MODELS[name]
    target = models_dir / spec["dirname"]
    if target.exists():
        return target
    models_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".tar.bz2", delete=False) as tmp:
        pass
    fetch(spec["url"], tmp.name)
    with tarfile.open(tmp.name, "r:bz2") as t:
        t.extractall(models_dir, filter="data")
    if not target.exists():
        raise FileNotFoundError(f"archive did not contain {spec['dirname']}")
    return target
```
(If Step 1 changed asset names, update `MODELS` dirname/url pairs accordingly.)

- [ ] **Step 5: Run** `pytest tests/test_download.py -q` — PASS. Commit — `feat: model downloader`.

- [ ] **Step 6: Implement engines**

`speakeasy/engine/parakeet.py`:
```python
from pathlib import Path
import numpy as np
import sherpa_onnx

class ParakeetEngine:
    def __init__(self, model_dir: Path, num_threads: int = 4):
        d = Path(model_dir)
        enc = next(d.glob("encoder*.onnx"))
        dec = next(d.glob("decoder*.onnx"))
        joi = next(d.glob("joiner*.onnx"))
        self._rec = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(enc), decoder=str(dec), joiner=str(joi),
            tokens=str(d / "tokens.txt"),
            num_threads=num_threads, model_type="nemo_transducer")

    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> str:
        s = self._rec.create_stream()
        s.accept_waveform(sample_rate, samples)
        self._rec.decode_stream(s)
        return s.result.text.strip()
```

`speakeasy/engine/whisper_onnx.py`:
```python
from pathlib import Path
import numpy as np
import sherpa_onnx

class WhisperEngine:
    def __init__(self, model_dir: Path, num_threads: int = 4):
        d = Path(model_dir)
        self._rec = sherpa_onnx.OfflineRecognizer.from_whisper(
            encoder=str(next(d.glob("*encoder*.onnx"))),
            decoder=str(next(d.glob("*decoder*.onnx"))),
            tokens=str(next(d.glob("*tokens*.txt"))),
            num_threads=num_threads, language="en", task="transcribe")

    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> str:
        s = self._rec.create_stream()
        s.accept_waveform(sample_rate, samples)
        self._rec.decode_stream(s)
        return s.result.text.strip()
```

`speakeasy/engine/__init__.py`:
```python
from pathlib import Path
from typing import Protocol
import numpy as np
from speakeasy.engine.download import ensure_model

class Engine(Protocol):
    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> str: ...

def create_engine(name: str, models_dir: Path) -> Engine:
    model_dir = ensure_model(name, models_dir)
    if name == "parakeet":
        from speakeasy.engine.parakeet import ParakeetEngine
        return ParakeetEngine(model_dir)
    if name == "whisper":
        from speakeasy.engine.whisper_onnx import WhisperEngine
        return WhisperEngine(model_dir)
    raise ValueError(f"unknown engine {name!r}")
```
If `from_transducer`/`from_whisper` keyword names don't match the installed sherpa-onnx version, check `python -c "import sherpa_onnx, inspect; print(inspect.signature(sherpa_onnx.OfflineRecognizer.from_transducer))"` and adapt — keep the class interfaces unchanged.

- [ ] **Step 7: Integration test with synthesized speech**

```bash
mkdir -p tests/assets
say -v Samantha -o tests/assets/sample.aiff "The quick brown fox jumps over the lazy dog"
afconvert -f WAVE -d LEI16@16000 -c 1 tests/assets/sample.aiff tests/assets/sample.wav
```

`tests/test_engine_integration.py`:
```python
import re, wave
from pathlib import Path
import numpy as np
import pytest
from speakeasy.engine import create_engine

EXPECTED = "the quick brown fox jumps over the lazy dog"

def load_wav(p: Path) -> np.ndarray:
    with wave.open(str(p), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

def norm(s: str) -> str:
    return re.sub(r"[^a-z ]", "", s.lower()).strip()

@pytest.mark.integration
def test_parakeet_transcribes_sample():
    eng = create_engine("parakeet", Path.home() / ".speakeasy" / "models")
    text = norm(eng.transcribe(load_wav(Path("tests/assets/sample.wav"))))
    missing = [w for w in EXPECTED.split() if w not in text]
    assert len(missing) <= 1, f"missing {missing} in {text!r}"
```

- [ ] **Step 8: Run** `pytest tests/test_engine_integration.py -m integration -q` — PASS (first run downloads ~650 MB; allow time). Then `pytest -q` still green.
- [ ] **Step 9: Commit** — `feat: parakeet + whisper engines with integration proof` (include real transcription output in commit body).

---

### Task 8: audio.py — recorder

**Files:**
- Create: `speakeasy/audio.py`, `tests/test_audio.py`

**Interfaces:**
- Produces: `class Recorder(samplerate: int = 16000, device: int | None = None, stream_factory=None)` with `start()`, `stop() -> np.ndarray` (float32 mono, everything since start), `snapshot() -> np.ndarray` (buffer so far), `level: float` property (RMS of last block, 0..1). `stream_factory(callback) -> stream` defaults to a `sounddevice.InputStream` wrapper; tests inject a fake.

- [ ] **Step 1: Failing test**

`tests/test_audio.py`:
```python
import numpy as np
from speakeasy.audio import Recorder

class FakeStream:
    def __init__(self, callback): self.callback = callback; self.active = False
    def start(self): self.active = True
    def stop(self): self.active = False
    def close(self): pass

def test_capture_and_snapshot():
    holder = {}
    def factory(callback):
        holder["s"] = FakeStream(callback); return holder["s"]
    r = Recorder(stream_factory=factory)
    r.start()
    block = np.ones((1600, 1), dtype=np.float32) * 0.5
    holder["s"].callback(block, 1600, None, None)
    holder["s"].callback(block, 1600, None, None)
    assert len(r.snapshot()) == 3200
    assert 0.4 < r.level <= 0.6
    out = r.stop()
    assert len(out) == 3200 and out.dtype == np.float32

def test_stop_resets():
    holder = {}
    r = Recorder(stream_factory=lambda cb: holder.setdefault("s", FakeStream(cb)))
    r.start()
    holder["s"].callback(np.zeros((160, 1), np.float32), 160, None, None)
    r.stop()
    r.start()
    assert len(r.snapshot()) == 0
```

- [ ] **Step 2: Run** `pytest tests/test_audio.py -q` — FAIL.

- [ ] **Step 3: Implement `speakeasy/audio.py`**

```python
import threading
import numpy as np

class Recorder:
    def __init__(self, samplerate: int = 16000, device: int | None = None,
                 stream_factory=None):
        self.samplerate = samplerate
        self._device = device
        self._factory = stream_factory or self._default_factory
        self._lock = threading.Lock()
        self._chunks: list[np.ndarray] = []
        self._level = 0.0
        self._stream = None

    def _default_factory(self, callback):
        import sounddevice as sd
        return sd.InputStream(samplerate=self.samplerate, channels=1,
                              dtype="float32", device=self._device,
                              callback=callback)

    def _callback(self, indata, frames, time_info, status):
        mono = np.asarray(indata, dtype=np.float32).reshape(-1)
        with self._lock:
            self._chunks.append(mono.copy())
            self._level = float(np.sqrt(np.mean(mono ** 2)))

    def start(self) -> None:
        with self._lock:
            self._chunks = []
            self._level = 0.0
        self._stream = self._factory(self._callback)
        self._stream.start()

    def snapshot(self) -> np.ndarray:
        with self._lock:
            if not self._chunks:
                return np.zeros(0, dtype=np.float32)
            return np.concatenate(self._chunks)

    def stop(self) -> np.ndarray:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        return self.snapshot()

    @property
    def level(self) -> float:
        return self._level
```

- [ ] **Step 4: Run** `pytest tests/test_audio.py -q` — PASS.
- [ ] **Step 5: Commit** — `feat: mic recorder with injectable stream`

---

### Task 9: injector.py — universal text injection

**Files:**
- Create: `speakeasy/injector.py`, `tests/test_injector.py`

**Interfaces:**
- Produces: `inject(text: str, pasteboard=None, post_paste=None, secure_check=None, restore_delay: float = 0.6, sleep=time.sleep) -> str` returning `'injected' | 'clipboard_only'`; `secure_input_active() -> bool`; `frontmost_app_name() -> str | None`.
- Behavior: if `secure_check()` true ⇒ set clipboard to text, do NOT paste, return `'clipboard_only'`. Else save current clipboard string, set text, call `post_paste()` (default: CGEvent ⌘V), sleep `restore_delay`, restore old clipboard in `finally`, return `'injected'`.

- [ ] **Step 1: Failing test**

`tests/test_injector.py`:
```python
from speakeasy.injector import inject

class FakePasteboard:
    def __init__(self, initial="old"): self.value = initial; self.sets = []
    def get(self): return self.value
    def set(self, s): self.value = s; self.sets.append(s)

def test_paste_and_restore():
    pb, events = FakePasteboard("old"), []
    status = inject("hello", pasteboard=pb, post_paste=lambda: events.append("v"),
                    secure_check=lambda: False, sleep=lambda s: None)
    assert status == "injected"
    assert pb.sets == ["hello", "old"]      # set text, then restored
    assert events == ["v"]

def test_secure_input_clipboard_only():
    pb, events = FakePasteboard("old"), []
    status = inject("pw", pasteboard=pb, post_paste=lambda: events.append("v"),
                    secure_check=lambda: True, sleep=lambda s: None)
    assert status == "clipboard_only"
    assert pb.value == "pw" and events == []

def test_restore_even_if_paste_raises():
    pb = FakePasteboard("old")
    def boom(): raise RuntimeError("nope")
    try:
        inject("x", pasteboard=pb, post_paste=boom,
               secure_check=lambda: False, sleep=lambda s: None)
    except RuntimeError:
        pass
    assert pb.value == "old"
```

- [ ] **Step 2: Run** `pytest tests/test_injector.py -q` — FAIL.

- [ ] **Step 3: Implement `speakeasy/injector.py`**

```python
import ctypes, ctypes.util, time

def secure_input_active() -> bool:
    carbon = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/Carbon.framework/Carbon")
    return bool(carbon.IsSecureEventInputEnabled())

def frontmost_app_name() -> str | None:
    from AppKit import NSWorkspace
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return str(app.localizedName()) if app else None

class _SystemPasteboard:
    def get(self):
        from AppKit import NSPasteboard, NSPasteboardTypeString
        return NSPasteboard.generalPasteboard().stringForType_(NSPasteboardTypeString)
    def set(self, s):
        from AppKit import NSPasteboard, NSPasteboardTypeString
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(s, NSPasteboardTypeString)

def _post_cmd_v():
    import Quartz
    KEY_V = 9
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    down = Quartz.CGEventCreateKeyboardEvent(src, KEY_V, True)
    up = Quartz.CGEventCreateKeyboardEvent(src, KEY_V, False)
    Quartz.CGEventSetFlags(down, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventSetFlags(up, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)

def inject(text: str, pasteboard=None, post_paste=None, secure_check=None,
           restore_delay: float = 0.6, sleep=time.sleep) -> str:
    pb = pasteboard or _SystemPasteboard()
    secure = secure_check or secure_input_active
    paste = post_paste or _post_cmd_v
    if secure():
        pb.set(text)
        return "clipboard_only"
    old = pb.get()
    pb.set(text)
    try:
        paste()
        sleep(restore_delay)
    finally:
        pb.set(old if old is not None else "")
    return "injected"
```

- [ ] **Step 4: Run** `pytest tests/test_injector.py -q` — PASS.
- [ ] **Step 5: Commit** — `feat: clipboard-paste injector with secure-input gate`

---

### Task 10: hotkey.py — global toggle

**Files:**
- Create: `speakeasy/hotkey.py`, `tests/test_hotkey.py`

**Interfaces:**
- Produces: `should_toggle(event_type: int, keycode: int, target_keycode: int) -> bool` (pure); `class HotkeyListener(keycode: int, on_toggle: Callable[[], None])` with `.start()` (installs `CGEventTap` for key-down on the **current** runloop and swallows the matched key) and `.stop()`.

- [ ] **Step 1: Failing test**

`tests/test_hotkey.py`:
```python
import Quartz
from speakeasy.hotkey import should_toggle

def test_matches_only_keydown_of_target():
    assert should_toggle(Quartz.kCGEventKeyDown, 96, 96) is True
    assert should_toggle(Quartz.kCGEventKeyUp, 96, 96) is False
    assert should_toggle(Quartz.kCGEventKeyDown, 49, 96) is False
```

- [ ] **Step 2: Run** `pytest tests/test_hotkey.py -q` — FAIL.

- [ ] **Step 3: Implement `speakeasy/hotkey.py`**

```python
from typing import Callable
import Quartz

def should_toggle(event_type: int, keycode: int, target_keycode: int) -> bool:
    return event_type == Quartz.kCGEventKeyDown and keycode == target_keycode

class HotkeyListener:
    def __init__(self, keycode: int, on_toggle: Callable[[], None]):
        self._keycode = keycode
        self._on_toggle = on_toggle
        self._tap = None
        self._source = None

    def _handler(self, proxy, event_type, event, refcon):
        if event_type == Quartz.kCGEventTapDisabledByTimeout and self._tap:
            Quartz.CGEventTapEnable(self._tap, True)
            return event
        keycode = Quartz.CGEventGetIntegerValueField(
            event, Quartz.kCGKeyboardEventKeycode)
        if should_toggle(event_type, keycode, self._keycode):
            self._on_toggle()
            return None          # swallow the key
        return event

    def start(self) -> None:
        self._tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionDefault,
            Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown),
            self._handler, None)
        if self._tap is None:
            raise PermissionError(
                "CGEventTap failed — grant Accessibility permission to this app "
                "(System Settings > Privacy & Security > Accessibility)")
        self._source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), self._source,
                                  Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(self._tap, True)

    def stop(self) -> None:
        if self._tap:
            Quartz.CGEventTapEnable(self._tap, False)
            self._tap = None
```

- [ ] **Step 4: Run** `pytest tests/test_hotkey.py -q` — PASS.
- [ ] **Step 5: Commit** — `feat: global hotkey via CGEventTap`

---

### Task 11: overlay.py — floating panel

**Files:**
- Create: `speakeasy/overlay.py`, `tests/test_overlay.py`

**Interfaces:**
- Produces: `@dataclass OverlayModel(state: str = "hidden", partial: str = "", mode_name: str = "raw", level: float = 0.0)` with `label() -> str` (pure: `"● Recording — <mode>"`, `"… Transcribing"`, `"✍ Rewriting — <mode>"`, `""` for hidden); `class OverlayPanel()` with `render(model: OverlayModel) -> None` (creates/updates a non-activating floating `NSPanel` centered near top of screen showing `label()` + up to 120 chars of `partial`; hides when state == `"hidden"`). All AppKit calls must happen on the main thread — `render` dispatches via `pyobjc` `performSelectorOnMainThread_withObject_waitUntilDone_`.

- [ ] **Step 1: Failing test (model only — panel is verified in E2E)**

`tests/test_overlay.py`:
```python
from speakeasy.overlay import OverlayModel

def test_labels():
    assert OverlayModel().label() == ""
    assert OverlayModel(state="recording", mode_name="email").label() == "● Recording — email"
    assert OverlayModel(state="transcribing").label() == "… Transcribing"
    assert OverlayModel(state="rewriting", mode_name="clean").label() == "✍ Rewriting — clean"
```

- [ ] **Step 2: Run** `pytest tests/test_overlay.py -q` — FAIL.

- [ ] **Step 3: Implement `speakeasy/overlay.py`**

```python
from dataclasses import dataclass

@dataclass
class OverlayModel:
    state: str = "hidden"       # hidden|recording|transcribing|rewriting
    partial: str = ""
    mode_name: str = "raw"
    level: float = 0.0

    def label(self) -> str:
        if self.state == "recording":
            return f"● Recording — {self.mode_name}"
        if self.state == "transcribing":
            return "… Transcribing"
        if self.state == "rewriting":
            return f"✍ Rewriting — {self.mode_name}"
        return ""

class OverlayPanel:
    WIDTH, HEIGHT = 420, 64

    def __init__(self):
        self._panel = None

    def _ensure_panel(self):
        from AppKit import (NSPanel, NSScreen, NSColor, NSTextField, NSFont,
                            NSWindowStyleMaskNonactivatingPanel,
                            NSWindowStyleMaskBorderless, NSBackingStoreBuffered,
                            NSFloatingWindowLevel)
        import Foundation
        if self._panel is not None:
            return
        screen = NSScreen.mainScreen().frame()
        rect = Foundation.NSMakeRect(
            (screen.size.width - self.WIDTH) / 2,
            screen.size.height - self.HEIGHT - 60, self.WIDTH, self.HEIGHT)
        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        p = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False)
        p.setLevel_(NSFloatingWindowLevel)
        p.setOpaque_(False)
        p.setBackgroundColor_(
            NSColor.colorWithCalibratedWhite_alpha_(0.08, 0.92))
        p.setIgnoresMouseEvents_(True)
        p.setCollectionBehavior_(1 << 0)   # canJoinAllSpaces
        t = NSTextField.alloc().initWithFrame_(
            Foundation.NSMakeRect(12, 8, self.WIDTH - 24, self.HEIGHT - 16))
        t.setEditable_(False); t.setBezeled_(False); t.setDrawsBackground_(False)
        t.setTextColor_(NSColor.whiteColor())
        t.setFont_(NSFont.systemFontOfSize_(14))
        p.contentView().addSubview_(t)
        self._panel, self._text = p, t

    def _render_main(self, model: OverlayModel):
        self._ensure_panel()
        if model.state == "hidden":
            self._panel.orderOut_(None)
            return
        partial = model.partial[-120:]
        self._text.setStringValue_(
            model.label() + ("\n" + partial if partial else ""))
        self._panel.orderFrontRegardless()

    def render(self, model: OverlayModel) -> None:
        from Foundation import NSObject
        import objc
        panel = self

        class _R(NSObject):
            def run_(self, _):
                panel._render_main(model)

        _R.alloc().init().performSelectorOnMainThread_withObject_waitUntilDone_(
            objc.selector(_R.run_, signature=b"v@:@"), None, False)
```
Note for implementer: if the `performSelectorOnMainThread` wrapper fights you, an equivalent acceptable implementation is `AppHelper.callAfter(self._render_main, model)` from `PyObjCTools` — keep `render(model)` signature identical.

- [ ] **Step 4: Run** `pytest tests/test_overlay.py -q` — PASS.
- [ ] **Step 5: Commit** — `feat: overlay model + non-activating panel`

---

### Task 12: app.py + __main__ — controller state machine & menu bar

**Files:**
- Create: `speakeasy/app.py`, `speakeasy/__main__.py`, `tests/test_app.py`

**Interfaces:**
- Consumes: everything above, exactly as specified in each task's Produces block.
- Produces: `class Controller(engine, recorder, history, modes: dict[str, Mode], pool, settings: Settings, overlay_render: Callable[[OverlayModel], None] = lambda m: None, inject_fn = injector.inject, rewrite_fn = rewrite.rewrite, frontmost = lambda: None, min_samples: int = 6400)` with `toggle() -> None` and `state: str` (`"idle" | "recording" | "processing"`); `run()` in `__main__` wiring real components + rumps menu bar.
- Controller rules: toggle in idle ⇒ recorder.start, state=recording, overlay recording. Toggle in recording ⇒ state=processing, overlay transcribing, `samples = recorder.stop()`; if `len(samples) < min_samples` (~0.4 s) ⇒ history-less discard, overlay hidden, state=idle. Else `raw = engine.transcribe(samples)`; empty raw ⇒ same discard path but history row with status `discarded`. Else `rid = history.record(raw, mode.name, frontmost())` **before** anything else; raw mode ⇒ `status = inject_fn(raw)`, `history.update(rid, status=status)`. LLM mode ⇒ overlay rewriting, try `rewrite_fn` → inject rewritten, `history.update(rid, status=..., rewritten=..., account=..., latency_ms=...)`; on `RewriteError` ⇒ inject raw, `history.update(rid, status="rewrite_failed")`. Always end overlay hidden, state=idle. Toggle while processing ⇒ ignored. Processing runs on a `threading.Thread` (daemon) so the runloop never blocks; guard re-entry with `self._busy`.

- [ ] **Step 1: Failing tests**

`tests/test_app.py`:
```python
import numpy as np
from speakeasy.app import Controller
from speakeasy.config import Settings
from speakeasy.history import History
from speakeasy.modes import Mode
from speakeasy.rewrite import RewriteResult, RewriteError

class FakeRecorder:
    def __init__(self, samples): self._s = samples; self.started = 0
    def start(self): self.started += 1
    def stop(self): return self._s
    def snapshot(self): return self._s
    level = 0.1

class FakeEngine:
    def __init__(self, text="hello world"): self.text = text
    def transcribe(self, samples, sample_rate=16000): return self.text

def make(tmp_path, mode="raw", samples=None, rewrite_fn=None, inject_log=None):
    samples = samples if samples is not None else np.zeros(16000, np.float32)
    inject_log = inject_log if inject_log is not None else []
    modes = {"raw": Mode("raw", None),
             "clean": Mode("clean", "Clean it.")}
    s = Settings(active_mode=mode)
    h = History(tmp_path / "h.db")
    c = Controller(
        engine=FakeEngine(), recorder=FakeRecorder(samples), history=h,
        modes=modes, pool=None, settings=s,
        inject_fn=lambda text: inject_log.append(text) or "injected",
        rewrite_fn=rewrite_fn or (lambda *a, **k: RewriteResult("CLEANED", "acct-a", 5)),
        run_async=False)
    return c, h, inject_log

def test_raw_dictation_flow(tmp_path):
    c, h, log = make(tmp_path)
    c.toggle(); assert c.state == "recording"
    c.toggle(); assert c.state == "idle"
    assert log == ["hello world"]
    row = h.recent()[0]
    assert row["status"] == "injected" and row["raw"] == "hello world"

def test_llm_mode_rewrites(tmp_path):
    c, h, log = make(tmp_path, mode="clean")
    c.toggle(); c.toggle()
    assert log == ["CLEANED"]
    row = h.recent()[0]
    assert row["rewritten"] == "CLEANED" and row["account"] == "acct-a"

def test_rewrite_failure_falls_back_to_raw(tmp_path):
    def boom(*a, **k): raise RewriteError("all cooling")
    c, h, log = make(tmp_path, mode="clean", rewrite_fn=boom)
    c.toggle(); c.toggle()
    assert log == ["hello world"]
    assert h.recent()[0]["status"] == "rewrite_failed"

def test_short_audio_discarded(tmp_path):
    c, h, log = make(tmp_path, samples=np.zeros(1000, np.float32))
    c.toggle(); c.toggle()
    assert log == [] and h.recent() == []
```

- [ ] **Step 2: Run** `pytest tests/test_app.py -q` — FAIL.

- [ ] **Step 3: Implement `speakeasy/app.py`**

```python
import threading
from typing import Callable
from speakeasy import injector, rewrite as rewrite_mod
from speakeasy.config import Settings
from speakeasy.modes import Mode
from speakeasy.overlay import OverlayModel
from speakeasy.rewrite import RewriteError

class Controller:
    def __init__(self, engine, recorder, history, modes: dict[str, Mode],
                 pool, settings: Settings,
                 overlay_render: Callable[[OverlayModel], None] = lambda m: None,
                 inject_fn=injector.inject,
                 rewrite_fn=rewrite_mod.rewrite,
                 frontmost: Callable[[], str | None] = lambda: None,
                 min_samples: int = 6400,
                 run_async: bool = True):
        self.engine, self.recorder, self.history = engine, recorder, history
        self.modes, self.pool, self.settings = modes, pool, settings
        self._render = overlay_render
        self._inject, self._rewrite, self._frontmost = inject_fn, rewrite_fn, frontmost
        self._min = min_samples
        self._async = run_async
        self.state = "idle"

    def toggle(self) -> None:
        if self.state == "idle":
            self.state = "recording"
            self.recorder.start()
            self._render(OverlayModel("recording",
                                      mode_name=self.settings.active_mode))
        elif self.state == "recording":
            self.state = "processing"
            self._render(OverlayModel("transcribing"))
            if self._async:
                threading.Thread(target=self._process, daemon=True).start()
            else:
                self._process()
        # processing: ignore

    def _finish(self):
        self._render(OverlayModel("hidden"))
        self.state = "idle"

    def _process(self) -> None:
        try:
            samples = self.recorder.stop()
            if len(samples) < self._min:
                return
            raw = self.engine.transcribe(samples)
            if not raw.strip():
                self.history.update(
                    self.history.record("", self.settings.active_mode,
                                        self._frontmost()),
                    status="discarded")
                return
            mode = self.modes.get(self.settings.active_mode, self.modes["raw"])
            rid = self.history.record(raw, mode.name, self._frontmost())
            if mode.system_prompt is None:
                self.history.update(rid, status=self._inject(raw))
                return
            self._render(OverlayModel("rewriting", mode_name=mode.name))
            try:
                res = self._rewrite(raw, mode, self.pool)
                status = self._inject(res.text)
                self.history.update(rid, status=status, rewritten=res.text,
                                    account=res.account, latency_ms=res.latency_ms)
            except RewriteError:
                self._inject(raw)
                self.history.update(rid, status="rewrite_failed")
        finally:
            self._finish()
```

- [ ] **Step 4: Run** `pytest tests/test_app.py -q` — PASS. Commit — `feat: controller state machine`.

- [ ] **Step 5: Implement `speakeasy/__main__.py` (wiring, no new logic)**

```python
from pathlib import Path
import rumps
from speakeasy import injector
from speakeasy.accounts import AccountPool
from speakeasy.app import Controller
from speakeasy.audio import Recorder
from speakeasy.config import load_settings, save_settings, speakeasy_dir
from speakeasy.engine import create_engine
from speakeasy.history import History
from speakeasy.hotkey import HotkeyListener
from speakeasy.modes import load_modes
from speakeasy.overlay import OverlayPanel

def run() -> None:
    base = speakeasy_dir()
    settings = load_settings()
    modes = load_modes(base / "modes.yaml")
    engine = create_engine(settings.engine, base / "models")
    pool = AccountPool(Path.home() / ".claude-accounts.json",
                       base / "state.json",
                       allowlist=settings.account_allowlist or None)
    overlay = OverlayPanel()
    controller = Controller(
        engine=engine, recorder=Recorder(device=settings.input_device),
        history=History(base / "history.db"), modes=modes, pool=pool,
        settings=settings, overlay_render=overlay.render,
        frontmost=injector.frontmost_app_name)

    app = rumps.App("speakeasy", title="🎙")
    def set_mode(sender):
        settings.active_mode = sender.title
        save_settings(settings)
        for item in app.menu["Mode"].values():
            item.state = item.title == sender.title
    mode_menu = rumps.MenuItem("Mode")
    for name in modes:
        item = rumps.MenuItem(name, callback=set_mode)
        item.state = name == settings.active_mode
        mode_menu.add(item)
    app.menu = [mode_menu]

    listener = HotkeyListener(settings.hotkey_keycode, controller.toggle)
    listener.start()      # rumps runs the main runloop; tap attaches to it
    app.run()

if __name__ == "__main__":
    run()
```

- [ ] **Step 6: Sanity launch** — `python -m speakeasy` from terminal; expect menu-bar 🎙 (grant mic + accessibility to your terminal when prompted). Ctrl-C to quit. Commit — `feat: menu bar entrypoint`.

---

### Task 13: partial transcripts (pseudo-streaming)

**Files:**
- Modify: `speakeasy/app.py` (Controller), `tests/test_app.py`

**Interfaces:**
- Produces: while recording, a `threading.Timer`-based loop every `partial_interval` (default 4.0 s) transcribes `recorder.snapshot()` and renders `OverlayModel("recording", partial=<text>, mode_name=...)`. Constructor gains `partial_interval: float = 4.0`; `run_async=False` (tests) also disables the timer loop; timer cancelled on stop.

- [ ] **Step 1: Failing test** — add to `tests/test_app.py`:

```python
def test_partial_rendering(tmp_path):
    rendered = []
    c, h, log = make(tmp_path)
    c._render = lambda m: rendered.append((m.state, m.partial))
    c._tick_partial()          # simulate one timer firing while idle -> no-op
    c.toggle()                 # recording
    c._tick_partial()
    assert ("recording", "hello world") in rendered
```

- [ ] **Step 2: Run** — FAIL (`_tick_partial` missing).

- [ ] **Step 3: Implement** — in `Controller.__init__` add `self._partial_interval = partial_interval`, `self._timer = None`. Add:

```python
    def _tick_partial(self) -> None:
        if self.state != "recording":
            return
        text = self.engine.transcribe(self.recorder.snapshot())
        self._render(OverlayModel("recording", partial=text,
                                  mode_name=self.settings.active_mode))
        if self._async:
            self._schedule_partial()

    def _schedule_partial(self) -> None:
        import threading as _t
        self._timer = _t.Timer(self._partial_interval, self._tick_partial)
        self._timer.daemon = True
        self._timer.start()
```
In `toggle()` recording branch: after `self._render(...)`, `if self._async: self._schedule_partial()`. In processing branch: `if self._timer: self._timer.cancel()`.

- [ ] **Step 4: Run** `pytest tests/test_app.py -q` — PASS.
- [ ] **Step 5: Commit** — `feat: live partial transcripts in overlay`

---

### Task 14: E2E proof + README (release gate)

**Files:**
- Create: `README.md`, `scripts/e2e_evidence.sh`, `evidence/` artifacts

- [ ] **Step 1: Full suite** — `pytest -q && pytest -m integration -q` — all green.

- [ ] **Step 2: Live E2E** — launch `python -m speakeasy`; open TextEdit; press F5, speak "testing speakeasy end to end", press F5; verify text lands in TextEdit. Capture: `screencapture -x evidence/e2e-inject-$(date +%Y%m%d-%H%M%S).png`. Verify screenshot actually shows the injected text (visual-confirmation binding) — Read the PNG.

- [ ] **Step 3: Rewrite-mode E2E** — switch mode to `clean` in the menu; dictate "um so basically this is uh a test"; verify injected text is cleaned; capture screenshot + `sqlite3 ~/.speakeasy/history.db "SELECT id,mode,account,status,latency_ms FROM dictations ORDER BY id DESC LIMIT 3;" > evidence/history-proof.txt`.

- [ ] **Step 4: README** — quickstart (brew portaudio, venv, `pip install -e .`, permissions walkthrough, `python -m speakeasy`), hotkey/modes/engines config, troubleshooting (TCC, secure input, model download), evidence links.

- [ ] **Step 5: Commit** — `docs: README + e2e evidence bundle`.

---

## Self-review notes

- Spec coverage: hotkey (T10), recorder (T8), engines + download + fallback (T7), overlay (T11), modes (T4), round-robin (T5), rewrite service (T6), injector + secure input (T9), history-before-injection (T3, enforced in T12), menu bar (T12), partials/latency (T13), E2E + packaging path (T14; py2app bundling itself is deferred to release per DD-6).
- Type consistency: `Mode` (name/system_prompt/model), `RewriteResult(text, account, latency_ms)`, `History.record/update/recent`, `Engine.transcribe(samples, sample_rate)` used identically across T4-T13.
- Known risk points called out inline: sherpa-onnx wheel availability (T1), model asset names (T7 Step 1), pyobjc main-thread rendering (T11), CGEventTap permission (T10).
