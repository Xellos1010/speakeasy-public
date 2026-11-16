"""The training wizard loads a local file:// asset and dispatches to
in-process Python objects only (the shared bridge registry, the live
TrainingFlowController) -- there is no reason for either the window host
or its bridge handlers to ever touch a network primitive. Mirrors
tests/test_bench.py::test_no_platform_specific_imports' source-grep
enforcement style (ADJ-SPK-024)."""
from pathlib import Path

import speakeasy.gui.handlers.mics as mics
import speakeasy.gui.handlers.training_wizard as training_wizard
import speakeasy.gui.wizard_window as wizard_window

_FORBIDDEN = ("socket", "http.client", "httplib", "urllib", "requests")


def _assert_no_network_primitives(module) -> None:
    src = Path(module.__file__).read_text()
    for name in _FORBIDDEN:
        # word-boundary-ish match on real import statements -- avoids false
        # positives on unrelated identifiers (e.g. a hypothetical
        # "websocket" or "sockets" would not match "socket" here since
        # there is no trailing space/import context).
        assert f"import {name}\n" not in src and f"import {name} " not in src, \
            f"{module.__file__} must not `import {name}`"
        assert f"from {name} " not in src and f"from {name}\n" not in src, \
            f"{module.__file__} must not `from {name} import ...`"


def test_wizard_window_has_no_network_primitives():
    _assert_no_network_primitives(wizard_window)


def test_training_wizard_handlers_has_no_network_primitives():
    _assert_no_network_primitives(training_wizard)


def test_mics_handlers_has_no_network_primitives():
    # mics.py's device enumeration (and now the mic-check screen's device
    # picker, which calls it) is local audio hardware via sounddevice, not
    # a network primitive -- same guarantee as the two modules above.
    _assert_no_network_primitives(mics)
