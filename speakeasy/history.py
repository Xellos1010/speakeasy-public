import os, sqlite3, time
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

# Additive columns (audit 2026-11-09 P1-1 / P2-9): the failure reason, and the saved
# audio of a dictation whose transcription failed (so it can be retried, never lost).
_ADDED_COLUMNS = {"error": "TEXT", "audio_path": "TEXT"}


def _private(path: Path) -> None:
    """Transcripts are private: 0600 for the db and its WAL/journal side files."""
    for p in (path, Path(f"{path}-wal"), Path(f"{path}-shm"), Path(f"{path}-journal")):
        try:
            if p.exists():
                os.chmod(p, 0o600)
        except OSError:
            pass


class History:
    def __init__(self, db_path: Path):
        db_path = Path(db_path)
        if not db_path.exists():
            os.close(os.open(db_path, os.O_CREAT | os.O_WRONLY, 0o600))
        self._path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(dictations)")}
        for col, typ in _ADDED_COLUMNS.items():
            if col not in have:
                self._conn.execute(f"ALTER TABLE dictations ADD COLUMN {col} {typ}")
        self._conn.commit()
        _private(db_path)

    def record(self, raw: str, mode: str, target_app: str | None,
               status: str = "pending") -> int:
        cur = self._conn.execute(
            "INSERT INTO dictations (ts, raw, mode, target_app, status) VALUES (?,?,?,?,?)",
            (time.time(), raw, mode, target_app, status))
        self._conn.commit()
        return cur.lastrowid

    def update(self, row_id: int, *, status: str, rewritten: str | None = None,
               account: str | None = None, latency_ms: int | None = None,
               raw: str | None = None, error: str | None = None,
               audio_path: str | None = None) -> None:
        self._conn.execute(
            "UPDATE dictations SET status=?, rewritten=COALESCE(?,rewritten),"
            " account=COALESCE(?,account), latency_ms=COALESCE(?,latency_ms),"
            " raw=COALESCE(?,raw), error=COALESCE(?,error),"
            " audio_path=COALESCE(?,audio_path) WHERE id=?",
            (status, rewritten, account, latency_ms, raw, error, audio_path, row_id))
        self._conn.commit()

    def clear_audio(self, row_id: int) -> None:
        self._conn.execute("UPDATE dictations SET audio_path=NULL WHERE id=?", (row_id,))
        self._conn.commit()

    def get(self, row_id: int) -> dict | None:
        r = self._conn.execute("SELECT * FROM dictations WHERE id=?", (row_id,)).fetchone()
        return dict(r) if r else None

    def recent(self, limit: int = 10) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM dictations ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def latest_retryable(self) -> dict | None:
        """Most recent dictation whose transcription failed and whose audio is kept."""
        r = self._conn.execute(
            "SELECT * FROM dictations WHERE status='engine_failed' AND audio_path IS NOT NULL"
            " ORDER BY id DESC LIMIT 1").fetchone()
        return dict(r) if r else None
