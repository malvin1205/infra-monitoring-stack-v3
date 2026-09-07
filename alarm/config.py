"""Shared constants for InfraWatch.

Deployment-tunable knobs and fixed identifiers used across the request
handlers, the monitoring-state engine, the availability pipeline, and the
background workers. Kept in one leaf module (no InfraWatch imports) so the
domain modules extracted from app.py can share them without importing app.

app.py re-imports every name, so `import app as alarm_app; alarm_app.DEFAULT_JOB_FILTER`
is unchanged.
"""
import os

# Default Prometheus job filter for /instances, /api/availability, the poller
# and the aggregator. "all" (or "*") means "every job except prometheus".
DEFAULT_JOB_FILTER = os.environ.get("JOB_FILTER", "all")

# Synthetic alert names the built-in poller raises (mirrors what Alertmanager
# would send). TargetDown = critical/down; SlowResponse = warning/degraded.
ALERTNAME_TARGET_DOWN = "TargetDown"
ALERTNAME_SLOW_RESPONSE = "SlowResponse"

# Global default latency (ms) above which an up target is "slow"/degraded.
# Per-instance overrides live in SlowThresholdRepository. Used by BOTH the
# SlowResponse alert (poller) and the live grid's severity/summary (audit F4).
DEFAULT_SLOW_RESPONSE_THRESHOLD_MS = float(os.environ.get("SLOW_RESPONSE_THRESHOLD_MS", "500"))

# Matches prometheus/prometheus.yml `global.scrape_interval`. Used to turn a
# Prometheus `count_over_time(...)` sample count back into a monitored-duration
# estimate for weighted (fleet_aggregate) availability math. Override via
# SCRAPE_INTERVAL_SECONDS if prometheus.yml's scrape_interval is ever changed.
try:
    SCRAPE_INTERVAL_SECONDS = float(os.environ.get("SCRAPE_INTERVAL_SECONDS", "2.0"))
except (TypeError, ValueError):
    SCRAPE_INTERVAL_SECONDS = 2.0

# How stale the latest SQLite availability bucket may be, for a *live* window
# (no ?end=), before /api/availability's "is this range fully materialized?"
# check gives up on the fast path and falls back to a live Prometheus query.
# The background aggregator (AVAIL_AGGREGATE_INTERVAL_SECONDS) only refreshes
# the in-progress hour's bucket once every 60s, and that cycle itself takes
# real time (Prometheus queries, up to 15s timeout). The frontend polls this
# endpoint every 15s regardless of user action — with zero slack between the
# two 60s numbers, a live range would drift past a bare 60s tolerance partway
# through most aggregation cycles, flip-flopping between the SQLite-materialized
# path and the live-hybrid path (two different estimation methods) on its own,
# with the UI number visibly changing every ~15s with no user action. 2.5x
# mirrors the same slack multiplier already used for the aggregator's
# leader-election lease.
_AVAIL_FRESHNESS_TOLERANCE_SEC = 150.0  # 60.0 * 2.5

# Separately: how old the NEWEST matched bucket's `updated_at` may be before the
# SQLite fast path is abandoned for a live window. The freshness tolerance above
# is about the bucket's nominal time span (which always reads as current, since
# an in-progress hour is stored with a future hour-end); this one catches a
# silently dead aggregator — buckets whose span still looks current but that
# stopped being rewritten. ~5 aggregator cycles (AVAIL_AGGREGATE_INTERVAL_SECONDS
# = 60s); older => fall through to the live Prometheus hybrid path instead of
# serving an ever-staler archive as "COMPLETE".
_AVAIL_STALE_BUCKET_TOLERANCE_SEC = 300.0
