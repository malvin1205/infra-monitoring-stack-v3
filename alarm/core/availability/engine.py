"""AvailabilityEngine: the deep domain module for fleet availability,
hybrid TSDB/SQLite reconciliation, SLA error budgets, and bucket aggregation.
"""
import logging
import math
import time
from typing import Optional, Dict, Any, List, Tuple

from .models import AvailabilityQuery, AvailabilityReport
from ._cache import AvailabilityCache
from .fleet import (
    merge_hybrid_fleet_availability,
    reconstruct_time_series_intervals,
    clip_hourly_bucket,
    derive_bucket_inputs,
    estimate_instance_cadence,
    sla_budget,
    get_sla_target_pct,
    summarize_entries,
)
from .helpers import (
    _attach_sla_budgets,
    _availability_status_counts,
    _build_fleet_trend,
)

try:
    from config import (
        DEFAULT_JOB_FILTER,
        SCRAPE_INTERVAL_SECONDS,
        _AVAIL_FRESHNESS_TOLERANCE_SEC,
        _AVAIL_STALE_BUCKET_TOLERANCE_SEC,
    )
    from storage import (
        AvailabilityBucketRepository,
        SlaTargetRepository,
    )
    from core.monitoring import (
        client as default_prom_client,
        queries as default_prom_queries,
    )
    from core.monitoring.state import (
        get_monitored_instances,
        get_instance_job_map,
        get_instance_cadence_map,
    )
    from core.monitoring.primitives import _parse_epoch_ts
    from core.alerts.engine import (
        load_maintenance_windows,
        maintenance_windows_by_instance,
    )
except (ImportError, ValueError):
    from alarm.config import (
        DEFAULT_JOB_FILTER,
        SCRAPE_INTERVAL_SECONDS,
        _AVAIL_FRESHNESS_TOLERANCE_SEC,
        _AVAIL_STALE_BUCKET_TOLERANCE_SEC,
    )
    from alarm.storage import (
        AvailabilityBucketRepository,
        SlaTargetRepository,
    )
    from alarm.core.monitoring import (
        client as default_prom_client,
        queries as default_prom_queries,
    )
    from alarm.core.monitoring.state import (
        get_monitored_instances,
        get_instance_job_map,
        get_instance_cadence_map,
    )
    from alarm.core.monitoring.primitives import _parse_epoch_ts
    from alarm.core.alerts.engine import (
        load_maintenance_windows,
        maintenance_windows_by_instance,
    )

logger = logging.getLogger("infrawatch.availability")


class AvailabilityEngine:
    """Deep module consolidating availability calculation, in-flight caching,
    hybrid Prometheus/SQLite fusion, and hourly bucket aggregation.
    """

    def __init__(
        self,
        prom_client=None,
        prom_queries=None,
        bucket_repo=None,
        sla_repo=None,
        cache: Optional[AvailabilityCache] = None,
    ):
        self.prom_client = prom_client or default_prom_client
        self.prom_queries = prom_queries or default_prom_queries
        self.bucket_repo = bucket_repo or AvailabilityBucketRepository
        self.sla_repo = sla_repo or SlaTargetRepository
        self.cache = cache or AvailabilityCache()

    def invalidate_cache(self, job: Optional[str] = None, instance: Optional[str] = None) -> int:
        """Evict matching cached availability queries."""
        return self.cache.invalidate(job=job, instance=instance)

    def get_availability(self, query: AvailabilityQuery) -> AvailabilityReport:
        """Single deep entry point for querying fleet and per-target availability."""
        t_req_start = time.perf_counter()
        now = time.time()

        # Resolve SLA targets map
        try:
            sla_target_map = self.sla_repo.get_all()
        except Exception:
            sla_target_map = {}
        sla_map_sig = hash(tuple(sorted(sla_target_map.items()))) if sla_target_map else 0

        # Endpoint resolution for cache keying
        try:
            endpoints_data = self.prom_client.load_endpoints()
            active_url = endpoints_data.get("active") or getattr(self.prom_client, "_DEFAULT_PROM_URL", "http://prometheus:9090")
        except Exception:
            active_url = "http://prometheus:9090"

        cache_key, effective_ttl = AvailabilityCache.derive_cache_key(
            endpoint=active_url,
            job=query.job,
            minutes_int=query.minutes_int,
            end_ts=query.end_ts,
            sla_target_pct=query.sla_target_pct,
            sla_days=query.sla_days,
            sla_map_sig=sla_map_sig,
            now=now,
        )

        # 1. Check in-flight cache (fast path before lock acquisition)
        cached_payload = self.cache.get(cache_key, effective_ttl, now=now)
        if cached_payload is not None:
            return AvailabilityReport(cached_payload)

        # 2. Acquire single-flight coalescing lock
        t_lock_wait_start = time.perf_counter()
        with self.cache.flight_lock_for(cache_key):
            t_lock_acquired = time.perf_counter()
            flight_wait_ms = (t_lock_acquired - t_lock_wait_start) * 1000.0

            # Re-check cache under lock in case another worker just completed it
            now_under_lock = time.time()
            cached_payload = self.cache.get(cache_key, effective_ttl, now=now_under_lock)
            if cached_payload is not None:
                return AvailabilityReport(cached_payload)

            # 3. Compute availability
            req_end = float(query.end_ts) if query.end_ts is not None else now
            req_start = req_end - (query.minutes * 60.0)

            try:
                monitored_instances = get_monitored_instances(job_filter=query.job)
            except Exception:
                monitored_instances = []

            if not monitored_instances:
                empty_summary = summarize_entries([], query.minutes)
                payload = self._build_empty_payload(query, req_end, empty_summary)
                self.cache.set(cache_key, payload, now=now_under_lock)
                return AvailabilityReport(payload)

            # Maintenance windows overlapping this query window
            maint_by_inst = self._resolve_maintenance_windows(req_start, req_end, query.job, monitored_instances)

            # Retrieve SQLite bucket records in the window [req_start, req_end]
            t_sqlite_start = time.perf_counter()
            try:
                db_bucket_records = self.bucket_repo.get_bucket_records(
                    job=query.job,
                    start_time=req_start,
                    end_time=req_end,
                    instances=monitored_instances,
                )
            except Exception:
                db_bucket_records = []
            t_sqlite_end = time.perf_counter()
            sqlite_duration_ms = (t_sqlite_end - t_sqlite_start) * 1000.0

            # Check if SQLite completely covers the requested window for all monitored instances
            is_sqlite_fully_complete = self._check_sqlite_coverage(
                db_bucket_records, monitored_instances, req_start, req_end, query.end_ts, now
            )

            if is_sqlite_fully_complete:
                # FAST PATH: Materialized SQLite coverage with 0 Prometheus queries
                payload = self._compute_fast_path_payload(
                    query=query,
                    req_start=req_start,
                    req_end=req_end,
                    monitored_instances=monitored_instances,
                    db_bucket_records=db_bucket_records,
                    maint_by_inst=maint_by_inst,
                    sla_target_map=sla_target_map,
                    flight_wait_ms=flight_wait_ms,
                    sqlite_duration_ms=sqlite_duration_ms,
                    t_req_start=t_req_start,
                )
            else:
                # HYBRID PATH: Merge live Prometheus TSDB samples with SQLite buckets
                payload = self._compute_hybrid_path_payload(
                    query=query,
                    req_start=req_start,
                    req_end=req_end,
                    monitored_instances=monitored_instances,
                    db_bucket_records=db_bucket_records,
                    maint_by_inst=maint_by_inst,
                    sla_target_map=sla_target_map,
                    flight_wait_ms=flight_wait_ms,
                    sqlite_duration_ms=sqlite_duration_ms,
                    t_req_start=t_req_start,
                )

            if not query.debug:
                payload.pop("_trace", None)

            self.cache.set(cache_key, payload, now=now_under_lock)
            return AvailabilityReport(payload)

    def aggregate_hourly_buckets(self, now: Optional[float] = None) -> int:
        """Roll up completed historical 1-hour availability buckets and persist to SQLite.
        Returns the count of bucket records written.

        Ported from the original _aggregate_availability_cycle: one bucket row
        per instance PER HOUR (not one wide row per aggregation window), exact
        reconstruction from raw 0/1 samples when available with a per-hour
        outage_json payload, and a first_ts/last_ts overlap fallback when the
        range query is empty. changes() counts both edges of an outage, so the
        incident count is halved (ceil) before persisting.
        """
        curr_time = now if now is not None else time.time()
        try:
            instance_job_map = get_instance_job_map("all")
            monitored = sorted(instance_job_map.keys())
        except Exception:
            instance_job_map = {}
            monitored = []

        if not monitored:
            return 0

        latest_end = self.bucket_repo.get_latest_bucket_end("all")
        windows_to_aggregate = self._availability_aggregation_windows(curr_time, latest_end)
        total_written = 0

        executor = getattr(self.prom_client, "_SHARED_EXECUTOR", None)

        for w_start, w_end in windows_to_aggregate:
            w_minutes = max(1, int(round((w_end - w_start) / 60.0)))
            step_sec = SCRAPE_INTERVAL_SECONDS
            if w_minutes > 10080:
                step_sec = 300.0
            elif w_minutes > 1440:
                step_sec = 30.0

            at_suffix = f" @ {int(w_end)}"
            queries = {
                "probe_avail": f"avg_over_time(probe_success[{w_minutes}m]{at_suffix}) * 100",
                "up_avail": f"avg_over_time(up[{w_minutes}m]{at_suffix}) * 100",
                "probe_count": f"count_over_time(probe_success[{w_minutes}m]{at_suffix})",
                "up_count": f"count_over_time(up[{w_minutes}m]{at_suffix})",
                "probe_first_ts": f"min_over_time(timestamp(probe_success)[{w_minutes}m:]{at_suffix})",
                "probe_last_ts": f"max_over_time(timestamp(probe_success)[{w_minutes}m:]{at_suffix})",
                "up_first_ts": f"min_over_time(timestamp(up)[{w_minutes}m:]{at_suffix})",
                "up_last_ts": f"max_over_time(timestamp(up)[{w_minutes}m:]{at_suffix})",
                "duration": f"avg_over_time(probe_duration_seconds[{w_minutes}m]{at_suffix}) * 1000",
                "probe_incidents": f"changes(probe_success[{w_minutes}m]{at_suffix})",
                "up_incidents": f"changes(up[{w_minutes}m]{at_suffix})",
            }

            if executor:
                futures = {k: executor.submit(self.prom_queries.fetch_prom_query_map, q, 10.0, 15.0) for k, q in queries.items()}
                results = {}
                for k, f in futures.items():
                    try:
                        results[k] = f.result()
                    except Exception:
                        results[k] = {}
            else:
                results = {}
                for k, q in queries.items():
                    try:
                        results[k] = self.prom_queries.fetch_prom_query_map(q, 10.0, 15.0)
                    except Exception:
                        results[k] = {}

            range_step = 15.0 if w_minutes <= 180 else 30.0
            try:
                probe_range_map = self.prom_queries.fetch_prom_range_map("probe_success", w_start, w_end, range_step, cache_ttl=10.0, timeout=20.0)
            except Exception:
                probe_range_map = {}
            try:
                up_range_map = self.prom_queries.fetch_prom_range_map("up", w_start, w_end, range_step, cache_ttl=10.0, timeout=20.0)
            except Exception:
                up_range_map = {}

            probe_results_raw = {
                "avail": results.get("probe_avail", {}), "count": results.get("probe_count", {}),
                "first_ts": results.get("probe_first_ts", {}), "last_ts": results.get("probe_last_ts", {}),
                "incidents": results.get("probe_incidents", {}),
            }
            up_results_raw = {
                "avail": results.get("up_avail", {}), "count": results.get("up_count", {}),
                "first_ts": results.get("up_first_ts", {}), "last_ts": results.get("up_last_ts", {}),
                "incidents": results.get("up_incidents", {}),
            }
            merged_maps = derive_bucket_inputs(monitored, probe_results_raw, up_results_raw)
            avail_map = merged_maps["avail"]
            count_map = merged_maps["count"]
            first_ts_map = merged_maps["first_ts"]
            last_ts_map = merged_maps["last_ts"]
            incidents_map = merged_maps["incidents"]
            duration_map = results.get("duration", {})

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
                        if cur_h >= curr_time:
                            break  # never materialize a bucket for an hour that has not started
                        eff_end = min(nxt_h, curr_time)
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
                            "updated_at": curr_time,
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

                # Fallback path: no raw samples — approximate from the
                # avg_over_time / count / first-last-timestamp scalars.
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
                        "updated_at": curr_time,
                    })
                    cur_h = nxt_h

            if bucket_records:
                try:
                    self.bucket_repo.save_buckets(bucket_records)
                    total_written += len(bucket_records)
                except Exception:
                    logger.exception(
                        "availability aggregator: save_buckets failed for %d record(s)", len(bucket_records)
                    )

        return total_written

    def _resolve_maintenance_windows(self, req_start: float, req_end: float, job: str, monitored_instances: List[str]):
        try:
            all_windows = load_maintenance_windows()
            overlapping = [
                w for w in all_windows
                if _parse_epoch_ts(w.get("end_epoch") if w.get("end_epoch") is not None else w.get("end", 0)) > req_start
                and _parse_epoch_ts(w.get("start_epoch") if w.get("start_epoch") is not None else w.get("start", 0)) < req_end
            ]
            if overlapping:
                needs_job = any((w.get("scope") or w.get("scope_type")) == "job" for w in overlapping)
                job_map = get_instance_job_map(job) if needs_job else {}
                return maintenance_windows_by_instance(monitored_instances, job_map, overlapping) or None
        except Exception:
            pass
        return None

    def _check_sqlite_coverage(
        self,
        db_bucket_records: List[Dict[str, Any]],
        monitored_instances: List[str],
        req_start: float,
        req_end: float,
        end_ts: Optional[float],
        now: float,
    ) -> bool:
        if not db_bucket_records or not monitored_instances:
            return False

        instances_in_db = set()
        instance_spans = {}
        newest_bucket_update = 0.0

        for b in db_bucket_records:
            inst = b.get("instance")
            cov_sec = float(b.get("coverage_seconds", 0) or 0)
            if inst in monitored_instances and cov_sec > 0:
                instances_in_db.add(inst)
                st = float(b.get("bucket_start", 0))
                en = float(b.get("bucket_end", 0))
                try:
                    newest_bucket_update = max(newest_bucket_update, float(b.get("updated_at", 0) or 0))
                except (TypeError, ValueError):
                    pass
                cur = instance_spans.get(inst)
                if cur is None:
                    instance_spans[inst] = (st, en, 1)
                else:
                    instance_spans[inst] = (min(cur[0], st), max(cur[1], en), cur[2] + 1)

        aggregator_fresh = (
            end_ts is not None
            or (newest_bucket_update > 0.0 and (now - newest_bucket_update) < _AVAIL_STALE_BUCKET_TOLERANCE_SEC)
        )

        if len(instances_in_db) == len(monitored_instances) and aggregator_fresh:
            expected_hours = max(1, int(round((req_end - req_start) / 3600.0)))
            for inst in monitored_instances:
                sp = instance_spans.get(inst)
                if not sp or sp[0] > (req_start + 60.0) or sp[1] < (req_end - _AVAIL_FRESHNESS_TOLERANCE_SEC) or sp[2] < max(1, expected_hours - 1):
                    return False
            return True
        return False

    def _compute_fast_path_payload(
        self,
        query: AvailabilityQuery,
        req_start: float,
        req_end: float,
        monitored_instances: List[str],
        db_bucket_records: List[Dict[str, Any]],
        maint_by_inst: Any,
        sla_target_map: Dict[str, float],
        flight_wait_ms: float,
        sqlite_duration_ms: float,
        t_req_start: float,
    ) -> Dict[str, Any]:
        t_merge_start = time.perf_counter()
        entries, summary_dict = merge_hybrid_fleet_availability(
            req_start=req_start,
            req_end=req_end,
            monitored_instances=monitored_instances,
            sqlite_buckets=db_bucket_records,
            prom_results_map={},
            expected_interval_sec=SCRAPE_INTERVAL_SECONDS,
            maintenance_by_instance=maint_by_inst,
            sla_threshold_by_instance=sla_target_map,
        )
        t_merge_end = time.perf_counter()
        merge_duration_ms = (t_merge_end - t_merge_start) * 1000.0

        fleet_sla_budget = _attach_sla_budgets(
            summary_dict, query.minutes * 60.0, query.sla_target_pct, query.sla_days, target_map=sla_target_map
        )
        hybrid_meta = summary_dict.get("hybrid", {})

        if any(e.get("availability_pct") is None for e in entries):
            counts = _availability_status_counts(entries, self.prom_queries.fetch_prom_query_map("probe_success"))
        else:
            counts = _availability_status_counts(entries)

        lowest_availability = sorted(
            [e for e in summary_dict["per_server"]["values"] if e.get("availability_pct") is not None and e["availability_pct"] < 100.0],
            key=lambda e: (
                e["availability_pct"],
                -(e.get("downtime_minutes") or 0.0),
                -(e.get("incidents") or 0),
            ),
        )

        trend_series, trend_slot_sec = _build_fleet_trend(req_end, query.minutes * 60.0, monitored_instances)
        t_done = time.perf_counter()

        trace_data = {
            "route": "/api/availability",
            "path": "sqlite_fast_path",
            "minutes": query.minutes_int,
            "flight_wait_ms": round(flight_wait_ms, 2),
            "sqlite_duration_ms": round(sqlite_duration_ms, 2),
            "sqlite_record_count": len(db_bucket_records),
            "prom_duration_ms": 0.0,
            "prom_query_count": 0,
            "prom_queries": {},
            "hybrid_merge_ms": round(merge_duration_ms, 2),
            "materialize_ms": 0.0,
            "total_backend_ms": round((t_done - t_req_start) * 1000.0, 2),
        }

        return self._assemble_payload(
            query=query,
            req_end=req_end,
            monitored_instances=monitored_instances,
            summary_dict=summary_dict,
            hybrid_meta=hybrid_meta,
            fleet_sla_budget=fleet_sla_budget,
            counts=counts,
            lowest_availability=lowest_availability,
            trend_series=trend_series,
            trend_slot_sec=trend_slot_sec,
            source="materialized",
            data_status="COMPLETE",
            trace_data=trace_data,
        )

    def _compute_hybrid_path_payload(
        self,
        query: AvailabilityQuery,
        req_start: float,
        req_end: float,
        monitored_instances: List[str],
        db_bucket_records: List[Dict[str, Any]],
        maint_by_inst: Any,
        sla_target_map: Dict[str, float],
        flight_wait_ms: float,
        sqlite_duration_ms: float,
        t_req_start: float,
    ) -> Dict[str, Any]:
        at_suffix = f" @ {query.end_ts}" if query.end_ts is not None else ""
        req_timeout = max(4.0, min(15.0, query.minutes / 1500.0))
        avail_cache_ttl = 15.0 if query.minutes_int >= 1440 else 5.0

        queries = {
            "probe_avail": f"avg_over_time(probe_success[{query.minutes_int}m]{at_suffix}) * 100",
            "up_avail": f"avg_over_time(up[{query.minutes_int}m]{at_suffix}) * 100",
            "probe_count": f"count_over_time(probe_success[{query.minutes_int}m]{at_suffix})",
            "up_count": f"count_over_time(up[{query.minutes_int}m]{at_suffix})",
            "duration": f"avg_over_time(probe_duration_seconds[{query.minutes_int}m]{at_suffix}) * 1000",
            "probe_incidents": f"changes(probe_success[{query.minutes_int}m]{at_suffix})",
            "up_incidents": f"changes(up[{query.minutes_int}m]{at_suffix})",
            "live_probe": "probe_success" if query.end_ts is None else None,
            "live_up": "up" if query.end_ts is None else None,
        }
        if query.minutes_int <= 60:
            queries["probe_first_ts"] = f"min_over_time(timestamp(probe_success)[{query.minutes_int}m:]{at_suffix})"
            queries["probe_last_ts"] = f"max_over_time(timestamp(probe_success)[{query.minutes_int}m:]{at_suffix})"
            queries["up_first_ts"] = f"min_over_time(timestamp(up)[{query.minutes_int}m:]{at_suffix})"
            queries["up_last_ts"] = f"max_over_time(timestamp(up)[{query.minutes_int}m:]{at_suffix})"

        t_prom_start = time.perf_counter()
        executor = getattr(self.prom_client, "_SHARED_EXECUTOR", None)

        def _call_q(expr):
            t_s = time.perf_counter()
            res = self.prom_queries.fetch_prom_query_map(expr, cache_ttl=avail_cache_ttl, timeout=req_timeout)
            return res, (time.perf_counter() - t_s) * 1000.0

        results = {}
        prom_query_timings = {}
        if executor:
            futures = {k: executor.submit(_call_q, q) for k, q in queries.items() if q}
            for k, f in futures.items():
                try:
                    res, q_dur = f.result()
                    results[k] = res
                    prom_query_timings[k] = round(q_dur, 2)
                except Exception:
                    results[k] = {}
                    prom_query_timings[k] = -1.0
        else:
            for k, q in queries.items():
                if q:
                    res, q_dur = _call_q(q)
                    results[k] = res
                    prom_query_timings[k] = round(q_dur, 2)

        t_prom_end = time.perf_counter()
        prom_duration_ms = (t_prom_end - t_prom_start) * 1000.0

        probe_results_raw = {
            "avail": results.get("probe_avail", {}), "count": results.get("probe_count", {}),
            "first_ts": results.get("probe_first_ts", {}), "last_ts": results.get("probe_last_ts", {}),
            "incidents": results.get("probe_incidents", {}), "live": results.get("live_probe", {}),
        }
        up_results_raw = {
            "avail": results.get("up_avail", {}), "count": results.get("up_count", {}),
            "first_ts": results.get("up_first_ts", {}), "last_ts": results.get("up_last_ts", {}),
            "incidents": results.get("up_incidents", {}), "live": results.get("live_up", {}),
        }
        merged_maps = derive_bucket_inputs(monitored_instances, probe_results_raw, up_results_raw)
        avail_map = merged_maps["avail"]
        count_map = merged_maps["count"]
        first_ts_map = merged_maps["first_ts"]
        last_ts_map = merged_maps["last_ts"]
        incidents_map = merged_maps["incidents"]
        live_map = merged_maps["live"]
        duration_map = results.get("duration", {})

        try:
            cadence_map = dict(get_instance_cadence_map(query.job))
        except Exception:
            cadence_map = {}
        for inst in monitored_instances:
            estimated = estimate_instance_cadence(inst, count_map, first_ts_map, last_ts_map)
            if estimated is not None:
                cadence_map[inst] = estimated

        prom_results_map = {
            "first_ts": first_ts_map,
            "last_ts": last_ts_map,
            "count": count_map,
            "avail": avail_map,
            "incidents": incidents_map,
            "duration": duration_map,
        }

        t_merge_start = time.perf_counter()
        entries, summary_dict = merge_hybrid_fleet_availability(
            req_start=req_start,
            req_end=req_end,
            monitored_instances=monitored_instances,
            sqlite_buckets=db_bucket_records,
            prom_results_map=prom_results_map,
            expected_interval_sec=cadence_map,
            maintenance_by_instance=maint_by_inst,
            sla_threshold_by_instance=sla_target_map,
        )
        t_merge_end = time.perf_counter()
        merge_duration_ms = (t_merge_end - t_merge_start) * 1000.0

        fleet_sla_budget = _attach_sla_budgets(
            summary_dict, query.minutes * 60.0, query.sla_target_pct, query.sla_days, target_map=sla_target_map
        )
        hybrid_meta = summary_dict.get("hybrid", {})
        counts = _availability_status_counts(entries, live_map)

        lowest_availability = sorted(
            [e for e in summary_dict["per_server"]["values"] if e.get("availability_pct") is not None and e["availability_pct"] < 100.0],
            key=lambda e: (
                e["availability_pct"],
                -(e.get("downtime_minutes") or 0.0),
                -(e.get("incidents") or 0),
            ),
        )

        trend_series, trend_slot_sec = _build_fleet_trend(req_end, query.minutes * 60.0, monitored_instances)
        t_done = time.perf_counter()

        trace_data = {
            "route": "/api/availability",
            "path": "hybrid_path",
            "minutes": query.minutes_int,
            "flight_wait_ms": round(flight_wait_ms, 2),
            "sqlite_duration_ms": round(sqlite_duration_ms, 2),
            "sqlite_record_count": len(db_bucket_records),
            "prom_duration_ms": round(prom_duration_ms, 2),
            "prom_query_count": len([q for q in queries.values() if q]),
            "prom_queries": prom_query_timings,
            "hybrid_merge_ms": round(merge_duration_ms, 2),
            "materialize_ms": 0.0,
            "total_backend_ms": round((t_done - t_req_start) * 1000.0, 2),
        }

        return self._assemble_payload(
            query=query,
            req_end=req_end,
            monitored_instances=monitored_instances,
            summary_dict=summary_dict,
            hybrid_meta=hybrid_meta,
            fleet_sla_budget=fleet_sla_budget,
            counts=counts,
            lowest_availability=lowest_availability,
            trend_series=trend_series,
            trend_slot_sec=trend_slot_sec,
            source=hybrid_meta.get("source", "fallback"),
            data_status=hybrid_meta.get("data_status", "PARTIAL"),
            trace_data=trace_data,
        )

    def _assemble_payload(
        self,
        query: AvailabilityQuery,
        req_end: float,
        monitored_instances: List[str],
        summary_dict: Dict[str, Any],
        hybrid_meta: Dict[str, Any],
        fleet_sla_budget: Any,
        counts: Dict[str, int],
        lowest_availability: List[Dict[str, Any]],
        trend_series: List[Dict[str, Any]],
        trend_slot_sec: int,
        source: str,
        data_status: str,
        trace_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "ok": True,
            "period_minutes": round(query.minutes, 2),
            "requested_window_seconds": hybrid_meta.get("requested_window_seconds", round(query.minutes * 60.0, 1)),
            "coverage_seconds": hybrid_meta.get("coverage_seconds", 0.0),
            "unknown_seconds": hybrid_meta.get("unknown_seconds", 0.0),
            "missing_seconds": hybrid_meta.get("missing_seconds", hybrid_meta.get("unknown_seconds", 0.0)),
            "sqlite_seconds": hybrid_meta.get("sqlite_seconds", 0.0),
            "prometheus_seconds": hybrid_meta.get("prometheus_seconds", 0.0),
            "overlap_removed_seconds": hybrid_meta.get("overlap_removed_seconds", 0.0),
            "maintenance_excluded_seconds": hybrid_meta.get("maintenance_excluded_seconds", 0.0),
            "maintenance_scheduled_seconds": hybrid_meta.get("maintenance_scheduled_seconds", 0.0),
            "sla": fleet_sla_budget,
            "coverage_percent": hybrid_meta.get("coverage_percent", 0.0),
            "availability_percent": summary_dict["fleet_aggregate"]["value"],
            "data_status": data_status,
            "telemetry_audit": hybrid_meta.get("telemetry_audit", summary_dict.get("telemetry_audit", {})),
            "end": query.end_ts,
            "counts": {
                "total": len(monitored_instances),
                "scored": summary_dict.get("scored_count", 0),
                "eligible": summary_dict.get("eligible_count", 0),
                "online": counts["online"],
                "warning": counts["warning"],
                "offline": counts["offline"],
            },
            "overall": summary_dict["fleet_aggregate"]["value"],
            "fleet_aggregate": summary_dict["fleet_aggregate"],
            "fleet_average": summary_dict["fleet_average"],
            "health_ratio": summary_dict["health_ratio"],
            "zero_downtime_ratio": summary_dict.get("zero_downtime_ratio"),
            "sla_compliance": summary_dict.get("sla_compliance"),
            "sla_compliance_ratio": summary_dict.get("sla_compliance_ratio"),
            "coverage_ratio": summary_dict.get("coverage_ratio"),
            "per_server": summary_dict["per_server"],
            "lowest_availability": lowest_availability,
            "hosts_requiring_attention": lowest_availability,
            "entries": summary_dict["per_server"]["values"],
            "targets": {e["id"]: e["availability_pct"] for e in summary_dict["per_server"]["values"]},
            "analytics": summary_dict.get("analytics", {}),
            "trend": trend_series,
            "trend_end_ts": int(req_end),
            "trend_start_ts": int(req_end - query.minutes * 60.0),
            "trend_bucket_seconds": trend_slot_sec,
            "source": source,
            "_trace": trace_data,
        }

    def _build_empty_payload(self, query: AvailabilityQuery, req_end: float, empty_summary: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "ok": True,
            "period_minutes": round(query.minutes, 2),
            "requested_window_seconds": round(query.minutes * 60.0, 1),
            "coverage_seconds": 0.0,
            "unknown_seconds": round(query.minutes * 60.0, 1),
            "sqlite_seconds": 0.0,
            "prometheus_seconds": 0.0,
            "overlap_removed_seconds": 0.0,
            "coverage_percent": 0.0,
            "availability_percent": None,
            "data_status": "NO_DATA",
            "end": query.end_ts,
            "counts": {
                "total": 0,
                "scored": 0,
                "eligible": 0,
                "online": 0,
                "warning": 0,
                "offline": 0,
            },
            "overall": None,
            "sla": sla_budget(0.0, 0.0, query.sla_target_pct, query.minutes * 60.0, query.sla_days),
            "fleet_aggregate": empty_summary["fleet_aggregate"],
            "fleet_average": empty_summary["fleet_average"],
            "health_ratio": empty_summary["health_ratio"],
            "zero_downtime_ratio": empty_summary.get("zero_downtime_ratio"),
            "sla_compliance": empty_summary.get("sla_compliance"),
            "sla_compliance_ratio": empty_summary.get("sla_compliance_ratio"),
            "coverage_ratio": empty_summary.get("coverage_ratio"),
            "per_server": empty_summary["per_server"],
            "lowest_availability": [],
            "hosts_requiring_attention": [],
            "entries": [],
            "targets": {},
            "analytics": empty_summary.get("analytics", {}),
            "trend": [],
            "trend_end_ts": int(req_end),
            "trend_start_ts": int(req_end - query.minutes * 60.0),
            "trend_bucket_seconds": 3600,
            "source": "nodata",
        }

    @staticmethod
    def _availability_aggregation_windows(now: float, latest_end: Optional[float]) -> List[Tuple[float, float]]:
        import math
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
