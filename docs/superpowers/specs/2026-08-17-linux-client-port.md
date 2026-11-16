# Linux client port — spec (ADJ-SPK-003)

Status: DRAFT — scoping only, no implementation on master. Routes back to SpeakEasy-Advisor
for adjudication before any implementation branch is cut.

Scope boundary (ADJ-SPK-006): this spec covers a **single-host** Linux dictation client —
hotkey capture, text injection, engine settings — identical in shape to the existing macOS
app. It does NOT cover the cross-host network STT service (separate spec, ADJ-SPK-006). The
seam between the two is `transcribe(audio) -> text`, satisfied today by a local
`speakeasy.engine.parakeet.ParakeetEngine` call; a future remote backend implements the same
signature and is swapped in without touching anything below.

Target environment verified on this host: Ubuntu 24.04, GNOME, **X11** (not Wayland) —
`XDG_SESSION_TYPE=x11`. Everything below assumes X11. A Wayland host would need a materially
different approach (portal APIs / evdev+uinput with udev rules) and is out of scope here.

## 1. Parity matrix — macOS `hotkey.py` -> Linux X11

| macOS (`speakeasy/hotkey.py`) | Linux X11 equivalent | Notes |
|---|---|---|
| `Quartz.CGEventTapCreate` (session-wide event tap) | `pynput.keyboard.Listener` (X11 backend: `python-xlib` `XGrabKey`/record extension under the hood) | No Accessibility-style permission prompt on X11 — any client on the same X server can already read global key events. This is a real, pre-existing X11 property, not something this app introduces; worth a one-line README note since it's a meaningfully different trust model than macOS's explicit Accessibility grant. |
| `kCGEventFlagsChanged` (modifier-only events, keycode 54 = Right Command) | X11 has no Right-Command key. Closest hardware analog is **Right Super/Meta** (keysym `Super_R`), unclaimed by GNOME defaults on this box. Alternatives: Right Ctrl (heavily used in terminals — bad choice) or Right Alt (often remapped to AltGr by layout — bad choice). | **Open decision for the operator**, not decided here: default to `Super_R`, but keep it config-driven exactly like the existing `int` keycode legacy path, so it's a one-line change either way. |
| `ModifierHotkeyStateMachine` (tap-toggle / hold-PTT / chord-guard, pure state machine, no Quartz objects) | **Ported as-is, unchanged.** It already takes primitives (`event_type`, `keycode`, `modifier_flag_set`, `timestamp`) — zero Quartz-specific code inside it. The only new code is the X11-side adapter that feeds it from `pynput`/`Xlib` events instead of `CGEventTapCreate` callbacks. | This is the single biggest reuse win in the whole port — the hard-won tap/hold/chord logic (and its unit tests) needs no changes at all. |
| `CGEventGetFlags` / `kCGEventFlagMaskCommand` | `pynput` reports modifier state via `Key.super_r`/`Key.super` press/release events directly (no bitmask read needed) — adapter tracks down/up itself and derives the same `cmd_flag_set` boolean the state machine expects. | |
| `PermissionError` on failed `CGEventTapCreate` (no Accessibility grant) | X11 grab can fail if another client already holds an exclusive grab on that key; adapter should raise an equivalent, clearly-worded error rather than failing silently. | Low-probability failure mode (Super_R is rarely pre-grabbed), still worth a clear error per the existing error-message quality bar. |

**Reuse verdict (amended per ADJ-SPK-007):** the *logic* in `ModifierHotkeyStateMachine` and
its event-kind primitives (`EVENT_FLAGS_CHANGED`, `EVENT_KEY_DOWN`, the `ACTION_*` constants)
ports with zero changes — but `speakeasy/hotkey.py` as a *module* cannot even be imported on
Linux today: it has a top-level `import Quartz` (line 4) plus a Quartz-using helper,
`should_toggle` (line 21-23). This is a required extraction, not optional cleanup: pull the
state machine + primitives (and their existing unit tests) into a new platform-neutral module
(e.g. `speakeasy/hotkey_core.py`); `speakeasy/hotkey.py` keeps `Quartz`, `HotkeyListener`, and
`should_toggle` as the mac-specific adapter shell around it; a new `speakeasy/hotkey_linux.py`
is the X11 adapter shell around the same core. Same pattern `sysinfo.py`'s cross-platform
intent already implied but didn't yet deliver.

## 2. Injection story — macOS `injector.py` -> Linux X11

macOS flow: save clipboard (`NSPasteboard`) -> set clipboard to dictated text -> post synthetic
Cmd-V -> sleep `restore_delay` -> restore old clipboard. Same shape ports directly; the pieces
that change are clipboard access and the paste keystroke.

| Piece | macOS | Linux X11 options | Recommendation |
|---|---|---|---|
| Clipboard get/set | `NSPasteboard` (Cocoa, always available) | (a) `xclip`/`xsel` CLI + `pyperclip` wrapping them — **not installed on this box, needs `apt install xclip`** (sudo, another blocker hit repeatedly today); (b) implement `CLIPBOARD` selection ownership directly via `python-xlib` (`XSetSelectionOwner` + `SelectionRequest` handling) — pure pip, no system package. | **(b)** — avoids adding a system-package requirement the README doesn't currently have, and avoids depending on the user having `xclip` pre-installed. More code than shelling out to `xclip`, but it's a self-contained, well-documented X11 pattern. |
| Paste keystroke | `Quartz.CGEventCreateKeyboardEvent` + `CGEventPost` (Cmd-V) | `xdotool key --clearmodifiers ctrl+v` (already installed on this box) via `subprocess`, OR synthesize the same XTest events `xdotool` itself uses, directly via `python-xlib`'s `Xlib.ext.xtest`. | Start with **`xdotool` via subprocess** — it's already present, it's a single well-tested external call, and it sidesteps hand-rolling XTest key-event synthesis. Revisit a pure-Xlib XTest path later only if shelling out proves unreliable in practice (e.g. under a compositor quirk). |
| "Secure input" analog (macOS `IsSecureEventInputEnabled`) | Carbon API | No direct X11 equivalent — X11 has no per-app secure-input mode. The closest real risk is a focused password field; there is no reliable, portable way to detect that on X11 without app-specific cooperation. | Document as a known gap rather than fake a check that can't actually detect the condition — don't reproduce `secure_input_active()`'s behavior by always returning `False` (that would silently claim a guarantee that doesn't exist on this platform). |
| Direct text injection alternative to paste | (not used on Mac — paste-only) | `xdotool type -- "<text>"` types each character directly, no clipboard involved at all. | Worth offering as a config option (`injection_mode: paste \| type`), since it sidesteps clipboard save/restore entirely — trade-off is `type` is slower for long dictations and some apps' IME/autocomplete can interfere with raw synthetic typing where paste wouldn't. |

**Clipboard save/restore correctness note:** the macOS `inject()`'s try/finally (always restore
old clipboard, even if paste itself throws) is a correctness property worth preserving exactly,
not just the happy path — the Linux port's equivalent function should keep the same
try/finally shape around a get/set/paste/restore sequence.

## 3. R-01 compliance notes

This spec is entirely single-host: capture, state machine, and injection all happen on the
same machine, in-process, with no network involvement of any kind. It complies with R-01
(as amended) trivially — there is nothing here for the amendment's constraints (a)–(e) to
apply to. The only place R-01 becomes relevant to this code is the seam mentioned above:
`transcribe(audio) -> text` is the single point where a future remote backend (ADJ-SPK-006's
service spec) would plug in. This spec deliberately does not touch that seam's networking
side — it only guarantees the seam exists cleanly (i.e., the Linux client's hotkey/injection
code never assumes the engine is local, it just calls an injected `transcribe` callable).

**Seam correction (per ADJ-SPK-007):** the seam is richer than `transcribe(audio) -> str`.
`ParakeetEngine` also exposes `transcribe_ex(audio) -> TranscriptionResult` (text +
`avg_logprob`), and the live dictation path uses that richer type, not the plain string. Noted
here so ADJ-SPK-006's service spec designs its wire response around `TranscriptionResult` from
the start — a string-only remote backend would silently drop `avg_logprob` and anything else
added to that type later.

## 4. Per-host settings persistence (R-06)

Measured, not assumed, per R-06 — this box's actual numbers from today's benchmark:

```json
{
  "host_id": "linux-host",
  "tailscale_ip": "192.0.2.10",
  "updated": "2026-08-17",
  "engine": {
    "name": "parakeet",
    "provider": "cpu",
    "num_threads": 4,
    "measured_p50_ms": {"3s_clip": 211.9, "5s_clip": 320.3, "8s_clip": 479.3},
    "measured_p95_ms": {"3s_clip": 216.1, "5s_clip": 324.4, "8s_clip": 484.9},
    "gpu_available": true,
    "gpu_provider_measured_slower": true,
    "gpu_measured_p50_ms": {"3s_clip": 245.2, "5s_clip": 370.4, "8s_clip": 550.4}
  }
}
```

Shape notes:
- One such record per host, keyed by `host_id`; lives wherever the eventual priority-ranking
  config lives (host-selection logic reads these, never re-benchmarks at runtime).
- `gpu_available` and `gpu_provider_measured_slower` are recorded explicitly, not implied by
  omission — a future re-benchmark (newer sherpa-onnx, newer driver) might flip this, and the
  record should show a measurement was taken, not just a current conclusion.
- Where this record physically lives (this box only? synced to the other hosts via the pm/
  metadata channel R-01(e) still permits?) is a question for the network-service spec
  (ADJ-SPK-006), not this one — noted here only so the shape is consistent when that spec
  picks it up.

## Open decisions for the operator (not blocking this spec's adjudication)

1. Hotkey default: `Super_R` vs. something else (see §1).
2. Injection mode default: `paste` (clipboard, matches Mac) vs. `type` (direct, no clipboard) —
   or expose both and let the operator pick per-host.
3. Whether `xclip`/`xsel` (simpler, but a new system-package requirement) is preferable to a
   pure-Xlib clipboard implementation (self-contained, more code) — leaning pure-Xlib per §2,
   but flagging since it's a real trade-off, not a clear-cut call.
