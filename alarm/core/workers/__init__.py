"""Background workers module: Prometheus alert poller and availability aggregator.

Exposes deep modules TargetPoller and AvailabilityAggregator along with singleton
instances and lifecycle helpers.
"""
from .poller import (
    TargetPoller,
    PrometheusQueryAdapter,
    MonitoringStateAdapter,
    target_poller,
    compute_state_transitions,
    compute_slow_response_transitions,
    start_alert_poller,
    ALERT_POLL_INTERVAL_SECONDS,
    WEBHOOK_ACTIVE_WINDOW_SECONDS,
    SLOW_RESPONSE_DEBOUNCE_N,
    _poller_state,
    _slow_poller_state,
    _maintenance_active_prev,
    _LAST_POLLER_TICK,
    _seed_poller_state,
    _reconcile_orphaned_alerts,
    _poll_targets_once,
    _reconcile_status_json_into_sqlite,
)

from .aggregator import (
    AvailabilityAggregator,
    availability_aggregator,
    start_availability_aggregator,
    AVAIL_AGGREGATE_INTERVAL_SECONDS,
    AVAIL_BUCKET_RETENTION_SECONDS,
    _AVAIL_AGGREGATOR_WORKER_ID,
    _LAST_AGGREGATOR_TICK,
    _availability_aggregation_windows,
    _aggregate_availability_cycle,
)

__all__ = [
    # Deep module classes & adapters
    "TargetPoller",
    "PrometheusQueryAdapter",
    "MonitoringStateAdapter",
    "AvailabilityAggregator",
    # Singletons
    "target_poller",
    "availability_aggregator",
    # Lifecycle entry points
    "start_alert_poller",
    "start_availability_aggregator",
    # Pure functions
    "compute_state_transitions",
    "compute_slow_response_transitions",
    # Constants & state (backward compatibility)
    "ALERT_POLL_INTERVAL_SECONDS",
    "WEBHOOK_ACTIVE_WINDOW_SECONDS",
    "SLOW_RESPONSE_DEBOUNCE_N",
    "AVAIL_AGGREGATE_INTERVAL_SECONDS",
    "AVAIL_BUCKET_RETENTION_SECONDS",
    "_AVAIL_AGGREGATOR_WORKER_ID",
    "_LAST_POLLER_TICK",
    "_LAST_AGGREGATOR_TICK",
    "_poller_state",
    "_slow_poller_state",
    "_maintenance_active_prev",
    "_seed_poller_state",
    "_reconcile_orphaned_alerts",
    "_poll_targets_once",
    "_availability_aggregation_windows",
    "_aggregate_availability_cycle",
    "_reconcile_status_json_into_sqlite",
]
