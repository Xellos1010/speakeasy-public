"""Per-clip audio QC gate for the voice-training environment.

Governing spec: docs/superpowers/specs/2026-07-18-voice-training-environment.md
(§3 step 4 "Per-clip QC gate"; §4 manifest `qc` schema; §8 acceptance criterion 2).

All functions are pure and operate on float32 mono PCM at 16 kHz (the
convention used throughout this codebase — see `speakeasy/audio.py::Recorder`,
whose default `samplerate` is 16000 and whose `stop()`/`snapshot()` return
float32 arrays in the [-1.0, 1.0] range). No file I/O, no state.

IMPORTANT FIELD-NAME NOTE (read before consuming `QCResult` downstream, e.g.
in T3's session controller): the manifest schema (spec §4) names the
voiced-frame fraction `silence_ratio`, e.g. the schema's own example shows
`"silence_ratio": 0.81` for a clip that clearly *passed* QC. Despite the name,
this field holds the fraction of frames the energy-threshold VAD classified
as VOICED, not silent. 0.0 means the whole clip was silent; 1.0 means every
frame was voiced. A clip is rejected when this value is BELOW 0.4 (less than
40% voiced -> too much silence), matching spec §3 step 4 and §8 AC2
(`qc.silence_ratio>=0.4`). The field name is kept exactly as `silence_ratio`
here and in `QCResult` to match the manifest schema verbatim; treat it
semantically as "voiced_fraction".
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Fixed operating convention for this whole module: 16 kHz mono float32,
# matching speakeasy/audio.py::Recorder. silence_ratio()/snr_db() intentionally
# take no sample_rate parameter -- there is exactly one supported rate.
SAMPLE_RATE = 16000

CLIP_THRESHOLD = 0.99
MIN_VOICED_RATIO = 0.4
MIN_RMS_DBFS = -45.0
MIN_SNR_DB = 15.0

_POWER_EPS = 1e-12  # floor to keep log10()/division well-defined for silent input


def check_clipping(samples: np.ndarray) -> bool:
    """True if `samples` clips: max(abs(samples)) >= 0.99.

    0.99 is used (rather than 1.0) as an int16-full-scale proxy on float32
    audio, per spec §3 step 4. Empty input is treated as not clipped.
    """
    samples = np.asarray(samples, dtype=np.float32)
    if samples.size == 0:
        return False
    return bool(np.max(np.abs(samples)) >= CLIP_THRESHOLD)


def rms_dbfs(samples: np.ndarray) -> float:
    """RMS level of `samples` in dBFS (full-scale float32, 1.0 == 0 dBFS).

    Returns -inf for an empty or all-zero (silent) clip rather than raising
    or producing NaN.
    """
    samples = np.asarray(samples, dtype=np.float32)
    if samples.size == 0:
        return float("-inf")
    mean_power = float(np.mean(np.square(samples, dtype=np.float64)))
    if mean_power <= 0.0:
        return float("-inf")
    rms = np.sqrt(mean_power)
    return float(20.0 * np.log10(rms))


def silence_ratio(
    samples: np.ndarray,
    frame_ms: int = 30,
    energy_threshold_dbfs: float = -40.0,
) -> float:
    """Fraction of `frame_ms`-length frames classified VOICED by a simple
    energy-threshold VAD (no model). See the module docstring for the
    important note that this is the VOICED fraction, not the silent one.

    A frame is "voiced" when its RMS level (via `rms_dbfs`) is
    >= `energy_threshold_dbfs`. Returns 0.0 for an empty clip (treated as
    fully silent) rather than raising or dividing by zero.
    """
    samples = np.asarray(samples, dtype=np.float32)
    if samples.size == 0:
        return 0.0
    frame_len = max(1, int(SAMPLE_RATE * frame_ms / 1000))
    n_frames = int(np.ceil(samples.size / frame_len))
    voiced = 0
    for i in range(n_frames):
        frame = samples[i * frame_len:(i + 1) * frame_len]
        if rms_dbfs(frame) >= energy_threshold_dbfs:
            voiced += 1
    return voiced / n_frames


def snr_db(samples: np.ndarray, noise_ms: int = 200) -> float:
    """Leading/trailing noise-floor SNR estimate, in dB.

    The noise floor is estimated from the leading + trailing `noise_ms`
    segments of the clip; signal power is estimated from whatever remains
    in the middle. For clips shorter than 2*noise_ms (not enough samples to
    carve out non-overlapping leading/trailing windows), the windows are
    shrunk to fit and, if no middle segment remains, the whole clip is reused
    as the signal estimate too -- this degrades gracefully to ~0 dB rather
    than crashing or dividing by zero.

    Returns -inf if both the estimated noise and signal power are zero
    (e.g. an all-zero/silent clip).
    """
    samples = np.asarray(samples, dtype=np.float32)
    if samples.size == 0:
        return float("-inf")

    n = int(SAMPLE_RATE * noise_ms / 1000)
    n = min(n, samples.size // 2)

    if n < 1:
        noise = samples
        signal = samples
    else:
        noise = np.concatenate([samples[:n], samples[-n:]])
        signal = samples[n:samples.size - n]
        if signal.size == 0:
            signal = samples

    noise_power = float(np.mean(np.square(noise, dtype=np.float64))) if noise.size else 0.0
    signal_power = float(np.mean(np.square(signal, dtype=np.float64))) if signal.size else 0.0

    if signal_power <= 0.0 and noise_power <= 0.0:
        return float("-inf")

    ratio = signal_power / (noise_power + _POWER_EPS)
    if ratio <= 0.0:
        return float("-inf")
    return float(10.0 * np.log10(ratio))


@dataclass(frozen=True)
class QCResult:
    """Per-clip QC verdict, matching the manifest `qc` schema (spec §4).

    See the module docstring for the `silence_ratio` naming caveat: this
    field holds the VOICED-frame fraction, not the silent fraction.

    Fields:
        clipping:      see `check_clipping`.
        silence_ratio: voiced-frame fraction from `silence_ratio()` (0.0-1.0).
        rms_dbfs:      overall RMS level in dBFS, from `rms_dbfs()`.
        snr_db:        leading/trailing noise-floor SNR estimate, from `snr_db()`.
        passed:        True iff all QC gates pass (spec §3 step 4 / §8 AC2):
                        `not clipping and silence_ratio >= 0.4
                         and rms_dbfs >= -45.0 and snr_db >= 15.0`.
    """

    clipping: bool
    silence_ratio: float
    rms_dbfs: float
    snr_db: float
    passed: bool


def qc_clip(samples: np.ndarray, sample_rate: int = 16000) -> QCResult:
    """Run the full per-clip QC gate (spec §3 step 4) and return a `QCResult`.

    `sample_rate` is accepted (matching the manifest/session-controller call
    convention) but this module's helpers are hardcoded to the codebase's one
    supported rate (`SAMPLE_RATE = 16000`, matching `speakeasy/audio.py`'s
    `Recorder` default) -- passing any other value raises `ValueError` rather
    than silently mis-measuring a resampled clip.
    """
    if sample_rate != SAMPLE_RATE:
        raise ValueError(
            f"qc_clip only supports sample_rate={SAMPLE_RATE} (got {sample_rate}); "
            "resample before calling qc_clip."
        )

    samples = np.asarray(samples, dtype=np.float32)

    clipping = check_clipping(samples)
    voiced_ratio = silence_ratio(samples)
    rms = rms_dbfs(samples)
    snr = snr_db(samples)

    passed = (
        not clipping
        and voiced_ratio >= MIN_VOICED_RATIO
        and rms >= MIN_RMS_DBFS
        and snr >= MIN_SNR_DB
    )

    return QCResult(
        clipping=clipping,
        silence_ratio=voiced_ratio,
        rms_dbfs=rms,
        snr_db=snr,
        passed=passed,
    )
