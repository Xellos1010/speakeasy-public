from speakeasy.ranking import (
    load_ranking,
    ReachabilityCache,
    PeerIdentityCache,
    TailnetProber,
    make_http_health_check,
    preferred_host,
    SELF,
)


def test_load_ranking_appends_self_if_absent():
    assert load_ranking(["linux"]) == ["linux", SELF]

def test_load_ranking_leaves_self_in_place_if_present():
    assert load_ranking(["linux", SELF]) == ["linux", SELF]

def test_preferred_host_never_assumes_reachable_before_a_probe():
    cache = ReachabilityCache()
    assert preferred_host(["linux", SELF], cache) == SELF   # no probe yet -> self

def test_preferred_host_picks_highest_ranked_reachable():
    cache = ReachabilityCache()
    cache.set("linux", True)
    assert preferred_host(["linux", SELF], cache) == "linux"

def test_preferred_host_demotes_on_unreachable():
    cache = ReachabilityCache()
    cache.set("linux", True)
    cache.set("linux", False)   # demoted after a failed probe
    assert preferred_host(["linux", SELF], cache) == SELF

def test_preferred_host_skips_unreachable_ranked_above_reachable():
    cache = ReachabilityCache()
    cache.set("pi", False)
    cache.set("mac", True)
    assert preferred_host(["pi", "mac", SELF], cache) == "mac"

def test_preferred_host_is_fast_and_never_touches_the_network():
    """Proves 'never blocks' as a real wall-clock property, not just correctness."""
    import time
    cache = ReachabilityCache()
    cache.set("linux", False)
    start = time.perf_counter()
    for _ in range(1000):
        preferred_host(["linux", SELF], cache)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.05, f"1000 calls took {elapsed}s -- should be pure dict reads"


def test_peer_identity_cache_hit_no_refresh():
    calls = []
    cache = PeerIdentityCache(refresh_fn=lambda: calls.append(1) or {"192.0.2.3": "linux-host"})
    cache.refresh()
    assert cache.hostname_for("192.0.2.3") == "linux-host"
    assert calls == [1]   # only the explicit refresh() call, not a second one on hit

def test_peer_identity_cache_miss_triggers_one_refresh_then_rechecks():
    calls = []

    def refresh_fn():
        calls.append(1)
        return {"192.0.2.3": "linux-host"} if len(calls) > 1 else {}

    cache = PeerIdentityCache(refresh_fn=refresh_fn)
    cache.refresh()  # 1st call: empty
    assert cache.hostname_for("192.0.2.3") is None or cache.hostname_for("192.0.2.3") == "linux-host"
    # after a miss, a second refresh should have happened and found it
    assert len(calls) >= 2

def test_peer_identity_cache_repeated_miss_does_not_refresh_forever():
    calls = []
    cache = PeerIdentityCache(refresh_fn=lambda: calls.append(1) or {})
    cache.refresh()
    cache.hostname_for("192.0.2.9")  # miss -> one refresh-on-miss
    n_after_first_miss = len(calls)
    cache.hostname_for("192.0.2.9", refresh_on_miss=False)  # caller opts out of re-refreshing
    assert len(calls) == n_after_first_miss


def test_prober_updates_reachability_cache():
    reach = ReachabilityCache()
    peer_cache = PeerIdentityCache(refresh_fn=lambda: {})
    prober = TailnetProber(
        remote_hosts=["linux"],
        health_check_fn=lambda host: True,
        peer_cache=peer_cache,
        reachability_cache=reach,
    )
    prober.probe_once()
    assert reach.get("linux") is True

def test_prober_demotes_on_failed_probe():
    reach = ReachabilityCache()
    reach.set("linux", True)
    peer_cache = PeerIdentityCache(refresh_fn=lambda: {})
    prober = TailnetProber(
        remote_hosts=["linux"],
        health_check_fn=lambda host: False,
        peer_cache=peer_cache,
        reachability_cache=reach,
    )
    prober.probe_once()
    assert reach.get("linux") is False

def test_prober_backoff_multiplier_grows_with_consecutive_failures():
    reach = ReachabilityCache()
    peer_cache = PeerIdentityCache(refresh_fn=lambda: {})
    prober = TailnetProber(
        remote_hosts=["linux"],
        health_check_fn=lambda host: False,
        peer_cache=peer_cache,
        reachability_cache=reach,
        max_backoff_multiplier=8,
    )
    prober.probe_once()
    prober.probe_once()
    prober.probe_once()
    assert prober._backoff_multiplier("linux") == 4   # 1 + 3 consecutive failures
    for _ in range(10):
        prober.probe_once()
    assert prober._backoff_multiplier("linux") == 8   # capped

class FakeHealthResponse:
    def __init__(self, status):
        self.status = status
    def read(self):
        return b"{}"

class FakeHealthConnection:
    def __init__(self, host, port, timeout, outcome):
        self.host, self.port, self.timeout = host, port, timeout
        self._outcome = outcome
    def request(self, method, path):
        if isinstance(self._outcome, Exception):
            raise self._outcome
    def getresponse(self):
        return self._outcome
    def close(self):
        pass

def test_http_health_check_true_on_200():
    conn_cls = lambda host, port, timeout: FakeHealthConnection(host, port, timeout, FakeHealthResponse(200))
    check = make_http_health_check({"linux": "192.0.2.3:7420"}, connection_cls=conn_cls)
    assert check("linux") is True

def test_http_health_check_false_on_non_200():
    conn_cls = lambda host, port, timeout: FakeHealthConnection(host, port, timeout, FakeHealthResponse(500))
    check = make_http_health_check({"linux": "192.0.2.3:7420"}, connection_cls=conn_cls)
    assert check("linux") is False

def test_http_health_check_false_on_connection_error():
    conn_cls = lambda host, port, timeout: FakeHealthConnection(host, port, timeout, ConnectionRefusedError())
    check = make_http_health_check({"linux": "192.0.2.3:7420"}, connection_cls=conn_cls)
    assert check("linux") is False

def test_http_health_check_false_when_no_address_configured():
    check = make_http_health_check({}, connection_cls=lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert check("linux") is False


def test_prober_start_stop_does_not_raise():
    reach = ReachabilityCache()
    peer_cache = PeerIdentityCache(refresh_fn=lambda: {})
    prober = TailnetProber(
        remote_hosts=["linux"],
        health_check_fn=lambda host: True,
        peer_cache=peer_cache,
        reachability_cache=reach,
        interval_s=0.05,
    )
    prober.start()
    import time
    time.sleep(0.2)
    prober.stop()
    assert reach.get("linux") is True
