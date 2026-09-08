"""JSON-file persistence for InfraWatch's denormalized caches.

status.json / logs.json / history.json / history_archive.json are a
write-through cache; SQLite (incidents / event_logs) is the source of truth
for active-alert state and for /history & /logs (audit F1). This module owns
the file paths, the retention limits, and the atomic load/save helpers.

Leaf module — no InfraWatch imports. Tests point the cache at a temp dir by
rebinding the path attributes here (`json_store.STATUS_FILE = ...`), so every
reader must reference them as `json_store.<NAME>`, never `from json_store
import STATUS_FILE`.
"""
import json
import logging
import os
import threading
import time

logger = logging.getLogger("infrawatch")

try:
    from config import DATA_DIR, ALARM_DIR
except ImportError:
    from alarm.config import DATA_DIR, ALARM_DIR

_DIR = DATA_DIR
STATUS_FILE = os.path.join(_DIR, "status.json")
HISTORY_FILE = os.path.join(_DIR, "history.json")
HISTORY_ARCHIVE_FILE = os.path.join(_DIR, "history_archive.json")
LOGS_FILE = os.path.join(_DIR, "logs.json")

# Safe migration: if JSON caches exist in legacy ALARM_DIR and not in DATA_DIR, copy them over.
try:
    for _fn in ("status.json", "history.json", "history_archive.json", "logs.json"):
        _legacy = os.path.join(ALARM_DIR, _fn)
        _target = os.path.join(DATA_DIR, _fn)
        if os.path.exists(_legacy) and not os.path.exists(_target):
            import shutil
            shutil.copy2(_legacy, _target)
except Exception:
    pass

MAX_HISTORY = 1000
MAX_LOGS = 200
MAX_ARCHIVE_HISTORY = 5000


def load_json(path, default=None):
    if default is None:
        default = {}
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(path, data):
    try:
        tmp_path = path + f".tmp.{os.getpid()}.{threading.get_ident()}"
        os.makedirs(os.path.dirname(os.path.abspath(tmp_path)), exist_ok=True)
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        for attempt in range(5):
            try:
                os.replace(tmp_path, path)
                break
            except (OSError, PermissionError):
                if attempt == 4:
                    try:
                        with open(path, 'w', encoding='utf-8') as f:
                            json.dump(data, f, indent=2)
                    except Exception:
                        pass
                    try:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)
                    except Exception:
                        pass
                else:
                    time.sleep(0.01 * (attempt + 1))
    except Exception as e:
        logger.error(f"Error saving {path}: {e}")


def save_with_retention(main_path, archive_path, data, limit):
    if len(data) > limit:
        overflow = data[limit:]
        archive = load_json(archive_path, [])
        archive = (overflow + archive)[:MAX_ARCHIVE_HISTORY]
        save_json(archive_path, archive)
        data = data[:limit]
    save_json(main_path, data)
