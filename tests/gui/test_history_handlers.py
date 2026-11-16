"""Tests for speakeasy/gui/handlers/history.py.

Uses a real History(tmp_path / "h.db") seeded via history.record(...)/update(...)
(the real sqlite-backed API, speakeasy/history.py) -- no mocking of History
itself. history_copy_text is exercised against a fake pasteboard swapped in
for speakeasy.gui.handlers.history._SystemPasteboard so the suite doesn't
depend on/mutate the real macOS clipboard.
"""
import time

from speakeasy.history import History
from speakeasy.gui.handlers import history as history_handlers
from speakeasy.gui.handlers.history import (
    _relative_time,
    history_get_entries,
    history_copy_text,
)


class _FakeCtx:
    def __init__(self, history):
        self.history = history


# ---------------------------------------------------------------- _relative_time

def test_relative_time_just_now():
    now = 1_000_000.0
    assert _relative_time(now - 10, now) == "just now"


def test_relative_time_minutes_ago():
    now = 1_000_000.0
    assert _relative_time(now - 90, now) == "1 min ago"


def test_relative_time_hours_ago():
    now = 1_000_000.0
    assert _relative_time(now - 3 * 3600, now) == "3 hr ago"


def test_relative_time_days_ago():
    now = 1_000_000.0
    assert _relative_time(now - 2 * 86400, now) == "2 d ago"


def test_relative_time_never_negative_for_future_ts():
    now = 1_000_000.0
    assert _relative_time(now + 50, now) == "just now"


# ---------------------------------------------------------- history_get_entries

def test_empty_history_returns_no_entries(tmp_path):
    h = History(tmp_path / "h.db")
    ctx = _FakeCtx(h)

    result = history_get_entries({"search": "", "mode": "all", "status": "all", "limit": 50}, ctx)

    assert result == {"entries": [], "total_matching": 0}


def test_search_matches_raw_text_case_insensitively(tmp_path):
    h = History(tmp_path / "h.db")
    h.record("Buy milk and eggs", "raw", "Notes", status="injected")
    h.record("Call the plumber", "raw", "Notes", status="injected")
    ctx = _FakeCtx(h)

    result = history_get_entries({"search": "MILK", "mode": "all", "status": "all", "limit": 50}, ctx)

    assert result["total_matching"] == 1
    assert result["entries"][0]["text"] == "Buy milk and eggs"


def test_search_matches_rewritten_text(tmp_path):
    h = History(tmp_path / "h.db")
    rid = h.record("send the revised proposal", "clean", "Slack", status="pending")
    h.update(rid, status="injected", rewritten="Send the revised proposal, please.")
    h.record("unrelated dictation", "raw", "Notes", status="injected")
    ctx = _FakeCtx(h)

    result = history_get_entries({"search": "please", "mode": "all", "status": "all", "limit": 50}, ctx)

    assert result["total_matching"] == 1
    assert result["entries"][0]["text"] == "Send the revised proposal, please."


def test_search_no_match_returns_empty(tmp_path):
    h = History(tmp_path / "h.db")
    h.record("Buy milk and eggs", "raw", "Notes", status="injected")
    ctx = _FakeCtx(h)

    result = history_get_entries({"search": "spaceship", "mode": "all", "status": "all", "limit": 50}, ctx)

    assert result == {"entries": [], "total_matching": 0}


def test_mode_filter(tmp_path):
    h = History(tmp_path / "h.db")
    h.record("raw one", "raw", "Notes", status="injected")
    h.record("clean one", "clean", "Slack", status="injected")
    ctx = _FakeCtx(h)

    result = history_get_entries({"search": "", "mode": "clean", "status": "all", "limit": 50}, ctx)

    assert result["total_matching"] == 1
    assert result["entries"][0]["text"] == "clean one"


def test_status_filter(tmp_path):
    h = History(tmp_path / "h.db")
    h.record("discarded one", "raw", None, status="discarded")
    h.record("injected one", "raw", "Notes", status="injected")
    ctx = _FakeCtx(h)

    result = history_get_entries({"search": "", "mode": "all", "status": "discarded", "limit": 50}, ctx)

    assert result["total_matching"] == 1
    assert result["entries"][0]["text"] == "discarded one"


def test_combined_filters(tmp_path):
    h = History(tmp_path / "h.db")
    h.record("run the deploy script", "clean", "Terminal", status="clipboard_only")
    h.record("run the tests", "clean", "Terminal", status="injected")
    h.record("run the deploy script", "raw", "Terminal", status="clipboard_only")
    ctx = _FakeCtx(h)

    result = history_get_entries(
        {"search": "deploy", "mode": "clean", "status": "clipboard_only", "limit": 50}, ctx)

    assert result["total_matching"] == 1
    assert result["entries"][0]["text"] == "run the deploy script"
    assert result["entries"][0]["mode"] == "clean"
    assert result["entries"][0]["status"] == "clipboard_only"


def test_has_diff_true_when_rewritten_differs_from_raw(tmp_path):
    h = History(tmp_path / "h.db")
    rid = h.record("send the revised proposal", "clean", "Slack", status="pending")
    h.update(rid, status="injected", rewritten="Send the revised proposal.")
    ctx = _FakeCtx(h)

    result = history_get_entries({"search": "", "mode": "all", "status": "all", "limit": 50}, ctx)

    entry = result["entries"][0]
    assert entry["has_diff"] is True
    assert entry["raw"] == "send the revised proposal"
    assert entry["rewritten"] == "Send the revised proposal."


def test_has_diff_false_when_no_rewrite(tmp_path):
    h = History(tmp_path / "h.db")
    h.record("buy milk and eggs", "raw", "Notes", status="injected")
    ctx = _FakeCtx(h)

    result = history_get_entries({"search": "", "mode": "all", "status": "all", "limit": 50}, ctx)

    entry = result["entries"][0]
    assert entry["has_diff"] is False
    assert entry["rewritten"] is None
    assert entry["text"] == "buy milk and eggs"


def test_has_diff_false_when_rewritten_equals_raw(tmp_path):
    h = History(tmp_path / "h.db")
    rid = h.record("already clean text", "clean", "Slack", status="pending")
    h.update(rid, status="injected", rewritten="already clean text")
    ctx = _FakeCtx(h)

    result = history_get_entries({"search": "", "mode": "all", "status": "all", "limit": 50}, ctx)

    entry = result["entries"][0]
    assert entry["has_diff"] is False


def test_limit_caps_entries_but_total_matching_reflects_all_matches(tmp_path):
    h = History(tmp_path / "h.db")
    for i in range(5):
        h.record(f"dictation {i}", "raw", "Notes", status="injected")
    ctx = _FakeCtx(h)

    result = history_get_entries({"search": "", "mode": "all", "status": "all", "limit": 2}, ctx)

    assert result["total_matching"] == 5
    assert len(result["entries"]) == 2


def test_entries_ordered_most_recent_first(tmp_path):
    h = History(tmp_path / "h.db")
    h.record("first", "raw", "Notes", status="injected")
    h.record("second", "raw", "Notes", status="injected")
    ctx = _FakeCtx(h)

    result = history_get_entries({"search": "", "mode": "all", "status": "all", "limit": 50}, ctx)

    assert [e["text"] for e in result["entries"]] == ["second", "first"]


def test_registered_on_bridge():
    from speakeasy.gui.bridge import ACTIONS

    assert ACTIONS["history_get_entries"] is history_get_entries
    assert ACTIONS["history_copy_text"] is history_copy_text


# ------------------------------------------------------------- history_copy_text

class _FakePasteboard:
    def __init__(self):
        self.value = None

    def get(self):
        return self.value

    def set(self, s):
        self.value = s


def test_history_copy_text_sets_clipboard_via_system_pasteboard(monkeypatch):
    fake = _FakePasteboard()
    monkeypatch.setattr(history_handlers, "_SystemPasteboard", lambda: fake)

    result = history_copy_text({"text": "hello clipboard"}, None)

    assert fake.value == "hello clipboard"
    assert result == {"copied": True}


def test_history_copy_text_handles_missing_text_key(monkeypatch):
    fake = _FakePasteboard()
    monkeypatch.setattr(history_handlers, "_SystemPasteboard", lambda: fake)

    result = history_copy_text({}, None)

    assert fake.value == ""
    assert result == {"copied": True}
