"""Prometheus HTTP access layer for InfraWatch.

Everything that speaks the Prometheus HTTP API and the machinery around it:
the registered-endpoint state cache, the per-PromQL response cache, single-
flight coalescing locks, the failed-candidate circuit breaker, the shared
worker pool, the DNS-rebinding hot-path re-check, and endpoint failover in
`fetch_prometheus_json`.

Extracted verbatim from app.py — behaviour is unchanged. app.py re-imports
every public name from here (including the caches/locks the tests reach into)
so `import app as alarm_app; alarm_app.PROMETHEUS_CACHE` keeps working. The
thin per-shape adapters that parse a Prometheus response into an
`{instance: value}` map (`fetch_prom_query_map` etc.) stay in app.py next to
their callers.
"""
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.request import urlopen, Request
from urllib.error import URLError

try:
    import ssrf
    from storage import EndpointRepository, AvailabilityBucketRepository
except ImportError:  # pragma: no cover - import style shim, mirrors app.py
    from alarm import ssrf
    from alarm.storage import EndpointRepository, AvailabilityBucketRepository

logger = logging.getLogger("infrawatch")

# The one compiled-in fallback. Used to be a 4-entry topology-guessing list
# (host.docker.internal / localhost / 127.0.0.1) — removed: /api/endpoints
# already lets an operator register exactly the Prometheus URL their
# deployment needs, so guessing at container-networking conventions no
# longer earns its keep. This single default covers the common case (a
# compose service literally named "prometheus") without guessing further.
# Blank/unset PROMETHEUS_URL => no default endpoint: the deployment runs with
# an empty endpoint list until the operator adds one in the UI. Only a
# non-blank value seeds and acts as the failover fallback.
_DEFAULT_PROM_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090").strip()

_ENDPOINTS_CACHE = {"ts": 0.0, "data": None}
_ENDPOINTS_CACHE_TTL = 2.0  # SQLite rarely changes; avoid a query on every hot-path call

# EndpointRepository (SQLite) is the sole source of truth — endpoints.json
# used to be read first (when present) and written on every mutation as a
# parallel copy; every mutating route below now writes only through the
# repository, and load_endpoints() reads only from it (short-TTL cached).
def load_endpoints():
    now = time.time()
    cached = _ENDPOINTS_CACHE["data"]
    if cached is not None and now - _ENDPOINTS_CACHE["ts"] < _ENDPOINTS_CACHE_TTL:
        return cached

    try:
        data = EndpointRepository.load_endpoints_state(_DEFAULT_PROM_URL)
    except Exception:
        data = ({"active": _DEFAULT_PROM_URL, "endpoints": [_DEFAULT_PROM_URL]}
                if _DEFAULT_PROM_URL else {"active": None, "endpoints": []})

    _ENDPOINTS_CACHE["data"] = data
    _ENDPOINTS_CACHE["ts"] = now
    return data

LAST_WORKING_PROMETHEUS_URL = None
PROMETHEUS_CACHE = {}
PROMETHEUS_CACHE_LOCK = threading.Lock()
PROMETHEUS_CACHE_TTL_DEFAULT = 8.0  # backend cache window for identical PromQL responses (5-15s band)

# Shared thread pool executor to prevent continuous thread creation/destruction
_SHARED_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix="infrawatch-worker")

# Separate, small pool for the SSRF DNS-revalidation check (_cached_is_safe_endpoint_url
# below). socket.getaddrinfo() has no portable timeout, so a hung/slow resolver
# can tie up a worker for well past our 1s wait — kept off _SHARED_EXECUTOR so
# that can never queue behind (or starve) actual Prometheus query submission.
_DNS_CHECK_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="infrawatch-dnscheck")

# Cooldown circuit-breaker for unreachable candidate endpoints (prevents timeout cascades)
_FAILED_CANDIDATES = {}
_FAILED_CANDIDATES_LOCK = threading.Lock()
_FAILED_CANDIDATE_TTL = 5.0

# Single-flight locks: when several requests need the same (endpoint, PromQL)
# result at once (e.g. /instances and /api/availability both polling
# probe_success around the same tick), only the first actually calls
# Prometheus — the rest wait and reuse its result instead of duplicating it.
_FETCH_LOCKS = {}
_FETCH_LOCKS_GUARD = threading.Lock()

# Query params like custom time ranges / per-target history embed a live
# timestamp, so their cache keys are effectively unique each call. Bound the
# resulting cache/lock growth with a periodic sweep instead of a per-request
# scan.
_CACHE_LAST_PRUNE = [0.0]
_CACHE_PRUNE_INTERVAL = 30.0
_CACHE_MAX_AGE = 120.0

# Availability result cache & single-flight coalescing
_AVAILABILITY_CACHE = {}
_AVAILABILITY_CACHE_LOCK = threading.Lock()
_AVAILABILITY_FLIGHT_LOCKS = {}
_AVAILABILITY_FLIGHT_LOCKS_GUARD = threading.Lock()

def _fetch_lock_for(key):
    with _FETCH_LOCKS_GUARD:
        lock = _FETCH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _FETCH_LOCKS[key] = lock
        return lock

def _avail_flight_lock_for(key):
    with _AVAILABILITY_FLIGHT_LOCKS_GUARD:
        lock = _AVAILABILITY_FLIGHT_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _AVAILABILITY_FLIGHT_LOCKS[key] = lock
        return lock

def clear_availability_cache(clear_db=True):
    with _AVAILABILITY_CACHE_LOCK:
        _AVAILABILITY_CACHE.clear()
    with _AVAILABILITY_FLIGHT_LOCKS_GUARD:
        _AVAILABILITY_FLIGHT_LOCKS.clear()
    if clear_db:
        try:
            AvailabilityBucketRepository.clear_all_buckets()
        except Exception:
            pass

def _maybe_prune_cache(now):
    if now - _CACHE_LAST_PRUNE[0] < _CACHE_PRUNE_INTERVAL:
        return
    _CACHE_LAST_PRUNE[0] = now
    with PROMETHEUS_CACHE_LOCK:
        stale_keys = [k for k, (ts, _d, _b) in PROMETHEUS_CACHE.items() if now - ts > _CACHE_MAX_AGE]
        for k in stale_keys:
            PROMETHEUS_CACHE.pop(k, None)
    with _FETCH_LOCKS_GUARD:
        for k in stale_keys:
            _FETCH_LOCKS.pop(k, None)
        if len(_FETCH_LOCKS) > 500:
            unlocked = [k for k, lock in list(_FETCH_LOCKS.items()) if not lock.locked() and k not in PROMETHEUS_CACHE]
            for k in unlocked:
                _FETCH_LOCKS.pop(k, None)
    with _AVAILABILITY_CACHE_LOCK:
        stale_avail = [k for k, (ts, _d) in _AVAILABILITY_CACHE.items() if now - ts > _CACHE_MAX_AGE]
        for k in stale_avail:
            _AVAILABILITY_CACHE.pop(k, None)
    with _AVAILABILITY_FLIGHT_LOCKS_GUARD:
        for k in stale_avail:
            _AVAILABILITY_FLIGHT_LOCKS.pop(k, None)
        if len(_AVAILABILITY_FLIGHT_LOCKS) > 500:
            unlocked_avail = [k for k, lock in list(_AVAILABILITY_FLIGHT_LOCKS.items()) if not lock.locked() and k not in _AVAILABILITY_CACHE]
            for k in unlocked_avail:
                _AVAILABILITY_FLIGHT_LOCKS.pop(k, None)

# is_safe_endpoint_url() does a real (blocking) DNS resolution — fine once,
# at /api/endpoints registration time, but fetch_prometheus_json is called
# for every distinct PromQL query, many times a minute. A short TTL cache
# keeps the DNS-rebinding re-check cheap on the hot path while still closing
# the gap within one TTL window of a hostname's record changing (registration
# alone left it trusted forever).
_SAFE_CANDIDATE_CACHE = {}
_SAFE_CANDIDATE_CACHE_LOCK = threading.Lock()
_SAFE_CANDIDATE_CACHE_TTL = 20.0
# A hostname that doesn't resolve at all (e.g. a Compose-internal name whose
# container isn't up yet) can take several seconds to fail via the system
# resolver, with no way to bound socket.getaddrinfo's own timeout portably.
# Run it off-thread with a hard deadline so a slow/hanging lookup can't stall
# an HTTP request or poll cycle; on timeout, fail open — same treatment
# is_safe_endpoint_url already gives an unresolvable hostname, since a name
# that's merely slow to resolve is not the DNS-rebinding case this guards
# against (a rebound name resolves fine, just to a different, blocked IP).
_SAFE_CHECK_TIMEOUT_SEC = 1.0

def _cached_is_safe_endpoint_url(url):
    now = time.time()
    with _SAFE_CANDIDATE_CACHE_LOCK:
        cached = _SAFE_CANDIDATE_CACHE.get(url)
        if cached and now - cached[0] < _SAFE_CANDIDATE_CACHE_TTL:
            return cached[1], cached[2]
    try:
        future = _DNS_CHECK_EXECUTOR.submit(ssrf.is_safe_endpoint_url, url)
        ok, reason = future.result(timeout=_SAFE_CHECK_TIMEOUT_SEC)
    except TimeoutError:
        # Fail open but do NOT cache it — a slow/hanging lookup is transient;
        # caching "safe" for the full TTL would let a deliberately-stalled
        # DNS response buy an attacker a trusted window instead of being
        # re-checked on the very next call like the comment above promises.
        return True, "DNS check timed out; treated as safe (re-checked next cycle)"
    except Exception:
        return True, "DNS check errored; treated as safe (re-checked next cycle)"
    with _SAFE_CANDIDATE_CACHE_LOCK:
        _SAFE_CANDIDATE_CACHE[url] = (now, ok, reason)
    return ok, reason

def _filter_safe_candidates(urls):
    """Re-validate each URL's currently-resolved IP right before it's used as a
    poll target. is_safe_endpoint_url() is also run once at endpoint
    registration time (/api/endpoints), but a hostname that resolved to a
    public IP then can be repointed via DNS to a loopback/link-local/metadata
    address afterwards (DNS rebinding) — the background poller and aggregator
    would otherwise keep trusting that first check forever. Re-running the
    same check (TTL-cached, see above) closes that gap for anything that came
    from user/operator input: active endpoint, other saved endpoints, and
    PROMETHEUS_URL (_DEFAULT_PROM_URL) are all revalidated here."""
    safe = []
    for u in urls:
        if not u:
            continue
        ok, reason = _cached_is_safe_endpoint_url(u)
        if ok:
            safe.append(u)
        else:
            logger.warning("Skipping Prometheus candidate %s: %s", u, reason)
    return safe

def fetch_url(url, timeout=1.5):
    try:
        req = Request(url, headers={"User-Agent": "InfraWatch/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return resp.read().decode('utf-8', errors='ignore')
    except URLError as e:
        if hasattr(e, 'read'):
            try:
                return e.read().decode('utf-8', errors='ignore')
            except Exception:
                pass
    except Exception:
        pass
    return None

def fetch_prometheus_json(path, use_cache=True, cache_ttl=None, timeout=None):
    global LAST_WORKING_PROMETHEUS_URL, PROMETHEUS_CACHE
    if cache_ttl is None:
        cache_ttl = PROMETHEUS_CACHE_TTL_DEFAULT
    now = time.time()
    _maybe_prune_cache(now)

    endpoints_data = load_endpoints()
    active_url = endpoints_data.get("active")

    cache_key = f"{active_url}:{path}"
    if use_cache:
        with PROMETHEUS_CACHE_LOCK:
            if cache_key in PROMETHEUS_CACHE:
                cached_ts, cached_data, cached_base = PROMETHEUS_CACHE[cache_key]
                if now - cached_ts < cache_ttl:
                    return cached_data, cached_base

    with _fetch_lock_for(cache_key):
        # Re-check: whoever held the lock before us may have just populated it.
        if use_cache:
            with PROMETHEUS_CACHE_LOCK:
                if cache_key in PROMETHEUS_CACHE:
                    cached_ts, cached_data, cached_base = PROMETHEUS_CACHE[cache_key]
                    if time.time() - cached_ts < cache_ttl:
                        return cached_data, cached_base

        # Primary candidates: active endpoint + last known working endpoint
        primary_candidates = []
        if active_url:
            primary_candidates.append(active_url)
        if LAST_WORKING_PROMETHEUS_URL and LAST_WORKING_PROMETHEUS_URL not in primary_candidates:
            primary_candidates.append(LAST_WORKING_PROMETHEUS_URL)
        primary_candidates = _filter_safe_candidates(primary_candidates)

        # Try primary candidates first (2.5s default timeout)
        for base_url in primary_candidates:
            with _FAILED_CANDIDATES_LOCK:
                failed_at = _FAILED_CANDIDATES.get(base_url, 0)
                if now - failed_at < _FAILED_CANDIDATE_TTL and base_url != LAST_WORKING_PROMETHEUS_URL:
                    continue

            req_timeout = 2.5 if timeout is None else timeout
            raw = fetch_url(f"{base_url.rstrip('/')}{path}", timeout=req_timeout)
            if raw:
                try:
                    data = json.loads(raw)
                    # Even if status is error (e.g. invalid query syntax), the server is alive
                    LAST_WORKING_PROMETHEUS_URL = base_url
                    with _FAILED_CANDIDATES_LOCK:
                        _FAILED_CANDIDATES.pop(base_url, None)
                    if data.get('status') == 'success' or 'data' in data:
                        if use_cache:
                            with PROMETHEUS_CACHE_LOCK:
                                PROMETHEUS_CACHE[cache_key] = (time.time(), data, base_url)
                    return data, base_url
                except Exception:
                    with _FAILED_CANDIDATES_LOCK:
                        _FAILED_CANDIDATES[base_url] = time.time()
                    continue
            else:
                with _FAILED_CANDIDATES_LOCK:
                    _FAILED_CANDIDATES[base_url] = time.time()

        # If primary candidates fail (or none exist), fallback to other registered
        # endpoints and _DEFAULT_PROM_URL (PROMETHEUS_URL env, or the
        # prometheus:9090 compose default) — the one an operator actually
        # configured for this deployment, ahead of the other saved endpoints.
        fallback_candidates = []
        if _DEFAULT_PROM_URL and _DEFAULT_PROM_URL not in primary_candidates:
            fallback_candidates.append(_DEFAULT_PROM_URL)
        for ep in endpoints_data.get("endpoints", []):
            if ep not in primary_candidates and ep not in fallback_candidates:
                fallback_candidates.append(ep)
        fallback_candidates = _filter_safe_candidates(fallback_candidates)

        for base_url in fallback_candidates:
            with _FAILED_CANDIDATES_LOCK:
                failed_at = _FAILED_CANDIDATES.get(base_url, 0)
                if now - failed_at < _FAILED_CANDIDATE_TTL:
                    continue

            req_timeout = 0.8 if timeout is None else timeout
            raw = fetch_url(f"{base_url.rstrip('/')}{path}", timeout=req_timeout)
            if raw:
                try:
                    data = json.loads(raw)
                    LAST_WORKING_PROMETHEUS_URL = base_url
                    with _FAILED_CANDIDATES_LOCK:
                        _FAILED_CANDIDATES.pop(base_url, None)
                    if data.get('status') == 'success' or 'data' in data:
                        if use_cache:
                            with PROMETHEUS_CACHE_LOCK:
                                PROMETHEUS_CACHE[cache_key] = (time.time(), data, base_url)
                    return data, base_url
                except Exception:
                    with _FAILED_CANDIDATES_LOCK:
                        _FAILED_CANDIDATES[base_url] = time.time()
                    continue
            else:
                with _FAILED_CANDIDATES_LOCK:
                    _FAILED_CANDIDATES[base_url] = time.time()
        return None, None
