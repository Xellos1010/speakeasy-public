import numpy as np
from speakeasy.audio import Recorder

class FakeStream:
    def __init__(self, callback): self.callback = callback; self.active = False
    def start(self): self.active = True
    def stop(self): self.active = False
    def close(self): pass

def test_capture_and_snapshot():
    holder = {}
    def factory(callback):
        holder["s"] = FakeStream(callback); return holder["s"]
    r = Recorder(stream_factory=factory)
    r.start()
    block = np.ones((1600, 1), dtype=np.float32) * 0.5
    holder["s"].callback(block, 1600, None, None)
    holder["s"].callback(block, 1600, None, None)
    assert len(r.snapshot()) == 3200
    assert 0.4 < r.level <= 0.6
    out = r.stop()
    assert len(out) == 3200 and out.dtype == np.float32

def test_stop_resets():
    holder = {}
    r = Recorder(stream_factory=lambda cb: holder.setdefault("s", FakeStream(cb)))
    r.start()
    holder["s"].callback(np.zeros((160, 1), np.float32), 160, None, None)
    r.stop()
    r.start()
    assert len(r.snapshot()) == 0
