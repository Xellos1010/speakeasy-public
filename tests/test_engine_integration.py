import re, wave
from pathlib import Path
import numpy as np
import pytest
from speakeasy.engine import create_engine
from speakeasy.engine.download import ensure_model
from speakeasy.engine.parakeet import ParakeetEngine, HotwordsUnavailable

EXPECTED = "the quick brown fox jumps over the lazy dog"

def load_wav(p: Path) -> np.ndarray:
    with wave.open(str(p), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

def norm(s: str) -> str:
    return re.sub(r"[^a-z ]", "", s.lower()).strip()

@pytest.mark.integration
def test_parakeet_transcribes_sample():
    eng = create_engine("parakeet", Path.home() / ".speakeasy" / "models")
    text = norm(eng.transcribe(load_wav(Path("tests/assets/sample.wav"))))
    missing = [w for w in EXPECTED.split() if w not in text]
    assert len(missing) <= 1, f"missing {missing} in {text!r}"

@pytest.mark.integration
def test_parakeet_hotwords_path_transcribes_or_skips_cleanly():
    # T6 (docs/superpowers/specs/2026-07-18-voice-training-environment.md
    # §1.1, §5, §10 open question 1): hotwords is an experimental, gated,
    # opt-in decoding path. This does not assert an accuracy delta (§1.1's
    # issue #3267 caveat means modified_beam_search is not yet trusted as a
    # default) -- it only asserts the additive param path wires through
    # sherpa-onnx without raising and still returns a transcript, OR skips
    # cleanly with the documented reason when bpe_vocab is unavailable.
    model_dir = ensure_model("parakeet", Path.home() / ".speakeasy" / "models")
    try:
        eng = ParakeetEngine(model_dir, hotwords=[("quick brown fox", 2.5)])
    except HotwordsUnavailable as e:
        pytest.skip(
            "hotwords path unavailable for the bundled "
            "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8 model: bpe_vocab "
            "cannot be derived from the shipped encoder/decoder/joiner "
            ".onnx + tokens.txt alone -- PR #3077's "
            "scripts/nemo/generate_bpe_vocab.py utility requires "
            "nemo_toolkit (NVIDIA NeMo) plus the original .nemo checkpoint "
            "(or a HuggingFace/NGC download of "
            "nvidia/parakeet-tdt-0.6b-v2) to read the SentencePiece "
            "tokenizer's per-token scores, and neither ships with this "
            f"int8 ONNX export. ({e})"
        )
    text = eng.transcribe(load_wav(Path("tests/assets/sample.wav")))
    assert text.strip() != ""
