# speakeasy

Fully-local, SuperWhisper-style dictation for macOS and Linux (X11). Press a hotkey, speak, and the
transcript is injected into whatever text field currently has focus — in any app.
Speech-to-text runs entirely on-device (Parakeet via sherpa-onnx). Optional AI
"rewrite modes" (clean / email / prompt-polish) run through headless Claude using a
round-robin across your local Claude accounts.

- **Local & private:** raw dictation never touches the network.
- **Accurate & fast:** Parakeet-TDT 0.6B (int8) — exact-match on the test sample at
  ~230 ms transcription latency on an Intel i9; no GPU required.
- **Universal injection:** real system paste into any focused text field.
- **Rewrite modes:** headless `claude -p` (Haiku default, swappable per mode), account
  round-robin with rotate-on-usage-limit; falls back to the raw transcript on failure.
- **Never loses a dictation:** every result is written to SQLite history *before* injection.

## Configuration for your own setup

Hostnames, addresses, paths and the names of your secrets live in a **deployment profile** kept
outside the repository (`$SPEAKEASY_PROFILE` or `~/.config/speakeasy/profile/profile.json`).
With no profile, safe defaults apply. See [docs/PROFILE.md](docs/PROFILE.md).

## Requirements

**macOS:**
- macOS (built/tested on macOS 26, Intel).
- Python 3.11+ (3.13 used in development).
- Homebrew `portaudio` (for the microphone).

**Linux:**
- X11 session (`XDG_SESSION_TYPE=x11`) — built/tested on Ubuntu 24.04 + GNOME. Wayland is
  not supported (X11's global-input model doesn't carry over; a Wayland port would need a
  different approach — portal APIs or evdev+uinput — and hasn't been done).
- Python 3.11+.
- `xdotool` (system package, e.g. `apt install xdotool`) — used for the paste keystroke
  and, optionally, direct-type injection. Clipboard access itself needs no system package
  (implemented directly against Xlib's `CLIPBOARD` selection).
- **Trust model note:** X11 has no equivalent of macOS's explicit Accessibility grant —
  any client connected to the same X server can already read global key events and inject
  synthetic input. This is a pre-existing property of X11 itself, not something speakeasy
  introduces, but it's a meaningfully different security posture than macOS's permission
  prompt and worth knowing before running this on a shared or untrusted X session.

### Network service (Linux) — optional, off by default

Lets other hosts on your own tailnet route dictation audio to this machine's engine (see
`docs/superpowers/specs/2026-08-18-network-service.md`). Binds only to this host's
Tailscale interface address — never `0.0.0.0`, never LAN, never the public internet — and
refuses to start at all if `tailscale ip -4` can't resolve one. Requires `~/.speakeasy/host_settings.json`
(the per-host measured-benchmark record; §4 of the port spec) to exist before it's usable
as a ranking target — nothing crashes without it, the client side just stays local-only.

This runs as its own process, independent of the menu-bar dictation app, so this box's
availability as an inference host doesn't depend on whether you happen to have the
dictation client open. A template systemd **user** unit is provided at
`scripts/systemd/speakeasy-service.service` — **not installed or enabled by anything in
this repo**; install it yourself when you want this host to serve others:

```bash
mkdir -p ~/.config/systemd/user
cp scripts/systemd/speakeasy-service.service ~/.config/systemd/user/
# edit WorkingDirectory/ExecStart in the copy if your checkout isn't at ~/Projects/speakeasy
systemctl --user daemon-reload
systemctl --user enable --now speakeasy-service.service
```

Or run it directly for testing, no systemd involved: `python -m speakeasy.service [port]`
(default port 7420; the bind interface itself is never a launcher option).

## Install

```bash
brew install portaudio
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
```

The first launch downloads the Parakeet model (~650 MB) to `~/.speakeasy/models/`.

> **TLS note:** the python.org build used in dev ships without a CA bundle, so the model
> download can fail with `SSL_CERT_VERIFY_FAILED`. Fix once with any of: run
> `/Applications/Python\ 3.13/Install\ Certificates.command`, `pip install certifi`, or
> export `SSL_CERT_FILE=/etc/ssl/cert.pem` (macOS system roots).

## Permissions (one-time)

speakeasy needs three macOS privacy grants. Run it once and grant when prompted, or
pre-grant under **System Settings → Privacy & Security**:

- **Microphone** — to record.
- **Accessibility** — to capture the global hotkey and post the paste keystroke.
- **Screen Recording** is *not* required.

When run from a terminal in development, these grants attach to the *terminal* app.
For a standalone build, package with `py2app` so grants attach to a stable bundle ID
(see "Packaging").

## Run

```bash
python -m speakeasy
```

A 🎙 menu-bar item appears. The default hotkey is **Right ⌘** (Right Command):

- **Tap** (press and release within 0.35 s, no other key in between) toggles recording —
  press to start, tap again to stop. An overlay shows a live partial transcript while
  recording; the final transcript is injected at your cursor on stop.
- **Hold** (push-to-talk): hold Right ⌘ down to record, release to stop and inject —
  no second press needed.
- **Chords are ignored**: if you type while holding Right ⌘ (e.g. right-⌘C to copy),
  speakeasy treats it as a chord and does nothing on release — Right ⌘ still passes
  through to the app underneath normally, since modifier key events are never swallowed.

Switch rewrite modes from the menu-bar **Mode** submenu.

## Configuration

All under `~/.speakeasy/`:

- `settings.json` — `hotkey` (default `"right_command"`; tap-to-toggle / hold-to-talk on
  Right ⌘), or set it to a legacy int keycode (e.g. `96` for F5) for the old
  keyDown-based toggle-only behavior; `engine` (`parakeet`|`whisper`), `input_device`,
  `active_mode`, `account_allowlist`.
- `modes.yaml` — override or add rewrite modes: each entry may set `system_prompt` and
  `model`. Built-ins: `raw` (no LLM), `clean`, `email`, `prompt-polish`. `raw` is always
  present and never rewritten.
- `history.db` — SQLite dictation log (id, ts, raw, mode, rewritten, account, latency,
  target app, status). The menu bar shows the last 10 with re-copy.
- `state.json` — account round-robin cursor + cooling timers.

### Rewrite modes & accounts

Rewrites shell out to `CLAUDE_CONFIG_DIR=<account dir> claude -p … --model <model>`,
cycling accounts from `~/.claude-accounts.json`. `*-elevated` accounts are skipped unless
listed in `account_allowlist`. On a usage-limit/auth failure an account is put on a 1-hour
cooldown and the next is tried; if all are cooling, the raw transcript is injected instead.

## Verification & evidence

Unit + integration suite (TDD, one module per component):

```bash
pytest -q                                   # 26 unit tests
pytest -m integration -q                    # real Parakeet transcription of a sample WAV
```

End-to-end proofs (drive the real pipeline into a real external app; the mic's acoustic
path is the only thing substituted, with a real recorded WAV — acoustic speaker→mic
loopback is silent on this hardware):

```bash
python scripts/e2e_stickies.py raw          # raw transcript -> synthetic Cmd-V -> Stickies
python scripts/e2e_stickies.py clean        # + real Haiku rewrite via account round-robin
```

Captured evidence is written to `evidence/` (gitignored) when you run the scripts above.

Measured on this machine: transcription ~230 ms; raw dictation end-to-end < 1 s (engine
warm); Haiku rewrite round-trip ~5.2 s.

> **Why Stickies for the automated proof:** a system-wide synthetic ⌘V lands in the
> *active* app's focused field — exactly how real use works, where your own target app
> stays active while speakeasy's overlay is non-activating. A headless test process can't
> become the active app, so the proof targets Stickies (bundled, focuses cleanly) and
> reads the pasted value back via the Accessibility API. TextEdit was avoided because its
> iCloud document modals hang AppleScript in this environment.

## Packaging (release)

```bash
scripts/package.sh        # full py2app build -> dist/speakeasy.app (ad-hoc signed)
scripts/package.sh -A     # alias mode (dev; symlinks source, not for distribution)
```

The script bootstraps a dedicated `.venv-pkg/` (py2app never pollutes the dev
`.venv/`), builds `dist/speakeasy.app` with `CFBundleIdentifier=org.speakeasy.dictation` (override: `SPEAKEASY_BUNDLE_ID` or the deployment profile)
and `LSUIElement` (menu-bar only, no Dock icon), then ad-hoc signs it. Model files
stay in `~/.speakeasy/models/` (outside the bundle). Decision record:
`docs/adr/0002-py2app-packaging.md`.

**One-time human step after install:** launch the app once, then grant
**Microphone** and **Accessibility** to *speakeasy* under System Settings →
Privacy & Security (grants attach to the stable bundle ID, so they survive
rebuilds). App Store notarization is out of scope.

Start-at-login (optional): install a LaunchAgent at
`~/Library/LaunchAgents/org.speakeasy.dictation.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>org.speakeasy.dictation</string>
  <key>ProgramArguments</key>
  <array><string>/Applications/speakeasy.app/Contents/MacOS/speakeasy</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
```

## Architecture

`speakeasy/` — `hotkey` (CGEventTap) · `audio` (sounddevice recorder) · `engine/`
(sherpa-onnx Parakeet + whisper fallback + model downloader) · `overlay` (non-activating
NSPanel) · `modes` · `accounts` (round-robin) · `rewrite` (headless claude) · `injector`
(clipboard-paste + secure-input gate) · `history` (SQLite) · `app` (controller state
machine) · `__main__` (menu-bar wiring). Design & plan: `docs/superpowers/`.

## Out of scope (v1)

Windows, Wayland, App Store notarization, local LLM inference, server/remote inference,
meeting/system-audio capture, non-English.

## NX workspace

`speakeasy` is governed as a standalone NX workspace (see
`docs/adr/0001-nx-workspace.md`). NX wraps the existing Python toolchain via
`nx:run-commands` — nothing about the Python install/test/run flow above
changes; NX just gives it governed, discoverable entry points:

```bash
npm install               # once, to fetch nx (node_modules/, not committed)
npx nx show projects       # -> ["speakeasy"]
npx nx run speakeasy:test              # pytest -q (unit tests)
npx nx run speakeasy:test-integration  # pytest -m integration
npx nx run speakeasy:lint              # ruff if installed in .venv, else compileall
npx nx run speakeasy:serve             # python -m speakeasy
npx nx run speakeasy:e2e               # scripts/e2e_stickies.py raw
```

All targets assume an activated project `.venv` at the repo root (see Install,
above) and run with `PYTHONPATH=.` so they exercise the local checkout's source.


## License

SpeakEasy is licensed under the Functional Source License 1.1 with Apache 2.0 Future License (FSL-1.1-ALv2): free to use,
modify and share for any purpose except a Competing Use; each version becomes Apache-2.0 two years after release. The code is
source-available under the [Fair Source](https://fair.io) model, not open source in the OSI sense. See [LICENSE](LICENSE) and
[NOTICE](NOTICE). For commercial licensing, open an issue. "SpeakEasy" name and logo are not licensed.

Contributions need a signed Contributor License Agreement; see [CONTRIBUTING.md](CONTRIBUTING.md).
