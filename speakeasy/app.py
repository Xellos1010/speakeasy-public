import logging
import threading
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)
from speakeasy import injector, rewrite as rewrite_mod
from speakeasy import translate_audit as translate_audit_mod
from speakeasy.config import Settings
from speakeasy.engine.postprocess import Postprocessor
from speakeasy.modes import Mode
from speakeasy.overlay import OverlayModel
from speakeasy.rewrite import RewriteError
from speakeasy.training.qc import (
    MIN_RMS_DBFS,
    MIN_SNR_DB,
    MIN_VOICED_RATIO,
    QCResult,
    qc_clip,
)
from speakeasy.training.session import TrainingSession

# Defensive/lazy: the engine-side `transcribe_ex`/`TranscriptionResult`
# consumption contract (spec item 2) is being implemented in parallel under
# speakeasy/engine/*, which this work order does not own. This import must
# never break `speakeasy.app` regardless of merge order between the two
# branches -- `_process_translate` below duck-types the result (`.text` /
# `.avg_logprob`) rather than depending on this import at runtime.
try:
    from speakeasy.engine import TranscriptionResult  # noqa: F401
except Exception:
    TranscriptionResult = None

class Controller:
    def __init__(self, engine, recorder, history, modes: dict[str, Mode],
                 pool, settings: Settings,
                 overlay_render: Callable[[OverlayModel], None] = lambda m: None,
                 inject_fn=injector.inject,
                 rewrite_fn=rewrite_mod.rewrite,
                 translate_audit_fn=translate_audit_mod.audit_translation,
                 replace_last_fn=injector.replace_last,
                 translate_engine=None,
                 frontmost: Callable[[], str | None] = lambda: None,
                 min_samples: int = 6400,
                 run_async: bool = True,
                 translate_audit_async: bool = True,
                 partial_interval: float = 4.0,
                 level_interval: float = 0.12,
                 postprocess: Optional[Postprocessor] = None,
                 audio_dir: Optional[Path] = None,
                 clipboard_fn: Optional[Callable[[str], None]] = None):
        self._audio_dir = audio_dir
        self._clipboard = clipboard_fn
        self.engine, self.recorder, self.history = engine, recorder, history
        self.modes, self.pool, self.settings = modes, pool, settings
        self._render = overlay_render
        self._inject, self._rewrite, self._frontmost = inject_fn, rewrite_fn, frontmost
        # Translate-mode plumbing (spec items 1-4). `translate_engine` is an
        # optional dedicated "whisper-translate" engine instance; None falls
        # back to `self.engine` so this stays usable before the wider
        # __main__.py wiring (out of this work order's file scope) picks a
        # distinct engine per settings.engine/mode. `translate_audit_async`
        # exists purely for deterministic tests -- production always runs the
        # audit in a background thread so it never blocks injection.
        self._translate_audit = translate_audit_fn
        self._replace_last = replace_last_fn
        self._translate_engine = translate_engine
        self._translate_audit_async = translate_audit_async
        self._min = min_samples
        self._async = run_async
        self._partial_interval = partial_interval
        self._level_interval = level_interval
        self._last_partial = ""
        self._level_timer = None
        # Populated by _process()/_process_translate() right before the
        # `finally: self._finish()` fires, so _finish() can show a
        # post-dictation scratchpad (TSK-SPK-UX-SCRATCHPAD) instead of just
        # hiding: automatic ⌘V injection has no way to confirm a text field
        # was actually focused, and injector.inject() always restores the
        # prior clipboard 0.6s later either way -- without this, a failed
        # auto-insert left the transcribed text completely unrecoverable.
        self._last_result_text = ""
        self._last_result_status = ""
        # Correction-rule post-processing pass (spec §5 note, §9 T8): applied
        # to the engine's raw output ahead of mode/rewrite dispatch. Default
        # constructs the real Postprocessor (active profile under
        # ~/.speakeasy/profile, per its own default) -- constructor injection
        # exists purely for testability, mirroring `speakeasy_dir()`'s
        # existing eager-default-path convention (speakeasy/config.py).
        self._postprocess = postprocess if postprocess is not None else Postprocessor()
        self._timer = None
        self.state = "idle"
        self._hold_active = False
        # Explicit mode flag (spec T4): when a training session is active,
        # hotkey toggles route to the training flow instead of dictation
        # recording/injection. `None` (the default) means dictation-as-today;
        # any assigned `TrainingFlowController` instance means training-active.
        self.training: Optional["TrainingFlowController"] = None

    def toggle(self) -> None:
        if self.state == "idle":
            self.state = "recording"
            self.recorder.start()
            self._render(OverlayModel("recording",
                                      mode_name=self.settings.active_mode))
            if self._async:
                self._schedule_partial()
                self._schedule_level()
        elif self.state == "recording":
            self.state = "processing"
            if self._timer:
                self._timer.cancel()
            if self._level_timer:
                self._level_timer.cancel()
            self._render(OverlayModel("transcribing"))
            if self._async:
                threading.Thread(target=self._process, daemon=True).start()
            else:
                self._process()
        # processing: ignore

    def on_tap_toggle(self) -> None:
        """Right-Command tap (or legacy F5 keyDown): toggle record/stop.

        When a training session is active (`self.training` is not None),
        the tap routes to the training flow's own record/stop-and-QC toggle
        instead of dictation recording+injection (spec T4). Dictation
        behavior is otherwise untouched.
        """
        if self.training is not None:
            self.training.toggle()
            return
        self.toggle()

    def on_hold_start(self) -> None:
        """Right-Command hold crossed the tap/hold threshold: push-to-talk start.

        No-op while a training session is active -- training uses tap-toggle
        only (see `on_tap_toggle`); dictation push-to-talk must not fire
        underneath a live training recording.
        """
        if self.training is not None:
            return
        if self.state == "idle":
            self._hold_active = True
            self.toggle()

    def on_hold_end(self) -> None:
        """Right-Command released after a hold-start: push-to-talk stop.

        Only stops recording that this hold started — a hold release after
        the recording was already stopped by a separate tap, or before a
        hold-start ever fired, is a no-op. Also a no-op while a training
        session is active (see `on_hold_start`).
        """
        if self.training is not None:
            return
        if self._hold_active and self.state == "recording":
            self._hold_active = False
            self.toggle()

    def _tick_partial(self) -> None:
        if self.state != "recording":
            return
        text = self.engine.transcribe(self.recorder.snapshot())
        self._last_partial = text
        self._render(OverlayModel("recording", partial=text,
                                  mode_name=self.settings.active_mode))
        if self._async:
            self._schedule_partial()

    def _schedule_partial(self) -> None:
        import threading as _t
        self._timer = _t.Timer(self._partial_interval, self._tick_partial)
        self._timer.daemon = True
        self._timer.start()

    def _tick_level(self) -> None:
        # Live audio-level meter for the overlay (TSK-SPK-UX-METER): runs on
        # a much shorter cadence than `_tick_partial` (which re-transcribes,
        # too expensive to run at meter refresh rate) so the bar animation
        # tracks real RMS smoothly instead of jumping every partial_interval.
        if self.state != "recording":
            return
        self._render(OverlayModel("recording", partial=self._last_partial,
                                  mode_name=self.settings.active_mode,
                                  level=self.recorder.level))
        if self._async:
            self._schedule_level()

    def _schedule_level(self) -> None:
        import threading as _t
        self._level_timer = _t.Timer(self._level_interval, self._tick_level)
        self._level_timer.daemon = True
        self._level_timer.start()

    def _finish(self):
        text = self._last_result_text
        if text or self._last_result_status:
            self._render(OverlayModel("done", partial=text,
                                      message=self._last_result_status))
            self._schedule_result_hide()
        else:
            self._render(OverlayModel("hidden"))
        self.state = "idle"

    def _schedule_result_hide(self, duration: float = 6.0) -> None:
        import threading as _t

        def _hide():
            # Guard against clobbering a NEW recording's overlay: if the
            # user already started dictating again within `duration`,
            # state is back to "recording" and this stale timer must no-op.
            if self.state == "idle":
                self._render(OverlayModel("hidden"))

        timer = _t.Timer(duration, _hide)
        timer.daemon = True
        timer.start()

    def _save_audio(self, rid: int, samples) -> Optional[str]:
        """P1-1: persist the utterance (0600, dir 0700) BEFORE transcription so an
        engine failure never loses the dictation. Best-effort: a write failure is
        logged and dictation continues."""
        if self._audio_dir is None:
            return None
        try:
            import os, wave
            import numpy as np
            d = Path(self._audio_dir)
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)
            path = d / f"dictation-{rid}.wav"
            pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
                   * 32767.0).astype("<i2").tobytes()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f, wave.open(f, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(pcm)
            return str(path)
        except Exception:
            logger.exception("could not save dictation audio for row %s", rid)
            return None

    def _drop_audio(self, rid: int, audio_path: Optional[str]) -> None:
        if not audio_path:
            return
        try:
            Path(audio_path).unlink(missing_ok=True)
            self.history.clear_audio(rid)
        except Exception:
            logger.exception("could not remove dictation audio %s", audio_path)

    def _process(self) -> None:
        self._last_result_text = ""
        self._last_result_status = ""
        try:
            samples = self.recorder.stop()
            if len(samples) < self._min:
                return
            mode = self.modes.get(self.settings.active_mode, self.modes["raw"])
            target_app = self._frontmost()
            # P1-1: the history row and the audio exist BEFORE transcription, so no
            # failure past this point can lose the dictation or leave no trace.
            rid = self.history.record("", mode.name, target_app, status="transcribing")
            audio_path = self._save_audio(rid, samples)
            if audio_path:
                self.history.update(rid, status="transcribing", audio_path=audio_path)
            self._run_pipeline(rid, mode, samples, target_app, audio_path)
        except Exception:
            logger.exception("dictation processing failed before a history row existed")
        finally:
            self._finish()

    def _run_pipeline(self, rid: int, mode: Mode, samples, target_app,
                      audio_path: Optional[str], deliver=None) -> None:
        """Transcribe -> postprocess -> (rewrite) -> deliver, for an existing history
        row. Every failure is recorded on the row with a reason and surfaced on the
        overlay; the row is never left `transcribing`/`pending`."""
        deliver = deliver or self._inject
        # translate is its own dedicated path -- it never enters the
        # generic system_prompt-based rewrite/raw dispatch below, even
        # though its system_prompt is also None (spec item 1).
        if mode.name == "translate":
            try:
                self._process_translate(mode, samples, rid=rid, target_app=target_app,
                                        deliver=deliver)
            except Exception as exc:
                self._fail(rid, "engine_failed", exc)
                return
            self._drop_audio(rid, audio_path)
            return
        try:
            raw = self.engine.transcribe(samples)
        except Exception as exc:
            self._fail(rid, "engine_failed", exc)  # audio kept for retry
            return
        if not raw.strip():
            self.history.update(rid, status="discarded", raw="")
            self._drop_audio(rid, audio_path)
            return
        self.history.update(rid, status="pending", raw=raw)
        self._drop_audio(rid, audio_path)
        # Corrections apply to what proceeds to injection/rewrite, not to
        # the history's `raw` column, which keeps the engine's raw output
        # (spec §5 note, §9 T8).
        try:
            corrected = self._postprocess.process(raw)
        except Exception:
            logger.exception("postprocess failed; using the engine's raw text")
            corrected = raw
        if mode.system_prompt is None:
            self._deliver(rid, corrected, deliver)
            return
        self._render(OverlayModel("rewriting", mode_name=mode.name))
        try:
            res = self._rewrite(corrected, mode, self.pool)
            text = (res.text or "").strip()
            if not text:
                raise RewriteError("empty rewrite")
        except Exception as exc:
            # P0-1: ANY rewrite failure (missing CLI, timeout, exit, exhaustion,
            # unexpected error) falls back to the raw transcript -- never lose it.
            logger.warning("rewrite failed (%s): %s; inserting raw text",
                           type(exc).__name__, exc)
            self._deliver(rid, corrected, deliver, fail_status="rewrite_failed",
                          error=f"{type(exc).__name__}: {exc}"[:500])
            return
        self._deliver(rid, text, deliver, rewritten=text, account=res.account,
                      latency_ms=res.latency_ms)

    def _deliver(self, rid: int, text: str, deliver, fail_status: Optional[str] = None,
                 error: Optional[str] = None, **fields) -> None:
        try:
            status = deliver(text)
        except Exception as exc:
            logger.exception("injection failed")
            self.history.update(rid, status="inject_failed",
                                error=f"{type(exc).__name__}: {exc}"[:500], **fields)
            self._last_result_text, self._last_result_status = text, "inject_failed"
            return
        recorded = fail_status or status or "injected"
        self.history.update(rid, status=recorded, error=error, **fields)
        self._last_result_text = text
        self._last_result_status = recorded

    def _fail(self, rid: int, status: str, exc: BaseException) -> None:
        logger.error("dictation %s: %s: %s", rid, status, exc, exc_info=exc)
        self.history.update(rid, status=status, error=f"{type(exc).__name__}: {exc}"[:500])
        self._last_result_text = ""
        self._last_result_status = status

    def retry(self, row_id: Optional[int] = None) -> Optional[str]:
        """P1-1 retry: re-run a failed dictation from its saved audio. The result is
        put on the clipboard (never pasted into whatever happens to be focused now).
        Returns the final status, or None if there is nothing to retry."""
        row = self.history.get(row_id) if row_id is not None else self.history.latest_retryable()
        if not row or not row.get("audio_path") or not Path(row["audio_path"]).exists():
            return None
        from speakeasy.canary import _load_wav
        samples = _load_wav(Path(row["audio_path"]))
        mode = self.modes.get(row["mode"], self.modes["raw"])
        self._last_result_text = ""
        self._last_result_status = ""
        self.state = "processing"
        self._render(OverlayModel("transcribing"))
        try:
            self._run_pipeline(row["id"], mode, samples, row.get("target_app"),
                               row["audio_path"], deliver=self._clipboard_deliver)
        finally:
            self._finish()
        return self.history.get(row["id"])["status"]

    def _clipboard_deliver(self, text: str) -> str:
        if self._clipboard is None:
            return self._inject(text)
        self._clipboard(text)
        return "clipboard_only"

    def _process_translate(self, mode: Mode, samples, rid: Optional[int] = None,
                           target_app=None, deliver=None) -> None:
        """Translate-mode dictation (spec items 2-4): transcribe via the
        `transcribe_ex` engine-consumption contract when available (falling
        back to plain `transcribe()` with avg_logprob=None), record + inject
        the raw transcript IMMEDIATELY -- injection is never blocked on the
        localization audit -- then, only if the confidence gate says the
        translation looks suspect, kick off the Haiku audit in the
        background to correct it in place."""
        engine = self._translate_engine if self._translate_engine is not None else self.engine
        fn = getattr(engine, "transcribe_ex", None)
        if fn is not None:
            result = fn(samples, sample_rate=16000)
            # Duck-typed on purpose (see the defensive TranscriptionResult
            # import at module scope): works whether or not the parallel
            # engine branch defining the real dataclass has landed yet.
            raw = getattr(result, "text", None)
            avg_logprob = getattr(result, "avg_logprob", None)
            if raw is None:
                raw, avg_logprob = str(result), None
        else:
            raw, avg_logprob = engine.transcribe(samples), None

        if not raw.strip():
            if rid is None:
                rid = self.history.record("", mode.name, self._frontmost())
            self.history.update(rid, status="discarded")
            return

        if rid is None:
            target_app = self._frontmost()
            rid = self.history.record(raw, mode.name, target_app, status="translate_raw")
        else:
            self.history.update(rid, status="translate_raw", raw=raw)
        (deliver or self._inject)(raw)
        self._last_result_text, self._last_result_status = raw, "translated"

        if self._translate_audit_needed(avg_logprob, raw):
            args = (rid, raw, target_app)
            if self._translate_audit_async:
                threading.Thread(target=self._run_translate_audit,
                                 args=args, daemon=True).start()
            else:
                self._run_translate_audit(*args)

    def _translate_audit_needed(self, avg_logprob: float | None, text: str) -> bool:
        """Gate policy (spec item 3): audit iff enabled AND (no confidence
        signal at all, OR confidence is below threshold, OR the transcript
        is suspiciously short)."""
        if not self.settings.translate_audit_enabled:
            return False
        if avg_logprob is None:
            return True
        if avg_logprob < self.settings.translate_audit_logprob_threshold:
            return True
        if len(text.split()) < 3:
            return True
        return False

    def _run_translate_audit(self, rid: int, raw: str, expected_app: str | None) -> None:
        self._render(OverlayModel("polishing", mode_name="translate"))
        try:
            try:
                result = self._translate_audit(raw, self.pool)
            except translate_audit_mod.TranslateAuditError:
                self.history.update(rid, status="translate_audit_failed")
                return
            corrected = result.text
            if corrected == raw:
                return  # already natural -- no real diff, leave translate_raw as-is
            status = self._replace_last(raw, corrected, expected_app,
                                        frontmost=self._frontmost)
            if status == "skipped":
                self.history.update(rid, status="translate_correction_skipped")
            else:
                self.history.update(rid, status="translate_corrected",
                                    rewritten=corrected)
        finally:
            self._render(OverlayModel("hidden"))


def _qc_fail_summary(qc_result: QCResult) -> str:
    """Human-readable reason string for a failed QC gate (spec §3 step 4),
    built from `speakeasy.training.qc`'s own thresholds so this never drifts
    out of sync with the actual gate."""
    reasons = []
    if qc_result.clipping:
        reasons.append("clipping")
    if qc_result.silence_ratio < MIN_VOICED_RATIO:
        reasons.append(f"too much silence ({qc_result.silence_ratio:.0%} voiced)")
    if qc_result.rms_dbfs < MIN_RMS_DBFS:
        reasons.append(f"too quiet ({qc_result.rms_dbfs:.1f} dBFS)")
    if qc_result.snr_db < MIN_SNR_DB:
        reasons.append(f"too noisy ({qc_result.snr_db:.1f} dB SNR)")
    return "; ".join(reasons) or "quality check failed"


def training_resume_available(sessions_dir) -> bool:
    """True iff an `in_progress` training session manifest exists under
    `sessions_dir` -- backs the "Resume last session" menu item's enabled
    state (spec T4). Pure passthrough to `TrainingSession.resume` so the
    menu wiring and this check can never disagree about what "resumable"
    means."""
    return TrainingSession.resume(sessions_dir) is not None


class TrainingFlowController:
    """Drives the voice-training UX flow (spec §3 steps 1-7) on top of
    `TrainingSession` (manifest/resumability/split), `qc_clip` (per-clip QC
    gate) and the existing `Recorder`.

    All UI interaction (showing prompt text, a QC failure, or the end-of-
    session summary) happens through injected callbacks -- this class makes
    no direct rumps/AppKit calls, matching `Controller`'s own
    testability idiom. `speakeasy/__main__.py` wires the callbacks to the
    overlay/rumps UI; `Controller.on_tap_toggle` routes hotkey taps here
    instead of dictation whenever `Controller.training` is set to an
    instance of this class (see spec T4 file-scope note).
    """

    def __init__(self, recorder, session: TrainingSession,
                 show_prompt: Callable[[str], None] = lambda text: None,
                 show_qc_failure: Callable[[str, int], None] = lambda reason, attempts: None,
                 show_summary: Callable[[dict], None] = lambda summary: None,
                 qc_fn=qc_clip,
                 show_review: Optional[Callable[[str, object, QCResult], None]] = None):
        self.recorder = recorder
        self.session = session
        self._show_prompt = show_prompt
        self._show_qc_failure = show_qc_failure
        self._show_summary = show_summary
        self._qc_fn = qc_fn
        # ADJ-SPK-033: absent (None, the default) means the human-review hook
        # isn't wired at all -- toggle() auto-commits a QC pass exactly as it
        # always has, byte-identical to the pre-wizard behavior (C1). Only a
        # real callable here switches a QC pass into the reviewing state
        # below; qc_fn itself and the QC-fail path are untouched either way.
        self._show_review = show_review
        self.state = "idle"  # idle | recording | reviewing
        self.done = False
        # Set only while state == "reviewing": (prompt_id, samples, qc_result)
        # for the held, not-yet-committed take. accept()/retake() both clear
        # this to None as the FIRST thing they do on a real call, so a second
        # call to either (in any combination) finds it already None and no-ops
        # with a warning -- accept() commits at most once per take (C2-R).
        self._pending_review: Optional[tuple] = None
        # Prompts flagged low_quality (max attempts exhausted) during THIS
        # live flow are skipped for the remainder of this run without
        # mutating their manifest status further -- a *separate* future
        # TrainingFlowController instance (e.g. after quitting and resuming
        # via "Resume last session") starts with an empty skip set and will
        # offer them again, matching TrainingSession.current_prompt's own
        # resumability contract (spec §3.6).
        self._skipped_this_run: set[str] = set()
        self._active_prompt_id: Optional[str] = None
        self._announce_next()

    # -- prompt sequencing (session.manifest is the source of truth; this
    #    class only adds the "skip a just-exhausted prompt for this run"
    #    filter on top of it) ---------------------------------------------

    def _current_prompt(self):
        for p in self.session.manifest.prompts:
            if p.status == "accepted":
                continue
            if p.prompt_id in self._skipped_this_run:
                continue
            return p
        return None

    def _find_prompt(self, prompt_id: str):
        for p in self.session.manifest.prompts:
            if p.prompt_id == prompt_id:
                return p
        return None

    def _announce_next(self) -> None:
        prompt = self._current_prompt()
        if prompt is None:
            self._complete()
        else:
            self._show_prompt(prompt.text)

    def _complete(self) -> None:
        self.done = True
        summary = self.session.finish()
        self._show_summary(summary)

    # -- recording (spec §3 steps 3-4) ------------------------------------

    def toggle(self) -> None:
        """Tap-toggle for the current prompt: idle -> start recording;
        recording -> stop, run QC, and advance (re-prompt on QC failure up
        to 3 attempts, then skip; advance to the next prompt on success;
        show the session summary once every prompt is accepted or
        skipped)."""
        if self.done:
            return
        if self.state == "idle":
            prompt = self._current_prompt()
            if prompt is None:
                self._complete()
                return
            self._active_prompt_id = prompt.prompt_id
            self.state = "recording"
            self.recorder.start()
            return

        if self.state == "reviewing":
            # A held take is awaiting accept()/retake() -- those are the only
            # exit paths from this state; a stray toggle() (e.g. an errant
            # hotkey tap) is a no-op, not a second stop()/start().
            return

        # state == "recording"
        self.state = "idle"
        samples = self.recorder.stop()
        prompt_id = self._active_prompt_id
        qc_result = self._qc_fn(samples)
        prompt = self._find_prompt(prompt_id)

        if not qc_result.passed:
            # QC-fail path is unchanged regardless of show_review (ADJ-SPK-033):
            # record_result still commits the failed attempt immediately, and
            # the 3-attempt budget is still applied session-side.
            status = self.session.record_result(prompt_id, samples, qc_result)
            if status == "low_quality":
                self._skipped_this_run.add(prompt_id)
                self._show_qc_failure(_qc_fail_summary(qc_result), prompt.attempts)
                self._announce_next()
            else:  # "pending" -- attempts remain: re-show same prompt
                self._show_qc_failure(_qc_fail_summary(qc_result), prompt.attempts)
                self._show_prompt(prompt.text)
            return

        # QC passed.
        if self._show_review is None:
            # Legacy behavior, byte-identical to pre-wizard: auto-commit.
            self.session.record_result(prompt_id, samples, qc_result)
            self._announce_next()
            return

        # Wizard behavior: hold for human accept()/retake() instead of
        # auto-committing. A QC-passing take the human rejects does not
        # increment attempts -- the 3-attempt budget is the QC-failure
        # budget (spec 3.4), not a human-taste budget -- so nothing is
        # written to the manifest until accept() actually commits it.
        self._pending_review = (prompt_id, samples, qc_result)
        self.state = "reviewing"
        self._show_review(prompt.text, samples, qc_result)

    @property
    def pending_review_samples(self):
        """The held take's samples while state == "reviewing", for playback
        of the in-review take -- None once accept()/retake() has cleared
        _pending_review (or if nothing is being reviewed). Read-only: this
        property has no effect on the C2-R commit-at-most-once lifecycle,
        it only exposes what's already there for as long as it's there."""
        return self._pending_review[1] if self._pending_review is not None else None

    def accept(self) -> None:
        """Commit the held, QC-passing take via the existing record_result
        path and advance. At most one commit per take (C2-R): a second call
        (or a retake() call after this one) finds _pending_review already
        cleared and is a no-op with a logged warning."""
        if self._pending_review is None:
            logger.warning("TrainingFlowController.accept() called with no pending review -- ignored")
            return
        prompt_id, samples, qc_result = self._pending_review
        self._pending_review = None
        self.state = "idle"
        self.session.record_result(prompt_id, samples, qc_result)
        self._announce_next()

    def retake(self) -> None:
        """Discard the held take (no manifest write, no attempts increment)
        and re-arm the same prompt for a fresh recording. At most one retake
        (or accept()) is honored per take (C2-R): a second call finds
        _pending_review already cleared and is a no-op with a logged
        warning."""
        if self._pending_review is None:
            logger.warning("TrainingFlowController.retake() called with no pending review -- ignored")
            return
        prompt_id, _samples, _qc_result = self._pending_review
        self._pending_review = None
        self.state = "idle"
        prompt = self._find_prompt(prompt_id)
        self._show_prompt(prompt.text)


def history_menu_entries(rows: list[dict], title_limit: int = 48) -> list[tuple[str, str]]:
    """(menu title, text to copy) pairs for the History submenu
    (TSK-SPK-UX-HISTORYMENU). Copy text prefers the rewritten output the
    user actually received; titles are truncated, copies never are."""
    entries = []
    for row in rows:
        text = (row.get("rewritten") or row.get("raw") or "").strip()
        if not text:
            continue
        snippet = text if len(text) <= title_limit else text[: title_limit - 1] + "…"
        entries.append((f"{snippet}  ({row.get('mode', 'raw')})", text))
    return entries
