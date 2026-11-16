import math

import pytest

from speakeasy.gui.handlers import training_wizard as tw
from speakeasy.training import qc as tw_qc
from speakeasy.training.session import ManifestPrompt


def _prompt(prompt_id, text, source="harvard", status="pending", attempts=0):
    return ManifestPrompt(prompt_id=prompt_id, source=source, text=text, split="train",
                          clip_path=f"clips/{prompt_id}.wav", status=status, attempts=attempts)


class _FakeManifest:
    def __init__(self, prompts):
        self.prompts = prompts


class _FakeSession:
    def __init__(self, prompts):
        self.manifest = _FakeManifest(prompts)


class _FakeRecorder:
    def __init__(self, level=0.0):
        self.level = level


class _FakeFlow:
    """Duck-types only what handlers/training_wizard.py actually touches
    on a live TrainingFlowController: .state, .session.manifest.prompts,
    .recorder.level, and toggle()/accept()/retake()."""

    def __init__(self, prompts, state="idle", pending_review_samples=None):
        self.session = _FakeSession(prompts)
        self.recorder = _FakeRecorder()
        self.state = state
        self.toggle_calls = 0
        self.accept_calls = 0
        self.retake_calls = 0
        self.next_state_after_toggle = state
        self.pending_review_samples = pending_review_samples

    def toggle(self):
        self.toggle_calls += 1
        self.state = self.next_state_after_toggle

    def accept(self):
        self.accept_calls += 1
        self.state = "idle"

    def retake(self):
        self.retake_calls += 1
        self.state = "idle"


class _FakeController:
    def __init__(self, training=None):
        self.training = training


class _FakeSettings:
    def __init__(self, input_device=None):
        self.input_device = input_device


class _FakeCtx:
    def __init__(self, training=None, resumable=False, start_result=None, resume_result=None,
                 input_device=None, set_input_device_result=None):
        self.controller = _FakeController(training)
        self._resumable = resumable
        self._start_result = start_result if start_result is not None else {"started": True, "surface": "wizard"}
        self._resume_result = resume_result if resume_result is not None else {"resumed": True, "surface": "wizard"}
        self.last_start_size = None
        self.resume_called = False
        self.settings = _FakeSettings(input_device)
        self.set_input_device_calls = []
        self._set_input_device_result = set_input_device_result

    def training_resumable(self):
        return self._resumable

    def start_training_wizard(self, size):
        self.last_start_size = size
        return self._start_result

    def resume_training_wizard(self):
        self.resume_called = True
        return self._resume_result

    def set_input_device(self, device):
        self.set_input_device_calls.append(device)
        if self._set_input_device_result is not None:
            return self._set_input_device_result
        return {"ok": True, "device": device, "fell_back": False}


# ---------------------------------------------------------------------------
# training_wizard_get_state
# ---------------------------------------------------------------------------


def test_get_state_no_active_flow_reports_inactive_shape():
    ctx = _FakeCtx(training=None, resumable=True)

    result = tw.training_wizard_get_state({}, ctx)

    assert result == {"active": False, "state": None, "prompts": [], "accepted": 0,
                       "total": 0, "current": None, "resumable": True}


def test_get_state_active_flow_reports_real_manifest_snapshot():
    prompts = [_prompt("harvard-0000", "a", status="accepted"),
               _prompt("harvard-0001", "b", status="pending", attempts=1)]
    flow = _FakeFlow(prompts, state="idle")
    ctx = _FakeCtx(training=flow, resumable=False)

    result = tw.training_wizard_get_state({}, ctx)

    assert result["active"] is True
    assert result["state"] == "idle"
    assert result["accepted"] == 1
    assert result["total"] == 2
    assert result["resumable"] is False
    assert result["current"] == {"id": "harvard-0001", "text": "b", "source": "harvard", "attempts": 1}
    assert [p["attempts"] for p in result["prompts"]] == [0, 1]


def test_get_state_current_is_none_once_everything_is_resolved():
    prompts = [_prompt("h-0", "a", status="accepted"),
               _prompt("h-1", "b", status="low_quality", attempts=3)]
    flow = _FakeFlow(prompts)
    ctx = _FakeCtx(training=flow)

    result = tw.training_wizard_get_state({}, ctx)

    assert result["current"] is None
    assert result["accepted"] == 1
    assert result["total"] == 2


# ---------------------------------------------------------------------------
# training_wizard_toggle
# ---------------------------------------------------------------------------


def test_toggle_no_active_flow_returns_clean_error_not_a_raise():
    ctx = _FakeCtx(training=None)

    result = tw.training_wizard_toggle({}, ctx)

    assert result == {"ok": False, "error": "no active training session"}


def test_toggle_calls_flow_toggle_and_reports_resulting_state():
    flow = _FakeFlow([], state="idle")
    flow.next_state_after_toggle = "idle"
    ctx = _FakeCtx(training=flow)

    result = tw.training_wizard_toggle({}, ctx)

    assert flow.toggle_calls == 1
    assert result == {"ok": True, "state": "idle"}


def test_toggle_starts_level_push_when_now_recording(monkeypatch):
    flow = _FakeFlow([], state="idle")
    flow.next_state_after_toggle = "recording"
    ctx = _FakeCtx(training=flow)
    calls = []
    monkeypatch.setattr(tw, "_start_level_push", lambda f, c: calls.append((f, c)))

    tw.training_wizard_toggle({}, ctx)

    assert calls == [(flow, ctx)]


def test_toggle_does_not_start_level_push_when_not_recording(monkeypatch):
    flow = _FakeFlow([], state="idle")
    flow.next_state_after_toggle = "idle"  # e.g. a QC failure returned it to idle/re-armed
    ctx = _FakeCtx(training=flow)
    calls = []
    monkeypatch.setattr(tw, "_start_level_push", lambda f, c: calls.append((f, c)))

    tw.training_wizard_toggle({}, ctx)

    assert calls == []


def test_start_level_push_pushes_real_recorder_level_via_level_to_bar_heights(monkeypatch):
    from speakeasy.overlay import level_to_bar_heights

    flow = _FakeFlow([], state="recording")
    flow.recorder.level = 0.2
    ctx = _FakeCtx(training=flow)

    pushed = []

    class _FakeWindow:
        def push(self, event, payload):
            pushed.append((event, payload))

    monkeypatch.setattr("speakeasy.gui.wizard_window.get_or_create", lambda ctx: _FakeWindow())

    scheduled = []

    class _FakeTimer:
        def __init__(self, interval, fn):
            self.interval = interval
            self.fn = fn
            self.daemon = False

        def start(self):
            scheduled.append(self)

    tw._start_level_push(flow, ctx, timer_cls=_FakeTimer)

    assert pushed == [("wizard_level", {"levels": level_to_bar_heights(0.2)})]
    assert len(scheduled) == 1
    assert scheduled[0].interval == tw._LEVEL_INTERVAL

    # Recording stopped before the next tick fires: no further push, no
    # further timer scheduled -- the loop must stop itself.
    flow.state = "idle"
    scheduled[0].fn()
    assert pushed == [("wizard_level", {"levels": level_to_bar_heights(0.2)})]
    assert len(scheduled) == 1


# ---------------------------------------------------------------------------
# training_wizard_accept / training_wizard_retake
# ---------------------------------------------------------------------------


def test_accept_no_active_flow_returns_clean_error():
    ctx = _FakeCtx(training=None)
    assert tw.training_wizard_accept({}, ctx) == {"ok": False, "error": "no active training session"}


def test_accept_calls_flow_accept():
    flow = _FakeFlow([])
    ctx = _FakeCtx(training=flow)

    result = tw.training_wizard_accept({}, ctx)

    assert flow.accept_calls == 1
    assert result == {"ok": True}


def test_retake_no_active_flow_returns_clean_error():
    ctx = _FakeCtx(training=None)
    assert tw.training_wizard_retake({}, ctx) == {"ok": False, "error": "no active training session"}


def test_retake_calls_flow_retake():
    flow = _FakeFlow([])
    ctx = _FakeCtx(training=flow)

    result = tw.training_wizard_retake({}, ctx)

    assert flow.retake_calls == 1
    assert result == {"ok": True}


# ---------------------------------------------------------------------------
# training_wizard_start / training_wizard_resume
# ---------------------------------------------------------------------------


def test_start_passes_size_through_and_merges_snapshot_when_active():
    prompts = [_prompt("h-0", "hello")]
    flow = _FakeFlow(prompts)
    ctx = _FakeCtx(training=flow, start_result={"started": True, "surface": "wizard"})

    result = tw.training_wizard_start({"size": "full"}, ctx)

    assert ctx.last_start_size == "full"
    assert result["started"] is True
    assert result["active"] is True
    assert result["current"] == {"id": "h-0", "text": "hello", "source": "harvard", "attempts": 0}


def test_start_defaults_to_quick_if_size_omitted():
    ctx = _FakeCtx()
    tw.training_wizard_start({}, ctx)
    assert ctx.last_start_size == "quick"


def test_start_without_a_resulting_active_flow_returns_ctx_result_only():
    ctx = _FakeCtx(training=None, start_result={"started": False, "reason": "boom"})
    result = tw.training_wizard_start({}, ctx)
    assert result == {"started": False, "reason": "boom"}


def test_resume_calls_ctx_resume_training_wizard_and_merges_snapshot():
    prompts = [_prompt("h-0", "hi", status="accepted")]
    flow = _FakeFlow(prompts)
    ctx = _FakeCtx(training=flow, resume_result={"resumed": True, "surface": "wizard"})

    result = tw.training_wizard_resume({}, ctx)

    assert ctx.resume_called is True
    assert result["resumed"] is True
    assert result["accepted"] == 1


def test_resume_no_resumable_session_passes_through_ctx_result():
    ctx = _FakeCtx(training=None, resume_result={"resumed": False, "reason": "no resumable session"})
    result = tw.training_wizard_resume({}, ctx)
    assert result == {"resumed": False, "reason": "no resumable session"}


# --- playback of the in-review take (ADJ-SPK-035) ---------------------------


class _FakeSounddevice:
    def __init__(self):
        self.stop_calls = 0
        self.play_calls = []  # list of (samples, samplerate)

    def stop(self):
        self.stop_calls += 1

    def play(self, samples, samplerate):
        self.play_calls.append((samples, samplerate))


def test_play_calls_sounddevice_stop_then_play_at_the_sample_rate():
    import numpy as np
    samples = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    sd = _FakeSounddevice()

    tw._play(samples, sounddevice_module=sd)

    assert sd.stop_calls == 1
    assert len(sd.play_calls) == 1
    played_samples, rate = sd.play_calls[0]
    assert played_samples is samples
    assert rate == tw.SAMPLE_RATE


def test_play_review_plays_exactly_the_pending_takes_samples(monkeypatch):
    import numpy as np
    samples = np.array([0.5, -0.5], dtype=np.float32)
    prompts = [_prompt("h-0", "hello")]
    flow = _FakeFlow(prompts, state="reviewing", pending_review_samples=samples)
    ctx = _FakeCtx(training=flow)
    played = []
    monkeypatch.setattr(tw, "_play", lambda s: played.append(s))

    result = tw.training_wizard_play_review({}, ctx)

    assert result == {"ok": True}
    assert len(played) == 1
    assert played[0] is samples


def test_play_review_errors_when_no_review_is_pending(monkeypatch):
    prompts = [_prompt("h-0", "hello")]
    flow = _FakeFlow(prompts, state="idle", pending_review_samples=None)
    ctx = _FakeCtx(training=flow)
    played = []
    monkeypatch.setattr(tw, "_play", lambda s: played.append(s))

    result = tw.training_wizard_play_review({}, ctx)

    assert result["ok"] is False
    assert played == []  # playback exists ONLY while a review is pending


def test_play_review_errors_when_no_active_session():
    ctx = _FakeCtx(training=None)
    result = tw.training_wizard_play_review({}, ctx)
    assert result == {"ok": False, "error": "no active training session"}


def test_play_review_after_accept_no_longer_has_samples_to_replay():
    """Samples lifecycle unchanged: once accept() has cleared
    _pending_review, pending_review_samples reads back None -- there is no
    separate handle kept alive for a post-accept replay."""
    prompts = [_prompt("h-0", "hello")]
    flow = _FakeFlow(prompts, state="reviewing", pending_review_samples=None)
    flow.accept_calls = 1  # simulating "accept() already ran and cleared it"
    ctx = _FakeCtx(training=flow)

    result = tw.training_wizard_play_review({}, ctx)

    assert result == {"ok": False, "error": "no take pending review"}


# ---------------------------------------------------------------------------
# mic-check monitor (_level_to_dbfs, training_wizard_mic_check_*)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_mic_check_state():
    """`_mic_check_state` is deliberately module-level (a single-slot
    singleton, see the module docstring) -- reset it before AND after every
    test in this file so a monitor "started" by one test can never leak
    into another's assertions."""
    tw._mic_check_state["recorder"] = None
    yield
    tw._mic_check_state["recorder"] = None


def test_level_to_dbfs_exact_zero_is_all_zero_and_negative_infinity():
    dbfs, all_zero = tw._level_to_dbfs(0.0)
    assert all_zero is True
    assert dbfs == float("-inf")


def test_level_to_dbfs_tiny_nonzero_level_is_very_quiet_not_all_zero():
    dbfs, all_zero = tw._level_to_dbfs(0.001)
    assert all_zero is False
    assert dbfs == pytest.approx(20.0 * math.log10(0.001))
    assert dbfs < tw_qc.MIN_RMS_DBFS  # -60 dBFS, well under the -45 floor


def test_level_to_dbfs_normal_level_is_a_normal_finite_number_not_all_zero():
    dbfs, all_zero = tw._level_to_dbfs(0.2)
    assert all_zero is False
    assert dbfs == pytest.approx(20.0 * math.log10(0.2))
    assert dbfs >= tw_qc.MIN_RMS_DBFS


class _FakeMonitorRecorder:
    """Duck-types only what _start_mic_check/_stop_mic_check touch: a
    settable .level, start()/stop()."""

    def __init__(self, device=None):
        self.device = device
        self.level = 0.0
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class _FakeMicWindow:
    def __init__(self):
        self.pushed = []

    def push(self, event, payload):
        self.pushed.append((event, payload))


class _FakeMicTimer:
    scheduled = []

    def __init__(self, interval, fn):
        self.interval = interval
        self.fn = fn
        self.daemon = False

    def start(self):
        _FakeMicTimer.scheduled.append(self)


def test_start_mic_check_constructs_recorder_starts_it_and_pushes_a_reading(monkeypatch):
    _FakeMicTimer.scheduled = []
    window = _FakeMicWindow()
    monkeypatch.setattr("speakeasy.gui.wizard_window.get_or_create", lambda ctx: window)
    ctx = _FakeCtx()
    built = []

    def _recorder_cls(device=None):
        r = _FakeMonitorRecorder(device=device)
        r.level = 0.3
        built.append(r)
        return r

    tw._start_mic_check(ctx, 2, recorder_cls=_recorder_cls, timer_cls=_FakeMicTimer)

    assert len(built) == 1
    assert built[0].device == 2
    assert built[0].started is True
    dbfs, all_zero = tw._level_to_dbfs(0.3)
    assert window.pushed == [("wizard_mic_level", {"dbfs": dbfs, "all_zero": all_zero})]
    assert len(_FakeMicTimer.scheduled) == 1


def test_start_mic_check_while_one_already_running_stops_the_old_recorder_first(monkeypatch):
    _FakeMicTimer.scheduled = []
    window = _FakeMicWindow()
    monkeypatch.setattr("speakeasy.gui.wizard_window.get_or_create", lambda ctx: window)
    ctx = _FakeCtx()
    built = []

    def _recorder_cls(device=None):
        r = _FakeMonitorRecorder(device=device)
        built.append(r)
        return r

    tw._start_mic_check(ctx, 0, recorder_cls=_recorder_cls, timer_cls=_FakeMicTimer)
    first = built[0]
    tw._start_mic_check(ctx, 1, recorder_cls=_recorder_cls, timer_cls=_FakeMicTimer)

    assert first.stopped is True  # no leaked stream from the first start()
    assert len(built) == 2
    assert built[1].device == 1
    assert tw._mic_check_state["recorder"] is built[1]


def test_stop_mic_check_stops_the_recorder_and_no_further_ticks_push(monkeypatch):
    _FakeMicTimer.scheduled = []
    window = _FakeMicWindow()
    monkeypatch.setattr("speakeasy.gui.wizard_window.get_or_create", lambda ctx: window)
    ctx = _FakeCtx()
    recorder = _FakeMonitorRecorder()

    tw._start_mic_check(ctx, None, recorder_cls=lambda device=None: recorder, timer_cls=_FakeMicTimer)
    assert len(window.pushed) == 1
    pending_tick = _FakeMicTimer.scheduled[0].fn

    tw._stop_mic_check()

    assert recorder.stopped is True
    assert tw._mic_check_state["recorder"] is None
    # The already-scheduled tick fires anyway (a real threading.Timer can't
    # be un-fired retroactively) -- it must detect the stop and not push
    # again or reschedule itself.
    pending_tick()
    assert len(window.pushed) == 1
    assert len(_FakeMicTimer.scheduled) == 1


def test_stop_mic_check_is_a_safe_noop_when_nothing_is_running():
    tw._stop_mic_check()  # must not raise
    assert tw._mic_check_state["recorder"] is None


def test_mic_check_start_handler_uses_payload_device(monkeypatch):
    calls = []
    monkeypatch.setattr(tw, "_start_mic_check", lambda ctx, device: calls.append(device))
    ctx = _FakeCtx(input_device=5)

    result = tw.training_wizard_mic_check_start({"device": 2}, ctx)

    assert calls == [2]
    assert result == {"ok": True}


def test_mic_check_start_handler_falls_back_to_settings_input_device_when_omitted(monkeypatch):
    calls = []
    monkeypatch.setattr(tw, "_start_mic_check", lambda ctx, device: calls.append(device))
    ctx = _FakeCtx(input_device=7)

    tw.training_wizard_mic_check_start({}, ctx)

    assert calls == [7]


def test_mic_check_start_handler_honors_explicit_none_as_system_default(monkeypatch):
    calls = []
    monkeypatch.setattr(tw, "_start_mic_check", lambda ctx, device: calls.append(device))
    ctx = _FakeCtx(input_device=7)

    tw.training_wizard_mic_check_start({"device": None}, ctx)

    assert calls == [None]  # explicit None in the payload, not "omitted"


def test_mic_check_stop_handler_calls_stop_mic_check(monkeypatch):
    calls = []
    monkeypatch.setattr(tw, "_stop_mic_check", lambda: calls.append(True))
    ctx = _FakeCtx()

    result = tw.training_wizard_mic_check_stop({}, ctx)

    assert calls == [True]
    assert result == {"ok": True}


def test_mic_check_set_device_stops_persists_and_restarts_on_resolved_device(monkeypatch):
    stop_calls = []
    start_calls = []
    monkeypatch.setattr(tw, "_stop_mic_check", lambda: stop_calls.append(True))
    monkeypatch.setattr(tw, "_start_mic_check", lambda ctx, device: start_calls.append(device))
    ctx = _FakeCtx(set_input_device_result={"ok": True, "device": 3, "fell_back": False})

    result = tw.training_wizard_mic_check_set_device({"device": 3}, ctx)

    assert stop_calls == [True]
    assert ctx.set_input_device_calls == [3]
    assert start_calls == [3]  # restarts on whatever ctx.set_input_device resolved to
    assert result == {"ok": True, "device": 3, "fell_back": False}


def test_mic_check_set_device_restarts_on_the_fallback_device_when_stale(monkeypatch):
    start_calls = []
    monkeypatch.setattr(tw, "_stop_mic_check", lambda: None)
    monkeypatch.setattr(tw, "_start_mic_check", lambda ctx, device: start_calls.append(device))
    ctx = _FakeCtx(set_input_device_result={
        "ok": True, "device": None, "fell_back": True,
        "reason": "input device 9 is no longer available; using the system default",
    })

    result = tw.training_wizard_mic_check_set_device({"device": 9}, ctx)

    assert ctx.set_input_device_calls == [9]
    assert start_calls == [None]  # restarted on the FALLBACK device, not the stale request
    assert result["fell_back"] is True
