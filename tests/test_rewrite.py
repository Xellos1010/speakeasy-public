import subprocess
import pytest
from speakeasy.modes import Mode
from speakeasy.rewrite import rewrite, RewriteError


@pytest.fixture(autouse=True)
def _fake_claude_bin(monkeypatch):
    """Hosts without the claude CLI (e.g. Linux CI) still exercise the runner logic."""
    monkeypatch.setattr("speakeasy.claude_cli.resolve_claude", lambda override=None: "/fake/bin/claude")


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
