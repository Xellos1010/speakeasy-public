"""Operator-profile hook (docs/mobile/OPERATOR-PROFILE-HOOK.md, DECISIONS.md D4).

The operator profile is ``<OPERATOR_PROFILES_DIR>/<OPERATOR_PROFILE>.json`` (default
``~/.speakeasy/operator-profiles/default.json``; both settable via the deployment profile,
docs/PROFILE.md). It is
NEVER written. This module derives SpeakEasy facts (voice-first input, spoken vocabulary,
learned corrections, speaking pace, preferred dictation mode, devices) and writes an overlay that
holds only keys ABSENT from the operator file. ``merged_view`` gives consumers the combination
with operator-authored values always winning.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Optional

from speakeasy.mobile.fsutil import atomic_write_json, private_dir

OVERLAY_SCHEMA = "operator-profile-overlay/1"
DEFAULT_PROFILES_DIR = Path.home() / ".speakeasy" / "operator-profiles"


def profile_id() -> str:
    from speakeasy import deploy_profile
    return os.environ.get("OPERATOR_PROFILE") or deploy_profile.load().get("operator_overlay.id") or "default"


def profiles_dir() -> Path:
    from speakeasy import deploy_profile
    return Path(os.environ.get("OPERATOR_PROFILES_DIR")
                or deploy_profile.load().get("operator_overlay.dir")
                or DEFAULT_PROFILES_DIR).expanduser()


def proposed_fields(derived: dict) -> dict:
    prefs = derived.get("preferences") or {}
    return {
        "input": {
            "voice_first": True,
            "voice": {
                "surface": "speakeasy",
                "preferred_dictation_mode": prefs.get("preferred_mode"),
                "language": prefs.get("lang"),
                "devices": [d.get("name") or d["device_id"] for d in derived.get("devices", [])],
            },
        },
        "vocabulary": {
            "spoken_terms": [v["term"] for v in derived.get("vocab", [])[:100]],
            "speech_corrections": [
                {"heard": r["pattern"], "meant": r["replacement"]}
                for r in derived.get("corrections", []) if r.get("active")
            ][:50],
        },
        "pace": {
            "speech_words_per_minute": (derived.get("pace") or {}).get("words_per_minute"),
            "dictations_total": (derived.get("stats") or {}).get("dictations", 0),
        },
        "ui_preferences": prefs.get("ui") or {},
    }


def additive_overlay(base: dict, proposed: dict, prefix: str = "") -> tuple[dict, list]:
    """Keep only keys absent from `base` (recursing into dicts both sides have). Returns
    (overlay, skipped) where skipped lists operator-authored key paths left untouched."""
    out: dict = {}
    skipped: list = []
    for k, v in proposed.items():
        path = f"{prefix}{k}"
        if k not in base:
            out[k] = copy.deepcopy(v)
        elif isinstance(base[k], dict) and isinstance(v, dict):
            sub, sk = additive_overlay(base[k], v, path + ".")
            if sub:
                out[k] = sub
            skipped.extend(sk)
        else:
            skipped.append(path)
    return out, skipped


def merged_view(base: dict, overlay: dict) -> dict:
    """Base (operator-authored) always wins; overlay only fills gaps."""
    out = copy.deepcopy(base)
    for k, v in overlay.items():
        if k not in out:
            out[k] = copy.deepcopy(v)
        elif isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = merged_view(out[k], v)
    return out


def build_overlay(derived: dict, pid: Optional[str] = None, pdir: Optional[Path] = None) -> dict:
    pid = pid or profile_id()
    base_path = (pdir or profiles_dir()) / f"{pid}.json"
    try:
        raw = base_path.read_bytes()
        base = json.loads(raw)
        base_sha = hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError):
        base, base_sha = {}, None
    overlay, skipped = additive_overlay(base, proposed_fields(derived))
    return {
        "schema": OVERLAY_SCHEMA,
        "id": pid,
        "source": "speakeasy",
        "overlay_version": derived.get("version", 0),
        "speakeasy_profile_digest": derived.get("digest"),
        "generated_ts": time.time(),
        "base_path": str(base_path),
        "base_found": base_sha is not None,
        "base_sha256": base_sha,
        "overlay": overlay,
        "skipped_operator_authored_keys": skipped,
    }


def overlay_dir(profile_root: Path) -> Path:
    return private_dir(Path(profile_root) / "operator-overlay")


def write_overlay(profile, versioned: bool = True, pid: Optional[str] = None,
                  pdir: Optional[Path] = None, beside: bool = False) -> list[Path]:
    doc = build_overlay(profile.derived(), pid, pdir)
    d = overlay_dir(profile.root)
    written = []
    latest = d / f"{doc['id']}.speakeasy-overlay.json"
    atomic_write_json(latest, doc)
    written.append(latest)
    if versioned:
        vp = d / f"{doc['id']}.speakeasy-overlay.v{doc['overlay_version']}.json"
        atomic_write_json(vp, doc)
        written.append(vp)
    if beside and doc["base_found"]:
        bp = Path(doc["base_path"]).parent / f"{doc['id']}.speakeasy-overlay.json"
        atomic_write_json(bp, doc)
        written.append(bp)
    return written
