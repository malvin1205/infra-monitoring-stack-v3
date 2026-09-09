"""Availability buckets, leases, SLA targets, and latency threshold repositories for InfraWatch.
"""
import time
import json
import sqlite3
from typing import List, Dict, Any, Optional

try:
    from ..connection import db_read, db_transaction
except (ImportError, ValueError):
    from alarm.storage.connection import db_read, db_transaction


class SlaTargetRepository:
    """Per-target SLA availability target (%). Absent -> the deployment
    default (SLA_TARGET_PCT env / SLA_COMPLIANCE_THRESHOLD) applies."""

    @staticmethod
    def get_all(db_path: Optional[str] = None) -> Dict[str, float]:
        with db_read(db_path) as conn:
            rows = conn.execute("SELECT instance, target_pct FROM sla_targets").fetchall()
            return {r["instance"]: float(r["target_pct"]) for r in rows}

    @staticmethod
    def get_target(instance: str, db_path: Optional[str] = None) -> Optional[float]:
        with db_read(db_path) as conn:
            row = conn.execute(
                "SELECT target_pct FROM sla_targets WHERE instance = ?", (instance,)
            ).fetchone()
            return float(row["target_pct"]) if row else None

    @staticmethod
    def set_target(instance: str, target_pct: float, updated_by: Optional[str] = None,
                   db_path: Optional[str] = None):
        pct = max(0.0, min(100.0, float(target_pct)))
        with db_transaction(db_path) as conn:
            conn.execute(
                """INSERT INTO sla_targets (instance, target_pct, updated_by, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(instance) DO UPDATE SET
                       target_pct = excluded.target_pct,
                       updated_by = excluded.updated_by,
                       updated_at = excluded.updated_at""",
                (instance, pct, updated_by, time.time()),
            )
        return pct

    @staticmethod
    def delete_target(instance: str, db_path: Optional[str] = None) -> bool:
        with db_transaction(db_path) as conn:
            cur = conn.execute("DELETE FROM sla_targets WHERE instance = ?", (instance,))
            return cur.rowcount > 0


class SlowThresholdRepository:
    """Per-instance SlowResponse threshold (ms). Absent -> the deployment
    default (DEFAULT_SLOW_RESPONSE_THRESHOLD_MS) applies — some targets
    (e.g. a naturally slower overseas endpoint) aren't actually degraded
    at the global default, they're just always like that."""

    @staticmethod
    def get_all(db_path: Optional[str] = None) -> Dict[str, float]:
        with db_read(db_path) as conn:
            rows = conn.execute("SELECT instance, threshold_ms FROM slow_thresholds").fetchall()
            return {r["instance"]: float(r["threshold_ms"]) for r in rows}

    @staticmethod
    def get_threshold(instance: str, db_path: Optional[str] = None) -> Optional[float]:
        with db_read(db_path) as conn:
            row = conn.execute(
                "SELECT threshold_ms FROM slow_thresholds WHERE instance = ?", (instance,)
            ).fetchone()
            return float(row["threshold_ms"]) if row else None

    @staticmethod
    def set_threshold(instance: str, threshold_ms: float, updated_by: Optional[str] = None,
                       db_path: Optional[str] = None):
        ms = max(0.0, float(threshold_ms))
        with db_transaction(db_path) as conn:
            conn.execute(
                """INSERT INTO slow_thresholds (instance, threshold_ms, updated_by, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(instance) DO UPDATE SET
                       threshold_ms = excluded.threshold_ms,
                       updated_by = excluded.updated_by,
                       updated_at = excluded.updated_at""",
                (instance, ms, updated_by, time.time()),
            )
        return ms

    @staticmethod
    def delete_threshold(instance: str, db_path: Optional[str] = None) -> bool:
        with db_transaction(db_path) as conn:
            cur = conn.execute("DELETE FROM slow_thresholds WHERE instance = ?", (instance,))
            return cur.rowcount > 0


class AvailabilityBucketRepository:
    @staticmethod
    def save_buckets(buckets: List[Dict[str, Any]], db_path: Optional[str] = None):
        if not buckets:
            return
        now = time.time()
        with db_transaction(db_path) as conn:
            conn.executemany("""
                INSERT OR REPLACE INTO availability_buckets (
                    instance, job, bucket_start, bucket_end,
                    uptime_seconds, downtime_seconds, unknown_seconds, coverage_seconds,
                    sample_count, availability_pct, incident_count, avg_latency_ms, updated_at,
                    outage_json
                ) VALUES (
                    :instance, :job, :bucket_start, :bucket_end,
                    :uptime_seconds, :downtime_seconds, :unknown_seconds, :coverage_seconds,
                    :sample_count, :availability_pct, :incident_count, :avg_latency_ms, :updated_at,
                    :outage_json
                )
            """, [
                {
                    "instance": b["instance"],
                    "job": b.get("job", "blackbox"),
                    "bucket_start": float(b["bucket_start"]),
                    "bucket_end": float(b["bucket_end"]),
                    "uptime_seconds": float(b.get("uptime_seconds", 0.0)),
                    "downtime_seconds": float(b.get("downtime_seconds", 0.0)),
                    "unknown_seconds": float(b.get("unknown_seconds", 0.0)),
                    "coverage_seconds": float(b.get("coverage_seconds", 0.0)),
                    "sample_count": int(b.get("sample_count", 0)),
                    "availability_pct": float(b["availability_pct"]) if b.get("availability_pct") is not None else None,
                    "incident_count": int(b.get("incident_count", 0)),
                    "avg_latency_ms": float(b.get("avg_latency_ms", 0.0)),
                    "updated_at": float(b.get("updated_at", now)),
                    "outage_json": b["outage_json"] if isinstance(b.get("outage_json"), str) else (
                        json.dumps(b["outage_json"]) if b.get("outage_json") is not None else None
                    ),
                }
                for b in buckets
            ])

    @staticmethod
    def get_aggregated_availability(
        job: str,
        start_time: float,
        end_time: float,
        instances: Optional[List[str]] = None,
        db_path: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        with db_read(db_path) as conn:
            query = """
                SELECT 
                    instance,
                    job,
                    SUM(uptime_seconds) as total_uptime_sec,
                    SUM(downtime_seconds) as total_downtime_sec,
                    SUM(unknown_seconds) as total_unknown_sec,
                    SUM(coverage_seconds) as total_coverage_sec,
                    SUM(sample_count) as total_samples,
                    SUM(incident_count) as total_incidents,
                    AVG(avg_latency_ms) as mean_latency_ms,
                    MIN(bucket_start) as earliest_bucket_start,
                    MAX(bucket_end) as latest_bucket_end,
                    COUNT(*) as bucket_count
                FROM availability_buckets
                WHERE (job = ? OR ? = 'all')
                  AND bucket_end > ?
                  AND bucket_start < ?
            """
            params: List[Any] = [job, job, start_time, end_time]
            if instances:
                placeholders = ",".join("?" for _ in instances)
                query += f" AND instance IN ({placeholders})"
                params.extend(instances)
            query += " GROUP BY instance, job ORDER BY instance ASC"
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def get_bucket_records(
        job: str,
        start_time: float,
        end_time: float,
        instances: Optional[List[str]] = None,
        db_path: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        with db_read(db_path) as conn:
            query = """
                SELECT id, instance, job, bucket_start, bucket_end,
                       uptime_seconds, downtime_seconds, unknown_seconds, coverage_seconds,
                       sample_count, availability_pct, incident_count, avg_latency_ms, updated_at,
                       outage_json
                FROM (
                    SELECT *,
                        ROW_NUMBER() OVER (
                            PARTITION BY instance, bucket_start
                            ORDER BY updated_at DESC, id DESC
                        ) AS rn
                    FROM availability_buckets
                    WHERE (job = ? OR ? = 'all')
                      AND bucket_end > ?
                      AND bucket_start < ?
            """
            params: List[Any] = [job, job, start_time, end_time]
            if instances:
                placeholders = ",".join("?" for _ in instances)
                query += f" AND instance IN ({placeholders})"
                params.extend(instances)
            query += """
                )
                WHERE rn = 1
                ORDER BY instance ASC, bucket_start ASC
            """
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def get_latest_bucket_end(job: str = 'all', db_path: Optional[str] = None) -> Optional[float]:
        with db_read(db_path) as conn:
            row = conn.execute(
                "SELECT MAX(bucket_end) as max_end FROM availability_buckets WHERE (job = ? OR ? = 'all')",
                (job, job)
            ).fetchone()
            return float(row["max_end"]) if row and row["max_end"] is not None else None

    @staticmethod
    def get_bucket_count_in_range(job: str, start_time: float, end_time: float, db_path: Optional[str] = None) -> int:
        with db_read(db_path) as conn:
            if job == 'all':
                row = conn.execute(
                    "SELECT COUNT(DISTINCT instance || ':' || bucket_start) as c FROM availability_buckets WHERE bucket_end > ? AND bucket_start < ?",
                    (start_time, end_time)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) as c FROM availability_buckets WHERE job = ? AND bucket_end > ? AND bucket_start < ?",
                    (job, start_time, end_time)
                ).fetchone()
            return int(row["c"]) if row and row["c"] is not None else 0

    @staticmethod
    def prune_old_buckets(retention_seconds: float = 35 * 86400, db_path: Optional[str] = None) -> int:
        cutoff = time.time() - retention_seconds
        with db_transaction(db_path) as conn:
            cur = conn.execute("DELETE FROM availability_buckets WHERE bucket_end < ?", (cutoff,))
            return cur.rowcount

    @staticmethod
    def clear_all_buckets(db_path: Optional[str] = None):
        with db_transaction(db_path) as conn:
            conn.execute("DELETE FROM availability_buckets")


class AggregationLeaseRepository:
    @staticmethod
    def acquire_or_renew(lease_name: str, owner_id: str, ttl_sec: float = 30.0, db_path: Optional[str] = None) -> bool:
        now = time.time()
        with db_transaction(db_path) as conn:
            cur = conn.execute("""
                UPDATE aggregation_leases
                SET owner_id = ?, acquired_at = ?, expires_at = ?
                WHERE lease_name = ? AND (expires_at < ? OR owner_id = ?)
            """, (owner_id, now, now + ttl_sec, lease_name, now, owner_id))
            if cur.rowcount > 0:
                return True
            try:
                conn.execute("""
                    INSERT INTO aggregation_leases (lease_name, owner_id, acquired_at, expires_at)
                    VALUES (?, ?, ?, ?)
                """, (lease_name, owner_id, now, now + ttl_sec))
                return True
            except sqlite3.IntegrityError:
                return False

    @staticmethod
    def release(lease_name: str, owner_id: str, db_path: Optional[str] = None):
        with db_transaction(db_path) as conn:
            conn.execute("DELETE FROM aggregation_leases WHERE lease_name = ? AND owner_id = ?", (lease_name, owner_id))
