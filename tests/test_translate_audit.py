import subprocess
import pytest
from speakeasy.translate_audit import (
    audit_translation,
    build_audit_prompt,
    TranslateAuditError,
)


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
        name = self.names.pop(0)
        return Account(name, Path("/tmp") / name)
    def mark_cooling(self, name, seconds=3600.0):
        self.cooled.append(name)

def test_prompt_embeds_raw_text_and_matches_spec_wrapper():
    prompt = build_audit_prompt("i go to store yesterday")
    assert "<translation>" in prompt and "</translation>" in prompt
    assert "i go to store yesterday" in prompt
    assert "localization editor" in prompt
    assert "Output only the corrected English text" in prompt

def test_success_first_account():
    def ok_runner(cmd, **kw):
        assert "--model" in cmd
        assert "claude" in cmd[0]
        assert kw["env"]["CLAUDE_CONFIG_DIR"].endswith("/tmp/acct-a")
        return subprocess.CompletedProcess(cmd, 0, stdout="I went to the store yesterday.", stderr="")
    r = audit_translation("i go to store yesterday", FakePool(["acct-a"]), runner=ok_runner)
    assert r.text == "I went to the store yesterday." and r.account == "acct-a"

def test_rotates_on_failure():
    calls = []
    def flaky(cmd, **kw):
        calls.append(kw["env"]["CLAUDE_CONFIG_DIR"])
        code = 1 if len(calls) == 1 else 0
        return subprocess.CompletedProcess(cmd, code, stdout="ok", stderr="usage limit")
    pool = FakePool(["acct-a", "acct-b"])
    r = audit_translation("hi", pool, runner=flaky)
    assert r.account == "acct-b" and pool.cooled == ["acct-a"]

def test_timeout_rotates_without_cooling():  # P1-4: timeouts are not usage limits
    def timeout_then_ok(cmd, **kw):
        if kw["env"]["CLAUDE_CONFIG_DIR"].endswith("acct-a"):
            raise subprocess.TimeoutExpired(cmd, 20.0)
        return subprocess.CompletedProcess(cmd, 0, stdout="fixed", stderr="")
    pool = FakePool(["acct-a", "acct-b"])
    r = audit_translation("hi", pool, runner=timeout_then_ok)
    assert r.text == "fixed" and pool.cooled == []

def test_raw_fallback_is_callers_job_audit_raises_on_exhaustion():
    """translate_audit itself never silently returns the raw text on
    failure -- it raises so the Controller can record `translate_audit_failed`
    while leaving the already-injected raw transcript alone (spec item 5)."""
    def dead(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="limit")
    with pytest.raises(TranslateAuditError):
        audit_translation("hi", FakePool(["acct-a"]), runner=dead)

def test_no_eligible_account_raises():
    with pytest.raises(TranslateAuditError):
        audit_translation("hi", FakePool([]))
