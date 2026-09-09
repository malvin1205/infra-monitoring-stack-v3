"""Availability domain models: query parameters and structured report."""
from dataclasses import dataclass
from typing import Optional, Dict, Any, List


@dataclass(frozen=True)
class AvailabilityQuery:
    job: str = "node"
    minutes: float = 1440.0
    end_ts: Optional[float] = None
    sla_target_pct: float = 99.9
    sla_days: int = 30
    debug: bool = False

    @property
    def minutes_int(self) -> int:
        return int(round(self.minutes))

    @classmethod
    def from_request(
        cls,
        args: Any,
        default_job: str = "node",
        default_sla_target_pct: float = 99.9
    ) -> "AvailabilityQuery":
        # Extract job
        # Do NOT case-fold: this value is matched verbatim against the stored
        # bucket `job` column and passed to get_monitored_instances(). Lower-
        # casing it silently breaks the SQLite coverage fast path whenever
        # Prometheus reports a non-lowercase job name.
        job_raw = args.get("job") if hasattr(args, "get") else default_job
        job = (job_raw if job_raw is not None else default_job).strip()
        if not job:
            job = default_job

        # Extract minutes or days
        minutes_param = args.get("minutes") if hasattr(args, "get") else None
        if minutes_param is not None:
            try:
                minutes = float(minutes_param)
            except (TypeError, ValueError):
                minutes = 1440.0
        else:
            try:
                days = float(args.get("days", 1)) if hasattr(args, "get") else 1.0
            except (TypeError, ValueError):
                days = 1.0
            minutes = days * 1440.0
        minutes = max(1.0, min(minutes, 366 * 1440.0))

        # SLA target percent
        try:
            sla_target_param = args.get("sla_target") if hasattr(args, "get") else None
            if sla_target_param is not None:
                sla_target_pct = max(0.0, min(100.0, float(sla_target_param)))
            else:
                sla_target_pct = default_sla_target_pct
        except (TypeError, ValueError):
            sla_target_pct = default_sla_target_pct

        # SLA days
        try:
            sla_days = max(1, min(365, int(float(args.get("sla_days", 30))))) if hasattr(args, "get") else 30
        except (TypeError, ValueError):
            sla_days = 30

        # End timestamp
        end_ts = None
        end_param = args.get("end") if hasattr(args, "get") else None
        if end_param is not None:
            try:
                end_ts = int(float(end_param))
            except (TypeError, ValueError):
                end_ts = None

        debug = bool(args.get("debug")) if hasattr(args, "get") else False

        return cls(
            job=job,
            minutes=minutes,
            end_ts=end_ts,
            sla_target_pct=sla_target_pct,
            sla_days=sla_days,
            debug=debug,
        )


@dataclass
class AvailabilityReport:
    payload: Dict[str, Any]

    @property
    def fleet(self) -> Dict[str, Any]:
        return self.payload.get("fleet_aggregate", {})

    @property
    def instances(self) -> List[Dict[str, Any]]:
        return self.payload.get("entries", [])

    @property
    def status_counts(self) -> Dict[str, int]:
        return self.payload.get("counts", {})

    @property
    def trend(self) -> List[Dict[str, Any]]:
        return self.payload.get("trend", [])

    @property
    def sla_budgets(self) -> Dict[str, Any]:
        return self.payload.get("sla", {})

    @property
    def telemetry(self) -> Dict[str, Any]:
        return self.payload.get("_trace", {})

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.payload)
