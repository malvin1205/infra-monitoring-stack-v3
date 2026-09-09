"""Inventory and infrastructure topology repositories for InfraWatch.
"""
import time
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

try:
    from ..connection import db_read, db_transaction
except (ImportError, ValueError):
    from alarm.storage.connection import db_read, db_transaction


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
