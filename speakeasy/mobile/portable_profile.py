"""Portable, cumulative operator profile (docs/mobile/DECISIONS.md D2).

Storage, under ``~/.speakeasy/profile/`` (dir 0700, files 0600), beside -- never touching --
the voice ProfileStore files (``profile-v<N>.json`` / ``active.json``):

- ``events.jsonl``  append-only interaction log. One JSON event per line:
  ``{"id", "ts", "type", "device_id", "host", "data"}``. Events are never edited or removed.
- ``derived.json``  the profile derived from the full event set (pure function of the set).

Merge between hosts/devices is a union of events by ``id`` followed by re-derivation, so it is
commutative, associative and idempotent (CRDT-style grow-only set). ``version`` is the event
count and ``digest`` hashes the sorted ids, so two hosts holding the same events agree on both.
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Iterable, Optional

from speakeasy.mobile.fsutil import (
    append_line,
    atomic_write_json,
    file_lock,
    private_dir,
)
from speakeasy.training.profile import (
    _is_noise_diff,
    _word_diff_rules,
    apply_corrections,
    apply_hotword_fuzzy_correction,
)

SCHEMA = "speakeasy.portable-profile/1"
DERIVED_SCHEMA = "speakeasy.derived-profile/1"
MIN_OCCURRENCES_COMMON = 2  # same noise threshold as training/profile.py
MAX_EVENT_BYTES = 64 * 1024

_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-]*")


def default_profile_dir() -> Path:
    return Path.home() / ".speakeasy" / "profile"


def _norm_ts(ts) -> float:
    """Accept epoch seconds or milliseconds; default now."""
    try:
        t = float(ts)
    except (TypeError, ValueError):
        return time.time()
    if t > 1e11:  # milliseconds
        t /= 1000.0
    return t


def event_id_for(device_id: Optional[str], etype: str, ts: float, data: dict) -> str:
    blob = json.dumps([device_id, etype, round(ts, 3), data], sort_keys=True, default=str)
    return "evt_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def make_event(etype: str, data: dict, *, device_id: Optional[str] = None,
               ts=None, event_id: Optional[str] = None, host: Optional[str] = None) -> dict:
    t = _norm_ts(ts) if ts is not None else time.time()
    data = data if isinstance(data, dict) else {"value": data}
    eid = event_id or event_id_for(device_id, etype, t, data)
    return {
        "id": str(eid),
        "ts": t,
        "type": str(etype),
        "device_id": device_id,
        "host": host or socket.gethostname(),
        "data": data,
    }


def _valid_event(ev) -> bool:
    return (isinstance(ev, dict) and isinstance(ev.get("id"), str) and ev["id"]
            and isinstance(ev.get("type"), str) and isinstance(ev.get("data", {}), dict))


def _is_vocab_like(phrase: str) -> bool:
    """A replacement carries vocabulary signal (proper noun, acronym, product name, number)
    when any token has an uppercase letter or a digit."""
    return any(any(c.isupper() or c.isdigit() for c in tok) for tok in _WORD_RE.findall(phrase))


def correction_pairs(before: str, after: str) -> list[tuple[str, str]]:
    """Word-level substitutions the operator made (reuses training/profile.py's diff)."""
    out = []
    for pattern, replacement in _word_diff_rules(before or "", after or ""):
        if pattern.lower() == replacement.lower() and pattern == replacement:
            continue
        if len(pattern) < 2:
            continue
        out.append((pattern, replacement))
    return out


def derive(events: Iterable[dict]) -> dict:
    """Pure function of the event SET (input order is irrelevant)."""
    evs = sorted({e["id"]: e for e in events if _valid_event(e)}.values(),
                 key=lambda e: (e.get("ts", 0), e["id"]))
    ids = sorted(e["id"] for e in evs)
    digest = hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()[:16]

    vocab = Counter()
    rule_counts: Counter = Counter()
    rule_last_ts: dict = {}
    explicit: dict = {}  # pattern.lower() -> (ts, id, pattern, replacement)
    mode_counts = Counter()
    lang_counts = Counter()
    app_counts = Counter()
    ui: dict = {}  # key -> (ts, id, value)
    devices: dict = {}
    words_total = 0
    seconds_total = 0.0
    dictations = 0
    corrections_total = 0
    types = Counter()

    for e in evs:
        d = e.get("data") or {}
        t = e["type"]
        types[t] += 1
        dev = e.get("device_id")
        if dev:
            rec = devices.setdefault(dev, {"device_id": dev, "name": None, "first_seen": e["ts"],
                                           "last_seen": e["ts"], "events": 0})
            rec["last_seen"] = max(rec["last_seen"], e["ts"])
            rec["events"] += 1
        if t == "dictation":
            dictations += 1
            if d.get("mode"):
                mode_counts[str(d["mode"])] += 1
            if d.get("lang"):
                lang_counts[str(d["lang"])] += 1
            if d.get("app_hint"):
                app_counts[str(d["app_hint"])] += 1
            dur = d.get("duration_s") or 0
            if isinstance(dur, (int, float)) and dur >= 0.5:
                words_total += len(_WORD_RE.findall(d.get("raw_text") or ""))
                seconds_total += float(dur)
        elif t == "mode_choice" and d.get("mode"):
            mode_counts[str(d["mode"])] += 1
        elif t == "correction":
            corrections_total += 1
            for pattern, replacement in correction_pairs(d.get("before", ""), d.get("corrected_text", "")):
                key = (pattern.lower(), replacement)
                rule_counts[key] += 1
                rule_last_ts[key] = e["ts"]
                if _is_vocab_like(replacement):
                    for tok in _WORD_RE.findall(replacement):
                        if any(c.isupper() or c.isdigit() for c in tok):
                            vocab[tok] += 1
        elif t == "vocab_add" and d.get("term"):
            vocab[str(d["term"]).strip()] += 3
        elif t == "replacement" and d.get("pattern") and d.get("replacement") is not None:
            explicit[str(d["pattern"]).lower()] = (e["ts"], e["id"], str(d["pattern"]), str(d["replacement"]))
        elif t == "preference" and d.get("key"):
            k = str(d["key"])
            cur = ui.get(k)
            if cur is None or (e["ts"], e["id"]) >= (cur[0], cur[1]):
                ui[k] = (e["ts"], e["id"], d.get("value"))
        elif t == "device_paired" and dev:
            devices[dev]["name"] = d.get("device_name") or devices[dev]["name"]

    # Resolve learned correction rules: per pattern pick the most frequent replacement
    # (latest wins ties); activate vocab-like fixes immediately, common-word fixes on the
    # second sighting.
    by_pattern: dict = {}
    for (plow, repl), cnt in rule_counts.items():
        best = by_pattern.get(plow)
        cand = (cnt, rule_last_ts[(plow, repl)], repl)
        if best is None or cand[:2] > best[:2]:
            by_pattern[plow] = cand
    learned = []
    for plow, (cnt, _ts, repl) in by_pattern.items():
        # A bare first-letter capitalisation ("please" -> "Please") is usually sentence
        # position, not vocabulary: it waits for a second sighting like common-word fixes.
        first_cap_only = _is_noise_diff(plow, repl) and repl == plow[:1].upper() + plow[1:]
        active = (cnt >= MIN_OCCURRENCES_COMMON) or (_is_vocab_like(repl) and not first_cap_only)
        learned.append({"pattern": plow, "replacement": repl, "count": cnt,
                        "active": bool(active), "source": "feedback"})
    learned.sort(key=lambda r: (-r["count"], r["pattern"]))
    explicit_rules = [{"pattern": p, "replacement": r, "count": 1, "active": True, "source": "explicit"}
                      for (_ts, _id, p, r) in sorted(explicit.values(), key=lambda x: x[2].lower())]
    explicit_keys = {r["pattern"].lower() for r in explicit_rules}
    corrections = explicit_rules + [r for r in learned if r["pattern"] not in explicit_keys]

    hotwords = [{"phrase": term, "score": 2.0} for term, _c in
                sorted(vocab.items(), key=lambda kv: (-kv[1], kv[0].lower()))]
    preferred_mode = (sorted(mode_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
                      if mode_counts else None)
    lang = sorted(lang_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if lang_counts else None
    wpm = round(words_total / seconds_total * 60.0, 1) if seconds_total > 0 else None

    return {
        "schema": DERIVED_SCHEMA,
        "version": len(evs),
        "digest": digest,
        "event_count": len(evs),
        "event_types": dict(sorted(types.items())),
        "vocab": [{"term": t, "count": c} for t, c in
                  sorted(vocab.items(), key=lambda kv: (-kv[1], kv[0].lower()))],
        "hotwords": hotwords,
        "corrections": corrections,
        "preferences": {
            "preferred_mode": preferred_mode,
            "mode_counts": dict(sorted(mode_counts.items())),
            "lang": lang,
            "top_apps": [{"app": a, "count": c} for a, c in
                         sorted(app_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]],
            "ui": {k: v[2] for k, v in sorted(ui.items())},
        },
        "pace": {"words_per_minute": wpm, "dictations": dictations,
                 "speech_seconds": round(seconds_total, 1)},
        "stats": {"dictations": dictations, "corrections": corrections_total},
        "devices": sorted(devices.values(), key=lambda r: r["device_id"]),
    }


class PortableProfile:
    """Thread-safe (in-process lock) and process-safe (flock) event log + derived view."""

    def __init__(self, root: Optional[Path] = None):
        self.root = private_dir(Path(root) if root else default_profile_dir())
        self.events_path = self.root / "events.jsonl"
        self.derived_path = self.root / "derived.json"
        self.lock_path = self.root / ".portable.lock"
        self._lock = threading.RLock()
        self._events: dict = {}
        self._utterances: dict = {}  # utterance_id -> dictation event
        self._loaded_size = -1
        self._derived: Optional[dict] = None
        self._listeners = []

    # -- loading -----------------------------------------------------------------------
    def _reload_if_changed(self) -> None:
        try:
            size = self.events_path.stat().st_size
        except OSError:
            size = 0
        if size == self._loaded_size:
            return
        events: dict = {}
        if size:
            with open(self.events_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue  # torn final line from a crash: skip, never fail
                    if _valid_event(ev):
                        events.setdefault(ev["id"], ev)
        self._events = events
        self._utterances = {
            ev["data"]["utterance_id"]: ev for ev in events.values()
            if ev["type"] == "dictation" and ev.get("data", {}).get("utterance_id")
        }
        self._loaded_size = size
        self._derived = None

    def events(self) -> list[dict]:
        with self._lock:
            self._reload_if_changed()
            return sorted(self._events.values(), key=lambda e: (e.get("ts", 0), e["id"]))

    def derived(self) -> dict:
        with self._lock:
            self._reload_if_changed()
            if self._derived is None:
                self._derived = derive(self._events.values())
            return self._derived

    @property
    def version(self) -> int:
        return self.derived()["version"]

    def find_utterance(self, utterance_id: str) -> Optional[dict]:
        with self._lock:
            self._reload_if_changed()
            return self._utterances.get(utterance_id)

    def on_change(self, fn) -> None:
        self._listeners.append(fn)

    # -- writing -----------------------------------------------------------------------
    def append(self, events: Iterable[dict]) -> int:
        """Append events not already present (by id). Returns how many were added."""
        added = 0
        with self._lock, file_lock(self.lock_path):
            self._reload_if_changed()
            for ev in events:
                if not _valid_event(ev) or ev["id"] in self._events:
                    continue
                line = json.dumps(ev, sort_keys=True, ensure_ascii=False)
                if len(line.encode("utf-8")) > MAX_EVENT_BYTES:
                    continue
                append_line(self.events_path, line)
                self._events[ev["id"]] = ev
                if ev["type"] == "dictation" and ev["data"].get("utterance_id"):
                    self._utterances[ev["data"]["utterance_id"]] = ev
                added += 1
            try:
                self._loaded_size = self.events_path.stat().st_size
            except OSError:
                pass
            if added:
                self._derived = derive(self._events.values())
                atomic_write_json(self.derived_path, self._derived)
        if added:
            for fn in list(self._listeners):
                try:
                    fn(self)
                except Exception:
                    pass
        return added

    def record(self, etype: str, data: dict, **kw) -> dict:
        ev = make_event(etype, data, **kw)
        self.append([ev])
        return ev

    # -- apply to text -----------------------------------------------------------------
    def apply(self, text: str) -> str:
        d = self.derived()
        out = apply_hotword_fuzzy_correction(text, d.get("hotwords") or []) if d.get("hotwords") else text
        rules = [{"pattern": r["pattern"], "replacement": r["replacement"], "kind": "literal",
                  "case_sensitive": False} for r in d.get("corrections", []) if r.get("active")]
        return apply_corrections(out, rules) if rules else out

    # -- portability -------------------------------------------------------------------
    def export_bundle(self) -> dict:
        return {"schema": SCHEMA, "exported_ts": time.time(), "host": socket.gethostname(),
                "events": self.events(), "derived": self.derived()}

    def import_bundle(self, bundle: dict) -> dict:
        if not isinstance(bundle, dict) or bundle.get("schema") != SCHEMA:
            raise ValueError(f"not a {SCHEMA} bundle")
        evs = bundle.get("events")
        if not isinstance(evs, list):
            raise ValueError("bundle.events must be a list")
        added = self.append(evs)
        return {"ok": True, "added": added, "total": self.derived()["event_count"],
                "profile_version": self.version}

    def summary(self) -> dict:
        d = self.derived()
        return {
            "version": d["version"],
            "digest": d["digest"],
            "event_count": d["event_count"],
            "vocab_count": len(d["vocab"]),
            "top_vocab": [v["term"] for v in d["vocab"][:20]],
            "top_corrections": d["corrections"][:20],
            "preferences": d["preferences"],
            "pace": d["pace"],
            "stats": d["stats"],
            "devices": d["devices"],
        }


def new_utterance_id() -> str:
    return "utt_" + uuid.uuid4().hex[:20]
