"""Background workers: the Prometheus-native alert poller and the availability
aggregator — their pure transition/window logic, their per-cycle bodies, the
thread launchers and the process-wide lifecycle guards.

Extracted verbatim from app.py (Phase 2 step 9) — behaviour is unchanged.
app.py re-imports every public name (constants, the `_poller_state` /
`_slow_poller_state` / `_maintenance_active_prev` dicts, the `_LAST_*_TICK`
heartbeats, `start_alert_poller` / `start_availability_aggregator`) so
`import app as alarm_app; alarm_app._poller_state` and the tests' direct
mutation of it keep working, and so does /health.

The cycle bodies resolve their Prometheus-fetch and lease collaborators
through `_ctx()` (a lazy lookup of the already-imported app module, never an
import-time dependency) so a test's `patch.object(alarm_app, 'fetch_...')`
is still honoured after the move. The DISABLE_ALERT_POLLER / DISABLE_
AVAILABILITY_AGGREGATOR boot gate stays in app.py.
"""
import os
import sys
import math
import time
import uuid
import threading
import logging

try:
    from config import DEFAULT_SLOW_RESPONSE_THRESHOLD_MS, SCRAPE_INTERVAL_SECONDS
    from config import ALERTNAME_TARGET_DOWN, ALERTNAME_SLOW_RESPONSE
    import json_store
    from storage import (
        IncidentRepository, AvailabilityBucketRepository,
        AggregationLeaseRepository, SlowThresholdRepository,
    )
    from monitoring_primitives import classify_scrape_failure, _outage_past_grace
    from prometheus_client import _SHARED_EXECUTOR
    from alerts import (
        _LAST_WEBHOOK_AT, active_incident_list, record_alert_event,
        load_maintenance_windows, get_active_maintenance,
    )
    from fleet_availability import (
        reconstruct_time_series_intervals, derive_bucket_inputs, estimate_instance_cadence,
    )
except ImportError:
    from alarm.config import DEFAULT_SLOW_RESPONSE_THRESHOLD_MS, SCRAPE_INTERVAL_SECONDS
    from alarm.config import ALERTNAME_TARGET_DOWN, ALERTNAME_SLOW_RESPONSE
    from alarm import json_store
    from alarm.storage import (
        IncidentRepository, AvailabilityBucketRepository,
        AggregationLeaseRepository, SlowThresholdRepository,
    )
    from alarm.monitoring_primitives import classify_scrape_failure, _outage_past_grace
    from alarm.prometheus_client import _SHARED_EXECUTOR
    from alarm.alerts import (
        _LAST_WEBHOOK_AT, active_incident_list, record_alert_event,
        load_maintenance_windows, get_active_maintenance,
    )
    from alarm.fleet_availability import (
        reconstruct_time_series_intervals, derive_bucket_inputs, estimate_instance_cadence,
    )

logger = logging.getLogger("infrawatch")


def _ctx():
    """The app module — looked up lazily (never imported at module load) so
    this file stays clear of an import cycle with app.py. The cycle bodies go
    through it for the collaborators the tests rebind on the app module —
    get_monitored_instances, get_instance_job_map, fetch_all_probe_metrics,
    fetch_down_since_prom_map, fetch_prom_query_map, fetch_prom_range_map.
    (Class method patches like AggregationLeaseRepository.acquire_or_renew hit
    the shared storage class object, so those stay imported normally.)"""
    return sys.modules.get("app") or sys.modules.get("alarm.app") or sys.modules.get("__main__")


ALERT_POLL_INTERVAL_SECONDS = float(os.environ.get("ALERT_POLL_INTERVAL", "15"))
WEBHOOK_ACTIVE_WINDOW_SECONDS = 120

# SlowResponse (warning-severity, up-but-degraded) config. See
# compute_slow_response_transitions() for the debounce rule this backs.
# DEFAULT_SLOW_RESPONSE_THRESHOLD_MS is defined near the top of this file so
# build_canonical_monitoring_state() can use it for the live grid too (F4).
SLOW_RESPONSE_DEBOUNCE_N = int(os.environ.get("SLOW_RESPONSE_DEBOUNCE_N", "3"))

_poller_state = {}  # instance -> 'up' | 'down', seeded from status.json at startup
_slow_poller_state = {}  # instance -> {'consec_slow', 'consec_fast', 'firing'} for SlowResponse debounce
_maintenance_active_prev = set()  # instance-scoped maintenance windows active as of the last poll tick
_LAST_POLLER_TICK = [0.0]  # heartbeat for /health — set every tick, whether or not it did work

def compute_state_transitions(success_map, prev_state):
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
                # Cold-start / first observation: target is DOWN.
                # Emit transition to establish active incident in status/logs/history.
                transitions.append((inst, False))
        elif (prev == 'up') != is_up:
            transitions.append((inst, is_up))
    return transitions, new_state

def compute_slow_response_transitions(readings, prev_state, thresholds, debounce_n=SLOW_RESPONSE_DEBOUNCE_N):
    """Pure function (same shape as compute_state_transitions): given
    instance->(is_up, response_time_ms) readings and the last debounce state
    per instance, return (transitions, updated_state).

    Response time is naturally noisy — firing/resolving on a single sample
    would flap exactly like the occurrence-counting bug Incident History
    task #1 fixed, just for a new alert type. Requires `debounce_n`
    CONSECUTIVE over-threshold samples to fire, and `debounce_n` consecutive
    under-threshold samples to resolve — one bad or one good sample alone
    changes nothing. A target going DOWN supersedes "slow": streaks reset
    and a firing SlowResponse resolves immediately (being down isn't a
    degraded-but-up condition, it's TargetDown's job).

    thresholds: {instance: threshold_ms}, missing -> caller's global default.
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

def _seed_poller_state():
    # Currently-firing incidents (survived from before a backend restart) seed
    # as 'down' so we don't re-fire a duplicate "went offline" for an outage
    # that's already recorded — only its eventual recovery still needs to be
    # observed and resolved. SQLite is the source of truth (audit F1).
    for a in active_incident_list():
        inst = a.get('instance')
        if inst:
            _poller_state[inst] = 'down'

def _reconcile_orphaned_alerts(monitored_instances):
    """Auto-resolves poller-owned alerts (TargetDown, SlowResponse) whose
    instance no longer exists in the monitored set at all — e.g. removed
    from Prometheus's scrape config entirely, not merely down. Without this,
    such an alert can never be observed recovering (compute_state_transitions
    / compute_slow_response_transitions only look at instances still present
    in success_map/instances) and stays firing forever, permanently pinning
    system status to CRITICAL/WARNING.
    Reads the active set from SQLite (single source of truth, audit F1) so a
    phantom incident that only exists in the DB — not in the status.json
    cache — is still reconciled here. Only touches poller-owned alerts, and
    only runs when `instances` is non-empty (Prometheus reachable) — see call
    site."""
    orphaned = [
        a for a in active_incident_list()
        if a.get('name') in (ALERTNAME_TARGET_DOWN, ALERTNAME_SLOW_RESPONSE) and a.get('instance') not in monitored_instances
    ]
    for a in orphaned:
        _slow_poller_state.pop(a.get('instance'), None)
        record_alert_event(
            name=a.get('name'),
            severity=a.get('severity', 'critical'),
            instance=a.get('instance'),
            summary=f"{a.get('instance')} auto-resolved (no longer monitored)",
            job='',
            event_time=time.time(),
            is_now_firing=False,
            receiver="prometheus-poller-reconcile",
            key=a.get('key') or f"{a.get('name')}|{a.get('instance')}",
        )

def _poll_targets_once():
    c = _ctx()
    _LAST_POLLER_TICK[0] = time.time()

    if time.time() - _LAST_WEBHOOK_AT[0] < WEBHOOK_ACTIVE_WINDOW_SECONDS:
        return  # Alertmanager delivered a webhook recently — it's authoritative, don't double-fire

    instances = c.get_monitored_instances()
    if not instances:
        return

    _reconcile_orphaned_alerts(instances)

    success_map, duration_map, status_code_map = c.fetch_all_probe_metrics()
    if not success_map:
        return  # Prometheus unreachable this tick — never fabricate a transition from no data

    # Instance-scoped maintenance: freeze this instance's tracked state for
    # the window so no transition is recorded, then force one fresh check the
    # moment the window ends — "auto resume monitoring". If it's still down,
    # that's now a real transition (compute_state_transitions sees the forced
    # 'up' vs the real 'down') and a fresh incident is raised; record_alert_
    # event()'s own dedup no-ops it harmlessly if that incident was already
    # firing from before the window.
    #
    # If it instead recovered WHILE under maintenance, compute_state_
    # transitions sees forced-'up' == actual 'up' and never emits an event at
    # all — so a TargetDown incident that started before the window and
    # recovered silently during it would stay "firing" in SQLite forever
    # (the poller never evaluates a maintained instance, so no resolve is
    # ever recorded any other way). Confirm the real state right here instead
    # and explicitly resolve — record_alert_event() no-ops safely if nothing
    # was actually firing.
    # ponytail: job-scoped maintenance is enforced only in record_alert_event()
    # (still no false incident/alarm) — this resume nudge is instance-only;
    # extend to jobs if job-wide maintenance flapping becomes a problem.
    windows = load_maintenance_windows()
    active_now = {inst for inst in instances if get_active_maintenance(inst, windows=windows)}
    just_ended = _maintenance_active_prev - active_now
    if just_ended:
        try:
            _slow_firing_instances = {
                a['instance'] for a in IncidentRepository.get_active_incidents()
                if a.get('name') == ALERTNAME_SLOW_RESPONSE
            }
        except Exception:
            _slow_firing_instances = set()
    for inst in just_ended:
        _poller_state[inst] = 'up'
        # Seed (not just discard) the debounce state to match what SQLite
        # actually has open — popping this entirely would make
        # compute_slow_response_transitions believe nothing was firing for
        # this instance, so it could never observe a firing->resolved
        # transition for a SlowResponse incident that's genuinely still
        # 'firing' in the DB from before the window (same class of bug as
        # TargetDown above, different mechanism: here it's the poller's own
        # bookkeeping forgetting the DB's state, not a forced probe reading
        # matching the real one). Fresh counters either way — a stale streak
        # from right before maintenance shouldn't count toward the debounce.
        _slow_poller_state[inst] = {
            'consec_slow': 0, 'consec_fast': 0,
            'firing': inst in _slow_firing_instances
        }
        val = success_map.get(inst)
        if val is not None and str(val) in ('1', '1.0'):
            record_alert_event(
                name=ALERTNAME_TARGET_DOWN,
                severity="critical",
                instance=inst,
                summary=f"{inst} recovered (confirmed after maintenance window ended)",
                job="blackbox",
                event_time=time.time(),
                is_now_firing=False,
                receiver="prometheus-poller",
                key=f"{ALERTNAME_TARGET_DOWN}|{inst}",
            )
    _maintenance_active_prev.clear()
    _maintenance_active_prev.update(active_now)

    scoped = {inst: v for inst, v in success_map.items() if inst in instances and inst not in active_now}
    transitions, new_state = compute_state_transitions(scoped, _poller_state)

    now = time.time()

    # Debounce down-transitions: don't fire an outage until the target has been
    # down for OUTAGE_GRACE_SECONDS (same gate as build_canonical_monitoring_state).
    # A held instance is left UNLATCHED in _poller_state so the next tick
    # re-checks it — a blip that recovers inside the window never fires.
    if any(not is_up for _, is_up in transitions):
        down_since_map = c.fetch_down_since_prom_map()
        held = []
        for inst, is_up in transitions:
            if not is_up and not _outage_past_grace(down_since_map.get(inst), now):
                new_state[inst] = _poller_state.get(inst, 'up')
            else:
                held.append((inst, is_up))
        transitions = held

    _poller_state.update(new_state)

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
            # Same classifier as /instances — poller has no per-instance
            # lastError (that lives on /api/v1/targets, which this loop
            # doesn't fetch), so this degrades to HTTP-code-based
            # classification or "Unknown", same as any other probe-only target.
            classification = classify_scrape_failure('down', '', http_status_code)
            summary = f"{inst} is unreachable ({classification['category']})"
            last_error = classification['detail']

        record_alert_event(
            name=ALERTNAME_TARGET_DOWN,
            severity="critical",
            instance=inst,
            summary=summary,
            job="blackbox",
            event_time=now,
            is_now_firing=(not is_up),
            receiver="prometheus-poller",
            key=f"{ALERTNAME_TARGET_DOWN}|{inst}",
            latency_ms=latency_ms,
            http_status_code=http_status_code,
            last_error=last_error,
        )

    # SlowResponse (warning-severity): evaluated every tick for every
    # currently-scoped instance, independent of whether TargetDown had a
    # transition — the debounce needs a consecutive-sample count, not just
    # the ticks where something already changed. See
    # compute_slow_response_transitions() for the debounce/threshold rules.
    readings = {}
    for inst, val in scoped.items():
        lat = duration_map.get(inst)
        try:
            rt_ms = round(float(lat) * 1000, 1) if lat is not None else None
        except (TypeError, ValueError):
            rt_ms = None
        readings[inst] = (str(val) in ('1', '1.0'), rt_ms)

    try:
        slow_thresholds = SlowThresholdRepository.get_all()
    except Exception:
        slow_thresholds = {}

    slow_transitions, new_slow_state = compute_slow_response_transitions(
        readings, _slow_poller_state, slow_thresholds
    )
    _slow_poller_state.update(new_slow_state)

    for inst, is_now_firing in slow_transitions:
        threshold = slow_thresholds.get(inst, DEFAULT_SLOW_RESPONSE_THRESHOLD_MS)
        rt_ms = readings[inst][1]
        summary = (
            f"{inst} response time degraded ({rt_ms}ms > {threshold}ms threshold)" if is_now_firing
            else f"{inst} response time recovered"
        )
        record_alert_event(
            name=ALERTNAME_SLOW_RESPONSE,
            severity="warning",
            instance=inst,
            summary=summary,
            job="blackbox",
            event_time=now,
            is_now_firing=is_now_firing,
            receiver="prometheus-poller",
            key=f"{ALERTNAME_SLOW_RESPONSE}|{inst}",
            latency_ms=rt_ms,
        )

def _poller_loop():
    _seed_poller_state()
    while True:
        try:
            _poll_targets_once()
        except Exception as e:
            logger.error(f"Alert poller error: {e}", exc_info=True)
        time.sleep(ALERT_POLL_INTERVAL_SECONDS)

_poller_thread_started = False

def start_alert_poller():
    global _poller_thread_started
    if _poller_thread_started:
        return
    _poller_thread_started = True
    threading.Thread(target=_poller_loop, daemon=True).start()

# ── Background Availability Aggregator ───────────────────────────────────────
_AVAIL_AGGREGATOR_WORKER_ID = f"{os.getpid()}_{uuid.uuid4().hex[:8]}"
AVAIL_AGGREGATE_INTERVAL_SECONDS = 60.0
# Buckets older than this are pruned every aggregator cycle — the archive holds
# nothing older, so no range query can return data past this horizon.
AVAIL_BUCKET_RETENTION_SECONDS = 35 * 86400
_avail_aggregator_started = False
# Heartbeat for /health — set at the top of every aggregator cycle (leader or
# not), so a wedged/crashed aggregator thread is visible instead of silently
# stopping all bucket materialization (audit F5).
_LAST_AGGREGATOR_TICK = [0.0]

def _availability_aggregation_windows(now, latest_end):
    """Time windows the aggregator should (re)aggregate this cycle.

    Backfill (empty/stale store): 6h windows from an hour-aligned start
    `floor(now/3600) - 7d` up to `now`. 21600 and 86400*7 are whole
    multiples of 3600, so every interior boundary stays hour-aligned; only
    the final window ends at the unaligned `now` to cover the in-progress
    hour. An unaligned start would push every boundary mid-hour (06:16:14,
    12:16:14, ...) and, since the per-window loop floors/ceils each window
    to whole hours, two consecutive windows would then both touch the same
    hourly bucket — each seeing only its own slice of that hour and
    overwriting the other, leaving the rest of that hour unaggregated.

    Steady state: just the last completed hour, plus the in-progress hour
    once it is >= 30s old.
    """
    if latest_end is None or latest_end < (now - 86400 * 7):
        windows = []
        cur_t = math.floor(now / 3600.0) * 3600.0 - 86400 * 7
        while cur_t < now:
            next_t = min(cur_t + 21600, now)
            windows.append((cur_t, next_t))
            cur_t = next_t
        return windows

    hour_end = math.floor(now / 3600.0) * 3600.0
    windows = [(hour_end - 3600.0, hour_end)]
    if now - hour_end >= 30.0:
        windows.append((hour_end, now))
    return windows


def _aggregate_availability_cycle():
    """Incremental availability aggregation run executed only by the elected leader worker."""
    c = _ctx()
    _LAST_AGGREGATOR_TICK[0] = time.time()
    is_leader = AggregationLeaseRepository.acquire_or_renew(
        lease_name="avail_aggregator",
        owner_id=_AVAIL_AGGREGATOR_WORKER_ID,
        ttl_sec=AVAIL_AGGREGATE_INTERVAL_SECONDS * 2.5
    )
    if not is_leader:
        return

    now = time.time()
    instance_job_map = c.get_instance_job_map('all')
    monitored = sorted(instance_job_map.keys())
    if not monitored:
        return

    latest_end = AvailabilityBucketRepository.get_latest_bucket_end('all')
    windows_to_aggregate = _availability_aggregation_windows(now, latest_end)

    for w_start, w_end in windows_to_aggregate:
        w_minutes = max(1, int(round((w_end - w_start) / 60.0)))
        step_sec = SCRAPE_INTERVAL_SECONDS
        if w_minutes > 10080:
            step_sec = 300.0
        elif w_minutes > 1440:
            step_sec = 30.0

        at_suffix = f" @ {int(w_end)}"
        queries = {
            'probe_avail': f"avg_over_time(probe_success[{w_minutes}m]{at_suffix}) * 100",
            'up_avail': f"avg_over_time(up[{w_minutes}m]{at_suffix}) * 100",
            'probe_count': f"count_over_time(probe_success[{w_minutes}m]{at_suffix})",
            'up_count': f"count_over_time(up[{w_minutes}m]{at_suffix})",
            'probe_first_ts': f"min_over_time(timestamp(probe_success)[{w_minutes}m:]{at_suffix})",
            'probe_last_ts': f"max_over_time(timestamp(probe_success)[{w_minutes}m:]{at_suffix})",
            'up_first_ts': f"min_over_time(timestamp(up)[{w_minutes}m:]{at_suffix})",
            'up_last_ts': f"max_over_time(timestamp(up)[{w_minutes}m:]{at_suffix})",
            'duration': f"avg_over_time(probe_duration_seconds[{w_minutes}m]{at_suffix}) * 1000",
            'probe_incidents': f"changes(probe_success[{w_minutes}m]{at_suffix})",
            'up_incidents': f"changes(up[{w_minutes}m]{at_suffix})",
        }

        futures = {k: _SHARED_EXECUTOR.submit(c.fetch_prom_query_map, q, 10.0, 15.0) for k, q in queries.items()}
        results = {}
        for k, f in futures.items():
            try:
                results[k] = f.result()
            except Exception:
                results[k] = {}

        # Raw 0/1 sample stream for the exact reconstruction engine. One range
        # query for the whole fleet per window; per instance per hour it is
        # sliced and fed to reconstruct_time_series_intervals(). Step is
        # coarse enough to bound the payload (all instances at once) but fine
        # enough for hour-level accounting — sub-`range_step` blips can be
        # missed here; the live /api/availability path still re-queries fresh
        # and /api/target-history uses a finer step. On any failure the maps
        # come back empty and every instance falls back to the scalar
        # avg_over_time approximation below.
        range_step = 15.0 if w_minutes <= 180 else 30.0
        try:
            probe_range_map = c.fetch_prom_range_map("probe_success", w_start, w_end, range_step, cache_ttl=10.0, timeout=20.0)
        except Exception:
            probe_range_map = {}
        try:
            up_range_map = c.fetch_prom_range_map("up", w_start, w_end, range_step, cache_ttl=10.0, timeout=20.0)
        except Exception:
            up_range_map = {}

        # Same classify-and-merge step as api_availability's materialize
        # path, via derive_bucket_inputs() — see its docstring.
        probe_results_raw = {
            "avail": results.get('probe_avail', {}), "count": results.get('probe_count', {}),
            "first_ts": results.get('probe_first_ts', {}), "last_ts": results.get('probe_last_ts', {}),
            "incidents": results.get('probe_incidents', {}),
        }
        up_results_raw = {
            "avail": results.get('up_avail', {}), "count": results.get('up_count', {}),
            "first_ts": results.get('up_first_ts', {}), "last_ts": results.get('up_last_ts', {}),
            "incidents": results.get('up_incidents', {}),
        }
        merged_maps = derive_bucket_inputs(monitored, probe_results_raw, up_results_raw)
        avail_map = merged_maps["avail"]
        count_map = merged_maps["count"]
        first_ts_map = merged_maps["first_ts"]
        last_ts_map = merged_maps["last_ts"]
        incidents_map = merged_maps["incidents"]

        duration_map = results.get('duration', {})

        observed_cadences = []
        for inst in monitored:
            estimated = estimate_instance_cadence(inst, count_map, first_ts_map, last_ts_map)
            if estimated is not None:
                observed_cadences.append(estimated)
        fleet_median_cadence = (
            sorted(observed_cadences)[len(observed_cadences) // 2]
            if observed_cadences else SCRAPE_INTERVAL_SECONDS
        )

        bucket_records = []
        h_start = math.floor(w_start / 3600.0) * 3600.0
        h_end = math.ceil(w_end / 3600.0) * 3600.0
        num_hours = max(1, int(round((h_end - h_start) / 3600.0)))
        w_duration_sec = float(w_end - w_start)

        for inst in monitored:
            raw_avail = avail_map.get(inst)
            raw_count = count_map.get(inst)
            raw_dur = duration_map.get(inst)
            raw_inc = incidents_map.get(inst)

            latency = 0.0
            if raw_dur is not None:
                try:
                    latency = round(float(raw_dur), 1)
                except (ValueError, TypeError):
                    latency = 0.0

            # Exact path: raw 0/1 samples for this instance (probe_success
            # first, fall back to the `up` series for node/exporter targets).
            samples = probe_range_map.get(inst) or up_range_map.get(inst)
            if samples:
                cad = (
                    estimate_instance_cadence(inst, count_map, first_ts_map, last_ts_map)
                    or (fleet_median_cadence if fleet_median_cadence > 0 else range_step)
                )
                cur_h = h_start
                while cur_h < h_end:
                    nxt_h = cur_h + 3600.0
                    if cur_h >= now:
                        break  # never materialize a bucket for an hour that has not started
                    eff_end = min(nxt_h, now)
                    rec = reconstruct_time_series_intervals(
                        samples, window_start_ts=cur_h, window_end_ts=eff_end,
                        expected_interval_sec=cad,
                    )
                    hour_pts = [v for ts, v in samples if cur_h <= ts < eff_end]
                    bucket_records.append({
                        "instance": inst,
                        "job": instance_job_map.get(inst, "blackbox"),
                        "bucket_start": cur_h,
                        "bucket_end": nxt_h,
                        "uptime_seconds": round(rec["uptime_seconds"], 2),
                        "downtime_seconds": round(rec["downtime_seconds"], 2),
                        "unknown_seconds": round(rec["unknown_seconds"], 2),
                        "coverage_seconds": round(rec["coverage_seconds"], 2),
                        "sample_count": len(hour_pts),
                        "availability_pct": rec["availability_pct"],
                        "incident_count": int(rec["incident_count"]),
                        "avg_latency_ms": latency,
                        "updated_at": now,
                        # per-hour outages + still-in-outage-at-hour-start/end
                        # flags: merge_hybrid_target_availability drops the
                        # duplicate incident where one outage straddles the
                        # boundary between two contiguous buckets. "i" carries
                        # each outage's absolute [start, end] so the maintenance
                        # carve-out can intersect the real outage, not the hour.
                        "outage_json": {
                            "d": [round(float(x), 1) for x in rec.get("outage_durations_sec", [])],
                            "i": [[round(float(s), 1), round(float(e), 1)]
                                  for s, e in rec.get("outage_intervals_sec", [])],
                            "ongoing_start": bool(hour_pts and hour_pts[0] == 0),
                            "ongoing_end": bool(rec.get("is_ongoing_outage")),
                        },
                    })
                    cur_h = nxt_h
                continue

            # Fallback path: no raw samples (Prometheus range query failed, or
            # short retention) — approximate from the avg_over_time / count /
            # first-last-timestamp scalars, same as before this pass.
            avail_pct = None
            if raw_avail is not None:
                try:
                    avail_pct = round(max(0.0, min(100.0, float(raw_avail))), 2)
                except (ValueError, TypeError):
                    avail_pct = None

            sample_count = 0
            cov_sec = 0.0
            f_ts = float(first_ts_map.get(inst, 0)) if first_ts_map else 0.0
            l_ts = float(last_ts_map.get(inst, 0)) if last_ts_map else 0.0

            if raw_count is not None:
                try:
                    sample_count = int(float(raw_count))
                    if sample_count >= 2:
                        span = l_ts - f_ts
                        if f_ts > 0 and l_ts > 0 and span > 0:
                            intv = span / (sample_count - 1)
                            lead_in = min(intv, max(0.0, f_ts - w_start)) if (f_ts - w_start) <= intv * 1.5 else 0.0
                            lead_out = min(intv, max(0.0, w_end - l_ts)) if (w_end - l_ts) <= intv * 1.5 else 0.0
                            cov_sec = min(span + lead_in + lead_out, w_duration_sec)
                        else:
                            eff_cad = fleet_median_cadence if fleet_median_cadence > 0 else step_sec
                            cov_sec = min(sample_count * eff_cad, w_duration_sec)
                    elif sample_count == 1:
                        eff_cad = fleet_median_cadence if fleet_median_cadence > 0 else step_sec
                        cov_sec = min(eff_cad, w_duration_sec)
                except (ValueError, TypeError):
                    cov_sec = 0.0
            elif avail_pct is not None:
                cov_sec = w_duration_sec

            if avail_pct is not None:
                up_rate = min(1.0, max(0.0, float(avail_pct) / 100.0))
                down_rate = round(1.0 - up_rate, 6)
            else:
                up_rate = 0.0
                down_rate = 0.0
            down_sec = round(cov_sec * down_rate, 2)

            inc_count = 0
            if raw_inc is not None:
                try:
                    inc_count = int(math.ceil(float(raw_inc) / 2.0))
                except (ValueError, TypeError):
                    inc_count = 0
            if inc_count == 0 and down_sec > 0:
                inc_count = 1

            cur_h = h_start
            while cur_h < h_end:
                nxt_h = cur_h + 3600.0
                if f_ts > 0 and l_ts > 0 and l_ts >= f_ts:
                    overlap_start = max(cur_h, f_ts)
                    overlap_end = min(nxt_h, l_ts)
                    overlap_sec = max(0.0, overlap_end - overlap_start)
                    if overlap_sec > 0:
                        h_cov = min(3600.0, overlap_sec)
                        h_down = round(h_cov * down_rate, 2)
                        h_up = max(0.0, round(h_cov - h_down, 2))
                        h_unk = max(0.0, round(3600.0 - h_cov, 2))
                        h_avail = avail_pct
                    else:
                        h_cov = 0.0
                        h_down = 0.0
                        h_up = 0.0
                        h_unk = 3600.0
                        h_avail = None
                elif avail_pct is not None or cov_sec > 0:
                    h_cov = min(3600.0, cov_sec / num_hours)
                    h_down = round(h_cov * down_rate, 2)
                    h_up = max(0.0, round(h_cov - h_down, 2))
                    h_unk = max(0.0, round(3600.0 - h_cov, 2))
                    h_avail = avail_pct
                else:
                    h_cov = 0.0
                    h_down = 0.0
                    h_up = 0.0
                    h_unk = 3600.0
                    h_avail = None

                bucket_records.append({
                    "instance": inst,
                    "job": instance_job_map.get(inst, "blackbox"),
                    "bucket_start": cur_h,
                    "bucket_end": nxt_h,
                    "uptime_seconds": round(h_up, 2),
                    "downtime_seconds": round(h_down, 2),
                    "unknown_seconds": round(h_unk, 2),
                    "coverage_seconds": round(h_cov, 2),
                    "sample_count": sample_count // num_hours,
                    "availability_pct": h_avail,
                    "incident_count": inc_count if cur_h == h_start else 0,
                    "avg_latency_ms": latency,
                    "updated_at": now
                })
                cur_h = nxt_h

        if bucket_records:
            try:
                AvailabilityBucketRepository.save_buckets(bucket_records)
            except Exception:
                # Was silently swallowed (audit F5) — a persistently failing
                # write silently stops all bucket materialization.
                logger.exception("availability aggregator: save_buckets failed for %d record(s)", len(bucket_records))

    try:
        AvailabilityBucketRepository.prune_old_buckets(AVAIL_BUCKET_RETENTION_SECONDS)
    except Exception:
        logger.exception("availability aggregator: prune_old_buckets failed")


def _availability_aggregator_loop():
    while True:
        try:
            _aggregate_availability_cycle()
        except Exception as e:
            logger.error(f"Availability aggregator error: {e}", exc_info=True)
        time.sleep(AVAIL_AGGREGATE_INTERVAL_SECONDS)


def start_availability_aggregator():
    global _avail_aggregator_started
    if _avail_aggregator_started:
        return
    _avail_aggregator_started = True
    threading.Thread(target=_availability_aggregator_loop, daemon=True, name="avail-aggregator").start()


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
                name=a.get("name", "Unknown"), severity=a.get("severity", "critical"),
                instance=a.get("instance", "-"), summary=a.get("summary", ""),
                job=a.get("job", ""), event_time=float(a.get("time", time.time())),
                is_now_firing=True, key=key,
            )
            imported += 1
        if imported:
            logger.info("Recovered %d active incident(s) from status.json into SQLite on boot", imported)
    except Exception:
        logger.exception("status.json -> SQLite active-incident reconcile failed")
