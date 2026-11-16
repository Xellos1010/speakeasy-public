"""Unit tests for speakeasy.live_translate: streaming endpoint detector,
ordered worker-queue draining, markdown append/flush, and SIGINT drain
logic. No model download, no real mic/audio hardware, no sherpa_onnx import.
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from speakeasy.live_translate import (
    SegmenterConfig,
    StreamingSegmenter,
    SegmentWorker,
    TranscriptLine,
    format_hms,
    render_header,
    init_output,
    append_line,
    append_footer,
    run_polish_at_end,
    build_arg_parser,
    frames_from_samples,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _tone(rate, seconds, amplitude=0.5):
    n = int(rate * seconds)
    t = np.arange(n) / rate
    return (amplitude * np.sin(2 * np.pi * 5 * t)).astype(np.float32)


def _silence(rate, seconds):
    return np.zeros(int(rate * seconds), dtype=np.float32)


def _feed_all(seg, frames):
    """Feed a list of frames into a segmenter, collecting every closed
    segment (in order) along with the frame index it closed on."""
    closed = []
    for frame in frames:
        out = seg.feed(frame)
        if out is not None:
            closed.append(out)
    return closed


# --------------------------------------------------------------------------
# StreamingSegmenter state machine
# --------------------------------------------------------------------------

def test_segmenter_closes_after_trailing_silence_once_min_dur_reached():
    rate = 1000
    cfg = SegmenterConfig(sample_rate=rate, min_dur=1.0, max_dur=15.0, silence_hold_s=0.75)
    seg = StreamingSegmenter(cfg)
    # 0.3s speech, then silence fed in small 0.1s frames until it closes.
    frames = [_tone(rate, 0.3)] + [_silence(rate, 0.1)] * 20
    closed = _feed_all(seg, frames)
    assert len(closed) == 1
    dur = len(closed[0]) / rate
    # Should close once seg_dur>=min_dur(1.0) AND trailing silence>=0.75s,
    # i.e. around 0.3 + 0.75 = 1.05s, well before all 20 silence frames used.
    assert 1.0 <= dur <= 1.2


def test_segmenter_never_closes_on_silence_without_prior_speech():
    rate = 1000
    cfg = SegmenterConfig(sample_rate=rate, min_dur=1.0, max_dur=3.0, silence_hold_s=0.5)
    seg = StreamingSegmenter(cfg)
    # Pure silence for just under max_dur: must not close on the silence-hold
    # path (no speech has ever occurred), only the max_dur force-close fires.
    frames = [_silence(rate, 0.2)] * 14  # 2.8s total, < 3.0s max_dur
    closed = _feed_all(seg, frames)
    assert closed == []


def test_segmenter_force_closes_pure_silence_at_max_dur():
    rate = 1000
    cfg = SegmenterConfig(sample_rate=rate, min_dur=1.0, max_dur=3.0, silence_hold_s=0.5)
    seg = StreamingSegmenter(cfg)
    frames = [_silence(rate, 0.2)] * 16  # 3.2s total, crosses max_dur=3.0
    closed = _feed_all(seg, frames)
    assert len(closed) == 1
    assert len(closed[0]) / rate >= 3.0


def test_segmenter_force_closes_continuous_speech_at_max_dur():
    rate = 1000
    cfg = SegmenterConfig(sample_rate=rate, min_dur=1.0, max_dur=2.0, silence_hold_s=0.5)
    seg = StreamingSegmenter(cfg)
    frames = [_tone(rate, 0.25)] * 10  # 2.5s continuous tone, no silence ever
    closed = _feed_all(seg, frames)
    assert len(closed) == 1
    dur = len(closed[0]) / rate
    assert 2.0 <= dur < 2.5
    # remaining tail (0.5s) should still be sitting in the buffer for next close
    assert seg.flush() is not None


def test_segmenter_min_dur_gate_delays_close_even_with_long_silence_hold_config():
    rate = 1000
    cfg = SegmenterConfig(sample_rate=rate, min_dur=2.0, max_dur=15.0, silence_hold_s=0.3)
    seg = StreamingSegmenter(cfg)
    # speech then silence long enough to satisfy hold at 0.3s, but total
    # duration only crosses min_dur=2.0 after more frames.
    frames = [_tone(rate, 0.5)] + [_silence(rate, 0.1)] * 20
    closed = _feed_all(seg, frames)
    assert len(closed) == 1
    dur = len(closed[0]) / rate
    assert dur >= 2.0


def test_segmenter_empty_frame_is_noop():
    seg = StreamingSegmenter(SegmenterConfig(sample_rate=1000))
    assert seg.feed(np.zeros(0, dtype=np.float32)) is None


def test_flush_returns_none_when_buffer_empty():
    seg = StreamingSegmenter(SegmenterConfig(sample_rate=1000))
    assert seg.flush() is None


def test_flush_discards_silence_only_tail():
    rate = 1000
    seg = StreamingSegmenter(SegmenterConfig(sample_rate=rate, min_dur=5.0, max_dur=15.0))
    seg.feed(_silence(rate, 0.4))
    assert seg.flush() is None


def test_flush_returns_pending_speech_segment_even_below_min_dur():
    rate = 1000
    seg = StreamingSegmenter(SegmenterConfig(sample_rate=rate, min_dur=5.0, max_dur=15.0))
    seg.feed(_tone(rate, 0.4))
    out = seg.flush()
    assert out is not None
    assert len(out) / rate == pytest.approx(0.4, abs=1e-6)


def test_flush_clears_state_so_segmenter_is_reusable():
    rate = 1000
    seg = StreamingSegmenter(SegmenterConfig(sample_rate=rate, min_dur=5.0, max_dur=15.0))
    seg.feed(_tone(rate, 0.4))
    seg.flush()
    assert seg.flush() is None  # nothing left after the first flush


# --------------------------------------------------------------------------
# frames_from_samples chunking helper
# --------------------------------------------------------------------------

def test_frames_from_samples_splits_into_fixed_size_chunks_with_remainder():
    samples = np.arange(25, dtype=np.float32)
    frames = list(frames_from_samples(samples, frame_size=10))
    assert [len(f) for f in frames] == [10, 10, 5]
    assert np.array_equal(np.concatenate(frames), samples)


# --------------------------------------------------------------------------
# SegmentWorker: ordered draining despite variable engine latency
# --------------------------------------------------------------------------

class _Result:
    def __init__(self, text):
        self.text = text


def test_worker_preserves_submission_order_despite_variable_latency():
    delays = {0: 0.05, 1: 0.0, 2: 0.02}
    results = []

    def transcribe_fn(samples):
        seq = int(samples[0])
        time.sleep(delays[seq])
        return _Result(f"text-{seq}")

    def on_result(seq, result, close_wall, engine_latency, lag):
        results.append((seq, result.text))

    worker = SegmentWorker(transcribe_fn, on_result)
    worker.start()
    for i in range(3):
        worker.submit(i, np.array([i], dtype=np.float32), time.monotonic(), datetime.now())
    worker.drain_and_stop(timeout=5)

    assert results == [(0, "text-0"), (1, "text-1"), (2, "text-2")]


def test_worker_drain_and_stop_waits_for_all_pending():
    processed = []

    def transcribe_fn(samples):
        time.sleep(0.02)
        return _Result("ok")

    def on_result(seq, result, close_wall, engine_latency, lag):
        processed.append(seq)

    worker = SegmentWorker(transcribe_fn, on_result)
    worker.start()
    for i in range(5):
        worker.submit(i, np.zeros(1, dtype=np.float32), time.monotonic(), datetime.now())
    worker.drain_and_stop(timeout=5)
    assert processed == [0, 1, 2, 3, 4]


def test_worker_error_does_not_kill_thread_or_skip_remaining_items():
    processed = []
    errors = []

    def transcribe_fn(samples):
        if samples[0] == 1:
            raise RuntimeError("boom")
        return _Result("ok")

    def on_result(seq, result, close_wall, engine_latency, lag):
        processed.append(seq)

    def on_error(seq, exc):
        errors.append((seq, str(exc)))

    worker = SegmentWorker(transcribe_fn, on_result, on_error=on_error)
    worker.start()
    for i in range(3):
        worker.submit(i, np.array([i], dtype=np.float32), time.monotonic(), datetime.now())
    worker.drain_and_stop(timeout=5)

    assert processed == [0, 2]
    assert errors == [(1, "boom")]


def test_worker_measures_engine_latency_and_e2e_lag():
    captured = {}

    def transcribe_fn(samples):
        time.sleep(0.03)
        return _Result("ok")

    def on_result(seq, result, close_wall, engine_latency, lag):
        captured["engine_latency"] = engine_latency
        captured["lag"] = lag

    worker = SegmentWorker(transcribe_fn, on_result)
    worker.start()
    close_time = time.monotonic()
    time.sleep(0.01)  # simulate queueing delay before the worker gets to it
    worker.submit(0, np.zeros(1, dtype=np.float32), close_time, datetime.now())
    worker.drain_and_stop(timeout=5)

    assert captured["engine_latency"] >= 0.03
    assert captured["lag"] >= 0.03  # lag includes engine latency + queue wait


# --------------------------------------------------------------------------
# Timestamp + markdown rendering
# --------------------------------------------------------------------------

def test_format_hms():
    assert format_hms(datetime(2026, 1, 1, 9, 5, 3)) == "09:05:03"


def test_transcript_line_render():
    line = TranscriptLine(start_time=datetime(2026, 1, 1, 14, 30, 0), text="hola",
                          engine_latency_s=0.1, lag_s=0.2)
    assert line.render() == "[14:30:00] hola"


def test_render_header_contains_required_fields():
    header = render_header("translate", "sherpa-onnx-whisper-small",
                           "microphone (device default)",
                           when=datetime(2026, 1, 1, 9, 0, 0))
    assert "Mode: translate" in header
    assert "Model: sherpa-onnx-whisper-small" in header
    assert "2026-01-01T09:00:00" in header
    assert "speaker labels not available" in header


# --------------------------------------------------------------------------
# Output file: append + flush durability
# --------------------------------------------------------------------------

def test_init_output_then_append_line_persists_in_order(tmp_path):
    out = tmp_path / "session.md"
    init_output(out, "translate", "model-x", "microphone", when=datetime(2026, 1, 1))
    append_line(out, "[09:00:00] hola")
    append_line(out, "[09:00:05] adios")
    content = out.read_text()
    assert "Mode: translate" in content
    idx1 = content.index("[09:00:00] hola")
    idx2 = content.index("[09:00:05] adios")
    assert idx1 < idx2


def test_append_line_survives_reopen_between_calls(tmp_path):
    """Simulates crash-safety: each append_line call is a fully independent
    open/write/flush/close, so a crash between calls loses nothing already
    written."""
    out = tmp_path / "session.md"
    init_output(out, "transcribe", "model-y", "microphone", when=datetime(2026, 1, 1))
    append_line(out, "line one")
    text_after_first = out.read_text()
    assert "line one" in text_after_first
    append_line(out, "line two")
    assert "line one" in out.read_text()
    assert "line two" in out.read_text()


def test_append_footer_adds_stats_block(tmp_path):
    out = tmp_path / "session.md"
    init_output(out, "translate", "model-x", "microphone", when=datetime(2026, 1, 1))
    append_footer(out, {"count": 2, "engine_latencies": [0.5, 0.7], "lags": [0.6, 0.9]})
    content = out.read_text()
    assert "## Session stats" in content
    assert "segments: 2" in content


# --------------------------------------------------------------------------
# --polish-at-end (mocked polish function, never touches raw file)
# --------------------------------------------------------------------------

def test_run_polish_at_end_writes_separate_file_and_preserves_raw(tmp_path):
    out = tmp_path / "session.md"
    out.write_text("RAW CONTENT UNTOUCHED\n")
    lines = [
        TranscriptLine(start_time=datetime(2026, 1, 1, 9, 0, 0), text="hola",
                       engine_latency_s=0.1, lag_s=0.2),
    ]

    class FakePool:
        pass

    def fake_polish(raw_text, pool, **kwargs):
        assert "[09:00:00] hola" in raw_text
        return "[09:00:00] Hello.", "acct-a"

    polished_path = run_polish_at_end(lines, out, FakePool(), polish_fn=fake_polish)

    assert polished_path == out.with_suffix(".polished.md")
    assert out.read_text() == "RAW CONTENT UNTOUCHED\n"  # raw never overwritten
    assert "Hello." in polished_path.read_text()


def test_run_polish_at_end_returns_none_and_warns_on_failure(tmp_path, capsys):
    out = tmp_path / "session.md"
    out.write_text("RAW\n")
    lines = [TranscriptLine(start_time=datetime(2026, 1, 1), text="x",
                            engine_latency_s=0.0, lag_s=0.0)]

    class FakePool:
        pass

    def failing_polish(raw_text, pool, **kwargs):
        from speakeasy.translate_file import PolishError
        raise PolishError("no eligible account")

    result = run_polish_at_end(lines, out, FakePool(), polish_fn=failing_polish)
    assert result is None
    assert out.read_text() == "RAW\n"
    captured = capsys.readouterr()
    assert "warning" in captured.out.lower() or "warning" in captured.err.lower()


def test_run_polish_at_end_noop_when_pool_is_none(tmp_path):
    out = tmp_path / "session.md"
    out.write_text("RAW\n")
    result = run_polish_at_end([], out, None, polish_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")))
    assert result is None


# --------------------------------------------------------------------------
# CLI argument parsing (no engine construction, no model download)
# --------------------------------------------------------------------------

def test_arg_parser_defaults():
    parser = build_arg_parser()
    args = parser.parse_args([])
    assert args.mode == "translate"
    assert args.polish_at_end is False
    assert args.device is None
    assert args.input_wav is None
    assert args.stats is False


def test_arg_parser_accepts_all_flags(tmp_path):
    parser = build_arg_parser()
    wav = tmp_path / "in.wav"
    out = tmp_path / "out.md"
    args = parser.parse_args([
        "--mode", "transcribe", "--out", str(out), "--polish-at-end",
        "--device", "3", "--input-wav", str(wav), "--stats",
    ])
    assert args.mode == "transcribe"
    assert args.out == out
    assert args.polish_at_end is True
    assert args.device == 3
    assert args.input_wav == wav
    assert args.stats is True
