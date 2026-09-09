"""Storage subsystem facade for InfraWatch.

Maintains complete backward compatibility by re-exporting connection management,
schema migration, and all 13 domain repository classes from their dedicated deep modules.
"""
from typing import Optional, List, Dict, Any

try:
    from .connection import (
        DB_DIR,
        DEFAULT_DB_PATH,
        _INITIALIZED_DBS,
        _connect_raw,
        get_db,
        db_transaction,
        db_read,
    )
    from .schema import (
        init_db,
        _maybe_import_from_json,
    )
    from .repositories import (
        IncidentRepository,
        EventLogRepository,
        AcknowledgmentRepository,
        AvailabilityBucketRepository,
        AggregationLeaseRepository,
        SlaTargetRepository,
        SlowThresholdRepository,
        MaintenanceRepository,
        DependencyRepository,
        EndpointRepository,
        DeletedTargetRepository,
        UserRepository,
        AuditLogRepository,
    )
except (ImportError, ValueError):
    from alarm.storage.connection import (
        DB_DIR,
        DEFAULT_DB_PATH,
        _INITIALIZED_DBS,
        _connect_raw,
        get_db,
        db_transaction,
        db_read,
    )
    from alarm.storage.schema import (
        init_db,
        _maybe_import_from_json,
    )
    from alarm.storage.repositories import (
        IncidentRepository,
        EventLogRepository,
        AcknowledgmentRepository,
        AvailabilityBucketRepository,
        AggregationLeaseRepository,
        SlaTargetRepository,
        SlowThresholdRepository,
        MaintenanceRepository,
        DependencyRepository,
        EndpointRepository,
        DeletedTargetRepository,
        UserRepository,
        AuditLogRepository,
    )

__all__ = [
    "DB_DIR",
    "DEFAULT_DB_PATH",
    "_INITIALIZED_DBS",
    "_connect_raw",
    "get_db",
    "db_transaction",
    "db_read",
    "init_db",
    "_maybe_import_from_json",
    "IncidentRepository",
    "EventLogRepository",
    "AcknowledgmentRepository",
    "AvailabilityBucketRepository",
    "AggregationLeaseRepository",
    "SlaTargetRepository",
    "SlowThresholdRepository",
    "MaintenanceRepository",
    "DependencyRepository",
    "EndpointRepository",
    "DeletedTargetRepository",
    "UserRepository",
    "AuditLogRepository",
]
