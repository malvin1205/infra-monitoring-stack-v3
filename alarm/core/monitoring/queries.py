"""Higher-level Prometheus query adapters.

Thin functions that run one or a few PromQL queries via prometheus_client and
parse the response into `{instance: value}` / `{instance: [(ts, 0|1)]}` maps.
No caching or failover logic of their own — that all lives in
prometheus_client. Nothing imports app.

The single mock seam for these and for prometheus_client is
`prometheus_client.fetch_prometheus_json` (call it as a module attribute).
"""
from urllib.parse import quote

try:
    from . import client as _pc
except (ImportError, ValueError):
    try:
        from core.monitoring import client as _pc
    except ImportError:
        from alarm.core.monitoring import client as _pc

_SHARED_EXECUTOR = _pc._SHARED_EXECUTOR


def fetch_prom_query_map(query_expr, cache_ttl=5.0, timeout=None):
    raw, base = _pc.fetch_prometheus_json(f"/api/v1/query?query={quote(query_expr)}", use_cache=True, cache_ttl=cache_ttl, timeout=timeout)
    val_map = {}
    if raw and raw.get('status') == 'success':
        results = raw.get('data', {}).get('result', [])
        for r in results:
            labels = r.get('metric', {})
            inst = labels.get('instance') or labels.get('target') or labels.get('url')
            val = r.get('value', [None, None])[1]
            if inst and val is not None:
                val_map[inst] = val
    return val_map


def fetch_prom_range_map(query_expr, start_ts, end_ts, step_sec, cache_ttl=5.0, timeout=None):
    """Range query -> {instance: [(ts_float, 0|1), ...]} sorted by ts.

    Used by the availability aggregator to feed raw probe samples straight
    into reconstruct_time_series_intervals() (the exact engine) instead of
    approximating from avg_over_time(). Values are coerced to 0/1 the same
    way reconstruct_time_series_intervals does. Returns {} on any failure so
    callers can fall back to the scalar path per-instance.
    """
    step = max(1, int(round(step_sec)))
    path = (
        f"/api/v1/query_range?query={quote(query_expr)}"
        f"&start={int(start_ts)}&end={int(end_ts)}&step={step}"
    )
    raw, _ = _pc.fetch_prometheus_json(path, use_cache=True, cache_ttl=cache_ttl, timeout=timeout)
    series = {}
    if not raw or raw.get('status') != 'success':
        return series
    for r in raw.get('data', {}).get('result', []):
        labels = r.get('metric', {})
        inst = labels.get('instance') or labels.get('target') or labels.get('url')
        if not inst:
            continue
        pts = []
        for pair in r.get('values', []):
            try:
                ts = float(pair[0])
                v = 1 if str(pair[1]) in ('1', '1.0', 'up', 'true', 'True') else 0
                pts.append((ts, v))
            except (ValueError, TypeError, IndexError):
                continue
        if pts:
            pts.sort(key=lambda p: p[0])
            series[inst] = pts
    return series


def fetch_down_since_prom_map(cache_ttl=10.0):
    last_up_map = {}

    # 1. PromQL query for exact last UP timestamp for targets that were previously UP
    query_up = 'max_over_time(timestamp(probe_success == 1)[1d:15s])'
    raw_up, _ = _pc.fetch_prometheus_json(f"/api/v1/query?query={quote(query_up)}", use_cache=True, cache_ttl=cache_ttl)
    if not raw_up or not raw_up.get('data', {}).get('result'):
        query_up = 'max_over_time(timestamp(up == 1)[1d:15s])'
        raw_up, _ = _pc.fetch_prometheus_json(f"/api/v1/query?query={quote(query_up)}", use_cache=True, cache_ttl=cache_ttl)

    if raw_up and raw_up.get('status') == 'success':
        for r in raw_up.get('data', {}).get('result', []):
            metric = r.get('metric', {})
            inst = metric.get('instance') or metric.get('target') or metric.get('url')
            val = r.get('value', [None, None])[1]
            if inst and val is not None:
                try:
                    last_up_map[inst] = float(val)
                except ValueError:
                    pass

    # 2. Initial DOWN timestamp for targets continuously DOWN (no `== 1` sample
    #    in the query-1 window). Range widened to 32d so an outage older than a
    #    day is no longer clamped to "~24h ago" — it now reads its real age up
    #    to a month back. 15m step keeps the point count bounded (~3k); the
    #    outage-start estimate is then accurate to ±15m, which is immaterial for
    #    a multi-day outage. Falls back to `up == 0` the same way query 1 does.
    query_down = 'min_over_time(timestamp(probe_success == 0)[32d:15m])'
    raw_down, _ = _pc.fetch_prometheus_json(f"/api/v1/query?query={quote(query_down)}", use_cache=True, cache_ttl=cache_ttl)
    if not raw_down or not raw_down.get('data', {}).get('result'):
        query_down = 'min_over_time(timestamp(up == 0)[32d:15m])'
        raw_down, _ = _pc.fetch_prometheus_json(f"/api/v1/query?query={quote(query_down)}", use_cache=True, cache_ttl=cache_ttl)
    if raw_down and raw_down.get('status') == 'success':
        for r in raw_down.get('data', {}).get('result', []):
            metric = r.get('metric', {})
            inst = metric.get('instance') or metric.get('target') or metric.get('url')
            val = r.get('value', [None, None])[1]
            if inst and val is not None and inst not in last_up_map:
                try:
                    last_up_map[inst] = float(val)
                except ValueError:
                    pass

    return last_up_map


def fetch_all_probe_metrics(cache_ttl=3.0, timeout=None):
    f_succ = _SHARED_EXECUTOR.submit(fetch_prom_query_map, "probe_success", cache_ttl, timeout)
    f_dur = _SHARED_EXECUTOR.submit(fetch_prom_query_map, "probe_duration_seconds", cache_ttl, timeout)
    f_code = _SHARED_EXECUTOR.submit(fetch_prom_query_map, "probe_http_status_code", cache_ttl, timeout)

    success_map = f_succ.result()
    duration_map = f_dur.result()
    status_code_map = f_code.result()

    if not success_map:
        success_map = fetch_prom_query_map("up", cache_ttl=cache_ttl, timeout=timeout)

    return success_map, duration_map, status_code_map
