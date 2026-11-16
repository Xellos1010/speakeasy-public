"""Multi-host inference ranking (docs/superpowers/specs/2026-08-18-network-service.md §2)
and the shared background prober that keeps both the reachability cache (R5.4, drives
preferred_host()) and the tailnet peer-identity cache (H2, drives the service's ACL check)
warm without ever blocking the dictation hot path on a live network call.
"""

import http.client
import subprocess
import threading
import time
from typing import Callable

SELF = "self"
HEALTH_CHECK_TIMEOUT_S = 2.0


def make_http_health_check(host_addresses: dict[str, str],
                            connection_cls=http.client.HTTPConnection,
                            timeout: float = HEALTH_CHECK_TIMEOUT_S) -> Callable[[str], bool]:
    """Real health_check_fn for TailnetProber: GET /health on the configured address
    for `host`, reachable iff it responds 200 within `timeout`. Any connection-level
    failure (refused, timeout, DNS/tailnet resolution failure) means unreachable --
    this never raises, matching R5.4's "a probe failure only demotes the cache"."""

    def check(host: str) -> bool:
        address = host_addresses.get(host)
        if not address:
            return False
        addr_host, _, port_str = address.partition(":")
        port = int(port_str) if port_str else 7420
        conn = connection_cls(addr_host, port, timeout=timeout)
        try:
            conn.request("GET", "/health")
            response = conn.getresponse()
            response.read()
            return response.status == 200
        except Exception:
            return False
        finally:
            conn.close()

    return check


def load_ranking(configured: list[str]) -> list[str]:
    """R2.1/R2.4: self is always implicitly last, even if the config omits it —
    the list can never rank self out entirely, so a fallback always exists."""
    ranking = list(configured)
    if SELF not in ranking:
        ranking.append(SELF)
    return ranking


class ReachabilityCache:
    """Thread-safe {host: bool} cache. A host with no entry yet (never probed) is
    treated as NOT reachable — R5.4: "reachability comes from a cheap probe, never
    assumed from config" — so a freshly-configured ranking entry doesn't get treated
    as reachable before any probe has actually succeeded."""

    def __init__(self):
        self._lock = threading.Lock()
        self._state: dict[str, bool] = {}

    def get(self, host: str) -> bool:
        with self._lock:
            return self._state.get(host, False)

    def set(self, host: str, reachable: bool) -> None:
        with self._lock:
            self._state[host] = reachable


def preferred_host(ranking: list[str], cache: ReachabilityCache) -> str:
    """R2.2/R2.3: highest-ranked currently-reachable entry, reading the cache only —
    never probes, never blocks. `self` is always reachable by definition."""
    for host in ranking:
        if host == SELF or cache.get(host):
            return host
    return SELF  # unreachable, but load_ranking() guarantees SELF is always present


class PeerIdentityCache:
    """H2: {ip: hostname} for the service's ACL check, refreshed by the same prober
    interval as the reachability cache instead of shelling out to `tailscale status
    --json` on every connection. `refresh_on_miss` lets a genuinely new peer avoid
    being bounced by a merely-stale cache without paying the subprocess cost on every
    single connection, including repeated misses for an actually-disallowed peer."""

    def __init__(self, refresh_fn: Callable[[], dict[str, str]]):
        self._refresh_fn = refresh_fn
        self._lock = threading.Lock()
        self._peers: dict[str, str] = {}

    def refresh(self) -> None:
        peers = self._refresh_fn()
        with self._lock:
            self._peers = dict(peers)

    def hostname_for(self, ip: str, refresh_on_miss: bool = True) -> str | None:
        with self._lock:
            hostname = self._peers.get(ip)
        if hostname is not None or not refresh_on_miss:
            return hostname
        self.refresh()
        with self._lock:
            return self._peers.get(ip)


def default_tailscale_peers() -> dict[str, str]:
    """Real refresh_fn for PeerIdentityCache: `tailscale status --json`'s Peer map,
    keyed by each peer's first TailscaleIPs entry -> its HostName."""
    import json

    out = subprocess.run(["tailscale", "status", "--json"], capture_output=True,
                          text=True, timeout=5, check=True).stdout
    data = json.loads(out)
    peers = {}
    for peer in data.get("Peer", {}).values():
        ips = peer.get("TailscaleIPs") or []
        if ips:
            peers[ips[0]] = peer.get("HostName", "")
    return peers


class TailnetProber:
    """Background thread (one per running service/client instance) that keeps
    ReachabilityCache and PeerIdentityCache warm on an interval, so preferred_host()
    and the ACL check both stay purely cache-reads on their hot paths.

    Backs off on repeated failure for a given host (R5.4: "backs off on repeated
    failure rather than hammering an offline laptop") rather than probing every
    host every interval regardless of recent results.
    """

    def __init__(self, remote_hosts: list[str],
                 health_check_fn: Callable[[str], bool],
                 peer_cache: PeerIdentityCache,
                 reachability_cache: ReachabilityCache,
                 interval_s: float = 30.0,
                 max_backoff_multiplier: int = 8,
                 time_fn: Callable[[], float] = time.monotonic,
                 sleep_fn: Callable[[float], None] = time.sleep):
        self._remote_hosts = remote_hosts
        self._health_check_fn = health_check_fn
        self._peer_cache = peer_cache
        self._reachability_cache = reachability_cache
        self._interval_s = interval_s
        self._max_backoff_multiplier = max_backoff_multiplier
        self._time = time_fn
        self._sleep = sleep_fn
        self._consecutive_failures: dict[str, int] = {h: 0 for h in remote_hosts}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def probe_once(self) -> None:
        self._peer_cache.refresh()
        for host in self._remote_hosts:
            reachable = self._health_check_fn(host)
            self._reachability_cache.set(host, reachable)
            if reachable:
                self._consecutive_failures[host] = 0
            else:
                self._consecutive_failures[host] += 1

    def _backoff_multiplier(self, host: str) -> int:
        return min(1 + self._consecutive_failures.get(host, 0), self._max_backoff_multiplier)

    def _run(self) -> None:
        next_probe_at: dict[str, float] = {h: 0.0 for h in self._remote_hosts}
        while not self._stop.is_set():
            now = self._time()
            self._peer_cache.refresh()
            for host in self._remote_hosts:
                if now < next_probe_at[host]:
                    continue
                reachable = self._health_check_fn(host)
                self._reachability_cache.set(host, reachable)
                if reachable:
                    self._consecutive_failures[host] = 0
                else:
                    self._consecutive_failures[host] += 1
                next_probe_at[host] = now + self._interval_s * self._backoff_multiplier(host)
            self._sleep(min(self._interval_s, 1.0))

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
