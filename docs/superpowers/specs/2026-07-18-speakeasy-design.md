# speakeasy — local dictation with universal injection (design)

Date: 2026-07-18 · Status: approved (deferred-approval mode, operator directive) · Initiative: INI-SPEAKEASY

## Problem

Evan wants a SuperWhisper-class dictation tool he owns outright: fast, highly accurate English
speech-to-text that runs fully locally on this Mac (Intel i9-9880H, 16 GB, AMD 5500M — no Apple
Neural Engine) and injects the transcript into whatever text field currently has focus, in any app.
LLM rewrite modes (SuperWhisper-style) are required, powered by headless Claude using the existing
multi-account subscription round-robin — not API billing.

## Decisions already made (operator-answered)

1. Inference: **fully local** on this Mac.
2. Trigger UX: **toggle hotkey + floating overlay** (SuperWhisper-style).
3. Post-processing: **LLM rewrite modes**, raw mode remains the default.
4. LLM backend: **headless `claude -p` round-robin** across accounts in `~/.claude-accounts.json`,
   Haiku default, per-mode model swap allowed.
5. Approach: **Python daemon + pyobjc native bindings** (approach B).

## Architecture

Single Python 3.11+ process, menu-bar resident. No server. Network used only by LLM rewrite modes.

```
hotkey listener ─► recorder ─► STT engine (chunked pseudo-streaming)
                                    │ partials
                               overlay panel (non-activating NSPanel)
                                    │ final transcript
                          mode router ──► raw ─────────────► injector
                                     └──► rewrite service ─► injector
                                          (claude -p round-robin)
                          history store (SQLite) ◄── every step, before injection
```

### Components

| Component | Tech | Contract |
|---|---|---|
| Hotkey listener | Quartz `CGEventTap` | configurable toggle key (default **F5**, alt ⌥-Space); tap=start, tap=stop |
| Recorder | `sounddevice`, 16 kHz mono | ring buffer; selected device persisted |
| STT engine | **sherpa-onnx + Parakeet-TDT 0.6B int8** (default); whisper `small.en` int8 via sherpa-onnx (fallback engine — DD-8) | `Engine.transcribe(chunk) -> partial`, `Engine.finalize() -> Transcript`; ~4 s chunking while recording so final lands <1.5 s after stop |
| Overlay | pyobjc `NSPanel` (non-activating, floating) | record dot, level meter, live partial text, mode name; never takes focus |
| Mode router | `modes.yaml` | `raw` (default), `clean`, `email`, `prompt-polish`; each mode: system prompt + model |
| Rewrite service | subprocess `claude -p --model <m>` | `CLAUDE_CONFIG_DIR` = next account dir (round-robin, skip-on-usage-limit, persisted cursor); 20 s timeout |
| Injector | CGEvent ⌘V paste | save clipboard → set text → paste → restore (try/finally); AX API fallback; secure-input detected → clipboard-only + notification |
| History store | SQLite `~/.speakeasy/history.db` | row per dictation: ts, raw, mode, rewritten, target app, status; written **before** injection |
| Menu bar | `rumps`/pyobjc | mode/engine/mic pickers, last-10 history with re-copy, launch-at-login |

### Data flow

Tap F5 → overlay appears, capture + chunked transcription start → tap F5 → engine finalizes →
history row written → raw mode injects immediately; LLM mode shows "rewriting…", calls rewrite
service, updates history row, injects result. Rewrite failure ⇒ inject raw transcript + notify
(a dictation is never lost — reversible/observable-state rule).

## Rewrite service — account round-robin

- Reads `~/.claude-accounts.json`; eligible accounts = configurable allowlist (default: all
  non-elevated dirs that have credentials present).
- Cursor persisted in `~/.speakeasy/state.json`; each call advances the cursor.
- On usage-limit/auth error: mark account cooling (timestamped), rotate to next; all cooling ⇒
  fail → raw-inject fallback.
- Invocation: `CLAUDE_CONFIG_DIR=<dir> claude -p <prompt> --model claude-haiku-4-5-20251001
  --output-format text` with the mode's system prompt; stdin carries the transcript.
- Every call logged to history (account used, latency, outcome).

## Error handling

| Failure | Behavior |
|---|---|
| Mic/accessibility permission missing | first-run checker walks through TCC grants; hard-fail with instructions, never silent |
| Engine model missing | downloader fetches model to `~/.speakeasy/models/` with checksum; fallback engine if load fails |
| Rewrite timeout / non-zero / all accounts cooling | inject raw transcript, notify, mark history `rewrite_failed` |
| Secure input active | no injection; transcript to clipboard + notification, history `clipboard_only` |
| Empty/too-short audio | discard with overlay flash, history `discarded` |
| Clipboard restore | guaranteed via try/finally; original clipboard also stashed in history row |

## Testing

- **Unit** (pytest): round-robin rotation incl. cooling/exhaustion; mode config parsing; injector
  clipboard save/restore (mocked pasteboard); history store CRUD; secure-input gate.
- **Engine bench**: script transcribing bundled sample WAVs → asserts WER vs golden text and RTF
  < 0.5 on this machine.
- **Integration**: synthetic WAV → engine → mode router (raw) → mocked injector → golden transcript.
- **E2E proof (release gate)**: real hotkey dictation into TextEdit with screenshot evidence
  (visual-confirmation binding); real rewrite call via round-robin with captured attribution.

## Packaging & permissions

Dev: run from terminal (terminal's mic/AX permissions apply). Release: `py2app` bundle
`speakeasy.app` so TCC grants attach to a stable bundle ID; LaunchAgent for start-at-login.
Model files (~650 MB) live outside the bundle in `~/.speakeasy/models/`.

## Performance targets

- Stop-tap → injected text: **< 1.5 s** raw (≤ 15 s dictation), **< 6 s** with Haiku rewrite.
- Idle RSS < 500 MB (engine loaded); peak < 1.2 GB. No GPU dependency.

## Out of scope (v1)

Windows/Linux; App Store/notarization; local LLM; server inference; meeting/system-audio
capture; non-English; hold-to-talk mode (phase 2); VAD auto-stop (phase 2).

## Deferred decisions queue

Defaults were chosen where the design left a question open; each is amendable post-hoc.
