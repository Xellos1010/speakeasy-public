import logging
import numpy as np
from speakeasy.app import Controller, TrainingFlowController, training_resume_available
from speakeasy.config import Settings
from speakeasy.history import History
from speakeasy.modes import Mode
from speakeasy.rewrite import RewriteResult, RewriteError
from speakeasy.training.qc import QCResult
from speakeasy.training.session import TrainingSession

class FakeRecorder:
    def __init__(self, samples): self._s = samples; self.started = 0
    def start(self): self.started += 1
    def stop(self): return self._s
    def snapshot(self): return self._s
    level = 0.1

class FakeEngine:
    def __init__(self, text="hello world"): self.text = text
    def transcribe(self, samples, sample_rate=16000): return self.text

def make(tmp_path, mode="raw", samples=None, rewrite_fn=None, inject_log=None, postprocess=None):
    samples = samples if samples is not None else np.zeros(16000, np.float32)
    inject_log = inject_log if inject_log is not None else []
    modes = {"raw": Mode("raw", None),
             "clean": Mode("clean", "Clean it.")}
    s = Settings(active_mode=mode)
    h = History(tmp_path / "h.db")
    c = Controller(
        engine=FakeEngine(), recorder=FakeRecorder(samples), history=h,
        modes=modes, pool=None, settings=s,
        inject_fn=lambda text: inject_log.append(text) or "injected",
        rewrite_fn=rewrite_fn or (lambda *a, **k: RewriteResult("CLEANED", "acct-a", 5)),
        run_async=False, postprocess=postprocess)
    return c, h, inject_log

def test_raw_dictation_flow(tmp_path):
    c, h, log = make(tmp_path)
    c.toggle(); assert c.state == "recording"
    c.toggle(); assert c.state == "idle"
    assert log == ["hello world"]
    row = h.recent()[0]
    assert row["status"] == "injected" and row["raw"] == "hello world"

def test_llm_mode_rewrites(tmp_path):
    c, h, log = make(tmp_path, mode="clean")
    c.toggle(); c.toggle()
    assert log == ["CLEANED"]
    row = h.recent()[0]
    assert row["rewritten"] == "CLEANED" and row["account"] == "acct-a"

def test_rewrite_failure_falls_back_to_raw(tmp_path):
    def boom(*a, **k): raise RewriteError("all cooling")
    c, h, log = make(tmp_path, mode="clean", rewrite_fn=boom)
    c.toggle(); c.toggle()
    assert log == ["hello world"]
    assert h.recent()[0]["status"] == "rewrite_failed"

def test_short_audio_discarded(tmp_path):
    c, h, log = make(tmp_path, samples=np.zeros(1000, np.float32))
    c.toggle(); c.toggle()
    assert log == [] and h.recent() == []

# --- post-dictation scratchpad (TSK-SPK-UX-SCRATCHPAD): _finish() must show
# what was transcribed/injected instead of silently hiding, since automatic
# ⌘V injection has no way to confirm anything was actually focused. ---------

def test_raw_dictation_shows_scratchpad_with_injected_text(tmp_path):
    rendered = []
    c, h, log = make(tmp_path)
    c._render = lambda m: rendered.append((m.state, m.partial, m.message))
    c.toggle(); c.toggle()
    assert ("done", "hello world", "injected") in rendered

def test_rewrite_shows_scratchpad_with_rewritten_text(tmp_path):
    rendered = []
    c, h, log = make(tmp_path, mode="clean")
    c._render = lambda m: rendered.append((m.state, m.partial, m.message))
    c.toggle(); c.toggle()
    assert ("done", "CLEANED", "injected") in rendered

def test_rewrite_failure_shows_scratchpad_with_raw_fallback_text(tmp_path):
    def boom(*a, **k): raise RewriteError("all cooling")
    rendered = []
    c, h, log = make(tmp_path, mode="clean", rewrite_fn=boom)
    c._render = lambda m: rendered.append((m.state, m.partial, m.message))
    c.toggle(); c.toggle()
    assert ("done", "hello world", "rewrite_failed") in rendered

def test_discarded_silence_hides_without_scratchpad(tmp_path):
    rendered = []
    c, h, log = make(tmp_path, samples=np.zeros(1000, np.float32))
    c._render = lambda m: rendered.append(m.state)
    c.toggle(); c.toggle()
    assert "done" not in rendered
    assert rendered[-1] == "hidden"

def test_scratchpad_auto_hides_after_duration_if_still_idle(tmp_path):
    rendered = []
    c, h, log = make(tmp_path)
    c._render = lambda m: rendered.append(m.state)
    c.toggle(); c.toggle()
    assert rendered[-1] == "done"
    # exercise the guarded hide callback synchronously (avoid a real sleep in tests)
    import threading as _t
    captured = {}
    orig_timer = _t.Timer
    def fake_timer(duration, fn):
        captured["fn"] = fn
        return orig_timer(0, lambda: None)
    _t.Timer = fake_timer
    try:
        c._schedule_result_hide()
        captured["fn"]()
    finally:
        _t.Timer = orig_timer
    assert rendered[-1] == "hidden"

def test_scratchpad_hide_noop_if_new_recording_started(tmp_path):
    rendered = []
    c, h, log = make(tmp_path)
    c._render = lambda m: rendered.append(m.state)
    c.toggle(); c.toggle()  # first dictation -> done
    c.toggle()              # starts a NEW recording before the hide timer fires
    assert c.state == "recording"
    rendered.clear()
    import threading as _t
    captured = {}
    orig_timer = _t.Timer
    def fake_timer(duration, fn):
        captured["fn"] = fn
        return orig_timer(0, lambda: None)
    _t.Timer = fake_timer
    try:
        c._schedule_result_hide()
        captured["fn"]()
    finally:
        _t.Timer = orig_timer
    assert rendered == []  # guarded: state is "recording", not "idle" -> no-op

def test_partial_rendering(tmp_path):
    rendered = []
    c, h, log = make(tmp_path)
    c._render = lambda m: rendered.append((m.state, m.partial))
    c._tick_partial()          # simulate one timer firing while idle -> no-op
    c.toggle()                 # recording
    c._tick_partial()
    assert ("recording", "hello world") in rendered

def test_level_rendering(tmp_path):
    rendered = []
    c, h, log = make(tmp_path)
    c._render = lambda m: rendered.append((m.state, m.level))
    c._tick_level()             # idle -> no-op
    assert rendered == []
    c.toggle()                  # recording (FakeRecorder.level == 0.1)
    c._tick_level()
    assert ("recording", 0.1) in rendered

def test_level_rendering_carries_last_partial_text(tmp_path):
    rendered = []
    c, h, log = make(tmp_path)
    c._render = lambda m: rendered.append((m.state, m.partial, m.level))
    c.toggle()
    c._tick_partial()            # sets _last_partial = "hello world"
    c._tick_level()
    assert ("recording", "hello world", 0.1) in rendered

# --- hotkey callbacks: on_tap_toggle / on_hold_start / on_hold_end ----------

def test_on_tap_toggle_behaves_like_toggle(tmp_path):
    c, h, log = make(tmp_path)
    c.on_tap_toggle(); assert c.state == "recording"
    c.on_tap_toggle(); assert c.state == "idle"
    assert log == ["hello world"]

def test_hold_start_then_hold_end_is_push_to_talk(tmp_path):
    c, h, log = make(tmp_path)
    c.on_hold_start(); assert c.state == "recording"
    c.on_hold_end(); assert c.state == "idle"
    assert log == ["hello world"]

def test_hold_end_without_hold_start_is_a_no_op(tmp_path):
    c, h, log = make(tmp_path)
    c.on_hold_end()             # no matching hold-start; must not stop/crash
    assert c.state == "idle"
    assert log == []

def test_hold_end_does_not_stop_a_tap_started_recording(tmp_path):
    c, h, log = make(tmp_path)
    c.on_tap_toggle()           # tap-started recording, not hold
    assert c.state == "recording"
    c.on_hold_end()             # a stray hold-release must not stop it
    assert c.state == "recording"
    c.on_tap_toggle()           # clean up via tap
    assert c.state == "idle"

def test_hold_start_while_already_recording_is_ignored(tmp_path):
    c, h, log = make(tmp_path)
    c.on_tap_toggle()           # recording via tap
    c.on_hold_start()           # must not double-start / raise
    assert c.state == "recording"
    c.on_tap_toggle()
    assert c.state == "idle"

# --- training flow (TSK-SPK-TRAIN-T4) ---------------------------------------


class FakeTrainingRecorder:
    def __init__(self, samples):
        self._s = samples
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1
        return self._s

    level = 0.1


def make_qc_result(passed: bool) -> QCResult:
    if passed:
        return QCResult(clipping=False, silence_ratio=0.9, rms_dbfs=-10.0, snr_db=30.0, passed=True)
    return QCResult(clipping=False, silence_ratio=0.1, rms_dbfs=-60.0, snr_db=1.0, passed=False)


def make_training_session(tmp_path, size=2, seed=0):
    """A tiny real TrainingSession (no history/vocab) backed by a small
    vendored-style corpus -- exercises the real manifest/split/QC-recording
    machinery from T3, not a fake."""
    corpus = tmp_path / "harvard.txt"
    corpus.write_text(
        "\n".join(f"Sentence number {i}." for i in range(10)) + "\n", encoding="utf-8")
    sessions_dir = tmp_path / "sessions"
    session = TrainingSession.create(
        sessions_dir, size=size, corpus_path=corpus,
        history_db=None, vocab_path=None, seed=seed)
    return session, sessions_dir


def test_training_mode_routes_hotkey_to_training_recorder_not_dictation(tmp_path):
    c, h, log = make(tmp_path)                    # dictation controller (untouched baseline)
    session, _ = make_training_session(tmp_path)
    prompts_shown = []
    train_recorder = FakeTrainingRecorder(np.zeros(16000, np.float32))
    flow = TrainingFlowController(
        recorder=train_recorder, session=session,
        show_prompt=lambda t: prompts_shown.append(t),
        qc_fn=lambda samples: make_qc_result(True))
    c.training = flow

    c.on_tap_toggle()                              # should start TRAINING recording only
    assert train_recorder.started == 1
    assert c.recorder.started == 0                 # dictation FakeRecorder never touched
    assert c.state == "idle"                       # dictation Controller.state untouched

    c.on_tap_toggle()                              # stop -> QC pass -> advance
    assert train_recorder.stopped == 1
    assert log == []                               # no dictation injection ever happened
    assert c.recorder.started == 0


def test_dictation_untouched_when_no_training_session_active(tmp_path):
    """Regression guard (spec T4): with `Controller.training` at its default
    of `None`, hotkey behavior is exactly as it was before this feature."""
    c, h, log = make(tmp_path)
    assert c.training is None
    c.on_tap_toggle(); assert c.state == "recording"
    c.on_tap_toggle(); assert c.state == "idle"
    assert log == ["hello world"]
    row = h.recent()[0]
    assert row["status"] == "injected" and row["raw"] == "hello world"


def test_qc_fail_reprompts_up_to_three_attempts_then_advances(tmp_path):
    session, _ = make_training_session(tmp_path, size=2)
    prompts_shown = []
    qc_failure_attempts = []
    results = iter([make_qc_result(False), make_qc_result(False), make_qc_result(False)])
    flow = TrainingFlowController(
        recorder=FakeTrainingRecorder(np.zeros(16000, np.float32)),
        session=session,
        show_prompt=lambda t: prompts_shown.append(t),
        show_qc_failure=lambda reason, attempts: qc_failure_attempts.append(attempts),
        qc_fn=lambda samples: next(results))

    first_prompt_text = prompts_shown[0]            # announced by the constructor
    for _ in range(3):
        flow.toggle()                                # start recording
        flow.toggle()                                # stop -> QC fails

    assert qc_failure_attempts == [1, 2, 3]
    # attempts 1-2 re-prompt the SAME prompt; attempt 3 exhausts MAX_ATTEMPTS,
    # flags low_quality, and advances to the next prompt instead.
    assert prompts_shown[1] == first_prompt_text
    assert prompts_shown[2] == first_prompt_text
    assert prompts_shown[3] != first_prompt_text

    first = session.manifest.prompts[0]
    assert first.status == "low_quality"
    assert first.attempts == 3


def test_session_completes_fires_summary_with_correct_counts(tmp_path):
    session, _ = make_training_session(tmp_path, size=2)
    summaries = []
    flow = TrainingFlowController(
        recorder=FakeTrainingRecorder(np.zeros(16000, np.float32)),
        session=session,
        show_summary=lambda s: summaries.append(s),
        qc_fn=lambda samples: make_qc_result(True))

    flow.toggle(); flow.toggle()                     # prompt 1: accepted
    assert flow.done is False
    flow.toggle(); flow.toggle()                     # prompt 2: accepted -> session complete

    assert flow.done is True
    assert len(summaries) == 1
    assert summaries[0]["n_clips"] == 2
    assert summaries[0]["n_rejected"] == 0
    assert session.manifest.status == "complete"


def test_training_resume_available_enablement(tmp_path):
    sessions_dir = tmp_path / "sessions"
    assert training_resume_available(sessions_dir) is False    # no manifest yet

    session, _ = make_training_session(tmp_path, size=1)
    assert training_resume_available(sessions_dir) is True     # in_progress manifest exists

    flow = TrainingFlowController(
        recorder=FakeTrainingRecorder(np.zeros(16000, np.float32)),
        session=session, qc_fn=lambda samples: make_qc_result(True))
    flow.toggle(); flow.toggle()                               # single prompt -> session completes

    assert training_resume_available(sessions_dir) is False    # complete, no longer resumable


# --- training wizard human-review hook (ADJ-SPK-033) -------------------------


def test_show_review_absent_is_byte_identical_legacy_autocommit(tmp_path):
    """C1: constructing without show_review (the default, None) behaves
    exactly as before this feature existed -- QC pass auto-commits, no
    reviewing state is ever entered."""
    session, _ = make_training_session(tmp_path, size=1)
    flow = TrainingFlowController(
        recorder=FakeTrainingRecorder(np.zeros(16000, np.float32)),
        session=session, qc_fn=lambda samples: make_qc_result(True))
    flow.toggle()                                   # start
    flow.toggle()                                   # stop -> QC pass -> auto-commit
    assert flow.state == "idle"
    assert session.manifest.prompts[0].status == "accepted"


def test_qc_pass_holds_for_review_instead_of_autocommitting(tmp_path):
    session, _ = make_training_session(tmp_path, size=1)
    reviews = []
    flow = TrainingFlowController(
        recorder=FakeTrainingRecorder(np.zeros(16000, np.float32)),
        session=session, qc_fn=lambda samples: make_qc_result(True),
        show_review=lambda text, samples, qc: reviews.append((text, qc)))
    flow.toggle()                                   # start
    flow.toggle()                                   # stop -> QC pass -> HOLD for review
    assert flow.state == "reviewing"
    assert len(reviews) == 1
    assert session.manifest.prompts[0].status == "pending"   # not yet committed
    assert session.manifest.prompts[0].attempts == 0          # record_result not called yet


def test_qc_fail_path_unchanged_when_show_review_is_wired(tmp_path):
    """QC-FAIL PATH UNCHANGED (ADJ-SPK-033): show_review only affects the
    QC-pass branch."""
    session, _ = make_training_session(tmp_path, size=2)
    qc_failures = []
    flow = TrainingFlowController(
        recorder=FakeTrainingRecorder(np.zeros(16000, np.float32)),
        session=session,
        show_qc_failure=lambda reason, attempts: qc_failures.append(attempts),
        qc_fn=lambda samples: make_qc_result(False),
        show_review=lambda text, samples, qc: (_ for _ in ()).throw(
            AssertionError("show_review must not fire on QC fail")))
    flow.toggle(); flow.toggle()
    assert qc_failures == [1]
    assert flow.state == "idle"                     # never entered "reviewing"
    assert session.manifest.prompts[0].status == "pending"
    assert session.manifest.prompts[0].attempts == 1


def test_accept_commits_the_held_take_and_advances(tmp_path):
    session, _ = make_training_session(tmp_path, size=2)
    prompts_shown = []
    flow = TrainingFlowController(
        recorder=FakeTrainingRecorder(np.zeros(16000, np.float32)),
        session=session, qc_fn=lambda samples: make_qc_result(True),
        show_prompt=lambda t: prompts_shown.append(t),
        show_review=lambda text, samples, qc: None)
    flow.toggle(); flow.toggle()                     # QC pass -> reviewing
    assert flow.state == "reviewing"

    flow.accept()
    assert flow.state == "idle"
    assert session.manifest.prompts[0].status == "accepted"
    assert session.manifest.prompts[0].attempts == 1
    assert prompts_shown[-1] != prompts_shown[0]     # advanced to prompt 2


def test_retake_discards_take_without_incrementing_attempts_and_rearms_same_prompt(tmp_path):
    session, _ = make_training_session(tmp_path, size=2)
    prompts_shown = []
    flow = TrainingFlowController(
        recorder=FakeTrainingRecorder(np.zeros(16000, np.float32)),
        session=session, qc_fn=lambda samples: make_qc_result(True),
        show_prompt=lambda t: prompts_shown.append(t),
        show_review=lambda text, samples, qc: None)
    flow.toggle(); flow.toggle()                     # QC pass -> reviewing
    first_prompt_text = prompts_shown[0]

    flow.retake()
    assert flow.state == "idle"
    assert session.manifest.prompts[0].status == "pending"   # discarded, not committed
    assert session.manifest.prompts[0].attempts == 0          # NOT a QC-failure attempt
    assert prompts_shown[-1] == first_prompt_text             # re-armed the SAME prompt

    # a fresh take on the re-armed prompt can still be accepted normally
    flow.toggle(); flow.toggle()
    flow.accept()
    assert session.manifest.prompts[0].status == "accepted"
    assert session.manifest.prompts[0].attempts == 1


def test_double_accept_commits_at_most_once_and_warns(tmp_path, caplog):
    session, _ = make_training_session(tmp_path, size=2)
    flow = TrainingFlowController(
        recorder=FakeTrainingRecorder(np.zeros(16000, np.float32)),
        session=session, qc_fn=lambda samples: make_qc_result(True),
        show_review=lambda text, samples, qc: None)
    flow.toggle(); flow.toggle()                     # QC pass -> reviewing

    flow.accept()
    attempts_after_first_accept = session.manifest.prompts[0].attempts
    with caplog.at_level(logging.WARNING):
        flow.accept()                                # second accept: must no-op
    assert session.manifest.prompts[0].attempts == attempts_after_first_accept  # exactly one commit
    assert any("accept()" in r.message for r in caplog.records)


def test_accept_then_retake_commits_at_most_once_and_warns(tmp_path, caplog):
    session, _ = make_training_session(tmp_path, size=2)
    flow = TrainingFlowController(
        recorder=FakeTrainingRecorder(np.zeros(16000, np.float32)),
        session=session, qc_fn=lambda samples: make_qc_result(True),
        show_review=lambda text, samples, qc: None)
    flow.toggle(); flow.toggle()                     # QC pass -> reviewing

    flow.accept()
    assert session.manifest.prompts[0].status == "accepted"
    with caplog.at_level(logging.WARNING):
        flow.retake()                                # must no-op, not discard the commit
    assert session.manifest.prompts[0].status == "accepted"     # still committed, unchanged
    assert any("retake()" in r.message for r in caplog.records)


def test_pending_review_samples_available_while_reviewing_then_cleared_by_accept(tmp_path):
    session, _ = make_training_session(tmp_path, size=1)
    samples = np.full(16000, 0.25, dtype=np.float32)
    flow = TrainingFlowController(
        recorder=FakeTrainingRecorder(samples),
        session=session, qc_fn=lambda s: make_qc_result(True),
        show_review=lambda text, samples, qc: None)
    assert flow.pending_review_samples is None       # nothing held yet
    flow.toggle(); flow.toggle()                      # QC pass -> reviewing
    assert flow.pending_review_samples is samples
    flow.accept()
    assert flow.pending_review_samples is None         # cleared, no post-accept replay handle


def test_pending_review_samples_cleared_by_retake(tmp_path):
    session, _ = make_training_session(tmp_path, size=1)
    samples = np.full(16000, 0.25, dtype=np.float32)
    flow = TrainingFlowController(
        recorder=FakeTrainingRecorder(samples),
        session=session, qc_fn=lambda s: make_qc_result(True),
        show_review=lambda text, samples, qc: None)
    flow.toggle(); flow.toggle()
    assert flow.pending_review_samples is samples
    flow.retake()
    assert flow.pending_review_samples is None


def test_toggle_is_noop_while_reviewing(tmp_path):
    """A stray hotkey tap while a take is awaiting accept()/retake() must not
    be treated as a second stop() -- it has nothing to stop."""
    session, _ = make_training_session(tmp_path, size=1)
    recorder = FakeTrainingRecorder(np.zeros(16000, np.float32))
    flow = TrainingFlowController(
        recorder=recorder, session=session, qc_fn=lambda samples: make_qc_result(True),
        show_review=lambda text, samples, qc: None)
    flow.toggle(); flow.toggle()                     # QC pass -> reviewing
    stopped_before = recorder.stopped

    flow.toggle()                                     # stray tap
    assert flow.state == "reviewing"
    assert recorder.stopped == stopped_before          # recorder.stop() not called again


# --- postprocessor wiring (TSK-SPK-TRAIN-T8) ---------------------------------

class StubPostprocessor:
    """Test double standing in for `speakeasy.engine.postprocess.Postprocessor`
    -- records what it was called with and applies a fixed text substitution."""
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def process(self, raw_text):
        self.calls.append(raw_text)
        return self.mapping.get(raw_text, raw_text)


def test_default_postprocess_constructs_real_postprocessor(tmp_path):
    from speakeasy.engine.postprocess import Postprocessor
    c, h, log = make(tmp_path)
    assert isinstance(c._postprocess, Postprocessor)


def test_postprocessor_corrects_raw_transcript_before_injection(tmp_path):
    stub = StubPostprocessor({"hello world": "hello universe"})
    c, h, log = make(tmp_path, postprocess=stub)
    c.toggle(); c.toggle()

    assert log == ["hello universe"]            # corrected text reaches injection
    assert stub.calls == ["hello world"]        # postprocessor saw the engine's raw output

    row = h.recent()[0]
    assert row["raw"] == "hello world"          # history raw column keeps engine raw output
    assert row["status"] == "injected"


def test_corrections_survive_a_live_engine_swap(tmp_path):
    """TSK-SPK-TRANSFER: profile corrections (the primary personalization
    lever per docs/superpowers/specs/2026-07-18-voice-training-environment.md
    section 1.2) must carry over when the active engine changes -- e.g. via
    the Models menu's live `controller.engine = new_engine` swap. Postprocessor
    is a Controller-level concern independent of self.engine by construction;
    this pins that property down as a regression-checked contract, not just
    an implicit consequence of how the two attributes happen to be wired."""
    stub = StubPostprocessor({"hello world": "hello universe"})
    c, h, log = make(tmp_path, postprocess=stub)
    c.toggle(); c.toggle()
    assert log == ["hello universe"]

    class OtherEngine:
        def transcribe(self, samples, sample_rate=16000):
            return "hello world"
    c.engine = OtherEngine()  # simulates a Models-menu model switch mid-session
    c.toggle(); c.toggle()
    assert log == ["hello universe", "hello universe"]


def test_postprocessor_corrects_text_before_rewrite_pipeline(tmp_path):
    stub = StubPostprocessor({"hello world": "hello universe"})
    seen_by_rewrite = []

    def rewrite_fn(raw, mode, pool):
        seen_by_rewrite.append(raw)
        return RewriteResult("CLEANED", "acct-a", 5)

    c, h, log = make(tmp_path, mode="clean", rewrite_fn=rewrite_fn, postprocess=stub)
    c.toggle(); c.toggle()

    assert seen_by_rewrite == ["hello universe"]  # rewrite pipeline sees corrected text
    assert log == ["CLEANED"]

    row = h.recent()[0]
    assert row["raw"] == "hello world"            # history raw column unaffected by corrections
    assert row["rewritten"] == "CLEANED"


def test_rewrite_failure_falls_back_to_corrected_text(tmp_path):
    stub = StubPostprocessor({"hello world": "hello universe"})

    def boom(*a, **k): raise RewriteError("all cooling")

    c, h, log = make(tmp_path, mode="clean", rewrite_fn=boom, postprocess=stub)
    c.toggle(); c.toggle()

    assert log == ["hello universe"]              # fallback injects corrected text, not raw
    row = h.recent()[0]
    assert row["status"] == "rewrite_failed"
    assert row["raw"] == "hello world"


def test_dictation_flow_untouched_when_postprocessor_is_identity(tmp_path):
    """No-op postprocessor (identity passthrough, as a no-active-profile
    Postprocessor behaves) must reproduce the pre-T8 dictation flow exactly."""
    stub = StubPostprocessor({})
    c, h, log = make(tmp_path, postprocess=stub)
    c.toggle(); assert c.state == "recording"
    c.toggle(); assert c.state == "idle"
    assert log == ["hello world"]
    row = h.recent()[0]
    assert row["status"] == "injected" and row["raw"] == "hello world"


# --- History submenu (TSK-SPK-UX-HISTORYMENU) -------------------------------

from speakeasy.app import history_menu_entries


class TestHistoryMenuEntries:
    def _row(self, **kw):
        base = dict(id=1, raw="hello world", rewritten=None, mode="raw",
                    status="injected")
        base.update(kw)
        return base

    def test_prefers_rewritten_text_for_copy(self):
        rows = [self._row(id=3, raw="raw text", rewritten="polished text", mode="clean")]
        (entry,) = history_menu_entries(rows)
        title, copy_text = entry
        assert copy_text == "polished text"
        assert "polished text" in title and "clean" in title

    def test_falls_back_to_raw(self):
        rows = [self._row(raw="just raw", rewritten=None)]
        ((title, copy_text),) = history_menu_entries(rows)
        assert copy_text == "just raw"

    def test_truncates_long_titles_but_copies_full_text(self):
        long = "x" * 300
        ((title, copy_text),) = history_menu_entries([self._row(raw=long)])
        assert len(title) <= 70
        assert copy_text == long

    def test_empty_rows_and_empty_text_skipped(self):
        rows = [self._row(raw="", rewritten=None), self._row(id=2, raw="keep me")]
        entries = history_menu_entries(rows)
        assert len(entries) == 1 and entries[0][1] == "keep me"


# --- translate mode (spec items 1-4, 6) --------------------------------------

class FakeTranslateResult:
    """Duck-typed stand-in for the engine-side `TranscriptionResult` (spec
    item 2) -- the real dataclass is owned by a parallel work order under
    speakeasy/engine/*, so this test suite never imports it."""
    def __init__(self, text, avg_logprob):
        self.text = text
        self.avg_logprob = avg_logprob


class FakeTranslateEngine:
    def __init__(self, text="hola mundo", avg_logprob=-0.1):
        self.text = text
        self.avg_logprob = avg_logprob
        self.calls = []

    def transcribe_ex(self, samples, sample_rate=16000):
        self.calls.append((len(samples), sample_rate))
        return FakeTranslateResult(self.text, self.avg_logprob)

    def transcribe(self, samples, sample_rate=16000):
        return self.text


class FakeTranslateEngineNoEx:
    """No transcribe_ex -- exercises the fallback-to-transcribe() path."""
    def __init__(self, text="hola mundo"):
        self.text = text

    def transcribe(self, samples, sample_rate=16000):
        return self.text


class FakeTranslateAuditResult:
    def __init__(self, text):
        self.text = text


def make_translate(tmp_path, engine=None, translate_audit_fn=None,
                    replace_last_fn=None, translate_audit_async=False,
                    translate_audit_enabled=True,
                    translate_audit_logprob_threshold=-0.55,
                    samples=None):
    samples = samples if samples is not None else np.zeros(16000, np.float32)
    modes = {"raw": Mode("raw", None), "translate": Mode("translate", None)}
    s = Settings(active_mode="translate",
                 translate_audit_enabled=translate_audit_enabled,
                 translate_audit_logprob_threshold=translate_audit_logprob_threshold)
    h = History(tmp_path / "h.db")
    inject_log = []
    engine = engine or FakeTranslateEngine()
    c = Controller(
        engine=engine, recorder=FakeRecorder(samples), history=h,
        modes=modes, pool="POOL", settings=s,
        inject_fn=lambda text: inject_log.append(text) or "injected",
        translate_audit_fn=translate_audit_fn or (lambda raw, pool: FakeTranslateAuditResult(raw)),
        replace_last_fn=replace_last_fn or (lambda *a, **k: "replaced"),
        translate_audit_async=translate_audit_async,
        run_async=False, frontmost=lambda: "TextEdit")
    return c, h, inject_log, engine


def test_translate_gate_truth_table(tmp_path):
    c, h, log, engine = make_translate(tmp_path)
    long_text = "a long enough sentence here"
    assert c._translate_audit_needed(None, long_text) is True          # no signal -> audit
    assert c._translate_audit_needed(-0.9, long_text) is True          # low confidence -> audit
    assert c._translate_audit_needed(-0.1, long_text) is False         # confident + long -> no audit
    assert c._translate_audit_needed(-0.1, "hi there") is True         # confident but short -> audit
    c.settings.translate_audit_enabled = False
    assert c._translate_audit_needed(None, "short") is False           # disabled -> never audit
    assert c._translate_audit_needed(-0.9, "short") is False


def test_translate_uses_transcribe_ex_when_available(tmp_path):
    engine = FakeTranslateEngine(text="hello", avg_logprob=-0.05)
    samples = np.zeros(16000, np.float32)
    c, h, log, engine = make_translate(tmp_path, engine=engine, samples=samples)
    c.toggle(); c.toggle()
    assert engine.calls == [(len(samples), 16000)]


def test_translate_falls_back_to_transcribe_when_no_transcribe_ex(tmp_path):
    audit_calls = []
    engine = FakeTranslateEngineNoEx(text="hola mundo esto es una prueba larga")
    c, h, log, engine = make_translate(
        tmp_path, engine=engine,
        translate_audit_fn=lambda raw, pool: audit_calls.append(raw) or FakeTranslateAuditResult(raw))
    c.toggle(); c.toggle()
    assert log == ["hola mundo esto es una prueba larga"]
    assert audit_calls == ["hola mundo esto es una prueba larga"]  # avg_logprob None -> gate fires


def test_translate_empty_transcript_discarded(tmp_path):
    engine = FakeTranslateEngine(text="   ", avg_logprob=-0.1)
    c, h, log, engine = make_translate(tmp_path, engine=engine)
    c.toggle(); c.toggle()
    assert log == []
    assert h.recent()[0]["status"] == "discarded"


def test_translate_injects_raw_immediately_no_audit_when_confident(tmp_path):
    audit_calls = []
    engine = FakeTranslateEngine(text="hello world this is fine", avg_logprob=-0.1)
    c, h, log, engine = make_translate(
        tmp_path, engine=engine,
        translate_audit_fn=lambda raw, pool: audit_calls.append(raw) or FakeTranslateAuditResult(raw))
    c.toggle(); c.toggle()
    assert log == ["hello world this is fine"]                # raw injected immediately
    row = h.recent()[0]
    assert row["status"] == "translate_raw"
    assert row["raw"] == "hello world this is fine"
    assert audit_calls == []                                   # gate did not fire


def test_translate_audit_corrects_in_place_when_gate_fires(tmp_path):
    engine = FakeTranslateEngine(text="i go store yesterday", avg_logprob=-0.9)
    replace_calls = []
    def fake_replace(old, new, expected_app, frontmost=None):
        replace_calls.append((old, new, expected_app))
        return "replaced"
    c, h, log, engine = make_translate(
        tmp_path, engine=engine,
        translate_audit_fn=lambda raw, pool: FakeTranslateAuditResult("I went to the store yesterday."),
        replace_last_fn=fake_replace)
    c.toggle(); c.toggle()
    assert log == ["i go store yesterday"]                     # raw still injected first
    assert replace_calls == [("i go store yesterday",
                              "I went to the store yesterday.", "TextEdit")]
    row = h.recent()[0]
    assert row["status"] == "translate_corrected"
    assert row["rewritten"] == "I went to the store yesterday."


def test_translate_audit_failure_marks_status(tmp_path):
    from speakeasy.translate_audit import TranslateAuditError
    engine = FakeTranslateEngine(text="i go store yesterday", avg_logprob=-0.9)
    def boom(raw, pool):
        raise TranslateAuditError("exhausted")
    c, h, log, engine = make_translate(tmp_path, engine=engine, translate_audit_fn=boom)
    c.toggle(); c.toggle()
    assert log == ["i go store yesterday"]
    row = h.recent()[0]
    assert row["status"] == "translate_audit_failed"
    assert row["rewritten"] is None


def test_translate_audit_skipped_when_focus_changed(tmp_path):
    engine = FakeTranslateEngine(text="i go store yesterday", avg_logprob=-0.9)
    c, h, log, engine = make_translate(
        tmp_path, engine=engine,
        translate_audit_fn=lambda raw, pool: FakeTranslateAuditResult("I went to the store yesterday."),
        replace_last_fn=lambda *a, **k: "skipped")
    c.toggle(); c.toggle()
    row = h.recent()[0]
    assert row["status"] == "translate_correction_skipped"
    assert row["rewritten"] is None


def test_translate_audit_noop_when_already_natural(tmp_path):
    engine = FakeTranslateEngine(text="hi there", avg_logprob=-0.1)  # short -> gate fires
    replace_calls = []
    c, h, log, engine = make_translate(
        tmp_path, engine=engine,
        translate_audit_fn=lambda raw, pool: FakeTranslateAuditResult(raw),  # unchanged
        replace_last_fn=lambda *a, **k: replace_calls.append(1) or "replaced")
    c.toggle(); c.toggle()
    assert replace_calls == []
    row = h.recent()[0]
    assert row["status"] == "translate_raw"                     # not overwritten -- no real diff


def test_translate_audit_disabled_never_runs(tmp_path):
    called = []
    engine = FakeTranslateEngine(text="hi", avg_logprob=None)   # would trigger if enabled
    c, h, log, engine = make_translate(
        tmp_path, engine=engine, translate_audit_enabled=False,
        translate_audit_fn=lambda raw, pool: called.append(1) or FakeTranslateAuditResult(raw))
    c.toggle(); c.toggle()
    assert called == []
    row = h.recent()[0]
    assert row["status"] == "translate_raw"


def test_translate_audit_renders_polishing_state(tmp_path):
    engine = FakeTranslateEngine(text="i go store yesterday", avg_logprob=-0.9)
    c, h, log, engine = make_translate(
        tmp_path, engine=engine,
        translate_audit_fn=lambda raw, pool: FakeTranslateAuditResult("I went to the store yesterday."))
    rendered = []
    c._render = lambda m: rendered.append(m.state)
    c.toggle(); c.toggle()
    assert "polishing" in rendered
