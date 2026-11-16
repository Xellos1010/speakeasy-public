from pathlib import Path
import numpy as np
import sherpa_onnx

from speakeasy.engine import TranscriptionResult

class WhisperEngine:
    def __init__(self, model_dir: Path, language: str = "en",
                 task: str = "transcribe", num_threads: int = 4):
        d = Path(model_dir)
        try:
            encoder = next(d.glob("*int8*encoder*.onnx"))
        except StopIteration:
            encoder = next(d.glob("*encoder*.onnx"))
        try:
            decoder = next(d.glob("*int8*decoder*.onnx"))
        except StopIteration:
            decoder = next(d.glob("*decoder*.onnx"))
        self._rec = sherpa_onnx.OfflineRecognizer.from_whisper(
            encoder=str(encoder),
            decoder=str(decoder),
            tokens=str(next(d.glob("*tokens*.txt"))),
            num_threads=num_threads, language=language, task=task)

    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> str:
        s = self._rec.create_stream()
        s.accept_waveform(sample_rate, samples)
        self._rec.decode_stream(s)
        return s.result.text.strip()

    def transcribe_ex(self, samples: np.ndarray, sample_rate: int = 16000) -> TranscriptionResult:
        """Like transcribe(), but also surfaces a confidence signal.

        avg_logprob is the mean of sherpa-onnx's per-token
        ``result.ys_log_probs`` (verified present on the installed
        sherpa-onnx 1.13.4 OfflineRecognizerResult for both whisper and
        transducer recognizers). If a given sherpa-onnx build/model doesn't
        expose that attribute (or returns an empty list), avg_logprob is
        None -- it is never fabricated.
        """
        s = self._rec.create_stream()
        s.accept_waveform(sample_rate, samples)
        self._rec.decode_stream(s)
        result = s.result
        text = result.text.strip()
        log_probs = getattr(result, "ys_log_probs", None)
        avg_logprob = (sum(log_probs) / len(log_probs)) if log_probs else None
        return TranscriptionResult(text=text, avg_logprob=avg_logprob)
