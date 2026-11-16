import numpy as np
import pytest

from speakeasy.training.qc import (
    QCResult,
    check_clipping,
    qc_clip,
    rms_dbfs,
    silence_ratio,
    snr_db,
)

SR = 16000


def _tone(duration_s: float, freq: float = 440.0, amplitude: float = 0.5, sr: int = SR) -> np.ndarray:
    t = np.arange(int(duration_s * sr), dtype=np.float64) / sr
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _noise(duration_s: float, amplitude: float = 0.001, sr: int = SR, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(duration_s * sr)) * amplitude).astype(np.float32)


# ---------------------------------------------------------------------------
# check_clipping
# ---------------------------------------------------------------------------

def test_check_clipping_all_zero_is_false():
    assert check_clipping(np.zeros(1600, dtype=np.float32)) is False


def test_check_clipping_pure_tone_below_threshold_is_false():
    assert check_clipping(_tone(0.5, amplitude=0.5)) is False


def test_check_clipping_clipped_square_wave_is_true():
    square = np.ones(1600, dtype=np.float32)
    square[::2] = -1.0
    assert check_clipping(square) is True


def test_check_clipping_exactly_at_threshold_is_true():
    samples = np.zeros(10, dtype=np.float32)
    samples[0] = 0.99
    assert check_clipping(samples) is True


def test_check_clipping_empty_array_is_false():
    assert check_clipping(np.zeros(0, dtype=np.float32)) is False


# ---------------------------------------------------------------------------
# rms_dbfs
# ---------------------------------------------------------------------------

def test_rms_dbfs_all_zero_is_negative_infinity():
    assert rms_dbfs(np.zeros(1600, dtype=np.float32)) == float("-inf")


def test_rms_dbfs_empty_array_is_negative_infinity():
    assert rms_dbfs(np.zeros(0, dtype=np.float32)) == float("-inf")


def test_rms_dbfs_full_scale_tone_is_near_zero_dbfs():
    # A full-scale sine has RMS = 1/sqrt(2) => ~-3 dBFS
    level = rms_dbfs(_tone(0.5, amplitude=1.0))
    assert -3.5 < level < -2.5


def test_rms_dbfs_quiet_tone_is_low():
    level = rms_dbfs(_tone(0.5, amplitude=0.001))
    assert level < -45.0


# ---------------------------------------------------------------------------
# silence_ratio (voiced-frame fraction)
# ---------------------------------------------------------------------------

def test_silence_ratio_all_zero_is_zero_no_crash():
    assert silence_ratio(np.zeros(1600, dtype=np.float32)) == 0.0


def test_silence_ratio_empty_array_is_zero():
    assert silence_ratio(np.zeros(0, dtype=np.float32)) == 0.0


def test_silence_ratio_continuous_tone_is_fully_voiced():
    ratio = silence_ratio(_tone(1.0, amplitude=0.5))
    assert ratio == pytest.approx(1.0)


def test_silence_ratio_half_silent_half_tone():
    clip = np.concatenate([np.zeros(int(0.5 * SR), dtype=np.float32), _tone(0.5, amplitude=0.5)])
    ratio = silence_ratio(clip)
    assert 0.4 < ratio < 0.6


def test_silence_ratio_bounded_between_zero_and_one():
    ratio = silence_ratio(_tone(0.37, amplitude=0.5))
    assert 0.0 <= ratio <= 1.0


# ---------------------------------------------------------------------------
# snr_db
# ---------------------------------------------------------------------------

def test_snr_db_all_zero_no_div_by_zero():
    result = snr_db(np.zeros(1600, dtype=np.float32))
    assert result == float("-inf")


def test_snr_db_empty_array_no_crash():
    assert snr_db(np.zeros(0, dtype=np.float32)) == float("-inf")


def test_snr_db_tone_with_quiet_noise_floor_is_high():
    noise_ms = 200
    lead = _noise(noise_ms / 1000, amplitude=0.001, seed=1)
    trail = _noise(noise_ms / 1000, amplitude=0.001, seed=2)
    mid = _tone(1.0, amplitude=0.5)
    clip = np.concatenate([lead, mid, trail])
    result = snr_db(clip, noise_ms=noise_ms)
    assert result >= 15.0


def test_snr_db_pure_continuous_tone_has_no_meaningful_gain():
    # No distinguishable noise-floor segment: edges are as loud as the middle,
    # so the estimate degenerates toward ~0 dB rather than reporting false SNR.
    result = snr_db(_tone(1.0, amplitude=0.5), noise_ms=200)
    assert -3.0 < result < 3.0


def test_snr_db_short_clip_below_two_noise_windows_does_not_crash():
    # 100 ms clip vs. default noise_ms=200 (2*noise_ms would be 400ms)
    clip = _tone(0.1, amplitude=0.5)
    result = snr_db(clip, noise_ms=200)
    assert np.isfinite(result) or result == float("-inf")


def test_snr_db_single_sample_does_not_crash():
    result = snr_db(np.array([0.5], dtype=np.float32), noise_ms=200)
    assert np.isfinite(result) or result == float("-inf")


# ---------------------------------------------------------------------------
# qc_clip / QCResult
# ---------------------------------------------------------------------------

def _good_clip() -> np.ndarray:
    noise_ms = 200
    lead = _noise(noise_ms / 1000, amplitude=0.001, seed=3)
    trail = _noise(noise_ms / 1000, amplitude=0.001, seed=4)
    mid = _tone(1.5, amplitude=0.3)
    return np.concatenate([lead, mid, trail])


def test_qc_clip_returns_qcresult_dataclass():
    result = qc_clip(_good_clip())
    assert isinstance(result, QCResult)


def test_qc_clip_good_clip_passes():
    result = qc_clip(_good_clip())
    assert result.clipping is False
    assert result.silence_ratio >= 0.4
    assert result.rms_dbfs >= -45.0
    assert result.snr_db >= 15.0
    assert result.passed is True


def test_qc_clip_all_zero_fails():
    result = qc_clip(np.zeros(16000, dtype=np.float32))
    assert result.passed is False
    assert result.clipping is False
    assert result.silence_ratio == 0.0


def test_qc_clip_clipped_square_wave_fails():
    square = np.ones(16000, dtype=np.float32)
    square[::2] = -1.0
    result = qc_clip(square)
    assert result.clipping is True
    assert result.passed is False


def test_qc_clip_mostly_silent_fails_on_silence_ratio():
    clip = np.concatenate([
        _tone(0.2, amplitude=0.5),
        np.zeros(int(1.8 * SR), dtype=np.float32),
    ])
    result = qc_clip(clip)
    assert result.silence_ratio < 0.4
    assert result.passed is False


def test_qc_clip_quiet_clip_fails_on_rms():
    clip = _tone(1.0, amplitude=0.0005)
    result = qc_clip(clip)
    assert result.rms_dbfs < -45.0
    assert result.passed is False


def test_qc_clip_field_names_match_manifest_schema():
    result = qc_clip(_good_clip())
    assert set(vars(result).keys()) == {"clipping", "silence_ratio", "rms_dbfs", "snr_db", "passed"}


def test_qc_clip_rejects_non_16k_sample_rate():
    with pytest.raises(ValueError):
        qc_clip(_good_clip(), sample_rate=8000)


def test_qc_clip_short_clip_does_not_crash():
    clip = _tone(0.05, amplitude=0.3)
    result = qc_clip(clip)
    assert isinstance(result, QCResult)
