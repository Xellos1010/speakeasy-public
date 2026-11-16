"""Mobile API v1 (docs/mobile/API-CONTRACT.md): every endpoint, auth rejection,
history-before-response, idempotent profile merge, pairing, operator overlay, sync."""

import json
import os
import socket
import stat
import uuid
from http.client import HTTPConnection
from pathlib import Path

import pytest

from speakeasy.engine import TranscriptionResult
from speakeasy.history import History
from speakeasy.mobile import operator_overlay, sync as sync_mod
from speakeasy.mobile.audio_io import decode_audio, parse_multipart
from speakeasy.mobile.auth import DeviceRegistry
from speakeasy.mobile.portable_profile import PortableProfile, derive, make_event
from speakeasy.mobile.server import MobileApp, start_servers
from speakeasy.modes import load_modes
from speakeasy.rewrite import RewriteError, RewriteResult

FIXTURE_WAV = Path(__file__).resolve().parents[1] / "assets" / "sample.wav"


class StubEngine:
    def __init__(self, text="please email jon about speak easy", history=None):
        self.text = text
        self.calls = 0
        self.history = history
        self.history_rows_at_call = None

    def transcribe_ex(self, samples, sample_rate=16000):
        self.calls += 1
        assert sample_rate == 16000 and len(samples) > 16000
        return TranscriptionResult(self.text, -0.2)


class RecordingHistory(History):
    """Real SQLite history that also records the order of writes vs responses."""

    def __init__(self, path, journal):
        super().__init__(path)
        self.journal = journal

    def record(self, *a, **kw):
        rid = super().record(*a, **kw)
        self.journal.append(("history_record", rid))
        return rid


@pytest.fixture
def env(tmp_path):
    journal = []
    history = RecordingHistory(tmp_path / "history.db", journal)
    engine = StubEngine()
    rewrites = {"fail": False}

    def rewrite_fn(text, mode, pool):
        if rewrites["fail"]:
            raise RewriteError("boom")
        return RewriteResult(text.upper(), "acct", 5)

    app = MobileApp(engine=engine, engine_name="parakeet", model_name="test-model",
                    profile=PortableProfile(tmp_path / "profile"),
                    registry=DeviceRegistry(tmp_path / "mobile"), history=history,
                    modes=load_modes(None), rewrite_fn=rewrite_fn, host="test-host")
    srv = start_servers(app, ["127.0.0.1"], 0)[0]
    port = srv.server_address[1]

    class Env:
        pass

    e = Env()
    e.app, e.port, e.history, e.engine, e.journal, e.rewrites, e.tmp = (
        app, port, history, engine, journal, rewrites, tmp_path)

    def req(method, path, body=None, token=None, headers=None, raw=None):
        c = HTTPConnection("127.0.0.1", port, timeout=10)
        h = dict(headers or {})
        if token:
            h["Authorization"] = f"Bearer {token}"
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        out = (r.status, json.loads(r.read() or b"{}"))
        c.close()
        journal.append(("response", path))
        return out

    def pair(name="pixel"):
        code = app.registry.new_pairing_code(name)["code"]
        st, res = req("POST", "/v1/pair", {"code": code, "device_name": name})
        assert st == 200, res
        return res

    def transcribe(token, mode="raw", device_id="dev", utterance_id=None, audio=None, fname="a.wav"):
        boundary = uuid.uuid4().hex
        parts = []
        fields = {"device_id": device_id, "mode": mode, "lang": "en", "app_hint": "com.slack"}
        if utterance_id:
            fields["utterance_id"] = utterance_id
        for k, v in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; filename="{fname}"\r\n'
                     f'Content-Type: audio/wav\r\n\r\n'.encode()
                     + (audio if audio is not None else FIXTURE_WAV.read_bytes()) + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        return req("POST", "/v1/transcribe", token=token, raw=b"".join(parts),
                   headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})

    e.req, e.pair, e.transcribe = req, pair, transcribe
    yield e
    srv.shutdown()
    srv.server_close()


# -- health / auth / pairing ------------------------------------------------------------

def test_health_no_auth(env):
    st, body = env.req("GET", "/v1/health")
    assert st == 200
    assert body == {"ok": True, "engine": "parakeet", "model": "test-model",
                    "profile_version": 0, "host": "test-host"}


@pytest.mark.parametrize("method,path", [
    ("POST", "/v1/transcribe"), ("POST", "/v1/feedback"), ("GET", "/v1/profile"),
    ("GET", "/v1/profile/export"), ("POST", "/v1/profile/import"), ("POST", "/v1/events")])
def test_auth_rejection(env, method, path):
    st, body = env.req(method, path, token=None, raw=b"{}")
    assert st == 401 and "token" in body["error"]
    st, _ = env.req(method, path, token="not-a-real-token", raw=b"{}")
    assert st == 401


def test_pairing_one_time_and_hashed(env):
    res = env.pair()
    assert res["device_id"].startswith("dev_") and len(res["token"]) >= 32
    raw = (env.tmp / "mobile" / "devices.json").read_text()
    assert res["token"] not in raw  # only hashes at rest
    assert stat.S_IMODE((env.tmp / "mobile" / "devices.json").stat().st_mode) == 0o600
    code = env.app.registry.new_pairing_code("x")["code"]
    assert env.req("POST", "/v1/pair", {"code": code, "device_name": "x"})[0] == 200
    assert env.req("POST", "/v1/pair", {"code": code, "device_name": "x"})[0] == 401  # reused


def test_pairing_lockout_after_wrong_codes(env):
    good = env.app.registry.new_pairing_code("p")["code"]
    bad = f"{(int(good) + 1) % 1_000_000:06d}"
    codes = [env.req("POST", "/v1/pair", {"code": bad, "device_name": "p"})[0] for _ in range(5)]
    assert codes[:4] == [401] * 4 and codes[4] == 429
    assert env.req("POST", "/v1/pair", {"code": good, "device_name": "p"})[0] == 401  # revoked


def test_expired_code_rejected(tmp_path):
    t = [1000.0]
    reg = DeviceRegistry(tmp_path, now=lambda: t[0])
    code = reg.new_pairing_code("p", ttl_s=10)["code"]
    t[0] += 11
    assert reg.pair(code, "p") == {}


# -- transcribe ---------------------------------------------------------------------------

def test_transcribe_raw_writes_history_before_response(env):
    tok = env.pair()["token"]
    st, body = env.transcribe(tok, mode="raw")
    assert st == 200, body
    assert set(body) >= {"utterance_id", "text", "raw_text", "mode", "model", "latency_ms",
                         "profile_version"}
    assert body["raw_text"] == "please email jon about speak easy" and body["mode"] == "raw"
    kinds = [k for k, _ in env.journal]
    assert kinds.index("history_record") < len(kinds) - 1 and kinds[-1] == "response"
    rows = env.history.recent(5)
    assert rows[0]["raw"] == body["raw_text"] and rows[0]["target_app"].startswith("mobile:pixel")
    ev = env.app.profile.find_utterance(body["utterance_id"])
    assert ev and ev["data"]["text"] == body["text"]
    assert body["profile_version"] == env.app.profile.version


def test_transcribe_rewrite_and_fallback(env):
    tok = env.pair()["token"]
    st, body = env.transcribe(tok, mode="clean")
    assert st == 200 and body["text"] == body["raw_text"].upper() and body["rewrite_status"] == "ok"
    env.rewrites["fail"] = True
    st, body = env.transcribe(tok, mode="email")
    assert st == 200 and body["text"] == body["raw_text"] and body["rewrite_status"] == "fallback"
    assert env.history.recent(1)[0]["status"] == "rewrite_failed"
    st, body = env.transcribe(tok, mode="prompt")
    assert st == 200 and env.history.recent(1)[0]["mode"] == "prompt-polish"


def test_transcribe_idempotent_retry(env):
    tok = env.pair()["token"]
    a = env.transcribe(tok, utterance_id="utt_fixed1")[1]
    b = env.transcribe(tok, utterance_id="utt_fixed1")[1]
    assert a["text"] == b["text"] and env.engine.calls == 1 and len(env.history.recent(10)) == 1


def test_transcribe_bad_inputs(env):
    tok = env.pair()["token"]
    assert env.transcribe(tok, mode="shout")[0] == 400
    assert env.transcribe(tok, audio=b"not audio at all", fname="x.m4a")[0] == 415
    assert list((env.tmp / "mobile" / "failed").iterdir())  # kept for retry, never lost


# -- feedback / profile growth ----------------------------------------------------------

def test_feedback_learns_and_applies(env):
    tok = env.pair()["token"]
    body = env.transcribe(tok)[1]
    st, fb = env.req("POST", "/v1/feedback", {"utterance_id": body["utterance_id"],
                                              "corrected_text": "please email Jon about SpeakEasy",
                                              "device_id": "x"}, token=tok)
    assert st == 200 and fb["ok"]
    learned = {(l["pattern"], l["replacement"]): l for l in fb["learned"] if l["kind"] == "correction"}
    assert learned[("speak easy", "SpeakEasy")]["active"] is True
    nxt = env.transcribe(tok)[1]
    assert "SpeakEasy" in nxt["text"] and nxt["raw_text"] == "please email jon about speak easy"
    st, prof = env.req("GET", "/v1/profile", token=tok)
    assert st == 200 and prof["vocab_count"] >= 1 and "SpeakEasy" in prof["top_vocab"]
    assert prof["preferences"]["preferred_mode"] == "raw"
    assert env.req("POST", "/v1/feedback", {"utterance_id": "nope", "corrected_text": "x"}, token=tok)[0] == 404


def test_common_word_rule_needs_second_sighting():
    evs = [make_event("correction", {"before": "meet their", "corrected_text": "meet there"}, ts=1)]
    rule = [r for r in derive(evs)["corrections"] if r["pattern"] == "their"][0]
    assert rule["active"] is False
    evs.append(make_event("correction", {"before": "go their", "corrected_text": "go there"}, ts=2))
    assert [r for r in derive(evs)["corrections"] if r["pattern"] == "their"][0]["active"] is True


def test_events_endpoint_and_preferences(env):
    tok = env.pair()["token"]
    evs = [{"type": "preference", "ts": 1_700_000_000_000, "data": {"key": "theme", "value": "dark"}, "id": "a1"},
           {"type": "preference", "ts": 1_700_000_001_000, "data": {"key": "theme", "value": "light"}, "id": "a2"},
           {"type": "vocab_add", "ts": 1_700_000_002, "data": {"term": "Kapra"}, "id": "a3"},
           {"type": "replacement", "ts": 1_700_000_003, "data": {"pattern": "a c m e", "replacement": "ACME"}, "id": "a4"},
           {"type": "app_usage", "ts": 1_700_000_004, "data": {"screen": "main"}}]
    st, res = env.req("POST", "/v1/events", {"device_id": "x", "events": evs}, token=tok)
    assert st == 200 and res["accepted"] == 5
    v = res["profile_version"]
    assert env.req("POST", "/v1/events", {"device_id": "x", "events": evs}, token=tok)[1]["profile_version"] == v
    prof = env.req("GET", "/v1/profile", token=tok)[1]
    assert prof["preferences"]["ui"]["theme"] == "light" and "Kapra" in prof["top_vocab"]
    assert env.app.profile.apply("call a c m e now") == "call ACME now"
    assert env.req("POST", "/v1/events", {"events": [{"data": {}}]}, token=tok)[0] == 400


# -- export / import / merge idempotency ------------------------------------------------

def test_export_import_merge_idempotent(env, tmp_path):
    tok = env.pair()["token"]
    env.transcribe(tok)
    st, bundle = env.req("GET", "/v1/profile/export", token=tok)
    assert st == 200 and bundle["schema"] == "speakeasy.portable-profile/1" and bundle["events"]
    other = PortableProfile(tmp_path / "other")
    other.record("vocab_add", {"term": "Acme"}, device_id="linux-ws", ts=5)
    r1 = env.req("POST", "/v1/profile/import", other.export_bundle(), token=tok)[1]
    r2 = env.req("POST", "/v1/profile/import", other.export_bundle(), token=tok)[1]
    assert r1["added"] == 1 and r2["added"] == 0 and r1["profile_version"] == r2["profile_version"]
    other.import_bundle(env.req("GET", "/v1/profile/export", token=tok)[1])
    assert other.derived()["digest"] == env.app.profile.derived()["digest"]  # converged
    assert env.req("POST", "/v1/profile/import", {"schema": "x"}, token=tok)[0] == 400


def test_merge_is_order_independent(tmp_path):
    a, b = PortableProfile(tmp_path / "a"), PortableProfile(tmp_path / "b")
    a.record("vocab_add", {"term": "Fable"}, device_id="mac", ts=1)
    b.record("mode_choice", {"mode": "email"}, device_id="phone", ts=2)
    ab, ba = PortableProfile(tmp_path / "ab"), PortableProfile(tmp_path / "ba")
    ab.import_bundle(a.export_bundle()); ab.import_bundle(b.export_bundle())
    ba.import_bundle(b.export_bundle()); ba.import_bundle(a.export_bundle())
    da, db = ab.derived(), ba.derived()
    assert da == db
    assert stat.S_IMODE((tmp_path / "ab" / "events.jsonl").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "ab").stat().st_mode) == 0o700


def test_torn_line_is_skipped(tmp_path):
    p = PortableProfile(tmp_path)
    p.record("vocab_add", {"term": "Pi"}, ts=1)
    with open(tmp_path / "events.jsonl", "a") as f:
        f.write('{"id": "half')
    assert PortableProfile(tmp_path).version == 1


# -- sync (peer) ------------------------------------------------------------------------

def test_sync_with_peer_converges(env, tmp_path):
    code = env.app.registry.new_pairing_code("linux-ws")["code"]
    url = f"http://127.0.0.1:{env.port}"
    sync_mod.pair_peer("mac", code, "linux-ws", url=url, root=tmp_path / "peers")
    assert stat.S_IMODE((tmp_path / "peers" / "peers.json").stat().st_mode) == 0o600
    tok = env.pair()["token"]
    env.transcribe(tok)
    local = PortableProfile(tmp_path / "linux-profile")
    local.record("vocab_add", {"term": "Raspberry"}, device_id="linux-ws", ts=3)
    res = sync_mod.sync(local, "mac", root=tmp_path / "peers")
    assert res["pulled_added"] >= 2 and res["pushed_added"] == 1
    assert local.derived()["digest"] == env.app.profile.derived()["digest"]
    again = sync_mod.sync(local, "mac", root=tmp_path / "peers")
    assert again["pulled_added"] == 0 and again["pushed_added"] == 0


# -- operator profile overlay -----------------------------------------------------------

def test_operator_overlay_never_clobbers(tmp_path):
    pdir = tmp_path / "operator-profiles"
    pdir.mkdir()
    base = {"id": "acct-a", "input": {"primary": "mouse", "voice_first": False},
            "vocabulary": {"preferred": {"memo": "Bank description"}}, "pace": {"decision_feedback_ms": 100}}
    (pdir / "acct-a.json").write_text(json.dumps(base))
    before = (pdir / "acct-a.json").read_bytes()
    prof = PortableProfile(tmp_path / "profile")
    prof.record("vocab_add", {"term": "SpeakEasy"}, ts=1)
    paths = operator_overlay.write_overlay(prof, pid="acct-a", pdir=pdir)
    doc = json.loads(paths[0].read_text())
    assert (pdir / "acct-a.json").read_bytes() == before
    assert "input.voice_first" in doc["skipped_operator_authored_keys"]
    assert "primary" not in doc["overlay"]["input"] and "voice_first" not in doc["overlay"]["input"]
    assert doc["overlay"]["vocabulary"]["spoken_terms"] == ["SpeakEasy"]
    merged = operator_overlay.merged_view(base, doc["overlay"])
    assert merged["input"]["primary"] == "mouse" and merged["input"]["voice_first"] is False
    assert merged["vocabulary"]["preferred"] == base["vocabulary"]["preferred"]
    assert "speech_words_per_minute" in merged["pace"]


# -- audio helpers ----------------------------------------------------------------------

def test_decode_fixture_wav_and_m4a_via_ffmpeg(tmp_path):
    x = decode_audio(FIXTURE_WAV.read_bytes(), "a.wav")
    assert 2.0 < len(x) / 16000 < 3.0
    import shutil, subprocess
    ff = shutil.which("ffmpeg")
    if not ff:
        pytest.skip("ffmpeg not installed")
    m4a = tmp_path / "a.m4a"
    subprocess.run([ff, "-loglevel", "error", "-y", "-i", str(FIXTURE_WAV), "-c:a", "aac", str(m4a)], check=True)
    y = decode_audio(m4a.read_bytes(), "a.m4a", tmp_dir=tmp_path / "t")
    assert abs(len(y) - len(x)) < 16000 * 0.2


def test_multipart_requires_boundary():
    from speakeasy.mobile.audio_io import AudioError
    with pytest.raises(AudioError):
        parse_multipart("application/json", b"{}")


def test_sentence_capitalisation_is_not_vocab():
    evs = [make_event("correction", {"before": "please go", "corrected_text": "Please go"}, ts=1)]
    rules = {r["pattern"]: r for r in derive(evs)["corrections"]}
    assert rules["please"]["active"] is False
