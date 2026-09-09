"""User authentication, sessions, and audit logging repositories for InfraWatch.
"""
import time
import sqlite3
from typing import List, Dict, Any, Optional

try:
    from ..connection import db_read, db_transaction
except (ImportError, ValueError):
    from alarm.storage.connection import db_read, db_transaction


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
            fields.append("session_epoch = session_epoch + 1")
        if not fields:
            return False
        fields.append("updated_at = ?")
        params.append(now)
        params.append(user_id)
        with db_transaction(db_path) as conn:
            cur = conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", params)
            return cur.rowcount > 0


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
