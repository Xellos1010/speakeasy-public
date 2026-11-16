from speakeasy.history import History

def test_record_before_injection(tmp_path):
    h = History(tmp_path / "h.db")
    rid = h.record("hello world", "raw", "TextEdit")
    rows = h.recent()
    assert rows[0]["id"] == rid and rows[0]["status"] == "pending"
    assert rows[0]["raw"] == "hello world"

def test_record_with_explicit_status(tmp_path):
    h = History(tmp_path / "h.db")
    rid = h.record("hola mundo", "translate", "TextEdit", status="translate_raw")
    row = h.recent()[0]
    assert row["id"] == rid and row["status"] == "translate_raw"


def test_update_and_order(tmp_path):
    h = History(tmp_path / "h.db")
    a = h.record("first", "raw", None)
    b = h.record("second", "email", None)
    h.update(b, status="injected", rewritten="Dear team, second", account="acct-c", latency_ms=900)
    rows = h.recent(limit=2)
    assert [r["id"] for r in rows] == [b, a]
    assert rows[0]["rewritten"] == "Dear team, second"
    assert rows[0]["account"] == "acct-c"
