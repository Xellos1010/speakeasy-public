import json
import socket
import threading
import time
from http.client import HTTPConnection

import numpy as np
import pytest

from speakeasy.engine import TranscriptionResult
from speakeasy.ranking import PeerIdentityCache
from speakeasy import service as service_mod
from speakeasy.service import (
    BindAddressUnavailable,
    DEFAULT_PORT,
    MAX_CONTENT_LENGTH,
    PROTOCOL_VERSION,
    resolve_tailscale_bind_address,
    run_server,
)


class StubEngine:
    def __init__(self, text="hello", avg_logprob=-0.3):
        self.calls = []
        self._text = text
        self._avg_logprob = avg_logprob

    def transcribe(self, samples, sample_rate=16000):
        return self.transcribe_ex(samples, sample_rate).text

    def transcribe_ex(self, samples, sample_rate=16000):
        self.calls.append((len(samples), sample_rate))
        return TranscriptionResult(text=self._text, avg_logprob=self._avg_logprob)


ENGINE_SETTINGS = {"host_id": "test-host", "name": "parakeet", "provider": "cpu",
                    "settings_updated": "2026-08-18"}


def _allow_all_cache():
    return PeerIdentityCache(refresh_fn=lambda: {"127.0.0.1": "test-peer"})

def _allow_none_cache():
    return PeerIdentityCache(refresh_fn=lambda: {})


class RunningServer:
    """Test-only server wrapper. HF2 (ADJ-SPK-023/027 reopened): a Mac run with
    pytest-randomly reproduced a raw ConnectionResetError inside resp.read() in
    ~half of randomized orderings, matching a teardown race between successive
    RunningServer instances. Two structural fixes, not a timing band-aid:
    (1) daemon_threads=True on the ThreadingHTTPServer -- without it, per-connection
    handler threads spawned by ThreadingMixIn are NOT daemon by default and can
    outlive close(), and (2) close() now actually joins the serve_forever thread
    (the original version called shutdown()+server_close() but never waited for the
    thread to actually exit, so the next test's server could start before this one's
    was fully torn down). port=0 (OS-assigned ephemeral, read back via
    server_address) was already correct and is unchanged.
    """

    def __init__(self, engine, peer_cache, port=0):
        self.engine = engine
        self.server = run_server(engine, ENGINE_SETTINGS, peer_cache, port=port,
                                  bind_resolver=lambda: "127.0.0.1")
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        time.sleep(0.05)

    @property
    def port(self):
        return self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=2.0)
        self.server.server_close()


def post_transcribe(port, samples: np.ndarray, utterance_id="u-1",
                     protocol_version=str(PROTOCOL_VERSION), sample_rate=16000,
                     content_length_override=None):
    body = samples.astype(np.float32).tobytes()
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"X-Speakeasy-Sample-Rate": str(sample_rate)}
    if utterance_id is not None:
        headers["X-Speakeasy-Utterance-Id"] = utterance_id
    if protocol_version is not None:
        headers["X-Speakeasy-Protocol-Version"] = protocol_version
    if content_length_override is not None:
        headers["Content-Length"] = str(content_length_override)
        conn.putrequest("POST", "/transcribe", skip_host=True)
        for k, v in headers.items():
            conn.putheader(k, v)
        conn.endheaders()
        conn.send(body)
        return conn.getresponse()
    conn.request("POST", "/transcribe", body=body, headers=headers)
    return conn.getresponse()


# --- C3: fail-closed startup -------------------------------------------------

def test_run_server_fails_closed_when_bind_address_unavailable():
    def broken_resolver():
        raise BindAddressUnavailable("tailscaled not running")

    with pytest.raises(BindAddressUnavailable):
        run_server(StubEngine(), ENGINE_SETTINGS, _allow_all_cache(),
                   bind_resolver=broken_resolver)

def test_resolve_tailscale_bind_address_raises_on_command_failure():
    def failing_run(*a, **k):
        raise FileNotFoundError("tailscale not installed")

    with pytest.raises(BindAddressUnavailable):
        resolve_tailscale_bind_address(run=failing_run)

def test_resolve_tailscale_bind_address_raises_on_empty_output():
    class FakeResult:
        stdout = ""
    with pytest.raises(BindAddressUnavailable):
        resolve_tailscale_bind_address(run=lambda *a, **k: FakeResult())


# --- S1: bind behavior --------------------------------------------------------

def test_server_binds_to_the_resolved_address_only():
    rs = RunningServer(StubEngine(), _allow_all_cache())
    try:
        assert rs.server.server_address[0] == "127.0.0.1"
        # A connection to a DIFFERENT loopback-adjacent address that nothing
        # binds is refused -- proves this isn't also listening on 0.0.0.0.
        with pytest.raises((ConnectionRefusedError, OSError)):
            s = socket.create_connection(("127.0.0.2", rs.port), timeout=1)
            s.close()
    finally:
        rs.close()


# --- S2: ACL ------------------------------------------------------------------

def test_disallowed_peer_refused_before_engine_is_called():
    engine = StubEngine()
    rs = RunningServer(engine, _allow_none_cache())
    try:
        resp = post_transcribe(rs.port, np.zeros(1000, dtype=np.float32))
        assert resp.status == 403
        resp.read()
        assert engine.calls == []  # never reached the engine
    finally:
        rs.close()

def test_allowed_peer_reaches_the_engine():
    engine = StubEngine(text="transcribed text")
    rs = RunningServer(engine, _allow_all_cache())
    try:
        resp = post_transcribe(rs.port, np.zeros(1000, dtype=np.float32))
        assert resp.status == 200
        data = json.loads(resp.read())
        assert data["text"] == "transcribed text"
        assert engine.calls == [(1000, 16000)]
    finally:
        rs.close()

def test_health_endpoint_also_gated_by_acl():
    rs = RunningServer(StubEngine(), _allow_none_cache())
    try:
        conn = HTTPConnection("127.0.0.1", rs.port, timeout=5)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        assert resp.status == 403
    finally:
        rs.close()


# --- R4.1: wire contract -------------------------------------------------------

def test_response_is_transcriptionresult_shaped_with_engine_identity():
    rs = RunningServer(StubEngine(text="hi", avg_logprob=-0.42), _allow_all_cache())
    try:
        resp = post_transcribe(rs.port, np.zeros(100, dtype=np.float32), utterance_id="abc-123")
        data = json.loads(resp.read())
        assert data["text"] == "hi"
        assert data["avg_logprob"] == pytest.approx(-0.42)
        assert data["utterance_id"] == "abc-123"
        assert data["engine"]["host_id"] == "test-host"
        assert data["protocol_version"] == PROTOCOL_VERSION
    finally:
        rs.close()

def test_none_avg_logprob_is_json_null_not_omitted_or_coerced():
    rs = RunningServer(StubEngine(text="hi", avg_logprob=None), _allow_all_cache())
    try:
        resp = post_transcribe(rs.port, np.zeros(100, dtype=np.float32))
        raw = resp.read()
        data = json.loads(raw)
        assert "avg_logprob" in data
        assert data["avg_logprob"] is None
        assert b'"avg_logprob": null' in raw or b'"avg_logprob":null' in raw
    finally:
        rs.close()


# --- R4.2/R4.3: handshake + request shape -------------------------------------

def test_missing_utterance_id_rejected_before_body_processing():
    engine = StubEngine()
    rs = RunningServer(engine, _allow_all_cache())
    try:
        resp = post_transcribe(rs.port, np.zeros(100, dtype=np.float32), utterance_id=None)
        assert resp.status == 400
        resp.read()
        assert engine.calls == []
    finally:
        rs.close()

def test_protocol_version_mismatch_rejected():
    rs = RunningServer(StubEngine(), _allow_all_cache())
    try:
        resp = post_transcribe(rs.port, np.zeros(100, dtype=np.float32), protocol_version="99")
        assert resp.status == 409
    finally:
        rs.close()


# --- H1: Content-Length cap ----------------------------------------------------

def test_oversized_content_length_rejected_before_read():
    engine = StubEngine()
    rs = RunningServer(engine, _allow_all_cache())
    try:
        conn = HTTPConnection("127.0.0.1", rs.port, timeout=5)
        conn.putrequest("POST", "/transcribe", skip_host=True)
        conn.putheader("X-Speakeasy-Utterance-Id", "u-1")
        conn.putheader("X-Speakeasy-Protocol-Version", str(PROTOCOL_VERSION))
        conn.putheader("Content-Length", str(MAX_CONTENT_LENGTH + 1))
        conn.endheaders()
        # Deliberately do NOT send a body of that size -- if the handler tried to
        # read it before refusing, this would hang/timeout instead of returning fast.
        resp = conn.getresponse()
        assert resp.status == 413
        assert engine.calls == []
    finally:
        rs.close()


# --- SF3: misaligned Content-Length gets a clean 400, not a server-side crash --

def test_misaligned_content_length_rejected_cleanly():
    engine = StubEngine()
    rs = RunningServer(engine, _allow_all_cache())
    try:
        resp = post_transcribe(rs.port, np.zeros(100, dtype=np.float32),
                                content_length_override=101)  # not a multiple of 4
        assert resp.status == 400
        assert engine.calls == []
    finally:
        rs.close()


# --- SF1: concurrent requests never call the engine at the same time -----------

def test_engine_calls_are_serialized_under_concurrent_requests():
    concurrent_peak = []
    in_flight = []
    lock = threading.Lock()

    class SlowEngine(StubEngine):
        def transcribe_ex(self, samples, sample_rate=16000):
            with lock:
                in_flight.append(1)
                concurrent_peak.append(len(in_flight))
            time.sleep(0.05)
            with lock:
                in_flight.pop()
            return super().transcribe_ex(samples, sample_rate)

    engine = SlowEngine()
    rs = RunningServer(engine, _allow_all_cache())
    try:
        threads = [threading.Thread(target=post_transcribe, args=(rs.port, np.zeros(100, dtype=np.float32)),
                                     kwargs={"utterance_id": f"u-{i}"}) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert max(concurrent_peak) == 1, f"expected serialized access, saw concurrency {concurrent_peak}"
    finally:
        rs.close()


# --- S3: no audio at rest (behavioral, not code-reading) -----------------------

# --- launcher (`python -m speakeasy.service`) ----------------------------------

def test_main_parses_port_from_argv(monkeypatch):
    captured = {}

    def fake_run_server(engine, engine_settings, peer_cache, port=DEFAULT_PORT, **kw):
        captured["port"] = port
        class FakeServer:
            server_address = ("192.0.2.3", port)
            def serve_forever(self):
                raise KeyboardInterrupt()  # exit main() immediately for the test
            def server_close(self):
                pass
        return FakeServer()

    monkeypatch.setattr("speakeasy.config.load_settings", lambda: type("S", (), {"engine": "parakeet"})())
    monkeypatch.setattr("speakeasy.config.load_host_settings_record", lambda: {})
    monkeypatch.setattr("speakeasy.engine.create_engine", lambda name, models_dir: StubEngine())
    monkeypatch.setattr("speakeasy.ranking.default_tailscale_peers", lambda: {})
    monkeypatch.setattr(service_mod, "run_server", fake_run_server)

    service_mod.main(["8080"])
    assert captured["port"] == 8080

def test_main_defaults_to_default_port(monkeypatch):
    captured = {}

    def fake_run_server(engine, engine_settings, peer_cache, port=DEFAULT_PORT, **kw):
        captured["port"] = port
        class FakeServer:
            server_address = ("192.0.2.3", port)
            def serve_forever(self):
                raise KeyboardInterrupt()
            def server_close(self):
                pass
        return FakeServer()

    monkeypatch.setattr("speakeasy.config.load_settings", lambda: type("S", (), {"engine": "parakeet"})())
    monkeypatch.setattr("speakeasy.config.load_host_settings_record", lambda: {})
    monkeypatch.setattr("speakeasy.engine.create_engine", lambda name, models_dir: StubEngine())
    monkeypatch.setattr("speakeasy.ranking.default_tailscale_peers", lambda: {})
    monkeypatch.setattr(service_mod, "run_server", fake_run_server)

    service_mod.main([])
    assert captured["port"] == DEFAULT_PORT

def test_main_fails_closed_and_exits_when_bind_unavailable(monkeypatch):
    def failing_run_server(*a, **kw):
        raise BindAddressUnavailable("tailscaled not running")

    monkeypatch.setattr("speakeasy.config.load_settings", lambda: type("S", (), {"engine": "parakeet"})())
    monkeypatch.setattr("speakeasy.config.load_host_settings_record", lambda: {})
    monkeypatch.setattr("speakeasy.engine.create_engine", lambda name, models_dir: StubEngine())
    monkeypatch.setattr("speakeasy.ranking.default_tailscale_peers", lambda: {})
    monkeypatch.setattr(service_mod, "run_server", failing_run_server)

    with pytest.raises(SystemExit):
        service_mod.main([])


def test_no_file_is_opened_anywhere_during_a_served_request(monkeypatch, tmp_path):
    opened_paths = []
    real_open = open

    def tracking_open(path, *args, **kwargs):
        opened_paths.append(str(path))
        return real_open(path, *args, **kwargs)

    engine = StubEngine()
    rs = RunningServer(engine, _allow_all_cache())
    try:
        monkeypatch.setattr("builtins.open", tracking_open)
        resp = post_transcribe(rs.port, np.zeros(1000, dtype=np.float32))
        assert resp.status == 200
        resp.read()
    finally:
        rs.close()
    assert opened_paths == [], f"expected zero file opens, got: {opened_paths}"
