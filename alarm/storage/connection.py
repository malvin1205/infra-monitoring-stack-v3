"""Database connection and transaction management for InfraWatch.
"""
import os
import sqlite3
from contextlib import contextmanager
from typing import Optional, Set

try:
    from config import DATA_DIR, ALARM_DIR
except (ImportError, ValueError):
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

_INITIALIZED_DBS: Set[str] = set()


def _connect_raw(path: str, set_wal: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    if set_wal:
        conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    return conn


def get_db(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or os.environ.get("INFRAWATCH_DB_PATH", DEFAULT_DB_PATH)
    if path not in _INITIALIZED_DBS:
        try:
            from .schema import init_db
        except (ImportError, ValueError):
            from alarm.storage.schema import init_db
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
