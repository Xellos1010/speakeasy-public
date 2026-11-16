# SpeakEasy mobile decisions

## D1 (2026-11-09, coordinator): local-only becomes "operator-owned devices only"

The README invariant "raw dictation never touches the network" is preserved in this form:
audio may travel from the operator's phone to the operator's own workstation over the Tailscale
tailnet (WireGuard). Never Tailscale Funnel, never any third-party or cloud speech-to-text.
The mobile API server binds the tailnet IP and 127.0.0.1 only (never 0.0.0.0); the phone
reaches it through `tailscale serve` (tailnet-only HTTPS on :8445). Rewrite modes keep their
existing behaviour (text only, via the local `claude` CLI), unchanged from the desktop app.

## D2 (2026-11-09): the Mac is the canonical profile home

The portable operator profile lives at `~/.speakeasy/profile/` (append-only `events.jsonl` +
derived `derived.json`, files 0600, dir 0700) beside the existing voice ProfileStore files
(`profile-v<N>.json`, `active.json`), which are untouched. Other workstations and the Pi carry
it with `speakeasy profile sync --peer <host>`: a union of events by id, then re-derive. The
profile version is the event count, so two hosts with the same event set report the same
version and digest.

## D3 (2026-11-09): device auth

Pairing codes are 6-digit, one-time, short-lived (10 min default). Device tokens are 32-byte
random values; only SHA-256 hashes are stored (`~/.speakeasy/mobile/devices.json`, 0600). The
token store is never part of a profile export. Tokens are never logged.

## D4 (2026-11-09): operator-profile hook is an overlay, never an edit

The operator profile (`<OPERATOR_PROFILES_DIR>/<id>.json`,
selected by `OPERATOR_PROFILE`, default `default`) is never modified. `speakeasy profile
operator-overlay` writes a versioned overlay containing only keys that are absent from the
operator file. See OPERATOR-PROFILE-HOOK.md.
