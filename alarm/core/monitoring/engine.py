"""FleetStateEngine: Deep monitoring state engine.

Consolidates:
1. Probe metrics ingestion (concurrent fetch of /api/v1/targets and probe metrics).
2. Target enrichment (probe readings, maintenance window suppression, dependency correlation,
   SQLite active incidents, operator acknowledgments, and node-exporter infrastructure correlation).
3. Fleet-level rollup reduction (FleetSummary, FleetState, and system status determination).
"""
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

try:
    from .models import FleetQuery, FleetSummary, FleetState
    from . import client as promclient
    from . import queries as prom_queries
    from .primitives import (
        matches_job_filter,
        classify_scrape_failure,
        _earliest_outage_start,
        _sane_epoch,
        _outage_past_grace,
        find_node_exporter_status,
        _parse_prom_duration_sec,
    )
    from config import (
        DEFAULT_JOB_FILTER,
        ALERTNAME_TARGET_DOWN,
        DEFAULT_SLOW_RESPONSE_THRESHOLD_MS,
    )
    from storage import (
        load_deleted_targets,
        SlowThresholdRepository,
        AcknowledgmentRepository,
        IncidentRepository,
    )
    from core.alerts import (
        active_incident_list,
        load_maintenance_windows,
        get_active_maintenance,
        load_dependencies,
        apply_correlation_suppression,
    )
    from core.availability.fleet import classify_probe_failure, get_availability_settings
except (ImportError, ValueError):
    from alarm.core.monitoring.models import FleetQuery, FleetSummary, FleetState
    from alarm.core.monitoring import client as promclient
    from alarm.core.monitoring import queries as prom_queries
    from alarm.core.monitoring.primitives import (
        matches_job_filter,
        classify_scrape_failure,
        _earliest_outage_start,
        _sane_epoch,
        _outage_past_grace,
        find_node_exporter_status,
        _parse_prom_duration_sec,
    )
    from alarm.config import (
        DEFAULT_JOB_FILTER,
        ALERTNAME_TARGET_DOWN,
        DEFAULT_SLOW_RESPONSE_THRESHOLD_MS,
    )
    from alarm.storage import (
        load_deleted_targets,
        SlowThresholdRepository,
        AcknowledgmentRepository,
        IncidentRepository,
    )
    from alarm.core.alerts import (
        active_incident_list,
        load_maintenance_windows,
        get_active_maintenance,
        load_dependencies,
        apply_correlation_suppression,
    )
    from alarm.core.availability.fleet import classify_probe_failure, get_availability_settings

logger = logging.getLogger("infrawatch.monitoring")


def _derive_probe_readings(
    keys: Tuple[str, ...],
    probe_success_map: Dict[str, Any],
    probe_duration_map: Dict[str, Any],
    probe_status_code_map: Dict[str, Any],
    down_since_prom_map: Dict[str, Any],
    raw_target: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Optional[float], Optional[float], Optional[int], str]:
    """Derive health, down_since, response_time_ms, http_code, and last_error
    from probe metrics and Prometheus targets.
    """
    def _first(mapping):
        for k in keys:
            if k in mapping:
                return mapping[k]
        return None

    p_success = _first(probe_success_map)
    if p_success is not None:
        health = 'up' if str(p_success) in ('1', '1.0') else 'down'
    elif raw_target is not None:
        raw_health = raw_target.get('health')
        health = raw_health if raw_health in ('up', 'down') else 'unknown'
    else:
        health = 'unknown'

    # Sanitize the raw Prometheus timestamp (junk/small/negative -> 0) and
    # reset it to 0 for a healthy target. Without this a malformed value ages
    # the wallboard outage from 1970 ("496859h") and, being far-future,
    # flips _outage_past_grace() to hide a real outage.
    if health != 'up':
        down_since_val = _sane_epoch(_first(down_since_prom_map))
    else:
        down_since_val = 0

    p_dur = _first(probe_duration_map)
    if p_dur is not None:
        try:
            response_time_ms = round(float(p_dur) * 1000, 1)
        except (TypeError, ValueError):
            response_time_ms = None
    elif raw_target is not None:
        scrape_dur = raw_target.get('lastScrapeDuration')
        try:
            response_time_ms = round(float(scrape_dur) * 1000, 1) if scrape_dur is not None else None
        except (TypeError, ValueError):
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


class FleetStateEngine:
    """Deep module orchestrating probe ingestion, target enrichment, and fleet rollups."""

    def __init__(
        self,
        prom_client=None,
        prom_queries=None,
        incident_repo=None,
        active_incident_provider: Optional[Callable] = None,
        maintenance_loader: Optional[Callable] = None,
        maintenance_checker: Optional[Callable] = None,
        dependency_loader: Optional[Callable] = None,
        correlation_suppressor: Optional[Callable] = None,
        ack_repo=None,
        slow_threshold_repo=None,
        deleted_targets_loader: Optional[Callable] = None,
        availability_settings_loader: Optional[Callable] = None,
        probe_failure_classifier: Optional[Callable] = None,
        executor=None,
    ):
        self.prom_client = prom_client or promclient
        self.prom_queries = prom_queries or globals().get("prom_queries")
        self.incident_repo = incident_repo or IncidentRepository
        self.active_incident_provider = active_incident_provider or active_incident_list
        self.maintenance_loader = maintenance_loader or load_maintenance_windows
        self.maintenance_checker = maintenance_checker or get_active_maintenance
        self.dependency_loader = dependency_loader or load_dependencies
        self.correlation_suppressor = correlation_suppressor or apply_correlation_suppression
        self.ack_repo = ack_repo or AcknowledgmentRepository
        self.slow_threshold_repo = slow_threshold_repo or SlowThresholdRepository
        self.deleted_targets_loader = deleted_targets_loader or load_deleted_targets
        self.availability_settings_loader = availability_settings_loader or get_availability_settings
        self.probe_failure_classifier = probe_failure_classifier or classify_probe_failure
        self.executor = executor or promclient._SHARED_EXECUTOR

    def get_fleet_state(self, query: Optional[FleetQuery] = None) -> FleetState:
        """Main entry point: executes probe snapshot ingestion, target enrichment,
        and fleet summary reduction.
        """
        if query is None:
            query = FleetQuery()

        job_filter = query.job_filter
        now_ts = int(query.now if query.now is not None else time.time())

        # Stage 1: Probe Ingestion
        probe_snapshot = self._fetch_probe_snapshot()
        if probe_snapshot.get("unreachable"):
            return FleetState(
                ok=False,
                system_status="CRITICAL",
                summary=FleetSummary(
                    system_status="CRITICAL",
                    has_alarm=True,
                    has_unacknowledged_alarm=True,
                ),
                error="Prometheus engine is unreachable",
                job_filter=job_filter,
                updated=now_ts,
            )

        # Stage 2: Target Enrichment
        targets, available_jobs = self._enrich_targets(probe_snapshot, now_ts, job_filter)

        # Stage 3: Fleet Rollup & Alert Consolidation
        return self._compute_fleet_summary(
            targets=targets,
            now_ts=now_ts,
            available_jobs=available_jobs,
            active_base=probe_snapshot.get("active_base"),
            job_filter=job_filter,
        )

    def _fetch_probe_snapshot(self) -> Dict[str, Any]:
        """Stage 1: Concurrent fetch of /api/v1/targets and probe metrics, plus conditional subqueries."""
        f_targets = self.executor.submit(self.prom_client.fetch_prometheus_json, '/api/v1/targets', True, 3.0)
        f_metrics = self.executor.submit(self.prom_queries.fetch_all_probe_metrics, 3.0)

        raw_targets, active_base = f_targets.result()
        probe_success_map, probe_duration_map, probe_status_code_map = f_metrics.result()

        if raw_targets is None:
            return {"unreachable": True}

        has_down = False
        if probe_success_map:
            has_down = any(str(v) in ('0', '0.0') for v in probe_success_map.values())
        if not has_down and raw_targets and raw_targets.get('status') == 'success':
            has_down = any(t.get('health') != 'up' for t in raw_targets.get('data', {}).get('activeTargets', []))

        use_node_exporter = False
        try:
            use_node_exporter = self.availability_settings_loader().get("use_node_exporter_correlation", False)
        except Exception:
            pass

        if has_down:
            down_since_prom_map = self.prom_queries.fetch_down_since_prom_map(cache_ttl=10.0)
            node_exporter_up_map = (
                self.prom_queries.fetch_prom_query_map('up{job="node_exporter"}', cache_ttl=10.0)
                if use_node_exporter else {}
            )
        else:
            down_since_prom_map = {}
            node_exporter_up_map = {}

        return {
            "unreachable": False,
            "raw_targets": raw_targets,
            "active_base": active_base,
            "probe_success_map": probe_success_map or {},
            "probe_duration_map": probe_duration_map or {},
            "probe_status_code_map": probe_status_code_map or {},
            "down_since_prom_map": down_since_prom_map or {},
            "node_exporter_up_map": node_exporter_up_map or {},
            "use_node_exporter": use_node_exporter,
        }

    def _enrich_targets(
        self,
        snapshot: Dict[str, Any],
        now_ts: int,
        job_filter: str,
    ) -> Tuple[List[Dict[str, Any]], Set[str]]:
        """Stage 2: Target-level fusion with probe readings, maintenance, correlation, and SQLite state."""
        raw_targets = snapshot["raw_targets"]
        probe_success_map = snapshot["probe_success_map"]
        probe_duration_map = snapshot["probe_duration_map"]
        probe_status_code_map = snapshot["probe_status_code_map"]
        down_since_prom_map = snapshot["down_since_prom_map"]
        node_exporter_up_map = snapshot["node_exporter_up_map"]
        use_node_exporter = snapshot["use_node_exporter"]

        try:
            deleted_targets = set(self.deleted_targets_loader())
        except Exception:
            deleted_targets = set()

        try:
            slow_thresholds = self.slow_threshold_repo.get_all()
        except Exception:
            slow_thresholds = {}

        def _slow_threshold_for(inst):
            return slow_thresholds.get(inst, DEFAULT_SLOW_RESPONSE_THRESHOLD_MS)

        def _infra_correlation_for(health, addr):
            if not use_node_exporter or health == 'up':
                return None
            found, healthy = find_node_exporter_status(addr, node_exporter_up_map)
            return self.probe_failure_classifier(True, found, healthy)

        available_jobs = set()
        if raw_targets and raw_targets.get('status') == 'success':
            for t in raw_targets.get('data', {}).get('activeTargets', []):
                j = t.get('labels', {}).get('job') or t.get('scrapePool')
                if j and j != 'prometheus':
                    available_jobs.add(j)

        active_alerts_list = self.active_incident_provider()
        alerts_by_instance: Dict[str, List[Dict[str, Any]]] = {}
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

                if (
                    job != 'prometheus'
                    and matches_job_filter(job, scrape_pool, job_filter)
                    and inst_name not in deleted_targets
                    and scrape_url not in deleted_targets
                ):
                    seen_instances.add(inst_name)
                    seen_instances.add(scrape_url)

                    health, down_since_val, response_time_ms, http_code, last_error = _derive_probe_readings(
                        (inst_name, scrape_url),
                        probe_success_map,
                        probe_duration_map,
                        probe_status_code_map,
                        down_since_prom_map,
                        raw_target=t,
                    )
                    classification = classify_scrape_failure(health, last_error, http_code)
                    infra_correlation = _infra_correlation_for(health, scrape_url or inst_name)

                    matched_alerts = alerts_by_instance.get(inst_name, []) + [
                        a for a in alerts_by_instance.get(scrape_url, [])
                        if a not in alerts_by_instance.get(inst_name, [])
                    ]
                    down_since_val = _earliest_outage_start(down_since_val, matched_alerts, health)

                    result.append({
                        "instance": inst_name,
                        "job": job or "blackbox",
                        "health": health,
                        "probe_state": health,
                        "responseTimeMs": response_time_ms,
                        "httpStatusCode": http_code,
                        "lastScrape": t.get('lastScrape', ''),
                        "scrapeUrl": scrape_url,
                        "lastError": last_error,
                        "failureCategory": classification["category"],
                        "failureDetail": classification["detail"],
                        "infraCorrelation": infra_correlation,
                        "labels": labels,
                        "isWeb": False,
                        "downSince": down_since_val,
                        "active_alerts": matched_alerts,
                    })

        # Merge non-probed targets with active alerts (e.g. host-level alerts)
        for inst, inst_alerts in alerts_by_instance.items():
            if inst not in seen_instances and inst not in deleted_targets:
                alert_job = inst_alerts[0].get('job', 'alertmanager') if inst_alerts else 'alertmanager'
                if matches_job_filter(alert_job, alert_job, job_filter):
                    is_any_down = any(a.get('name') == ALERTNAME_TARGET_DOWN for a in inst_alerts)
                    health = 'down' if is_any_down else 'up'
                    down_since_val = _sane_epoch(inst_alerts[0].get('time')) if is_any_down else 0
                    result.append({
                        "instance": inst,
                        "job": alert_job,
                        "health": health,
                        "probe_state": health,
                        "responseTimeMs": None,
                        "httpStatusCode": None,
                        "lastScrape": "—",
                        "scrapeUrl": inst,
                        "lastError": "",
                        "failureCategory": "Alertmanager" if health != 'up' else None,
                        "failureDetail": inst_alerts[0].get('summary', '') if inst_alerts else '',
                        "labels": {"job": alert_job, "instance": inst},
                        "isWeb": False,
                        "downSince": down_since_val,
                        "active_alerts": inst_alerts,
                    })
                    seen_instances.add(inst)

        # Attach maintenance state
        maintenance_windows = self.maintenance_loader()
        for item in result:
            mw = self.maintenance_checker(item['instance'], item.get('job'), windows=maintenance_windows)
            item['maintenance'] = bool(mw)
            item['maintenanceId'] = mw.get('id') if mw else None
            item['maintenanceUntil'] = mw.get('end') if mw else None
            item['maintenanceReason'] = mw.get('reason', '') if mw else ''

        # Dependency suppression
        deps = self.dependency_loader()
        parent_map = {d['child']: d['parent'] for d in deps}
        self.correlation_suppressor(result, parent_map)
        dep_id_map = {d['child']: d['id'] for d in deps}
        for item in result:
            item['dependencyId'] = dep_id_map.get(item['instance'])
            item['is_suppressed'] = bool(item.get('suppressedBy') or item.get('maintenance'))

        # Global alert acknowledgments
        try:
            active_acks = self.ack_repo.get_active_acknowledgments()
        except Exception:
            active_acks = {}

        for item in result:
            ack_rec = active_acks.get(item['instance'])
            item['acknowledged'] = bool(ack_rec)
            item['acknowledged_by'] = ack_rec['acknowledged_by'] if ack_rec else None
            item['acknowledged_at'] = ack_rec['acknowledged_at'] if ack_rec else None

        # Severity & effective status derivations
        _now_wall = float(now_ts)
        for item in result:
            is_down = item['health'] == 'down'
            is_nodata = item['health'] not in ('up', 'down')
            is_maint = item['maintenance']
            is_supp = bool(item.get('suppressedBy'))
            slow_threshold_ms = _slow_threshold_for(item['instance'])
            item['slowThresholdMs'] = slow_threshold_ms
            is_slow = (
                item['health'] == 'up'
                and item['responseTimeMs'] is not None
                and item['responseTimeMs'] > slow_threshold_ms
            )
            has_crit_alert = any(a.get('severity') == 'critical' for a in item.get('active_alerts', []))
            has_warn_alert = any(a.get('severity') == 'warning' for a in item.get('active_alerts', []))

            confirmed_outage = is_down and _outage_past_grace(item.get('downSince'), _now_wall)
            item['pending_outage'] = is_down and not confirmed_outage

            is_alarmable = (confirmed_outage or has_crit_alert or has_warn_alert) and not is_maint and not is_supp
            item['is_alarmable'] = is_alarmable

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

        # Auto-clean resolved acknowledgments on unfiltered sweeps
        if (not job_filter or job_filter.lower() in ('all', '*')) and active_acks:
            try:
                active_down_set = {t['instance'] for t in result if t['health'] != 'up'}
                self.ack_repo.clear_resolved(active_down_set)
            except Exception:
                pass

        return result, available_jobs

    def _compute_fleet_summary(
        self,
        targets: List[Dict[str, Any]],
        now_ts: int,
        available_jobs: Set[str],
        active_base: Optional[str],
        job_filter: str,
    ) -> FleetState:
        """Stage 3: Pure reduction computing summary counts, system status, and alert aggregation."""
        # Per-target slow threshold was already resolved and stamped onto each
        # item as `slowThresholdMs` in _enrich_targets — reuse it instead of a
        # second SlowThresholdRepository.get_all() round-trip.
        total = len(targets)
        up_count = sum(1 for t in targets if t['health'] == 'up')
        down_count = sum(1 for t in targets if t['health'] == 'down')
        nodata_count = sum(1 for t in targets if t['health'] not in ('up', 'down'))
        slow_count = sum(
            1 for t in targets
            if t['health'] == 'up'
            and t['responseTimeMs'] is not None
            and t['responseTimeMs'] > t.get('slowThresholdMs', DEFAULT_SLOW_RESPONSE_THRESHOLD_MS)
        )
        maint_count = sum(1 for t in targets if t['maintenance'])
        supp_count = sum(1 for t in targets if t.get('suppressedBy'))
        alarmable_down = sum(1 for t in targets if t['is_alarmable'] and t['health'] != 'up')
        alarmable_alerts = sum(len(t.get('active_alerts', [])) for t in targets if t['is_alarmable'])
        unacked_down = sum(1 for t in targets if t['is_alarmable'] and t['health'] != 'up' and not t.get('acknowledged'))
        acked_down = sum(1 for t in targets if t['is_alarmable'] and t['health'] != 'up' and t.get('acknowledged'))

        has_critical = (alarmable_down > 0 or any(
            a.get('severity') == 'critical' for t in targets if t['is_alarmable'] for a in t.get('active_alerts', [])
        ))
        has_warning = (slow_count > 0 or any(
            a.get('severity') == 'warning' for t in targets if t['is_alarmable'] for a in t.get('active_alerts', [])
        ))

        if has_critical:
            system_status = "CRITICAL"
        elif has_warning:
            system_status = "WARNING"
        else:
            system_status = "NORMAL"

        has_alarm = (alarmable_down > 0 or alarmable_alerts > 0)
        is_globally_acknowledged = (alarmable_down > 0 and unacked_down == 0)

        summary = FleetSummary(
            total=total,
            up=up_count,
            down=down_count,
            no_data=nodata_count,
            slow=slow_count,
            maintenance=maint_count,
            suppressed=supp_count,
            alarmable_down=alarmable_down,
            alarmable_alerts=alarmable_alerts,
            unacknowledged_down=unacked_down,
            acknowledged_down=acked_down,
            is_acknowledged=is_globally_acknowledged,
            has_alarm=has_alarm,
            has_unacknowledged_alarm=(unacked_down > 0),
            system_status=system_status,
        )

        fleet_active_alerts = []
        seen_alert_keys = set()
        for t in targets:
            for a in t.get('active_alerts', []):
                k = a.get('key') or f"{a.get('name')}|{a.get('instance')}"
                if k not in seen_alert_keys:
                    seen_alert_keys.add(k)
                    fleet_active_alerts.append(a)

        return FleetState(
            ok=True,
            system_status=system_status,
            summary=summary,
            targets=targets,
            active_alerts=fleet_active_alerts,
            available_jobs=sorted(list(available_jobs)),
            job_filter=job_filter,
            source="prometheus" if active_base else "local",
            prometheus_url=active_base,
            updated=now_ts,
        )

    def get_instance_job_map(self, job_filter: Optional[str] = None) -> Dict[str, str]:
        """Instance -> real Prometheus job name for every monitored target matching job_filter."""
        if job_filter is None:
            job_filter = DEFAULT_JOB_FILTER

        raw_targets, _ = self.prom_client.fetch_prometheus_json('/api/v1/targets', use_cache=True, cache_ttl=3.0)
        try:
            deleted_targets = set(self.deleted_targets_loader())
        except Exception:
            deleted_targets = set()

        job_map = {}
        if raw_targets and raw_targets.get('status') == 'success':
            targets = raw_targets.get('data', {}).get('activeTargets', [])
            for t in targets:
                labels = t.get('labels', {})
                job = labels.get('job') or t.get('scrapePool') or ''
                scrape_pool = t.get('scrapePool', '')
                inst_name = labels.get('instance', t.get('scrapeUrl', '?'))
                if (
                    job != 'prometheus'
                    and matches_job_filter(job, scrape_pool, job_filter)
                    and inst_name not in deleted_targets
                ):
                    job_map[inst_name] = job or scrape_pool or 'blackbox'

        for a in self.active_incident_provider():
            inst = a.get('instance')
            if (
                inst
                and inst not in deleted_targets
                and matches_job_filter(a.get('job', 'alertmanager'), 'alertmanager', job_filter)
            ):
                job_map.setdefault(inst, a.get('job') or 'alertmanager')

        return job_map

    def get_instance_cadence_map(self, job_filter: Optional[str] = None) -> Dict[str, float]:
        """Instance -> real per-target scrape interval (seconds)."""
        if job_filter is None:
            job_filter = DEFAULT_JOB_FILTER

        raw_targets, _ = self.prom_client.fetch_prometheus_json('/api/v1/targets', use_cache=True, cache_ttl=3.0)
        try:
            deleted_targets = set(self.deleted_targets_loader())
        except Exception:
            deleted_targets = set()

        cadence_map = {}
        if raw_targets and raw_targets.get('status') == 'success':
            for t in raw_targets.get('data', {}).get('activeTargets', []) or []:
                labels = t.get('labels', {})
                job = labels.get('job') or t.get('scrapePool') or ''
                scrape_pool = t.get('scrapePool', '')
                inst_name = labels.get('instance', t.get('scrapeUrl', '?'))
                if (
                    job != 'prometheus'
                    and matches_job_filter(job, scrape_pool, job_filter)
                    and inst_name not in deleted_targets
                ):
                    sec = _parse_prom_duration_sec(t.get('scrapeInterval'))
                    if sec:
                        cadence_map[inst_name] = sec

        return cadence_map

    def get_monitored_instances(self, job_filter: Optional[str] = None) -> List[str]:
        return sorted(self.get_instance_job_map(job_filter).keys())


# Default singleton instance
fleet_state_engine = FleetStateEngine()
