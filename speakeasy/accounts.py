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
        from speakeasy.config import write_private
        write_private(self._state_path, json.dumps(self._state))

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
