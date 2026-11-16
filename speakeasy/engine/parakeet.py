import tempfile
from pathlib import Path
import numpy as np
import sherpa_onnx

from speakeasy.engine import TranscriptionResult

# Conventional filenames a bpe_vocab could be vendored under, alongside the
# encoder/decoder/joiner/tokens.txt already shipped in a model_dir.
_BPE_VOCAB_CANDIDATES = ("bpe.vocab", "bpe_vocab.txt")


class HotwordsUnavailable(Exception):
    """Raised when hotwords/contextual biasing is requested but the assets
    sherpa-onnx's modified_beam_search + bpe_vocab path needs (per PR #3077,
    https://github.com/k2-fsa/sherpa-onnx/pull/3077) are not available for
    this engine's model bundle.

    Investigated for TSK-SPK-TRAIN-T6 / spec §10 open question 1: for the
    bundled ``sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8`` model this is the
    expected outcome, not a bug. PR #3077's own extraction utility
    (``scripts/nemo/generate_bpe_vocab.py``) loads the SentencePiece
    tokenizer via ``nemo_toolkit`` (NVIDIA NeMo) from either the original
    ``.nemo`` checkpoint or a HuggingFace/NGC download of
    ``nvidia/parakeet-tdt-0.6b-v2``, then dumps each token's SentencePiece
    score to a two-column ``token<TAB>log_prob`` file. Neither the ``.nemo``
    checkpoint nor ``nemo_toolkit`` ships with the quantized ONNX export at
    ``~/.speakeasy/models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8`` — that
    directory contains only ``encoder.int8.onnx``, ``decoder.int8.onnx``,
    ``joiner.int8.onnx``, and ``tokens.txt`` (a ``symbol integer_id`` map,
    NOT the ``token log_prob`` bpe_vocab format). So bpe_vocab cannot be
    derived from what's bundled today without adding ``nemo_toolkit`` (a
    large, GPU-oriented dependency) and fetching the original checkpoint —
    out of scope for this additive, opt-in path. Pass ``bpe_vocab=``
    explicitly if one has been generated out-of-band.
    """


class ParakeetEngine:
    def __init__(
        self,
        model_dir: Path,
        num_threads: int = 4,
        decoding_method: str = "greedy_search",
        hotwords: list[tuple[str, float]] | None = None,
        hotwords_score: float = 1.5,
        bpe_vocab: str | Path | None = None,
    ):
        d = Path(model_dir)
        enc = next(d.glob("encoder*.onnx"))
        dec = next(d.glob("decoder*.onnx"))
        joi = next(d.glob("joiner*.onnx"))

        kwargs = dict(
            encoder=str(enc), decoder=str(dec), joiner=str(joi),
            tokens=str(d / "tokens.txt"),
            num_threads=num_threads, model_type="nemo_transducer",
            decoding_method=decoding_method,
        )

        # Keep the temp hotwords file path alive on self for the engine's
        # lifetime (sherpa_onnx only needs the path, but the file must not
        # be deleted before the recognizer is constructed).
        self._hotwords_file = None

        if hotwords:
            resolved_bpe_vocab = self._resolve_bpe_vocab(d, bpe_vocab)
            if resolved_bpe_vocab is None:
                raise HotwordsUnavailable(
                    "hotwords requested but no bpe_vocab is available for "
                    f"model_dir={d}. See speakeasy.engine.parakeet."
                    "HotwordsUnavailable's docstring for why the bundled "
                    "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8 model cannot "
                    "supply one, or pass bpe_vocab= explicitly."
                )
            self._hotwords_file = self._write_hotwords_file(hotwords)
            kwargs.update(
                decoding_method="modified_beam_search",
                hotwords_file=self._hotwords_file,
                hotwords_score=hotwords_score,
                modeling_unit="bpe",
                bpe_vocab=str(resolved_bpe_vocab),
            )

        self._rec = sherpa_onnx.OfflineRecognizer.from_transducer(**kwargs)

    @staticmethod
    def _resolve_bpe_vocab(model_dir: Path, bpe_vocab: str | Path | None):
        if bpe_vocab:
            p = Path(bpe_vocab)
            return p if p.exists() else None
        for name in _BPE_VOCAB_CANDIDATES:
            p = model_dir / name
            if p.exists():
                return p
        return None

    @staticmethod
    def _write_hotwords_file(hotwords: list[tuple[str, float]]) -> str:
        fd, path = tempfile.mkstemp(suffix=".txt", prefix="speakeasy-hotwords-")
        with open(fd, "w", encoding="utf-8") as f:
            for phrase, score in hotwords:
                f.write(f"{phrase} :{score}\n")
        return path

    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> str:
        s = self._rec.create_stream()
        s.accept_waveform(sample_rate, samples)
        self._rec.decode_stream(s)
        return s.result.text.strip()

    def transcribe_ex(self, samples: np.ndarray, sample_rate: int = 16000) -> TranscriptionResult:
        """Like transcribe(), but also surfaces a confidence signal (see
        WhisperEngine.transcribe_ex for the ys_log_probs contract this
        implements identically)."""
        s = self._rec.create_stream()
        s.accept_waveform(sample_rate, samples)
        self._rec.decode_stream(s)
        result = s.result
        text = result.text.strip()
        log_probs = getattr(result, "ys_log_probs", None)
        avg_logprob = (sum(log_probs) / len(log_probs)) if log_probs else None
        return TranscriptionResult(text=text, avg_logprob=avg_logprob)
