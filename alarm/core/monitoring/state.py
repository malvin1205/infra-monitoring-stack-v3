"""Monitoring-state domain: the canonical fleet-health grid.

build_canonical_monitoring_state() fuses Prometheus /api/v1/targets, the probe
metric maps, active incidents (SQLite), maintenance windows, dependency
correlation and per-target SLA/slow-threshold overrides into the single
target list every live view renders from. _derive_probe_readings() is the
shared per-target enrichment; get_instance_job_map / _cadence_map /
get_monitored_instances resolve the monitored set.

Depends on prom_queries, prometheus_client, website_targets, alerts,
monitoring_primitives, fleet_availability, storage and config — nothing
imports app. Prometheus adapters are called as prom_queries.<fn> /
promclient.<fn> so one patch target covers routes, this engine, and the
workers.
"""
import time

try:
    from . import client as promclient
    from . import queries as prom_queries
    from . import primitives as monitoring_primitives
    from .primitives import (
        matches_job_filter, classify_scrape_failure, _earliest_outage_start,
        _sane_epoch, _outage_past_grace, _extract_host, find_node_exporter_status,
        _parse_prom_duration_sec,
    )
    from config import (
        DEFAULT_JOB_FILTER, ALERTNAME_TARGET_DOWN,
        DEFAULT_SLOW_RESPONSE_THRESHOLD_MS, SCRAPE_INTERVAL_SECONDS,
    )
    from storage import (
        load_website_targets, load_deleted_targets,
        SlowThresholdRepository, AcknowledgmentRepository,
    )
    from core.alerts import (
        active_incident_list, load_maintenance_windows, get_active_maintenance,
        load_dependencies, apply_correlation_suppression,
    )
    from core.availability import classify_probe_failure, get_availability_settings
except (ImportError, ValueError):
    from alarm.core.monitoring import client as promclient
    from alarm.core.monitoring import queries as prom_queries
    from alarm.core.monitoring import primitives as monitoring_primitives
    from alarm.core.monitoring.primitives import (
        matches_job_filter, classify_scrape_failure, _earliest_outage_start,
        _sane_epoch, _outage_past_grace, _extract_host, find_node_exporter_status,
        _parse_prom_duration_sec,
    )
    from alarm.config import (
        DEFAULT_JOB_FILTER, ALERTNAME_TARGET_DOWN,
        DEFAULT_SLOW_RESPONSE_THRESHOLD_MS, SCRAPE_INTERVAL_SECONDS,
    )
    from alarm.storage import (
        load_website_targets, load_deleted_targets,
        SlowThresholdRepository, AcknowledgmentRepository,
    )
    from alarm.core.alerts import (
        active_incident_list, load_maintenance_windows, get_active_maintenance,
        load_dependencies, apply_correlation_suppression,
    )
    from alarm.core.availability import classify_probe_failure, get_availability_settings


# ── Canonical Monitoring State Engine ───────────────────────────────────────
def _derive_probe_readings(keys, probe_success_map, probe_duration_map, probe_status_code_map,
                            down_since_prom_map, raw_target=None):
    """Shared by both target-enrichment loops in build_canonical_monitoring_state:
    derive health/down_since/response_time_ms/http_code/last_error from the
    Prometheus probe metric maps for a target identified by one or more
    lookup keys (instance name and/or scrapeUrl — checked in order, first
    match wins).

    raw_target, when given (the /api/v1/targets entry for a target Prometheus
    itself discovered), supplies two extra fallbacks the probe-target path
    always had: its own `health` field when probe_success has no reading yet,
    and `lastScrapeDuration` when probe_duration_seconds has no reading yet —
    plus lastError for classification. Custom (manually-added, non-Prometheus
    -discovered) targets pass raw_target=None and keep the narrower fallbacks
    (health='unknown', last_error='') they always had.

    response_time_ms is None when there is genuinely no latency reading (no
    probe_duration_seconds sample and no lastScrapeDuration) — callers must not
    treat that as "0ms"/"< 1 ms". A real numeric 0.0 only comes from an actual
    zero-valued sample.

    This only extracts what both loops were already computing identically —
    it does not change either path's behavior, including one small existing
    difference the two call sites had for http_code: the probe-target path
    treats probe_status_code_map's raw value with `is not None` (so a real
    "0" — Blackbox's code for "no HTTP response" — comes through as 0), while
    the custom-target path used a truthy check (so "0"/0 there resolves to
    None). Preserved via the raw_target branch below rather than silently
    unified, since nothing here proves which behavior is "correct"."""
    def _first(mapping):
        for k in keys:
            if k in mapping:
                return mapping[k]
        return None

    p_success = _first(probe_success_map)
    if p_success is not None:
        health = 'up' if str(p_success) in ('1', '1.0') else 'down'
    elif raw_target is not None:
        health = raw_target.get('health', 'unknown')
    else:
        health = 'unknown'

    if health != 'up':
        down_since_val = _sane_epoch(_first(down_since_prom_map))
    else:
        down_since_val = 0

    p_duration = _first(probe_duration_map)
    if p_duration is not None:
        try:
            response_time_ms = round(float(p_duration) * 1000, 1)
        except ValueError:
            response_time_ms = None
    elif raw_target is not None:
        scrape_dur = raw_target.get('lastScrapeDuration')
        if scrape_dur is not None:
            try:
                response_time_ms = round(float(scrape_dur) * 1000, 1)
            except ValueError:
                response_time_ms = None
        else:
            response_time_ms = None
    else:
        response_time_ms = None

    p_code = _first(probe_status_code_map)
    if raw_target is not None:
        if p_code is not None:
            try:
                http_code = int(float(p_code))
            except ValueError:
                http_code = None
        else:
            http_code = None
    else:
        try:
            http_code = int(float(p_code)) if p_code else None
        except ValueError:
            http_code = None

    last_error = raw_target.get('lastError', '') if raw_target is not None else ''
    return health, down_since_val, response_time_ms, http_code, last_error

def build_canonical_monitoring_state(job_param=None):
    if job_param is None:
        job_param = DEFAULT_JOB_FILTER

    f_targets = promclient._SHARED_EXECUTOR.submit(promclient.fetch_prometheus_json, '/api/v1/targets', True, 3.0)
    f_metrics = promclient._SHARED_EXECUTOR.submit(prom_queries.fetch_all_probe_metrics, 3.0)

    raw_targets, active_base = f_targets.result()
    probe_success_map, probe_duration_map, probe_status_code_map = f_metrics.result()

    # Conditional query: only run heavy 1-day subqueries when down targets exist
    has_down = False
    if probe_success_map:
        has_down = any(str(v) in ('0', '0.0') for v in probe_success_map.values())
    if not has_down and raw_targets and raw_targets.get('status') == 'success':
        has_down = any(t.get('health') != 'up' for t in raw_targets.get('data', {}).get('activeTargets', []))

    use_node_exporter_correlation = get_availability_settings().get("use_node_exporter_correlation", False)

    # Per-instance "slow" latency threshold — the live grid must honour the
    # same SlowThresholdRepository overrides the SlowResponse alert does, not
    # a hardcoded 500ms (audit F4).
    try:
        _slow_thresholds = SlowThresholdRepository.get_all()
    except Exception:
        _slow_thresholds = {}

    def _slow_threshold_for(inst):
        return _slow_thresholds.get(inst, DEFAULT_SLOW_RESPONSE_THRESHOLD_MS)

    if has_down:
        down_since_prom_map = prom_queries.fetch_down_since_prom_map(cache_ttl=10.0)
        # Bulk single query (same shape as probe_success_map above), only
        # ever fetched when the setting is ON and something is actually
        # down -- mirrors down_since_prom_map's own "only when needed" gate.
        node_exporter_up_map = prom_queries.fetch_prom_query_map('up{job="node_exporter"}', cache_ttl=10.0) if use_node_exporter_correlation else {}
    else:
        down_since_prom_map = {}
        node_exporter_up_map = {}

    def _infra_correlation_for(health, addr):
        """None when the setting is OFF or the target is up -- classification
        never runs, so OFF is byte-for-byte the pre-existing behavior."""
        if not use_node_exporter_correlation or health == 'up':
            return None
        found, healthy = find_node_exporter_status(addr, node_exporter_up_map)
        return classify_probe_failure(True, found, healthy)

    config_web_targets = load_website_targets()
    deleted_targets = set(load_deleted_targets())
    now_ts = int(time.time())

    if raw_targets is None and not config_web_targets:
        return {
            "ok": False,
            "error": "Prometheus engine is unreachable",
            "system_status": "CRITICAL",
            "summary": {
                "total": 0, "up": 0, "down": 0, "slow": 0, "maintenance": 0,
                "suppressed": 0, "alarmable_down": 0, "alarmable_alerts": 0,
                "unacknowledged_down": 0, "acknowledged_down": 0,
                "is_acknowledged": False, "has_alarm": True,
                "has_unacknowledged_alarm": True, "system_status": "CRITICAL"
            },
            "active_alerts": [],
            "targets": []
        }

    available_jobs = set()
    if raw_targets and raw_targets.get('status') == 'success':
        for t in raw_targets.get('data', {}).get('activeTargets', []):
            j = t.get('labels', {}).get('job') or t.get('scrapePool')
            if j and j != 'prometheus':
                available_jobs.add(j)

    # Active-alert state: SQLite incidents is the SINGLE source of truth
    # (audit F1). status.json is only a denormalized write-through cache;
    # reading it preferentially let the two diverge silently — a SQLite-only
    # firing incident became an unclearable phantom CRITICAL, a status.json
    # -only one desynced /history from /status.
    active_alerts_list = active_incident_list()

    alerts_by_instance = {}
    for a in active_alerts_list:
        inst = a.get('instance')
        if inst:
            alerts_by_instance.setdefault(inst, []).append(a)

    result = []
    seen_instances = set()

    if raw_targets and raw_targets.get('status') == 'success':
        targets = raw_targets.get('data', {}).get('activeTargets', [])
        for t in targets:
            labels = t.get('labels', {})
            job = labels.get('job') or t.get('scrapePool') or ''
            scrape_pool = t.get('scrapePool', '')
            inst_name = labels.get('instance', t.get('scrapeUrl', '?'))
            scrape_url = t.get('scrapeUrl', inst_name)

            # Filter targets by job matching
            if job != 'prometheus' and matches_job_filter(job, scrape_pool, job_param) and inst_name not in deleted_targets and scrape_url not in deleted_targets:
                seen_instances.add(inst_name)
                seen_instances.add(scrape_url)

                health, down_since_val, response_time_ms, http_code, last_error = _derive_probe_readings(
                    (inst_name, scrape_url), probe_success_map, probe_duration_map,
                    probe_status_code_map, down_since_prom_map, raw_target=t
                )
                classification = classify_scrape_failure(health, last_error, http_code)
                infra_correlation = _infra_correlation_for(health, scrape_url or inst_name)

                matched_alerts = alerts_by_instance.get(inst_name, []) + [
                    a for a in alerts_by_instance.get(scrape_url, [])
                    if a not in alerts_by_instance.get(inst_name, [])
                ]
                down_since_val = _earliest_outage_start(down_since_val, matched_alerts, health)

                result.append({
                    "instance":        inst_name,
                    "job":             job or "blackbox",
                    "health":          health,
                    "probe_state":     health,
                    "responseTimeMs":  response_time_ms,
                    "httpStatusCode":  http_code,
                    "lastScrape":      t.get('lastScrape', ''),
                    "scrapeUrl":       scrape_url,
                    "lastError":       last_error,
                    "failureCategory": classification["category"],
                    "failureDetail":   classification["detail"],
                    "infraCorrelation": infra_correlation,
                    "labels":          labels,
                    "isWeb":           False,
                    "downSince":       down_since_val,
                    "active_alerts":   matched_alerts
                })

    # Merge custom user-added targets
    for target_url in config_web_targets:
        if matches_job_filter("custom", "custom", job_param) and target_url not in seen_instances and target_url not in deleted_targets:
            health, down_since_val, response_time_ms, http_code, last_error = _derive_probe_readings(
                (target_url,), probe_success_map, probe_duration_map,
                probe_status_code_map, down_since_prom_map, raw_target=None
            )
            classification = classify_scrape_failure(health, last_error, http_code)
            infra_correlation = _infra_correlation_for(health, target_url)
            matched_alerts = alerts_by_instance.get(target_url, [])
            down_since_val = _earliest_outage_start(down_since_val, matched_alerts, health)

            result.append({
                "instance":        target_url,
                "job":             "custom",
                "health":          health,
                "probe_state":     health,
                "responseTimeMs":  response_time_ms,
                "httpStatusCode":  http_code,
                "lastScrape":      "—",
                "scrapeUrl":       target_url,
                "lastError":       "",
                "failureCategory": classification["category"],
                "failureDetail":   classification["detail"],
                "infraCorrelation": infra_correlation,
                "labels":          {"job": "custom", "instance": target_url},
                "isWeb":           False,
                "downSince":       down_since_val,
                "active_alerts":   matched_alerts
            })
            seen_instances.add(target_url)

    # Merge any non-probed targets with active alerts (e.g. Host/OS-level CPU, Memory, Disk alerts)
    for inst, inst_alerts in alerts_by_instance.items():
        if inst not in seen_instances and inst not in deleted_targets:
            alert_job = inst_alerts[0].get('job', 'alertmanager') if inst_alerts else 'alertmanager'
            if matches_job_filter(alert_job, alert_job, job_param):
                is_any_down = any(a.get('name') == ALERTNAME_TARGET_DOWN for a in inst_alerts)
                health = 'down' if is_any_down else 'up'
                down_since_val = _sane_epoch(inst_alerts[0].get('time')) if is_any_down else 0
                result.append({
                    "instance":        inst,
                    "job":             alert_job,
                    "health":          health,
                    "probe_state":     health,
                    "responseTimeMs":  None,
                    "httpStatusCode":  None,
                    "lastScrape":      "—",
                    "scrapeUrl":       inst,
                    "lastError":       "",
                    "failureCategory": "Alertmanager" if health != 'up' else None,
                    "failureDetail":   inst_alerts[0].get('summary', '') if inst_alerts else '',
                    "labels":          {"job": alert_job, "instance": inst},
                    "isWeb":           False,
                    "downSince":       down_since_val,
                    "active_alerts":   inst_alerts
                })
                seen_instances.add(inst)

    # Attach maintenance state
    maintenance_windows = load_maintenance_windows()
    for item in result:
        mw = get_active_maintenance(item['instance'], item.get('job'), windows=maintenance_windows)
        item['maintenance'] = bool(mw)
        item['maintenanceId'] = mw.get('id') if mw else None
        item['maintenanceUntil'] = mw.get('end') if mw else None
        item['maintenanceReason'] = mw.get('reason', '') if mw else ''

    # Alert correlation (dependency suppression)
    deps = load_dependencies()
    parent_map = {d['child']: d['parent'] for d in deps}
    apply_correlation_suppression(result, parent_map)
    dep_id_map = {d['child']: d['id'] for d in deps}
    for item in result:
        item['dependencyId'] = dep_id_map.get(item['instance'])
        item['is_suppressed'] = bool(item.get('suppressedBy') or item.get('maintenance'))

    # Attach global alert acknowledgments from SQLite
    try:
        active_acks = AcknowledgmentRepository.get_active_acknowledgments()
    except Exception:
        active_acks = {}

    for item in result:
        ack_rec = active_acks.get(item['instance'])
        item['acknowledged'] = bool(ack_rec)
        item['acknowledged_by'] = ack_rec['acknowledged_by'] if ack_rec else None
        item['acknowledged_at'] = ack_rec['acknowledged_at'] if ack_rec else None

    # Derive authoritative target-level severity & effective_status
    _now_wall = time.time()
    for item in result:
        # 'unknown' means Prometheus has no probe_success sample for this
        # target at all (e.g. a websites.yml entry no scrape job covers) —
        # that is "no data", NOT an outage. Treating it as down made every
        # un-scraped custom target a phantom confirmed outage on startup:
        # is_down was `!= 'up'`, downSince defaulted to 0, and
        # _outage_past_grace(0) fail-opens → CRITICAL + siren every boot.
        is_down = item['health'] == 'down'
        is_nodata = item['health'] not in ('up', 'down')
        is_maint = item['maintenance']
        is_supp = bool(item.get('suppressedBy'))
        slow_threshold_ms = _slow_threshold_for(item['instance'])
        item['slowThresholdMs'] = slow_threshold_ms
        is_slow = (item['health'] == 'up' and item['responseTimeMs'] is not None
                   and item['responseTimeMs'] > slow_threshold_ms)
        has_crit_alert = any(a.get('severity') == 'critical' for a in item.get('active_alerts', []))
        has_warn_alert = any(a.get('severity') == 'warning' for a in item.get('active_alerts', []))

        # Debounce: a down target isn't a confirmed outage until it's been down
        # for OUTAGE_GRACE_SECONDS. Within the window it still renders as down,
        # but doesn't count toward alarms / CRITICAL.
        confirmed_outage = is_down and _outage_past_grace(item.get('downSince'), _now_wall)
        item['pending_outage'] = is_down and not confirmed_outage

        # Is target actionable / alarmable? 'no data' on its own never is — it
        # only becomes alarmable if it also carries a real firing alert.
        is_alarmable = (confirmed_outage or has_crit_alert or has_warn_alert) and not is_maint and not is_supp
        item['is_alarmable'] = is_alarmable

        # Effective severity
        if is_maint:
            item['severity'] = "maintenance"
        elif is_supp:
            item['severity'] = "suppressed"
        elif is_down or has_crit_alert:
            item['severity'] = "critical"
        elif is_slow or has_warn_alert:
            item['severity'] = "warning"
        elif is_nodata:
            item['severity'] = "unknown"
        else:
            item['severity'] = "ok"

        # Effective status
        if is_maint:
            item['effective_status'] = "maintenance"
        elif is_supp:
            item['effective_status'] = "suppressed"
        elif is_down:
            item['effective_status'] = "down"
        elif is_slow or has_warn_alert:
            item['effective_status'] = "degraded"
        elif is_nodata:
            item['effective_status'] = "no_data"
        else:
            item['effective_status'] = "up"

    # Auto-clean acknowledgments for targets that have recovered (health == 'up').
    # `result` is filtered by job_param, so it only has full down-state visibility
    # on the unfiltered ("all") sweep -- running this on a job-scoped view would
    # see every other job's down instances as "absent" and wipe their acks.
    # Only take a write lock when there is actually an acknowledgment to
    # possibly clear — otherwise this GET path opened a BEGIN IMMEDIATE
    # transaction on SQLite every 5s per polling client for nothing.
    if (not job_param or job_param.lower() in ('all', '*')) and active_acks:
        try:
            active_down_set = {t['instance'] for t in result if t['health'] != 'up'}
            AcknowledgmentRepository.clear_resolved(active_down_set)
        except Exception:
            pass

    # Compute authoritative global system metrics & status
    total = len(result)
    up_count = sum(1 for t in result if t['health'] == 'up')
    down_count = sum(1 for t in result if t['health'] == 'down')
    nodata_count = sum(1 for t in result if t['health'] not in ('up', 'down'))
    slow_count = sum(1 for t in result if t['health'] == 'up' and t['responseTimeMs'] is not None and t['responseTimeMs'] > _slow_threshold_for(t['instance']))
    maint_count = sum(1 for t in result if t['maintenance'])
    supp_count = sum(1 for t in result if t.get('suppressedBy'))
    alarmable_down = sum(1 for t in result if t['is_alarmable'] and t['health'] != 'up')
    alarmable_alerts = sum(len(t.get('active_alerts', [])) for t in result if t['is_alarmable'])
    unacked_down = sum(1 for t in result if t['is_alarmable'] and t['health'] != 'up' and not t.get('acknowledged'))
    acked_down = sum(1 for t in result if t['is_alarmable'] and t['health'] != 'up' and t.get('acknowledged'))

    has_critical = (alarmable_down > 0 or any(
        a.get('severity') == 'critical' for t in result if t['is_alarmable'] for a in t.get('active_alerts', [])
    ))
    has_warning = (slow_count > 0 or any(
        a.get('severity') == 'warning' for t in result if t['is_alarmable'] for a in t.get('active_alerts', [])
    ))

    if has_critical:
        system_status = "CRITICAL"
    elif has_warning:
        system_status = "WARNING"
    else:
        system_status = "NORMAL"

    has_alarm = (alarmable_down > 0 or alarmable_alerts > 0)
    # Global ACK state: true if all alarmable down targets are acknowledged
    is_globally_acknowledged = (alarmable_down > 0 and unacked_down == 0)

    summary = {
        "total": total,
        "up": up_count,
        "down": down_count,
        "no_data": nodata_count,
        "slow": slow_count,
        "maintenance": maint_count,
        "suppressed": supp_count,
        "alarmable_down": alarmable_down,
        "alarmable_alerts": alarmable_alerts,
        "unacknowledged_down": unacked_down,
        "acknowledged_down": acked_down,
        "is_acknowledged": is_globally_acknowledged,
        "has_alarm": has_alarm,
        "has_unacknowledged_alarm": (unacked_down > 0),
        "system_status": system_status
    }

    # Aggregate active alerts across fleet
    fleet_active_alerts = []
    seen_alert_keys = set()
    for t in result:
        for a in t.get('active_alerts', []):
            k = a.get('key') or f"{a.get('name')}|{a.get('instance')}"
            if k not in seen_alert_keys:
                seen_alert_keys.add(k)
                fleet_active_alerts.append(a)

    return {
        "ok": True,
        "system_status": system_status,
        "summary": summary,
        "active_alerts": fleet_active_alerts,
        "targets": result,
        "available_jobs": sorted(list(available_jobs)),
        "job_filter": job_param,
        "source": "prometheus" if active_base else "local",
        "prometheus_url": active_base,
        "updated": now_ts
    }


def get_instance_job_map(job_filter=None):
    """Instance -> real Prometheus job name for every monitored target matching
    job_filter. Shared by get_monitored_instances() and the availability
    aggregator so persisted buckets get tagged with the target's actual job
    instead of a hardcoded guess."""
    if job_filter is None:
        job_filter = DEFAULT_JOB_FILTER
    raw_targets, _ = promclient.fetch_prometheus_json('/api/v1/targets', use_cache=True, cache_ttl=3.0)
    config_web_targets = load_website_targets()
    deleted_targets = set(load_deleted_targets())

    job_map = {}
    if raw_targets and raw_targets.get('status') == 'success':
        targets = raw_targets.get('data', {}).get('activeTargets', [])
        for t in targets:
            labels = t.get('labels', {})
            job = labels.get('job') or t.get('scrapePool') or ''
            scrape_pool = t.get('scrapePool', '')
            inst_name = labels.get('instance', t.get('scrapeUrl', '?'))
            if job != 'prometheus' and matches_job_filter(job, scrape_pool, job_filter) and inst_name not in deleted_targets:
                job_map[inst_name] = job or scrape_pool or 'blackbox'

    for target_url in config_web_targets:
        if matches_job_filter("custom", "custom", job_filter) and target_url not in deleted_targets:
            job_map.setdefault(target_url, "custom")

    # Also include instances with active alerts — from SQLite, the single
    # source of truth for active-alert state (audit F1).
    for a in active_incident_list():
        inst = a.get('instance')
        if inst and inst not in deleted_targets and matches_job_filter(a.get('job', 'alertmanager'), 'alertmanager', job_filter):
            job_map.setdefault(inst, a.get('job') or 'alertmanager')

    return job_map


def get_instance_cadence_map(job_filter=None):
    """Instance -> real per-target scrape interval (seconds), read straight
    from Prometheus's own /api/v1/targets `scrapeInterval` field.

    This is what expected_interval_sec should always be keyed by: a mixed
    fleet scrapes blackbox-ping targets at 60s and node-exporter/http targets
    at 15s, so one global SCRAPE_INTERVAL_SECONDS (or a fleet-wide median) is
    wrong for whichever job doesn't match it. scrapeInterval is present on
    every active target regardless of requested window size, unlike the
    first_ts/last_ts subqueries (which api_availability skips for windows
    over 60 minutes for cost reasons) — so it works for the 24h/7d/30d cases
    those can't cover.
    """
    if job_filter is None:
        job_filter = DEFAULT_JOB_FILTER
    raw_targets, _ = promclient.fetch_prometheus_json('/api/v1/targets', use_cache=True, cache_ttl=3.0)
    deleted_targets = set(load_deleted_targets())

    cadence_map = {}
    if raw_targets and raw_targets.get('status') == 'success':
        for t in raw_targets.get('data', {}).get('activeTargets', []):
            labels = t.get('labels', {})
            job = labels.get('job') or t.get('scrapePool') or ''
            scrape_pool = t.get('scrapePool', '')
            inst_name = labels.get('instance', t.get('scrapeUrl', '?'))
            if job != 'prometheus' and matches_job_filter(job, scrape_pool, job_filter) and inst_name not in deleted_targets:
                sec = _parse_prom_duration_sec(t.get('scrapeInterval'))
                if sec:
                    cadence_map[inst_name] = sec

    # Custom (websites.yml) targets are never in Prometheus's activeTargets, so
    # they carry no scrapeInterval here and — on windows > 60m, where the
    # observed-cadence estimate is also unavailable — fall through to
    # DEFAULT_SCRAPE_INTERVAL_SEC (2s) in merge_hybrid_target_availability,
    # collapsing their coverage to a few percent and flagging them
    # INSUFFICIENT_DATA. Give them the fleet's observed median cadence instead
    # (the best available guess for whatever job actually probes them); fall
    # back to the deployment scrape interval only when nothing else is known.
    if matches_job_filter("custom", "custom", job_filter):
        if cadence_map:
            _vals = sorted(cadence_map.values())
            _fallback_cadence = _vals[len(_vals) // 2]
        else:
            _fallback_cadence = SCRAPE_INTERVAL_SECONDS
        for target_url in load_website_targets():
            if target_url not in deleted_targets:
                cadence_map.setdefault(target_url, _fallback_cadence)
    return cadence_map


def get_monitored_instances(job_filter=None):
    return sorted(get_instance_job_map(job_filter).keys())

