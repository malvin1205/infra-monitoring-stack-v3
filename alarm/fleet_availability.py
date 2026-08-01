"""
Fleet availability calculator.

Menghitung 4 metrik availability yang terpisah untuk sekumpulan server:
  1. per_server      — availability masing-masing server
  2. fleet_average    — rata-rata unweighted dari availability per server
  3. fleet_aggregate  — availability weighted berdasarkan total waktu (metrik SLA)
  4. health_ratio     — persentase server yang tidak pernah down sama sekali
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional, Union

ServerRecord = Dict[str, Union[str, int, float, datetime]]


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
    return max(0.0, min(100.0, value))


def summarize_entries(entries: List[Dict], period_minutes: float) -> Dict:
    """
    Shared aggregation core, reused by calculate_fleet_availability() (fed from
    manually tracked downtime_minutes/created_at) and by any other caller that
    already has per-server figures (e.g. derived from Prometheus query results).

    entries: [{ id, name, availability_pct, denominator_minutes, downtime_minutes }]
      - availability_pct may be None for a server with no data in the window;
        such entries are excluded from fleet_average/fleet_aggregate but still
        listed under per_server.
    """
    server_count = len(entries)

    if server_count == 0:
        return {
            "period_minutes": round(period_minutes, 2),
            "server_count": 0,
            "per_server": {"label": "Per-Server Availability", "unit": "%", "values": []},
            "fleet_average": {"label": "Fleet Average Availability (unweighted)", "value": None, "unit": "%"},
            "fleet_aggregate": {"label": "Fleet Aggregate Availability (weighted, SLA)", "value": None, "unit": "%"},
            "health_ratio": {"label": "Health Ratio (never-down servers)", "value": None, "unit": "%"},
        }

    per_server = []
    total_capacity = 0.0
    total_downtime = 0.0
    never_down_count = 0
    availability_sum = 0.0
    scored_count = 0

    for entry in entries:
        availability = entry.get("availability_pct")
        denominator = float(entry.get("denominator_minutes") or 0)
        downtime_minutes = float(entry.get("downtime_minutes") or 0)

        per_server.append({
            "id": entry.get("id"),
            "name": entry.get("name"),
            "availability_pct": availability,
            "denominator_minutes": round(denominator, 2),
            "downtime_minutes": round(downtime_minutes, 2),
        })

        if availability is not None:
            availability_sum += availability
            scored_count += 1
            total_capacity += denominator
            total_downtime += downtime_minutes
            if downtime_minutes <= 0:
                never_down_count += 1

    fleet_average = round(availability_sum / scored_count, 2) if scored_count > 0 else None

    fleet_aggregate = (
        round(_clamp_pct(((total_capacity - total_downtime) / total_capacity) * 100.0), 2)
        if total_capacity > 0 else None
    )

    health_ratio = round((never_down_count / scored_count) * 100.0, 2) if scored_count > 0 else None

    return {
        "period_minutes": round(period_minutes, 2),
        "server_count": server_count,
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
    }


def calculate_fleet_availability(
    servers: List[ServerRecord],
    period_minutes: float,
    now: Optional[datetime] = None,
) -> Dict:
    """
    servers: [{ id, name, downtime_minutes, created_at }]
    period_minutes: panjang periode filter dalam menit (mis. 7 hari = 10080)
    now: waktu acuan "sekarang" (default: waktu saat ini, UTC) — override untuk testing
    """
    if period_minutes <= 0:
        raise ValueError("period_minutes must be > 0")

    now = now or datetime.now(timezone.utc)

    entries = []
    for server in servers:
        downtime_minutes = float(server.get("downtime_minutes", 0) or 0)
        created_at = _parse_created_at(server["created_at"])

        age_minutes = max((now - created_at).total_seconds() / 60.0, 0.0)
        # Server baru (umur < periode filter) -> pakai usianya sendiri sebagai denominator
        denominator = min(age_minutes, period_minutes)

        if denominator <= 0:
            availability = 100.0 if downtime_minutes <= 0 else 0.0
        else:
            availability = _clamp_pct(((denominator - downtime_minutes) / denominator) * 100.0)

        entries.append({
            "id": server.get("id"),
            "name": server.get("name"),
            "availability_pct": round(availability, 2),
            "denominator_minutes": denominator,
            "downtime_minutes": downtime_minutes,
        })

    return summarize_entries(entries, period_minutes)


if __name__ == "__main__":
    import json

    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    period_7d = 7 * 24 * 60  # 10080 menit

    demo_servers = [
        {"id": "srv-1", "name": "web-01", "downtime_minutes": 0, "created_at": "2026-01-01T00:00:00Z"},
        {"id": "srv-2", "name": "web-02", "downtime_minutes": 60, "created_at": "2026-01-01T00:00:00Z"},
        {"id": "srv-3", "name": "web-03", "downtime_minutes": 1440, "created_at": "2026-01-01T00:00:00Z"},
        # server baru, umur 2 hari (< periode 7 hari)
        {"id": "srv-4", "name": "web-04-new", "downtime_minutes": 30, "created_at": "2026-07-28T00:00:00Z"},
    ]

    result = calculate_fleet_availability(demo_servers, period_7d, now=now)
    print(json.dumps(result, indent=2))
