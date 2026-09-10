"""Alerts / incidents domain: the firing/resolved state machine, maintenance-
window suppression, and dependency (alert-correlation) links.

record_alert_event() is the single funnel both the Alertmanager webhook and
the built-in poller go through — it applies one transition to the SQLite
incidents table and the status/logs/history JSON caches, deduping repeat
notifications, and fires the async Telegram dispatch. Maintenance windows are
checked from inside it so planned downtime suppresses alerts without a second
pipeline.

Depends on json_store, storage, telegram_notifier, monitoring_primitives and
flask.g only — nothing imports app.
"""
import logging
import threading
import time

from flask import g, has_request_context

try:
    from storage import json_store, IncidentRepository, MaintenanceRepository, DependencyRepository
    from .telegram import dispatch_alert_async
except (ImportError, ValueError):
    from alarm.storage import json_store, IncidentRepository, MaintenanceRepository, DependencyRepository
    from alarm.core.alerts.telegram import dispatch_alert_async

logger = logging.getLogger("infrawatch")

# Serializes the load-modify-save cycle for status.json/logs.json/history.json
# in webhook() — without it, concurrent deliveries (Alertmanager routinely
# fans out several groups at once under threaded=True) each read the same
# stale list and clobber each other's inserts on save.
#
# This is an in-PROCESS lock. The deployment is single-process (gunicorn
# --workers 1 --threads 8, see Dockerfile) so that is sufficient. Scaling to
# --workers > 1 would let two processes race these JSON files — but they are
# only a denormalized cache now: SQLite (incidents / event_logs) is the single
# source of truth for active-alert state and for /history & /logs (audit F1),
# and its writes ARE cross-process safe (BEGIN IMMEDIATE + per-key dedupe, see
# test_persistence_concurrency). A multi-worker deploy would still need a
# cross-process lock here (and poller leader election) before it is correct.
_WEBHOOK_LOCK = threading.Lock()

# Set whenever /webhook receives a real Alertmanager delivery — the poller
# fallback checks this and backs off, since Alertmanager (when present) is the
# authoritative source per the "never fabricate events" requirement.
_LAST_WEBHOOK_AT = [0.0]


def active_incident_list():
    """The current set of firing alerts. SQLite `incidents` is the single
    source of truth (audit F1); status.json is a denormalized cache used only
    as a fallback when the SQLite read itself fails (corrupt/locked DB), which
    preserves the pre-F1 resilience of the read paths."""
    try:
        return IncidentRepository.get_active_incidents()
    except Exception:
        logger.exception("active_incident_list: SQLite read failed; using status.json cache")
        status_data = json_store.load_json(json_store.STATUS_FILE, None)
        return status_data.get('alerts', []) if isinstance(status_data, dict) else []


# ── Maintenance windows ──────────────────────────────────────────────────────
# Planned downtime for a host or a whole job/group. Checked from inside
# record_alert_event() — the single place both the webhook and the poller
# funnel through — so a maintenance window suppresses alerts/alarms without a
# second alert pipeline.
# load_maintenance_windows() / load_dependencies() are called several times
# per /instances and /status request via build_canonical_monitoring_state()
# (and its helpers), each time a fresh SQLite connection + SELECT. Memoize
# them for the lifetime of a single request only, via flask.g: a mutation in
# one request is always visible to the next, and code paths with no request
# context (the background poller, direct calls in tests) transparently get an
# uncached live read.
def _request_memo(key, producer):
    if not has_request_context():
        return producer()
    cache = getattr(g, "_infrawatch_memo", None)
    if cache is None:
        cache = {}
        g._infrawatch_memo = cache
    if key not in cache:
        cache[key] = producer()
    return cache[key]


def _invalidate_maint_cache():
    if has_request_context():
        getattr(g, "_infrawatch_memo", {}).pop("maint_windows", None)


def _invalidate_dep_cache():
    if has_request_context():
        getattr(g, "_infrawatch_memo", {}).pop("dependencies", None)


def load_maintenance_windows():
    """SQLite (MaintenanceRepository) is the sole source of truth.
    maintenance.json used to be written alongside every create/delete and
    merged in here by id — a second, non-transactional copy that could
    silently drift from the DB (the exact failure mode this now avoids by
    construction: there's only one write path)."""
    def _load():
        try:
            return MaintenanceRepository.list_windows()
        except Exception:
            return []
    return _request_memo("maint_windows", _load)


def maintenance_windows_by_instance(instances, job_map, windows=None):
    """{instance: [(start_ts, end_ts), ...]} for maintenance windows overlapping
    each given instance (directly, or via a job-scoped window). Used to carve
    planned downtime out of the SLA denominator in /api/availability. Empty
    dict when there are no windows (the common case — zero added overhead)."""
    if windows is None:
        windows = load_maintenance_windows()
    if not windows or not instances:
        return {}
    job_map = job_map or {}
    out = {}
    for w in windows:
        s = _parse_epoch_ts(w.get('start_epoch') if w.get('start_epoch') is not None else w.get('start', 0))
        e = _parse_epoch_ts(w.get('end_epoch') if w.get('end_epoch') is not None else w.get('end', 0))
        if e <= s:
            continue
        scope = w.get('scope') or w.get('scope_type')
        target = w.get('target') or w.get('scope_target')
        if not target:
            continue  # malformed/legacy record — without this, target==None
            # matches every instance absent from job_map (None == None below)
        for inst in instances:
            matched = (target == job_map.get(inst)) if scope == 'job' else (target == inst)
            if matched:
                out.setdefault(inst, []).append((s, e))
    return out


def get_active_maintenance(instance, job=None, windows=None, now=None):
    """First maintenance window currently covering this instance/job, or None.
    `now` defaults to wall-clock; pass an explicit event time so the app-layer
    suppression check in record_alert_event() agrees with the SQLite-layer one
    (audit F6)."""
    if windows is None:
        windows = load_maintenance_windows()
    if now is None:
        now = time.time()
    for w in windows:
        start_raw = w.get('start_epoch') if w.get('start_epoch') is not None else w.get('start', 0)
        end_raw = w.get('end_epoch') if w.get('end_epoch') is not None else w.get('end', 0)
        start_ts = _parse_epoch_ts(start_raw)
        end_ts = _parse_epoch_ts(end_raw)
        if not (start_ts <= now <= end_ts):
            continue
        scope = w.get('scope') or w.get('scope_type')
        target = w.get('target') or w.get('scope_target')
        if scope == 'job':
            if job and target == job:
                return w
        elif target == instance:
            return w
    return None


def record_alert_event(name, severity, instance, summary, job, event_time, is_now_firing,
                        receiver='', generatorURL='', key=None, latency_ms=None,
                        http_status_code=None, last_error=None):
    """Applies one alert firing/resolved transition to status.json/logs.json/
    history.json and SQLite database. Shared by the Alertmanager webhook and the Prometheus-poller
    fallback so both get identical transition-only dedupe and incident-history reconciliation.
    Returns False if this was a repeat notification with no actual state
    change (already firing, or already resolved).
    """
    key = key or f"{name}|{instance}"

    with _WEBHOOK_LOCK:
        if is_now_firing and get_active_maintenance(instance, job, now=event_time):
            return False  # suppressed: instance/job is under an active maintenance window.

        status_data = json_store.load_json(json_store.STATUS_FILE, {"status": "NORMAL", "alerts": [], "updated": event_time})
        active_alerts = {}
        for a in status_data.get('alerts', []):
            k = a.get('key') or f"{a.get('name')}|{a.get('instance')}"
            active_alerts[k] = a

        was_firing = key in active_alerts
        if was_firing == is_now_firing:
            return False  # no state transition — dedupe repeat notifications

        # SQLite keeps its own was_firing/is_now_firing dedup (needed so two
        # worker processes racing on the same transition still land exactly
        # once — see test_persistence_concurrency.py). That means a failed
        # write here isn't just a missed record: it leaves SQLite's row on
        # stale status, so the *next* real transition for this key can look
        # like a no-op to SQLite's own dedup and get silently dropped too —
        # permanently desyncing /history from /status. One retry closes the
        # common transient case (lock contention, disk hiccup) cheaply.
        # ponytail: not a full reconciliation job; a periodic sweep that
        # reconciles SQLite incident status against status.json would close
        # the rest, add if repeated failures show up in the error log.
        for attempt in (1, 2):
            try:
                IncidentRepository.record_alert_event(
                    name=name, severity=severity, instance=instance, summary=summary,
                    job=job, event_time=event_time, is_now_firing=is_now_firing,
                    receiver=receiver, generatorURL=generatorURL, key=key, latency_ms=latency_ms,
                    http_status_code=http_status_code, last_error=last_error
                )
                break
            except Exception as e:
                if attempt == 2:
                    logger.error(f"Error updating SQLite incident repository (gave up after retry): {e}")

        duration_seconds = None
        if is_now_firing:
            active_alerts[key] = {
                "key":      key,
                "name":     name,
                "severity": severity,
                "instance": instance,
                "summary":  summary,
                "time":     event_time
            }
        else:
            started = active_alerts.pop(key, None)
            if started:
                duration_seconds = max(0, event_time - started.get('time', event_time))

        logs = json_store.load_json(json_store.LOGS_FILE, [])
        logs.insert(0, {
            "time":            event_time,
            "event":           "firing" if is_now_firing else "resolved",
            "name":            name,
            "severity":        severity,
            "instance":        instance,
            "summary":         summary,
            "job":             job,
            "receiver":        receiver,
            "generatorURL":    generatorURL,
            "latency_ms":      latency_ms,
            "duration_seconds": duration_seconds,
        })

        history = json_store.load_json(json_store.HISTORY_FILE, [])
        if is_now_firing:
            # occurrences/first_seen mirror IncidentRepository's SQLite
            # semantics (see storage.py) — a re-fire of a key that already
            # has a prior entry here increments instead of reading as a
            # fresh "occurrences x1" (this JSON copy is a fallback only;
            # SQLite is the source /history reads from when available).
            prior = next((h for h in history if h.get('key') == key), None)
            history.insert(0, {
                "key":          key,
                "name":         name,
                "severity":     severity,
                "instance":     instance,
                "summary":      summary,
                "time":         event_time,
                "status":       "firing",
                "job":          job,
                "receiver":     receiver,
                "generatorURL": generatorURL,
                "occurrences":  (prior.get('occurrences', 1) + 1) if prior else 1,
                "first_seen":   (prior.get('first_seen') or prior.get('time')) if prior else event_time,
                "http_status_code": http_status_code,
                "last_error":   last_error,
            })
        else:
            for h in history:
                if h.get('key') == key and h.get('status') == 'firing':
                    h['status'] = 'resolved'
                    h['resolved_time'] = event_time
                    h['duration_seconds'] = (
                        duration_seconds if duration_seconds is not None
                        else max(0, event_time - h.get('time', event_time))
                    )
                    break

        firing_list = list(active_alerts.values())
        if any(a.get('severity', 'critical') == 'critical' for a in firing_list):
            status_state = "CRITICAL"
        elif firing_list:
            status_state = "WARNING"
        else:
            status_state = "NORMAL"

        json_store.save_json(json_store.STATUS_FILE, {"status": status_state, "alerts": firing_list, "updated": time.time()})
        json_store.save_json(json_store.LOGS_FILE, logs[:json_store.MAX_LOGS])
        json_store.save_with_retention(json_store.HISTORY_FILE, json_store.HISTORY_ARCHIVE_FILE, history, json_store.MAX_HISTORY)

        # Asynchronously dispatch Telegram notification on verified state
        # transition. min_severity (telegram_notifier.get_telegram_config,
        # enforced in _async_send_worker) is what actually decides whether
        # this severity gets pushed — not a check here, so it stays a real,
        # user-editable setting (PUT /api/telegram) instead of a hardcoded
        # gate that would silently swallow any other severity="warning"
        # alert (e.g. a real Alertmanager rule) this function is also shared
        # with, not just the new SlowResponse alert it was added for.
        try:
            dispatch_alert_async(
                name=name,
                severity=severity,
                instance=instance,
                summary=summary,
                job=job,
                event_time=event_time,
                is_now_firing=is_now_firing,
                duration_seconds=duration_seconds,
                latency_ms=latency_ms
            )
        except Exception as e:
            logger.error(f"Error dispatching telegram alert: {e}")

        return True


# ── Alert Correlation (Phase 12) ─────────────────────────────────────────────
# A "depends on" link between two instances, used only to decide what to
# visually de-emphasize on the wallboard when both ends are down at once.
# Never touches status/logs/history — the underlying alert for a suppressed
# child still fires and is recorded exactly as if this feature didn't exist.
def load_dependencies():
    """SQLite (DependencyRepository) is the sole source of truth — see
    load_maintenance_windows() above for why dependencies.json's old dual-write
    (SQLite + a separate JSON copy) was removed rather than kept as a
    merge-by-id fallback. Memoized per-request via flask.g."""
    def _load():
        try:
            return DependencyRepository.list_dependencies()
        except Exception:
            return []
    return _request_memo("dependencies", _load)


def apply_correlation_suppression(targets, parent_map):
    """Pure function: tags each target in-place with dependsOn/suppressedBy.
    A target is suppressedBy=<parent> only when it's down AND its declared
    parent is also down — never touches health/downSince, so the real alert
    for a suppressed child is unaffected."""
    down_instances = {t['instance'] for t in targets if t['health'] != 'up'}
    for t in targets:
        parent = parent_map.get(t['instance'])
        t['dependsOn'] = parent
        t['suppressedBy'] = parent if (t['health'] != 'up' and parent in down_instances) else None
    return targets
