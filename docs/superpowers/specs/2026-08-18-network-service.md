# Network STT service — spec (ADJ-SPK-006)

Status: DRAFT — scoping only, no implementation on master. Written against an internal requirements document that is not
published — every requirement below cites its requirement id (R2.x/R3.x/R4.x/R5.x/S-x) so review is a checklist against that
document, not a re-read of this one.

Scope boundary (unchanged from ADJ-SPK-006): this spec covers the cross-host piece only. The
single-host client (hotkey, injection, local engine) is the already-merged
`2026-08-17-linux-client-port.md` work; the seam between the two is
`transcribe_ex(audio) -> TranscriptionResult`, satisfied today by a local call, extended here
by an optional remote call with the identical return shape.

Per §1 of the requirements document: this is explicitly NOT a speed feature today (measured i9 ≈ i7, minus a
network hop). It exists for battery/thermal offload while traveling, future weak-capture
devices, and future model asymmetry. Nothing below should be read or documented as a latency
win.

## 1. Wire contract

**R4.1 — Response shape.**
Design: JSON response body:
```json
{
  "protocol_version": 1,
  "utterance_id": "<the request's own id, echoed>",
  "text": "...",
  "avg_logprob": -0.31,
  "engine": {"host_id": "linux-host", "name": "parakeet", "provider": "cpu",
             "settings_updated": "2026-08-17"}
}
```
`text`/`avg_logprob` map 1:1 onto `TranscriptionResult` (verified against the actual type:
`NamedTuple(text: str, avg_logprob: float | None)`) — `avg_logprob` MUST be JSON `null`, not
omitted or coerced to `0`/`NaN`, whenever the engine returns `None` (e.g. an engine that can't
supply a confidence signal). A future field added to
`TranscriptionResult` extends this object; the client's decode step must ignore unknown
fields (forward-compat), never require an exact shape match.
Acceptance test: a client that decodes a response missing a not-yet-invented future field does
not raise; a client that decodes today's fixed field set produces a `TranscriptionResult`
equal to what the local engine's `transcribe_ex` would for the same audio+text.

**R4.2 — Handshake / engine identity.**
Design: every response (including `/health`) carries the `engine` object above. The client
never sends engine settings in the request — the server always runs its own measured optimum
(R-06; port-spec §4's per-host JSON is exactly this `engine` object's source). On
`protocol_version` mismatch the client treats the host as unreachable for this call (same code
path as R5.1's fallback) rather than attempting negotiation.
Acceptance test: a server advertising `protocol_version: 2` against a client that only
understands `1` falls back to local transcription, not an exception or a hang; the fallback
path is exactly R5.1's, not a special case.

**R4.3 — Request shape.**
Design: `POST /transcribe`, headers `X-Speakeasy-Protocol-Version: 1`,
`X-Speakeasy-Utterance-Id: <client-generated UUID4>`, `X-Speakeasy-Sample-Rate: 16000`; body is
raw little-endian float32 PCM (the exact in-memory shape `ParakeetEngine.transcribe_ex` already
takes — no re-encoding step, no WAV container, to keep the no-audio-at-rest property in §4
trivially true: the body is a byte-for-byte copy of the numpy array already in memory).
Acceptance test: a request without `X-Speakeasy-Utterance-Id` is rejected (400) before the body
is read past the headers — this id is load-bearing for R5.3 and must not be optional.

## 2. Ranking (R2.1–R2.5)

Design: each host's `settings.json` (existing `speakeasy/config.py:Settings`, extended) gets a
new field:
```json
"inference_ranking": ["linux", "self"]
```
A resolver module (`speakeasy/ranking.py`, new) owns:
- R2.1/R2.4: loading this list; `self` is appended if absent (never actually removable — the
  loader enforces this, doesn't just document it).
- R2.2/R2.3: `preferred_host() -> str` returns the highest-ranked entry whose *cached* probe
  state (R5.4) is reachable, defaulting to `self` — this call never blocks, never probes
  synchronously, only reads a dict already populated by the background prober.
- R2.5: the ranking list itself is plain settings-file metadata; it rides whatever
  metadata-sync channel R3.2 defines (text/profile sync), never a special case.

Acceptance test: with the prober's cache pre-seeded `{"linux": False}` and ranking
`["linux", "self"]`, `preferred_host()` returns `"self"` in well under a millisecond (proves
"never blocks" — a test asserting wall-clock, not just correctness) and never touches the
network.

## 3. Per-host settings & profile sync (R3.1–R3.3)

R3.1 is already satisfied by the port spec's §4 JSON shape (measured engine optimum, one record
per `host_id`, written only by that host's own benchmark run) — this spec adds nothing there,
just confirms the wire contract's `engine` object (§1 above) is sourced directly from that
record, never recomputed per-request.

R3.2/R3.3 (profile sync) are explicitly **out of scope for this spec's implementation** — the
existing versioned-profile-store (`speakeasy/training/profile.py`) already has the
append-only/rollback-pointer shape R3.2 asks for; wiring its sync transport is deferred to a
follow-up spec once a metadata-sync channel exists (this spec's request/response transport
isn't that channel — it's audio-in/text-out, not a general sync bus). Noted here only so this
spec doesn't accidentally foreclose it: the `engine` object's `settings_updated` timestamp
field is deliberately generic enough to be reused as a sync cursor later.

## 4. Transport, bind, and auth (S1, S2)

Design: `http.server.ThreadingHTTPServer` (stdlib, no new dependency) bound explicitly to the
host's own tailscale IP — resolved at startup via `tailscale ip -4` (subprocess, already the
pattern this codebase uses for external tools), never `0.0.0.0`, never a config-supplied bind
address (the tailscale IP is the *only* legal bind address, not a default). Fixed port 7420
(approved; arbitrary/unregistered — operator can override the port, the interface is never
configurable).

**Startup fail-closed (C3).** If `tailscale ip -4` exits non-zero, times out, or prints nothing
parseable (tailscaled not running, not logged in, or the interface otherwise absent) — the
service refuses to start at all: log a clear error and exit non-zero. It never falls back to
any other interface, never binds `0.0.0.0` as a "just get it running" default, and never
retries into a degraded bind. This is the security-relevant edge of "the tailscale IP is the
only legal bind address": a bind address chosen by fallback logic is no longer that rule.
Acceptance test: with `tailscale ip -4` mocked to fail (non-zero exit), starting the service
raises/exits before any socket is opened at all — assert no listening socket exists afterward,
not just that an error was logged.

**S1 — bind test.** Design: after startup, assert the listening socket's bound address equals
`tailscale ip -4`'s output, and separately assert a connection attempt to `127.0.0.1:<port>` or
the machine's LAN IP fails (connection refused — nothing is listening there because nothing
ever binds there). Both assertions belong in the same test: a bind-address check alone doesn't
prove nothing *else* is also listening on a wildcard address.

**S2 — ACL/allowlist test.** Design: Tailscale's own ACL (configured in the tailnet's admin
policy, outside this codebase — the spec documents the required ACL shape but the ACL itself is
an operator action, not code) is the primary gate: a non-allowed node's traffic never reaches
this box's kernel routing table for the tailnet interface at all. The service adds a second,
belt-and-suspenders check: on each connection, resolve the peer's tailscale identity against a
config allowlist of hostnames; refuse (close the connection) before reading any body bytes if
the peer isn't listed.
**H2 (folded in):** resolving peer identity via a fresh `tailscale status --json` subprocess
*per connection* costs tens of ms on what's supposed to be a low-latency path. Instead: the same
background prober thread from §6/R5.4 already polls tailnet state on an interval — it also
maintains a `{ip: hostname}` cache with a short TTL (proposed: same interval as the health
probe, 30s). The per-connection check reads that cache; on a cache miss (a peer not seen in the
last refresh — plausibly a newly-joined legitimate node), it triggers one synchronous
refresh-then-recheck before refusing, so a real new peer isn't bounced by stale cache alone, but
a repeated miss (already refreshed, still not found) refuses immediately without re-shelling out
every time.
Acceptance test: a request whose source IP doesn't appear in the cached peer
list (simulated by pointing the allowlist check at a fixture peer list that excludes the test's
own connecting address) is refused, and a request counter/log confirms zero bytes of the body
were read before refusal.

## 5. No audio at rest (S3, R5.5)

Design: the request handler reads the body directly into an in-memory `bytes`/`numpy.ndarray`
(`self.rfile.read(content_length)` into a buffer, never `tempfile`, never a path anywhere in
the call stack from socket-accept to `transcribe_ex` return). On any exception mid-request
(bad audio, engine error, client disconnect), the handler's cleanup path is "let the buffer be
garbage-collected" — there is deliberately no cleanup-a-file step because there is never a file.

**S3 — no-audio-at-rest test.** Design: `strace -e trace=open,openat,creat` (or a Python-level
monkeypatch of `open`/`tempfile.*` that fails the test if called) wrapped around a real request
through the handler; assert zero file-open calls occur anywhere in the served-request code
path. This needs to be a real behavioral test (something that would actually fail if a future
change added a stray `tempfile.NamedTemporaryFile`), not a code-reading assertion.

**H1 (folded in) — bounded input.** Even on a trusted tailnet, the handler rejects any request
whose `Content-Length` exceeds a sane max-utterance bound (proposed: 60s of float32 16kHz mono
audio, ~3.8MB, generously above any real dictation length) with `413 Payload Too Large`, checked
against the header before `rfile.read()` is called — never after reading an unbounded body into
memory first.

R5.5 (no store-and-forward) is the same property under failure: an unreachable-preferred-host
or timed-out call never queues the audio buffer anywhere for retry — the caller either gets a
response now or falls back to local now; there is no third state.

## 6. Failure modes (R5.1–R5.4)

**R5.1 — silent local fallback.**
Design: the client's `transcribe_ex` wrapper (new: `speakeasy/engine/remote.py`, implementing
the same `Engine` protocol as `ParakeetEngine`/`WhisperEngine`) tries the preferred remote host
first if `preferred_host() != "self"`; on any connection-level failure (refused, DNS/tailnet
resolution failure, timeout — see R5.2), it falls back to a local engine instance transparently
and logs (not raises) the fallback.
Acceptance test (C2 — corrected; the original version of this test had the two cases backwards):
**(a)** remote port not listening at all → the kernel returns `ECONNREFUSED` immediately, so
fallback must be observably fast (asserted via wall-clock, not just "eventually succeeds") —
this does NOT exercise the deadline/timeout path, only the immediate-refusal path. **(b)** a
fixture socket that accepts the TCP connection but never sends a response → this is what
actually exercises R5.2's deadline: fallback happens only once `deadline_s` elapses, not before
and not much after. Both (a) and (b) must return a valid `TranscriptionResult` with no exception
reaching the caller, and both must log the fallback with which case occurred.

**R5.2 — hard per-utterance deadline.**
Design (C1 — corrected to match the requirements document's stated default): before issuing the remote call,
the client computes `deadline_s = 1.5 * this_client_hosts_own_local_p95_ms(clip_duration) /
1000` from its *own* settings record (port-spec §4 shape) — **p95, not p50** (the earlier draft
used p50 with no stated rationale for deviating from the requirements document; p95 is correct because p50 as
a baseline would make roughly half of all normal local runs look "slow" relative to it, which
is the wrong reference point for a fallback trigger — p95 already absorbs the local host's own
ordinary variance).
Clip-length lookup (Q2 resolved): **linear-extrapolate beyond the longest measured band, do not
clamp to it.** The measured numbers are near-linear in clip length (this box: 212/320/479ms at
3/5/8s ≈ 60ms/s) — clamping under-budgets long utterances and would cause spurious fallbacks on
exactly the longer dictations where remote offload matters most. Fit (or simply linearly
interpolate/extrapolate between the two nearest measured points) rather than snapping to the
nearest band. Adding a ~15s measured band to the benchmark to confirm the slope is worthwhile
follow-up, not required before implementation.
The remote HTTP call is issued with `deadline_s` as its socket/read timeout. On timeout, cancel
(close the socket) and fall through to R5.1's local path immediately — never a second, longer
wait.
Acceptance test: a remote host that responds correctly but after `deadline_s + 200ms` never has
its late response injected (see R5.3) and the client's own local fallback result is what
actually gets used, and this is observably faster than "wait for the slow response" would have
been (a wall-clock assertion, not just a correctness one).

**R5.3 — duplicate-injection guard.**
**SATISFIED BY CONSTRUCTION (ADJ-SPK-021), not by the mechanism originally designed here.**
The implementation (`speakeasy/engine/remote.py:RemoteFallbackEngine`) is sequential, not
hedged: it blocks on the remote call with `timeout=deadline_s`, and on any failure — including
a timeout — closes that connection and falls back to local, all within the same synchronous
call, in the same thread, before returning exactly one result to the caller
(`speakeasy/app.py`'s `Controller` sees one `Engine.transcribe()` call return one value; it
never runs two paths concurrently for one utterance). Once a socket read times out, the
connection is closed and abandoned — there is no path for a "late-arriving remote response" to
reach the JSON-decode step at all, because there's no live socket left for it to arrive on.
The original design below (a set/dict of in-flight ids with a resolved flag, discarding a
loser) presupposes a **hedged/concurrent-race** architecture, which this isn't. The
acceptance test below is accordingly **vacuous in this design** — kept here for the record,
not as a live requirement. The requirement **REACTIVATES in full** if a future revision moves
to hedged requests or streaming (§7 already scopes streaming out of this revision); any spec
touching either must re-cite R5.3 and this note.

The utterance-id check that does exist in `_call_remote` (rejects a response whose id doesn't
match what was sent) is a different, narrower thing: it guards against a wrong/stale response
on a connection, not a race between two concurrently-running paths — worth keeping distinct
from the vacuous mechanism below, per ADJ-SPK-021.

*Original design (superseded, kept for history):* the caller (whatever's above the engine
abstraction — `speakeasy/app.py`'s Controller) tracks in-flight utterance ids in a small
set/dict with a resolved flag. Whichever result (local fallback or a late-arriving remote
response) reaches the injection step first for a given utterance id wins and marks it
resolved; the loser is discarded on arrival (checked before injection, not before the network
call — the response still gets read and dropped, not left to leak a socket).
*Original acceptance test (vacuous, see above):* simulate both a local-fallback result and a
late remote result for the same utterance id arriving in each of the two possible orders;
assert exactly one injection happens in both orderings, and the discarded result is logged,
not silently dropped without a trace. The operative tests for the actual design are
`test_timeout_falls_back_to_local_r5_1_case_b` and
`test_mismatched_utterance_id_in_response_falls_back_never_trusted` in
`tests/test_remote_engine.py`.

**R5.4 — lightweight probe, cache-only on the hot path.**
Design: `GET /health` on the same listener (same bind/ACL rules as §4 — this is not a separate,
less-guarded endpoint) returns `{"protocol_version": 1, "engine": {...}, "status": "ok"}` with
no audio processing at all. A background thread (started once at app launch, not per-dictation)
probes every host in the ranking list on an interval (proposed: 30s; backs off on repeated
failure rather than hammering an offline laptop) and writes results into the cache §2's
`preferred_host()` reads. A probe failure updates the cache to "unreachable" for that host;
`preferred_host()` on the next call reads the updated cache and demotes accordingly. Nothing on
the dictation hot path ever waits on this thread.
Acceptance test: killing (not gracefully stopping) the preferred remote host mid-session, the
*next* dictation after the following probe interval uses local fallback with no added latency
on that dictation specifically (the probe absorbed the discovery cost in the background, not
on the user's utterance).

## 7. Explicit non-goals (S4, §6 Pi exclusion)

- S4: Tailscale Funnel and any port-forward/NAT rule are never configured by this service or
  its install instructions. This is a documented non-goal, not an oversight — the spec's own
  install docs (when written) must say so explicitly, matching the pattern R-01(a) set.
- Always-on single-board host (requirements §6): while R-02 holds, it appears in no ranking config, no probe list,
  no allowlist. This spec's code has no Pi-specific branch to remove later — it simply never
  mentions it, so releasing R-02 is purely an operator config change (adding a ranking entry +
  ACL entry), not a code change.
- Streaming transport: out of scope for this revision (per your ADJ-SPK-007 note) — the
  request/response shape above is deliberately simple; a streaming revision is a separate
  spec if ever wanted.

## Decisions (resolved by ADJ-SPK-011; no longer open)

1. **Port 7420** — approved as proposed. Operator may override the port; the bind interface
   itself is never configurable (see §4).
2. **Clip-length lookup beyond the measured bands — linear-extrapolate, don't clamp.** Folded
   into §6/R5.2 above.
3. **`speakeasy/engine/remote.py` is a wrapper, not a sibling engine class.** It composes
   whichever local engine is configured (`create_engine`'s existing return value) and adds the
   remote-attempt-then-fallback behavior around it — R5.1's fallback needs a real local engine
   instance regardless, so this is one object, not two. Implementation note: the `Engine`
   Protocol in `speakeasy/engine/__init__.py` currently declares only `transcribe()`, not
   `transcribe_ex()` — extend the Protocol to declare both as part of this work, so the
   wrapper's contract (and every engine's conformance to it) is typed rather than incidental.
