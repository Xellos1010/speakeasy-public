from pathlib import Path
from typing import NamedTuple, Protocol
import numpy as np
from speakeasy.engine.download import ensure_model


class TranscriptionResult(NamedTuple):
    text: str
    avg_logprob: float | None  # mean per-token log-prob; None when engine can't provide it


class Engine(Protocol):
    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> str: ...
    def transcribe_ex(self, samples: np.ndarray, sample_rate: int = 16000) -> TranscriptionResult: ...

def create_engine(name: str, models_dir: Path) -> Engine:
    model_dir = ensure_model(name, models_dir)
    if name == "parakeet":
        from speakeasy.engine.parakeet import ParakeetEngine
        return ParakeetEngine(model_dir)
    if name == "whisper":
        from speakeasy.engine.whisper_onnx import WhisperEngine
        return WhisperEngine(model_dir)
    if name == "whisper-translate":
        from speakeasy.engine.whisper_onnx import WhisperEngine
        return WhisperEngine(model_dir, language="es", task="translate")
    raise ValueError(f"unknown engine {name!r}")
