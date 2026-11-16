"""Unit tests for the whisper-translate engine (ES->EN) + the
TranscriptionResult confidence contract.

sherpa_onnx.OfflineRecognizer is stubbed throughout -- these tests never
touch the real ONNX runtime or download a model. Confirmed separately
(against the already-downloaded parakeet int8 model, sherpa-onnx 1.13.4)
that real OfflineRecognizerResult objects expose ``ys_log_probs`` as a
list[float] of per-token log-probs; ``ys_probs`` is NOT exposed as an
attribute. transcribe_ex relies on ys_log_probs only, with a documented
None fallback.
"""
import math
from pathlib import Path

import numpy as np
import pytest

import speakeasy.engine as engine_pkg
import speakeasy.engine.parakeet as parakeet_mod
import speakeasy.engine.whisper_onnx as whisper_onnx
from speakeasy.engine import TranscriptionResult, create_engine
from speakeasy.engine.download import MODELS


class _FakeResult:
    """Mimics sherpa_onnx's OfflineRecognizerResult just enough for tests."""

    def __init__(self, text, ys_log_probs=None, has_ys_log_probs=True):
        self.text = text
        if has_ys_log_probs:
            self.ys_log_probs = ys_log_probs


class _FakeStream:
    def __init__(self, result):
        self.result = result

    def accept_waveform(self, sample_rate, samples):
        pass


def _make_fake_recognizer(result, factory_name="from_whisper"):
    """Returns (FakeRecognizerClass, captured) where captured['kwargs']
    records whatever kwargs the engine passed to the sherpa_onnx factory
    classmethod (from_whisper / from_transducer)."""
    captured = {}

    def _factory(cls, **kwargs):
        captured["kwargs"] = kwargs
        return cls()

    FakeRecognizer = type("FakeRecognizer", (), {
        factory_name: classmethod(_factory),
        "create_stream": lambda self: _FakeStream(result),
        "decode_stream": lambda self, stream: None,
    })
    return FakeRecognizer, captured


@pytest.fixture
def whisper_model_dir(tmp_path):
    d = tmp_path / "sherpa-onnx-whisper-small"
    d.mkdir()
    (d / "small-encoder.int8.onnx").touch()
    (d / "small-decoder.int8.onnx").touch()
    (d / "small-tokens.txt").touch()
    return d


@pytest.fixture
def parakeet_model_dir(tmp_path):
    d = tmp_path / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8"
    d.mkdir()
    (d / "encoder.int8.onnx").touch()
    (d / "decoder.int8.onnx").touch()
    (d / "joiner.int8.onnx").touch()
    (d / "tokens.txt").touch()
    return d


# --- MODELS entry -----------------------------------------------------

def test_models_entry_whisper_translate_points_at_multilingual_small():
    spec = MODELS["whisper-translate"]
    assert spec["dirname"] == "sherpa-onnx-whisper-small"
    assert spec["url"].endswith("/sherpa-onnx-whisper-small.tar.bz2")
    # must NOT collide with the existing English-only "whisper" entry
    assert spec["dirname"] != MODELS["whisper"]["dirname"]
    assert spec["url"] != MODELS["whisper"]["url"]


# --- WhisperEngine task/language parameterization ----------------------

def test_whisper_engine_defaults_preserve_old_behavior(monkeypatch, whisper_model_dir):
    FakeRecognizer, captured = _make_fake_recognizer(_FakeResult("hi"))
    monkeypatch.setattr(whisper_onnx.sherpa_onnx, "OfflineRecognizer", FakeRecognizer)

    whisper_onnx.WhisperEngine(whisper_model_dir)

    assert captured["kwargs"]["language"] == "en"
    assert captured["kwargs"]["task"] == "transcribe"


def test_whisper_engine_accepts_translate_language_task(monkeypatch, whisper_model_dir):
    FakeRecognizer, captured = _make_fake_recognizer(_FakeResult("hola"))
    monkeypatch.setattr(whisper_onnx.sherpa_onnx, "OfflineRecognizer", FakeRecognizer)

    whisper_onnx.WhisperEngine(whisper_model_dir, language="es", task="translate")

    assert captured["kwargs"]["language"] == "es"
    assert captured["kwargs"]["task"] == "translate"


def test_create_engine_whisper_translate_wires_es_translate(monkeypatch, whisper_model_dir):
    monkeypatch.setattr(engine_pkg, "ensure_model",
                         lambda name, models_dir: whisper_model_dir)
    FakeRecognizer, captured = _make_fake_recognizer(_FakeResult("hola"))
    monkeypatch.setattr(whisper_onnx.sherpa_onnx, "OfflineRecognizer", FakeRecognizer)

    eng = create_engine("whisper-translate", Path("/unused"))

    assert isinstance(eng, whisper_onnx.WhisperEngine)
    assert captured["kwargs"]["language"] == "es"
    assert captured["kwargs"]["task"] == "translate"


def test_create_engine_whisper_still_transcribes_english(monkeypatch, whisper_model_dir):
    monkeypatch.setattr(engine_pkg, "ensure_model",
                         lambda name, models_dir: whisper_model_dir)
    FakeRecognizer, captured = _make_fake_recognizer(_FakeResult("hi"))
    monkeypatch.setattr(whisper_onnx.sherpa_onnx, "OfflineRecognizer", FakeRecognizer)

    eng = create_engine("whisper", Path("/unused"))

    assert isinstance(eng, whisper_onnx.WhisperEngine)
    assert captured["kwargs"]["language"] == "en"
    assert captured["kwargs"]["task"] == "transcribe"


def test_create_engine_unknown_name_still_raises(monkeypatch, whisper_model_dir):
    monkeypatch.setattr(engine_pkg, "ensure_model",
                         lambda name, models_dir: whisper_model_dir)
    with pytest.raises(ValueError):
        create_engine("nonexistent-engine", Path("/unused"))


# --- transcribe() stays byte-for-byte compatible -----------------------

def test_transcribe_still_returns_plain_str(monkeypatch, whisper_model_dir):
    FakeRecognizer, _ = _make_fake_recognizer(_FakeResult("plain text"))
    monkeypatch.setattr(whisper_onnx.sherpa_onnx, "OfflineRecognizer", FakeRecognizer)
    eng = whisper_onnx.WhisperEngine(whisper_model_dir)

    text = eng.transcribe(np.zeros(16000, dtype=np.float32))

    assert text == "plain text"
    assert type(text) is str


# --- transcribe_ex() / TranscriptionResult confidence contract ---------

def test_transcribe_ex_returns_mean_logprob(monkeypatch, whisper_model_dir):
    log_probs = [-0.1, -0.2, -0.3]
    FakeRecognizer, _ = _make_fake_recognizer(
        _FakeResult("hello world", ys_log_probs=log_probs))
    monkeypatch.setattr(whisper_onnx.sherpa_onnx, "OfflineRecognizer", FakeRecognizer)
    eng = whisper_onnx.WhisperEngine(whisper_model_dir)

    result = eng.transcribe_ex(np.zeros(16000, dtype=np.float32))

    assert isinstance(result, TranscriptionResult)
    assert result.text == "hello world"
    assert math.isclose(result.avg_logprob, sum(log_probs) / len(log_probs))


def test_transcribe_ex_none_when_attribute_missing(monkeypatch, whisper_model_dir):
    FakeRecognizer, _ = _make_fake_recognizer(
        _FakeResult("hello", has_ys_log_probs=False))
    monkeypatch.setattr(whisper_onnx.sherpa_onnx, "OfflineRecognizer", FakeRecognizer)
    eng = whisper_onnx.WhisperEngine(whisper_model_dir)

    result = eng.transcribe_ex(np.zeros(16000, dtype=np.float32))

    assert result.text == "hello"
    assert result.avg_logprob is None


def test_transcribe_ex_none_when_logprobs_empty(monkeypatch, whisper_model_dir):
    FakeRecognizer, _ = _make_fake_recognizer(
        _FakeResult("hello", ys_log_probs=[]))
    monkeypatch.setattr(whisper_onnx.sherpa_onnx, "OfflineRecognizer", FakeRecognizer)
    eng = whisper_onnx.WhisperEngine(whisper_model_dir)

    result = eng.transcribe_ex(np.zeros(16000, dtype=np.float32))

    assert result.avg_logprob is None


def test_parakeet_transcribe_ex_returns_mean_logprob(monkeypatch, parakeet_model_dir):
    log_probs = [-0.5, -1.5]
    FakeRecognizer, _ = _make_fake_recognizer(
        _FakeResult("quick fox", ys_log_probs=log_probs),
        factory_name="from_transducer")
    monkeypatch.setattr(parakeet_mod.sherpa_onnx, "OfflineRecognizer", FakeRecognizer)
    eng = parakeet_mod.ParakeetEngine(parakeet_model_dir)

    result = eng.transcribe_ex(np.zeros(16000, dtype=np.float32))

    assert isinstance(result, TranscriptionResult)
    assert result.text == "quick fox"
    assert math.isclose(result.avg_logprob, sum(log_probs) / len(log_probs))


# --- optional integration test, guarded --------------------------------

@pytest.mark.integration
def test_whisper_translate_real_model_transcribes_spanish_to_english():
    import re
    import wave
    from speakeasy.engine.download import ensure_model

    models_dir = Path.home() / ".speakeasy" / "models"
    model_dir = models_dir / MODELS["whisper-translate"]["dirname"]
    if not model_dir.exists():
        pytest.skip("multilingual whisper-small model not downloaded locally")

    eng = create_engine("whisper-translate", models_dir)
    with wave.open("tests/assets/sample.wav", "rb") as w:
        raw = w.readframes(w.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    text = eng.transcribe(samples)
    assert text.strip() != ""
