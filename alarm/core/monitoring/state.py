"""Monitoring-state domain: backward-compatibility adapter and facade over FleetStateEngine.

Exposes `build_canonical_monitoring_state`, `get_instance_job_map`,
`get_instance_cadence_map`, and `get_monitored_instances`.
"""
from typing import Dict, List, Optional

try:
    from config import DEFAULT_JOB_FILTER
    from .models import FleetQuery, FleetSummary, FleetState
    from .engine import (
        FleetStateEngine,
        fleet_state_engine,
        _derive_probe_readings,
    )
except (ImportError, ValueError):
    from alarm.config import DEFAULT_JOB_FILTER
    from alarm.core.monitoring.models import FleetQuery, FleetSummary, FleetState
    from alarm.core.monitoring.engine import (
        FleetStateEngine,
        fleet_state_engine,
        _derive_probe_readings,
    )


def build_canonical_monitoring_state(job_param: Optional[str] = None) -> Dict:
    """Backward-compatible facade returning canonical fleet state dictionary."""
    query = FleetQuery(job_filter=job_param if job_param is not None else DEFAULT_JOB_FILTER)
    return fleet_state_engine.get_fleet_state(query).to_dict()


def get_instance_job_map(job_filter: Optional[str] = None) -> Dict[str, str]:
    """Instance -> real Prometheus job name for every monitored target matching job_filter."""
    return fleet_state_engine.get_instance_job_map(job_filter)


def get_instance_cadence_map(job_filter: Optional[str] = None) -> Dict[str, float]:
    """Instance -> real per-target scrape interval (seconds)."""
    return fleet_state_engine.get_instance_cadence_map(job_filter)


def get_monitored_instances(job_filter: Optional[str] = None) -> List[str]:
    """Sorted list of all monitored target instances."""
    return fleet_state_engine.get_monitored_instances(job_filter)
