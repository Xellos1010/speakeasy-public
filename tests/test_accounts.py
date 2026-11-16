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
