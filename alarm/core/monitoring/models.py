"""Fleet monitoring domain models: value specifications and domain outcomes.
"""
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional
import time

try:
    from config import DEFAULT_JOB_FILTER
except (ImportError, ValueError):
    from alarm.config import DEFAULT_JOB_FILTER


@dataclass(frozen=True)
class FleetQuery:
    """Value specification defining the query parameters for a fleet monitoring evaluation."""
    job_filter: str = DEFAULT_JOB_FILTER
    now: Optional[float] = None

    @classmethod
    def from_request(cls, args: Dict[str, Any], default_job: str = DEFAULT_JOB_FILTER) -> "FleetQuery":
        job = args.get("job")
        if not job or not str(job).strip():
            job = default_job
        else:
            job = str(job).strip()
        return cls(job_filter=job)


@dataclass(frozen=True)
class FleetSummary:
    """Consolidated health metrics and status counters across all monitored targets."""
    total: int = 0
    up: int = 0
    down: int = 0
    no_data: int = 0
    slow: int = 0
    maintenance: int = 0
    suppressed: int = 0
    alarmable_down: int = 0
    alarmable_alerts: int = 0
    unacknowledged_down: int = 0
    acknowledged_down: int = 0
    is_acknowledged: bool = False
    has_alarm: bool = False
    has_unacknowledged_alarm: bool = False
    system_status: str = "NORMAL"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FleetState:
    """Structured canonical domain snapshot of fleet health, target grid, and active alerts."""
    ok: bool
    system_status: str
    summary: FleetSummary
    targets: List[Dict[str, Any]] = field(default_factory=list)
    active_alerts: List[Dict[str, Any]] = field(default_factory=list)
    available_jobs: List[str] = field(default_factory=list)
    job_filter: str = DEFAULT_JOB_FILTER
    source: str = "local"
    prometheus_url: Optional[str] = None
    updated: int = field(default_factory=lambda: int(time.time()))
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Full representation for /instances and /api/instances."""
        payload: Dict[str, Any] = {
            "ok": self.ok,
            "system_status": self.system_status,
            "summary": self.summary.to_dict(),
            "active_alerts": self.active_alerts,
            "targets": self.targets,
            "available_jobs": sorted(self.available_jobs),
            "job_filter": self.job_filter,
            "source": self.source,
            "prometheus_url": self.prometheus_url,
            "updated": self.updated,
        }
        if not self.ok and self.error:
            payload["error"] = self.error
        return payload

    def to_status_dict(self) -> Dict[str, Any]:
        """Streamlined representation for /status and /api/status."""
        return {
            "status": self.system_status,
            "system_status": self.system_status,
            "alerts": self.active_alerts,
            "summary": self.summary.to_dict(),
            "updated": self.updated,
        }
