import sqlite3
import os
import time
import json
from datetime import datetime
from typing import List, Dict, Any, Optional

from contextlib import contextmanager

DB_DIR = os.path.dirname(__file__)
DEFAULT_DB_PATH = os.path.join(DB_DIR, "infrawatch.db")


def get_db(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or os.environ.get("INFRAWATCH_DB_PATH", DEFAULT_DB_PATH)
    conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    return conn


@contextmanager
def db_transaction(db_path: Optional[str] = None):
    conn = get_db(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        yield conn
        conn.execute("COMMIT;")
    except Exception:
        try:
            conn.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        conn.close()


@contextmanager
def db_read(db_path: Optional[str] = None):
    conn = get_db(db_path)
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: Optional[str] = None):
    conn = get_db(db_path)
    with conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE NOT NULL,
                fingerprint TEXT,
                name TEXT NOT NULL,
                severity TEXT NOT NULL,
                instance TEXT NOT NULL,
                summary TEXT,
                job TEXT,
                receiver TEXT DEFAULT '',
                generator_url TEXT DEFAULT '',
                status TEXT NOT NULL, -- 'firing' or 'resolved'
                started_at REAL NOT NULL,
                resolved_at REAL,
                duration_seconds REAL,
                latency_ms REAL,
                updated_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);
            CREATE INDEX IF NOT EXISTS idx_incidents_instance ON incidents(instance);
            CREATE INDEX IF NOT EXISTS idx_incidents_started_at ON incidents(started_at);
            CREATE INDEX IF NOT EXISTS idx_incidents_updated_at ON incidents(updated_at DESC);

            CREATE TABLE IF NOT EXISTS event_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT NOT NULL, -- 'firing' or 'resolved'
                name TEXT NOT NULL,
                severity TEXT NOT NULL,
                instance TEXT NOT NULL,
                summary TEXT,
                job TEXT,
                time REAL NOT NULL,
                duration_seconds REAL,
                latency_ms REAL,
                fingerprint TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_logs_time ON event_logs(time DESC);
            CREATE INDEX IF NOT EXISTS idx_logs_instance ON event_logs(instance);

            CREATE TABLE IF NOT EXISTS maintenance_windows (
                id TEXT PRIMARY KEY,
                scope TEXT NOT NULL, -- 'instance' or 'job'
                target TEXT NOT NULL,
                reason TEXT DEFAULT '',
                start_epoch REAL NOT NULL,
                end_epoch REAL NOT NULL,
                start_iso TEXT,
                end_iso TEXT,
                created_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_maint_epochs ON maintenance_windows(start_epoch, end_epoch);
            CREATE INDEX IF NOT EXISTS idx_maint_target ON maintenance_windows(target);

            CREATE TABLE IF NOT EXISTS dependencies (
                id TEXT PRIMARY KEY,
                parent TEXT NOT NULL,
                child TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_deps_parent ON dependencies(parent);
            CREATE INDEX IF NOT EXISTS idx_deps_child ON dependencies(child);

            CREATE TABLE IF NOT EXISTS endpoints (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                is_active INTEGER DEFAULT 0,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS deleted_targets (
                instance TEXT PRIMARY KEY,
                deleted_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS availability_buckets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance TEXT NOT NULL,
                job TEXT NOT NULL,
                bucket_start REAL NOT NULL,
                bucket_end REAL NOT NULL,
                uptime_seconds REAL NOT NULL DEFAULT 0.0,
                downtime_seconds REAL NOT NULL DEFAULT 0.0,
                unknown_seconds REAL NOT NULL DEFAULT 0.0,
                coverage_seconds REAL NOT NULL DEFAULT 0.0,
                sample_count INTEGER NOT NULL DEFAULT 0,
                availability_pct REAL,
                incident_count INTEGER NOT NULL DEFAULT 0,
                avg_latency_ms REAL NOT NULL DEFAULT 0.0,
                updated_at REAL NOT NULL,
                UNIQUE(job, instance, bucket_start)
            );

            CREATE INDEX IF NOT EXISTS idx_avail_lookup ON availability_buckets(job, instance, bucket_start);
            CREATE INDEX IF NOT EXISTS idx_avail_range ON availability_buckets(job, bucket_start, bucket_end);
            CREATE INDEX IF NOT EXISTS idx_avail_time ON availability_buckets(bucket_start, bucket_end);

            CREATE TABLE IF NOT EXISTS aggregation_leases (
                lease_name TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                acquired_at REAL NOT NULL,
                expires_at REAL NOT NULL
            );
        """)
        # Sanitize any legacy corrupted buckets (e.g. down hosts marked with positive uptime)
        conn.execute("DELETE FROM availability_buckets WHERE uptime_seconds > 0 AND availability_pct = 0.0")
        conn.execute("DELETE FROM availability_buckets WHERE coverage_seconds = 0.0 AND (uptime_seconds > 0 OR downtime_seconds > 0)")
        conn.commit()

    # Seed from JSON files only if using the default production DB and table is empty
    if db_path is None:
        _maybe_import_from_json(conn)
    conn.close()


def _maybe_import_from_json(conn: sqlite3.Connection):
    try:
        count = conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        if count == 0:
            status_file = os.path.join(DB_DIR, "status.json")
            if os.path.exists(status_file):
                with open(status_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                with conn:
                    for a in data.get("alerts", []):
                        k = a.get("key") or f"{a.get('name')}|{a.get('instance')}"
                        t = float(a.get("time", time.time()))
                        conn.execute("""
                            INSERT OR IGNORE INTO incidents (key, fingerprint, name, severity, instance, summary, job, status, started_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, 'firing', ?, ?)
                        """, (k, k, a.get("name", "Unknown"), a.get("severity", "critical"), a.get("instance", "-"), a.get("summary", ""), a.get("job", ""), t, t))

            logs_file = os.path.join(DB_DIR, "logs.json")
            if os.path.exists(logs_file):
                with open(logs_file, "r", encoding="utf-8") as f:
                    logs_data = json.load(f)
                with conn:
                    for l in logs_data:
                        conn.execute("""
                            INSERT INTO event_logs (event, name, severity, instance, summary, job, time, duration_seconds, latency_ms, fingerprint)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            l.get("event", "firing"),
                            l.get("name", "Alert"),
                            l.get("severity", "critical"),
                            l.get("instance", "-"),
                            l.get("summary", ""),
                            l.get("job", ""),
                            float(l.get("time", time.time())),
                            l.get("duration_seconds"),
                            l.get("latency_ms"),
                            l.get("fingerprint")
                        ))

            history_file = os.path.join(DB_DIR, "history.json")
            if os.path.exists(history_file):
                with open(history_file, "r", encoding="utf-8") as f:
                    hist_data = json.load(f)
                with conn:
                    for h in hist_data:
                        k = h.get("key") or f"{h.get('name')}|{h.get('instance')}|{h.get('time')}"
                        st = float(h.get("time", time.time()))
                        dur = h.get("duration_seconds")
                        conn.execute("""
                            INSERT OR IGNORE INTO incidents (key, fingerprint, name, severity, instance, summary, job, status, started_at, resolved_at, duration_seconds, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, 'resolved', ?, ?, ?, ?)
                        """, (
                            k, k, h.get("name", "Alert"), h.get("severity", "critical"),
                            h.get("instance", "-"), h.get("summary", ""), h.get("job", ""),
                            st, st + (dur or 0), dur, st + (dur or 0)
                        ))

            endpoints_file = os.path.join(DB_DIR, "endpoints.json")
            if os.path.exists(endpoints_file):
                with open(endpoints_file, "r", encoding="utf-8") as f:
                    ep_data = json.load(f)
                with conn:
                    for ep in ep_data.get("endpoints", []):
                        conn.execute("""
                            INSERT OR IGNORE INTO endpoints (id, name, url, is_active, created_at)
                            VALUES (?, ?, ?, ?, ?)
                        """, (ep.get("id"), ep.get("name"), ep.get("url"), 1 if ep.get("is_active") else 0, ep.get("created_at", time.time())))

            maint_file = os.path.join(DB_DIR, "maintenance.json")
            if os.path.exists(maint_file):
                with open(maint_file, "r", encoding="utf-8") as f:
                    maint_data = json.load(f)
                with conn:
                    for m in maint_data:
                        conn.execute("""
                            INSERT OR IGNORE INTO maintenance_windows (id, scope, target, reason, start_epoch, end_epoch, start_iso, end_iso, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            m.get("id"), m.get("scope") or m.get("scope_type", "instance"),
                            m.get("target") or m.get("scope_target", "-"), m.get("reason", ""),
                            float(m.get("start_epoch") or m.get("start") or 0),
                            float(m.get("end_epoch") or m.get("end") or 0),
                            str(m.get("start", "")), str(m.get("end", "")),
                            float(m.get("created_at", time.time()))
                        ))

            dep_file = os.path.join(DB_DIR, "dependencies.json")
            if os.path.exists(dep_file):
                with open(dep_file, "r", encoding="utf-8") as f:
                    dep_data = json.load(f)
                with conn:
                    for d in dep_data:
                        conn.execute("""
                            INSERT OR IGNORE INTO dependencies (id, parent, child, created_at)
                            VALUES (?, ?, ?, ?)
                        """, (d.get("id"), d.get("parent"), d.get("child"), float(d.get("created_at", time.time()))))
    except Exception:
        pass


# ── Repositories ─────────────────────────────────────────────────────────────

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
                    "latency_ms": r["latency_ms"]
                }
                for r in rows
            ]

    @staticmethod
    def get_history(limit: int = 1000, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
        with db_read(db_path) as conn:
            rows = conn.execute("""
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
                    "status": "resolved",
                    "job": r["job"],
                    "receiver": r["receiver"],
                    "generatorURL": r["generator_url"],
                    "resolved_time": r["resolved_at"],
                    "duration_seconds": r["duration_seconds"],
                    "latency_ms": r["latency_ms"]
                }
                for r in rows
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
                        receiver, generator_url, status, started_at, resolved_at, duration_seconds, updated_at, latency_ms
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'firing', ?, NULL, NULL, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        status = 'firing',
                        severity = excluded.severity,
                        summary = excluded.summary,
                        started_at = CASE WHEN incidents.status = 'firing' THEN incidents.started_at ELSE excluded.started_at END,
                        resolved_at = NULL,
                        duration_seconds = NULL,
                        updated_at = excluded.updated_at,
                        latency_ms = excluded.latency_ms
                """, (key, key, name, severity, instance, summary, job, receiver, generatorURL, event_time, event_time, latency_ms))

                conn.execute("""
                    INSERT INTO event_logs (event, name, severity, instance, summary, job, time, duration_seconds, latency_ms, fingerprint)
                    VALUES ('firing', ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """, (name, severity, instance, summary, job, event_time, latency_ms, key))
            else:
                started_at = row["started_at"] if row else event_time
                duration_seconds = round(float(event_time - started_at), 1)
                conn.execute("""
                    UPDATE incidents SET status = 'resolved', resolved_at = ?, duration_seconds = ?, updated_at = ?
                    WHERE key = ?
                """, (event_time, duration_seconds, event_time, key))

                conn.execute("""
                    INSERT INTO event_logs (event, name, severity, instance, summary, job, time, duration_seconds, latency_ms, fingerprint)
                    VALUES ('resolved', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (name, severity, instance, summary, job, event_time, duration_seconds, latency_ms, key))

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


class MaintenanceRepository:
    @staticmethod
    def list_windows(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
        now = time.time()
        with db_read(db_path) as conn:
            rows = conn.execute("SELECT * FROM maintenance_windows ORDER BY start_epoch DESC").fetchall()
            return [
                {
                    "id": r["id"],
                    "scope": r["scope"],
                    "target": r["target"],
                    "reason": r["reason"],
                    "start": r["start_epoch"],
                    "end": r["end_epoch"],
                    "start_epoch": r["start_epoch"],
                    "end_epoch": r["end_epoch"],
                    "start_iso": r["start_iso"],
                    "end_iso": r["end_iso"],
                    "active": (r["start_epoch"] <= now <= r["end_epoch"]),
                    "created_at": r["created_at"]
                }
                for r in rows
            ]

    @staticmethod
    def create_window(scope: str, target: str, reason: str, start: float, end: float, db_path: Optional[str] = None) -> Dict[str, Any]:
        win_id = f"mw_{int(time.time() * 1000)}"
        now = time.time()
        with db_transaction(db_path) as conn:
            conn.execute("""
                INSERT INTO maintenance_windows (id, scope, target, reason, start_epoch, end_epoch, start_iso, end_iso, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (win_id, scope, target, reason, start, end, str(start), str(end), now))
        return {
            "id": win_id,
            "scope": scope,
            "target": target,
            "reason": reason,
            "start": start,
            "end": end,
            "start_epoch": start,
            "end_epoch": end,
            "created_at": int(now)
        }

    @staticmethod
    def delete_window(window_id: str, db_path: Optional[str] = None) -> bool:
        with db_transaction(db_path) as conn:
            cursor = conn.execute("DELETE FROM maintenance_windows WHERE id = ?", (window_id,))
            return cursor.rowcount > 0

    @staticmethod
    def get_active_maintenance(instance: str, job: Optional[str] = None, now: Optional[float] = None, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
        curr = now if now is not None else time.time()
        with db_read(db_path) as conn:
            rows = conn.execute("""
                SELECT * FROM maintenance_windows WHERE start_epoch <= ? AND end_epoch >= ?
            """, (curr, curr)).fetchall()
            for r in rows:
                if r["scope"] == "job":
                    if job and r["target"] == job:
                        return dict(r)
                elif r["target"] == instance:
                    return dict(r)
            return None


class DependencyRepository:
    @staticmethod
    def list_dependencies(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
        with db_read(db_path) as conn:
            rows = conn.execute("SELECT * FROM dependencies ORDER BY created_at ASC").fetchall()
            return [{"id": r["id"], "parent": r["parent"], "child": r["child"], "created_at": r["created_at"]} for r in rows]

    @staticmethod
    def create_dependency(parent: str, child: str, db_path: Optional[str] = None) -> Dict[str, Any]:
        dep_id = f"dep_{int(time.time() * 1000)}"
        now = time.time()
        with db_transaction(db_path) as conn:
            # One parent per child
            conn.execute("DELETE FROM dependencies WHERE child = ?", (child,))
            conn.execute("INSERT INTO dependencies (id, parent, child, created_at) VALUES (?, ?, ?, ?)", (dep_id, parent, child, now))
        return {"id": dep_id, "parent": parent, "child": child, "created_at": int(now)}

    @staticmethod
    def delete_dependency(dep_id: str, db_path: Optional[str] = None) -> bool:
        with db_transaction(db_path) as conn:
            cursor = conn.execute("DELETE FROM dependencies WHERE id = ? OR child = ?", (dep_id, dep_id))
            return cursor.rowcount > 0

    @staticmethod
    def get_parent_map(db_path: Optional[str] = None) -> Dict[str, str]:
        with db_read(db_path) as conn:
            rows = conn.execute("SELECT child, parent FROM dependencies").fetchall()
            return {r["child"]: r["parent"] for r in rows}


class EndpointRepository:
    @staticmethod
    def create_endpoint(name: str, url: str, is_active: bool = False, db_path: Optional[str] = None) -> Dict[str, Any]:
        ep_id = f"ep_{int(time.time() * 1000)}"
        now = time.time()
        with db_transaction(db_path) as conn:
            existing = conn.execute("SELECT * FROM endpoints WHERE url = ?", (url,)).fetchone()
            if is_active:
                conn.execute("UPDATE endpoints SET is_active = 0")
            if existing:
                if is_active:
                    conn.execute("UPDATE endpoints SET is_active = 1 WHERE id = ?", (existing["id"],))
                return {"id": existing["id"], "name": existing["name"], "url": url, "is_active": is_active, "created_at": int(existing["created_at"])}
            conn.execute("""
                INSERT INTO endpoints (id, name, url, is_active, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (ep_id, name, url, 1 if is_active else 0, now))
        return {"id": ep_id, "name": name, "url": url, "is_active": is_active, "created_at": int(now)}

    @staticmethod
    def select_endpoint(endpoint_id_or_url: str, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with db_transaction(db_path) as conn:
            conn.execute("UPDATE endpoints SET is_active = 0")
            cursor = conn.execute("""
                UPDATE endpoints SET is_active = 1 WHERE id = ? OR url = ?
            """, (endpoint_id_or_url, endpoint_id_or_url))
            if cursor.rowcount == 0:
                # If not existing, insert as active
                ep_id = f"ep_{int(time.time() * 1000)}"
                now = time.time()
                conn.execute("""
                    INSERT INTO endpoints (id, name, url, is_active, created_at)
                    VALUES (?, ?, ?, 1, ?)
                """, (ep_id, endpoint_id_or_url, endpoint_id_or_url, now))
            row = conn.execute("SELECT * FROM endpoints WHERE is_active = 1 LIMIT 1").fetchone()
            return dict(row) if row else None

    @staticmethod
    def delete_endpoint(endpoint_id_or_url: str, db_path: Optional[str] = None) -> bool:
        with db_transaction(db_path) as conn:
            cursor = conn.execute("DELETE FROM endpoints WHERE id = ? OR url = ?", (endpoint_id_or_url, endpoint_id_or_url))
            # If deleted endpoint was active, activate another if available
            remaining = conn.execute("SELECT * FROM endpoints ORDER BY created_at ASC").fetchall()
            if remaining and not any(r["is_active"] for r in remaining):
                conn.execute("UPDATE endpoints SET is_active = 1 WHERE id = ?", (remaining[0]["id"],))
            return cursor.rowcount > 0

    @staticmethod
    def get_active_endpoint(db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with db_read(db_path) as conn:
            row = conn.execute("SELECT * FROM endpoints WHERE is_active = 1 LIMIT 1").fetchone()
            return dict(row) if row else None

    @staticmethod
    def load_endpoints_state(default_url: str, db_path: Optional[str] = None) -> Dict[str, Any]:
        with db_read(db_path) as conn:
            rows = conn.execute("SELECT * FROM endpoints ORDER BY created_at ASC").fetchall()
        if not rows:
            with db_transaction(db_path) as conn:
                ep_id = f"ep_{int(time.time() * 1000)}"
                conn.execute("""
                    INSERT OR IGNORE INTO endpoints (id, name, url, is_active, created_at)
                    VALUES (?, ?, ?, 1, ?)
                """, (ep_id, default_url, default_url, time.time()))
            return {"active": default_url, "endpoints": [default_url]}

        urls = [r["url"] for r in rows]
        active_row = next((r for r in rows if r["is_active"]), rows[0])
        return {"active": active_row["url"], "endpoints": urls}


class DeletedTargetRepository:
    @staticmethod
    def list_deleted(db_path: Optional[str] = None) -> List[str]:
        with db_read(db_path) as conn:
            rows = conn.execute("SELECT instance FROM deleted_targets").fetchall()
            return [r["instance"] for r in rows]

    @staticmethod
    def add_deleted(instance: str, db_path: Optional[str] = None):
        with db_transaction(db_path) as conn:
            conn.execute("INSERT OR REPLACE INTO deleted_targets (instance, deleted_at) VALUES (?, ?)", (instance, time.time()))

    @staticmethod
    def restore_target(instance: str, db_path: Optional[str] = None):
        with db_transaction(db_path) as conn:
            conn.execute("DELETE FROM deleted_targets WHERE instance = ?", (instance,))


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
                    sample_count, availability_pct, incident_count, avg_latency_ms, updated_at
                ) VALUES (
                    :instance, :job, :bucket_start, :bucket_end,
                    :uptime_seconds, :downtime_seconds, :unknown_seconds, :coverage_seconds,
                    :sample_count, :availability_pct, :incident_count, :avg_latency_ms, :updated_at
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
            # UNIQUE is (job, instance, bucket_start) — job is part of the key,
            # not just instance+hour. If an instance's job tag ever changes
            # between aggregation cycles (e.g. was mistagged, later corrected),
            # the same instance+hour can end up stored under two different job
            # rows. A job='all' query matches both, and summing both into the
            # merge math double-counts that hour's seconds — silently inflating
            # every downstream availability/uptime number. Dedup to the most
            # recently written row per (instance, bucket_start) before merging.
            query = """
                SELECT id, instance, job, bucket_start, bucket_end,
                       uptime_seconds, downtime_seconds, unknown_seconds, coverage_seconds,
                       sample_count, availability_pct, incident_count, avg_latency_ms, updated_at
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
            row = conn.execute(
                "SELECT COUNT(*) as c FROM availability_buckets WHERE (job = ? OR ? = 'all') AND bucket_end > ? AND bucket_start < ?",
                (job, job, start_time, end_time)
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

