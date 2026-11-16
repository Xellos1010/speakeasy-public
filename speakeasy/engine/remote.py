"""Remote-with-local-fallback engine wrapper (docs/superpowers/specs/2026-08-18-network-service.md
§1, §6 R5.1/R5.2) — composes whichever local engine is configured (Q3 ruling: a wrapper, not a
third sibling engine class, since the fallback needs a real local instance regardless) and adds
a preferred-remote-host attempt with a hard per-utterance deadline before falling back.
"""

import http.client
import json
import logging
import socket
import time
import uuid
from typing import Callable

import numpy as np

from speakeasy.engine import Engine, TranscriptionResult
from speakeasy.ranking import ReachabilityCache, preferred_host, SELF

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
DEADLINE_MULTIPLIER = 1.5  # C1: applied to local p95, not p50 (bridge's stated default)


def estimate_local_p95_ms(measured_p95_ms: dict, clip_duration_s: float) -> float:
    """Q2 ruling: linear-extrapolate beyond the measured bands rather than clamping to
    the longest one -- the measured numbers are near-linear in clip length, and clamping
    would under-budget exactly the longer dictations where remote offload matters most.

    `measured_p95_ms` keys look like "3s_clip", "5s_clip", "8s_clip" (port-spec §4 shape).
    A single measured point degenerates to "use that value for everything" (no slope to fit).
    """
    points = []
    for key, value in measured_p95_ms.items():
        duration_s = float(key.split("s_clip")[0])
        points.append((duration_s, float(value)))
    points.sort()
    if not points:
        raise ValueError("measured_p95_ms is empty -- no local benchmark to estimate from")
    if len(points) == 1:
        return points[0][1]
    durations = np.array([p[0] for p in points])
    values = np.array([p[1] for p in points])
    slope, intercept = np.polyfit(durations, values, 1)
    return float(slope * clip_duration_s + intercept)


class RemoteCallFailed(Exception):
    """Any reason the remote attempt didn't produce a usable result -- refused,
    timed out, version mismatch, malformed response. Always caught internally by
    transcribe_ex(); never allowed to reach the caller (R5.1: silent fallback)."""


class RemoteFallbackEngine:
    """Engine protocol implementation: tries the currently-preferred remote host (per
    the ranking's reachability cache) first, falls back to a local engine instance on
    any failure or deadline breach. Never raises for a remote-side problem -- the
    caller always gets a valid TranscriptionResult, from wherever it actually came.
    """

    def __init__(self, local_engine: Engine, ranking: list[str],
                 reachability_cache: ReachabilityCache,
                 host_addresses: dict[str, str],
                 local_settings_record: dict,
                 connection_cls=http.client.HTTPConnection,
                 clock: Callable[[], float] = time.perf_counter,
                 uuid_fn: Callable[[], str] = lambda: str(uuid.uuid4())):
        self._local_engine = local_engine
        self._ranking = ranking
        self._reachability_cache = reachability_cache
        self._host_addresses = host_addresses
        self._local_settings_record = local_settings_record
        self._connection_cls = connection_cls
        self._clock = clock
        self._uuid_fn = uuid_fn

    def transcribe(self, samples: np.ndarray, sample_rate: int = 16000) -> str:
        return self.transcribe_ex(samples, sample_rate=sample_rate).text

    def transcribe_ex(self, samples: np.ndarray, sample_rate: int = 16000) -> TranscriptionResult:
        target = preferred_host(self._ranking, self._reachability_cache)
        if target == SELF:
            return self._local_engine.transcribe_ex(samples, sample_rate=sample_rate)

        clip_duration_s = len(samples) / sample_rate
        deadline_s = self._deadline_for(clip_duration_s)
        try:
            return self._call_remote(target, samples, sample_rate, deadline_s)
        except RemoteCallFailed as e:
            logger.info("remote transcribe on %s failed (%s); falling back to local", target, e)
            return self._local_engine.transcribe_ex(samples, sample_rate=sample_rate)

    def _deadline_for(self, clip_duration_s: float) -> float:
        p95_ms = estimate_local_p95_ms(
            self._local_settings_record.get("measured_p95_ms", {}), clip_duration_s)
        return DEADLINE_MULTIPLIER * p95_ms / 1000.0

    def _call_remote(self, target: str, samples: np.ndarray, sample_rate: int,
                      deadline_s: float) -> TranscriptionResult:
        address = self._host_addresses.get(target)
        if not address:
            raise RemoteCallFailed(f"no address configured for host {target!r}")
        host, _, port_str = address.partition(":")
        port = int(port_str) if port_str else 7420

        utterance_id = self._uuid_fn()
        body = samples.astype(np.float32).tobytes()
        headers = {
            "X-Speakeasy-Utterance-Id": utterance_id,
            "X-Speakeasy-Protocol-Version": str(PROTOCOL_VERSION),
            "X-Speakeasy-Sample-Rate": str(sample_rate),
            "Content-Type": "application/octet-stream",
        }

        conn = self._connection_cls(host, port, timeout=deadline_s)
        try:
            conn.request("POST", "/transcribe", body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read()
        except (OSError, socket.timeout, http.client.HTTPException) as e:
            raise RemoteCallFailed(f"connection-level failure to {target}: {e}") from e
        finally:
            conn.close()

        if response.status != 200:
            raise RemoteCallFailed(f"{target} returned HTTP {response.status}: {raw[:200]!r}")

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RemoteCallFailed(f"malformed JSON from {target}: {e}") from e

        if data.get("utterance_id") != utterance_id:
            # Not our request's response (or a stale/duplicate) -- never trust it.
            raise RemoteCallFailed(f"{target} returned a mismatched utterance_id")

        if "text" not in data:
            # SF2: a 200 response missing "text" must NOT silently become an empty
            # transcription -- that's worse than no transcription at all. Treat it
            # as a failure and fall back to local, same as any other bad response.
            raise RemoteCallFailed(f"{target} returned a response with no 'text' field")

        return TranscriptionResult(text=data["text"], avg_logprob=data.get("avg_logprob"))
