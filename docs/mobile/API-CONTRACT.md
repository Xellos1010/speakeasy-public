# SpeakEasy mobile API contract, v1

Status: FROZEN v1 (2026-11-09). The Android app is built against this file. Any change is
written here first, with a dated entry in the changelog at the bottom.

## Transport

- Base URL (phone): `https://<your-host-magicdns-name>:8445` (configure via `server.public_url` in your deployment profile, see docs/PROFILE.md; Tailscale serve,
  tailnet only, never Funnel). It proxies to `http://127.0.0.1:8812` on the Mac.
- Direct tailnet URL (other workstations): `http://<tailnet-ip>:8812`.
- All bodies are JSON (`Content-Type: application/json`) except `POST /v1/transcribe`
  (multipart/form-data).
- Every error is a JSON body `{"error": "<message>"}` with an HTTP status: 400 bad input,
  401 missing/invalid token, 404 unknown route/utterance, 413 payload too large, 415
  unsupported audio, 429 too many pairing attempts, 500 engine failure.

## Auth

- Header on every route except `GET /v1/health` and `POST /v1/pair`:
  `Authorization: Bearer <device token>`.
- Tokens are issued by pairing. On the workstation the operator runs
  `speakeasy pair --device <name>`, which prints a 6-digit one-time pairing code (default life
  10 minutes) and a QR payload:
  `{"v":1,"type":"speakeasy-pair","url":"<base url>","code":"123456","device":"<name>"}`.
- The phone exchanges the code: `POST /v1/pair {"code": "123456", "device_name": "pixel"}` →
  `200 {"device_id": "dev_<hex>", "token": "<opaque>", "device_name": "pixel", "host": "<host>"}`.
  The token is shown once; the server stores only its SHA-256. Store it in Android Keystore /
  EncryptedSharedPreferences. Wrong codes: 401; after 5 wrong attempts all pending codes are
  revoked (429).

## Routes

### GET /v1/health (no auth)
`200 {"ok": true, "engine": "parakeet", "model": "<model dir name>", "profile_version": <int>, "host": "<hostname>"}`

### POST /v1/transcribe (auth, multipart/form-data)
Parts:
- `audio` (file, required): WAV 16 kHz mono PCM16 preferred; m4a/aac, ogg-opus, webm-opus,
  3gp/amr also accepted (decoded on the workstation with ffmpeg). Max 25 MB / 10 minutes.
- `device_id` (required), `mode` = `raw` | `clean` | `email` | `prompt` (default `raw`),
  `lang` (optional, e.g. `en`), `app_hint` (optional, e.g. the target app package),
  `utterance_id` (optional client-generated id for idempotent retry; server generates one if absent).

Response `200`:
`{"utterance_id": "utt_<hex>", "text": "<final text>", "raw_text": "<engine output>", "mode": "clean", "model": "<model>", "latency_ms": 412, "profile_version": 37}`

Semantics: the operator profile (vocab/hotwords, learned corrections, explicit replacements)
is applied to `raw_text`; rewrite modes (`clean`, `email`, `prompt`) then run through the
existing rewrite path and fall back to the profile-applied raw text on failure (the response
still 200s; `mode` echoes the requested mode and `rewrite_status` is `ok` | `fallback` | `none`).
History and the profile event log are written BEFORE the response is sent. A retry with the
same `utterance_id` returns the stored result without re-transcribing.

### POST /v1/feedback (auth)
Body `{"utterance_id": "utt_…", "corrected_text": "…", "device_id": "dev_…"}` →
`200 {"ok": true, "learned": [{"kind": "correction"|"vocab", "pattern": "…", "replacement": "…", "active": true|false}], "profile_version": <int>}`.
`active=false` means the rule is recorded but needs a second sighting before it is applied
(common-word rules); proper-noun/vocabulary fixes are active immediately.

### GET /v1/profile (auth)
Derived summary: `{"version": <int>, "digest": "<hex>", "event_count": n, "vocab_count": n, "top_vocab": [...], "top_corrections": [{"pattern","replacement","count","active"}], "preferences": {"preferred_mode": "clean", "mode_counts": {...}, "lang": "en", "ui": {...}}, "pace": {"words_per_minute": n}, "devices": [...]}`.

### GET /v1/profile/export (auth)
Full portable bundle: `{"schema": "speakeasy.portable-profile/1", "exported_ts": <epoch s>, "host": "…", "events": [<event>…], "derived": {…}}`.

### POST /v1/profile/import (auth)
Body = an export bundle. Merge only (union of events by `id`; never overwrites or deletes);
the profile is re-derived. → `200 {"ok": true, "added": n, "total": n, "profile_version": n}`.
Idempotent: importing the same bundle twice adds 0.

### POST /v1/events (auth)
Body `{"device_id": "dev_…", "events": [{"type": "app_usage"|"mode_choice"|"edit"|"preference"|"vocab_add"|"replacement"|…, "ts": <epoch seconds or ms>, "data": {…}, "id": "<optional client id>"}]}`
→ `200 {"ok": true, "accepted": n, "profile_version": n}`. Max 500 events per call, 64 KB per
event. Well-known types the profile derives from: `preference` `{"key","value"}` (last writer
wins), `vocab_add` `{"term"}`, `replacement` `{"pattern","replacement"}` (always active),
`mode_choice` `{"mode"}`. Any other type is stored in the log as-is.

## Changelog
- 2026-11-09 v1 frozen (coordinator contract + clarified error codes, pair response shape,
  optional `utterance_id`, `rewrite_status`).
