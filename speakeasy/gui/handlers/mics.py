"""Bridge handlers for the Mics & Speakers screen.

SCOPE: there is no speaker-diarization backend anywhere in this codebase --
no speaker-profile store, no per-stream translation, no auto-detection of
who is talking. The only real-data wiring this module does is enumerate the
Mac's actual physical audio input devices via `sounddevice`. Everything else
on the Mics & Speakers screen (speaker assignment, confirm chips, live
segments) remains local JS mock state on the frontend, pending a real
diarization backend.

Actions:
  mics_list_devices -> {devices: [{index, name, is_default}, ...]} for
    every real input-capable device (max_input_channels > 0) reported by
    sounddevice.query_devices(). `is_default` is True for the one device
    index reported by `default_device_fn()` (sounddevice.default.device[0]
    by default) -- if that lookup raises, or resolves to an index not
    among the enumerated input devices, every device's `is_default` is
    False rather than guessing. Never raises -- on any query failure
    (e.g. no audio subsystem available, as in some sandboxed/CI contexts)
    returns {devices: [], error: str} instead of crashing the bridge.
"""
from speakeasy.gui.bridge import register


def _default_query_devices():
    import sounddevice as sd  # imported lazily -- never touch audio hardware at module import time
    return sd.query_devices()


def _default_default_device_fn():
    import sounddevice as sd  # imported lazily, same discipline as query_devices above
    return sd.default.device[0]


@register("mics_list_devices")
def mics_list_devices(_payload, _ctx, query_devices=_default_query_devices,
                       default_device_fn=_default_default_device_fn):
    try:
        raw_devices = query_devices()
    except Exception as exc:
        return {"devices": [], "error": str(exc)}

    try:
        default_index = default_device_fn()
    except Exception:
        default_index = None

    devices = [
        {"index": idx, "name": d["name"], "is_default": idx == default_index}
        for idx, d in enumerate(raw_devices)
        if d.get("max_input_channels", 0) > 0
    ]
    return {"devices": devices}
