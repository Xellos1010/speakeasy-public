# ADR-0002: py2app standalone `.app` packaging

## Status

Accepted — 2026-07-18

## Context

Work order TSK-SPK-APP-001. `speakeasy` needs three macOS TCC privacy grants
(Microphone, Accessibility, and implicitly Apple Events for the paste
injection) to function. Run via `python -m speakeasy` from a terminal
(the dev workflow), those grants attach to the *terminal app's* bundle
identifier, not to speakeasy itself — so every different terminal a
developer uses needs its own grant, the grant isn't portable to another
machine/user, and there's no stable identity to hand a non-technical
end user an install runbook against.

v1 decision DA-3 explicitly deferred packaging: the final TCC grant step
requires a human clicking through System Settings, so v1 shipped as
`python -m speakeasy` with packaging sketched only as a README stub. This
ADR is the packaging work itself — everything up to, but not including,
that human click.

## Decision

Package with **py2app**, targeting a **stable bundle ID** of
`org.speakeasy.dictation`, ad-hoc signed, model files excluded from the bundle.

- `setup.py` (repo root) defines the py2app target: `APP =
  ["speakeasy/__main__.py"]`, `packages` forces full inclusion of
  `speakeasy`, `sherpa_onnx`, `sounddevice`, `_sounddevice_data`, and
  `rumps` (see "Native library bundling" below), and `plist` sets:
  - `CFBundleIdentifier: org.speakeasy.dictation` — the whole point: this string,
    not the invoking terminal, is what TCC's grant database keys off of.
  - `LSUIElement: True` — menu-bar-only app: no Dock icon, no app menu
    bar, matching the existing `rumps.App` UX.
  - `NSMicrophoneUsageDescription` — required or macOS silently refuses
    to grant/prompt for Microphone at all.
  - `NSAppleEventsUsageDescription` — declared defensively for the paste
    injector's use of Apple Events; harmless if unused by the actual
    injection mechanism.
  - Accessibility has **no** Info.plist usage-description key (unlike
    Microphone/Camera) — it is granted purely by the user adding the
    bundle to System Settings → Privacy & Security → Accessibility,
    keyed by `CFBundleIdentifier`/code identity. That manual add is the
    deferred human step from DA-3.
- `scripts/package.sh` is the reproducible build entry point: bootstraps
  a dedicated `.venv-pkg/` (never the shared dev `.venv/`), runs `python
  setup.py py2app` (full mode) or `-A` (alias mode) on request, then a
  post-build smoke (binary exists, ad-hoc `codesign --force --deep --sign
  -`, `codesign --verify`).
- Models stay **outside** the bundle, in `~/.speakeasy/models/`
  (`speakeasy/config.py:speakeasy_dir()`, used by
  `speakeasy/engine/download.py:ensure_model`) — this was already true
  of the existing code and required no change. Keeps the bundle a fixed
  ~170 MB (interpreter + native libs) instead of ~800 MB, and means a
  model upgrade doesn't require a rebuild/re-sign/re-grant cycle.
- Ad-hoc signing (`codesign --sign -`) only. Notarization is explicitly
  **out of scope** — it requires an Apple Developer Program account,
  hardened-runtime entitlements review, and Apple's notary service
  round-trip, none of which this work order provisions. Ad-hoc signing
  is sufficient for local/internal single-machine use (Gatekeeper will
  still warn on first launch from Finder for an unnotarized app from an
  unidentified developer; running the built binary directly, as this
  ADR's evidence does, bypasses that Finder-specific quarantine gate).

## Native library bundling ("recipe" workaround)

py2app's default dependency walker analyzes pure-Python `import`
graphs; it does not reliably follow `ctypes`/`dlopen`-loaded native
libraries sitting as *package data* inside a dependency (rather than as
a proper C-extension import). Two dependencies here are exactly that
shape:

- `sherpa_onnx` ships `libsherpa-onnx-c-api.dylib`,
  `libsherpa-onnx-cxx-api.dylib`, `libonnxruntime*.dylib`, and the
  compiled `_sherpa_onnx*.so` under `sherpa_onnx/lib/`.
- `sounddevice` doesn't link portaudio directly; a companion package,
  `_sounddevice_data`, ships `portaudio-binaries/libportaudio.dylib`,
  loaded via `ctypes.CDLL` at import time.

Rather than write custom py2app "recipes" (small Python modules that
teach the dependency walker about a package's non-standard binary
layout — the officially sanctioned py2app extension point for exactly
this problem), this ADR takes the coarser, simpler path: `setup.py`'s
`options.packages` list force-includes each of these packages *whole*
(directory copy, not import-graph analysis), so their `lib/` /
`portaudio-binaries/` subdirectories with all `.dylib`/`.so` files ride
along regardless of whether py2app's walker would have found them.
Confirmed present in the built bundle at
`dist/speakeasy.app/Contents/Resources/lib/python3.13/{sherpa_onnx/lib/*.dylib,_sounddevice_data/portaudio-binaries/libportaudio.dylib}`.
Trade-off: slightly larger bundle (whole packages, not a pruned
dependency graph) in exchange for zero custom recipe code to maintain.

## Full mode achieved (not a fallback to alias mode)

Two real build-environment problems were hit and fixed while iterating
(both are generic py2app/setuptools/macOS issues, not specific to this
codebase's logic):

1. **`error: install_requires is no longer supported`** — py2app's
   `finalize_options` hard-rejects any `Distribution` where
   `install_requires` is set (py2app/build_app.py). Modern setuptools
   synthesizes that attribute from `pyproject.toml`'s PEP 621
   `[project.dependencies]` table (needed for the ordinary `pip install
   -e .` dev workflow) even though `setup.py` never sets it itself —
   py2app can't distinguish "legacy install_requires kwarg" from
   "PEP 621-derived attribute" and blocks both. Fixed with a `NoDepsPy2App`
   subclass of py2app's command class that clears
   `self.distribution.install_requires` in `finalize_options()` before
   calling `super()` — safe here because py2app bundles the
   *already-installed* site-packages via `options.packages`, so pip-style
   requirement strings never factor into the frozen build.
2. **Interpreter choice matters.** The first `.venv-pkg` was created with
   whatever `python3` resolved first on `$PATH` on the build machine —
   Miniconda's Python 3.12. That produced a *buildable* app that
   immediately crashed on launch: `Library not loaded: @rpath/libffi.8.dylib`
   (conda's `_ctypes.so` links against conda's own libffi copy, which
   py2app's dependency walker doesn't know to bundle from outside
   site-packages). Rebuilding `.venv-pkg` against the same interpreter the
   project's shared dev `.venv` already uses —
   `/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13` (the
   python.org framework build) — resolved it with no further changes:
   framework builds are what py2app is designed and tested against.
   **Lesson for `scripts/package.sh` / future rebuilds:** always point
   `.venv-pkg` at `/usr/local/bin/python3.13` (symlinks to the framework
   build) explicitly rather than trusting a bare `python3` on `$PATH`, if
   Miniconda or another distribution is ever ahead of it in `$PATH`. The
   script bootstraps with `python3` today matching this repo's existing
   convention (README's `.venv` bootstrap does the same); this is a known
   sharp edge on machines with multiple Python distributions installed
   and not something scripts/package.sh currently guards against.

With that interpreter fix, **full py2app mode built and ran cleanly** —
no fallback to alias (`-A`) mode was needed. Alias mode remains available
(`scripts/package.sh -A`) purely as a faster dev-loop convenience (it
symlinks back to the source tree instead of copying it), not as a
distribution artifact.

## Launch evidence and its limits

`dist/speakeasy.app/Contents/MacOS/speakeasy`, ad-hoc signed, was launched
directly (not via Finder, to avoid Gatekeeper's quarantine-dialog loop)
and ran to completion of its full init sequence — model load, account
pool, overlay, recorder, history, hotkey listener — without a crash
across three timed runs. See the app-launch evidence log for
the captured output and a step-by-step diagnostic breakdown.

One finding worth recording: `speakeasy/hotkey.py` raises `PermissionError`
only when `Quartz.CGEventTapCreate` returns `None`; on this machine's
macOS build ("macOS 26" per README), `CGEventTapCreate` returned a valid
tap object even for the fresh, never-approved, ad-hoc-signed bundle (the
system TCC database, `TCC.db`, has zero Accessibility rows for anything
on this machine, confirming no pre-existing grant). This is a
macOS-version behavior difference from the older, more commonly
documented "returns NULL without trust" contract — the tap can
apparently be created without trust, it just presumably won't deliver
real events. That means this work order cannot produce the literal
`PermissionError` text as evidence on this OS; it can and does confirm
the bundle reaches and passes through that exact TCC-gated code path
cleanly. `speakeasy/hotkey.py` is outside this work order's file lease,
so no code change was made for this — it's a platform observation, not a
packaging defect. The practical grant step is unchanged and still
requires a human: see the README's updated "Packaging" section.

## Alternatives considered

1. **PyInstaller.** More popular project-wide, actively maintained, but
   its macOS `.app` support is a secondary target behind its primary
   onefile/onedir Windows/Linux focus; `LSUIElement`/menu-bar-app plist
   customization and native-framework (pyobjc/Quartz) bundling are less
   battle-tested than py2app's, which was purpose-built for exactly this
   "Python app as a native macOS `.app`" case. Rejected in favor of the
   tool whose whole reason to exist is this scenario. Also, the README's
   pre-existing "Packaging (release)" stub already sketched the py2app
   path, so this ADR follows established intent rather than introducing
   a new tool.
2. **Native Swift/AppKit shim that shells out to the Python process.**
   Would give the cleanest possible TCC identity (a real, small, notarizable
   app) and sidesteps py2app's native-library bundling friction entirely.
   Rejected for this work order: it's a second language/toolchain and
   build system to introduce and maintain, a substantially larger scope
   than "package the existing app," and doesn't remove the eventual need
   for a stable bundle ID and a human TCC-grant step anyway. Worth
   revisiting if notarized distribution to non-technical end users ever
   becomes a real requirement.
3. **`briefcase` (BeeWare).** Considered briefly; it targets
   cross-platform (iOS/Android/etc.) packaging with more opinionated
   project layout requirements than this single-platform, already-shaped
   codebase needed. Rejected as more machinery than the problem calls for.

## Consequences

- Privacy grants now attach to `org.speakeasy.dictation`, portable across
  terminals and stable across rebuilds (same bundle ID every build).
- `scripts/package.sh` is the one reproducible build path; `.venv-pkg/`
  is gitignored-equivalent to `.venv/` (not committed) and isolated from
  the shared dev environment.
- The human TCC-grant step (System Settings → Privacy & Security →
  Accessibility and → Microphone, adding `speakeasy.app`) is still
  required before the packaged app is actually usable — this ADR
  deliberately stops short of that, per the work order and per DA-3.
  See the README's "Packaging" section for the install runbook and a
  documented (not installed) LaunchAgent example for start-at-login.
- Notarization, Gatekeeper-clean first-run (no right-click-Open
  workaround), and Apple Developer Program signing remain unaddressed —
  explicitly out of scope. Distributing this build to another machine
  will trigger Gatekeeper's unidentified-developer warning on first
  Finder launch; running from Terminal (`open dist/speakeasy.app` after
  `xattr -dr com.apple.quarantine dist/speakeasy.app`, or a right-click →
  Open) is the workaround for internal/single-operator use.
