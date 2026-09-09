"""Storage subsystem: SQLite connection management, schema migrations, domain repositories, and JSON stores.
"""
from .connection import (
    DB_DIR,
    DEFAULT_DB_PATH,
    get_db,
    db_transaction,
    db_read,
)
from .schema import (
    init_db,
)
from .repositories import (
    IncidentRepository,
    EventLogRepository,
    MaintenanceRepository,
    DependencyRepository,
    EndpointRepository,
    DeletedTargetRepository,
    AvailabilityBucketRepository,
    AggregationLeaseRepository,
    SlaTargetRepository,
    SlowThresholdRepository,
    UserRepository,
    AcknowledgmentRepository,
    AuditLogRepository,
)
from .json_store import (
    load_json,
    save_json,
    save_with_retention,
    STATUS_FILE,
    HISTORY_FILE,
    HISTORY_ARCHIVE_FILE,
    LOGS_FILE,
    MAX_HISTORY,
    MAX_LOGS,
    MAX_ARCHIVE_HISTORY,
)
from .targets_store import (
    load_deleted_targets,
    save_deleted_targets,
)

__all__ = [
    "DB_DIR",
    "DEFAULT_DB_PATH",
    "get_db",
    "db_transaction",
    "db_read",
    "init_db",
    "IncidentRepository",
    "EventLogRepository",
    "MaintenanceRepository",
    "DependencyRepository",
    "EndpointRepository",
    "DeletedTargetRepository",
    "AvailabilityBucketRepository",
    "AggregationLeaseRepository",
    "SlaTargetRepository",
    "SlowThresholdRepository",
    "UserRepository",
    "AcknowledgmentRepository",
    "AuditLogRepository",
    "load_json",
    "save_json",
    "save_with_retention",
    "STATUS_FILE",
    "HISTORY_FILE",
    "HISTORY_ARCHIVE_FILE",
    "LOGS_FILE",
    "MAX_HISTORY",
    "MAX_LOGS",
    "MAX_ARCHIVE_HISTORY",
    "load_deleted_targets",
    "save_deleted_targets",
]
