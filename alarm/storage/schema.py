"""Database schema initialization, migrations, and legacy seeding for InfraWatch.
"""
import os
import time
import json
import sqlite3
from typing import Optional

try:
    from .connection import _connect_raw, _INITIALIZED_DBS, DB_DIR, DEFAULT_DB_PATH
except (ImportError, ValueError):
    from alarm.storage.connection import _connect_raw, _INITIALIZED_DBS, DB_DIR, DEFAULT_DB_PATH


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
