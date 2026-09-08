"""Availability & SLA domain: fleet availability, hourly buckets, and TSDB trends.
"""
from .fleet import (
    summarize_entries, reconstruct_time_series_intervals, calculate_percentile,
    clip_hourly_bucket, merge_hybrid_target_availability, merge_hybrid_fleet_availability,
    derive_bucket_inputs, estimate_instance_cadence, sla_budget, get_sla_target_pct,
    get_availability_settings, save_availability_settings, classify_probe_failure,
)
from .helpers import (
    _attach_sla_budgets, _availability_status_counts, _build_fleet_trend,
    _FLEET_TREND_CACHE, _FLEET_TREND_CACHE_TTL,
)
