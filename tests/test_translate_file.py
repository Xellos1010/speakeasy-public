import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from speakeasy.translate_file import (
    AudioDecodeError,
    TranslateFileError,
    TranscriptLine,
    format_timestamp,
    segment_audio,
    find_silence_points,
    load_audio,
    render_markdown,
    write_output,
    default_output_path,
    polish_transcript,
    PolishError,
)


# --------------------------------------------------------------------------
# Timestamp formatting
# --------------------------------------------------------------------------

def test_format_timestamp_zero():
    assert format_timestamp(0) == "00:00"


def test_format_timestamp_under_minute():
    assert format_timestamp(5.4) == "00:05"


def test_format_timestamp_minutes():
    assert format_timestamp(65) == "01:05"


def test_format_timestamp_long_audio_no_hour_rollover():
    # MM is allowed to exceed 59 rather than rolling into hours.
    assert format_timestamp(3661) == "61:01"


# --------------------------------------------------------------------------
# VAD chunker math on synthetic arrays
# --------------------------------------------------------------------------

def _tone(rate, seconds, amplitude=0.5):
    n = int(rate * seconds)
    t = np.arange(n) / rate
    return (amplitude * np.sin(2 * np.pi * 5 * t)).astype(np.float32)


def _silence(rate, seconds):
    return np.zeros(int(rate * seconds), dtype=np.float32)


def test_segment_audio_short_clip_is_single_segment():
    rate = 100  # small synthetic rate keeps arrays tiny and math easy to reason about
    samples = _tone(rate, 3.0)
    segs = segment_audio(samples, sample_rate=rate, min_dur=5.0, max_dur=28.0)
    assert segs == [(0, len(samples))]


def test_segment_audio_empty_returns_no_segments():
    assert segment_audio(np.array([], dtype=np.float32)) == []


def test_segment_audio_cuts_on_silence_gap():
    rate = 100
    speech1 = _tone(rate, 10.0)
    gap = _silence(rate, 1.0)
    speech2 = _tone(rate, 10.0)
    samples = np.concatenate([speech1, gap, speech2])
    segs = segment_audio(samples, sample_rate=rate, min_dur=5.0, max_dur=15.0,
                         frame_ms=50, silence_rms=0.05)
    assert len(segs) == 2
    # first segment should end inside/near the silence gap, not exactly at max_dur
    first_end = segs[0][1]
    gap_start = len(speech1)
    gap_end = len(speech1) + len(gap)
    assert gap_start - int(0.5 * rate) <= first_end <= gap_end + int(0.5 * rate)
    # segments are contiguous and cover the whole signal
    assert segs[0][0] == 0
    assert segs[1][0] == segs[0][1]
    assert segs[-1][1] == len(samples)


def test_segment_audio_forces_hard_cut_when_no_silence():
    rate = 100
    samples = _tone(rate, 40.0)  # continuous tone, no silence anywhere
    segs = segment_audio(samples, sample_rate=rate, min_dur=5.0, max_dur=15.0)
    max_samples = int(15.0 * rate)
    for start, end in segs:
        assert end - start <= max_samples
        assert end - start > 0
    assert segs[0] == (0, max_samples)
    assert segs[-1][1] == len(samples)


def test_segment_audio_segments_are_contiguous_and_complete():
    rate = 100
    samples = _tone(rate, 5.0)
    gap = _silence(rate, 1.0)
    samples = np.concatenate([samples, gap, _tone(rate, 30.0)])
    segs = segment_audio(samples, sample_rate=rate, min_dur=5.0, max_dur=12.0)
    assert segs[0][0] == 0
    assert segs[-1][1] == len(samples)
    for (s1, e1), (s2, e2) in zip(segs, segs[1:]):
        assert e1 == s2


def test_find_silence_points_flags_quiet_frames():
    rate = 100
    samples = np.concatenate([_tone(rate, 1.0), _silence(rate, 1.0), _tone(rate, 1.0)])
    points = find_silence_points(samples, sample_rate=rate, frame_ms=100, silence_rms=0.05)
    assert points  # should find at least one silent frame
    assert all(int(rate * 1.0) - int(rate * 0.15) <= p <= int(rate * 2.0) + int(rate * 0.15)
              for p in points)


# --------------------------------------------------------------------------
# Output-file structure
# --------------------------------------------------------------------------

def test_render_markdown_header_and_lines():
    lines = [TranscriptLine(0.0, "Hello there."), TranscriptLine(65.0, "How are you?")]
    md = render_markdown(Path("/tmp/conv.wav"), "sherpa-onnx-whisper-small", lines, mode="raw")
    assert "# Translation: conv.wav" in md
    assert "Mode: raw" in md
    assert "speaker labels not available" in md
    assert "[00:00] Hello there." in md
    assert "[01:05] How are you?" in md


def test_write_output_creates_file(tmp_path):
    out = tmp_path / "sub" / "out.md"
    lines = [TranscriptLine(0.0, "Hola translated.")]
    written = write_output(out, Path("in.wav"), "model-x", lines)
    assert written == out
    assert out.exists()
    assert "Hola translated." in out.read_text()


def test_default_output_path():
    assert default_output_path(Path("/x/conv.wav")) == Path("/x/conv.translated.md")
    assert default_output_path(Path("meeting.m4a")) == Path("meeting.translated.md")


# --------------------------------------------------------------------------
# ffmpeg-missing / bad-input error path (mocked)
# --------------------------------------------------------------------------

def test_load_audio_raises_actionable_message_when_ffmpeg_missing_and_not_wav(tmp_path):
    src = tmp_path / "conv.m4a"
    src.write_bytes(b"not really audio")
    with pytest.raises(TranslateFileError) as ei:
        load_audio(src, ffmpeg_bin=None, which=lambda name: None)
    assert "ffmpeg" in str(ei.value).lower()
    assert "brew install ffmpeg" in str(ei.value)


def test_load_audio_missing_file_raises():
    with pytest.raises(TranslateFileError):
        load_audio(Path("/nonexistent/path/does-not-exist.wav"),
                   ffmpeg_bin=None, which=lambda name: None)


def test_load_audio_uses_ffmpeg_when_available(tmp_path):
    src = tmp_path / "conv.wav"
    src.write_bytes(b"fake")
    calls = []

    def fake_runner(cmd, **kw):
        calls.append(cmd)
        data = np.array([0.1, -0.2, 0.3], dtype="<f4").tobytes()
        return subprocess.CompletedProcess(cmd, 0, stdout=data, stderr=b"")

    samples = load_audio(src, ffmpeg_bin="ffmpeg", runner=fake_runner)
    assert calls and calls[0][0] == "ffmpeg"
    assert isinstance(samples, np.ndarray)
    assert len(samples) == 3


def test_load_audio_ffmpeg_failure_raises_decode_error(tmp_path):
    src = tmp_path / "conv.mp3"
    src.write_bytes(b"fake")

    def failing_runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"Invalid data found")

    with pytest.raises(AudioDecodeError):
        load_audio(src, ffmpeg_bin="ffmpeg", runner=failing_runner)


def test_load_audio_pure_python_wav_path(tmp_path):
    src = tmp_path / "conv.wav"
    with wave.open(str(src), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((np.array([0, 16384, -16384], dtype="<i2")).tobytes())
    samples = load_audio(src, ffmpeg_bin=None, which=lambda name: None)
    assert len(samples) == 3
    assert abs(samples[1] - 0.5) < 0.01


# --------------------------------------------------------------------------
# --polish path (account rotation replicated from rewrite.py's pattern)
# --------------------------------------------------------------------------

class FakePool:
    def __init__(self, names):
        self.names = list(names)
        self.cooled = []

    def next_account(self):
        from speakeasy.accounts import Account
        if not self.names:
            return None
        return Account(self.names.pop(0), Path("/tmp"))

    def mark_cooling(self, name, seconds=3600.0):
        self.cooled.append(name)


def test_polish_transcript_success():
    def ok_runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="[00:00] Hi there.", stderr="")

    text, account = polish_transcript("[00:00] Hola.", FakePool(["acct-a"]), runner=ok_runner)
    assert text == "[00:00] Hi there."
    assert account == "acct-a"


def test_polish_transcript_rotates_on_failure():
    calls = []

    def flaky(cmd, **kw):
        calls.append(kw["env"]["CLAUDE_CONFIG_DIR"])
        code = 1 if len(calls) == 1 else 0
        return subprocess.CompletedProcess(cmd, code, stdout="ok", stderr="usage limit")

    pool = FakePool(["acct-a", "acct-b"])
    text, account = polish_transcript("hi", pool, runner=flaky)
    assert account == "acct-b"
    assert pool.cooled == ["acct-a"]


def test_polish_transcript_exhaustion_raises():
    def dead(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="limit")

    with pytest.raises(PolishError):
        polish_transcript("hi", FakePool(["acct-a"]), runner=dead)
