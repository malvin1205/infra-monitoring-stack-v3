"""TargetPoller: Prometheus-native alert poller and transition engine.

Encapsulates:
- Target health polling and state transition evaluation (TargetDown, SlowResponse)
- Cold-start outage detection vs. recovery transitions
- Instance-scoped maintenance window suppression and auto-resume
- Orphaned alert auto-reconciliation
- Worker heartbeat and self-health reporting
"""
import logging
import os
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

try:
    from config import (
        ALERTNAME_SLOW_RESPONSE,
        ALERTNAME_TARGET_DOWN,
        DEFAULT_SLOW_RESPONSE_THRESHOLD_MS,
        SCRAPE_INTERVAL_SECONDS,
    )
    from storage import (
        IncidentRepository,
        SlowThresholdRepository,
        json_store,
    )
    from core.monitoring import classify_scrape_failure, _outage_past_grace
    import core.monitoring.queries as prom_queries
    import core.monitoring.state as monitoring_state
    from core.alerts import (
        _LAST_WEBHOOK_AT,
        active_incident_list,
        get_active_maintenance,
        load_maintenance_windows,
        record_alert_event,
    )
    from core.workers.aggregator import (
        _AVAIL_AGGREGATOR_WORKER_ID,
        AVAIL_AGGREGATE_INTERVAL_SECONDS,
        AVAIL_BUCKET_RETENTION_SECONDS,
        _LAST_AGGREGATOR_TICK,
        AvailabilityAggregator,
        _aggregate_availability_cycle,
        _availability_aggregation_windows,
        availability_aggregator,
        start_availability_aggregator,
    )
except (ImportError, ValueError):
    from alarm.config import (
        ALERTNAME_SLOW_RESPONSE,
        ALERTNAME_TARGET_DOWN,
        DEFAULT_SLOW_RESPONSE_THRESHOLD_MS,
        SCRAPE_INTERVAL_SECONDS,
    )
    from alarm.storage import (
        IncidentRepository,
        SlowThresholdRepository,
        json_store,
    )
    from alarm.core.monitoring import classify_scrape_failure, _outage_past_grace
    import alarm.core.monitoring.queries as prom_queries
    import alarm.core.monitoring.state as monitoring_state
    from alarm.core.alerts import (
        _LAST_WEBHOOK_AT,
        active_incident_list,
        get_active_maintenance,
        load_maintenance_windows,
        record_alert_event,
    )
    from alarm.core.workers.aggregator import (
        _AVAIL_AGGREGATOR_WORKER_ID,
        AVAIL_AGGREGATE_INTERVAL_SECONDS,
        AVAIL_BUCKET_RETENTION_SECONDS,
        _LAST_AGGREGATOR_TICK,
        AvailabilityAggregator,
        _aggregate_availability_cycle,
        _availability_aggregation_windows,
        availability_aggregator,
        start_availability_aggregator,
    )

logger = logging.getLogger("infrawatch.poller")

ALERT_POLL_INTERVAL_SECONDS = float(os.environ.get("ALERT_POLL_INTERVAL", "15"))
WEBHOOK_ACTIVE_WINDOW_SECONDS = 120
SLOW_RESPONSE_DEBOUNCE_N = int(os.environ.get("SLOW_RESPONSE_DEBOUNCE_N", "3"))


# ── Adapters ──────────────────────────────────────────────────────────────────

class PrometheusQueryAdapter:
    """Default adapter fetching metrics directly from core.monitoring.queries,
    with transparent fallback for test monkeypatches on app.py.
    """

    def __init__(self, queries_module=None):
        self._queries = queries_module or prom_queries

    def fetch_all_probe_metrics(self, cache_ttl: float = 3.0, timeout: Optional[float] = None):
        app_mod = sys.modules.get("app") or sys.modules.get("alarm.app")
        if app_mod and hasattr(app_mod, "fetch_all_probe_metrics"):
            app_fn = getattr(app_mod, "fetch_all_probe_metrics")
            if app_fn != self._queries.fetch_all_probe_metrics:
                return app_fn(cache_ttl=cache_ttl, timeout=timeout)
        return self._queries.fetch_all_probe_metrics(cache_ttl=cache_ttl, timeout=timeout)

    def fetch_down_since_prom_map(self):
        app_mod = sys.modules.get("app") or sys.modules.get("alarm.app")
        if app_mod and hasattr(app_mod, "fetch_down_since_prom_map"):
            app_fn = getattr(app_mod, "fetch_down_since_prom_map")
            if app_fn != self._queries.fetch_down_since_prom_map:
                return app_fn()
        return self._queries.fetch_down_since_prom_map()


class MonitoringStateAdapter:
    """Default adapter fetching monitored target state directly from core.monitoring.state,
    with transparent fallback for test monkeypatches on app.py.
    """

    def __init__(self, state_module=None):
        self._state = state_module or monitoring_state

    def get_monitored_instances(self) -> List[str]:
        app_mod = sys.modules.get("app") or sys.modules.get("alarm.app")
        if app_mod and hasattr(app_mod, "get_monitored_instances"):
            app_fn = getattr(app_mod, "get_monitored_instances")
            if app_fn != self._state.get_monitored_instances:
                return app_fn()
        return self._state.get_monitored_instances()


# ── Pure Transition Functions ─────────────────────────────────────────────────

def compute_state_transitions(success_map: Dict[str, Any], prev_state: Dict[str, str]) -> Tuple[List[Tuple[str, bool]], Dict[str, str]]:
    """Pure function: given instance->probe_success value map and the last
    known state per instance, return (transitions, updated_state).
    - If target is first observed UP: seeds baseline state 'up', emits no transition (no false alert).
    - If target is first observed DOWN (cold-start outage): seeds state 'down' and emits (inst, False)
      so the active outage is immediately recorded instead of being silently ignored.
    - If target was already known: emits transition only when state changes.
    """
    transitions = []
    new_state = dict(prev_state)
    for inst, val in success_map.items():
        is_up = str(val) in ('1', '1.0')
        prev = prev_state.get(inst)
        new_state[inst] = 'up' if is_up else 'down'
        if prev is None:
            if not is_up:
                transitions.append((inst, False))
        elif (prev == 'up') != is_up:
            transitions.append((inst, is_up))
    return transitions, new_state


def compute_slow_response_transitions(
    readings: Dict[str, Tuple[bool, Optional[float]]],
    prev_state: Dict[str, Dict[str, Any]],
    thresholds: Dict[str, float],
    debounce_n: int = SLOW_RESPONSE_DEBOUNCE_N,
) -> Tuple[List[Tuple[str, bool]], Dict[str, Dict[str, Any]]]:
    """Pure function: given instance->(is_up, response_time_ms) readings and
    the last debounce state per instance, return (transitions, updated_state).

    Requires `debounce_n` consecutive over-threshold samples to fire, and
    `debounce_n` consecutive under-threshold samples to resolve.
    A target going DOWN supersedes "slow": streaks reset and a firing
    SlowResponse resolves immediately.
    """
    transitions = []
    new_state = {}
    for inst, (is_up, rt_ms) in readings.items():
        st = dict(prev_state.get(inst) or {'consec_slow': 0, 'consec_fast': 0, 'firing': False})

        if not is_up:
            st['consec_slow'] = 0
            st['consec_fast'] = 0
            if st['firing']:
                st['firing'] = False
                transitions.append((inst, False))
            new_state[inst] = st
            continue

        threshold = thresholds.get(inst, DEFAULT_SLOW_RESPONSE_THRESHOLD_MS)
        is_slow_now = rt_ms is not None and rt_ms > threshold
        if is_slow_now:
            st['consec_slow'] += 1
            st['consec_fast'] = 0
        else:
            st['consec_fast'] += 1
            st['consec_slow'] = 0

        if not st['firing'] and st['consec_slow'] >= debounce_n:
            st['firing'] = True
            transitions.append((inst, True))
        elif st['firing'] and st['consec_fast'] >= debounce_n:
            st['firing'] = False
            transitions.append((inst, False))

        new_state[inst] = st
    return transitions, new_state


# ── TargetPoller Module ───────────────────────────────────────────────────────

class TargetPoller:
    """Encapsulates target polling, transition computation, SlowResponse debouncing,
    maintenance window evaluation, and self-health reporting.
    """

    def __init__(
        self,
        query_adapter=None,
        state_adapter=None,
        alert_sink: Optional[Callable] = None,
        incident_repo=None,
        active_incident_provider: Optional[Callable] = None,
        maintenance_loader: Optional[Callable] = None,
        maintenance_checker: Optional[Callable] = None,
        slow_threshold_repo=None,
        poll_interval_sec: Optional[float] = None,
        webhook_active_window_sec: Optional[float] = None,
        debounce_n: Optional[int] = None,
        state: Optional[Dict[str, str]] = None,
        slow_state: Optional[Dict[str, Dict[str, Any]]] = None,
        maintenance_active_prev: Optional[Set[str]] = None,
        last_tick_ref: Optional[List[float]] = None,
        last_webhook_at_ref: Optional[List[float]] = None,
    ):
        self.query_adapter = query_adapter or PrometheusQueryAdapter()
        self.state_adapter = state_adapter or MonitoringStateAdapter()
        self.alert_sink = alert_sink or record_alert_event
        self.incident_repo = incident_repo or IncidentRepository
        self.active_incident_provider = active_incident_provider or active_incident_list
        self.maintenance_loader = maintenance_loader or load_maintenance_windows
        self.maintenance_checker = maintenance_checker or get_active_maintenance
        self.slow_threshold_repo = slow_threshold_repo or SlowThresholdRepository

        self.poll_interval_sec = (
            poll_interval_sec if poll_interval_sec is not None else ALERT_POLL_INTERVAL_SECONDS
        )
        self.webhook_active_window_sec = (
            webhook_active_window_sec if webhook_active_window_sec is not None else WEBHOOK_ACTIVE_WINDOW_SECONDS
        )
        self.debounce_n = debounce_n if debounce_n is not None else SLOW_RESPONSE_DEBOUNCE_N

        self._poller_state = state if state is not None else {}
        self._slow_poller_state = slow_state if slow_state is not None else {}
        self._maintenance_active_prev = (
            maintenance_active_prev if maintenance_active_prev is not None else set()
        )
        self._last_tick_ref = last_tick_ref
        self._last_webhook_at_ref = (
            last_webhook_at_ref if last_webhook_at_ref is not None else _LAST_WEBHOOK_AT
        )
        self._last_tick = 0.0

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started = False

    def seed_state(self) -> None:
        """Seed 'down' baseline from currently-firing incidents in SQLite so backend
        restarts don't trigger duplicate 'went offline' alerts.
        """
        try:
            for a in self.active_incident_provider():
                inst = a.get('instance')
                if inst:
                    self._poller_state[inst] = 'down'
        except Exception:
            logger.exception("TargetPoller: failed to seed state from active incidents")

    def reconcile_orphaned_alerts(self, monitored_instances: List[str]) -> None:
        """Auto-resolves poller-owned alerts (TargetDown, SlowResponse) whose
        instance no longer exists in the monitored set at all.
        """
        try:
            orphaned = [
                a for a in self.active_incident_provider()
                if a.get('name') in (ALERTNAME_TARGET_DOWN, ALERTNAME_SLOW_RESPONSE)
                and a.get('instance') not in monitored_instances
            ]
            for a in orphaned:
                inst = a.get('instance')
                if inst:
                    self._slow_poller_state.pop(inst, None)
                self.alert_sink(
                    name=a.get('name'),
                    severity=a.get('severity', 'critical'),
                    instance=inst,
                    summary=f"{inst} auto-resolved (no longer monitored)",
                    job='',
                    event_time=time.time(),
                    is_now_firing=False,
                    receiver="prometheus-poller-reconcile",
                    key=a.get('key') or f"{a.get('name')}|{inst}",
                )
        except Exception:
            logger.exception("TargetPoller: reconcile orphaned alerts failed")

    def poll_once(self, now: Optional[float] = None) -> None:
        """Execute one evaluation cycle over all monitored targets."""
        curr_time = now if now is not None else time.time()
        self._last_tick = curr_time
        if self._last_tick_ref is not None:
            self._last_tick_ref[0] = curr_time

        if curr_time - self._last_webhook_at_ref[0] < self.webhook_active_window_sec:
            return  # Alertmanager delivered a webhook recently — don't double-fire

        try:
            instances = self.state_adapter.get_monitored_instances()
        except Exception:
            logger.exception("TargetPoller: get_monitored_instances failed")
            return

        if not instances:
            return

        self.reconcile_orphaned_alerts(instances)

        try:
            success_map, duration_map, status_code_map = self.query_adapter.fetch_all_probe_metrics()
        except Exception:
            logger.exception("TargetPoller: fetch_all_probe_metrics failed")
            return

        if not success_map:
            return  # Prometheus unreachable this tick

        windows = self.maintenance_loader()
        active_now = {inst for inst in instances if self.maintenance_checker(inst, windows=windows)}
        just_ended = self._maintenance_active_prev - active_now

        if just_ended:
            try:
                slow_firing_instances = {
                    a['instance']
                    for a in self.incident_repo.get_active_incidents()
                    if a.get('name') == ALERTNAME_SLOW_RESPONSE
                }
            except Exception:
                slow_firing_instances = set()

            for inst in just_ended:
                self._poller_state[inst] = 'up'
                self._slow_poller_state[inst] = {
                    'consec_slow': 0,
                    'consec_fast': 0,
                    'firing': inst in slow_firing_instances,
                }
                val = success_map.get(inst)
                if val is not None and str(val) in ('1', '1.0'):
                    self.alert_sink(
                        name=ALERTNAME_TARGET_DOWN,
                        severity="critical",
                        instance=inst,
                        summary=f"{inst} recovered (confirmed after maintenance window ended)",
                        job="blackbox",
                        event_time=curr_time,
                        is_now_firing=False,
                        receiver="prometheus-poller",
                        key=f"{ALERTNAME_TARGET_DOWN}|{inst}",
                    )

        self._maintenance_active_prev.clear()
        self._maintenance_active_prev.update(active_now)

        scoped = {inst: v for inst, v in success_map.items() if inst in instances and inst not in active_now}
        transitions, new_state = compute_state_transitions(scoped, self._poller_state)

        # Debounce down-transitions
        if any(not is_up for _, is_up in transitions):
            try:
                down_since_map = self.query_adapter.fetch_down_since_prom_map()
            except Exception:
                down_since_map = {}
            held = []
            for inst, is_up in transitions:
                if not is_up and not _outage_past_grace(down_since_map.get(inst), curr_time):
                    new_state[inst] = self._poller_state.get(inst, 'up')
                else:
                    held.append((inst, is_up))
            transitions = held

        self._poller_state.update(new_state)

        for inst, is_up in transitions:
            lat = duration_map.get(inst)
            try:
                latency_ms = round(float(lat) * 1000, 1) if lat is not None else None
            except (TypeError, ValueError):
                latency_ms = None

            http_status_code = status_code_map.get(inst)
            last_error = None
            if is_up:
                summary = f"{inst} recovered"
            else:
                classification = classify_scrape_failure('down', '', http_status_code)
                summary = f"{inst} is unreachable ({classification['category']})"
                last_error = classification['detail']

            self.alert_sink(
                name=ALERTNAME_TARGET_DOWN,
                severity="critical",
                instance=inst,
                summary=summary,
                job="blackbox",
                event_time=curr_time,
                is_now_firing=(not is_up),
                receiver="prometheus-poller",
                key=f"{ALERTNAME_TARGET_DOWN}|{inst}",
                latency_ms=latency_ms,
                http_status_code=http_status_code,
                last_error=last_error,
            )

        # SlowResponse evaluation
        readings = {}
        for inst, val in scoped.items():
            lat = duration_map.get(inst)
            try:
                rt_ms = round(float(lat) * 1000, 1) if lat is not None else None
            except (TypeError, ValueError):
                rt_ms = None
            readings[inst] = (str(val) in ('1', '1.0'), rt_ms)

        try:
            slow_thresholds = self.slow_threshold_repo.get_all()
        except Exception:
            slow_thresholds = {}

        slow_transitions, new_slow_state = compute_slow_response_transitions(
            readings, self._slow_poller_state, slow_thresholds, debounce_n=self.debounce_n
        )
        self._slow_poller_state.update(new_slow_state)

        for inst, is_now_firing in slow_transitions:
            threshold = slow_thresholds.get(inst, DEFAULT_SLOW_RESPONSE_THRESHOLD_MS)
            rt_ms = readings[inst][1]
            summary = (
                f"{inst} response time degraded ({rt_ms}ms > {threshold}ms threshold)"
                if is_now_firing
                else f"{inst} response time recovered"
            )
            self.alert_sink(
                name=ALERTNAME_SLOW_RESPONSE,
                severity="warning",
                instance=inst,
                summary=summary,
                job="blackbox",
                event_time=curr_time,
                is_now_firing=is_now_firing,
                receiver="prometheus-poller",
                key=f"{ALERTNAME_SLOW_RESPONSE}|{inst}",
                latency_ms=rt_ms,
            )

    def get_health(self, now: Optional[float] = None) -> Dict[str, Any]:
        """Compute and return health status dictionary for /health reporting."""
        curr_time = now if now is not None else time.time()
        poller_enabled = os.environ.get("DISABLE_ALERT_POLLER") != "1"
        tick_age = (curr_time - self._last_tick) if self._last_tick > 0.0 else None
        webhook_recent = (curr_time - self._last_webhook_at_ref[0]) < self.webhook_active_window_sec
        alarm_service_ok = (
            webhook_recent
            or not poller_enabled
            or (tick_age is not None and tick_age < self.poll_interval_sec * 3)
        )
        return {
            "ok": alarm_service_ok,
            "last_tick_seconds_ago": round(tick_age, 1) if tick_age is not None else None,
        }

    def start(self) -> bool:
        """Start the background poller daemon thread."""
        if self._started:
            return False
        self._started = True
        self._stop_event.clear()

        def _loop():
            self.seed_state()
            while not self._stop_event.is_set():
                try:
                    self.poll_once()
                except Exception as e:
                    logger.error(f"Target poller error: {e}", exc_info=True)
                self._stop_event.wait(self.poll_interval_sec)

        self._thread = threading.Thread(target=_loop, name="target-poller", daemon=True)
        self._thread.start()
        logger.info(f"Target poller started (interval: {self.poll_interval_sec}s)")
        return True

    def stop(self) -> None:
        """Stop the background poller daemon thread."""
        self._stop_event.set()
        self._started = False


# ── Module Defaults & Backward Compatibility Re-exports ───────────────────────

_poller_state = {}
_slow_poller_state = {}
_maintenance_active_prev = set()
_LAST_POLLER_TICK = [0.0]

target_poller = TargetPoller(
    state=_poller_state,
    slow_state=_slow_poller_state,
    maintenance_active_prev=_maintenance_active_prev,
    last_tick_ref=_LAST_POLLER_TICK,
)


def _seed_poller_state():
    """Backward compatibility wrapper."""
    target_poller.seed_state()


def _reconcile_orphaned_alerts(monitored_instances):
    """Backward compatibility wrapper."""
    target_poller.reconcile_orphaned_alerts(monitored_instances)


def _poll_targets_once():
    """Backward compatibility wrapper."""
    target_poller.poll_once()


def start_alert_poller():
    """Backward compatibility entry point."""
    return target_poller.start()


def _reconcile_status_json_into_sqlite():
    """One-shot at boot: SQLite `incidents` is the source of truth for
    active-alert state (audit F1), but an ops restore that brings back only
    status.json (the denormalized cache) would otherwise lose the active
    incident entirely. Import any firing alert in status.json that SQLite
    doesn't already have as a firing incident, so recovery still works from
    either artefact. No-op when status.json is absent/empty (the common case).
    """
    try:
        status_data = json_store.load_json(json_store.STATUS_FILE, None)
        if not isinstance(status_data, dict):
            return
        alerts = status_data.get("alerts") or []
        if not alerts:
            return
        try:
            active_keys = {a.get("key") for a in IncidentRepository.get_active_incidents()}
        except Exception:
            active_keys = set()
        imported = 0
        for a in alerts:
            key = a.get("key") or f"{a.get('name')}|{a.get('instance')}"
            if key in active_keys:
                continue
            IncidentRepository.record_alert_event(
                name=a.get("name", "Unknown"),
                severity=a.get("severity", "critical"),
                instance=a.get("instance", "-"),
                summary=a.get("summary", ""),
                job=a.get("job", ""),
                event_time=float(a.get("time", time.time())),
                is_now_firing=True,
                key=key,
            )
            imported += 1
        if imported:
            logger.info("Recovered %d active incident(s) from status.json into SQLite on boot", imported)
    except Exception:
        logger.exception("status.json -> SQLite active-incident reconcile failed")
