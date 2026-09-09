"""Target state storage: deleted-target tombstones.

Deleted-target state lives in SQLite (DeletedTargetRepository).
Target discovery is driven dynamically by Prometheus (/api/v1/targets).
"""
import logging

try:
    from .db import DeletedTargetRepository
except (ImportError, ValueError):
    try:
        from storage.db import DeletedTargetRepository
    except ImportError:
        from alarm.storage.db import DeletedTargetRepository

logger = logging.getLogger("infrawatch")


def load_deleted_targets():
    """Returns list of tombstoned instances from SQLite."""
    try:
        return DeletedTargetRepository.list_deleted()
    except Exception:
        return []


def save_deleted_targets(deleted_list):
    """Diffs deleted_list against SQLite's current state and applies add/restore."""
    try:
        current_deleted = set(DeletedTargetRepository.list_deleted())
    except Exception:
        current_deleted = set()
    for d in deleted_list:
        if d not in current_deleted:
            DeletedTargetRepository.add_deleted(d)
    for d in current_deleted:
        if d not in deleted_list:
            DeletedTargetRepository.restore_target(d)
