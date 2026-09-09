"""Incident, event log, and acknowledgment repositories for InfraWatch.
"""
import time
from typing import List, Dict, Any, Optional

try:
    from ..connection import db_read, db_transaction
    from .inventory import MaintenanceRepository
except (ImportError, ValueError):
    from alarm.storage.connection import db_read, db_transaction
    from alarm.storage.repositories.inventory import MaintenanceRepository


class IncidentRepository:
    @staticmethod
    def get_active_incidents(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
        with db_read(db_path) as conn:
            rows = conn.execute("""
                SELECT * FROM incidents WHERE status = 'firing' ORDER BY started_at DESC
            """).fetchall()
            return [
                {
                    "key": r["key"],
                    "fingerprint": r["fingerprint"],
                    "name": r["name"],
                    "severity": r["severity"],
                    "instance": r["instance"],
                    "summary": r["summary"],
                    "job": r["job"],
                    "receiver": r["receiver"],
                    "generatorURL": r["generator_url"],
                    "time": r["started_at"],
                    "status": "firing",
                    "latency_ms": r["latency_ms"],
                    "occurrences": r["occurrences"],
                    "first_seen": r["first_seen"],
                    "http_status_code": r["http_status_code"],
                    "last_error": r["last_error"],
                    "total_down_seconds": r["total_down_seconds"],
                    "acknowledged_by": r["acknowledged_by"],
                    "acknowledged_at": r["acknowledged_at"]
                }
                for r in rows
            ]

    @staticmethod
    def get_history(limit: int = 1000, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
        with db_read(db_path) as conn:
            firing_rows = conn.execute("""
                SELECT * FROM incidents WHERE status = 'firing' ORDER BY updated_at DESC
            """).fetchall()
            resolved_rows = conn.execute("""
                SELECT * FROM incidents WHERE status = 'resolved' ORDER BY updated_at DESC LIMIT ?
            """, (limit,)).fetchall()
            return [
                {
                    "key": r["key"],
                    "name": r["name"],
                    "severity": r["severity"],
                    "instance": r["instance"],
                    "summary": r["summary"],
                    "time": r["started_at"],
                    "status": r["status"],
                    "job": r["job"],
                    "receiver": r["receiver"],
                    "generatorURL": r["generator_url"],
                    "resolved_time": r["resolved_at"],
                    "duration_seconds": r["duration_seconds"],
                    "latency_ms": r["latency_ms"],
                    "occurrences": r["occurrences"],
                    "first_seen": r["first_seen"] if r["first_seen"] is not None else r["started_at"],
                    "http_status_code": r["http_status_code"],
                    "last_error": r["last_error"],
                    "total_down_seconds": r["total_down_seconds"],
                    "acknowledged_by": r["acknowledged_by"],
                    "acknowledged_at": r["acknowledged_at"]
                }
                for r in list(firing_rows) + list(resolved_rows)
            ]

    @staticmethod
    def record_alert_event(
        name: str,
        severity: str,
        instance: str,
        summary: str,
        job: str,
        event_time: float,
        is_now_firing: bool,
        receiver: str = "",
        generatorURL: str = "",
        key: Optional[str] = None,
        latency_ms: Optional[float] = None,
        http_status_code: Optional[int] = None,
        last_error: Optional[str] = None,
        db_path: Optional[str] = None
    ) -> bool:
        key = key or f"{name}|{instance}"
        with db_transaction(db_path) as conn:
            # Check maintenance suppression
            if is_now_firing and MaintenanceRepository.get_active_maintenance(instance, job, now=event_time, db_path=db_path):
                return False

            row = conn.execute("SELECT * FROM incidents WHERE key = ?", (key,)).fetchone()
            was_firing = (row is not None and row["status"] == "firing")

            if was_firing == is_now_firing:
                return False  # Idempotent deduplication

            duration_seconds = None
            if is_now_firing:
                conn.execute("""
                    INSERT INTO incidents (
                        key, fingerprint, name, severity, instance, summary, job,
                        receiver, generator_url, status, started_at, first_seen, occurrences,
                        resolved_at, duration_seconds, updated_at, latency_ms, http_status_code, last_error
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'firing', ?, ?, 1, NULL, NULL, ?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        status = 'firing',
                        severity = excluded.severity,
                        summary = excluded.summary,
                        started_at = excluded.started_at,
                        first_seen = COALESCE(incidents.first_seen, excluded.first_seen),
                        occurrences = incidents.occurrences + 1,
                        resolved_at = NULL,
                        duration_seconds = NULL,
                        updated_at = excluded.updated_at,
                        latency_ms = excluded.latency_ms,
                        http_status_code = excluded.http_status_code,
                        last_error = excluded.last_error,
                        acknowledged_by = NULL,
                        acknowledged_at = NULL
                """, (key, key, name, severity, instance, summary, job, receiver, generatorURL,
                      event_time, event_time, event_time, latency_ms, http_status_code, last_error))

                conn.execute("""
                    INSERT INTO event_logs (event, name, severity, instance, summary, job, time, duration_seconds, latency_ms, fingerprint)
                    VALUES ('firing', ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """, (name, severity, instance, summary, job, event_time, latency_ms, key))
            else:
                started_at = row["started_at"] if row else event_time
                duration_seconds = round(float(event_time - started_at), 1)
                conn.execute("""
                    UPDATE incidents SET status = 'resolved', resolved_at = ?, duration_seconds = ?,
                        total_down_seconds = COALESCE(total_down_seconds, 0) + ?, updated_at = ?
                    WHERE key = ?
                """, (event_time, duration_seconds, duration_seconds, event_time, key))

                conn.execute("""
                    INSERT INTO event_logs (event, name, severity, instance, summary, job, time, duration_seconds, latency_ms, fingerprint)
                    VALUES ('resolved', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (name, severity, instance, summary, job, event_time, duration_seconds, latency_ms, key))

                conn.execute("""
                    DELETE FROM alert_acknowledgments WHERE instance = ? AND NOT EXISTS (
                        SELECT 1 FROM incidents WHERE instance = ? AND status = 'firing'
                    )
                """, (instance, instance))

            # Retention limits
            conn.execute("""
                DELETE FROM event_logs WHERE id NOT IN (
                    SELECT id FROM event_logs ORDER BY time DESC, id DESC LIMIT 5000
                )
            """)
            conn.execute("""
                DELETE FROM incidents WHERE status = 'resolved' AND id NOT IN (
                    SELECT id FROM incidents WHERE status = 'resolved' ORDER BY updated_at DESC, id DESC LIMIT 5000
                )
            """)
            return True


class EventLogRepository:
    @staticmethod
    def get_logs(
        limit: int = 50,
        instance: Optional[str] = None,
        since_time: Optional[float] = None,
        db_path: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        with db_read(db_path) as conn:
            query = "SELECT * FROM event_logs WHERE 1=1"
            params: List[Any] = []
            if instance:
                query += " AND instance = ?"
                params.append(instance)
            if since_time is not None:
                query += " AND time >= ?"
                params.append(since_time)
            query += " ORDER BY time DESC, id DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            return [
                {
                    "event": r["event"],
                    "name": r["name"],
                    "severity": r["severity"],
                    "instance": r["instance"],
                    "summary": r["summary"],
                    "job": r["job"],
                    "time": r["time"],
                    "duration_seconds": r["duration_seconds"],
                    "latency_ms": r["latency_ms"]
                }
                for r in rows
            ]


class AcknowledgmentRepository:
    @staticmethod
    def acknowledge_instances(
        instances: List[str],
        username: str,
        now: Optional[float] = None,
        db_path: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        t = now if now is not None else time.time()
        res = []
        with db_transaction(db_path) as conn:
            for inst in instances:
                inst_clean = str(inst).strip()
                if not inst_clean:
                    continue
                conn.execute("""
                    INSERT OR REPLACE INTO alert_acknowledgments (target_key, instance, acknowledged_by, acknowledged_at, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (inst_clean, inst_clean, username, t, t))
                conn.execute("""
                    UPDATE incidents SET acknowledged_by = ?, acknowledged_at = ?
                    WHERE instance = ? AND status = 'firing'
                """, (username, t, inst_clean))
                res.append({
                    "instance": inst_clean,
                    "acknowledged": True,
                    "acknowledged_by": username,
                    "acknowledged_at": t
                })
        return res

    @staticmethod
    def unacknowledge_instance(instance: str, db_path: Optional[str] = None) -> bool:
        with db_transaction(db_path) as conn:
            cur = conn.execute("DELETE FROM alert_acknowledgments WHERE target_key = ? OR instance = ?", (instance, instance))
            conn.execute("""
                UPDATE incidents SET acknowledged_by = NULL, acknowledged_at = NULL
                WHERE instance = ? AND status = 'firing'
            """, (instance,))
            return cur.rowcount > 0

    @staticmethod
    def get_active_acknowledgments(db_path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        with db_read(db_path) as conn:
            rows = conn.execute("SELECT * FROM alert_acknowledgments").fetchall()
            return {
                r["instance"]: {
                    "target_key": r["target_key"],
                    "instance": r["instance"],
                    "acknowledged_by": r["acknowledged_by"],
                    "acknowledged_at": r["acknowledged_at"],
                    "created_at": r["created_at"]
                }
                for r in rows
            }

    @staticmethod
    def clear_resolved(active_down_instances: set, db_path: Optional[str] = None):
        """Clean up acknowledgments for targets that are no longer down.
        Single DELETE with a NOT IN filter rather than SELECT + per-row DELETE."""
        instances = list(active_down_instances)
        with db_transaction(db_path) as conn:
            if instances:
                placeholders = ",".join("?" for _ in instances)
                conn.execute(
                    f"DELETE FROM alert_acknowledgments WHERE instance NOT IN ({placeholders})",
                    instances,
                )
            else:
                conn.execute("DELETE FROM alert_acknowledgments")
