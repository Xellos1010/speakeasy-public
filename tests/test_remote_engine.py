import json
import socket

import numpy as np
import pytest

from speakeasy.engine import TranscriptionResult
from speakeasy.engine.remote import RemoteFallbackEngine, estimate_local_p95_ms
from speakeasy.ranking import ReachabilityCache, SELF


# --- estimate_local_p95_ms (C1/Q2) --------------------------------------------

MEASURED = {"3s_clip": 216.1, "5s_clip": 324.4, "8s_clip": 484.9}  # this box's real p95s

def test_estimate_matches_measured_points_closely():
    # not exact (it's a fit across all 3 points, not an exact interpolation), but close.
    assert estimate_local_p95_ms(MEASURED, 5.0) == pytest.approx(324.4, abs=15)

def test_estimate_extrapolates_beyond_longest_band_rather_than_clamping():
    at_8s = estimate_local_p95_ms(MEASURED, 8.0)
    at_15s = estimate_local_p95_ms(MEASURED, 15.0)
    assert at_15s > at_8s * 1.5   # genuinely extrapolated upward, not clamped to the 8s value

def test_estimate_single_point_degenerates_to_that_value():
    assert estimate_local_p95_ms({"5s_clip": 300.0}, 12.0) == 300.0

def test_estimate_empty_raises():
    with pytest.raises(ValueError):
        estimate_local_p95_ms({}, 5.0)


# --- RemoteFallbackEngine ------------------------------------------------------

class LocalStub:
    def __init__(self):
        self.calls = 0
    def transcribe(self, samples, sample_rate=16000):
        return self.transcribe_ex(samples, sample_rate).text
    def transcribe_ex(self, samples, sample_rate=16000):
        self.calls += 1
        return TranscriptionResult(text="local result", avg_logprob=-0.1)


class FakeResponse:
    def __init__(self, status, payload: dict):
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")
    def read(self):
        return self._body


class FakeConnection:
    """Stand-in for http.client.HTTPConnection. `behavior` decides what happens."""
    def __init__(self, host, port, timeout, behavior=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.behavior = behavior
        self._response = None
        self._sent_utterance_id = None

    def request(self, method, path, body=None, headers=None):
        self._sent_utterance_id = headers.get("X-Speakeasy-Utterance-Id")
        outcome = self.behavior(self._sent_utterance_id)
        if isinstance(outcome, Exception):
            raise outcome
        self._response = outcome

    def getresponse(self):
        return self._response

    def close(self):
        pass


def make_connection_cls(behavior):
    def factory(host, port, timeout=None):
        return FakeConnection(host, port, timeout, behavior=behavior)
    return factory


def _engine_targeting_remote(behavior, ranking=("linux", SELF)):
    local = LocalStub()
    reach = ReachabilityCache()
    reach.set("linux", True)
    engine = RemoteFallbackEngine(
        local_engine=local,
        ranking=list(ranking),
        reachability_cache=reach,
        host_addresses={"linux": "192.0.2.3:7420"},
        local_settings_record={"measured_p95_ms": MEASURED},
        connection_cls=make_connection_cls(behavior),
    )
    return engine, local


def test_self_preferred_calls_local_directly_no_connection_attempted():
    local = LocalStub()
    reach = ReachabilityCache()  # nothing reachable -> self
    engine = RemoteFallbackEngine(
        local_engine=local, ranking=["linux", SELF], reachability_cache=reach,
        host_addresses={"linux": "192.0.2.3:7420"},
        local_settings_record={"measured_p95_ms": MEASURED},
        connection_cls=lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not connect")),
    )
    result = engine.transcribe_ex(np.zeros(16000, dtype=np.float32))
    assert result.text == "local result"
    assert local.calls == 1

def test_remote_success_returns_remote_result_not_local():
    def behavior(utterance_id):
        return FakeResponse(200, {"utterance_id": utterance_id, "text": "remote result",
                                   "avg_logprob": -0.2})
    engine, local = _engine_targeting_remote(behavior)
    result = engine.transcribe_ex(np.zeros(16000, dtype=np.float32))
    assert result.text == "remote result"
    assert result.avg_logprob == pytest.approx(-0.2)
    assert local.calls == 0

def test_connection_refused_falls_back_to_local_r5_1_case_a():
    def behavior(utterance_id):
        return ConnectionRefusedError("refused")
    engine, local = _engine_targeting_remote(behavior)
    result = engine.transcribe_ex(np.zeros(16000, dtype=np.float32))
    assert result.text == "local result"
    assert local.calls == 1

def test_timeout_falls_back_to_local_r5_1_case_b():
    def behavior(utterance_id):
        return socket.timeout("timed out")
    engine, local = _engine_targeting_remote(behavior)
    result = engine.transcribe_ex(np.zeros(16000, dtype=np.float32))
    assert result.text == "local result"
    assert local.calls == 1

def test_protocol_version_mismatch_response_falls_back_r4_2():
    def behavior(utterance_id):
        return FakeResponse(409, {"error": "protocol_version mismatch"})
    engine, local = _engine_targeting_remote(behavior)
    result = engine.transcribe_ex(np.zeros(16000, dtype=np.float32))
    assert result.text == "local result"
    assert local.calls == 1

def test_mismatched_utterance_id_in_response_falls_back_never_trusted():
    def behavior(utterance_id):
        return FakeResponse(200, {"utterance_id": "not-the-one-we-sent", "text": "sneaky"})
    engine, local = _engine_targeting_remote(behavior)
    result = engine.transcribe_ex(np.zeros(16000, dtype=np.float32))
    assert result.text == "local result"   # never used the mismatched response
    assert local.calls == 1

def test_response_missing_text_field_falls_back_never_injects_empty():
    """SF2: a 200 response with no 'text' key must not silently become an empty
    transcription -- that's worse than falling back, since it looks like the
    dictation succeeded and produced nothing."""
    def behavior(utterance_id):
        return FakeResponse(200, {"utterance_id": utterance_id})  # no "text" key at all
    engine, local = _engine_targeting_remote(behavior)
    result = engine.transcribe_ex(np.zeros(16000, dtype=np.float32))
    assert result.text == "local result"
    assert local.calls == 1

def test_malformed_json_falls_back():
    class BadResponse:
        status = 200
        def read(self):
            return b"not json"
    def behavior(utterance_id):
        return BadResponse()
    engine, local = _engine_targeting_remote(behavior)
    result = engine.transcribe_ex(np.zeros(16000, dtype=np.float32))
    assert result.text == "local result"
    assert local.calls == 1

def test_no_configured_address_for_target_falls_back():
    local = LocalStub()
    reach = ReachabilityCache()
    reach.set("pi", True)
    engine = RemoteFallbackEngine(
        local_engine=local, ranking=["pi", SELF], reachability_cache=reach,
        host_addresses={},  # pi has no address configured
        local_settings_record={"measured_p95_ms": MEASURED},
        connection_cls=make_connection_cls(lambda uid: FakeResponse(200, {})),
    )
    result = engine.transcribe_ex(np.zeros(16000, dtype=np.float32))
    assert result.text == "local result"
    assert local.calls == 1

def test_deadline_uses_p95_not_p50():
    """C1: the deadline must be derived from the measured p95, not p50 -- this test
    would pass even with a p50-based bug if p50==p95, so it deliberately uses a
    settings record where they differ."""
    local = LocalStub()
    reach = ReachabilityCache()
    reach.set("linux", True)
    engine = RemoteFallbackEngine(
        local_engine=local, ranking=["linux", SELF], reachability_cache=reach,
        host_addresses={"linux": "192.0.2.3:7420"},
        local_settings_record={"measured_p95_ms": {"5s_clip": 1000.0}},  # p95 = 1000ms
        connection_cls=make_connection_cls(lambda uid: FakeResponse(200, {})),
    )
    deadline = engine._deadline_for(5.0)
    assert deadline == pytest.approx(1.5 * 1000.0 / 1000.0)  # 1.5 * p95_s, not something smaller
