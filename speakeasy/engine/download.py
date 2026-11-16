import tarfile, tempfile, urllib.request
from pathlib import Path

_BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"

MODELS = {
    "parakeet": {
        "dirname": "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8",
        "url": _BASE + "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2",
    },
    "whisper": {
        "dirname": "sherpa-onnx-whisper-small.en",
        "url": _BASE + "sherpa-onnx-whisper-small.en.tar.bz2",
    },
    # Multilingual small model (as opposed to "whisper"'s English-only
    # small.en) -- needed for task="translate" (X->English) since Whisper's
    # translate task requires a multilingual checkpoint. Verified archive
    # contents via a partial-range fetch + `tar -tvf` listing (no local
    # sherpa-onnx docs page consulted): the tarball root dir is
    # "sherpa-onnx-whisper-small/" containing (among others)
    # small-encoder.int8.onnx, small-decoder.int8.onnx, small-tokens.txt --
    # matching the glob patterns WhisperEngine already uses.
    "whisper-translate": {
        "dirname": "sherpa-onnx-whisper-small",
        "url": _BASE + "sherpa-onnx-whisper-small.tar.bz2",
    },
}

def ensure_model(name: str, models_dir: Path,
                 fetch=urllib.request.urlretrieve) -> Path:
    spec = MODELS[name]
    target = models_dir / spec["dirname"]
    if target.exists():
        return target
    models_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".tar.bz2", delete=False) as tmp:
        pass
    fetch(spec["url"], tmp.name)
    with tarfile.open(tmp.name, "r:bz2") as t:
        t.extractall(models_dir, filter="data")
    if not target.exists():
        raise FileNotFoundError(f"archive did not contain {spec['dirname']}")
    return target
