"""Availability & SLA domain: fleet availability, hourly buckets, and TSDB trends.
Deep module presenting AvailabilityEngine at its primary seam.
"""
# 1. Base math and helpers (no monitoring imports)
from .fleet import (
    summarize_entries,
    reconstruct_time_series_intervals,
    calculate_percentile,
    clip_hourly_bucket,
    merge_hybrid_target_availability,
    merge_hybrid_fleet_availability,
    derive_bucket_inputs,
    estimate_instance_cadence,
    sla_budget,
    get_sla_target_pct,
    get_availability_settings,
    save_availability_settings,
    classify_probe_failure,
)
from .helpers import (
    _attach_sla_budgets,
    _availability_status_counts,
    _build_fleet_trend,
    _FLEET_TREND_CACHE,
    _FLEET_TREND_CACHE_TTL,
)

# 2. Models & cache
from .models import AvailabilityQuery, AvailabilityReport
from ._cache import AvailabilityCache

# 3. Deep engine (orchestrates monitoring, storage, and availability)
from .engine import AvailabilityEngine

# Default singleton instance for application use
availability_engine = AvailabilityEngine()

__all__ = [
    "AvailabilityEngine",
    "AvailabilityQuery",
    "AvailabilityReport",
    "AvailabilityCache",
    "availability_engine",
    "summarize_entries",
    "reconstruct_time_series_intervals",
    "calculate_percentile",
    "clip_hourly_bucket",
    "merge_hybrid_target_availability",
    "merge_hybrid_fleet_availability",
    "derive_bucket_inputs",
    "estimate_instance_cadence",
    "sla_budget",
    "get_sla_target_pct",
    "get_availability_settings",
    "save_availability_settings",
    "classify_probe_failure",
    "_attach_sla_budgets",
    "_availability_status_counts",
    "_build_fleet_trend",
    "_FLEET_TREND_CACHE",
    "_FLEET_TREND_CACHE_TTL",
]
