"""speakeasy/gui/wizard_start.py::open_wizard_or_fallback -- the runtime-
degradation wrapper every real wizard-opening entry point routes through
(training_wizard_start/resume handlers, the dashboard's Start/Resume
buttons, and the "Train my voice..." menu items -- see
speakeasy/__main__.py). These tests exercise a REAL failure path (a
raising open_wizard callable), not just a reading of the source."""
from speakeasy.gui.wizard_start import open_wizard_or_fallback


def test_success_path_runs_open_wizard_not_fallback():
    calls = []
    result = open_wizard_or_fallback(
        open_wizard=lambda: calls.append("opened"),
        fallback=lambda: calls.append("fallback"),
    )
    assert calls == ["opened"]
    assert result == {"surface": "wizard"}


def test_failure_path_runs_fallback_and_reports_the_error():
    calls = []

    def _failing_open():
        raise RuntimeError("no WebKit available")

    result = open_wizard_or_fallback(
        open_wizard=_failing_open,
        fallback=lambda: calls.append("fallback"),
    )

    assert calls == ["fallback"]
    assert result["surface"] == "overlay"
    assert "no WebKit available" in result["error"]


def test_failure_path_never_reraises():
    """The whole point: a bridge handler or menu callback calling this must
    never see the original exception propagate -- it always gets a plain
    dict back, even when both the wizard open AND (hypothetically) nothing
    else goes wrong with the fallback."""
    result = open_wizard_or_fallback(
        open_wizard=lambda: (_ for _ in ()).throw(OSError("asset missing")),
        fallback=lambda: None,
    )
    assert result["surface"] == "overlay"
    assert "asset missing" in result["error"]


def test_fallback_exception_is_not_swallowed():
    """If the fallback itself blows up, that's a real bug the caller needs
    to see -- open_wizard_or_fallback only guards the wizard-open step,
    not the degraded path itself."""
    import pytest

    def _failing_open():
        raise RuntimeError("wizard boom")

    def _failing_fallback():
        raise ValueError("overlay boom")

    with pytest.raises(ValueError, match="overlay boom"):
        open_wizard_or_fallback(_failing_open, _failing_fallback)
