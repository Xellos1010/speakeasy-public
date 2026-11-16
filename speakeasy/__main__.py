import os
import sys
import threading
from pathlib import Path
import rumps
from speakeasy.deploy_profile import accounts_path as _deploy_accounts_path
from speakeasy import canary, feedback, injector, permissions
from speakeasy.accounts import AccountPool
from speakeasy.app import Controller, TrainingFlowController, training_resume_available
from speakeasy.audio import Recorder
from speakeasy.config import (load_host_engine_settings, load_settings, save_settings,
                              secure_tree, speakeasy_dir)
from speakeasy.engine import create_engine
from speakeasy.engine.remote import RemoteFallbackEngine
from speakeasy.gui.handlers.mics import mics_list_devices
from speakeasy.gui.input_device import resolve_input_device
from speakeasy.history import History
from speakeasy.hotkey import HotkeyListener
from speakeasy.modes import load_modes
from speakeasy.overlay import OverlayModel, OverlayPanel, run_on_main_thread
from speakeasy.ranking import (
    ReachabilityCache,
    PeerIdentityCache,
    TailnetProber,
    default_tailscale_peers,
    load_ranking,
    make_http_health_check,
    SELF,
)
from speakeasy.sysinfo import ENGINE_INFO, detect_specs, recommend_engine
from speakeasy.training.qc import SAMPLE_RATE
from speakeasy.training.session import FULL_SIZE, MAX_ATTEMPTS, QUICK_SIZE, TrainingSession

class _LazyTranslateEngine:
    """Defers building the whisper-translate engine (2nd model, ~1GB RAM)
    until the first translate-mode dictation, so non-translate users pay
    nothing. Model files are cached under ~/.speakeasy/models, so after the
    one-time download the first build is load-time only."""

    def __init__(self, factory):
        self._factory = factory
        self._engine = None

    def _get(self):
        if self._engine is None:
            self._engine = self._factory()
        return self._engine

    def transcribe(self, samples, sample_rate=16000):
        return self._get().transcribe(samples, sample_rate)

    def transcribe_ex(self, samples, sample_rate=16000):
        return self._get().transcribe_ex(samples, sample_rate)


def _configured_rewrite(settings):
    """Rewrite bound to the operator's settings: claude CLI path override and the
    total time budget before the raw transcript is inserted (P0-1/P1-4)."""
    from speakeasy import rewrite as rewrite_mod

    def _rw(text, mode, pool):
        return rewrite_mod.rewrite(text, mode, pool, timeout=settings.rewrite_timeout_s,
                                   claude_bin=settings.claude_bin or None)
    return _rw


def _configured_translate_audit(settings):
    from speakeasy import translate_audit as ta

    def _audit(raw, pool):
        return ta.audit_translation(raw, pool, claude_bin=settings.claude_bin or None)
    return _audit


def run() -> None:
    from speakeasy.logging_setup import configure_logging
    configure_logging()
    # P2-3: everything this app creates is owner-only (transcripts, voice clips,
    # profiles, settings); existing files from older builds are migrated here.
    os.umask(0o077)
    base = speakeasy_dir()
    secure_tree(base)
    settings = load_settings()
    modes = load_modes(base / "modes.yaml")
    local_engine = create_engine(settings.engine, base / "models")
    engine = local_engine
    ranking = load_ranking(settings.inference_ranking)
    remote_hosts = [h for h in ranking if h != SELF]
    if remote_hosts:
        local_settings_record = load_host_engine_settings()
        if not local_settings_record.get("measured_p95_ms"):
            # R2.3-flavored safe default: a ranking naming a remote host but no
            # local benchmark record yet can't compute R5.2's deadline -- fall
            # back to plain local-only rather than crash or guess a deadline.
            print("speakeasy: inference_ranking configured but this host has no "
                  "benchmark record (~/.speakeasy/host_settings.json) -- "
                  "running local-only until one exists.", file=sys.stderr)
        else:
            reachability_cache = ReachabilityCache()
            peer_cache = PeerIdentityCache(refresh_fn=default_tailscale_peers)
            health_check = make_http_health_check(settings.host_addresses)
            prober = TailnetProber(remote_hosts, health_check, peer_cache, reachability_cache)
            prober.start()
            engine = RemoteFallbackEngine(local_engine, ranking, reachability_cache,
                                           settings.host_addresses, local_settings_record)
    translate_engine = _LazyTranslateEngine(
        lambda: create_engine("whisper-translate", base / "models"))
    pool = AccountPool(_deploy_accounts_path(),
                       base / "state.json",
                       allowlist=settings.account_allowlist or None)
    overlay = OverlayPanel()
    app = rumps.App("speakeasy", title="🎙")
    # Forward-reference box: the real GuiWindow is constructed near the end
    # of run() (needs `history`/`profile_store`/`_do_switch_engine`, all
    # defined later), but earlier code (icon/engine-switch callbacks) needs
    # to push to it if it's open. A single-element list sidesteps needing
    # `nonlocal` across nested closures for a plain forward reference.
    _gui_window = [None]

    # Menu-bar glyph doubles as the "is it active" indicator (TSK-SPK-UX-
    # MENUBARSTATE): an LSUIElement app gets no Dock running-dot by design
    # (see feedback.py), and switching to a regular app would mean a full
    # standard app menu bar -- the icon swap is the platform-idiomatic
    # alternative (SuperWhisper/Wispr Flow both do this). State changes can
    # originate off the main thread (`_process` runs in a background
    # thread), so the title write goes through `run_on_main_thread` same as
    # the overlay panel.
    _STATE_GLYPH = {
        "recording": "🔴",
        "transcribing": "🟡",
        "rewriting": "🟡",
        "polishing": "🟡",
    }
    _DEFAULT_GLYPH = "🎙"

    # Home screen's status chip vocabulary (speakeasy/gui/assets/app.html)
    # is idle/recording/transcribing/attention -- coarser than the overlay's
    # own state machine (rewriting/polishing are still "processing" from a
    # glance-level view, per progressive disclosure: Home doesn't need every
    # sub-state, just "is it doing something right now").
    _GUI_STATE = {
        "recording": ("recording", "Recording", "●"),
        "transcribing": ("transcribing", "Transcribing", "◐"),
        "rewriting": ("transcribing", "Transcribing", "◐"),
        "polishing": ("transcribing", "Transcribing", "◐"),
    }
    _GUI_STATE_DEFAULT = ("idle", "Idle", "●")

    def _render_and_update_icon(model: OverlayModel) -> None:
        overlay.render(model)
        glyph = _STATE_GLYPH.get(model.state, _DEFAULT_GLYPH)
        run_on_main_thread(lambda: setattr(app, "title", glyph))
        if _gui_window[0] is not None:
            state, label, symbol = _GUI_STATE.get(model.state, _GUI_STATE_DEFAULT)
            _gui_window[0].push("dictation_state", {"state": state, "label": label, "symbol": symbol})
            if model.state == "recording":
                from speakeasy.overlay import level_to_bar_heights
                _gui_window[0].push("audio_level", {"levels": level_to_bar_heights(model.level)})

    controller = Controller(
        engine=engine, recorder=Recorder(device=settings.input_device),
        history=History(base / "history.db"), modes=modes, pool=pool,
        settings=settings, overlay_render=_render_and_update_icon,
        translate_engine=translate_engine,
        frontmost=injector.frontmost_app_name,
        rewrite_fn=_configured_rewrite(settings),
        translate_audit_fn=_configured_translate_audit(settings),
        audio_dir=base / "audio",
        clipboard_fn=lambda text: injector._SystemPasteboard().set(text))
    def set_mode(sender):
        settings.active_mode = sender.title
        save_settings(settings)
        for item in app.menu["Mode"].values():
            item.state = item.title == sender.title
    mode_menu = rumps.MenuItem("Mode")
    for name in modes:
        item = rumps.MenuItem(name, callback=set_mode)
        item.state = name == settings.active_mode
        mode_menu.add(item)

    # --- "Models" menu (TSK-SPK-UX-MODELSELECT): pick which local STT
    # engine runs dictation, with the system specs shown for transparency.
    # Only 2 real dictation engines exist (engine/download.py) -- both run
    # fine via int8 CPU inference on any Mac capable of running speakeasy
    # at all, so there's no fabricated speed/accuracy tier ladder here like
    # SuperWhisper's tiny->large lineup, just an honest recommended default.
    specs = detect_specs()
    recommended = recommend_engine(specs)
    model_menu = rumps.MenuItem("Models")
    specs_item = rumps.MenuItem(
        f"This Mac: {specs.chip} · {specs.ram_gb:.0f}GB RAM")
    specs_item.set_callback(None)
    model_menu.add(specs_item)
    model_menu.add(rumps.separator)

    _engine_items: dict[str, "rumps.MenuItem"] = {}

    def _set_engine_loading(name, loading: bool) -> None:
        for n, item in _engine_items.items():
            item.title = (f"⏳ {ENGINE_INFO[n]['label']}" if loading and n == name
                         else ENGINE_INFO[n]["label"])

    def _do_switch_engine(name: str) -> dict:
        """Core engine-swap, callable from the menu AND the GUI window's
        bridge (speakeasy/gui/handlers/models.py) -- one code path, so the
        menu and the GUI window can never drift into different behavior.
        Returns immediately (loading has started in a background thread);
        completion is reported via the SAME feedback.flash the menu already
        used, plus a gui_window push event the GUI's JS can listen for.

        Validates `name` BEFORE persisting anything: the menu's own
        `_switch_engine` can only ever pass a real ENGINE_INFO key (resolved
        from `_engine_items`), but the GUI bridge's `models_switch_engine`
        forwards whatever string the JS payload contains -- an unvalidated
        write to `settings.engine` here would survive to the NEXT launch's
        unguarded `create_engine(settings.engine, ...)` and crash the app
        before the menu bar even appears (engine/__init__.py raises
        ValueError for an unknown engine name)."""
        if name not in ENGINE_INFO:
            return {"ok": False, "error": f"unknown engine {name!r}"}
        if name == settings.engine:
            return {"already_active": True}
        settings.engine = name
        save_settings(settings)
        for n, item in _engine_items.items():
            item.state = n == name
        _set_engine_loading(name, True)

        def _swap():
            overlay.render(OverlayModel(
                "info", message=f"Loading {ENGINE_INFO[name]['label']}…"))
            try:
                new_engine = create_engine(name, base / "models")
                controller.engine = new_engine
            except Exception as exc:
                run_on_main_thread(lambda: rumps.notification(
                    "speakeasy", "Model switch failed", str(exc)))
                run_on_main_thread(lambda: _set_engine_loading(name, False))
                if _gui_window[0] is not None:
                    _gui_window[0].push("engine_switch_result",
                                        {"ok": False, "engine": name, "error": str(exc)})
                return
            run_on_main_thread(lambda: _set_engine_loading(name, False))
            feedback.flash(overlay.render,
                           f"✓ Switched to {ENGINE_INFO[name]['label']}")
            if _gui_window[0] is not None:
                _gui_window[0].push("engine_switch_result",
                                    {"ok": True, "engine": name, "label": ENGINE_INFO[name]["label"]})

        threading.Thread(target=_swap, daemon=True).start()
        return {"started": True}

    def _set_input_device(device: int | None) -> dict:
        """Mic-selection counterpart to `_do_switch_engine` above -- same
        shape (mutate the live shared `settings` object, call
        `save_settings`, done), callable from both the wizard's mic-check
        bridge handler and (future) a menu entry. Validates `device`
        against the CURRENT real enumeration (mics_list_devices) before
        persisting: a stale index (e.g. an unplugged USB mic) never
        reaches `Recorder.start()`/`sounddevice.InputStream`, which would
        raise for an unknown device -- see
        speakeasy/gui/input_device.py::resolve_input_device for the pure
        validate-and-fall-back logic this wraps."""
        available = {d["index"] for d in mics_list_devices({}, None)["devices"]}
        resolved, fell_back = resolve_input_device(device, available)
        settings.input_device = resolved
        save_settings(settings)
        result = {"ok": True, "device": resolved, "fell_back": fell_back}
        if fell_back:
            result["reason"] = (
                f"input device {device} is no longer available; using the system default")
        return result

    def _switch_engine(sender) -> None:
        name = next(n for n, i in _engine_items.items() if i is sender)
        _do_switch_engine(name)

    for name, info in ENGINE_INFO.items():
        title = info["label"]
        item = rumps.MenuItem(title, callback=_switch_engine)
        item.state = name == settings.engine
        _engine_items[name] = item
        model_menu.add(item)
        detail = rumps.MenuItem(
            ("★ Recommended — " if name == recommended else "     ") + info["detail"])
        detail.set_callback(None)
        model_menu.add(detail)

    # --- "Train my voice..." flow (TSK-SPK-TRAIN-T4) ------------------------
    # Prompt display reuses the existing overlay (least-invasive per spec):
    # the "recording" OverlayModel state already renders a label + partial
    # text + is shown/hidden the same way dictation does, so training just
    # supplies its prompt text as `partial` under a distinct mode_name.
    # QC failures and the end-of-session summary use rumps.alert -- simple,
    # transient, no new UI surface needed for v1.
    training_sessions_dir = base / "training" / "sessions"
    training_corpus_path = Path(__file__).resolve().parent / "training" / "corpora" / "harvard_sentences.txt"
    training_vocab_path = base / "training" / "vocab.txt"

    def _training_show_prompt(text):
        overlay.render(OverlayModel("recording", partial=text, mode_name="training"))

    def _training_show_qc_failure(reason, attempts):
        rumps.alert(title="Voice training",
                    message=f"Clip rejected ({reason}) — attempt {attempts}/3")

    def _training_show_summary(summary):
        overlay.render(OverlayModel("hidden"))
        rumps.alert(title="Training session complete",
                    message=(f"{summary['n_clips']} clips recorded, "
                             f"{summary['n_rejected']} rejected, "
                             f"~{summary['est_minutes']} min"))
        controller.training = None
        _refresh_resume_item()

    def _begin_training(session):
        controller.training = TrainingFlowController(
            recorder=Recorder(device=settings.input_device),
            session=session,
            show_prompt=_training_show_prompt,
            show_qc_failure=_training_show_qc_failure,
            show_summary=_training_show_summary)
        _refresh_resume_item()

    # --- Training WIZARD flow (TSK-SPK-TRAIN-WIZARD) ------------------------
    # Same TrainingFlowController, same TrainingSession -- only the UI
    # callbacks differ: instead of the overlay + rumps.alert above, these
    # push real-time events to the wizard's own WKWebView window
    # (speakeasy/gui/wizard_window.py). `wizard_window` is assigned once,
    # near the Home GuiWindow construction below; every closure here only
    # references it by name and is never CALLED until well after that
    # assignment has happened (bridge dispatch / menu clicks, both long
    # after run() has finished its own setup).
    def _begin_training_wizard(session):
        def _show_summary(summary):
            # Real, re-derivable-from-the-manifest per-sentence facts for
            # the wizard's summary "evidence" table -- no fabricated
            # retake counts (the real manifest schema has none; see
            # speakeasy/training/session.py::ManifestPrompt).
            prompts = [
                {"id": p.prompt_id, "source": p.source, "status": p.status,
                 "attempts": p.attempts, "split": p.split}
                for p in session.manifest.prompts
            ]
            wizard_window.push("wizard_summary", {"summary": summary, "prompts": prompts})
            controller.training = None
            _refresh_resume_item()

        def _show_review(text, samples, qc_result):
            # Contract extension consumed here (ADJ-SPK-033): QC facts and
            # clip duration only -- never the raw PCM samples themselves,
            # and no real/simulated waveform render in the review step for
            # this first pass (scoped simplification, see the work order
            # report).
            wizard_window.push("wizard_review", {
                "text": text,
                "seconds": round(len(samples) / SAMPLE_RATE, 2) if len(samples) else 0.0,
                "qc": {
                    "passed": qc_result.passed,
                    "silence_ratio": qc_result.silence_ratio,
                    "rms_dbfs": qc_result.rms_dbfs,
                    "snr_db": qc_result.snr_db,
                },
            })

        controller.training = TrainingFlowController(
            recorder=Recorder(device=settings.input_device),
            session=session,
            # The push carries only `text` -- the wizard page treats it as
            # a refresh signal and re-fetches the full manifest snapshot
            # (source/attempts/counts/current) via training_wizard_get_state
            # so there is exactly one source of truth for that data, not
            # two independently-maintained copies.
            show_prompt=lambda text: wizard_window.push("wizard_prompt", {"text": text}),
            show_qc_failure=lambda reason, attempts: wizard_window.push(
                "wizard_qc_failure",
                {"reason": reason, "attempts": attempts, "max_attempts": MAX_ATTEMPTS}),
            show_summary=_show_summary,
            show_review=_show_review)
        _refresh_resume_item()

    def _open_wizard_for_session(session):
        _begin_training_wizard(session)
        wizard_window.show()

    def _start_training_flow(size_name: str) -> dict:
        """Shared by the wizard's OWN welcome-screen start action
        (training_wizard_start), the Training & Profile dashboard's Start
        button (training_start, once rewired below), and the "Train my
        voice..." menu item (once rewired below): create-or-resume the
        session, then try to open it in the wizard window, falling back
        to the existing overlay flow on ANY failure to do so (no WebKit
        available, a missing/corrupt asset, ...) -- see
        speakeasy/gui/wizard_start.py::open_wizard_or_fallback. Only ONE
        session is ever created/resumed regardless of which surface ends
        up showing it."""
        size = QUICK_SIZE if size_name == "quick" else FULL_SIZE
        session = TrainingSession.create_or_resume(
            training_sessions_dir, size=size, corpus_path=training_corpus_path,
            history_db=base / "history.db",
            vocab_path=training_vocab_path if training_vocab_path.exists() else None)
        result = open_wizard_or_fallback(
            lambda: _open_wizard_for_session(session),
            lambda: _begin_training(session))
        return {"started": True, **result}

    def _resume_training_flow() -> dict:
        """Resume counterpart to _start_training_flow -- same wizard-open-
        with-overlay-fallback wrapping, shared by training_wizard_resume,
        the dashboard's Resume button, and the menu's Resume item."""
        session = TrainingSession.resume(training_sessions_dir)
        if session is None:
            return {"started": False, "reason": "no resumable session"}
        result = open_wizard_or_fallback(
            lambda: _open_wizard_for_session(session),
            lambda: _begin_training(session))
        return {"started": True, **result}

    def _start_training(size):
        # Rewired (TSK-SPK-TRAIN-WIZARD step 5) to open the wizard window
        # first, falling back to the original overlay flow only if that
        # fails -- see _start_training_flow above. The overlay wiring
        # itself (_begin_training/_training_show_prompt/etc.) is
        # untouched; it's just reachable only via the fallback path now.
        size_name = "quick" if size == QUICK_SIZE else "full"

        def _handler(sender):
            _start_training_flow(size_name)
        return _handler

    def _resume_training(sender):
        # Same rewiring as _start_training above.
        _resume_training_flow()

    quick_item = rumps.MenuItem("Quick session (20)", callback=_start_training(QUICK_SIZE))
    full_item = rumps.MenuItem("Full session (80)", callback=_start_training(FULL_SIZE))
    resume_item = rumps.MenuItem("Resume last session", callback=_resume_training)

    def _refresh_resume_item():
        resume_item.set_callback(
            _resume_training if training_resume_available(training_sessions_dir) else None)

    _refresh_resume_item()
    train_menu = rumps.MenuItem("Train my voice…")
    train_menu.add(quick_item)
    train_menu.add(full_item)
    train_menu.add(resume_item)

    # --- History submenu (TSK-SPK-UX-HISTORYMENU): last 10 dictations,
    # click re-copies the text you received (rewritten if a mode ran).
    # Rebuilt on a timer -- rumps has no reliable menu-will-open hook.
    from speakeasy.app import history_menu_entries
    history_reader = History(base / "history.db")
    history_menu = rumps.MenuItem("History")

    def _copy_factory(text):
        def _copy(_sender):
            injector._SystemPasteboard().set(text)
        return _copy

    def _refresh_history(_timer=None):
        try:
            entries = history_menu_entries(history_reader.recent(10))
        except Exception:
            return
        # clear() explodes on a virgin MenuItem (rumps MenuItem._menu is None
        # until the first add) — canary-caught launch crash, guard required.
        if len(history_menu):
            history_menu.clear()
        if not entries:
            empty = rumps.MenuItem("No dictations yet")
            empty.set_callback(None)
            history_menu.add(empty)
            return
        for title, text in entries:
            history_menu.add(rumps.MenuItem(title, callback=_copy_factory(text)))

    _refresh_history()

    # --- Native GUI window (WKWebView host, speakeasy/gui/window.py) -------
    # Reuses the SAME live objects already constructed above (controller,
    # settings, history) -- the GUI window is a second view onto one shared
    # state, never a separate copy of it.
    from speakeasy.gui.window import GuiContext, get_or_create as get_or_create_gui
    from speakeasy.gui.wizard_start import open_wizard_or_fallback
    from speakeasy.gui.wizard_window import get_or_create as get_or_create_wizard
    from speakeasy.training.profile import ProfileStore
    profile_store = ProfileStore(base / "profile")

    def _gui_start_training(size_name: str) -> dict:
        # Rewired (TSK-SPK-TRAIN-WIZARD step 5) to open the wizard window
        # first, falling back to the original overlay flow only if that
        # fails -- see _start_training_flow above.
        return _start_training_flow(size_name)

    def _gui_resume_training() -> dict:
        # Same rewiring as _gui_start_training above.
        return _resume_training_flow()

    gui_ctx = GuiContext(
        controller=controller, settings=settings, history=history_reader,
        profile_store=profile_store, base=base,
        switch_engine=_do_switch_engine, detect_specs=detect_specs,
        start_training=_gui_start_training, resume_training=_gui_resume_training,
        training_resumable=lambda: training_resume_available(training_sessions_dir),
        start_training_wizard=_start_training_flow,
        resume_training_wizard=_resume_training_flow,
        set_input_device=_set_input_device)
    gui_window = get_or_create_gui(gui_ctx)
    _gui_window[0] = gui_window
    wizard_window = get_or_create_wizard(gui_ctx)

    def _open_gui(_sender=None):
        gui_window.show()
    open_gui_item = rumps.MenuItem("Open speakeasy…", callback=_open_gui)

    def _retry_failed(_sender):
        # P1-1: re-run the latest failed dictation from its saved audio (result
        # goes to the clipboard + scratchpad, never pasted blind).
        def _work():
            status = controller.retry()
            if status is None:
                _notify("Nothing to retry", "No failed dictation with saved audio.")
            _refresh_history()
        threading.Thread(target=_work, daemon=True).start()
    retry_item = rumps.MenuItem("Retry last failed dictation", callback=_retry_failed)

    app.menu = [open_gui_item, rumps.separator, mode_menu, model_menu, train_menu, history_menu,
                retry_item]
    rumps.Timer(_refresh_history, 15).start()

    listener = HotkeyListener(
        settings.hotkey,
        on_toggle=controller.on_tap_toggle,
        on_hold_start=controller.on_hold_start,
        on_hold_end=controller.on_hold_end)

    # --- degraded startup + self-heal (TSK-SPK-HOTFIX-LAUNCH) ---------------
    # Without the Accessibility grant, listener.start() raises
    # PermissionError; pre-fix that fired before app.run() and killed the
    # packaged app into py2app's error dialog on first Dock launch. Now the
    # menu bar ALWAYS comes up: the hotkey degrades off, macOS is asked to
    # show the grant prompt (which also lists the app in System Settings),
    # and a poll enables the hotkey the moment the grant appears — no
    # relaunch needed.
    hotkey_state = {"active": False}

    def _notify(title: str, message: str) -> None:
        try:
            rumps.notification("speakeasy", title, message)
        except Exception:
            pass  # notifications are best-effort (unsigned/dev builds)

    def _on_degraded(_msg: str) -> None:
        try:
            permissions.accessibility_trusted(prompt=True)
        except Exception:
            pass
        _notify("Hotkey disabled",
                "Grant Accessibility in System Settings > Privacy & "
                "Security. speakeasy will enable the hotkey automatically.")

    hotkey_state["active"] = permissions.start_hotkey_with_degradation(
        listener, _on_degraded)

    if not hotkey_state["active"]:
        def _retry_hotkey(timer):
            try:
                if not permissions.accessibility_trusted():
                    return
            except Exception:
                return
            if permissions.start_hotkey_with_degradation(listener, lambda m: None):
                hotkey_state["active"] = True
                timer.stop()
                _notify("Hotkey enabled", "Right ⌘ dictation is now active.")
        rumps.Timer(_retry_hotkey, 3).start()

    # Run-loop heartbeat: scripts/canary.sh launches the .app through
    # LaunchServices (the Dock path) and passes only when this appears.
    heartbeat_path = base / "run" / "heartbeat.json"

    def _beat(_timer=None):
        try:
            canary.write_heartbeat(heartbeat_path, {
                "pid": os.getpid(),
                "hotkey_active": hotkey_state["active"],
                "engine": settings.engine,
            })
        except Exception:
            pass
    _beat()
    rumps.Timer(_beat, 30).start()

    # Dock-click feedback (TSK-SPK-UX-DOCKFEEDBACK): an LSUIElement app has
    # no bounce/dot/app-menu, so flash the overlay on launch and again on
    # every Dock-icon click while running.
    def _running_flash():
        feedback.flash(overlay.render, feedback.RUNNING_MESSAGE)
    feedback.install_reopen_handler(_running_flash)
    rumps.Timer(lambda t: (_running_flash(), t.stop()), 1.0).start()

    app.run()

if __name__ == "__main__":
    if "--canary" in sys.argv:
        raise SystemExit(canary.main())
    if "--pipeline-selftest" in sys.argv:
        raise SystemExit(canary.pipeline_main(sys.argv[sys.argv.index("--pipeline-selftest") + 1:]))
    run()
