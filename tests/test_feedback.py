"""TSK-SPK-UX-DOCKFEEDBACK: launch/Dock-click feedback flash."""
from speakeasy.feedback import RUNNING_MESSAGE, flash, install_reopen_handler
from speakeasy.overlay import OverlayModel


class FakeTimer:
    instances = []

    def __init__(self, callback, interval):
        self.callback = callback
        self.interval = interval
        self.started = False
        self.stopped = False
        FakeTimer.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def fire(self):
        self.callback(self)


class TestFlash:
    def setup_method(self):
        FakeTimer.instances.clear()

    def test_shows_info_then_hides_on_timer(self):
        rendered = []
        flash(rendered.append, "hello", duration=1.5, timer_factory=FakeTimer)
        assert len(rendered) == 1
        assert rendered[0].state == "info" and rendered[0].label() == "hello"
        (t,) = FakeTimer.instances
        assert t.started and t.interval == 1.5
        t.fire()
        assert rendered[-1].state == "hidden"
        assert t.stopped

    def test_hide_still_stops_timer_when_render_raises(self):
        calls = {"n": 0}

        def render(model):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("render broke")

        try:
            flash(render, "x", timer_factory=FakeTimer)
            FakeTimer.instances[0].fire()
        except RuntimeError:
            pass
        assert FakeTimer.instances[0].stopped


class TestOverlayInfoState:
    def test_info_label_is_message_verbatim(self):
        m = OverlayModel(state="info", message=RUNNING_MESSAGE)
        assert m.label() == RUNNING_MESSAGE

    def test_other_states_unaffected(self):
        assert OverlayModel(state="recording", mode_name="raw").label().startswith("●")
        assert OverlayModel(state="hidden").label() == ""


class TestReopenHandler:
    def test_returns_bool_and_never_raises(self):
        # Full objc wiring is exercised by the packaged-app canary; here we
        # only require the contract: returns True on successful install (or
        # False, logged, if rumps internals are absent in the test env).
        assert install_reopen_handler(lambda: None) in (True, False)
