"""Device pairing + bearer-token auth (docs/mobile/DECISIONS.md D3).

State file ``~/.speakeasy/mobile/devices.json`` (0600, dir 0700) holds only SHA-256 hashes of
tokens and pairing codes. It is shared by the server and the ``speakeasy pair`` CLI through an
flock. Nothing here ever logs a token or a code.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid
from pathlib import Path
from typing import Optional

from speakeasy.mobile.fsutil import atomic_write_json, file_lock, private_dir, read_json

DEFAULT_CODE_TTL_S = 600
MAX_FAILED_ATTEMPTS = 5


def default_mobile_dir() -> Path:
    return Path.home() / ".speakeasy" / "mobile"


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class PairingLocked(Exception):
    """Too many wrong codes: all pending codes were revoked."""


class DeviceRegistry:
    def __init__(self, root: Optional[Path] = None, now=time.time):
        self.root = private_dir(Path(root) if root else default_mobile_dir())
        self.path = self.root / "devices.json"
        self.lock_path = self.root / ".devices.lock"
        self._now = now

    def _load(self) -> dict:
        st = read_json(self.path, {})
        st.setdefault("devices", {})
        st.setdefault("pairing", [])
        st.setdefault("failed_attempts", 0)
        return st

    def _save(self, st: dict) -> None:
        atomic_write_json(self.path, st)

    # -- pairing codes -----------------------------------------------------------------
    def new_pairing_code(self, device_hint: str, ttl_s: int = DEFAULT_CODE_TTL_S) -> dict:
        code = f"{secrets.randbelow(1_000_000):06d}"
        expires = self._now() + ttl_s
        with file_lock(self.lock_path):
            st = self._load()
            now = self._now()
            st["pairing"] = [p for p in st["pairing"] if p["expires_ts"] > now]
            st["pairing"].append({"code_sha256": _sha(code), "device_hint": device_hint,
                                  "expires_ts": expires})
            self._save(st)
        return {"code": code, "expires_ts": expires, "device": device_hint}

    def pair(self, code: str, device_name: str) -> dict:
        code = (code or "").strip()
        device_name = (device_name or "").strip()[:64] or "device"
        with file_lock(self.lock_path):
            st = self._load()
            now = self._now()
            pending = [p for p in st["pairing"] if p["expires_ts"] > now]
            match = None
            h = _sha(code)
            for p in pending:
                if hmac.compare_digest(p["code_sha256"], h):
                    match = p
            if match is None:
                st["failed_attempts"] = int(st.get("failed_attempts", 0)) + 1
                locked = st["failed_attempts"] >= MAX_FAILED_ATTEMPTS
                if locked:
                    st["pairing"] = []
                    st["failed_attempts"] = 0
                else:
                    st["pairing"] = pending
                self._save(st)
                if locked:
                    raise PairingLocked("too many wrong pairing codes; all pending codes revoked")
                return {}
            pending.remove(match)  # one-time
            st["pairing"] = pending
            st["failed_attempts"] = 0
            token = secrets.token_urlsafe(32)
            device_id = "dev_" + uuid.uuid4().hex[:16]
            st["devices"][device_id] = {
                "name": device_name,
                "hint": match.get("device_hint"),
                "token_sha256": _sha(token),
                "created_ts": now,
                "last_seen_ts": now,
                "revoked": False,
            }
            self._save(st)
        return {"device_id": device_id, "token": token, "device_name": device_name}

    # -- tokens ------------------------------------------------------------------------
    def authenticate(self, bearer: Optional[str]) -> Optional[dict]:
        if not bearer:
            return None
        h = _sha(bearer)
        st = self._load()
        for device_id, rec in st["devices"].items():
            if rec.get("revoked"):
                continue
            if hmac.compare_digest(rec.get("token_sha256", ""), h):
                return {"device_id": device_id, "name": rec.get("name")}
        return None

    def touch(self, device_id: str) -> None:
        with file_lock(self.lock_path):
            st = self._load()
            rec = st["devices"].get(device_id)
            if rec is not None:
                rec["last_seen_ts"] = self._now()
                self._save(st)

    def revoke(self, device_id: str) -> bool:
        with file_lock(self.lock_path):
            st = self._load()
            rec = st["devices"].get(device_id)
            if rec is None:
                return False
            rec["revoked"] = True
            self._save(st)
            return True

    def list_devices(self) -> list[dict]:
        st = self._load()
        return [{"device_id": k, "name": v.get("name"), "created_ts": v.get("created_ts"),
                 "last_seen_ts": v.get("last_seen_ts"), "revoked": v.get("revoked", False)}
                for k, v in sorted(st["devices"].items())]
