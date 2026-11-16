"""Network STT service (docs/superpowers/specs/2026-08-18-network-service.md §1, §4, §5).

Binds ONLY to this host's tailscale interface address, never 0.0.0.0, never a
config-supplied address — the tailscale IP is the only legal bind address (S1). If it
can't be resolved at startup, the service fails closed (C3): it refuses to start rather
than falling back to any other interface.
"""

import socket
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from speakeasy.engine import Engine, TranscriptionResult
from speakeasy.ranking import PeerIdentityCache

PROTOCOL_VERSION = 1
DEFAULT_PORT = 7420
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 4  # float32
MAX_UTTERANCE_SECONDS = 60
MAX_CONTENT_LENGTH = MAX_UTTERANCE_SECONDS * SAMPLE_RATE * BYTES_PER_SAMPLE  # H1


class BindAddressUnavailable(Exception):
    """Raised when the tailscale interface address can't be resolved. The caller
    (run_server / main) must let this propagate and refuse to start — never catch it
    to fall back onto another interface (C3)."""


def resolve_tailscale_bind_address(run=subprocess.run) -> str:
    try:
        result = run(["tailscale", "ip", "-4"], capture_output=True, text=True,
                      timeout=5, check=True)
    except Exception as e:
        raise BindAddressUnavailable(f"tailscale ip -4 failed: {e}") from e
    address = result.stdout.strip()
    if not address:
        raise BindAddressUnavailable("tailscale ip -4 returned no address")
    return address


def _engine_identity(engine_settings: dict) -> dict:
    return {
        "host_id": engine_settings.get("host_id", "unknown"),
        "name": engine_settings.get("name", "unknown"),
        "provider": engine_settings.get("provider", "unknown"),
        "settings_updated": engine_settings.get("settings_updated"),
    }


def make_handler(engine: Engine, engine_settings: dict, peer_cache: PeerIdentityCache):
    """Returns a BaseHTTPRequestHandler subclass closed over the given engine/settings/
    peer cache, since http.server wants a class, not an instance, per connection."""

    engine_lock = threading.Lock()  # SF1: serializes engine access across handler threads

    class Handler(BaseHTTPRequestHandler):
        server_version = "speakeasy-stt/1"

        def _refuse(self, code: int, message: str) -> None:
            # P2-1 (audit 2026-11-09, ADJ-SPK-031): an early refusal used to close the
            # socket with the client's unread body still in the receive buffer, which
            # makes the kernel send RST -- the client saw ConnectionResetError instead
            # of this JSON error (the flaky test_service early-refusal tests). Now the
            # refusal is complete (Content-Length + Connection: close), the write side
            # is shut (FIN) and any announced body is DISCARDED, bounded by
            # MAX_CONTENT_LENGTH and a short timeout. The refuse decision still happens
            # before any body byte is read (S2); discarded bytes are never kept.
            body = json.dumps({"error": message}).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True
            self._discard_request_body()

        def _discard_request_body(self) -> None:
            try:
                remaining = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                remaining = 0
            remaining = max(0, min(remaining, MAX_CONTENT_LENGTH))
            try:
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_WR)
            except OSError:
                return
            if remaining == 0:
                return
            try:
                self.connection.settimeout(2.0)
                while remaining > 0:
                    chunk = self.rfile.read1(min(65536, remaining)) if hasattr(
                        self.rfile, "read1") else self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except OSError:
                pass

        def _peer_allowed(self) -> bool:
            client_ip = self.client_address[0]
            return peer_cache.hostname_for(client_ip) is not None

        def do_GET(self):
            if self.path != "/health":
                self._refuse(404, "not found")
                return
            if not self._peer_allowed():
                self._refuse(403, "peer not on allowlist")
                return
            body = json.dumps({
                "protocol_version": PROTOCOL_VERSION,
                "engine": _engine_identity(engine_settings),
                "status": "ok",
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/transcribe":
                self._refuse(404, "not found")
                return
            # S2: refuse before reading any body bytes.
            if not self._peer_allowed():
                self._refuse(403, "peer not on allowlist")
                return

            utterance_id = self.headers.get("X-Speakeasy-Utterance-Id")
            if not utterance_id:
                self._refuse(400, "missing X-Speakeasy-Utterance-Id")  # R4.3
                return

            client_version = self.headers.get("X-Speakeasy-Protocol-Version")
            if client_version != str(PROTOCOL_VERSION):
                self._refuse(409, f"protocol_version mismatch (server={PROTOCOL_VERSION})")
                return

            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0:
                self._refuse(400, "missing or empty body")
                return
            if content_length > MAX_CONTENT_LENGTH:  # H1, checked before reading
                self._refuse(413, "payload too large")
                return
            if content_length % BYTES_PER_SAMPLE != 0:  # SF3
                self._refuse(400, "Content-Length is not a whole number of float32 samples")
                return

            # S3/R5.5: read straight into memory, no tempfile, no path, anywhere.
            raw = self.rfile.read(content_length)
            samples = np.frombuffer(raw, dtype=np.float32)

            sample_rate = int(self.headers.get("X-Speakeasy-Sample-Rate", SAMPLE_RATE))
            # SF1: sherpa-onnx's thread-safety under concurrent calls from
            # ThreadingHTTPServer's per-connection threads is unproven -- serialize
            # engine access rather than assume it's safe.
            with engine_lock:
                result: TranscriptionResult = engine.transcribe_ex(samples, sample_rate=sample_rate)

            body = json.dumps({
                "protocol_version": PROTOCOL_VERSION,
                "utterance_id": utterance_id,
                "text": result.text,
                "avg_logprob": result.avg_logprob,  # None -> JSON null, not omitted/coerced
                "engine": _engine_identity(engine_settings),
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass  # avoid stderr spam in tests; real logging is a separate concern

    return Handler


def run_server(engine: Engine, engine_settings: dict, peer_cache: PeerIdentityCache,
                port: int = DEFAULT_PORT,
                bind_resolver=resolve_tailscale_bind_address) -> ThreadingHTTPServer:
    """Fails closed (raises BindAddressUnavailable) if the tailscale address can't be
    resolved -- never binds 0.0.0.0 or any other fallback address, and never opens a
    socket at all in that case."""
    address = bind_resolver()  # C3: let this raise; no try/except-and-fallback here
    handler_cls = make_handler(engine, engine_settings, peer_cache)
    return ThreadingHTTPServer((address, port), handler_cls)


def main(argv=None) -> None:
    """`python -m speakeasy.service` launcher. Only the port is configurable (argv:
    an optional single int) -- the bind interface is never a launcher option, per
    the spec's "the tailscale IP is the only legal bind address" rule (§4/C3)."""
    import socket
    import sys as _sys

    from speakeasy.config import load_host_settings_record, load_settings, speakeasy_dir
    from speakeasy.engine import create_engine
    from speakeasy.ranking import default_tailscale_peers, PeerIdentityCache

    from speakeasy.logging_setup import configure_logging
    configure_logging()

    argv = argv if argv is not None else _sys.argv[1:]
    port = int(argv[0]) if argv else DEFAULT_PORT

    settings = load_settings()
    record = load_host_settings_record()
    engine_cfg = record.get("engine", {})
    engine_settings = {
        "host_id": record.get("host_id", socket.gethostname()),
        "name": engine_cfg.get("name", settings.engine),
        "provider": engine_cfg.get("provider", "cpu"),
        "settings_updated": record.get("updated"),
    }

    engine = create_engine(settings.engine, speakeasy_dir() / "models")
    peer_cache = PeerIdentityCache(refresh_fn=default_tailscale_peers)
    peer_cache.refresh()

    try:
        server = run_server(engine, engine_settings, peer_cache, port=port)
    except BindAddressUnavailable as e:
        print(f"speakeasy-service: refusing to start, no tailscale bind address: {e}",
              file=_sys.stderr)
        raise SystemExit(1) from e

    print(f"speakeasy-service: listening on {server.server_address[0]}:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
