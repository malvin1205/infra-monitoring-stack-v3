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
"""

import math
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

ServerRecord = Dict[str, Union[str, int, float, datetime]]

DEFAULT_MIN_SLA_COVERAGE_PERCENT = 50.0
SLA_COMPLIANCE_THRESHOLD = 99.9
DEFAULT_SCRAPE_INTERVAL_SEC = 2.0
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
    max_gap_sec = expected_interval_sec * gap_tolerance

    # Clean and filter samples within window
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
        elif v_curr == 0 and v_next == 1:
            # Rising edge: DOWN -> UP (Outage resolved)
            if in_outage and current_outage_duration > 0:
                outage_durations_sec.append(current_outage_duration)
            in_outage = False
            current_outage_duration = 0.0

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

        coverage_pct = round(_clamp_pct((coverage_minutes / period_minutes) * 100.0), 2) if period_minutes > 0 else 0.0
        unknown_pct = round(_clamp_pct(100.0 - coverage_pct), 2)

        if raw_avail is not None:
            availability_pct = round(_clamp_pct(raw_avail), 2)
        elif coverage_minutes > 0:
            availability_pct = round(_clamp_pct((uptime_minutes / coverage_minutes) * 100.0), 2)
        else:
            availability_pct = None

        incidents = int(entry.get("incidents", entry.get("incident_count", 0)) or 0)
        if incidents == 0 and downtime_minutes > 0:
            incidents = 1
        total_incidents += incidents

        avg_latency = float(entry.get("avg_latency_ms") or 0.0)

        # SLA eligibility check
        is_eligible = (availability_pct is not None) and (coverage_pct >= min_sla_coverage_pct)
        if not is_eligible:
            sla_status = "INSUFFICIENT_DATA"
            sla_reason = "No data" if availability_pct is None else f"Coverage ({coverage_pct}%) < {min_sla_coverage_pct}%"
        elif availability_pct >= sla_threshold:
            sla_status = "COMPLIANT"
            sla_reason = f"Availability ({availability_pct}%) >= {sla_threshold}%"
        else:
            sla_status = "NON_COMPLIANT"
            sla_reason = f"Availability ({availability_pct}%) < {sla_threshold}%"

        # Collect outage durations for fleet percentiles
        entry_outages = entry.get("outage_durations_min")
        if isinstance(entry_outages, list) and entry_outages:
            outage_durations.extend([float(d) for d in entry_outages if float(d) > 0])
        elif downtime_minutes > 0:
            outage_durations.append(downtime_minutes)

        per_server.append({
            "id": entry.get("id"),
            "name": entry.get("name") or entry.get("id"),
            "availability_pct": availability_pct,
            "coverage_minutes": round(coverage_minutes, 2),
            "denominator_minutes": round(coverage_minutes, 2),
            "uptime_minutes": round(uptime_minutes, 2),
            "downtime_minutes": round(downtime_minutes, 2),
            "unknown_minutes": round(unknown_minutes, 2),
            "coverage_pct": coverage_pct,
            "coverage_percent": coverage_pct,
            "unknown_pct": unknown_pct,
            "unknown_percent": unknown_pct,
            "sla_eligible": is_eligible,
            "sla_compliant": (sla_status == "COMPLIANT"),
            "sla_status": sla_status,
            "sla_eligibility_reason": sla_reason,
            "incidents": incidents,
            "incident_count": incidents,
            "avg_latency_ms": round(avg_latency, 1),
        })

        if availability_pct is not None and coverage_minutes > 0:
            availability_sum += availability_pct
            scored_count += 1
            total_coverage += coverage_minutes
            total_uptime += uptime_minutes
            total_downtime += downtime_minutes
            if downtime_minutes <= 0.0:
                never_down_count += 1

        if is_eligible:
            eligible_count += 1
            if sla_status == "COMPLIANT":
                sla_compliant_count += 1

    fleet_average = round(availability_sum / scored_count, 2) if scored_count > 0 else None

    fleet_aggregate = (
        round(_clamp_pct((total_uptime / total_coverage) * 100.0), 2)
        if total_coverage > 0 else None
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

    return {
        "period_minutes": round(period_minutes, 2),
        "server_count": server_count,
        "scored_count": scored_count,
        "eligible_count": eligible_count,
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
            "label": "Fleet Aggregate Availability (weighted, SLA)",
            "value": fleet_aggregate,
            "unit": "%",
        },
        "health_ratio": {
            "label": "Health Ratio (never-down servers)",
            "value": health_ratio,
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
            availability = 100.0 if downtime_minutes <= 0 else 0.0
        else:
            availability = _clamp_pct(((coverage_minutes - downtime_minutes) / coverage_minutes) * 100.0)

        entries.append({
            "id": server.get("id"),
            "name": server.get("name") or server.get("id"),
            "availability_pct": round(availability, 2),
            "denominator_minutes": coverage_minutes,
            "coverage_minutes": coverage_minutes,
            "downtime_minutes": downtime_minutes,
            "incidents": server.get("incidents", 0),
            "avg_latency_ms": server.get("avg_latency_ms", 0.0),
        })

    return summarize_entries(entries, period_minutes, min_sla_coverage_pct=min_sla_coverage_pct)


