"""Availability response helpers — SLA error-budget attachment, the
online/warning/offline status split, and the Prometheus-TSDB fleet trend
series with its 60s cache.

Extracted verbatim from app.py (Phase 2 step 8) — behaviour is unchanged.
app.py re-imports every name, so `import app as alarm_app;
alarm_app._build_fleet_trend` and `alarm_app._FLEET_TREND_CACHE` keep working
and stay monkeypatchable from the tests. The /api/availability route itself
(cache/single-flight/hybrid orchestration, welded to Flask `request` and the
rate-limit decorator) stays in app.py and calls these as module globals.
"""
import math
import re
import time
import logging
from urllib.parse import quote

try:
    from .fleet import sla_budget
    from core.monitoring import client as promclient
except (ImportError, ValueError):
    from alarm.core.availability.fleet import sla_budget
    from alarm.core.monitoring import client as promclient

logger = logging.getLogger("infrawatch")


def _attach_sla_budgets(summary_dict, window_sec, default_target_pct, project_days, target_map=None):
    """Compute the SLA error budget per target (mutating the per_server
    values) and for the fleet, from the maintenance-excluded downtime/observed
    figures. `target_map` gives per-instance SLA target overrides; the fleet
    budget uses the deployment default. Returns the fleet-level budget dict."""
    target_map = target_map or {}
    for e in summary_dict.get("per_server", {}).get("values", []):
        dt = e.get("sla_downtime_seconds", e.get("downtime_seconds", 0.0)) or 0.0
        obs = e.get("sla_observed_seconds", e.get("observed_seconds", 0.0)) or 0.0
        tp = target_map.get(e.get("id"), e.get("sla_target_pct") or default_target_pct)
        e["sla_budget"] = sla_budget(dt, obs, tp, window_sec, project_days)
    fa = summary_dict.get("fleet_aggregate", {}) or {}
    f_dt = float(fa.get("total_downtime_minutes") or 0.0) * 60.0
    f_obs = float(fa.get("total_observed_minutes") or 0.0) * 60.0
    # f_dt/f_obs are fleet TOTALS (summed over every scored host), so the
    # window they're measured against must be the fleet total too — one
    # host's window * scored host count. Passing the single-host window here
    # made the fleet error budget report ~Nx the real usage (N hosts ->
    # "BREACHED" on a healthy fleet).
    n_scored = int(summary_dict.get("scored_count") or 0) or 1
    return sla_budget(f_dt, f_obs, default_target_pct, window_sec * n_scored, project_days)


def _availability_status_counts(entries, live_map=None):
    """online / warning / offline split by historical availability_pct, with the
    live probe_success snapshot breaking the tie for entries that have no
    historical coverage yet (availability_pct is None). Shared verbatim by
    api_availability's SQLite fast path and its Prometheus hybrid path so the
    same target is never bucketed differently depending on which path served the
    request (audit m4)."""
    live_map = live_map or {}
    counts = {"online": 0, "warning": 0, "offline": 0}
    for e in entries:
        avail_pct = e.get("availability_pct")
        if avail_pct is not None:
            if avail_pct >= 99.9:
                st_val = 'online'
            elif avail_pct >= 95.0:
                st_val = 'warning'
            else:
                st_val = 'offline'
        else:
            live_val = live_map.get(e.get("id"))
            if live_val is not None:
                st_val = 'online' if str(live_val) in ('1', '1.0') else 'offline'
            else:
                st_val = 'warning'
        counts[st_val] += 1
    return counts


_FLEET_TREND_CACHE = {}          # key -> (wall_ts, (series, slot))
_FLEET_TREND_CACHE_TTL = 60.0    # hourly slots — a minute stale is nothing


def _build_fleet_trend(trend_end_ts, window_seconds, instances, max_points=180):
    """Fleet availability time series straight from the Prometheus TSDB, over the
    SAME window the rest of the response is scored against
    ([trend_end_ts - window_seconds, trend_end_ts]), for the modal's
    Availability Trend chart.

    One `query_range`, coverage-weighted fleet availability per slot:

        sum(sum_over_time(<m>{instance=~"..."}[slot]))
        / sum(count_over_time(<m>{instance=~"..."}[slot]))

    evaluated at each slot boundary — total UP samples over total samples, the
    TSDB analogue of the old sum(uptime)/sum(coverage) over hourly buckets, but
    no longer bounded by how far the SQLite archive happens to reach back.

    `instances` MUST be the exact set the rest of /api/availability scored
    (`monitored_instances`). The selector is pinned to it so the line matches
    the Fleet Availability headline: an unscoped `probe_success` also sums the
    other blackbox jobs Prometheus scrapes (icmp pings, external probes) — a
    healthier population that dragged the line ~6pts above the real fleet.

    `<m>` is probe_success, falling back to `up` for deployments with no
    blackbox probes. A slot is one hour for short windows, or a whole number of
    hours chosen to keep the series under `max_points` for longer ones (≈3h at
    7d, ≈6h at 30d). Slots the TSDB has no samples for (NaN) are omitted so the
    chart shows a gap.

    Returns (series, slot_seconds). series is [] on any query failure / empty
    TSDB / empty instance set — the frontend keeps its placeholder.
    """
    end = int(trend_end_ts)
    win = max(3600.0, float(window_seconds or 0.0))
    slot = 3600
    if win / slot > max_points:
        slot = int(math.ceil(win / max_points / 3600.0) * 3600.0)
    # First eval point is one slot in, so every point summarizes a slot that
    # sits fully inside [end - win, end] rather than reaching back before it.
    start = end - int(win) + slot

    if not instances:
        return [], slot

    # 60s cache: the whole /api/availability payload is already cached, but on a
    # miss (and under the single-flight fan-out) this keeps the extra query_range
    # to at most once a minute regardless of request volume.
    ck = (tuple(sorted(instances)), int(win), slot, max_points)
    hit = _FLEET_TREND_CACHE.get(ck)
    if hit and (time.time() - hit[0]) < _FLEET_TREND_CACHE_TTL:
        return hit[1]

    # Backtick raw string: re.escape() emits `\.` for the dots in an IP, which a
    # double-quoted PromQL string rejects as an unknown escape. Backtick strings
    # take backslashes literally. Instance labels never contain a backtick.
    inst_re = "|".join(re.escape(i) for i in instances)
    sel = f'{{instance=~`{inst_re}`}}'

    for metric in ("probe_success", "up"):
        expr = (
            f"sum(sum_over_time({metric}{sel}[{slot}s])) "
            f"/ sum(count_over_time({metric}{sel}[{slot}s]))"
        )
        path = (
            f"/api/v1/query_range?query={quote(expr)}"
            f"&start={start}&end={end}&step={slot}"
        )
        try:
            raw, _ = promclient.fetch_prometheus_json(path, use_cache=True, cache_ttl=45.0, timeout=6.0)
        except Exception:
            logger.warning("fleet trend: %s query_range failed", metric, exc_info=True)
            raw = None
        if not raw or raw.get("status") != "success":
            continue
        result = raw.get("data", {}).get("result", [])
        if not result:
            continue
        series = []
        for pair in result[0].get("values", []):
            try:
                ts = int(float(pair[0]))
                frac = float(pair[1])
            except (ValueError, TypeError, IndexError):
                continue
            if frac != frac:  # NaN -> no samples in this slot, leave a gap
                continue
            series.append({
                "ts": ts,
                "availability_pct": round(max(0.0, min(100.0, frac * 100.0)), 2),
            })
        if series:
            _FLEET_TREND_CACHE[ck] = (time.time(), (series, slot))
            return series, slot
    _FLEET_TREND_CACHE[ck] = (time.time(), ([], slot))
    return [], slot
