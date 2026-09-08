"""Alerts & Incidents domain: active incidents, correlation suppression, and Telegram notifications.
"""
from .engine import (
    _WEBHOOK_LOCK, _LAST_WEBHOOK_AT, active_incident_list,
    _request_memo, _invalidate_maint_cache, _invalidate_dep_cache,
    load_maintenance_windows, maintenance_windows_by_instance,
    get_active_maintenance, record_alert_event,
    load_dependencies, apply_correlation_suppression,
)
from .telegram import (
    get_telegram_config, save_telegram_config, test_telegram_connection,
    dispatch_alert_async,
)
