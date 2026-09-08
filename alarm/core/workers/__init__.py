"""Background workers: alert poller and availability aggregator.
"""
from .poller import (
    ALERT_POLL_INTERVAL_SECONDS, WEBHOOK_ACTIVE_WINDOW_SECONDS, SLOW_RESPONSE_DEBOUNCE_N,
    AVAIL_AGGREGATE_INTERVAL_SECONDS, AVAIL_BUCKET_RETENTION_SECONDS,
    _AVAIL_AGGREGATOR_WORKER_ID, _LAST_POLLER_TICK, _LAST_AGGREGATOR_TICK,
    _poller_state, _slow_poller_state, _maintenance_active_prev,
    compute_state_transitions, compute_slow_response_transitions,
    _availability_aggregation_windows, _seed_poller_state, _reconcile_orphaned_alerts,
    _poll_targets_once, _aggregate_availability_cycle,
    _reconcile_status_json_into_sqlite, start_alert_poller, start_availability_aggregator,
)
