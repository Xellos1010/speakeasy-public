"""`speakeasy serve`: the mobile API v1 (docs/mobile/API-CONTRACT.md).

Binds 127.0.0.1 and this host's tailnet IP only -- never 0.0.0.0 (DECISIONS.md D1). The phone
reaches it through `tailscale serve` (tailnet-only HTTPS) which proxies to 127.0.0.1.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional

from speakeasy.deploy_profile import accounts_path as _deploy_accounts_path
from speakeasy.mobile.audio_io import (
    MAX_AUDIO_BYTES,
    SAMPLE_RATE,
    AudioError,
    UnsupportedAudio,
    decode_audio,
    parse_multipart,
)
from speakeasy.mobile.auth import DeviceRegistry, PairingLocked
from speakeasy.mobile.fsutil import private_dir
from speakeasy.mobile.portable_profile import (
    PortableProfile,
    correction_pairs,
    make_event,
    new_utterance_id,
)

log = logging.getLogger("speakeasy.mobile")

DEFAULT_PORT = 8812
API_MODES = {"raw": "raw", "clean": "clean", "email": "email", "prompt": "prompt-polish",
             "prompt-polish": "prompt-polish"}
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_IMPORT_BYTES = 64 * 1024 * 1024
MAX_EVENTS_PER_CALL = 500


class HttpError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class MobileApp:
    """Request-independent state. Everything is injectable so tests use stubs."""

    def __init__(self, *, engine, engine_name: str, model_name: str,
                 profile: PortableProfile, registry: DeviceRegistry, history,
                 postprocess=None, modes: Optional[dict] = None, pool=None,
                 rewrite_fn: Optional[Callable] = None, spool_dir: Optional[Path] = None,
                 host: Optional[str] = None):
        self.engine = engine
        self.engine_name = engine_name
        self.model_name = model_name
        self.profile = profile
        self.registry = registry
        self.history = history
        self.postprocess = postprocess
        self.modes = modes or {}
        self.pool = pool
        self.rewrite_fn = rewrite_fn
        self.spool_dir = Path(spool_dir) if spool_dir else registry.root
        self.host = host or socket.gethostname()
        self.engine_lock = threading.Lock()

    # -- routes ------------------------------------------------------------------------
    def health(self) -> dict:
        return {"ok": True, "engine": self.engine_name, "model": self.model_name,
                "profile_version": self.profile.version, "host": self.host}

    def pair(self, body: dict) -> dict:
        try:
            res = self.registry.pair(str(body.get("code", "")), str(body.get("device_name", "")))
        except PairingLocked as e:
            raise HttpError(429, str(e))
        if not res:
            raise HttpError(401, "invalid or expired pairing code")
        self.profile.record("device_paired", {"device_name": res["device_name"]},
                            device_id=res["device_id"])
        return {**res, "host": self.host}

    def transcribe(self, device: dict, fields: dict, files: dict) -> dict:
        t0 = time.monotonic()
        audio = files.get("audio")
        if not audio or not audio["data"]:
            raise HttpError(400, "missing audio part")
        api_mode = (fields.get("mode") or "raw").strip().lower()
        if api_mode not in API_MODES:
            raise HttpError(400, f"unknown mode {api_mode!r} (raw|clean|email|prompt)")
        mode_name = API_MODES[api_mode]
        lang = (fields.get("lang") or "").strip() or None
        app_hint = (fields.get("app_hint") or "").strip()[:200] or None
        device_id = device["device_id"]
        utterance_id = (fields.get("utterance_id") or "").strip()[:64] or new_utterance_id()

        prior = self.profile.find_utterance(utterance_id)
        if prior is not None:  # idempotent retry: never re-transcribe or double-log
            return self._response(prior["data"])

        try:
            samples = decode_audio(audio["data"], audio.get("filename", ""),
                                   tmp_dir=self.spool_dir / "tmp")
        except UnsupportedAudio as e:
            self._spool_failed(utterance_id, audio)
            raise HttpError(415, str(e))
        except AudioError as e:
            raise HttpError(400, str(e))

        try:
            with self.engine_lock:
                result = self.engine.transcribe_ex(samples, sample_rate=SAMPLE_RATE)
        except Exception as e:  # never lose the dictation: keep the audio for retry
            self._spool_failed(utterance_id, audio)
            log.exception("engine failure for %s", utterance_id)
            raise HttpError(500, f"transcription failed: {type(e).__name__}")
        raw_text = result.text
        applied = raw_text
        if self.postprocess is not None:
            try:
                applied = self.postprocess.process(applied)
            except Exception:
                log.warning("voice-profile postprocess failed; using raw", exc_info=True)
        try:
            applied = self.profile.apply(applied)
        except Exception:
            log.warning("portable-profile apply failed; using voice-profile text", exc_info=True)

        target = f"mobile:{device.get('name') or device_id}" + (f":{app_hint}" if app_hint else "")
        rid = self.history.record(raw_text, mode_name, target)  # history BEFORE any response

        text, rewrite_status, account = applied, "none", None
        mode = self.modes.get(mode_name)
        if mode is not None and getattr(mode, "system_prompt", None) and self.rewrite_fn:
            try:
                res = self.rewrite_fn(applied, mode, self.pool)
                text, rewrite_status, account = res.text, "ok", res.account
                self.history.update(rid, status="delivered_mobile", rewritten=res.text,
                                    account=res.account, latency_ms=res.latency_ms)
            except Exception as e:
                log.warning("rewrite failed (%s); falling back to raw", type(e).__name__)
                rewrite_status = "fallback"
                self.history.update(rid, status="rewrite_failed")
        else:
            if mode_name != "raw":
                rewrite_status = "fallback"
            self.history.update(rid, status="delivered_mobile")

        latency_ms = int((time.monotonic() - t0) * 1000)
        data = {
            "utterance_id": utterance_id,
            "text": text,
            "raw_text": raw_text,
            "applied_text": applied,
            "mode": api_mode,
            "rewrite_status": rewrite_status,
            "model": self.model_name,
            "latency_ms": latency_ms,
            "lang": lang,
            "app_hint": app_hint,
            "duration_s": round(len(samples) / SAMPLE_RATE, 3),
            "history_id": rid,
            "avg_logprob": getattr(result, "avg_logprob", None),
        }
        ev = make_event("dictation", data, device_id=device_id, event_id="dict_" + utterance_id)
        self.profile.append([ev])
        data["profile_version"] = self.profile.version
        return self._response(data)

    def _response(self, d: dict) -> dict:
        return {"utterance_id": d["utterance_id"], "text": d["text"], "raw_text": d["raw_text"],
                "mode": d["mode"], "model": d.get("model", self.model_name),
                "latency_ms": d.get("latency_ms", 0),
                "rewrite_status": d.get("rewrite_status", "none"),
                "profile_version": self.profile.version}

    def _spool_failed(self, utterance_id: str, audio: dict) -> None:
        try:
            d = private_dir(self.spool_dir / "failed")
            suffix = Path(audio.get("filename") or "").suffix[:8] or ".bin"
            p = d / f"{utterance_id}{suffix}"
            p.write_bytes(audio["data"])
            p.chmod(0o600)
        except OSError:
            log.warning("could not spool failed audio", exc_info=True)

    def feedback(self, device: dict, body: dict) -> dict:
        utterance_id = str(body.get("utterance_id") or "")
        corrected = body.get("corrected_text")
        if not utterance_id or not isinstance(corrected, str):
            raise HttpError(400, "utterance_id and corrected_text are required")
        prior = self.profile.find_utterance(utterance_id)
        if prior is None:
            raise HttpError(404, "unknown utterance_id")
        before = prior["data"].get("text", "")
        import hashlib
        eid = "corr_" + hashlib.sha256(f"{utterance_id}\n{corrected}".encode()).hexdigest()[:24]
        self.profile.append([make_event("correction", {
            "utterance_id": utterance_id, "before": before, "raw_text": prior["data"].get("raw_text"),
            "corrected_text": corrected[:20000], "mode": prior["data"].get("mode"),
        }, device_id=device["device_id"], event_id=eid)])
        derived = self.profile.derived()
        rules = {r["pattern"].lower(): r for r in derived["corrections"]}
        vocab = {v["term"] for v in derived["vocab"]}
        learned = []
        for pattern, replacement in correction_pairs(before, corrected):
            r = rules.get(pattern.lower())
            learned.append({"kind": "correction", "pattern": pattern, "replacement": replacement,
                            "active": bool(r and r["replacement"] == replacement and r["active"])})
            for tok in replacement.split():
                tok = tok.strip(".,;:!?\"()")
                if tok in vocab:
                    learned.append({"kind": "vocab", "pattern": tok, "replacement": tok, "active": True})
        return {"ok": True, "learned": learned, "profile_version": derived["version"]}

    def events(self, device: dict, body: dict) -> dict:
        evs = body.get("events")
        if not isinstance(evs, list):
            raise HttpError(400, "events must be a list")
        if len(evs) > MAX_EVENTS_PER_CALL:
            raise HttpError(413, f"at most {MAX_EVENTS_PER_CALL} events per call")
        import hashlib
        out = []
        for e in evs:
            if not isinstance(e, dict) or not isinstance(e.get("type"), str) or not e["type"]:
                raise HttpError(400, "each event needs a string type")
            data = e.get("data") if isinstance(e.get("data"), dict) else {}
            cid = e.get("id")
            eid = None
            if cid:
                eid = "c_" + hashlib.sha256(f"{device['device_id']}\n{cid}".encode()).hexdigest()[:24]
            out.append(make_event(e["type"][:64], data, device_id=device["device_id"],
                                  ts=e.get("ts"), event_id=eid))
        self.profile.append(out)
        return {"ok": True, "accepted": len(out), "profile_version": self.profile.version}


def make_handler(app: MobileApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "speakeasy-mobile/1"

        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self, limit: int) -> bytes:
            te = (self.headers.get("Transfer-Encoding") or "").lower()
            if "chunked" in te:
                out = bytearray()
                while True:
                    line = self.rfile.readline(1024)
                    size = int(line.split(b";")[0].strip() or b"0", 16)
                    if size == 0:
                        self.rfile.readline(1024)
                        break
                    if len(out) + size > limit:
                        raise HttpError(413, "payload too large")
                    out += self.rfile.read(size)
                    self.rfile.readline(1024)
                return bytes(out)
            n = int(self.headers.get("Content-Length") or 0)
            if n > limit:
                raise HttpError(413, "payload too large")
            return self.rfile.read(n) if n > 0 else b""

        def _json(self, limit: int = MAX_JSON_BYTES) -> dict:
            raw = self._read_body(limit)
            try:
                obj = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                raise HttpError(400, "invalid JSON body")
            if not isinstance(obj, dict):
                raise HttpError(400, "JSON body must be an object")
            return obj

        def _device(self) -> dict:
            auth = self.headers.get("Authorization") or ""
            token = auth[7:].strip() if auth[:7].lower() == "bearer " else None
            dev = app.registry.authenticate(token)
            if dev is None:
                raise HttpError(401, "missing or invalid device token")
            return dev

        def _dispatch(self, method: str) -> None:
            t0 = time.monotonic()
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            code = 200
            try:
                if method == "GET" and path == "/v1/health":
                    out = app.health()
                elif method == "POST" and path == "/v1/pair":
                    out = app.pair(self._json())
                else:
                    dev = None
                    if path.startswith("/v1/"):
                        dev = self._device()
                    if method == "POST" and path == "/v1/transcribe":
                        body = self._read_body(MAX_AUDIO_BYTES + 1024 * 1024)
                        fields, files = parse_multipart(self.headers.get("Content-Type") or "", body)
                        out = app.transcribe(dev, fields, files)
                    elif method == "POST" and path == "/v1/feedback":
                        out = app.feedback(dev, self._json())
                    elif method == "POST" and path == "/v1/events":
                        out = app.events(dev, self._json())
                    elif method == "GET" and path == "/v1/profile":
                        out = app.profile.summary()
                    elif method == "GET" and path == "/v1/profile/export":
                        out = app.profile.export_bundle()
                    elif method == "POST" and path == "/v1/profile/import":
                        try:
                            out = app.profile.import_bundle(self._json(MAX_IMPORT_BYTES))
                        except ValueError as e:
                            raise HttpError(400, str(e))
                    else:
                        raise HttpError(404, "not found")
                    if dev is not None:
                        try:
                            app.registry.touch(dev["device_id"])
                        except Exception:
                            pass
                self._send(200, out)
            except HttpError as e:
                code = e.code
                self._send(e.code, {"error": e.message})
            except AudioError as e:
                code = 400
                self._send(400, {"error": str(e)})
            except Exception:
                code = 500
                log.exception("unhandled error on %s %s", method, path)
                self._send(500, {"error": "internal error"})
            finally:
                log.info("%s %s %s %dms", method, path, code, int((time.monotonic() - t0) * 1000))

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def log_message(self, format, *args):
            pass  # request lines are logged by _dispatch (path only; never headers)

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_servers(app: MobileApp, addresses: list[str], port: int) -> list[ThreadingHTTPServer]:
    servers = []
    handler = make_handler(app)
    for addr in addresses:
        if addr in ("0.0.0.0", "::", ""):
            raise ValueError("refusing to bind a wildcard address")
        srv = _Server((addr, port), handler)
        threading.Thread(target=srv.serve_forever, name=f"mobile-api-{addr}", daemon=True).start()
        servers.append(srv)
    return servers


def build_app(profile_dir: Optional[Path] = None, mobile_dir: Optional[Path] = None) -> MobileApp:
    from speakeasy.accounts import AccountPool
    from speakeasy.config import load_settings, speakeasy_dir
    from speakeasy.engine import create_engine
    from speakeasy.engine.download import MODELS
    from speakeasy.engine.postprocess import Postprocessor
    from speakeasy.history import History
    from speakeasy.modes import load_modes
    from speakeasy.rewrite import rewrite

    base = speakeasy_dir()
    settings = load_settings()
    engine_name = settings.engine if settings.engine in ("parakeet", "whisper") else "parakeet"
    engine = create_engine(engine_name, base / "models")
    model_name = MODELS.get(engine_name, {}).get("dirname", engine_name)
    try:
        pool = AccountPool(_deploy_accounts_path(), base / "state.json",
                           allowlist=settings.account_allowlist or None)
    except Exception:
        log.warning("no Claude account pool; rewrite modes will fall back to raw")
        pool = None

    def _rewrite(text, mode, pool_):
        if pool_ is None:
            raise RuntimeError("no account pool")
        return rewrite(text, mode, pool_, timeout=15.0, max_attempts=2)

    registry = DeviceRegistry(mobile_dir)
    return MobileApp(
        engine=engine, engine_name=engine_name, model_name=model_name,
        profile=PortableProfile(profile_dir), registry=registry,
        history=History(base / "history.db"), postprocess=Postprocessor(),
        modes=load_modes(base / "modes.yaml"), pool=pool, rewrite_fn=_rewrite,
        spool_dir=registry.root,
    )


def serve(port: int = DEFAULT_PORT, tailnet: bool = True) -> None:
    from speakeasy.logging_setup import configure_logging
    from speakeasy.service import BindAddressUnavailable, resolve_tailscale_bind_address

    configure_logging()
    app = build_app()
    try:
        from speakeasy.mobile.operator_overlay import write_overlay
        app.profile.on_change(lambda prof: write_overlay(prof, versioned=False))
    except Exception:
        log.warning("operator-profile overlay hook unavailable", exc_info=True)

    servers = start_servers(app, ["127.0.0.1"], port)
    log.info("mobile api listening on 127.0.0.1:%d", port)

    def _bind_tailnet():
        while True:
            try:
                addr = resolve_tailscale_bind_address()
                servers.extend(start_servers(app, [addr], port))
                log.info("mobile api listening on %s:%d (tailnet)", addr, port)
                return
            except (BindAddressUnavailable, OSError) as e:
                log.warning("tailnet bind unavailable (%s); retrying in 30s", e)
                time.sleep(30)

    if tailnet:
        threading.Thread(target=_bind_tailnet, name="tailnet-bind", daemon=True).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()
