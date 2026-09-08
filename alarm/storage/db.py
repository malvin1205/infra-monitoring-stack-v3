import sqlite3
import os
import time
import json
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from contextlib import contextmanager

try:
    from config import DATA_DIR, ALARM_DIR
except ImportError:
    from alarm.config import DATA_DIR, ALARM_DIR

DB_DIR = DATA_DIR
DEFAULT_DB_PATH = os.environ.get("INFRAWATCH_DB_PATH", os.path.join(DB_DIR, "infrawatch.db"))

# Safe migration: if DB doesn't exist in DATA_DIR but exists in legacy ALARM_DIR, copy it over.
try:
    _legacy_db = os.path.join(ALARM_DIR, "infrawatch.db")
    if not os.path.exists(DEFAULT_DB_PATH) and os.path.exists(_legacy_db):
        import shutil
        shutil.copy2(_legacy_db, DEFAULT_DB_PATH)
except Exception:
    pass


_INITIALIZED_DBS = set()


def _connect_raw(path: str, set_wal: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # journal_mode = WAL is persisted in the database file header once set, so
    # every later connection opens in WAL automatically. Re-issuing it on every
    # connection is the most expensive of these pragmas (it can force a
    # checkpoint) and pure waste on the per-request hot path — only init_db
    # needs to establish it. synchronous / busy_timeout are per-connection and
    # cheap, so they stay here.
    if set_wal:
        conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    return conn


def get_db(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or os.environ.get("INFRAWATCH_DB_PATH", DEFAULT_DB_PATH)
    if path not in _INITIALIZED_DBS:
        init_db(path)
    return _connect_raw(path)


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
    path = db_path or os.environ.get("INFRAWATCH_DB_PATH", DEFAULT_DB_PATH)
    conn = _connect_raw(path, set_wal=True)
    try:
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
                updated_at REAL NOT NULL,
                occurrences INTEGER NOT NULL DEFAULT 1,
                first_seen REAL,
                http_status_code INTEGER,
                last_error TEXT,
                total_down_seconds REAL NOT NULL DEFAULT 0,
                acknowledged_by TEXT,
                acknowledged_at REAL
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

            CREATE TABLE IF NOT EXISTS sla_targets (
                instance TEXT PRIMARY KEY,
                target_pct REAL NOT NULL,
                updated_by TEXT,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS slow_thresholds (
                instance TEXT PRIMARY KEY,
                threshold_ms REAL NOT NULL,
                updated_by TEXT,
                updated_at REAL NOT NULL
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

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                display_name TEXT,
                role TEXT NOT NULL DEFAULT 'viewer', -- 'owner', 'admin' or 'viewer'
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_login REAL,
                session_epoch INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
            CREATE INDEX IF NOT EXISTS idx_users_active ON users(is_active);

            CREATE TABLE IF NOT EXISTS alert_acknowledgments (
                target_key TEXT PRIMARY KEY, -- instance or alert fingerprint
                instance TEXT NOT NULL,
                acknowledged_by TEXT NOT NULL,
                acknowledged_at REAL NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_ack_instance ON alert_acknowledgments(instance);

            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_username TEXT NOT NULL,
                actor_role TEXT NOT NULL,
                action TEXT NOT NULL,
                resource TEXT,
                details TEXT,
                created_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_logs(created_at DESC);
        """)
        # Sanitize any legacy corrupted buckets (e.g. down hosts marked with positive uptime)
        conn.execute("DELETE FROM availability_buckets WHERE uptime_seconds > 0 AND availability_pct = 0.0")
        conn.execute("DELETE FROM availability_buckets WHERE coverage_seconds = 0.0 AND (uptime_seconds > 0 OR downtime_seconds > 0)")

        # outage_json (added in the "one engine" pass): per-hour outage durations
        # + ongoing-at-hour-end flag, written by the reconstruction-based
        # aggregator so incident counts can later be de-duplicated across the
        # hour boundary. Nullable — legacy rows and the approximate fallback
        # path simply leave it NULL.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(availability_buckets)").fetchall()}
        if "outage_json" not in cols:
            conn.execute("ALTER TABLE availability_buckets ADD COLUMN outage_json TEXT")

        # occurrences/first_seen/http_status_code/last_error (Incident History
        # revamp): older DBs pre-date these columns. occurrences/first_seen
        # backfill from what's already known (1 occurrence, first_seen ==
        # the row's current started_at) — real re-fire counts only start
        # accumulating from here on, which is honest: earlier flaps were
        # never counted anywhere, there's nothing truer to backfill.
        inc_cols = {r[1] for r in conn.execute("PRAGMA table_info(incidents)").fetchall()}
        if "occurrences" not in inc_cols:
            conn.execute("ALTER TABLE incidents ADD COLUMN occurrences INTEGER NOT NULL DEFAULT 1")
        if "first_seen" not in inc_cols:
            conn.execute("ALTER TABLE incidents ADD COLUMN first_seen REAL")
            conn.execute("UPDATE incidents SET first_seen = started_at WHERE first_seen IS NULL")
        if "http_status_code" not in inc_cols:
            conn.execute("ALTER TABLE incidents ADD COLUMN http_status_code INTEGER")
        if "last_error" not in inc_cols:
            conn.execute("ALTER TABLE incidents ADD COLUMN last_error TEXT")
        # total_down_seconds: cumulative downtime across ALL occurrences of
        # this key, not just the latest one — "Total Down" is documented as
        # an aggregate duration (Incident History task #3). duration_seconds
        # alone gets overwritten by started_at on every re-fire, so a 3x-flap
        # incident's column was silently only showing its LAST occurrence's
        # duration. Backfill from duration_seconds (best available truth for
        # pre-migration rows — their earlier occurrences' individual
        # durations were never retained anywhere either).
        if "total_down_seconds" not in inc_cols:
            conn.execute("ALTER TABLE incidents ADD COLUMN total_down_seconds REAL NOT NULL DEFAULT 0")
            conn.execute("UPDATE incidents SET total_down_seconds = COALESCE(duration_seconds, 0) WHERE status = 'resolved'")
        # acknowledged_by/acknowledged_at: alert_acknowledgments (keyed by
        # instance, live-only) gets deleted the moment a target stops being
        # down (see AcknowledgmentRepository.clear_resolved), so it can never
        # answer "who acknowledged THIS past incident" once it's resolved.
        # These columns are the durable copy — kept in sync in real time by
        # AcknowledgmentRepository.acknowledge_instances/unacknowledge_instance
        # while an incident is firing, and simply left alone (frozen) once it
        # resolves.
        if "acknowledged_by" not in inc_cols:
            conn.execute("ALTER TABLE incidents ADD COLUMN acknowledged_by TEXT")
        if "acknowledged_at" not in inc_cols:
            conn.execute("ALTER TABLE incidents ADD COLUMN acknowledged_at REAL")

        # session_epoch: bumped on password change so signed-cookie sessions
        # (which have no server-side store) held on other devices stop
        # authenticating. Pre-existing DBs get it at 0.
        user_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "session_epoch" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN session_epoch INTEGER NOT NULL DEFAULT 0")

        # 'owner' role: the founding account (first-boot setup) is now created
        # as 'owner', not 'admin'. Deployments that set up before this role
        # existed have an all-'admin' users table and no owner — promote the
        # lowest-id account (the one first-run setup created) so every install
        # has exactly one owner. Runs only while no owner exists.
        if conn.execute("SELECT COUNT(*) FROM users WHERE role = 'owner'").fetchone()[0] == 0:
            first = conn.execute("SELECT id FROM users ORDER BY id ASC LIMIT 1").fetchone()
            if first:
                conn.execute(
                    "UPDATE users SET role = 'owner', updated_at = ? WHERE id = ?",
                    (time.time(), first[0]),
                )
        conn.commit()

        # Seed from JSON files only if using the default production DB and table is empty
        if db_path is None:
            _maybe_import_from_json(conn)
        _INITIALIZED_DBS.add(path)
    finally:
        conn.close()


def _maybe_import_from_json(conn: sqlite3.Connection):
    try:
        def _empty(table):
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0

        # Each table is gated on its own row count, not a single shared check —
        # e.g. incidents already having rows (from webhook activity before the
        # legacy JSON files were copied in) must not skip importing endpoints/
        # maintenance/dependencies too.
        if _empty("incidents"):
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

        if _empty("event_logs"):
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

        if _empty("endpoints"):
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

        if _empty("maintenance_windows"):
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

        if _empty("dependencies"):
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
        # Includes BOTH resolved and still-firing incidents — Incident History
        # shows an "Ongoing" incident (not yet resolved) instead of hiding it
        # until it clears.
        #
        # Firing rows are fetched WITHOUT a limit and resolved rows WITH one,
        # then combined — a single "ORDER BY updated_at DESC LIMIT N" query
        # could silently push a long-running-but-quiet ongoing incident (its
        # updated_at only moves on a re-fire) out of the window once N other,
        # newer, unrelated incidents resolve — Ongoing rows must never be
        # allowed to just disappear from the list. Concurrent open incidents
        # in any real deployment are always a small number, so leaving them
        # unlimited is safe.
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

            # An "occurrence" is one full DOWN->UP->DOWN cycle for this key —
            # exactly the transition this dedupe guard already enforces (no
            # new occurrence without an intervening resolve). A fresh key
            # starts at 1; re-firing a key that already exists (guaranteed
            # 'resolved' by the guard above) increments the same row instead
            # of inserting a new one, so Incident History reflects real
            # flap counts instead of always reading "occurrences x1".
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
                # total_down_seconds accumulates across every occurrence of
                # this key — duration_seconds alone only ever reflects the
                # occurrence that JUST resolved, since started_at gets
                # overwritten on every re-fire (see the firing branch above).
                conn.execute("""
                    UPDATE incidents SET status = 'resolved', resolved_at = ?, duration_seconds = ?,
                        total_down_seconds = COALESCE(total_down_seconds, 0) + ?, updated_at = ?
                    WHERE key = ?
                """, (event_time, duration_seconds, duration_seconds, event_time, key))

                conn.execute("""
                    INSERT INTO event_logs (event, name, severity, instance, summary, job, time, duration_seconds, latency_ms, fingerprint)
                    VALUES ('resolved', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (name, severity, instance, summary, job, event_time, duration_seconds, latency_ms, key))

                # alert_acknowledgments is keyed by instance (live-only "is
                # THIS host's current trouble acked" state), not by incident
                # key — a host can have more than one alert type firing at
                # once (e.g. TargetDown + an unrelated Alertmanager rule).
                # Only clear the live ack once NOTHING is left firing for
                # this instance (the row above is already 'resolved' by now,
                # so it's naturally excluded here) — otherwise resolving one
                # alert would wrongly un-acknowledge another, still-firing,
                # already-handled one on the same host. This also closes the
                # original race this replaced: previously the ack was only
                # cleared by a poll-driven sweep (app.py's /instances
                # handler, AcknowledgmentRepository.clear_resolved) that
                # might not run before a resolve -> re-fire happens (or not
                # run at all, e.g. an unattended wallboard) — clearing it
                # here, immediately, means a genuinely new occurrence can
                # never inherit a stale ack from the one that just resolved.
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
        # start_iso/end_iso are a human-readable copy of the epoch columns —
        # write real ISO 8601 UTC, not str(<float>) which produced "1725620000.0".
        start_iso = datetime.fromtimestamp(float(start), tz=timezone.utc).isoformat()
        end_iso = datetime.fromtimestamp(float(end), tz=timezone.utc).isoformat()
        with db_transaction(db_path) as conn:
            conn.execute("""
                INSERT INTO maintenance_windows (id, scope, target, reason, start_epoch, end_epoch, start_iso, end_iso, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (win_id, scope, target, reason, start, end, start_iso, end_iso, now))
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
            # Seed from default_url only when the operator actually configured
            # one (PROMETHEUS_URL). A blank default means "no endpoint" — leave
            # the table empty instead of resurrecting a bogus seed every boot.
            if default_url:
                with db_transaction(db_path) as conn:
                    ep_id = f"ep_{int(time.time() * 1000)}"
                    conn.execute("""
                        INSERT OR IGNORE INTO endpoints (id, name, url, is_active, created_at)
                        VALUES (?, ?, ?, 1, ?)
                    """, (ep_id, default_url, default_url, time.time()))
                return {"active": default_url, "endpoints": [default_url]}
            return {"active": None, "endpoints": []}

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
                # UNIQUE is (job, instance, bucket_start) -- if an instance's
                # job tag changed between aggregation cycles, the same
                # instance+hour can exist under two job rows. A plain
                # COUNT(*) double-counts that hour for job='all', same issue
                # get_bucket_records() already dedups for. Count distinct
                # (instance, bucket_start) pairs instead.
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


class UserRepository:
    @staticmethod
    def _sanitize(row: sqlite3.Row, include_password_hash: bool = False) -> Dict[str, Any]:
        d = dict(row)
        if not include_password_hash:
            d.pop("password_hash", None)
        return d

    @staticmethod
    def count_users(db_path: Optional[str] = None) -> int:
        with db_read(db_path) as conn:
            row = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()
            return int(row["c"]) if row else 0

    @staticmethod
    def create_first_admin(
        username: str,
        password_hash: str,
        display_name: str = "",
        db_path: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Create the founding account. It gets the permanent 'owner' role —
        full access, and the only account an admin cannot touch or recreate."""
        now = time.time()
        username = username.strip()
        display_name = display_name.strip() if display_name else username
        with db_transaction(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
            if count > 0:
                return None  # Race condition protection: owner already created
            conn.execute("""
                INSERT INTO users (username, password_hash, display_name, role, is_active, created_at, updated_at)
                VALUES (?, ?, ?, 'owner', 1, ?, ?)
            """, (username, password_hash, display_name, now, now))
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            return UserRepository._sanitize(row, include_password_hash=False) if row else None

    @staticmethod
    def create_user(
        username: str,
        password_hash: str,
        role: str = "viewer",
        display_name: str = "",
        is_active: int = 1,
        db_path: Optional[str] = None
    ) -> Dict[str, Any]:
        now = time.time()
        username = username.strip()
        role = role.strip().lower()
        if role not in ("admin", "viewer"):
            role = "viewer"
        display_name = display_name.strip() if display_name else username
        with db_transaction(db_path) as conn:
            conn.execute("""
                INSERT INTO users (username, password_hash, display_name, role, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (username, password_hash, display_name, role, 1 if is_active else 0, now, now))
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            return UserRepository._sanitize(row, include_password_hash=False)

    @staticmethod
    def get_by_username(
        username: str,
        include_password_hash: bool = False,
        db_path: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        with db_read(db_path) as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
            return UserRepository._sanitize(row, include_password_hash=include_password_hash) if row else None

    @staticmethod
    def get_by_id(
        user_id: int,
        include_password_hash: bool = False,
        db_path: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        with db_read(db_path) as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return UserRepository._sanitize(row, include_password_hash=include_password_hash) if row else None

    @staticmethod
    def list_users(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
        with db_read(db_path) as conn:
            rows = conn.execute("SELECT id, username, display_name, role, is_active, created_at, updated_at, last_login FROM users ORDER BY id ASC").fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def update_last_login(user_id: int, now: Optional[float] = None, db_path: Optional[str] = None):
        t = now if now is not None else time.time()
        with db_transaction(db_path) as conn:
            conn.execute("UPDATE users SET last_login = ?, updated_at = ? WHERE id = ?", (t, t, user_id))

    @staticmethod
    def update_user(
        user_id: int,
        role: Optional[str] = None,
        is_active: Optional[int] = None,
        display_name: Optional[str] = None,
        password_hash: Optional[str] = None,
        db_path: Optional[str] = None
    ) -> bool:
        fields = []
        params = []
        now = time.time()
        if role is not None and role in ("admin", "viewer"):
            fields.append("role = ?")
            params.append(role)
        if is_active is not None:
            fields.append("is_active = ?")
            params.append(1 if is_active else 0)
        if display_name is not None:
            fields.append("display_name = ?")
            params.append(display_name.strip())
        if password_hash is not None:
            fields.append("password_hash = ?")
            params.append(password_hash)
            # Invalidate this user's other signed-cookie sessions on a
            # password change (checked in auth.get_current_authenticated_user).
            fields.append("session_epoch = session_epoch + 1")
        if not fields:
            return False
        fields.append("updated_at = ?")
        params.append(now)
        params.append(user_id)
        with db_transaction(db_path) as conn:
            cur = conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", params)
            return cur.rowcount > 0


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
                # Mirror onto the currently-firing incident row(s) for this
                # instance too — alert_acknowledgments is live-only state
                # (deleted the moment the instance recovers, see
                # clear_resolved below), so without this an incident that
                # gets acknowledged and then resolves would show no trace of
                # who acknowledged it once it lands in Incident History.
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


class AuditLogRepository:
    @staticmethod
    def record_action(
        actor_username: str,
        actor_role: str,
        action: str,
        resource: str = "",
        details: str = "",
        db_path: Optional[str] = None
    ) -> Dict[str, Any]:
        now = time.time()
        with db_transaction(db_path) as conn:
            conn.execute("""
                INSERT INTO audit_logs (actor_username, actor_role, action, resource, details, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (actor_username, actor_role, action, resource, details, now))
            # Retention limit: keep latest 5000 audit logs
            conn.execute("""
                DELETE FROM audit_logs WHERE id NOT IN (
                    SELECT id FROM audit_logs ORDER BY created_at DESC, id DESC LIMIT 5000
                )
            """)
        return {
            "actor_username": actor_username,
            "actor_role": actor_role,
            "action": action,
            "resource": resource,
            "details": details,
            "created_at": now
        }

    @staticmethod
    def get_recent_logs(limit: int = 100, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
        limit = max(1, min(limit, 500))
        with db_read(db_path) as conn:
            rows = conn.execute("""
                SELECT id, actor_username, actor_role, action, resource, details, created_at
                FROM audit_logs ORDER BY created_at DESC, id DESC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]


