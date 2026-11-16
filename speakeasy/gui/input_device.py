"""Validation for persisting speakeasy's mic input-device selection
(TSK-SPK-MICSELECT). Extracted into its own tiny module for the same reason
speakeasy/gui/wizard_start.py's `open_wizard_or_fallback` is: speakeasy/
__main__.py's run() is a large, side-effecting, not-independently-invokable
function, so the "does this requested device index still exist, and what do
we fall back to if not" logic needs to live somewhere a plain pytest test
can exercise it directly.
"""
from typing import Iterable, Optional, Tuple


def resolve_input_device(requested: Optional[int],
                          available_indices: Iterable[int]) -> Tuple[Optional[int], bool]:
    """Validate `requested` against `available_indices` (real input-capable
    device indices, e.g. from speakeasy/gui/handlers/mics.py::mics_list_devices).

    Returns (resolved_device, fell_back):
      - requested is None -> (None, False): explicit "use system default",
        always valid, never a fallback.
      - requested is a real, currently-available index -> (requested, False).
      - requested is stale (e.g. an unplugged USB mic no longer enumerated)
        -> (None, True): fall back to the system default rather than let a
        bad index reach Recorder.start()/sounddevice.InputStream, which
        raises for an unknown device index.
    """
    if requested is None:
        return None, False
    if requested in available_indices:
        return requested, False
    return None, True
