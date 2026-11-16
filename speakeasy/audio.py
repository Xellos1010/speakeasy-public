import threading
import numpy as np

class Recorder:
    def __init__(self, samplerate: int = 16000, device: int | None = None,
                 stream_factory=None):
        self.samplerate = samplerate
        self._device = device
        self._factory = stream_factory or self._default_factory
        self._lock = threading.Lock()
        self._chunks: list[np.ndarray] = []
        self._level = 0.0
        self._stream = None

    def _default_factory(self, callback):
        import sounddevice as sd
        return sd.InputStream(samplerate=self.samplerate, channels=1,
                              dtype="float32", device=self._device,
                              callback=callback)

    def _callback(self, indata, frames, time_info, status):
        mono = np.asarray(indata, dtype=np.float32).reshape(-1)
        with self._lock:
            self._chunks.append(mono.copy())
            self._level = float(np.sqrt(np.mean(mono ** 2)))

    def start(self) -> None:
        with self._lock:
            self._chunks = []
            self._level = 0.0
        self._stream = self._factory(self._callback)
        self._stream.start()

    def snapshot(self) -> np.ndarray:
        with self._lock:
            if not self._chunks:
                return np.zeros(0, dtype=np.float32)
            return np.concatenate(self._chunks)

    def stop(self) -> np.ndarray:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        return self.snapshot()

    @property
    def level(self) -> float:
        return self._level
