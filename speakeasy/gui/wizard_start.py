"""Runtime-degradation wrapper for opening the training wizard window
(speakeasy/gui/wizard_window.py).

Every real entry point that opens the wizard (the training_wizard_start/
training_wizard_resume bridge handlers, the Training & Profile dashboard's
Start/Resume buttons, and the "Train my voice..." menu items -- see
speakeasy/__main__.py's _start_training_flow/_resume_training_flow) routes
through `open_wizard_or_fallback` so that ANY failure to open the wizard
(no WebKit available, a missing/corrupt asset, ...) degrades to the
existing overlay-based training flow (`_begin_training`) instead of
crashing or silently doing nothing.

Extracted into its own tiny module specifically so this fallback behavior
is unit-testable without AppKit/WebKit: speakeasy/__main__.py's `run()` is
a large, side-effecting, not-independently-invokable function, so the
"open wizard, on failure fall back to overlay" logic needs to live
somewhere a plain pytest test can exercise a REAL failure path (see
tests/gui/test_wizard_start.py).
"""
from typing import Callable


def open_wizard_or_fallback(open_wizard: Callable[[], None],
                             fallback: Callable[[], None]) -> dict:
    """Call `open_wizard()`; if it raises ANY exception, call `fallback()`
    instead and report the failure. Never re-raises -- a failed wizard
    open must not crash the caller (a bridge handler, a menu callback),
    it must silently keep the user in a working state (the overlay flow).

    Returns {"surface": "wizard"} on success, or
    {"surface": "overlay", "error": str(exc)} if `open_wizard` raised and
    `fallback` ran instead.
    """
    try:
        open_wizard()
        return {"surface": "wizard"}
    except Exception as exc:
        fallback()
        return {"surface": "overlay", "error": str(exc)}
