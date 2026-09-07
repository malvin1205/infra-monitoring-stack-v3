"""
Fleet availability & SLA calculator.

Menghitung metrik ketersediaan, coverage, dan SLA terverifikasi secara matematis:
  1. per_server          — availability, uptime, downtime, coverage, unknown, dan SLA eligibility
  2. fleet_aggregate     — weighted availability berdasarkan total coverage waktu (metrik SLA enterprise)
  3. fleet_average       — rata-rata unweighted dari target dengan availability valid
  4. sla_compliance      — persentase target SLA-eligible yang memenuhi target >= 99.9%
  5. health_ratio        — persentase target valid yang zero downtime (100% up)
  6. coverage_ratio      — persentase observasi data aktual terhadap requested window
  7. analytics           — total insiden, mean, median (P50), P95, dan max outage duration


BUCKET & MERGE INVARIANTS (the contract every path here must preserve)
─────────────────────────────────────────────────────────────────────
An `availability_buckets` row, and every dict returned by clip_hourly_bucket()
/ merge_hybrid_target_availability(), holds ACTUAL OBSERVED SECONDS — never a
nominal or projected figure. For a completed hour the observed interval is the
whole [bucket_start, bucket_end]; for the in-progress hour it is only
[bucket_start, now]. A bucket is stored with a nominal hour end for schema
convenience, so consumers must clip the *time interval* to `now` and must NOT
re-scale the already-observed seconds a second time (see clip_hourly_bucket).

For any bucket / clipped result / per-target entry over an effective interval
of `dur` seconds ending no later than `now`:

  I1  uptime_seconds + downtime_seconds == coverage_seconds        (± 0.05)
  I2  coverage_seconds <= dur                                      (± 0.05)
  I3  coverage_seconds <= elapsed  (bucket_end is clipped to now;  no future
                                    time is ever observed or "unknown")
  I4  unknown_seconds == dur - coverage_seconds                    (>= 0)
  I5  availability_pct is None  iff  coverage_seconds == 0 ;
      otherwise 0 <= availability_pct <= 100  and  == uptime/coverage*100
  I6  coverage_percent + unknown_percent == 100                    (± 0.05)

availability_pct (= uptime/coverage) and coverage_percent (= coverage/
requested_window) use DIFFERENT denominators on purpose: the first is the SLA
number, the second is how much of the window we actually observed. Unmonitored
time is excluded from the SLA denominator, never counted as downtime.

reconstruct_time_series_intervals() enforces the same I1-I6 from raw samples
(its own "Mathematical Invariants Enforced" list) and is the exact reference
for what the bucket path approximates.
"""

import json
import logging
import math
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("infrawatch.availability")

ServerRecord = Dict[str, Union[str, int, float, datetime]]

DEFAULT_MIN_SLA_COVERAGE_PERCENT = 50.0
SLA_COMPLIANCE_THRESHOLD = 99.9
def _default_scrape_interval_sec() -> float:
    """Fallback expected_interval_sec when a caller doesn't pass one explicitly.
    Mirrors app.py's SCRAPE_INTERVAL_SECONDS so both stay in sync with
    prometheus.yml's global.scrape_interval without a second config knob."""
    try:
        return float(os.environ.get("SCRAPE_INTERVAL_SECONDS", "2.0"))
    except (TypeError, ValueError):
        return 2.0


DEFAULT_SCRAPE_INTERVAL_SEC = _default_scrape_interval_sec()
DEFAULT_GAP_TOLERANCE = 3.0  # Gap > 3x scrape interval is classified as UNKNOWN


def get_min_sla_coverage_percent() -> float:
    """Minimum observed-coverage percent a target needs to be SLA-eligible at
    all (below this, status is always INSUFFICIENT_DATA regardless of the
    availability number). Overridable via MIN_SLA_COVERAGE_PERCENT so ops can
    tune it per-deployment without a code change/redeploy."""
    val = os.environ.get("MIN_SLA_COVERAGE_PERCENT")
    if val is not None:
        try:
            return max(0.0, min(100.0, float(val)))
        except (TypeError, ValueError):
            pass
    return DEFAULT_MIN_SLA_COVERAGE_PERCENT


def get_sla_target_pct() -> float:
    """The SLA availability target a target/fleet is measured against.
    Overridable via SLA_TARGET_PCT; /api/availability can also override it
    per-request with ?sla_target=. Default = the compliance threshold (99.9)."""
    val = os.environ.get("SLA_TARGET_PCT")
    if val is not None:
        try:
            return max(0.0, min(100.0, float(val)))
        except (TypeError, ValueError):
            pass
    return SLA_COMPLIANCE_THRESHOLD


# ── Availability / SLA settings persistence ─────────────────────────────────
# Same JSON-file + get/save pattern as telegram_notifier.get_telegram_config /
# save_telegram_config -- the app's one existing "settings" mechanism, reused
# here rather than inventing a second one.
AVAILABILITY_SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "availability_settings.json")


def get_availability_settings() -> Dict[str, Any]:
    """Load availability/SLA settings. Default (use_node_exporter_correlation
    = False) preserves today's probe-only behavior exactly -- nothing below
    reads this setting, callers must gate on it explicitly."""
    settings: Dict[str, Any] = {"use_node_exporter_correlation": False}
    if os.path.exists(AVAILABILITY_SETTINGS_FILE):
        try:
            with open(AVAILABILITY_SETTINGS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                if isinstance(saved, dict):
                    settings.update(saved)
        except Exception as e:
            logger.warning("Failed to read %s: %s", AVAILABILITY_SETTINGS_FILE, e)
    settings["use_node_exporter_correlation"] = bool(settings.get("use_node_exporter_correlation", False))
    return settings


def save_availability_settings(new_settings: Dict[str, Any]) -> bool:
    """Persist availability/SLA settings to availability_settings.json."""
    try:
        current = get_availability_settings()
        current.update(new_settings)
        with open(AVAILABILITY_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2)
        return True
    except Exception as e:
        logger.error("Failed to save availability settings: %s", e)
        return False


# ── Node Exporter infrastructure correlation (classification only) ─────────
# Deliberately NOT part of the availability/SLA math above: classify_probe_failure
# is a pure function that only ever labels an ALREADY-DOWN probe result. It
# never runs when OFF, never touches availability_pct / uptime_seconds /
# downtime_seconds / coverage_seconds / weighting / SLA compliance, and never
# turns a down probe into "up". Callers (the live target loop in app.py) are
# responsible for finding node_exporter_found/node_exporter_healthy per
# target (e.g. a Prometheus `up{job="node_exporter"}` reading matched by
# host) -- this function has no I/O, so it's trivially unit-testable and
# there is exactly one place ("here") that decides service vs. infrastructure.
CORRELATION_SERVICE_ISSUE = "service_issue"
CORRELATION_INFRA_ISSUE = "infrastructure_issue"
CORRELATION_NO_NODE_EXPORTER = "no_node_exporter"


def classify_probe_failure(
    probe_is_down: bool,
    node_exporter_found: bool,
    node_exporter_healthy: Optional[bool],
) -> Optional[Dict[str, str]]:
    """Classify a DOWN probe as service-level vs. infrastructure-level using
    Node Exporter as a secondary signal.

    Returns None when the probe isn't down -- there's nothing to classify,
    and a healthy probe's availability is never influenced by this function
    either way. A target with no Node Exporter telemetry gets
    CORRELATION_NO_NODE_EXPORTER, never CORRELATION_INFRA_ISSUE: missing
    telemetry is evidence of nothing, and must not be read as an outage.
    """
    if not probe_is_down:
        return None
    if not node_exporter_found:
        return {
            "correlation": CORRELATION_NO_NODE_EXPORTER,
            "detail": "No Node Exporter telemetry for this target — probe-based classification only.",
        }
    if node_exporter_healthy:
        return {
            "correlation": CORRELATION_SERVICE_ISSUE,
            "detail": "Probe is down but Node Exporter reports the host healthy — likely a service/application issue.",
        }
    return {
        "correlation": CORRELATION_INFRA_ISSUE,
        "detail": "Probe is down and Node Exporter is also unavailable — likely an infrastructure/host issue.",
    }


def sla_budget(
    observed_downtime_sec: float,
    observed_seconds: float,
    target_pct: Optional[float] = None,
    window_seconds: Optional[float] = None,
    project_days: int = 30,
) -> Dict[str, Any]:
    """SLA error-budget accounting for one target or the fleet.

    `observed_*` should be the maintenance-EXCLUDED (SLA) figures.

      window.*     — allowed vs used vs remaining downtime over the observed
                     window (allowed = (1 - target/100) * window_seconds,
                     i.e. the article's downtime table, computed).
      projected.*  — the observed downtime RATE extrapolated to `project_days`
                     vs that period's budget: the "are we going to breach"
                     signal a 24h/7d window can't show on its own.
    """
    if target_pct is None:
        target_pct = get_sla_target_pct()
    target_pct = _clamp_pct(target_pct)
    err_frac = max(0.0, 1.0 - target_pct / 100.0)

    obs = max(0.0, float(observed_seconds or 0.0))
    dt = max(0.0, float(observed_downtime_sec or 0.0))
    win = float(window_seconds) if window_seconds and float(window_seconds) > 0 else obs

    allowed_win = err_frac * win
    remaining_win = allowed_win - dt
    if allowed_win > 0:
        used_pct = round(min(999.9, (dt / allowed_win) * 100.0), 1)
    else:
        used_pct = 0.0 if dt <= 0 else 999.9

    period_sec = max(1, int(project_days)) * 86400.0
    allowed_period = err_frac * period_sec
    rate = (dt / obs) if obs > 0 else 0.0
    projected_dt = rate * period_sec

    return {
        "target_pct": round(target_pct, 3),
        "has_data": obs > 0,
        "window": {
            "seconds": round(win, 1),
            "allowed_downtime_seconds": round(allowed_win, 1),
            "observed_downtime_seconds": round(dt, 1),
            "remaining_seconds": round(remaining_win, 1),
            "used_percent": max(0.0, used_pct),
            "breached": dt > allowed_win + 1e-6,
        },
        "projected": {
            "days": int(project_days),
            "allowed_downtime_seconds": round(allowed_period, 1),
            "downtime_seconds": round(projected_dt, 1),
            "remaining_seconds": round(allowed_period - projected_dt, 1),
            "breach": projected_dt > allowed_period + 1e-6,
        },
    }


def _parse_created_at(created_at: Union[str, int, float, datetime]) -> datetime:
    """Normalize created_at (epoch seconds, ISO 8601 string, atau datetime) ke aware UTC datetime."""
    if isinstance(created_at, datetime):
        return created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
    if isinstance(created_at, (int, float)):
        return datetime.fromtimestamp(created_at, tz=timezone.utc)
    if isinstance(created_at, str):
        s = created_at.strip()
        dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace("z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    raise ValueError(f"Unsupported created_at type: {type(created_at)!r}")


def _clamp_pct(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _outage_json(bucket: Dict[str, Any]) -> Dict[str, Any]:
    """Parse a bucket's outage_json (a JSON string from the aggregator, a dict
    in tests, absent on legacy rows) into a dict — {} when unavailable."""
    oj = bucket.get("outage_json")
    if isinstance(oj, str):
        try:
            oj = json.loads(oj)
        except (ValueError, TypeError):
            return {}
    return oj if isinstance(oj, dict) else {}


def _outage_flag(bucket: Dict[str, Any], key: str) -> bool:
    """Read a boolean flag out of a bucket's outage_json (stored as a JSON
    string by the aggregator, may be a dict in tests / absent on legacy rows)."""
    return bool(_outage_json(bucket).get(key))


def _bucket_outage_downtime_in_maintenance(
    bucket: Dict[str, Any],
    clip_start: float,
    clip_end: float,
    maintenance_windows: Optional[List[Tuple[float, float]]],
) -> Optional[float]:
    """Seconds of THIS bucket's actual outages (from outage_json["i"]) that fall
    inside both [clip_start, clip_end] and a maintenance window. Returns None
    when the bucket carries no per-outage intervals (legacy row / approximate
    fallback path) — the caller then keeps the older coarse estimate."""
    intervals = _outage_json(bucket).get("i")
    if not isinstance(intervals, list):
        return None
    total = 0.0
    for iv in intervals:
        try:
            o_s = max(float(clip_start), float(iv[0]))
            o_e = min(float(clip_end), float(iv[1]))
        except (TypeError, ValueError, IndexError):
            continue
        if o_e > o_s:
            # maintenance_overlap_seconds() merges overlapping windows, so an
            # outage isn't excused twice by two windows that touch.
            total += maintenance_overlap_seconds(maintenance_windows, o_s, o_e)
    return total


def calculate_percentile(values: List[float], percentile: float) -> Optional[float]:
    """Calculate percentile (0.0 to 100.0) from a list of numeric values using linear interpolation."""
    if not values:
        return None
    sorted_vals = sorted(values)
    if len(sorted_vals) == 1:
        return round(float(sorted_vals[0]), 2)
    k = (len(sorted_vals) - 1) * (percentile / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return round(float(sorted_vals[int(k)]), 2)
    d0 = sorted_vals[int(f)] * (c - k)
    d1 = sorted_vals[int(c)] * (k - f)
    return round(float(d0 + d1), 2)


def derive_bucket_inputs(
    instances: List[str],
    probe_results: Dict[str, Dict[str, Any]],
    up_results: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Classify each instance as a Blackbox probe target or a node/exporter
    target, then merge every named Prometheus query result (avail/count/
    first_ts/last_ts/incidents/live/...) from whichever of probe_results /
    up_results matches that classification.

    Used identically by both places in app.py that turn raw probe_success/up
    query results into per-instance availability inputs — api_availability's
    SQLite-materialize path and the background _aggregate_availability_cycle
    — which duplicated this exact classification (including the
    node/exporter name heuristic below) before it was extracted here.

    probe_results / up_results: {metric_name: {instance: value}}, e.g.
    {"avail": probe_avail_map, "count": probe_count_map, ...}. A metric only
    needs to be present in whichever dict(s) actually queried it — a caller
    that never queried e.g. "live" simply omits that key from both.
    """
    metric_names = set(probe_results) | set(up_results)
    merged: Dict[str, Dict[str, Any]] = {name: {} for name in metric_names}
    probe_avail = probe_results.get("avail", {})
    probe_count = probe_results.get("count", {})
    up_avail = up_results.get("avail", {})
    up_count = up_results.get("count", {})
    for inst in instances:
        # Route each instance to whichever series ACTUALLY has data for it
        # (audit F14) — a node_exporter target named by bare IP (e.g.
        # "10.0.0.5:9100") never matched the old "node"/"exporter" name
        # substring and was silently forced onto probe_success (empty ->
        # under-counted coverage). Probe wins when both have data (an explicit
        # blackbox target); the name hint is only a last-resort tiebreaker
        # when neither series carries samples, where the choice is moot.
        probe_has = inst in probe_avail or inst in probe_count
        up_has = inst in up_avail or inst in up_count
        if probe_has:
            source = probe_results
        elif up_has:
            source = up_results
        elif "node" in inst.lower() or "exporter" in inst.lower():
            source = up_results
        else:
            source = probe_results
        for name in metric_names:
            m = source.get(name, {})
            if inst in m:
                merged[name][inst] = m[inst]
    return merged


def estimate_instance_cadence(
    instance: str,
    count_map: Dict[str, Any],
    first_ts_map: Dict[str, Any],
    last_ts_map: Dict[str, Any],
) -> Optional[float]:
    """Observed per-instance scrape cadence (seconds/sample), from the span
    between an instance's first and last sample over its sample count in a
    query window. Returns None when there isn't enough data to estimate from
    (fewer than 2 samples, or a zero/negative span) — callers fall back to a
    different cadence source in that case.

    Used identically by api_availability (applied directly, per-instance)
    and _aggregate_availability_cycle (collected across instances into a
    fleet median) — the estimation formula itself was duplicated in both
    before this was extracted."""
    rc = count_map.get(instance)
    f_ts = float(first_ts_map.get(instance, 0))
    l_ts = float(last_ts_map.get(instance, 0))
    if rc and f_ts > 0 and l_ts > 0 and (l_ts - f_ts) > 0:
        try:
            c_num = float(rc)
            if c_num >= 2:
                return (l_ts - f_ts) / (c_num - 1)
        except (ValueError, TypeError):
            pass
    return None


def clip_hourly_bucket(
    bucket: Dict[str, Any],
    clip_start: float,
    clip_end: float,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """
    Proportionally clips an hourly or aggregated SQLite bucket to [clip_start, clip_end].
    Enforces bucket invariants and rejects corrupted / non-chronological records.

    `now` is the wall-clock upper bound (the effective "as of" time of the
    request). An in-progress hour is stored with a *nominal* hour end
    (e.g. bucket_start=13:00, bucket_end=14:00) but its uptime/downtime/
    coverage were only ever accumulated over the elapsed part [13:00, now].
    The proportional scaling below assumes those totals are spread across the
    whole [b_start, b_end] span, so without clamping b_end to `now` first it
    would scale an already-observed 1200s of coverage down a second time
    (1200 * 1200/3600 = 400s), silently turning elapsed-and-observed time
    into "unknown". Clamp the bucket's effective interval to `now`; for any
    completed bucket (b_end <= now) this is a no-op.

    Guarantees on the returned dict (None when the bucket is corrupt or does
    not intersect [clip_start, min(clip_end, now)]) — see the module-level
    "BUCKET & MERGE INVARIANTS":
      uptime_seconds + downtime_seconds == coverage_seconds  (exact)
      coverage_seconds <= duration_seconds == inter_end - inter_start
      unknown_seconds  == duration_seconds - coverage_seconds  (>= 0)
      inter_end <= min(clip_end, now)   (no future time)
      availability_pct is None iff coverage_seconds == 0, else 0..100
    """
    try:
        b_start = float(bucket.get("bucket_start") if bucket.get("bucket_start") is not None else bucket.get("earliest_bucket_start", 0))
        b_end = float(bucket.get("bucket_end") if bucket.get("bucket_end") is not None else bucket.get("latest_bucket_end", 0))
    except (ValueError, TypeError):
        return None

    if b_end <= b_start:
        return None

    eff_end = b_end if now is None else min(b_end, float(now))
    if eff_end <= b_start:
        return None

    inter_start = max(b_start, clip_start)
    inter_end = min(eff_end, clip_end)
    inter_dur = max(0.0, inter_end - inter_start)
    if inter_dur <= 0.0:
        return None

    b_dur = max(1.0, eff_end - b_start)
    ratio = min(1.0, max(0.0, inter_dur / b_dur))

    raw_up = max(0.0, float(bucket.get("total_uptime_sec", bucket.get("uptime_seconds", 0.0)) or 0.0))
    raw_down = max(0.0, float(bucket.get("total_downtime_sec", bucket.get("downtime_seconds", 0.0)) or 0.0))
    raw_cov = max(0.0, float(bucket.get("total_coverage_sec", bucket.get("coverage_seconds", 0.0)) or (raw_up + raw_down)))
    raw_cov = min(b_dur, raw_cov)

    cov_sec = max(0.0, min(inter_dur, round(raw_cov * ratio, 2)))
    up_sec = max(0.0, min(cov_sec, round(raw_up * ratio, 2)))
    down_sec = max(0.0, min(round(cov_sec - up_sec, 2), round(raw_down * ratio, 2)))
    # Ensure uptime + downtime == coverage
    cov_sec = round(up_sec + down_sec, 2)
    unk_sec = max(0.0, round(inter_dur - cov_sec, 2))

    samples = int(round(float(bucket.get("total_samples", bucket.get("sample_count", 0)) or 0) * ratio))
    incidents = int(bucket.get("total_incidents", bucket.get("incident_count", 0)) or 0) if ratio >= 0.5 else (1 if down_sec > 0 else 0)
    latency = float(bucket.get("mean_latency_ms", bucket.get("avg_latency_ms", 0.0)) or 0.0)

    avail_pct = round(_clamp_pct((up_sec / cov_sec) * 100.0), 2) if cov_sec > 0 else None

    return {
        "instance": bucket.get("instance"),
        "job": bucket.get("job", "blackbox"),
        "bucket_start": inter_start,
        "bucket_end": inter_end,
        "duration_seconds": inter_dur,
        "uptime_seconds": up_sec,
        "downtime_seconds": down_sec,
        "coverage_seconds": cov_sec,
        "unknown_seconds": unk_sec,
        "sample_count": samples,
        "incident_count": incidents,
        "avg_latency_ms": latency,
        "availability_pct": avail_pct,
        "source": "sqlite",
    }


def maintenance_overlap_seconds(
    windows: Optional[List[Tuple[float, float]]],
    req_start: float,
    req_end: float,
) -> float:
    """Total seconds of [req_start, req_end] covered by the UNION of the given
    maintenance windows (list of (start_ts, end_ts) already scoped to one
    target). Overlapping windows are merged so time is never counted twice."""
    if not windows or req_end <= req_start:
        return 0.0
    clipped: List[Tuple[float, float]] = []
    for w in windows:
        try:
            s = max(float(req_start), float(w[0]))
            e = min(float(req_end), float(w[1]))
        except (TypeError, ValueError, IndexError):
            continue
        if e > s:
            clipped.append((s, e))
    if not clipped:
        return 0.0
    clipped.sort()
    total = 0.0
    cur_s, cur_e = clipped[0]
    for s, e in clipped[1:]:
        if s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            total += cur_e - cur_s
            cur_s, cur_e = s, e
    total += cur_e - cur_s
    return total


def merge_hybrid_target_availability(
    req_start: float,
    req_end: float,
    target_id: str,
    target_name: str,
    job: str,
    sqlite_buckets: List[Dict[str, Any]],
    prom_metrics: Dict[str, Any],
    expected_interval_sec: float = DEFAULT_SCRAPE_INTERVAL_SEC,
    gap_tolerance: float = DEFAULT_GAP_TOLERANCE,
    min_sla_coverage_pct: Optional[float] = None,
    sla_threshold: float = SLA_COMPLIANCE_THRESHOLD,
    maintenance_windows: Optional[List[Tuple[float, float]]] = None,
) -> Dict[str, Any]:
    """
    Computes per-target hybrid availability by merging authoritative Prometheus telemetry
    with historical SQLite hourly buckets without overlap or double counting.

    maintenance_windows: (start_ts, end_ts) pairs scoped to THIS target. The
    observed fraction of that time is carved out of the SLA denominator
    (attributed downtime-first): `availability_pct` stays the raw
    maintenance-included number, while `availability_pct_excl_maintenance`
    (and the SLA compliance status) exclude planned downtime. No windows ->
    the two are identical and nothing changes.
    """
    if min_sla_coverage_pct is None:
        min_sla_coverage_pct = get_min_sla_coverage_percent()

    window_sec = max(0.0, float(req_end - req_start))
    if window_sec <= 0.0:
        return {
            "id": target_id,
            "name": target_name,
            "job": job,
            "availability_pct": None,
            "coverage_minutes": 0.0,
            "denominator_minutes": 0.0,
            "observed_minutes": 0.0,
            "observed_seconds": 0.0,
            "uptime_minutes": 0.0,
            "uptime_seconds": 0.0,
            "downtime_minutes": 0.0,
            "downtime_seconds": 0.0,
            "unknown_minutes": 0.0,
            "unknown_seconds": 0.0,
            "sqlite_seconds": 0.0,
            "prometheus_seconds": 0.0,
            "overlap_removed_seconds": 0.0,
            "coverage_pct": 0.0,
            "coverage_percent": 0.0,
            "unknown_pct": 100.0,
            "unknown_percent": 100.0,
            "is_limited_data": False,
            "is_no_data": True,
            "sla_eligible": False,
            "sla_compliant": False,
            "sla_status": "INSUFFICIENT_DATA",
            "sla_eligibility_reason": "Zero window duration",
            "data_status": "NO_DATA",
            "source": "nodata",
            "incidents": 0,
            "incident_count": 0,
            "sample_count": 0,
            "avg_latency_ms": 0.0,
        }

    # 1. Inspect Prometheus telemetry for this specific target
    raw_first_ts = prom_metrics.get("first_ts")
    raw_last_ts = prom_metrics.get("last_ts")
    raw_count = prom_metrics.get("count")
    raw_avail = prom_metrics.get("avail")
    raw_incidents = prom_metrics.get("incidents")
    raw_duration = prom_metrics.get("duration")

    f_ts = float(raw_first_ts) if raw_first_ts is not None else 0.0
    l_ts = float(raw_last_ts) if raw_last_ts is not None else 0.0
    s_count = None
    if raw_count is not None:
        try:
            s_count = int(float(raw_count))
        except (ValueError, TypeError):
            s_count = None

    has_prom = (s_count is not None and s_count > 0) or (raw_avail is not None) or (f_ts > 0 and l_ts >= f_ts)

    prom_cov_sec = 0.0
    prom_up_sec = 0.0
    prom_down_sec = 0.0
    prom_inc = 0
    prom_lat = float(raw_duration or 0.0) if raw_duration is not None else 0.0

    if has_prom:
        eff_cadence = expected_interval_sec if expected_interval_sec > 0 else DEFAULT_SCRAPE_INTERVAL_SEC

        # Calculate Prometheus coverage seconds
        full_window_from_avail = False
        if s_count is not None and s_count >= 2 and l_ts > f_ts and f_ts > 0:
            span_sec = l_ts - f_ts
            cadence_est = span_sec / (s_count - 1)
            if cadence_est <= gap_tolerance * eff_cadence:
                lead_in = min(cadence_est, max(0.0, f_ts - req_start)) if (f_ts - req_start) <= gap_tolerance * eff_cadence else 0.0
                lead_out = min(cadence_est, max(0.0, req_end - l_ts)) if (req_end - l_ts) <= gap_tolerance * eff_cadence else 0.0
                prom_cov_sec = min(span_sec + lead_in + lead_out, window_sec)
            else:
                prom_cov_sec = min(float(s_count) * eff_cadence, window_sec)
        elif s_count is not None and s_count == 1:
            prom_cov_sec = min(eff_cadence, window_sec)
        elif s_count is not None and s_count > 0:
            prom_cov_sec = min(float(s_count) * eff_cadence, window_sec)
        elif raw_avail is not None:
            prom_cov_sec = window_sec
            full_window_from_avail = True
        elif f_ts > 0 and l_ts > 0 and l_ts >= f_ts:
            prom_cov_sec = max(0.0, min(req_end, l_ts) - max(req_start, f_ts))
        else:
            prom_cov_sec = 0.0

        # Determine P_start/P_end boundary — this bounds where SQLite is
        # still allowed to contribute (only the non-overlapping remainder).
        # When avg_over_time already implies the *entire* window is covered
        # (full_window_from_avail), that must mean prom claims all of it —
        # forcing p_start/p_end to the full window regardless of what an
        # independent first/last-timestamp query happened to return.
        # Otherwise f_ts can land strictly inside the window (e.g. the
        # instance was added mid-window) while prom_cov_sec still equals the
        # *whole* window_sec, leaving [req_start, f_ts] open for SQLite to
        # also claim — double-counting that slice between the two sources.
        if full_window_from_avail:
            p_start = req_start
            p_end = req_end
        else:
            if f_ts > 0 and f_ts > req_start and f_ts <= req_end:
                p_start = f_ts
            elif prom_cov_sec > 0:
                p_start = max(req_start, req_end - prom_cov_sec)
            else:
                p_start = req_end

            if l_ts > 0 and l_ts < req_end and l_ts >= req_start:
                p_end = l_ts
            else:
                p_end = req_end

        # Only attribute an up/down split when there's an actual rate signal
        # (a reported avail% or a sample count) — p_start/p_end above may
        # still use the bare first/last-timestamp fallback to bound the
        # SQLite overlap window, but that fallback alone (no count, no
        # avail%) carries no evidence of up vs. down, so it shouldn't default
        # to "fully up" here; leave it unattributed and let the live status
        # snapshot (checked elsewhere) speak for current state instead.
        avail_rate = None
        if raw_avail is not None:
            try:
                avail_rate = max(0.0, min(1.0, float(raw_avail) / 100.0))
            except (ValueError, TypeError):
                # Unparseable, not "no signal" — but a garbage value is no
                # more trustworthy than no value at all. Same "leave it
                # unattributed" rule as the comment above, not "fully up":
                # defaulting to 1.0 here would silently inflate availability
                # for a slice where an actual outage may have occurred.
                avail_rate = None
        elif s_count is not None:
            avail_rate = 1.0

        if avail_rate is not None:
            prom_up_sec = round(prom_cov_sec * avail_rate, 2)
            prom_down_sec = round(prom_cov_sec * (1.0 - avail_rate), 2)
            prom_cov_sec = round(prom_up_sec + prom_down_sec, 2)
        else:
            prom_up_sec = 0.0
            prom_down_sec = 0.0
            prom_cov_sec = 0.0

        if raw_incidents is not None:
            try:
                prom_inc = int(math.ceil(float(raw_incidents) / 2.0))
            except (ValueError, TypeError):
                prom_inc = 0
        if prom_inc == 0 and prom_down_sec > 0:
            prom_inc = 1
    else:
        p_start = req_end
        p_end = req_end

    # 2. Derive non-overlapping SQLite contribution for interval [req_start, min(req_end, p_start)]
    sqlite_clip_start = req_start
    sqlite_clip_end = min(req_end, p_start)
    sqlite_up_sec = 0.0
    sqlite_down_sec = 0.0
    sqlite_cov_sec = 0.0
    sqlite_samples = 0
    sqlite_inc = 0
    sqlite_lat_weighted = 0.0
    overlap_removed_sec = 0.0
    # Maintenance carve-out accumulators — the coverage inside a maintenance
    # window (removed from the SLA denominator) and, of that, how much was
    # downtime (the planned outage that shouldn't count against SLA). Done
    # per clipped bucket interval, not window-globally, so a window over a
    # HEALTHY hour excuses no downtime.
    maint_cov_sec = 0.0
    maint_down_sec = 0.0

    if sqlite_clip_end > sqlite_clip_start:
        for b in sqlite_buckets:
            # Overlap tracking: portion of SQLite bucket that fell into authoritative Prometheus window
            try:
                b_st = float(b.get("bucket_start", 0))
                b_en = float(b.get("bucket_end", 0))
                if b_en > p_start and b_st < req_end:
                    overlap_dur = max(0.0, min(b_en, req_end) - max(b_st, p_start))
                    overlap_removed_sec += overlap_dur
            except (ValueError, TypeError):
                pass

            clipped = clip_hourly_bucket(b, sqlite_clip_start, sqlite_clip_end, now=req_end)
            if clipped:
                sqlite_up_sec += clipped["uptime_seconds"]
                sqlite_down_sec += clipped["downtime_seconds"]
                sqlite_cov_sec += clipped["coverage_seconds"]
                sqlite_samples += clipped["sample_count"]
                sqlite_inc += clipped["incident_count"]
                sqlite_lat_weighted += clipped["avg_latency_ms"] * clipped["coverage_seconds"]
                if maintenance_windows:
                    bm = maintenance_overlap_seconds(
                        maintenance_windows, clipped["bucket_start"], clipped["bucket_end"])
                    if bm > 0:
                        maint_cov_sec += min(clipped["coverage_seconds"], bm)
                        # Excuse only the downtime that ACTUALLY fell inside a
                        # maintenance window, from the bucket's per-outage
                        # intervals — not `min(bucket_downtime, bm)`, which
                        # assumed every second of downtime in the hour was
                        # planned even when the outage and the window never
                        # overlapped in time (audit M3). Legacy / approximate
                        # buckets carry no intervals -> fall back to the coarse
                        # estimate for those.
                        exact_md = _bucket_outage_downtime_in_maintenance(
                            b, clipped["bucket_start"], clipped["bucket_end"], maintenance_windows)
                        if exact_md is None:
                            maint_down_sec += min(clipped["downtime_seconds"], bm)
                        else:
                            maint_down_sec += min(clipped["downtime_seconds"], exact_md)

    sqlite_lat = (sqlite_lat_weighted / sqlite_cov_sec) if sqlite_cov_sec > 0 else 0.0

    # 2b. Cross-bucket incident de-dup. reconstruct_time_series_intervals runs
    # per hour, so an outage straddling an hour boundary is counted once in
    # each hour's bucket. Where a fully-contained bucket ends still in outage
    # and the next contiguous bucket begins still in outage, that is one
    # outage seen twice — drop the duplicate. Only fully-contained buckets
    # (a partially clipped boundary bucket already carries an approximate
    # incident count from clip_hourly_bucket). No signal -> no change.
    if sqlite_inc > 1 and len(sqlite_buckets) > 1:
        contained = sorted(
            (b for b in sqlite_buckets
             if _num(b.get("bucket_start")) >= sqlite_clip_start - 1.0
             and _num(b.get("bucket_end")) <= sqlite_clip_end + 1.0),
            key=lambda b: _num(b.get("bucket_start")),
        )
        for prev_b, cur_b in zip(contained, contained[1:]):
            if abs(_num(cur_b.get("bucket_start")) - _num(prev_b.get("bucket_end"))) > 1.0:
                continue  # not contiguous
            if _outage_flag(prev_b, "ongoing_end") and _outage_flag(cur_b, "ongoing_start"):
                sqlite_inc -= 1
        sqlite_inc = max(0, sqlite_inc)

    # Prometheus-attributed interval [p_start, p_end]: only a scalar avail% is
    # known here, no per-outage timestamps, so the carve stays proportional —
    # excuse the maintenance-overlapping FRACTION of this segment's downtime
    # (downtime assumed uniform across the segment), not `min(prom_down_sec, pm)`
    # which excused up to all of it regardless of when the outage happened
    # (audit M3).
    if maintenance_windows and prom_cov_sec > 0:
        pm = maintenance_overlap_seconds(maintenance_windows, p_start, p_end)
        if pm > 0:
            seg_len = max(1.0, p_end - p_start)
            pm_frac = max(0.0, min(1.0, pm / seg_len))
            maint_cov_sec += min(prom_cov_sec, pm)
            maint_down_sec += prom_down_sec * pm_frac

    # 3. Merge non-overlapping intervals — prom_up_sec/prom_down_sec were
    # already computed in step 1 (identical formula there, plus a sane
    # avail_rate=1.0 default for the raw_avail-missing-but-count-present
    # case). Recomputing here used to zero both out whenever raw_avail was
    # None even though prom_cov_sec (and total_cov_sec, derived from it) was
    # still positive — producing entries with observed_seconds > 0 but
    # availability_pct forced to None.
    total_up_sec = round(sqlite_up_sec + prom_up_sec, 2)
    total_down_sec = round(sqlite_down_sec + prom_down_sec, 2)
    raw_cov_sum = (total_up_sec + total_down_sec) if (prom_cov_sec > 0 or sqlite_cov_sec > 0) else prom_cov_sec
    # The SQLite (exact per-hour) + non-overlapping Prometheus contributions
    # must sum to <= the window. Exceeding it means a real accounting bug
    # (bucket clip let future/overlapping time through, or the Prometheus
    # seam double-counted) — the clamp below still keeps the number sane, but
    # log it instead of silently masking, per issue #3 in the SLA audit.
    if raw_cov_sum > window_sec + 1.0:
        logger.warning(
            "availability over-count for %s: sqlite=%.1fs prom=%.1fs sum=%.1fs > window=%.1fs (clamped)",
            target_id, sqlite_cov_sec, prom_cov_sec, raw_cov_sum, window_sec,
        )
    total_cov_sec = round(max(0.0, min(window_sec, raw_cov_sum)), 2)
    total_unk_sec = round(max(0.0, window_sec - total_cov_sec), 2)
    # s_count is count_over_time() over the WHOLE window; the SQLite side only
    # contributes [req_start, min(req_end, p_start)]. When both contribute, the
    # slice [req_start, p_start] is in s_count AND in sqlite_samples — scale
    # s_count down to just the Prometheus-attributed tail so the reported
    # sample_count isn't inflated on the seam (audit m3).
    if sqlite_cov_sec > 0 and window_sec > 0 and p_start > req_start:
        prom_sample_frac = max(0.0, min(1.0, (req_end - p_start) / window_sec))
        total_samples = sqlite_samples + int(round((s_count or 0) * prom_sample_frac))
    else:
        total_samples = sqlite_samples + (s_count or 0)
    total_inc = sqlite_inc + prom_inc

    avg_lat = 0.0
    if total_cov_sec > 0 and (prom_cov_sec > 0 or sqlite_cov_sec > 0):
        avg_lat = round((sqlite_lat * sqlite_cov_sec + prom_lat * prom_cov_sec) / total_cov_sec, 1)
        total_avail = round(_clamp_pct((total_up_sec / total_cov_sec) * 100.0), 2)
    else:
        total_avail = None

    cov_pct = round(_clamp_pct((total_cov_sec / window_sec) * 100.0), 2) if window_sec > 0 else 0.0
    unk_pct = round(_clamp_pct(100.0 - cov_pct), 2)

    # 3b. Maintenance carve-out. `maint_cov_sec` (planned time removed from the
    # SLA denominator) is coarse — the maintenance window's overlap with each
    # covered bucket/segment. `maint_down_sec` (planned downtime not counted
    # against SLA) is exact where the bucket carries per-outage intervals
    # (outage_json["i"]): only downtime whose outage actually overlapped the
    # window in time is excused, so a maintenance window over an hour that also
    # had an unrelated outage no longer wipes that outage from the SLA number.
    # The Prometheus scalar segment has no timestamps, so it stays proportional.
    # `availability_pct` above stays the raw number; SLA status uses `sla_avail`.
    maint_excluded_sec = round(min(total_cov_sec, maint_cov_sec), 2)
    maint_down_excused = round(min(total_down_sec, maint_down_sec, maint_excluded_sec), 2)
    if maint_excluded_sec > 0:
        sla_cov_sec = round(max(0.0, total_cov_sec - maint_excluded_sec), 2)
        sla_down_sec = round(max(0.0, total_down_sec - maint_down_excused), 2)
        sla_up_sec = round(max(0.0, sla_cov_sec - sla_down_sec), 2)
        sla_avail = round(_clamp_pct((sla_up_sec / sla_cov_sec) * 100.0), 2) if sla_cov_sec > 0 else total_avail
    else:
        sla_up_sec, sla_down_sec, sla_cov_sec = total_up_sec, total_down_sec, total_cov_sec
        sla_avail = total_avail
    sla_avail_eff = sla_avail if sla_avail is not None else total_avail

    # 4. Source classification
    if sqlite_cov_sec > 0 and prom_cov_sec > 0:
        target_src = "hybrid"
    elif sqlite_cov_sec > 0:
        target_src = "materialized"
    elif prom_cov_sec > 0:
        target_src = "fallback"
    else:
        target_src = "nodata"

    # 5. First & Last Seen Timestamps
    first_seen_ts = None
    last_seen_ts = None
    all_starts = []
    all_ends = []
    if sqlite_buckets:
        for b in sqlite_buckets:
            try:
                b_st = float(b.get("bucket_start", 0))
                b_en = float(b.get("bucket_end", 0))
                if b_st > 0: all_starts.append(b_st)
                if b_en > 0: all_ends.append(b_en)
            except (ValueError, TypeError):
                pass
    if f_ts > 0: all_starts.append(f_ts)
    if l_ts > 0: all_ends.append(l_ts)
    if all_starts: first_seen_ts = min(all_starts)
    if all_ends: last_seen_ts = max(all_ends)

    # 6. SLA, Diagnostics & Data Status
    if total_cov_sec <= 0 or total_avail is None:
        data_status = "NO_DATA"
        sla_status = "INSUFFICIENT_DATA"
        is_eligible = False
        sla_reason = "Zero observed telemetry in requested window"
        root_cause_code = "NO_DATA"
        root_cause_hint = "No telemetry samples recorded for this target in the selected time window."
        recommendation = "Confirm the target is active and its blackbox/exporter endpoint is scrapable by Prometheus."
    else:
        is_eligible = (cov_pct >= min_sla_coverage_pct)
        if not is_eligible:
            data_status = "INSUFFICIENT_DATA"
            sla_status = "INSUFFICIENT_DATA"
            sla_reason = f"Insufficient coverage ({cov_pct}% < {min_sla_coverage_pct}%)"
            if first_seen_ts and first_seen_ts > (req_start + 1800):
                root_cause_code = "NEW_TARGET"
                root_cause_hint = "Target was first monitored partway through the requested window (late discovery)."
                recommendation = "Use a shorter time window (e.g. 6h/24h) so the availability percentage reflects the target's active period."
            elif sqlite_cov_sec <= 0 and prom_cov_sec > 0:
                root_cause_code = "RETENTION_WINDOW"
                root_cause_hint = "Telemetry is only available in Prometheus's in-memory buffer and has not been fully aggregated into the SQLite archive yet."
                recommendation = "Wait for the periodic aggregation to run, or shorten the window to match the active Prometheus retention."
            else:
                root_cause_code = "SCRAPE_GAPS"
                root_cause_hint = "Gaps were found in this target's telemetry samples during the window."
                recommendation = "Check the target's network stability, or review the blackbox exporter logs for scrape outages."
        else:
            data_status = "COMPLETE" if cov_pct >= 95.0 else "PARTIAL"
            if cov_pct < 95.0:
                root_cause_code = "PARTIAL_COVERAGE"
                root_cause_hint = "Telemetry coverage is adequate for a first look, but there are minor observation gaps."
                recommendation = "A small fraction of data was not observed; the availability figure is valid with moderate confidence."
            else:
                root_cause_code = "OPTIMAL"
                root_cause_hint = "Telemetry coverage is optimal (>=95%) for performance evaluation and SLA audit."
                recommendation = "Telemetry data is complete and representative for a formal SLA audit."

            _maint_note = f" (excl. {round(maint_excluded_sec / 60.0, 1)}m planned)" if maint_excluded_sec > 0 else ""
            if sla_avail_eff >= sla_threshold:
                sla_status = "COMPLIANT"
                sla_reason = f"Availability ({sla_avail_eff}%) >= {sla_threshold}%{_maint_note}"
            else:
                sla_status = "NON_COMPLIANT"
                sla_reason = f"Availability ({sla_avail_eff}%) < {sla_threshold}%{_maint_note}"

    if cov_pct >= 95.0:
        confidence_level = "HIGH"
        confidence_score = 100
        conf_badge = "conf-high"
    elif cov_pct >= (min_sla_coverage_pct or DEFAULT_MIN_SLA_COVERAGE_PERCENT):
        confidence_level = "MODERATE"
        confidence_score = round(cov_pct)
        conf_badge = "conf-mid"
    else:
        confidence_level = "LOW"
        confidence_score = round(cov_pct)
        conf_badge = "conf-low"

    telemetry_audit = {
        "requested_window_seconds": round(window_sec, 1),
        "observed_seconds": round(total_cov_sec, 1),
        "missing_seconds": round(total_unk_sec, 1),
        "maintenance_excluded_seconds": maint_excluded_sec,
        "coverage_percent": cov_pct,
        "missing_percent": unk_pct,
        "first_seen_ts": first_seen_ts,
        "last_seen_ts": last_seen_ts,
        "root_cause_code": root_cause_code,
        "root_cause_hint": root_cause_hint,
        "confidence_level": confidence_level,
        "confidence_score": confidence_score,
        "confidence_badge": conf_badge,
        "sla_eligible": is_eligible,
        "sla_status": sla_status,
        "sla_reason": sla_reason,
        "recommendation": recommendation,
        "maintenance_excluded_seconds": maint_excluded_sec,
        "availability_pct_excl_maintenance": sla_avail,
        "storage": {
            "source": target_src,
            "sqlite_seconds": round(sqlite_cov_sec, 1),
            "prometheus_seconds": round(prom_cov_sec, 1),
            "overlap_removed_seconds": round(overlap_removed_sec, 1),
            "sample_count": total_samples,
        }
    }

    return {
        "id": target_id,
        "name": target_name,
        "job": job,
        "availability_pct": total_avail,
        "coverage_minutes": round(total_cov_sec / 60.0, 2),
        "denominator_minutes": round(total_cov_sec / 60.0, 2),
        "observed_minutes": round(total_cov_sec / 60.0, 2),
        "observed_seconds": round(total_cov_sec, 1),
        "uptime_minutes": round(total_up_sec / 60.0, 2),
        "uptime_seconds": round(total_up_sec, 1),
        "downtime_minutes": round(total_down_sec / 60.0, 2),
        "downtime_seconds": round(total_down_sec, 1),
        "unknown_minutes": round(total_unk_sec / 60.0, 2),
        "unknown_seconds": round(total_unk_sec, 1),
        "missing_minutes": round(total_unk_sec / 60.0, 2),
        "missing_seconds": round(total_unk_sec, 1),
        "sqlite_seconds": round(sqlite_cov_sec, 1),
        "prometheus_seconds": round(prom_cov_sec, 1),
        "overlap_removed_seconds": round(overlap_removed_sec, 1),
        "coverage_pct": cov_pct,
        "coverage_percent": cov_pct,
        "unknown_pct": unk_pct,
        "unknown_percent": unk_pct,
        "is_limited_data": (cov_pct < min_sla_coverage_pct and total_cov_sec > 0),
        "is_no_data": (total_cov_sec <= 0 or total_avail is None),
        "first_seen_ts": first_seen_ts,
        "last_seen_ts": last_seen_ts,
        "root_cause_code": root_cause_code,
        "root_cause_hint": root_cause_hint,
        "confidence_level": confidence_level,
        "confidence_score": confidence_score,
        "confidence_badge": conf_badge,
        "recommendation": recommendation,
        "telemetry_audit": telemetry_audit,
        "sla_eligible": is_eligible,
        "sla_compliant": (sla_status == "COMPLIANT"),
        "sla_status": sla_status,
        "sla_eligibility_reason": sla_reason,
        "sla_target_pct": round(float(sla_threshold), 3),
        "data_status": data_status,
        "source": target_src,
        "incidents": total_inc,
        "incident_count": total_inc,
        "sample_count": total_samples,
        "avg_latency_ms": round(avg_lat, 1),
        # SLA (maintenance-excluded) trio — summarize_entries prefers these
        # for the fleet aggregate / compliance ratio; == raw when no windows.
        "availability_pct_excl_maintenance": sla_avail,
        "maintenance_excluded_seconds": maint_excluded_sec,
        "maintenance_excluded_minutes": round(maint_excluded_sec / 60.0, 2),
        "sla_uptime_seconds": round(sla_up_sec, 1),
        "sla_downtime_seconds": round(sla_down_sec, 1),
        "sla_downtime_minutes": round(sla_down_sec / 60.0, 2),
        "sla_observed_seconds": round(sla_cov_sec, 1),
        "sla_observed_minutes": round(sla_cov_sec / 60.0, 2),
    }


def merge_hybrid_fleet_availability(
    req_start: float,
    req_end: float,
    monitored_instances: List[str],
    sqlite_buckets: List[Dict[str, Any]],
    prom_results_map: Dict[str, Any],
    expected_interval_sec: Union[float, Dict[str, float]] = DEFAULT_SCRAPE_INTERVAL_SEC,
    gap_tolerance: float = DEFAULT_GAP_TOLERANCE,
    min_sla_coverage_pct: Optional[float] = None,
    sla_threshold: float = SLA_COMPLIANCE_THRESHOLD,
    maintenance_by_instance: Optional[Dict[str, List[Tuple[float, float]]]] = None,
    sla_threshold_by_instance: Optional[Dict[str, float]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Merges per-target availability for all monitored instances and calculates fleet metrics.

    Every target is scored over the SAME requested window [req_start, req_end].
    A target first monitored partway through it honestly reads as low-coverage
    (and is flagged NEW_TARGET / limited-data by merge_hybrid_target_availability)
    rather than being silently rescoped to its own shorter life — the range the
    caller asked for is the range every number is measured against.

    sla_threshold_by_instance: {instance: target_pct} per-target SLA target
    overrides; a target missing from the map uses `sla_threshold`.

    expected_interval_sec may be a single fleet-wide seconds value, or a
    {instance: seconds} map (e.g. from get_instance_cadence_map) for a mixed
    fleet where jobs scrape at different cadences — a target missing from the
    map falls back to DEFAULT_SCRAPE_INTERVAL_SEC.

    maintenance_by_instance: {instance: [(start_ts, end_ts), ...]} of planned
    downtime windows scoped to each target; carved out of that target's SLA
    denominator (see merge_hybrid_target_availability). None = no exclusion.
    """
    period_minutes = max(0.0, (req_end - req_start) / 60.0)
    buckets_by_instance: Dict[str, List[Dict[str, Any]]] = {}
    for b in sqlite_buckets:
        inst = b.get("instance")
        if inst:
            buckets_by_instance.setdefault(inst, []).append(b)

    entries = []
    tot_sqlite_sec = 0.0
    tot_prom_sec = 0.0
    tot_overlap_removed = 0.0
    tot_cov_sec = 0.0
    tot_unk_sec = 0.0
    tot_maint_excluded_sec = 0.0
    # Wall-clock planned-maintenance time scheduled inside [req_start, req_end],
    # independent of coverage. tot_maint_excluded_sec (what actually left the
    # SLA denominator) can only ever be <= this, and lags it while the last
    # few minutes of telemetry are still being aggregated — so the two are
    # reported side by side ("Xm scheduled / Ys excluded so far").
    tot_maint_window_sec = 0.0

    for inst in monitored_instances:
        target_prom = {
            "first_ts": prom_results_map.get("first_ts", {}).get(inst),
            "last_ts": prom_results_map.get("last_ts", {}).get(inst),
            "count": prom_results_map.get("count", {}).get(inst),
            "avail": prom_results_map.get("avail", {}).get(inst),
            "incidents": prom_results_map.get("incidents", {}).get(inst),
            "duration": prom_results_map.get("duration", {}).get(inst),
        }
        inst_buckets = buckets_by_instance.get(inst, [])
        if isinstance(expected_interval_sec, dict):
            inst_interval_sec = expected_interval_sec.get(inst) or DEFAULT_SCRAPE_INTERVAL_SEC
        else:
            inst_interval_sec = expected_interval_sec
        inst_maint_wins = (maintenance_by_instance or {}).get(inst)
        entry = merge_hybrid_target_availability(
            req_start=req_start,
            req_end=req_end,
            target_id=inst,
            target_name=inst,
            job="blackbox",
            sqlite_buckets=inst_buckets,
            prom_metrics=target_prom,
            expected_interval_sec=inst_interval_sec,
            gap_tolerance=gap_tolerance,
            min_sla_coverage_pct=min_sla_coverage_pct,
            sla_threshold=float((sla_threshold_by_instance or {}).get(inst, sla_threshold)),
            maintenance_windows=inst_maint_wins,
        )
        entries.append(entry)
        tot_maint_excluded_sec += entry.get("maintenance_excluded_seconds", 0.0)
        if inst_maint_wins:
            tot_maint_window_sec += maintenance_overlap_seconds(inst_maint_wins, req_start, req_end)
        tot_sqlite_sec += entry.get("sqlite_seconds", 0.0)
        tot_prom_sec += entry.get("prometheus_seconds", 0.0)
        tot_overlap_removed += entry.get("overlap_removed_seconds", 0.0)
        tot_cov_sec += entry.get("observed_seconds", 0.0)
        tot_unk_sec += entry.get("unknown_seconds", 0.0)

    summary = summarize_entries(
        entries,
        period_minutes=period_minutes,
        min_sla_coverage_pct=min_sla_coverage_pct,
        sla_threshold=sla_threshold,
    )

    fleet_window_sec = float(len(monitored_instances) * period_minutes * 60.0) if monitored_instances else 0.0
    fleet_cov_pct = round(_clamp_pct((tot_cov_sec / fleet_window_sec) * 100.0), 2) if fleet_window_sec > 0 else 0.0

    if tot_cov_sec <= 0:
        fleet_data_status = "NO_DATA"
    elif fleet_cov_pct >= 95.0:
        fleet_data_status = "COMPLETE"
    elif fleet_cov_pct >= (min_sla_coverage_pct or DEFAULT_MIN_SLA_COVERAGE_PERCENT):
        fleet_data_status = "PARTIAL"
    else:
        fleet_data_status = "INSUFFICIENT_DATA"

    if tot_sqlite_sec > 0 and tot_prom_sec > 0:
        fleet_source = "hybrid"
    elif tot_sqlite_sec > 0:
        fleet_source = "materialized"
    elif tot_prom_sec > 0:
        fleet_source = "fallback"
    else:
        fleet_source = "nodata"

    limited_hosts = [e for e in entries if e.get("is_limited_data")]
    nodata_hosts = [e for e in entries if e.get("is_no_data")]
    optimal_hosts = [e for e in entries if e.get("coverage_pct", 0) >= 95.0]

    avg_cov_sec = round(tot_cov_sec / max(1, len(monitored_instances)), 1) if monitored_instances else 0.0
    avg_unk_sec = round(tot_unk_sec / max(1, len(monitored_instances)), 1) if monitored_instances else 0.0
    # Fleet TOTAL planned-maintenance time carved out of the SLA denominator.
    # NOT a per-host mean: maintenance windows are per-host events (most hosts
    # have none), so dividing the total by the whole fleet turned a real
    # 10-minute window into a meaningless "7s". The audit/SLA cards render
    # this as an absolute duration ("X excluded"), never as a % of window, so
    # the fleet total is the correct — and only non-misleading — quantity.
    # Matches summary.fleet_aggregate.maintenance_excluded_minutes exactly
    # (both are Σ per-host (coverage − sla_coverage)).
    maint_excluded_sec_total = round(tot_maint_excluded_sec, 1)
    maint_scheduled_sec_total = round(tot_maint_window_sec, 1)
    req_win_sec = round(period_minutes * 60.0, 1)

    if tot_cov_sec <= 0:
        fleet_root_code = "NO_DATA"
        fleet_root_hint = "No telemetry recorded for any host in the fleet during this window."
        fleet_recom = "Check Prometheus connectivity and confirm the exporters are actively collecting metrics."
    elif fleet_cov_pct < (min_sla_coverage_pct or DEFAULT_MIN_SLA_COVERAGE_PERCENT):
        if len(limited_hosts) == len(entries):
            fleet_root_code = "FLEET_RETENTION_WINDOW"
            fleet_root_hint = f"The entire fleet ({len(entries)} hosts) has only been observed for {fleet_cov_pct}% of the window."
            fleet_recom = "Use a shorter time range (e.g. Last 24 Hours) or wait for the SQLite database to backfill."
        else:
            fleet_root_code = "PARTIAL_FLEET_ONBOARDING"
            fleet_root_hint = f"{len(limited_hosts)} of {len(entries)} hosts have limited data (<50% coverage)."
            fleet_recom = "Review the newly onboarded hosts in the audit table below for a closer look."
    elif fleet_cov_pct < 95.0:
        fleet_root_code = "MODERATE_COVERAGE"
        fleet_root_hint = f"Fleet coverage spans {fleet_cov_pct}% of the window. Some hosts had observation gaps."
        fleet_recom = "Fleet data is representative for internal performance trends."
    else:
        fleet_root_code = "OPTIMAL_COVERAGE"
        fleet_root_hint = "Fleet telemetry coverage is optimal (>=95%) for operational reporting and SLA compliance."
        fleet_recom = "Fleet is in a healthy monitoring state."

    fleet_audit = {
        "requested_window_seconds": req_win_sec,
        "coverage_seconds": avg_cov_sec,
        "missing_seconds": avg_unk_sec,
        "maintenance_excluded_seconds": maint_excluded_sec_total,
        "maintenance_scheduled_seconds": maint_scheduled_sec_total,
        "coverage_percent": fleet_cov_pct,
        "missing_percent": round(max(0.0, 100.0 - fleet_cov_pct), 2),
        "data_status": fleet_data_status,
        "root_cause_code": fleet_root_code,
        "root_cause_hint": fleet_root_hint,
        "confidence_level": "HIGH" if fleet_cov_pct >= 95.0 else ("MODERATE" if fleet_cov_pct >= (min_sla_coverage_pct or DEFAULT_MIN_SLA_COVERAGE_PERCENT) else "LOW"),
        "confidence_score": round(fleet_cov_pct),
        "recommendation": fleet_recom,
        "counts": {
            "total_hosts": len(entries),
            "limited_hosts": len(limited_hosts),
            "nodata_hosts": len(nodata_hosts),
            "optimal_hosts": len(optimal_hosts),
        },
        "storage": {
            "source": fleet_source,
            "sqlite_seconds": round(tot_sqlite_sec / max(1, len(monitored_instances)), 1) if monitored_instances else 0.0,
            "prometheus_seconds": round(tot_prom_sec / max(1, len(monitored_instances)), 1) if monitored_instances else 0.0,
            "overlap_removed_seconds": round(tot_overlap_removed / max(1, len(monitored_instances)), 1) if monitored_instances else 0.0,
        }
    }

    hybrid_metadata = {
        "requested_window_seconds": req_win_sec,
        "coverage_seconds": avg_cov_sec,
        "unknown_seconds": avg_unk_sec,
        "missing_seconds": avg_unk_sec,
        "maintenance_excluded_seconds": maint_excluded_sec_total,
        "maintenance_scheduled_seconds": maint_scheduled_sec_total,
        "sqlite_seconds": round(tot_sqlite_sec / max(1, len(monitored_instances)), 1) if monitored_instances else 0.0,
        "prometheus_seconds": round(tot_prom_sec / max(1, len(monitored_instances)), 1) if monitored_instances else 0.0,
        "overlap_removed_seconds": round(tot_overlap_removed / max(1, len(monitored_instances)), 1) if monitored_instances else 0.0,
        "coverage_percent": fleet_cov_pct,
        "availability_percent": summary.get("fleet_aggregate", {}).get("value"),
        "data_status": fleet_data_status,
        "source": fleet_source,
        "telemetry_audit": fleet_audit,
    }

    return entries, {**summary, "hybrid": hybrid_metadata, "telemetry_audit": fleet_audit}


def reconstruct_time_series_intervals(
    samples: List[Tuple[float, Union[int, float, str]]],
    window_start_ts: float,
    window_end_ts: float,
    expected_interval_sec: float = DEFAULT_SCRAPE_INTERVAL_SEC,
    gap_tolerance: float = DEFAULT_GAP_TOLERANCE,
    min_sla_coverage_pct: Optional[float] = None,
    sla_threshold: float = SLA_COMPLIANCE_THRESHOLD,
) -> Dict[str, Any]:
    """
    Reconstruct exact observed intervals, coverage, uptime, downtime, UNKNOWN gaps,
    falling-edge incidents, and individual outage durations from raw ordered time-series samples.

    Mathematical Invariants Enforced:
      1. Coverage = Uptime + Downtime
      2. Requested Window = Coverage + Unknown
      3. 0 <= Availability <= 100 (or None if Coverage == 0)
      4. 0 <= Coverage Percent <= 100
      5. Coverage Percent + Unknown Percent == 100
    """
    if min_sla_coverage_pct is None:
        min_sla_coverage_pct = get_min_sla_coverage_percent()
    window_sec = max(0.0, float(window_end_ts - window_start_ts))
    # Determine effective expected interval from sample deltas if not explicitly overridden to a matching cadence
    cleaned: List[Tuple[float, int]] = []
    for ts_raw, val_raw in samples:
        try:
            ts = float(ts_raw)
            val = 1 if str(val_raw) in ('1', '1.0', 'up', 'true', 'True') else 0
            if window_start_ts <= ts <= window_end_ts:
                cleaned.append((ts, val))
        except (ValueError, TypeError):
            continue

    cleaned.sort(key=lambda x: x[0])

    if len(cleaned) >= 2:
        deltas = [cleaned[i+1][0] - cleaned[i][0] for i in range(len(cleaned)-1) if cleaned[i+1][0] > cleaned[i][0]]
        if deltas:
            median_delta = sorted(deltas)[len(deltas) // 2]
            if median_delta > expected_interval_sec:
                expected_interval_sec = median_delta

    max_gap_sec = expected_interval_sec * gap_tolerance

    if not cleaned or window_sec <= 0.0:
        return {
            "window_seconds": window_sec,
            "window_minutes": round(window_sec / 60.0, 2),
            "coverage_seconds": 0.0,
            "coverage_minutes": 0.0,
            "uptime_seconds": 0.0,
            "uptime_minutes": 0.0,
            "downtime_seconds": 0.0,
            "downtime_minutes": 0.0,
            "unknown_seconds": window_sec,
            "unknown_minutes": round(window_sec / 60.0, 2),
            "coverage_percent": 0.0,
            "unknown_percent": 100.0,
            "availability_pct": None,
            "incident_count": 0,
            "outage_durations_sec": [],
            "outage_intervals_sec": [],
            "outage_durations_min": [],
            "mean_outage_minutes": None,
            "median_outage_minutes": None,
            "p95_outage_minutes": None,
            "max_outage_minutes": None,
            "is_ongoing_outage": False,
            "sla_eligible": False,
            "sla_eligibility_reason": "Zero samples observed in requested window",
            "sla_status": "INSUFFICIENT_DATA",
        }

    uptime_sec = 0.0
    downtime_sec = 0.0
    unknown_sec = 0.0
    outage_durations_sec: List[float] = []
    # Absolute [start_ts, end_ts] of each closed outage in this window, clipped
    # to [window_start_ts, window_end_ts]. Lets a downstream consumer intersect
    # planned-maintenance windows against the actual outage, not the whole hour
    # (see merge_hybrid_target_availability's maintenance carve-out, audit M3).
    outage_intervals_sec: List[Tuple[float, float]] = []
    current_outage_start_ts: Optional[float] = None
    incident_count = 0

    first_ts, first_val = cleaned[0]
    last_ts, last_val = cleaned[-1]

    # 1. Boundary: Before first sample [window_start_ts, first_ts]
    pre_gap = first_ts - window_start_ts
    if pre_gap > max_gap_sec:
        # Excess is unknown; immediate pre-slice takes state of first sample
        obs_pre = min(pre_gap, expected_interval_sec)
        unknown_sec += (pre_gap - obs_pre)
        if first_val == 1:
            uptime_sec += obs_pre
        else:
            downtime_sec += obs_pre
    else:
        if first_val == 1:
            uptime_sec += pre_gap
        else:
            downtime_sec += pre_gap

    # Active outage tracker
    current_outage_duration = 0.0
    in_outage = (first_val == 0)
    if in_outage:
        incident_count += 1
        current_outage_duration += (first_ts - window_start_ts)
        current_outage_start_ts = window_start_ts

    # 2. Intermediate intervals between consecutive samples
    for i in range(len(cleaned) - 1):
        t_curr, v_curr = cleaned[i]
        t_next, v_next = cleaned[i + 1]
        delta_t = max(0.0, t_next - t_curr)

        if delta_t <= max_gap_sec:
            # Normal continuous observed interval
            if v_curr == 1:
                uptime_sec += delta_t
            else:
                downtime_sec += delta_t
                if in_outage:
                    current_outage_duration += delta_t
        else:
            # Excess gap classified as UNKNOWN
            slice_obs = min(delta_t, expected_interval_sec)
            unknown_sec += (delta_t - slice_obs)
            if v_curr == 1:
                uptime_sec += slice_obs
            else:
                downtime_sec += slice_obs
                if in_outage:
                    current_outage_duration += slice_obs

        # Transition detection
        if v_curr == 1 and v_next == 0:
            # Falling edge: UP -> DOWN
            incident_count += 1
            in_outage = True
            current_outage_duration = 0.0
            current_outage_start_ts = t_next
        elif v_curr == 0 and v_next == 1:
            # Rising edge: DOWN -> UP (Outage resolved)
            if in_outage and current_outage_duration > 0:
                outage_durations_sec.append(current_outage_duration)
                _os = current_outage_start_ts if current_outage_start_ts is not None else window_start_ts
                outage_intervals_sec.append((max(window_start_ts, _os), min(window_end_ts, t_next)))
            in_outage = False
            current_outage_duration = 0.0
            current_outage_start_ts = None

    # 3. Boundary: After last sample [last_ts, window_end_ts]
    post_gap = window_end_ts - last_ts
    if post_gap > max_gap_sec:
        obs_post = min(post_gap, expected_interval_sec)
        unknown_sec += (post_gap - obs_post)
        if last_val == 1:
            uptime_sec += obs_post
        else:
            downtime_sec += obs_post
            if in_outage:
                current_outage_duration += obs_post
    else:
        if last_val == 1:
            uptime_sec += post_gap
        else:
            downtime_sec += post_gap
            if in_outage:
                current_outage_duration += post_gap

    # Close ongoing outage at window boundary
    is_ongoing_outage = (last_val == 0)
    if in_outage and current_outage_duration > 0:
        outage_durations_sec.append(current_outage_duration)
        _os = current_outage_start_ts if current_outage_start_ts is not None else window_start_ts
        outage_intervals_sec.append((max(window_start_ts, _os), window_end_ts))

    # Invariant clamp
    coverage_sec = max(0.0, min(window_sec, uptime_sec + downtime_sec))
    unknown_sec = max(0.0, window_sec - coverage_sec)

    coverage_pct = round(_clamp_pct((coverage_sec / window_sec) * 100.0), 2) if window_sec > 0 else 0.0
    unknown_pct = round(_clamp_pct(100.0 - coverage_pct), 2)

    availability_pct = round(_clamp_pct((uptime_sec / coverage_sec) * 100.0), 2) if coverage_sec > 0 else None

    # Outage statistics
    outage_durations_min = [round(d / 60.0, 2) for d in outage_durations_sec if d > 0]
    total_downtime_min = round(downtime_sec / 60.0, 2)
    mean_outage_min = round(total_downtime_min / incident_count, 1) if incident_count > 0 else None
    median_outage_min = calculate_percentile(outage_durations_min, 50.0) if outage_durations_min else None
    p95_outage_min = calculate_percentile(outage_durations_min, 95.0) if outage_durations_min else None
    max_outage_min = round(max(outage_durations_min), 1) if outage_durations_min else None

    # SLA Eligibility & Status
    is_eligible = (availability_pct is not None) and (coverage_pct >= min_sla_coverage_pct)
    if not is_eligible:
        if availability_pct is None:
            reason = "No valid availability data"
        else:
            reason = f"Insufficient coverage ({coverage_pct}% < {min_sla_coverage_pct}%)"
        status = "INSUFFICIENT_DATA"
    else:
        if availability_pct >= sla_threshold:
            reason = f"Met SLA threshold ({availability_pct}% >= {sla_threshold}%)"
            status = "COMPLIANT"
        else:
            reason = f"Below SLA threshold ({availability_pct}% < {sla_threshold}%)"
            status = "NON_COMPLIANT"

    return {
        "window_seconds": round(window_sec, 2),
        "window_minutes": round(window_sec / 60.0, 2),
        "coverage_seconds": round(coverage_sec, 2),
        "coverage_minutes": round(coverage_sec / 60.0, 2),
        "uptime_seconds": round(uptime_sec, 2),
        "uptime_minutes": round(uptime_sec / 60.0, 2),
        "downtime_seconds": round(downtime_sec, 2),
        "downtime_minutes": total_downtime_min,
        "unknown_seconds": round(unknown_sec, 2),
        "unknown_minutes": round(unknown_sec / 60.0, 2),
        "coverage_percent": coverage_pct,
        "unknown_percent": unknown_pct,
        "availability_pct": availability_pct,
        "incident_count": incident_count,
        "outage_durations_sec": [round(d, 1) for d in outage_durations_sec],
        "outage_intervals_sec": [[round(s, 1), round(e, 1)] for s, e in outage_intervals_sec if e > s],
        "outage_durations_min": outage_durations_min,
        "mean_outage_minutes": mean_outage_min,
        "median_outage_minutes": median_outage_min,
        "p95_outage_minutes": p95_outage_min,
        "max_outage_minutes": max_outage_min,
        "is_ongoing_outage": is_ongoing_outage,
        "sla_eligible": is_eligible,
        "sla_eligibility_reason": reason,
        "sla_status": status,
    }


def summarize_entries(
    entries: List[Dict],
    period_minutes: float,
    min_sla_coverage_pct: Optional[float] = None,
    sla_threshold: float = SLA_COMPLIANCE_THRESHOLD,
) -> Dict:
    """
    Shared aggregation core for Prometheus query results & server record collections.
    Enforces all mathematical invariants across per-target and fleet levels.
    """
    if min_sla_coverage_pct is None:
        min_sla_coverage_pct = get_min_sla_coverage_percent()
    server_count = len(entries)
    period_minutes = max(0.0, float(period_minutes))

    if server_count == 0 or period_minutes <= 0.0:
        return {
            "period_minutes": round(period_minutes, 2),
            "server_count": 0,
            "scored_count": 0,
            "eligible_count": 0,
            "per_server": {"label": "Per-Server Availability", "unit": "%", "values": []},
            "fleet_average": {"label": "Fleet Average Availability (unweighted)", "value": None, "unit": "%"},
            "fleet_aggregate": {"label": "Fleet Aggregate Availability (weighted, SLA)", "value": None, "unit": "%"},
            "health_ratio": {"label": "Health Ratio (never-down servers)", "value": None, "unit": "%"},
            "zero_downtime_ratio": {"label": "Zero Downtime Ratio", "value": None, "unit": "%"},
            "sla_compliance": {"label": f"SLA Compliance (>={sla_threshold}%)", "value": None, "unit": "%"},
            "sla_compliance_ratio": {"label": f"SLA Compliance Ratio (>={sla_threshold}%)", "value": None, "unit": "%"},
            "coverage_ratio": {"label": "Fleet Coverage Ratio", "value": None, "unit": "%"},
            "analytics": {
                "total_incidents": 0,
                "total_downtime_minutes": 0.0,
                "mean_outage_minutes": None,
                "median_outage_minutes": None,
                "p95_outage_minutes": None,
                "max_outage_minutes": None,
                "most_unstable": [],
            },
        }

    per_server = []
    total_coverage = 0.0
    total_uptime = 0.0
    total_downtime = 0.0
    sla_total_coverage = 0.0   # maintenance-excluded — drives fleet_aggregate
    sla_total_uptime = 0.0
    total_incidents = 0
    never_down_count = 0
    scored_count = 0
    eligible_count = 0
    sla_compliant_count = 0
    availability_sum = 0.0
    outage_durations: List[float] = []

    for entry in entries:
        raw_avail = entry.get("availability_pct")
        denom_raw = float(entry.get("denominator_minutes") if entry.get("denominator_minutes") is not None else entry.get("coverage_minutes", period_minutes))
        coverage_minutes = max(0.0, min(period_minutes, denom_raw))

        downtime_raw = float(entry.get("downtime_minutes") or 0.0)
        downtime_minutes = max(0.0, min(coverage_minutes, downtime_raw))
        uptime_minutes = max(0.0, coverage_minutes - downtime_minutes)
        unknown_minutes = max(0.0, period_minutes - coverage_minutes)
        unknown_seconds = round(unknown_minutes * 60.0, 1)

        coverage_pct = round(_clamp_pct((coverage_minutes / period_minutes) * 100.0), 2) if period_minutes > 0 else 0.0
        unknown_pct = round(_clamp_pct(100.0 - coverage_pct), 2)

        if raw_avail is not None:
            availability_pct_raw = round(_clamp_pct(raw_avail), 2)
        elif coverage_minutes > 0:
            availability_pct_raw = round(_clamp_pct((uptime_minutes / coverage_minutes) * 100.0), 2)
        else:
            availability_pct_raw = None

        # Maintenance-excluded (SLA) view: prefer the trio computed by
        # merge_hybrid_target_availability; fall back to raw for legacy /
        # server-record entries that don't carry it. == raw when no windows.
        maint_excluded_minutes = float(entry.get("maintenance_excluded_minutes") or 0.0)
        _sla_dt = entry.get("sla_downtime_minutes")
        sla_downtime_minutes = downtime_minutes if _sla_dt is None else max(0.0, min(coverage_minutes, float(_sla_dt)))
        _sla_obs = entry.get("sla_observed_minutes")
        sla_observed_minutes = coverage_minutes if _sla_obs is None else max(0.0, min(coverage_minutes, float(_sla_obs)))
        sla_uptime_minutes = max(0.0, sla_observed_minutes - sla_downtime_minutes)
        _sla_av = entry.get("availability_pct_excl_maintenance")
        if _sla_av is not None:
            availability_pct = round(_clamp_pct(_sla_av), 2)
        elif sla_observed_minutes > 0:
            availability_pct = round(_clamp_pct((sla_uptime_minutes / sla_observed_minutes) * 100.0), 2)
        else:
            availability_pct = availability_pct_raw

        incidents = int(entry.get("incidents", entry.get("incident_count", 0)) or 0)
        if incidents == 0 and downtime_minutes > 0:
            incidents = 1
        total_incidents += incidents

        avg_latency = float(entry.get("avg_latency_ms") or 0.0)

        # SLA eligibility check — eligibility gate is raw coverage (did we
        # observe enough); compliance is the maintenance-excluded availability
        # against this target's own SLA target (falls back to the fleet one).
        # `or` would treat an explicit 0% override (a real, allowed value —
        # see PUT /api/sla-targets/<instance>, 0.0 <= pct <= 100.0) as falsy
        # and silently fall back to the fleet default instead of honoring it.
        _raw_sla_target = entry.get("sla_target_pct")
        entry_sla_target = float(_raw_sla_target) if _raw_sla_target is not None else sla_threshold
        is_eligible = (availability_pct is not None) and (coverage_pct >= min_sla_coverage_pct)
        if not is_eligible:
            sla_status = "INSUFFICIENT_DATA"
            sla_reason = "No data" if availability_pct is None else f"Coverage ({coverage_pct}%) < {min_sla_coverage_pct}%"
        elif availability_pct >= entry_sla_target:
            sla_status = "COMPLIANT"
            sla_reason = f"Availability ({availability_pct}%) >= {entry_sla_target}%"
        else:
            sla_status = "NON_COMPLIANT"
            sla_reason = f"Availability ({availability_pct}%) < {entry_sla_target}%"

        # Collect outage durations for fleet percentiles
        entry_outages = entry.get("outage_durations_min")
        if isinstance(entry_outages, list) and entry_outages:
            outage_durations.extend([float(d) for d in entry_outages if float(d) > 0])
        elif downtime_minutes > 0:
            outage_durations.append(downtime_minutes)

        # Data-quality status is re-derived from THIS window's coverage so it
        # can't disagree with coverage_pct above; the human explanation of *why*
        # coverage is short (retention window / new target / scrape gaps) is
        # carried straight through from merge_hybrid_target_availability, which
        # already did the first-seen reasoning. Legacy/server-record entries
        # simply carry None for these.
        if coverage_minutes <= 0 or availability_pct is None:
            entry_data_status = "NO_DATA"
        elif not is_eligible:
            entry_data_status = "INSUFFICIENT_DATA"
        elif coverage_pct >= 95.0:
            entry_data_status = "COMPLETE"
        else:
            entry_data_status = "PARTIAL"

        per_server.append({
            "id": entry.get("id"),
            "name": entry.get("name") or entry.get("id"),
            "job": entry.get("job", ""),
            "availability_pct": availability_pct,
            "availability_pct_raw": availability_pct_raw,
            "availability_pct_excl_maintenance": availability_pct,
            "maintenance_excluded_minutes": round(maint_excluded_minutes, 2),
            "maintenance_excluded_seconds": round(maint_excluded_minutes * 60.0, 1),
            "sla_downtime_minutes": round(sla_downtime_minutes, 2),
            "sla_downtime_seconds": round(sla_downtime_minutes * 60.0, 1),
            "sla_observed_minutes": round(sla_observed_minutes, 2),
            "sla_observed_seconds": round(sla_observed_minutes * 60.0, 1),
            "sla_target_pct": round(entry_sla_target, 3),
            "coverage_minutes": round(coverage_minutes, 2),
            "denominator_minutes": round(coverage_minutes, 2),
            "observed_minutes": round(coverage_minutes, 2),
            "observed_seconds": round(coverage_minutes * 60.0, 1),
            "uptime_minutes": round(uptime_minutes, 2),
            "uptime_seconds": round(uptime_minutes * 60.0, 1),
            "downtime_minutes": round(downtime_minutes, 2),
            "downtime_seconds": round(downtime_minutes * 60.0, 1),
            "unknown_minutes": round(unknown_minutes, 2),
            "unknown_seconds": unknown_seconds,
            "missing_minutes": round(unknown_minutes, 2),
            "missing_seconds": unknown_seconds,
            "coverage_pct": coverage_pct,
            "coverage_percent": coverage_pct,
            "unknown_pct": unknown_pct,
            "unknown_percent": unknown_pct,
            "is_limited_data": (coverage_pct < min_sla_coverage_pct and coverage_minutes > 0),
            "is_no_data": (coverage_minutes <= 0 or availability_pct is None),
            "sla_eligible": is_eligible,
            "sla_compliant": (sla_status == "COMPLIANT"),
            "sla_status": sla_status,
            "sla_eligibility_reason": sla_reason,
            "incidents": incidents,
            "incident_count": incidents,
            "avg_latency_ms": round(avg_latency, 1),
            # Per-host data-quality diagnostics — carried through so the audit
            # table can explain a low-coverage row instead of only flagging it.
            "data_status": entry_data_status,
            "root_cause_code": entry.get("root_cause_code"),
            "root_cause_hint": entry.get("root_cause_hint"),
            "recommendation": entry.get("recommendation"),
            "confidence_level": entry.get("confidence_level"),
            "confidence_score": entry.get("confidence_score"),
            "confidence_badge": entry.get("confidence_badge"),
            "first_seen_ts": entry.get("first_seen_ts"),
            "last_seen_ts": entry.get("last_seen_ts"),
        })

        if availability_pct is not None and coverage_minutes > 0:
            availability_sum += availability_pct
            scored_count += 1
            total_coverage += coverage_minutes          # raw — coverage ratio
            total_uptime += uptime_minutes
            total_downtime += downtime_minutes          # raw — outage analytics
            sla_total_coverage += sla_observed_minutes  # maintenance-excluded
            sla_total_uptime += sla_uptime_minutes
            if sla_downtime_minutes <= 0.0:
                never_down_count += 1

        if is_eligible:
            eligible_count += 1
            if sla_status == "COMPLIANT":
                sla_compliant_count += 1

    fleet_average = round(availability_sum / scored_count, 2) if scored_count > 0 else None

    fleet_aggregate = (
        round(_clamp_pct((sla_total_uptime / sla_total_coverage) * 100.0), 2)
        if sla_total_coverage > 0 else None
    )

    health_ratio = round((never_down_count / scored_count) * 100.0, 2) if scored_count > 0 else None
    sla_compliance_ratio = (
        round((sla_compliant_count / eligible_count) * 100.0, 2)
        if eligible_count > 0 else None
    )

    total_possible_window = server_count * period_minutes
    fleet_coverage_ratio = (
        round(_clamp_pct((total_coverage / total_possible_window) * 100.0), 2)
        if total_possible_window > 0 else None
    )

    # Statistical analytics for outages
    mean_outage = round(total_downtime / total_incidents, 1) if total_incidents > 0 else None
    median_outage = calculate_percentile(outage_durations, 50.0) if outage_durations else None
    p95_outage = calculate_percentile(outage_durations, 95.0) if outage_durations else None
    max_outage = round(max(outage_durations), 1) if outage_durations else None

    most_unstable = sorted(
        [e for e in per_server if e["incidents"] > 0],
        key=lambda e: (-e["incidents"], -e["downtime_minutes"])
    )

    downtime_split_pct = round(_clamp_pct(100.0 - fleet_aggregate), 2) if fleet_aggregate is not None else None

    return {
        "period_minutes": round(period_minutes, 2),
        "server_count": server_count,
        "scored_count": scored_count,
        "eligible_count": eligible_count,
        "healthy_hosts_count": never_down_count,
        "per_server": {
            "label": "Per-Server Availability",
            "unit": "%",
            "values": per_server,
        },
        "fleet_average": {
            "label": "Fleet Average Availability (unweighted)",
            "value": fleet_average,
            "unit": "%",
        },
        "fleet_aggregate": {
            "label": "Fleet Aggregate Availability (weighted)",
            "value": fleet_aggregate,
            "uptime_percent": fleet_aggregate,
            "downtime_percent": downtime_split_pct,
            "total_uptime_minutes": round(sla_total_uptime, 2),
            "total_downtime_minutes": round(max(0.0, sla_total_coverage - sla_total_uptime), 2),
            "total_observed_minutes": round(sla_total_coverage, 2),
            "maintenance_excluded_minutes": round(max(0.0, total_coverage - sla_total_coverage), 2),
            "unit": "%",
        },
        "health_ratio": {
            "label": "Healthy Hosts (no recorded downtime)",
            "value": health_ratio,
            "healthy_count": never_down_count,
            "total_count": scored_count,
            "server_count": server_count,
            "unit": "%",
        },
        "zero_downtime_ratio": {
            "label": "Zero Downtime Ratio",
            "value": health_ratio,
            "unit": "%",
        },
        "sla_compliance": {
            "label": f"SLA Compliance (>={sla_threshold}%)",
            "value": sla_compliance_ratio,
            "unit": "%",
        },
        "sla_compliance_ratio": {
            "label": f"SLA Compliance Ratio (>={sla_threshold}%)",
            "value": sla_compliance_ratio,
            "unit": "%",
        },
        "coverage_ratio": {
            "label": "Fleet Coverage Ratio",
            "value": fleet_coverage_ratio,
            "unit": "%",
        },
        "analytics": {
            "total_incidents": total_incidents,
            "total_downtime_minutes": round(total_downtime, 2),
            "mean_outage_minutes": mean_outage,
            "median_outage_minutes": median_outage,
            "p95_outage_minutes": p95_outage,
            "max_outage_minutes": max_outage,
            "most_unstable": most_unstable,
        },
    }


def calculate_fleet_availability(
    servers: List[ServerRecord],
    period_minutes: float,
    now: Optional[datetime] = None,
    min_sla_coverage_pct: Optional[float] = None,
) -> Dict:
    """
    Calculate availability metrics for a list of server records.
    servers: [{ id, name, downtime_minutes, created_at, incidents, ... }]
    """
    if period_minutes <= 0:
        raise ValueError("period_minutes must be > 0")

    now = now or datetime.now(timezone.utc)

    entries = []
    for server in servers:
        downtime_minutes = float(server.get("downtime_minutes", 0) or 0)
        created_at = _parse_created_at(server["created_at"])

        age_minutes = max((now - created_at).total_seconds() / 60.0, 0.0)
        coverage_minutes = min(age_minutes, period_minutes)

        if coverage_minutes <= 0:
            availability = None
        else:
            availability = round(_clamp_pct(((coverage_minutes - downtime_minutes) / coverage_minutes) * 100.0), 2)

        entries.append({
            "id": server.get("id"),
            "name": server.get("name") or server.get("id"),
            "availability_pct": availability,
            "denominator_minutes": coverage_minutes,
            "coverage_minutes": coverage_minutes,
            "downtime_minutes": downtime_minutes,
            "incidents": server.get("incidents", 0),
            "avg_latency_ms": server.get("avg_latency_ms", 0.0),
        })

    return summarize_entries(entries, period_minutes, min_sla_coverage_pct=min_sla_coverage_pct)


