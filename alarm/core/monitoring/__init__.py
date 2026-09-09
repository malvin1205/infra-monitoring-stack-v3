"""Monitoring domain: Prometheus client, state engine, metrics queries, and primitives.
"""
# 1. Models and deep engine
from .models import (
    FleetQuery,
    FleetSummary,
    FleetState,
)
from .engine import (
    FleetStateEngine,
    fleet_state_engine,
    _derive_probe_readings,
)

# 2. Primitives
from .primitives import (
    parse_alert_timestamp,
    alert_key,
    TARGET_HOST_RE,
    _BLOCKED_TARGET_HOSTS,
    is_valid_target,
    matches_job_filter,
    normalize_target,
    _parse_epoch_ts,
    _sane_epoch,
    _earliest_outage_start,
    classify_scrape_failure,
    _extract_host,
    find_node_exporter_status,
    OUTAGE_GRACE_SECONDS,
    _outage_past_grace,
    _PROM_DURATION_RE,
    _parse_prom_duration_sec,
)

# 3. Client and queries
from .client import (
    _DEFAULT_PROM_URL,
    load_endpoints,
    _ENDPOINTS_CACHE,
    LAST_WORKING_PROMETHEUS_URL,
    PROMETHEUS_CACHE,
    PROMETHEUS_CACHE_LOCK,
    PROMETHEUS_CACHE_TTL_DEFAULT,
    _SHARED_EXECUTOR,
    _FAILED_CANDIDATES,
    _FAILED_CANDIDATES_LOCK,
    _FETCH_LOCKS,
    _AVAILABILITY_CACHE,
    _AVAILABILITY_CACHE_LOCK,
    _AVAILABILITY_FLIGHT_LOCKS,
    _AVAILABILITY_FLIGHT_LOCKS_GUARD,
    _fetch_lock_for,
    _avail_flight_lock_for,
    clear_availability_cache,
    _maybe_prune_cache,
    _SAFE_CANDIDATE_CACHE,
    _cached_is_safe_endpoint_url,
    _filter_safe_candidates,
    fetch_url,
    fetch_prometheus_json,
)
from .queries import (
    fetch_prom_query_map,
    fetch_prom_range_map,
    fetch_down_since_prom_map,
    fetch_all_probe_metrics,
)

# 4. State facade
from .state import (
    build_canonical_monitoring_state,
    get_instance_job_map,
    get_instance_cadence_map,
    get_monitored_instances,
)

__all__ = [
    # Domain models & engine
    "FleetQuery",
    "FleetSummary",
    "FleetState",
    "FleetStateEngine",
    "fleet_state_engine",
    # State facade
    "_derive_probe_readings",
    "build_canonical_monitoring_state",
    "get_instance_job_map",
    "get_instance_cadence_map",
    "get_monitored_instances",
    # Queries & client
    "fetch_prom_query_map",
    "fetch_prom_range_map",
    "fetch_down_since_prom_map",
    "fetch_all_probe_metrics",
    "fetch_url",
    "fetch_prometheus_json",
    "load_endpoints",
    "clear_availability_cache",
    # Primitives
    "matches_job_filter",
    "classify_scrape_failure",
    "OUTAGE_GRACE_SECONDS",
    "_outage_past_grace",
]
