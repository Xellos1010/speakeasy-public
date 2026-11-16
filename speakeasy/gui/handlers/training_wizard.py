"""Bridge handlers for the training WIZARD window
(speakeasy/gui/wizard_window.py) -- real data only, no simulated/demo
state. Shares the SAME bridge registry (speakeasy/gui/bridge.py::ACTIONS)
as the Home window's handlers; the wizard's own WKUserContentController
just posts to a differently-named native handler ("wizardBridge" vs
"bridge") that calls the same bridge.dispatch(action, payload, ctx).

The live TrainingFlowController instance (if a session is active) is
reached via `ctx.controller.training` -- the same live object
`speakeasy/app.py`'s `Controller.on_tap_toggle` already routes hotkey taps
to (see `speakeasy/app.py` around Controller.__init__/on_tap_toggle); this
module never constructs its own TrainingFlowController or TrainingSession,
it only drives the one speakeasy/__main__.py already wired up.

Actions:
  training_wizard_get_state -> manifest snapshot (see _manifest_snapshot)
      plus a top-level "resumable" flag, mirroring training_get_state's
      "nothing yet" shape when no flow is active.
  training_wizard_toggle  -> TrainingFlowController.toggle() for the
      current prompt. While the resulting state is "recording", starts a
      real level-push loop (see _start_level_push) so the wizard's live
      waveform reflects the ACTUAL Recorder.level, not a fabricated
      envelope. Returns a clean {"ok": False, "error": ...} instead of
      raising when no flow is active (mirrors handlers/mics.py's
      "never raise, return a shaped error" convention).
  training_wizard_accept  -> TrainingFlowController.accept() (commits the
      held take). Same no-active-flow convention as toggle.
  training_wizard_retake  -> TrainingFlowController.retake() (discards the
      held take, no attempts increment). Same convention.
  training_wizard_start   -> payload {size: "quick"|"full"}; passthrough
      to ctx.start_training_wizard(size), whose actual session-creation +
      wizard-window-open(with overlay fallback) logic lives in
      speakeasy/__main__.py next to _begin_training (mirroring its
      shape -- see that module's _start_training_flow).
  training_wizard_resume  -> passthrough to ctx.resume_training_wizard().
  training_wizard_play_review -> plays the held take's samples (ADJ-SPK-035)
      via sounddevice.play(samples, SAMPLE_RATE) -- sounddevice is already
      a core dependency (speakeasy/audio.py records with it), so this adds
      no new one. Reads TrainingFlowController.pending_review_samples,
      which is None once accept()/retake() has cleared the held take, so
      playback only exists while a review is actually pending -- there is
      no separate lifecycle to keep in sync. A second play request while
      one is already playing restarts it (sd.stop() then sd.play() again)
      rather than overlapping two streams.
  training_wizard_mic_check_start -> payload {device: int|None}; starts a
      LIVE mic-check monitor: its own short-lived Recorder (independent of
      any TrainingFlowController) that discards whatever it accumulates,
      pushing "wizard_mic_level" ({"dbfs", "all_zero"}) on the same
      cadence as the recording waveform's "wizard_level" push -- a
      DISTINCT push event so the mic-check screen and the review/recording
      screens never fight over which one just fired. `device` falls back
      to `ctx.settings.input_device` if the payload omits the key
      (explicit `None` in the payload means "use system default" and is
      honored as-is, not treated as "omitted").
  training_wizard_mic_check_stop -> stops the monitor above. Safe to call
      when nothing is running (no-op, matching handlers/mics.py's
      never-raise convention).
  training_wizard_mic_check_set_device -> payload {device: int|None};
      stops any running monitor, persists the new device via
      `ctx.set_input_device` (speakeasy/__main__.py's `_set_input_device`,
      threaded onto GuiContext the same way start_training_wizard is),
      then restarts the monitor on whatever device that call actually
      resolved to (a stale/unplugged index is silently corrected to the
      system default there -- see speakeasy/gui/input_device.py). Returns
      ctx.set_input_device's own result unchanged, so the JS can detect
      `fell_back` and surface it.

Both training_wizard_start/resume merge in a fresh manifest snapshot
(if a flow ended up active) so the wizard's welcome screen can render the
first prompt immediately from the bridge response, rather than racing the
TrainingFlowController constructor's synchronous show_prompt() push
(which fires before the freshly-opened window's JS has necessarily
finished loading -- see speakeasy/gui/assets/training_wizard.html's
handling of the "wizard_prompt" push as a refresh signal, not a payload
of record).
"""
import math
import threading

from speakeasy.audio import Recorder
from speakeasy.gui.bridge import register
from speakeasy.overlay import level_to_bar_heights
from speakeasy.training.qc import SAMPLE_RATE

_LEVEL_INTERVAL = 0.12  # matches Controller._level_interval's default (speakeasy/app.py)
_NO_ACTIVE_SESSION = {"ok": False, "error": "no active training session"}
_NO_PENDING_REVIEW = {"ok": False, "error": "no take pending review"}

# Single-slot holder for the live mic-check monitor's Recorder (mirrors the
# single-instance singleton shape of speakeasy/gui/window.py's/
# wizard_window.py's `_instance`): at most one mic-check monitor is ever
# running. Module-level rather than ctx-attached because GuiContext is a
# plain dataclass of real backend refs, not a place to stash UI-only
# runtime state.
_mic_check_state = {"recorder": None}


def _live_flow(ctx):
    """The live TrainingFlowController, or None if no training session is
    currently active (ctx.controller.training is the same live-or-None
    reference speakeasy/app.py::Controller.on_tap_toggle already checks)."""
    return getattr(ctx.controller, "training", None)


def _manifest_snapshot(flow) -> dict:
    """Real, re-derivable-from-the-manifest snapshot: every prompt's
    id/text/source/status/attempts, accepted/total counts, the flow's own
    idle/recording/reviewing state, and the current (next-to-record)
    prompt if any. No fabricated fields -- everything here is either a
    ManifestPrompt attribute or a plain count over them."""
    manifest = flow.session.manifest
    prompts = [
        {
            "id": p.prompt_id,
            "text": p.text,
            "source": p.source,
            "status": p.status,
            "attempts": p.attempts,
        }
        for p in manifest.prompts
    ]
    accepted = sum(1 for p in manifest.prompts if p.status == "accepted")
    current = next(
        (p for p in manifest.prompts if p.status not in ("accepted", "low_quality")),
        None,
    )
    current_payload = None
    if current is not None:
        current_payload = {
            "id": current.prompt_id,
            "text": current.text,
            "source": current.source,
            "attempts": current.attempts,
        }
    return {
        "active": True,
        "state": flow.state,
        "prompts": prompts,
        "accepted": accepted,
        "total": len(manifest.prompts),
        "current": current_payload,
    }


def _inactive_snapshot() -> dict:
    return {"active": False, "state": None, "prompts": [], "accepted": 0,
            "total": 0, "current": None}


@register("training_wizard_get_state")
def training_wizard_get_state(_payload, ctx):
    flow = _live_flow(ctx)
    snapshot = _manifest_snapshot(flow) if flow is not None else _inactive_snapshot()
    snapshot["resumable"] = bool(ctx.training_resumable())
    return snapshot


def _start_level_push(flow, ctx, timer_cls=threading.Timer):
    """Push the recorder's REAL level (speakeasy/audio.py::Recorder.level)
    to the wizard window's live waveform on an interval, for as long as
    the flow is actually recording -- same shape
    ({"levels": [...]}, via the existing level_to_bar_heights helper) as
    the Home window's already-shipped "audio_level" push (see
    speakeasy/__main__.py's _render_and_update_icon). Stops itself the
    tick after recording ends: toggle() flips flow.state synchronously
    before this loop's next tick can observe a stale "recording" value, so
    no separate cross-thread stop-flag is needed."""
    from speakeasy.gui.wizard_window import get_or_create as _get_wizard
    window = _get_wizard(ctx)

    def _tick():
        if flow.state != "recording":
            return
        window.push("wizard_level", {"levels": level_to_bar_heights(flow.recorder.level)})
        timer = timer_cls(_LEVEL_INTERVAL, _tick)
        timer.daemon = True
        timer.start()

    _tick()


@register("training_wizard_toggle")
def training_wizard_toggle(_payload, ctx):
    flow = _live_flow(ctx)
    if flow is None:
        return dict(_NO_ACTIVE_SESSION)
    flow.toggle()
    if flow.state == "recording":
        _start_level_push(flow, ctx)
    return {"ok": True, "state": flow.state}


@register("training_wizard_accept")
def training_wizard_accept(_payload, ctx):
    flow = _live_flow(ctx)
    if flow is None:
        return dict(_NO_ACTIVE_SESSION)
    flow.accept()
    return {"ok": True}


@register("training_wizard_retake")
def training_wizard_retake(_payload, ctx):
    flow = _live_flow(ctx)
    if flow is None:
        return dict(_NO_ACTIVE_SESSION)
    flow.retake()
    return {"ok": True}


@register("training_wizard_start")
def training_wizard_start(payload, ctx):
    size = payload.get("size", "quick")
    result = dict(ctx.start_training_wizard(size))
    flow = _live_flow(ctx)
    if flow is not None:
        result.update(_manifest_snapshot(flow))
    return result


@register("training_wizard_resume")
def training_wizard_resume(_payload, ctx):
    result = dict(ctx.resume_training_wizard())
    flow = _live_flow(ctx)
    if flow is not None:
        result.update(_manifest_snapshot(flow))
    return result


def _play(samples, sounddevice_module=None):
    """Lazy-imports sounddevice by default (matches speakeasy/audio.py's own
    lazy-import discipline for this optional-hardware dependency); a test
    can inject a fake module instead via sounddevice_module."""
    sd = sounddevice_module
    if sd is None:
        import sounddevice as sd
    sd.stop()  # deterministic restart, never two overlapping streams
    sd.play(samples, SAMPLE_RATE)


@register("training_wizard_play_review")
def training_wizard_play_review(_payload, ctx):
    flow = _live_flow(ctx)
    if flow is None:
        return dict(_NO_ACTIVE_SESSION)
    samples = flow.pending_review_samples
    if samples is None:
        return dict(_NO_PENDING_REVIEW)
    _play(samples)
    return {"ok": True}


# --- mic-check monitor (TSK-SPK-MICSELECT) ----------------------------------


def _level_to_dbfs(level: float) -> tuple[float, bool]:
    """Convert a live Recorder.level RMS reading (0.0-1.0 scale) to
    (dbfs, all_zero).

    NOT speakeasy/training/qc.py::rms_dbfs -- that function operates on a
    samples array (a whole clip), not a single already-computed scalar
    level reading; this is the scalar equivalent of the same formula
    (20*log10(x)).

    `all_zero` is True only for an EXACT 0.0 reading -- this is the whole
    diagnostic point of the mic-check screen: an exact 0.0 means the input
    stream is receiving literally nothing (no permission grant, a muted/
    disconnected device), which is a DIFFERENT, more actionable problem
    than "receiving real but quiet audio" (a large negative but finite
    dBFS). Collapsing the two into one message would hide the fix (grant
    mic access) behind generic "move closer" advice.
    """
    if level == 0.0:
        return float("-inf"), True
    return 20.0 * math.log10(level), False


def _start_mic_check(ctx, device, recorder_cls=Recorder, timer_cls=threading.Timer):
    """Starts (or restarts) the live mic-check monitor on `device`.

    Unlike `_start_level_push` above (which rides the ALREADY-recording
    flow's own Recorder), this constructs its OWN short-lived Recorder
    purely to read `.level`; whatever it accumulates into `._chunks` is
    simply discarded (Recorder.start()'s per-callback-driven chunk list has
    no "monitor only" mode -- see speakeasy/audio.py). Always tears down any
    already-running monitor first (`_stop_mic_check`) so a second call
    while one is already running can never leak a stream.
    """
    _stop_mic_check()
    recorder = recorder_cls(device=device)
    recorder.start()
    _mic_check_state["recorder"] = recorder

    from speakeasy.gui.wizard_window import get_or_create as _get_wizard
    window = _get_wizard(ctx)

    def _tick():
        # Identity check (not just "is running"): if `_stop_mic_check` ran,
        # or a NEWER `_start_mic_check` call replaced this monitor, this
        # stale tick chain must not push or reschedule itself.
        if _mic_check_state["recorder"] is not recorder:
            return
        dbfs, all_zero = _level_to_dbfs(recorder.level)
        window.push("wizard_mic_level", {"dbfs": dbfs, "all_zero": all_zero})
        timer = timer_cls(_LEVEL_INTERVAL, _tick)
        timer.daemon = True
        timer.start()

    _tick()


def _stop_mic_check() -> None:
    """Stops the live mic-check monitor's Recorder, if one is running.
    No-op (never raises) if nothing is running -- safe to call
    unconditionally, e.g. on every screen transition out of the mic-check
    step and on training_wizard_mic_check_stop even when the JS never
    actually started one."""
    recorder = _mic_check_state["recorder"]
    if recorder is None:
        return
    _mic_check_state["recorder"] = None
    recorder.stop()


@register("training_wizard_mic_check_start")
def training_wizard_mic_check_start(payload, ctx):
    device = payload.get("device", ctx.settings.input_device)
    _start_mic_check(ctx, device)
    return {"ok": True}


@register("training_wizard_mic_check_stop")
def training_wizard_mic_check_stop(_payload, _ctx):
    _stop_mic_check()
    return {"ok": True}


@register("training_wizard_mic_check_set_device")
def training_wizard_mic_check_set_device(payload, ctx):
    _stop_mic_check()
    result = ctx.set_input_device(payload.get("device"))
    _start_mic_check(ctx, result.get("device"))
    return result
