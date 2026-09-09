from flask import Flask, request, jsonify, render_template, session, g
import time
import os
import math
import re
import sys
import uuid
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("infrawatch")

_this_dir = os.path.dirname(os.path.abspath(__file__))
_parent_dir = os.path.dirname(_this_dir)
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

# ── Structured Domain Imports (Modular Architecture) ──────────────────────────
try:
    from config import (
        DATA_DIR, ALARM_DIR,
        DEFAULT_JOB_FILTER, ALERTNAME_TARGET_DOWN, ALERTNAME_SLOW_RESPONSE,
        DEFAULT_SLOW_RESPONSE_THRESHOLD_MS, SCRAPE_INTERVAL_SECONDS,
        _AVAIL_FRESHNESS_TOLERANCE_SEC, _AVAIL_STALE_BUCKET_TOLERANCE_SEC,
    )
    import storage
    from storage import (
        init_db, IncidentRepository, EventLogRepository,
        MaintenanceRepository, DependencyRepository, EndpointRepository, DeletedTargetRepository,
        AvailabilityBucketRepository, AggregationLeaseRepository, SlaTargetRepository, SlowThresholdRepository,
        UserRepository, AcknowledgmentRepository, AuditLogRepository,
        load_json, save_json,
        load_deleted_targets, save_deleted_targets,
    )
    from storage import json_store
    import web
    from web import (
        rate_limit, _client_identity, _RATE_BUCKETS, _RATE_BUCKETS_LOCK,
        _RATE_LAST_PRUNE, _RATE_PRUNE_INTERVAL, _LOGIN_FAILS, _LOGIN_FAILS_LOCK,
        _LOGIN_MAX_FAILS, _LOGIN_LOCK_SECONDS, _login_locked, _login_note_failure,
        _login_clear, register_web_middleware, _is_blocked_ip, is_safe_endpoint_url,
    )
    from web import ssrf
    import core.auth as auth
    from core.auth import (
        API_KEY, require_api_key, require_webhook_secret, get_api_key, get_webhook_secret,
        get_session_secret, hash_password, verify_password, get_current_authenticated_user,
        has_permission, require_permission, require_auth, require_admin, ROLE_PERMISSIONS
    )
    import core.alerts.telegram as telegram_notifier
    from core.alerts.telegram import (
        get_telegram_config, save_telegram_config, test_telegram_connection
    )
    import core.alerts.engine as alerts
    from core.alerts.engine import (
        _WEBHOOK_LOCK, _LAST_WEBHOOK_AT, active_incident_list,
        _request_memo, _invalidate_maint_cache, _invalidate_dep_cache,
        load_maintenance_windows, maintenance_windows_by_instance,
        get_active_maintenance, record_alert_event,
        load_dependencies, apply_correlation_suppression,
    )
    import core.availability as availability_domain
    from core.availability import (
        availability_engine, AvailabilityQuery, AvailabilityReport,
        summarize_entries, reconstruct_time_series_intervals, calculate_percentile,
        clip_hourly_bucket, merge_hybrid_target_availability, merge_hybrid_fleet_availability,
        derive_bucket_inputs, estimate_instance_cadence, sla_budget, get_sla_target_pct,
        get_availability_settings, save_availability_settings, classify_probe_failure,
    )
    import core.availability.fleet as fleet_availability
    import core.availability.helpers as availability
    from core.availability.helpers import (
        _attach_sla_budgets, _availability_status_counts, _build_fleet_trend,
        _FLEET_TREND_CACHE, _FLEET_TREND_CACHE_TTL,
    )
    import core.monitoring.primitives as monitoring_primitives
    from core.monitoring.primitives import (
        parse_alert_timestamp, alert_key, TARGET_HOST_RE, _BLOCKED_TARGET_HOSTS,
        is_valid_target, matches_job_filter, normalize_target, _parse_epoch_ts,
        _sane_epoch, _earliest_outage_start, classify_scrape_failure,
        _extract_host, find_node_exporter_status, OUTAGE_GRACE_SECONDS,
        _outage_past_grace, _PROM_DURATION_RE, _parse_prom_duration_sec,
    )
    import core.monitoring.client as promclient
    from core.monitoring.client import (
        _DEFAULT_PROM_URL, load_endpoints, _ENDPOINTS_CACHE,
        LAST_WORKING_PROMETHEUS_URL, PROMETHEUS_CACHE, PROMETHEUS_CACHE_LOCK,
        PROMETHEUS_CACHE_TTL_DEFAULT, _SHARED_EXECUTOR,
        _FAILED_CANDIDATES, _FAILED_CANDIDATES_LOCK, _FETCH_LOCKS,
        _AVAILABILITY_CACHE, _AVAILABILITY_CACHE_LOCK,
        _AVAILABILITY_FLIGHT_LOCKS, _AVAILABILITY_FLIGHT_LOCKS_GUARD,
        _fetch_lock_for, _avail_flight_lock_for, clear_availability_cache,
        _maybe_prune_cache, _SAFE_CANDIDATE_CACHE,
        _cached_is_safe_endpoint_url, _filter_safe_candidates,
        fetch_url, fetch_prometheus_json,
    )
    import core.monitoring.queries as prom_queries
    from core.monitoring.queries import (
        fetch_prom_query_map, fetch_prom_range_map,
        fetch_down_since_prom_map, fetch_all_probe_metrics,
    )
    import core.monitoring.state as monitoring_state
    from core.monitoring import (
        FleetQuery, FleetSummary, FleetState, FleetStateEngine, fleet_state_engine,
        _derive_probe_readings, build_canonical_monitoring_state,
        get_instance_job_map, get_instance_cadence_map, get_monitored_instances,
    )
    import core.workers as background_workers
    from core.workers import (
        TargetPoller, AvailabilityAggregator, target_poller, availability_aggregator,
        ALERT_POLL_INTERVAL_SECONDS, WEBHOOK_ACTIVE_WINDOW_SECONDS, SLOW_RESPONSE_DEBOUNCE_N,
        AVAIL_AGGREGATE_INTERVAL_SECONDS, AVAIL_BUCKET_RETENTION_SECONDS,
        _AVAIL_AGGREGATOR_WORKER_ID, _LAST_POLLER_TICK, _LAST_AGGREGATOR_TICK,
        _poller_state, _slow_poller_state, _maintenance_active_prev,
        compute_state_transitions, compute_slow_response_transitions,
        _availability_aggregation_windows, _seed_poller_state, _reconcile_orphaned_alerts,
        _poll_targets_once, _aggregate_availability_cycle,
        _reconcile_status_json_into_sqlite, start_alert_poller, start_availability_aggregator,
    )
except ImportError:
    from alarm.config import (
        DATA_DIR, ALARM_DIR,
        DEFAULT_JOB_FILTER, ALERTNAME_TARGET_DOWN, ALERTNAME_SLOW_RESPONSE,
        DEFAULT_SLOW_RESPONSE_THRESHOLD_MS, SCRAPE_INTERVAL_SECONDS,
        _AVAIL_FRESHNESS_TOLERANCE_SEC, _AVAIL_STALE_BUCKET_TOLERANCE_SEC,
    )
    import alarm.storage as storage
    from alarm.storage import (
        init_db, IncidentRepository, EventLogRepository,
        MaintenanceRepository, DependencyRepository, EndpointRepository, DeletedTargetRepository,
        AvailabilityBucketRepository, AggregationLeaseRepository, SlaTargetRepository, SlowThresholdRepository,
        UserRepository, AcknowledgmentRepository, AuditLogRepository,
        load_json, save_json,
        load_deleted_targets, save_deleted_targets,
    )
    from alarm.storage import json_store
    import alarm.web as web
    from alarm.web import (
        rate_limit, _client_identity, _RATE_BUCKETS, _RATE_BUCKETS_LOCK,
        _RATE_LAST_PRUNE, _RATE_PRUNE_INTERVAL, _LOGIN_FAILS, _LOGIN_FAILS_LOCK,
        _LOGIN_MAX_FAILS, _LOGIN_LOCK_SECONDS, _login_locked, _login_note_failure,
        _login_clear, register_web_middleware, _is_blocked_ip, is_safe_endpoint_url,
    )
    from alarm.web import ssrf
    import alarm.core.auth as auth
    from alarm.core.auth import (
        API_KEY, require_api_key, require_webhook_secret, get_api_key, get_webhook_secret,
        get_session_secret, hash_password, verify_password, get_current_authenticated_user,
        has_permission, require_permission, require_auth, require_admin, ROLE_PERMISSIONS
    )
    import alarm.core.alerts.telegram as telegram_notifier
    from alarm.core.alerts.telegram import (
        get_telegram_config, save_telegram_config, test_telegram_connection
    )
    import alarm.core.alerts.engine as alerts
    from alarm.core.alerts.engine import (
        _WEBHOOK_LOCK, _LAST_WEBHOOK_AT, active_incident_list,
        _request_memo, _invalidate_maint_cache, _invalidate_dep_cache,
        load_maintenance_windows, maintenance_windows_by_instance,
        get_active_maintenance, record_alert_event,
        load_dependencies, apply_correlation_suppression,
    )
    import alarm.core.availability as availability_domain
    from alarm.core.availability import (
        availability_engine, AvailabilityQuery, AvailabilityReport,
        summarize_entries, reconstruct_time_series_intervals, calculate_percentile,
        clip_hourly_bucket, merge_hybrid_target_availability, merge_hybrid_fleet_availability,
        derive_bucket_inputs, estimate_instance_cadence, sla_budget, get_sla_target_pct,
        get_availability_settings, save_availability_settings, classify_probe_failure,
    )
    import alarm.core.availability.fleet as fleet_availability
    import alarm.core.availability.helpers as availability
    from alarm.core.availability.helpers import (
        _attach_sla_budgets, _availability_status_counts, _build_fleet_trend,
        _FLEET_TREND_CACHE, _FLEET_TREND_CACHE_TTL,
    )
    import alarm.core.monitoring.primitives as monitoring_primitives
    from alarm.core.monitoring.primitives import (
        parse_alert_timestamp, alert_key, TARGET_HOST_RE, _BLOCKED_TARGET_HOSTS,
        is_valid_target, matches_job_filter, normalize_target, _parse_epoch_ts,
        _sane_epoch, _earliest_outage_start, classify_scrape_failure,
        _extract_host, find_node_exporter_status, OUTAGE_GRACE_SECONDS,
        _outage_past_grace, _PROM_DURATION_RE, _parse_prom_duration_sec,
    )
    import alarm.core.monitoring.client as promclient
    from alarm.core.monitoring.client import (
        _DEFAULT_PROM_URL, load_endpoints, _ENDPOINTS_CACHE,
        LAST_WORKING_PROMETHEUS_URL, PROMETHEUS_CACHE, PROMETHEUS_CACHE_LOCK,
        PROMETHEUS_CACHE_TTL_DEFAULT, _SHARED_EXECUTOR,
        _FAILED_CANDIDATES, _FAILED_CANDIDATES_LOCK, _FETCH_LOCKS,
        _AVAILABILITY_CACHE, _AVAILABILITY_CACHE_LOCK,
        _AVAILABILITY_FLIGHT_LOCKS, _AVAILABILITY_FLIGHT_LOCKS_GUARD,
        _fetch_lock_for, _avail_flight_lock_for, clear_availability_cache,
        _maybe_prune_cache, _SAFE_CANDIDATE_CACHE,
        _cached_is_safe_endpoint_url, _filter_safe_candidates,
        fetch_url, fetch_prometheus_json,
    )
    import alarm.core.monitoring.queries as prom_queries
    from alarm.core.monitoring.queries import (
        fetch_prom_query_map, fetch_prom_range_map,
        fetch_down_since_prom_map, fetch_all_probe_metrics,
    )
    import alarm.core.monitoring.state as monitoring_state
    from alarm.core.monitoring import (
        FleetQuery, FleetSummary, FleetState, FleetStateEngine, fleet_state_engine,
        _derive_probe_readings, build_canonical_monitoring_state,
        get_instance_job_map, get_instance_cadence_map, get_monitored_instances,
    )
    import alarm.core.workers as background_workers
    from alarm.core.workers import (
        TargetPoller, AvailabilityAggregator, target_poller, availability_aggregator,
        ALERT_POLL_INTERVAL_SECONDS, WEBHOOK_ACTIVE_WINDOW_SECONDS, SLOW_RESPONSE_DEBOUNCE_N,
        AVAIL_AGGREGATE_INTERVAL_SECONDS, AVAIL_BUCKET_RETENTION_SECONDS,
        _AVAIL_AGGREGATOR_WORKER_ID, _LAST_POLLER_TICK, _LAST_AGGREGATOR_TICK,
        _poller_state, _slow_poller_state, _maintenance_active_prev,
        compute_state_transitions, compute_slow_response_transitions,
        _availability_aggregation_windows, _seed_poller_state, _reconcile_orphaned_alerts,
        _poll_targets_once, _aggregate_availability_cycle,
        _reconcile_status_json_into_sqlite, start_alert_poller, start_availability_aggregator,
    )

init_db()

# SCRAPE_INTERVAL_SECONDS, _AVAIL_FRESHNESS_TOLERANCE_SEC,
# _AVAIL_STALE_BUCKET_TOLERANCE_SEC and the job/alert-name constants live in
# config.py (re-imported above).

app = Flask(__name__)
app.secret_key = get_session_secret()
# Behind a reverse proxy, trust X-Forwarded-For/-Proto ONLY when explicitly
# told to (value = number of trusted proxy hops, usually "1"). Without this the
# per-IP login rate-limit and _client_identity() bucket every request under the
# proxy's address; with it they see the real client. Off by default so a direct
# client can't spoof the headers.
_trust_proxy_hops = int(os.environ.get("INFRAWATCH_TRUST_PROXY", "0"))
if _trust_proxy_hops > 0:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=_trust_proxy_hops, x_proto=_trust_proxy_hops)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_NAME'] = 'infrawatch_session'
# Secure cookie is the default now; a plain-HTTP LAN install can opt out with
# SESSION_COOKIE_SECURE=0. (Previously this was opt-IN, so every default
# deployment shipped a non-Secure session cookie.)
app.config['SESSION_COOKIE_SECURE'] = os.environ.get("SESSION_COOKIE_SECURE", "1") != "0"
# Flask defaults PERMANENT_SESSION_LIFETIME to 31 days; a NOC login on a
# shared workstation shouldn't stay valid that long unattended.
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=int(os.environ.get("INFRAWATCH_SESSION_HOURS", "24")))
# Static assets (JS/CSS/mp3) are safe to let browsers cache briefly — only the
# dynamic/live JSON endpoints need the no-cache headers below. The ?v=<mtime>
# query string (see inject_asset_version) busts this immediately on any change,
# so the window can be a full week rather than 5 minutes.
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 604800
# Reject oversized request bodies outright (memory-exhaustion floor). The
# webhook and every JSON API here deal in small payloads; 2 MiB is generous.
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get("INFRAWATCH_MAX_BODY_BYTES", str(2 * 1024 * 1024)))

# Asset-version cache-busting, gzip for text payloads, security headers, and
# the JSON 404/405/500 handlers — see web_middleware.py.
register_web_middleware(app)

# status.json / logs.json / history.json paths, MAX_HISTORY / MAX_LOGS and
# load_json / save_json / save_with_retention live in json_store.py. Readers
# reference them as json_store.<NAME> so a test can point the cache at a temp
# dir by rebinding json_store.STATUS_FILE etc.


# active_incident_list(), _WEBHOOK_LOCK, _LAST_WEBHOOK_AT, the maintenance-
# window helpers (load_maintenance_windows / maintenance_windows_by_instance /
# get_active_maintenance), the per-request flask.g memo, record_alert_event()
# and the dependency-correlation helpers all live in alerts.py (re-imported
# above; callers use bare names).

# The Prometheus HTTP access layer — _DEFAULT_PROM_URL, load_endpoints() and
# its cache, is_safe_endpoint_url() (ssrf.py), the DNS-rebinding hot-path
# re-check, the PromQL response cache + single-flight locks, and
# fetch_url()/fetch_prometheus_json() with endpoint failover — lives in
# prometheus_client.py. Every name is re-imported above so callers here and
# the tests (alarm_app.PROMETHEUS_CACHE, etc.) are unchanged.

# Target tombstone management — load_deleted_targets, save_deleted_targets
# live in targets_store.py (re-imported above).

# LAST_WORKING_PROMETHEUS_URL, PROMETHEUS_CACHE(+LOCK), _SHARED_EXECUTOR, the
# failed-candidate circuit breaker, single-flight locks, cache pruning, the
# DNS-rebinding re-check, fetch_url() and fetch_prometheus_json() are all in
# prometheus_client.py (re-imported above). Endpoint-mutating routes set
# promclient.LAST_WORKING_PROMETHEUS_URL directly so the failover primary
# tracks the operator's selection.

# Rate limiting (`rate_limit` decorator) and the per-username login lockout
# (`_login_locked` / `_login_note_failure` / `_login_clear`) live in
# rate_limit.py — re-imported above so `alarm_app._RATE_BUCKETS` etc. are
# unchanged.

# ── Authorization model (deliberate, see audit F52) ──────────────────────────
#   * Operational reads — /instances, /status, /history, /logs,
#     /api/availability, /api/targets, /api/prometheus-targets,
#     /api/maintenance (GET), /api/dependencies (GET), /api/jobs, /health* —
#     are intentionally UNauthenticated: this is a NOC LAN wallboard shown on
#     shared screens with no login, and everything above is already visible on
#     that wallboard.
#   * Config / secret / audit reads — /api/telegram, /api/settings/availability,
#     /api/sla-targets, /api/slow-thresholds, /api/audit/logs, /api/auth/users —
#     ARE gated (@require_permission): they expose bot tokens, tuning knobs, or
#     the audit trail, none of which belong on the open wallboard.
#   * All mutations (POST/PUT/PATCH/DELETE) require an admin session or the M2M
#     API key.
# /api/jobs (F37) is a curl-friendly diagnostic that returns the same job list
# already derivable from /api/prometheus-targets; kept public for parity with
# that endpoint rather than half-gated.
@app.route('/')
def index():
    # No credential is rendered here. The dashboard is a read-only LAN
    # wallboard; UI mutations authenticate with an admin *session cookie*
    # established via /api/auth/login — alarm.js apiFetch() sends only that
    # cookie, there is no client-side API key or authHeaders(). The M2M
    # INFRAWATCH_API_KEY / X-API-Key path is for scripts & automation only.
    return render_template('alarm.html')

# ── Authentication & User Management API ──────────────────────────────────────
@app.route('/api/auth/status', methods=['GET'])
def auth_status_api():
    try:
        user_count = UserRepository.count_users()
    except Exception:
        user_count = 0
    initialized = user_count > 0
    current_user = get_current_authenticated_user()
    return jsonify({
        "ok": True,
        "initialized": initialized,
        "authenticated": current_user is not None,
        "user": current_user
    })

@app.route('/api/auth/setup', methods=['POST'])
@rate_limit(10, 60)
def auth_setup_api():
    data = request.json or {}
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    confirm = str(data.get("confirm_password") or "")
    display_name = str(data.get("display_name") or username).strip()

    if not username or len(username) < 3:
        return jsonify({"ok": False, "error": "Username must be at least 3 characters"}), 400
    if not re.match(r'^[a-zA-Z0-9_\-\.]+$', username):
        return jsonify({"ok": False, "error": "Username contains invalid characters"}), 400
    if not password or len(password) < 12:
        return jsonify({"ok": False, "error": "Password must be at least 12 characters"}), 400
    if confirm and password != confirm:
        return jsonify({"ok": False, "error": "Passwords do not match"}), 400

    pw_hash = hash_password(password)
    user = UserRepository.create_first_admin(username=username, password_hash=pw_hash, display_name=display_name)
    if not user:
        return jsonify({"ok": False, "error": "System already initialized with an administrator"}), 409

    # Automatically create session and log in the first admin. Clear first so
    # nothing from a pre-auth session is carried across the privilege change.
    session.clear()
    session["user_id"] = user["id"]
    session["epoch"] = user.get("session_epoch", 0)
    session.permanent = True
    try:
        UserRepository.update_last_login(user["id"])
        AuditLogRepository.record_action(
            actor_username=username,
            actor_role="owner",
            action="SYSTEM_SETUP",
            resource=f"user:{username}",
            details="Founding owner account created"
        )
    except Exception:
        # The account exists and the session is set — a failure writing the
        # last-login timestamp or audit row must not turn a real login into a
        # 500 that tells the client it failed.
        logger.warning("post-setup bookkeeping failed for user %s", user["id"], exc_info=True)
    user_info = get_current_authenticated_user()
    return jsonify({"ok": True, "user": user_info})

# Precomputed once so the "no such user" path spends the same CPU on a hash
# comparison as the "user exists" path — closes the response-time oracle that
# otherwise lets an attacker enumerate valid usernames.
_DUMMY_PW_HASH = hash_password(uuid.uuid4().hex)

@app.route('/api/auth/login', methods=['POST'])
@rate_limit(20, 60)
def auth_login_api():
    data = request.json or {}
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")

    if not username or not password:
        return jsonify({"ok": False, "error": "Username and password are required"}), 400

    user_with_hash = UserRepository.get_by_username(username, include_password_hash=True)
    active = bool(user_with_hash and user_with_hash.get("is_active"))
    stored_hash = user_with_hash.get("password_hash", "") if user_with_hash else ""
    # Always run one verify — against the real hash if the account is usable,
    # the dummy otherwise — so response timing can't enumerate usernames.
    verified = verify_password(password, stored_hash if active else _DUMMY_PW_HASH)
    password_ok = active and verified

    if not password_ok:
        _login_note_failure(username)
        # The per-username lockout gates FAILED attempts only. A caller who
        # presents the correct password is admitted below regardless of lock
        # state — so a third party spraying bad passwords at a known username
        # can slow a brute-force run but can no longer lock the real user out
        # (previously this was a permanent account-denial DoS).
        if _login_locked(username):
            return jsonify({"ok": False, "error": "Too many failed attempts. Try again in a few minutes."}), 429
        return jsonify({"ok": False, "error": "Invalid username or password"}), 401

    _login_clear(username)
    # Rotate the session on the privilege change; drop any pre-auth contents.
    session.clear()
    session["user_id"] = user_with_hash["id"]
    session["epoch"] = user_with_hash.get("session_epoch", 0)
    session.permanent = True

    user_info = get_current_authenticated_user()
    try:
        UserRepository.update_last_login(user_with_hash["id"])
        AuditLogRepository.record_action(
            actor_username=user_info["username"],
            actor_role=user_info["role"],
            action="USER_LOGIN",
            resource=f"user:{user_info['username']}",
            details="User logged in via web session"
        )
    except Exception:
        # Session is already established; a bookkeeping failure must not 500 a
        # successful login.
        logger.warning("post-login bookkeeping failed for user %s", user_with_hash["id"], exc_info=True)
    return jsonify({"ok": True, "user": user_info})

@app.route('/api/auth/logout', methods=['POST'])
def auth_logout_api():
    user = get_current_authenticated_user()
    if user and not user.get("is_m2m"):
        AuditLogRepository.record_action(
            actor_username=user["username"],
            actor_role=user["role"],
            action="USER_LOGOUT",
            resource=f"user:{user['username']}",
            details="User logged out"
        )
    session.clear()
    return jsonify({"ok": True})

@app.route('/api/auth/me', methods=['GET'])
@require_auth
def auth_me_api():
    return jsonify({"ok": True, "user": g.current_user})

@app.route('/api/auth/users', methods=['GET'])
@require_permission("users.manage")
def list_users_api():
    return jsonify({"ok": True, "users": UserRepository.list_users()})

@app.route('/api/auth/users', methods=['POST'])
@require_permission("users.manage")
def create_user_api():
    data = request.json or {}
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    role = str(data.get("role") or "viewer").strip().lower()
    display_name = str(data.get("display_name") or username).strip()

    if not username or len(username) < 3:
        return jsonify({"ok": False, "error": "Username must be at least 3 characters"}), 400
    if not re.match(r'^[a-zA-Z0-9_\-\.]+$', username):
        return jsonify({"ok": False, "error": "Username contains invalid characters"}), 400
    if not password or len(password) < 12:
        return jsonify({"ok": False, "error": "Password must be at least 12 characters"}), 400
    if role == "owner":
        return jsonify({"ok": False, "error": "The owner role cannot be assigned — there is exactly one owner, set at first-boot setup"}), 403
    if role not in ("admin", "viewer"):
        return jsonify({"ok": False, "error": "Role must be admin or viewer"}), 400

    if UserRepository.get_by_username(username):
        return jsonify({"ok": False, "error": "Username already exists"}), 409

    pw_hash = hash_password(password)
    user = UserRepository.create_user(username=username, password_hash=pw_hash, role=role, display_name=display_name)
    AuditLogRepository.record_action(
        actor_username=g.current_user.get("username", "admin"),
        actor_role=g.current_user.get("role", "admin"),
        action="CREATE_USER",
        resource=f"user:{username}",
        details=f"Created user with role {role}"
    )
    return jsonify({"ok": True, "user": user})

@app.route('/api/auth/users/<int:user_id>', methods=['PATCH'])
@require_permission("users.manage")
def update_user_api(user_id):
    target = UserRepository.get_by_id(user_id)
    if not target:
        return jsonify({"ok": False, "error": "User not found"}), 404

    data = request.json or {}
    role = data.get("role")
    is_active = data.get("is_active")
    display_name = data.get("display_name")
    password = data.get("password") or None

    actor_role = g.current_user.get("role", "viewer")
    target_is_owner = target["role"] == "owner"

    # The owner account is protected: only the owner may touch it at all, its
    # role is permanent, and it can never be deactivated (no orphaning the
    # founder). An admin managing everyone else is unaffected.
    if target_is_owner and actor_role != "owner":
        return jsonify({"ok": False, "error": "Only the owner can modify the owner account"}), 403

    if role is not None:
        role = str(role).strip().lower()
        if role == "owner":
            return jsonify({"ok": False, "error": "The owner role cannot be assigned"}), 403
        if role not in ("admin", "viewer"):
            return jsonify({"ok": False, "error": "Role must be admin or viewer"}), 400
        if target_is_owner:
            return jsonify({"ok": False, "error": "The owner's role cannot be changed"}), 400
    if is_active is not None and not is_active and target_is_owner:
        return jsonify({"ok": False, "error": "The owner account cannot be deactivated"}), 400
    if password is not None and len(password) < 12:
        return jsonify({"ok": False, "error": "Password must be at least 12 characters"}), 400

    # Don't let the last active admin lock everyone (including themselves) out
    losing_admin = target["role"] == "admin" and target["is_active"] and (
        (role is not None and role != "admin") or (is_active is not None and not is_active)
    )
    if losing_admin:
        other_active_admins = sum(
            1 for u in UserRepository.list_users()
            if u["id"] != user_id and u["role"] == "admin" and u["is_active"]
        )
        if other_active_admins == 0:
            return jsonify({"ok": False, "error": "Cannot remove the last active admin"}), 400

    pw_hash = hash_password(password) if password else None
    updated = UserRepository.update_user(
        user_id, role=role, is_active=is_active, display_name=display_name, password_hash=pw_hash
    )
    if not updated:
        return jsonify({"ok": False, "error": "No changes to apply"}), 400

    AuditLogRepository.record_action(
        actor_username=g.current_user.get("username", "admin"),
        actor_role=g.current_user.get("role", "admin"),
        action="UPDATE_USER",
        resource=f"user:{target['username']}",
        details=f"role={role if role is not None else target['role']}, is_active={is_active if is_active is not None else target['is_active']}"
    )
    return jsonify({"ok": True, "user": UserRepository.get_by_id(user_id)})

# ── Alert Acknowledgment API ──────────────────────────────────────────────────
@app.route('/api/alerts/ack', methods=['POST'])
@rate_limit(30, 60)
@require_permission("alerts.ack")
def acknowledge_alert_api():
    data = request.json or {}
    instances = data.get("instances")
    instance = data.get("instance") or data.get("target")

    target_list = []
    if isinstance(instances, list):
        target_list = [str(x).strip() for x in instances if str(x).strip()]
    elif instance:
        target_list = [str(instance).strip()]

    if not target_list:
        # Acknowledge all currently down instances
        state = fleet_state_engine.get_fleet_state(FleetQuery(job_filter="all"))
        target_list = [t["instance"] for t in state.targets if t.get("health") != "up" and not t.get("maintenance") and not t.get("acknowledged")]

    if not target_list:
        return jsonify({"ok": True, "message": "No active down targets to acknowledge", "acknowledged": []})

    username = g.current_user.get("username", "operator")
    acked = AcknowledgmentRepository.acknowledge_instances(target_list, username=username)

    AuditLogRepository.record_action(
        actor_username=g.current_user.get("username", "operator"),
        actor_role=g.current_user.get("role", "admin"),
        action="ACK_ALERT",
        resource=",".join(target_list[:5]),
        details=f"Acknowledged {len(target_list)} down instance(s)"
    )
    return jsonify({"ok": True, "acknowledged": acked})

@app.route('/api/alerts/unack', methods=['POST'])
@rate_limit(30, 60)
@require_permission("alerts.ack")
def unacknowledge_alert_api():
    data = request.json or {}
    instance = str(data.get("instance") or data.get("target") or "").strip()
    if not instance:
        return jsonify({"ok": False, "error": "Instance is required"}), 400

    AcknowledgmentRepository.unacknowledge_instance(instance)
    AuditLogRepository.record_action(
        actor_username=g.current_user.get("username", "operator"),
        actor_role=g.current_user.get("role", "admin"),
        action="UNACK_ALERT",
        resource=instance,
        details="Unacknowledged instance outage"
    )
    return jsonify({"ok": True})

@app.route('/api/alerts/resolve', methods=['POST'])
@rate_limit(30, 60)
@require_permission("alerts.ack")
def resolve_alert_api():
    """Force-resolve a firing incident by key (or name + instance).

    Operator backstop for a phantom incident that no automatic path will ever
    clear (audit F1) — e.g. one recorded for an instance the external
    Prometheus no longer scrapes, so the poller's transition detector never
    sees it recover. `_reconcile_orphaned_alerts` handles poller-owned
    (TargetDown/SlowResponse) phantoms automatically once Prometheus is
    reachable; this covers everything else, and gives ops a manual lever.
    """
    data = request.json or {}
    key = str(data.get("key") or "").strip()
    name = str(data.get("name") or "").strip()
    instance = str(data.get("instance") or data.get("target") or "").strip()

    if not key:
        if name and instance:
            key = f"{name}|{instance}"
        else:
            return jsonify({"ok": False, "error": "key, or name + instance, is required"}), 400
    if (not name or not instance) and "|" in key:
        k_name, k_inst = key.split("|", 1)
        name = name or k_name
        instance = instance or k_inst

    # Resolve straight against SQLite (the source of truth). record_alert_event()
    # gates on the status.json cache first, so it would NO-OP a phantom that is
    # firing in SQLite but absent from that cache — which is exactly this
    # endpoint's target. IncidentRepository gates on the DB row itself.
    now = time.time()
    try:
        changed = IncidentRepository.record_alert_event(
            name=name or "Unknown", severity="critical", instance=instance or "-",
            summary=f"{instance or key} manually resolved by operator", job="",
            event_time=now, is_now_firing=False, key=key,
        )
    except Exception:
        logger.exception("resolve_alert_api: SQLite resolve failed for %s", key)
        return jsonify({"ok": False, "error": "Could not resolve incident"}), 500

    # Drop it from the status.json cache too, and recompute the cached global
    # status, so a fallback read of that cache can't resurrect it.
    try:
        with _WEBHOOK_LOCK:
            sd = json_store.load_json(json_store.STATUS_FILE, None)
            if isinstance(sd, dict) and sd.get("alerts"):
                kept = [a for a in sd["alerts"]
                        if (a.get("key") or f"{a.get('name')}|{a.get('instance')}") != key]
                if len(kept) != len(sd["alerts"]):
                    sd["alerts"] = kept
                    if any(a.get('severity', 'critical') == 'critical' for a in kept):
                        sd["status"] = "CRITICAL"
                    elif kept:
                        sd["status"] = "WARNING"
                    else:
                        sd["status"] = "NORMAL"
                    sd["updated"] = now
                    json_store.save_json(json_store.STATUS_FILE, sd)
    except Exception:
        logger.exception("resolve_alert_api: status.json cache cleanup failed for %s", key)

    AuditLogRepository.record_action(
        actor_username=g.current_user.get("username", "operator"),
        actor_role=g.current_user.get("role", "admin"),
        action="RESOLVE_ALERT",
        resource=key,
        details="Manually force-resolved incident" + ("" if changed else " (was not firing — no-op)"),
    )
    return jsonify({"ok": True, "key": key, "changed": bool(changed)})

# ── Audit Trail API ───────────────────────────────────────────────────────────
@app.route('/api/audit/logs', methods=['GET'])
@require_permission("audit.read")
def get_audit_logs_api():
    try:
        limit = int(request.args.get('limit', 100))
    except (TypeError, ValueError):
        limit = 100
    logs = AuditLogRepository.get_recent_logs(limit=limit)
    return jsonify({"ok": True, "logs": logs})

# ── Webhook ───────────────────────────────────────────────────────────────────
@app.route('/webhook', methods=['POST'])
@app.route('/api/webhook', methods=['POST'])
@require_webhook_secret
def webhook():
    data     = request.json or {}
    if not isinstance(data, dict):
        data = {}
    alerts   = data.get('alerts', [])
    if not isinstance(alerts, list):
        alerts = []
    receiver = data.get('receiver', '')
    now      = time.time()
    _LAST_WEBHOOK_AT[0] = now

    for a in alerts:
        if not isinstance(a, dict):
            continue  # malformed alert entry (null, string, number, ...) — ignore, don't crash

        status_str = a.get('status', 'unknown')
        labels      = a.get('labels')
        labels      = labels if isinstance(labels, dict) else {}
        annotations = a.get('annotations')
        annotations = annotations if isinstance(annotations, dict) else {}
        name        = labels.get('alertname', 'Unknown')
        severity    = labels.get('severity', 'critical')
        instance    = labels.get('instance', '-')
        summary     = annotations.get('summary', '')
        job         = labels.get('job', '')
        generatorURL = a.get('generatorURL', '')
        key         = alert_key(a, labels)
        is_now_firing = (status_str == 'firing')
        event_time = parse_alert_timestamp(
            a.get('startsAt') if is_now_firing else a.get('endsAt'), now)

        record_alert_event(
            name=name, severity=severity, instance=instance, summary=summary,
            job=job, event_time=event_time, is_now_firing=is_now_firing,
            receiver=receiver, generatorURL=generatorURL, key=key
        )

    return jsonify({"ok": True})

# ── Status / History / Logs ───────────────────────────────────────────────────
def _annotate_logs_with_acknowledgment(log_rows):
    """Live Alert Log rows are discrete past events, not ongoing state — an
    acknowledgment isn't a property of one specific historical row, it's a
    property of "is this instance's CURRENT outage acked right now". So this
    joins each still-open 'firing' row against the live acknowledgment table
    rather than storing ack info per log row. (Incident History uses the
    durable incidents.acknowledged_by/at columns instead, since a resolved
    incident needs the info to survive past the live table getting cleared —
    see AcknowledgmentRepository.clear_resolved.)

    event_logs is append-only: a 'firing' row's event column stays 'firing'
    forever, even for an outage that resolved ages ago, so an instance that's
    flapped several times has several 'firing' rows. log_rows arrives newest
    first (EventLogRepository.get_logs' ORDER BY time DESC) — the first
    firing row hit for a given instance, before any resolved row for that
    same instance is seen, is the only one that's actually still open;
    everything older belongs to an already-closed episode and must be left
    alone even if the instance happens to be acknowledged again right now.
    """
    try:
        ack_map = AcknowledgmentRepository.get_active_acknowledgments()
    except Exception:
        return
    if not ack_map:
        return
    seen_instances = set()
    for row in log_rows:
        inst = row.get('instance')
        event = row.get('event')
        if inst is None or inst in seen_instances or event not in ('firing', 'resolved'):
            continue
        seen_instances.add(inst)  # newest mention of this instance either way
        if event != 'firing':
            continue
        ack = ack_map.get(inst)
        if ack:
            row['acknowledged_by'] = ack['acknowledged_by']
            row['acknowledged_at'] = ack['acknowledged_at']

@app.route('/status')
@app.route('/api/status')
@rate_limit(120, 60)  # unauthenticated + fans out to Prometheus via build_canonical_monitoring_state (audit F16); no legit client polls this
def status():
    state = fleet_state_engine.get_fleet_state(FleetQuery())
    return jsonify(state.to_status_dict())

# NOTE: /history and /logs return a bare JSON array (no {"ok": ...} envelope)
# for backward compatibility with the History/Logs page consumers in alarm.js.
# Unifying them onto the standard envelope is tracked as audit finding F33 and
# needs a coordinated frontend change; not done here to avoid a silent break.
@app.route('/history')
@app.route('/api/history')
def history():
    try:
        # Return the SQLite result even when it is a legitimately empty list —
        # an empty history is not a read failure (audit F20).
        return jsonify(IncidentRepository.get_history(limit=json_store.MAX_HISTORY))
    except Exception:
        logger.exception("history(): SQLite read failed, falling back to history.json")
    # Fallback also folds in history_archive.json so overflow rows past
    # MAX_HISTORY remain reachable in this degraded path (audit F11).
    return jsonify((json_store.load_json(json_store.HISTORY_FILE, []) + json_store.load_json(json_store.HISTORY_ARCHIVE_FILE, []))[:json_store.MAX_HISTORY])

@app.route('/logs')
@app.route('/api/logs')
def logs():
    try:
        limit = int(request.args.get('limit', 50))
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, json_store.MAX_LOGS))
    try:
        # A legitimately empty log list is not a read failure (audit F20).
        data = EventLogRepository.get_logs(limit=limit)
        _annotate_logs_with_acknowledgment(data)
        return jsonify(data)
    except Exception:
        logger.exception("logs(): SQLite read failed, falling back to logs.json")
    data = json_store.load_json(json_store.LOGS_FILE, [])
    return jsonify(data[:limit])

_EP_STATUS_CACHE = {"ts": 0.0, "key": "", "data": None}

# ── Prometheus Endpoint Management API ───────────────────────────────────────
@app.route('/api/endpoints', methods=['GET'])
def get_endpoints_api():
    now = time.time()
    data = load_endpoints()
    active = data.get("active")
    endpoints = data.get("endpoints", [])

    cache_key = f"{active}:" + ",".join(endpoints)
    if _EP_STATUS_CACHE["data"] is not None and (now - _EP_STATUS_CACHE["ts"] < 10.0) and _EP_STATUS_CACHE.get("key") == cache_key:
        return jsonify(_EP_STATUS_CACHE["data"])

    def check_ep(ep):
        raw = promclient.fetch_url(f"{ep.rstrip('/')}/api/v1/status/flags", timeout=0.25)
        return {
            "url": ep,
            "active": ep == active,
            "online": raw is not None
        }

    with ThreadPoolExecutor(max_workers=5) as executor:
        result = list(executor.map(check_ep, endpoints))

    resp_data = {
        "ok": True,
        "active": active,
        "endpoints": result
    }
    _EP_STATUS_CACHE["ts"] = now
    _EP_STATUS_CACHE["key"] = cache_key
    _EP_STATUS_CACHE["data"] = resp_data
    return jsonify(resp_data)

@app.route('/api/endpoints', methods=['POST'])
@rate_limit(20, 60)
@require_permission('endpoints.write')
def add_endpoint_api():
    body = request.json or {}
    url = body.get('url', '').strip()
    if not url:
        return jsonify({"ok": False, "error": "Prometheus Endpoint URL is required"}), 400

    if "://" in url:
        scheme = url.split("://", 1)[0].lower()
        if scheme not in ("http", "https"):
            return jsonify({"ok": False, "error": f"Invalid scheme: '{scheme}'. Only http and https are allowed."}), 400
    else:
        url = f"http://{url}"
    url = url.rstrip('/')

    is_safe, err_msg = is_safe_endpoint_url(url)
    if not is_safe:
        return jsonify({"ok": False, "error": err_msg or "Invalid endpoint URL"}), 400

    with _WEBHOOK_LOCK:
        set_active = bool(body.get('set_active', True))
        try:
            EndpointRepository.create_endpoint(name=url, url=url, is_active=set_active)
            if set_active:
                EndpointRepository.select_endpoint(url)
        except Exception as e:
            logger.error(f"Error adding endpoint {url}: {e}")
            return jsonify({"ok": False, "error": "Failed to save endpoint"}), 500

        if set_active:
            promclient.LAST_WORKING_PROMETHEUS_URL = url
            with PROMETHEUS_CACHE_LOCK:
                PROMETHEUS_CACHE.clear()

        _ENDPOINTS_CACHE["data"] = None
        data = load_endpoints()
        _EP_STATUS_CACHE["data"] = None

    AuditLogRepository.record_action(
        actor_username=g.current_user.get("username", "admin"),
        actor_role=g.current_user.get("role", "admin"),
        action="ADD_ENDPOINT",
        resource=url,
        details=f"Added endpoint (set_active={set_active})"
    )
    return jsonify({"ok": True, "active": data["active"], "endpoints": data["endpoints"]})

@app.route('/api/endpoints/select', methods=['POST'])
@rate_limit(20, 60)
@require_permission('endpoints.write')
def select_endpoint_api():
    body = request.json or {}
    url = body.get('url', '').strip()
    if not url:
        return jsonify({"ok": False, "error": "Prometheus Endpoint URL is required"}), 400

    is_safe, err_msg = is_safe_endpoint_url(url)
    if not is_safe:
        return jsonify({"ok": False, "error": err_msg or "Invalid endpoint URL"}), 400

    with _WEBHOOK_LOCK:
        try:
            EndpointRepository.select_endpoint(url)
        except Exception:
            pass

        _ENDPOINTS_CACHE["data"] = None
        _EP_STATUS_CACHE["data"] = None

        # Immediately clear cache and force selected URL as primary
        promclient.LAST_WORKING_PROMETHEUS_URL = url
        with PROMETHEUS_CACHE_LOCK:
            PROMETHEUS_CACHE.clear()

    AuditLogRepository.record_action(
        actor_username=g.current_user.get("username", "admin"),
        actor_role=g.current_user.get("role", "admin"),
        action="SELECT_ENDPOINT",
        resource=url,
        details="Selected active Prometheus endpoint"
    )
    return jsonify({"ok": True, "active": url})

@app.route('/api/endpoints', methods=['DELETE'])
@rate_limit(20, 60)
@require_permission('endpoints.write')
def delete_endpoint_api():
    body = request.json or {}
    url = body.get('url', '').strip()

    with _WEBHOOK_LOCK:
        data = load_endpoints()

        if url not in data["endpoints"]:
            return jsonify({"ok": False, "error": "Prometheus Endpoint not found"}), 404

        # Deleting the last endpoint is allowed — the deployment is then in a
        # deliberate "no Prometheus configured" state (every query returns no
        # data) until the operator adds one. It will NOT be re-seeded on the
        # next boot unless PROMETHEUS_URL is set (see load_endpoints_state).

        was_active = (data["active"] == url)
        try:
            EndpointRepository.delete_endpoint(url)
        except Exception:
            pass

        _ENDPOINTS_CACHE["data"] = None
        data = load_endpoints()
        if was_active:
            promclient.LAST_WORKING_PROMETHEUS_URL = data["active"]
            with PROMETHEUS_CACHE_LOCK:
                PROMETHEUS_CACHE.clear()
        _EP_STATUS_CACHE["data"] = None

    AuditLogRepository.record_action(
        actor_username=g.current_user.get("username", "admin"),
        actor_role=g.current_user.get("role", "admin"),
        action="DELETE_ENDPOINT",
        resource=url,
        details="Deleted Prometheus endpoint"
    )
    return jsonify({"ok": True, "active": data["active"], "endpoints": data["endpoints"]})

# Standalone diagnostic utility — lists Prometheus job names for curl/ops use.
# Not called by the frontend (instances()/get_prometheus_available_targets()
# derive available_jobs inline); kept as a manual debugging endpoint.
@app.route('/api/jobs', methods=['GET'])
def get_jobs_api():
    raw_targets, _ = promclient.fetch_prometheus_json('/api/v1/targets')
    jobs = set()
    if raw_targets and raw_targets.get('status') == 'success':
        for t in raw_targets.get('data', {}).get('activeTargets', []):
            j = t.get('labels', {}).get('job') or t.get('scrapePool')
            if j and j != 'prometheus':
                jobs.add(j)
    return jsonify({"ok": True, "jobs": sorted(list(jobs)), "default_job": DEFAULT_JOB_FILTER})

# ── Targets CRUD API ──────────────────────────────────────────────────────────
@app.route('/api/prometheus-targets', methods=['GET'])
def get_prometheus_available_targets():
    job_param = request.args.get('job', DEFAULT_JOB_FILTER)
    raw_targets, _ = promclient.fetch_prometheus_json('/api/v1/targets')
    deleted_targets = set(load_deleted_targets())
    
    prom_list = []
    seen = set()

    if raw_targets and raw_targets.get('status') == 'success':
        targets = raw_targets.get('data', {}).get('activeTargets', [])
        for t in targets:
            labels = t.get('labels', {})
            job = labels.get('job') or t.get('scrapePool') or ''
            scrape_pool = t.get('scrapePool', '')
            inst_name = labels.get('instance', t.get('scrapeUrl', '?'))
            
            if job != 'prometheus' and matches_job_filter(job, scrape_pool, job_param) and inst_name not in seen:
                seen.add(inst_name)
                prom_list.append({
                    "instance": inst_name,
                    "isDeleted": inst_name in deleted_targets,
                    "job": job or "blackbox"
                })
            
    prom_list.sort(key=lambda x: x['instance'], reverse=False)
    return jsonify({"ok": True, "targets": prom_list})

def _prometheus_discovered_instances():
    """Best-effort set of instance names Prometheus currently scrapes. Empty
    set means 'could not tell' (Prometheus unreachable) — callers must not
    treat empty as 'nothing discovered'."""
    # Short timeout: on the wallboard this call is almost always cache-warm
    # (the dashboard polls /api/v1/targets continuously); when it isn't, a
    # missing warning is no worse than the pre-existing behavior, so don't
    # make the operator wait on a slow/dead Prometheus.
    raw, _ = promclient.fetch_prometheus_json('/api/v1/targets', use_cache=True, cache_ttl=10.0, timeout=0.8)
    if not raw or raw.get('status') != 'success':
        return set()
    out = set()
    for t in raw.get('data', {}).get('activeTargets', []):
        inst = (t.get('labels') or {}).get('instance')
        if inst:
            out.add(inst)
        if t.get('scrapeUrl'):
            out.add(t['scrapeUrl'])
    return out


@app.route('/api/targets', methods=['GET'])
def get_targets_api():
    # `deleted` is returned so the UI can show — and offer to restore —
    # tombstoned targets instead of them just vanishing forever. A
    # re-POST of any deleted url clears its tombstone.
    return jsonify({
        "ok": True,
        "targets": get_monitored_instances(),
        "deleted": load_deleted_targets(),
    })

@app.route('/api/targets', methods=['POST'])
@rate_limit(20, 60)
@require_permission('targets.write')
def add_target_api():
    data = request.json or {}
    url = data.get('url', '').strip()
    if not url:
        return jsonify({"ok": False, "error": "IP / Target host is required"}), 400
    if not is_valid_target(url):
        return jsonify({"ok": False, "error": "Invalid target — please use a valid hostname, IP, or URL"}), 400

    norm = normalize_target(url)
    drop = []
    try:
        with _WEBHOOK_LOCK:
            # Restore from tombstone if it was previously deleted.
            deleted = load_deleted_targets()
            drop = [d for d in deleted if normalize_target(d) == norm]
            if drop:
                for d in drop:
                    deleted.remove(d)
                save_deleted_targets(deleted)
    except Exception as e:
        logger.error("add_target_api failed for %s: %s", url, e)
        return jsonify({"ok": False, "error": "Could not persist target — see server log"}), 500

    AuditLogRepository.record_action(
        actor_username=g.current_user.get("username", "admin"),
        actor_role=g.current_user.get("role", "admin"),
        action="RESTORE_TARGET" if drop else "ADD_TARGET",
        resource=url,
        details="Restored target to monitoring" if drop else "Target verified in monitoring"
    )
    if drop:
        return jsonify({"ok": True, "message": "Target restored to monitoring."})

    discovered = _prometheus_discovered_instances()
    if discovered and url not in discovered and norm not in {normalize_target(d) for d in discovered}:
        return jsonify({
            "ok": True,
            "warning": "Prometheus is not currently scraping this target — configure your Prometheus scrape config to probe it."
        })
    return jsonify({"ok": True, "message": "Target is active and monitored by Prometheus."})

@app.route('/api/targets', methods=['DELETE'])
@rate_limit(20, 60)
@require_permission('targets.write')
def delete_target_api():
    data = request.json or {}
    url = data.get('url', '').strip()
    if not url:
        return jsonify({"ok": False, "error": "IP / Target host is required"}), 400

    norm = normalize_target(url)
    try:
        with _WEBHOOK_LOCK:
            deleted = load_deleted_targets()
            if not any(normalize_target(d) == norm for d in deleted):
                deleted.append(url)
                save_deleted_targets(deleted)
    except Exception as e:
        logger.error("delete_target_api failed for %s: %s", url, e)
        return jsonify({"ok": False, "error": "Could not persist target removal — see server log"}), 500

    AuditLogRepository.record_action(
        actor_username=g.current_user.get("username", "admin"),
        actor_role=g.current_user.get("role", "admin"),
        action="DELETE_TARGET",
        resource=url,
        details="Deleted website/IP target"
    )
    # `restorable` reminds the caller the tombstone is reversible (re-POST) —
    # a delete here only hides the target from InfraWatch, it cannot stop an
    # external Prometheus from scraping it.
    return jsonify({
        "ok": True,
        "restorable": True,
        "message": f"Target '{url}' hidden from monitoring. Re-add anytime to restore."
    })

# ── Maintenance windows API ─────────────────────────────────────────────────
@app.route('/api/maintenance', methods=['GET'])
def list_maintenance_api():
    windows = load_maintenance_windows()
    now = time.time()
    for w in windows:
        w['active'] = w.get('start', 0) <= now <= w.get('end', 0)
    windows.sort(key=lambda w: w.get('start', 0), reverse=True)
    return jsonify({"ok": True, "windows": windows})

@app.route('/api/maintenance', methods=['POST'])
@rate_limit(20, 60)
@require_permission('maintenance.write')
def create_maintenance_api():
    data = request.json or {}
    target = (data.get('target') or '').strip()
    scope = data.get('scope') if data.get('scope') in ('instance', 'job') else 'instance'
    reason = (data.get('reason') or '').strip()[:200]

    if not target:
        return jsonify({"ok": False, "error": "Target is required"}), 400
    try:
        start = int(float(data.get('start')))
        end = int(float(data.get('end')))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "start and end must be epoch timestamps"}), 400
    if end <= start:
        return jsonify({"ok": False, "error": "end timestamp must be after start timestamp"}), 400
    # Cap the span so a fat-fingered "epoch 0 → year 9999" window can't sit in
    # the table forever suppressing every alert for a target.
    if end - start > 366 * 86400:
        return jsonify({"ok": False, "error": "Maintenance window cannot exceed 366 days"}), 400

    with _WEBHOOK_LOCK:
        try:
            window = MaintenanceRepository.create_window(
                scope=scope, target=target, reason=reason, start=float(start), end=float(end)
            )
            _invalidate_maint_cache()
        except Exception:
            logger.exception("create_maintenance_api: DB write failed")
            return jsonify({"ok": False, "error": "Failed to save maintenance window"}), 500

    AuditLogRepository.record_action(
        actor_username=g.current_user.get("username", "admin"),
        actor_role=g.current_user.get("role", "admin"),
        action="CREATE_MAINTENANCE",
        resource=f"{scope}:{target}",
        details=f"Created maintenance window {window.get('id')} ({reason})"
    )
    return jsonify({"ok": True, "window": window})

@app.route('/api/maintenance/<window_id>', methods=['DELETE'])
@rate_limit(20, 60)
@require_permission('maintenance.write')
def delete_maintenance_api(window_id):
    with _WEBHOOK_LOCK:
        try:
            deleted = MaintenanceRepository.delete_window(window_id)
            _invalidate_maint_cache()
        except Exception:
            deleted = False
        if not deleted:
            return jsonify({"ok": False, "error": "Maintenance window not found"}), 404

    AuditLogRepository.record_action(
        actor_username=g.current_user.get("username", "admin"),
        actor_role=g.current_user.get("role", "admin"),
        action="DELETE_MAINTENANCE",
        resource=window_id,
        details="Deleted maintenance window"
    )
    return jsonify({"ok": True})

# ── Per-target SLA targets API ──────────────────────────────────────────────
# Optional per-instance SLA availability target (%). Absent -> the deployment
# default (SLA_TARGET_PCT env / 99.9). Drives that target's compliance status
# and error budget in /api/availability.
@app.route('/api/sla-targets', methods=['GET'])
@require_permission('targets.read')
def list_sla_targets_api():
    try:
        targets = SlaTargetRepository.get_all()
    except Exception:
        targets = {}
    return jsonify({"ok": True, "default_target_pct": get_sla_target_pct(), "targets": targets})

@app.route('/api/sla-targets/<path:instance>', methods=['PUT'])
@rate_limit(30, 60)
@require_permission('targets.write')
def set_sla_target_api(instance):
    instance = (instance or '').strip()
    if not instance:
        return jsonify({"ok": False, "error": "Instance is required"}), 400
    data = request.json or {}
    try:
        pct = float(data.get('target_pct'))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "target_pct must be a number 0-100"}), 400
    if not (0.0 <= pct <= 100.0):
        return jsonify({"ok": False, "error": "target_pct must be between 0 and 100"}), 400

    saved = SlaTargetRepository.set_target(instance, pct, updated_by=g.current_user.get("username"))
    clear_availability_cache(clear_db=False)
    AuditLogRepository.record_action(
        actor_username=g.current_user.get("username", "admin"),
        actor_role=g.current_user.get("role", "admin"),
        action="SET_SLA_TARGET",
        resource=instance,
        details=f"SLA target set to {saved}%"
    )
    return jsonify({"ok": True, "instance": instance, "target_pct": saved})

@app.route('/api/sla-targets/<path:instance>', methods=['DELETE'])
@rate_limit(30, 60)
@require_permission('targets.write')
def delete_sla_target_api(instance):
    instance = (instance or '').strip()
    try:
        removed = SlaTargetRepository.delete_target(instance)
    except Exception:
        removed = False
    if not removed:
        return jsonify({"ok": False, "error": "No SLA target override for that instance"}), 404
    clear_availability_cache(clear_db=False)
    AuditLogRepository.record_action(
        actor_username=g.current_user.get("username", "admin"),
        actor_role=g.current_user.get("role", "admin"),
        action="DELETE_SLA_TARGET",
        resource=instance,
        details="SLA target override removed (reverted to default)"
    )
    return jsonify({"ok": True, "instance": instance})

# ── Per-target SlowResponse threshold API ───────────────────────────────────
# Optional per-instance latency threshold (ms) for the SlowResponse warning
# alert. Absent -> DEFAULT_SLOW_RESPONSE_THRESHOLD_MS applies. A naturally
# slower target (e.g. an overseas endpoint) isn't "degraded" at the global
# default — override it here instead of it flapping warning forever.
@app.route('/api/slow-thresholds', methods=['GET'])
@require_permission('targets.read')
def list_slow_thresholds_api():
    try:
        thresholds = SlowThresholdRepository.get_all()
    except Exception:
        thresholds = {}
    return jsonify({"ok": True, "default_threshold_ms": DEFAULT_SLOW_RESPONSE_THRESHOLD_MS, "thresholds": thresholds})

@app.route('/api/slow-thresholds/<path:instance>', methods=['PUT'])
@rate_limit(30, 60)
@require_permission('targets.write')
def set_slow_threshold_api(instance):
    instance = (instance or '').strip()
    if not instance:
        return jsonify({"ok": False, "error": "Instance is required"}), 400
    data = request.json or {}
    try:
        ms = float(data.get('threshold_ms'))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "threshold_ms must be a number"}), 400
    if ms < 0:
        return jsonify({"ok": False, "error": "threshold_ms must be >= 0"}), 400

    saved = SlowThresholdRepository.set_threshold(instance, ms, updated_by=g.current_user.get("username"))
    AuditLogRepository.record_action(
        actor_username=g.current_user.get("username", "admin"),
        actor_role=g.current_user.get("role", "admin"),
        action="SET_SLOW_THRESHOLD",
        resource=instance,
        details=f"SlowResponse threshold set to {saved}ms"
    )
    return jsonify({"ok": True, "instance": instance, "threshold_ms": saved})

@app.route('/api/slow-thresholds/<path:instance>', methods=['DELETE'])
@rate_limit(30, 60)
@require_permission('targets.write')
def delete_slow_threshold_api(instance):
    instance = (instance or '').strip()
    try:
        removed = SlowThresholdRepository.delete_threshold(instance)
    except Exception:
        removed = False
    if not removed:
        return jsonify({"ok": False, "error": "No SlowResponse threshold override for that instance"}), 404
    AuditLogRepository.record_action(
        actor_username=g.current_user.get("username", "admin"),
        actor_role=g.current_user.get("role", "admin"),
        action="DELETE_SLOW_THRESHOLD",
        resource=instance,
        details="SlowResponse threshold override removed (reverted to default)"
    )
    return jsonify({"ok": True, "instance": instance})

@app.route('/api/dependencies', methods=['GET'])
def list_dependencies_api():
    return jsonify({"ok": True, "dependencies": load_dependencies()})

@app.route('/api/dependencies', methods=['POST'])
@rate_limit(20, 60)
@require_permission('dependencies.write')
def create_dependency_api():
    data = request.json or {}
    child = (data.get('child') or '').strip()
    parent = (data.get('parent') or '').strip()
    if not child or not parent:
        return jsonify({"ok": False, "error": "child and parent are required"}), 400
    if child == parent:
        return jsonify({"ok": False, "error": "Host cannot depend on itself"}), 400

    with _WEBHOOK_LOCK:
        try:
            dep = DependencyRepository.create_dependency(parent=parent, child=child)
            _invalidate_dep_cache()
        except Exception:
            logger.exception("create_dependency_api: DB write failed")
            return jsonify({"ok": False, "error": "Failed to save dependency"}), 500

    AuditLogRepository.record_action(
        actor_username=g.current_user.get("username", "admin"),
        actor_role=g.current_user.get("role", "admin"),
        action="CREATE_DEPENDENCY",
        resource=f"{parent}->{child}",
        details=f"Created dependency: {child} depends on {parent}"
    )
    return jsonify({"ok": True, "dependency": dep})

@app.route('/api/dependencies/<dep_id>', methods=['DELETE'])
@rate_limit(20, 60)
@require_permission('dependencies.write')
def delete_dependency_api(dep_id):
    with _WEBHOOK_LOCK:
        try:
            deleted = DependencyRepository.delete_dependency(dep_id)
            _invalidate_dep_cache()
        except Exception:
            deleted = False
        if not deleted:
            return jsonify({"ok": False, "error": "Dependency not found"}), 404

    AuditLogRepository.record_action(
        actor_username=g.current_user.get("username", "admin"),
        actor_role=g.current_user.get("role", "admin"),
        action="DELETE_DEPENDENCY",
        resource=dep_id,
        details="Deleted dependency link"
    )
    return jsonify({"ok": True})

# ── Telegram Notifications API ───────────────────────────────────────────────
# Operator-only: returns bot_token_masked/chat_id, which are not currently
# consumed by any part of the frontend (verified — no /api/telegram fetch
# exists in alarm.js) and shouldn't be reachable by an unauthenticated LAN
# viewer.
@app.route('/api/telegram', methods=['GET'])
@rate_limit(20, 60)
@require_permission('telegram.read')
def get_telegram_api():
    config = get_telegram_config()
    token = config.get("bot_token", "")
    masked_token = token[:8] + "..." + token[-6:] if len(token) > 14 else (token if token else "")
    return jsonify({
        "ok": True,
        "enabled": config.get("enabled", True),
        "bot_token_masked": masked_token,
        "has_token": bool(token),
        "chat_id": str(config.get("chat_id", "")),
        "send_firing": config.get("send_firing", True),
        "send_resolved": config.get("send_resolved", True),
        "min_severity": config.get("min_severity", "warning")
    })

@app.route('/api/telegram', methods=['POST'])
@rate_limit(20, 60)
@require_permission('telegram.write')
def save_telegram_api():
    data = request.json or {}
    updated = {}
    if "enabled" in data:
        updated["enabled"] = bool(data["enabled"])
    if "bot_token" in data and data["bot_token"].strip():
        updated["bot_token"] = data["bot_token"].strip()
    if "chat_id" in data:
        updated["chat_id"] = str(data["chat_id"]).strip()
    if "send_firing" in data:
        updated["send_firing"] = bool(data["send_firing"])
    if "send_resolved" in data:
        updated["send_resolved"] = bool(data["send_resolved"])
    if "min_severity" in data:
        updated["min_severity"] = str(data["min_severity"]).strip().lower()

    if save_telegram_config(updated):
        AuditLogRepository.record_action(
            actor_username=g.current_user.get("username", "admin"),
            actor_role=g.current_user.get("role", "admin"),
            action="UPDATE_TELEGRAM",
            resource="telegram_config",
            details="Updated Telegram notification settings"
        )
        return jsonify({"ok": True, "message": "Telegram configuration saved"})
    return jsonify({"ok": False, "error": "Failed to save configuration"}), 500

@app.route('/api/telegram/test', methods=['POST'])
@rate_limit(10, 60)
@require_permission('telegram.write')
def test_telegram_api():
    data = request.json or {}
    token = data.get("bot_token")
    cid = data.get("chat_id")
    ok, msg = test_telegram_connection(token, cid)
    if ok:
        AuditLogRepository.record_action(
            actor_username=g.current_user.get("username", "admin"),
            actor_role=g.current_user.get("role", "admin"),
            action="TEST_TELEGRAM",
            resource="telegram_test",
            details="Dispatched test notification to Telegram"
        )
        return jsonify({"ok": True, "message": "Test notification sent successfully to Telegram"})
    return jsonify({"ok": False, "error": msg}), 400

# ── Availability / SLA Settings API ──────────────────────────────────────────
@app.route('/api/settings/availability', methods=['GET'])
@rate_limit(20, 60)
@require_permission('availability.read')
def get_availability_settings_api():
    settings = get_availability_settings()
    return jsonify({
        "ok": True,
        "use_node_exporter_correlation": settings.get("use_node_exporter_correlation", False),
    })

@app.route('/api/settings/availability', methods=['POST'])
@rate_limit(20, 60)
@require_permission('availability.write')
def save_availability_settings_api():
    data = request.json or {}
    updated = {}
    if "use_node_exporter_correlation" in data:
        updated["use_node_exporter_correlation"] = bool(data["use_node_exporter_correlation"])

    if save_availability_settings(updated):
        availability_engine.invalidate_cache()
        AuditLogRepository.record_action(
            actor_username=g.current_user.get("username", "admin"),
            actor_role=g.current_user.get("role", "admin"),
            action="UPDATE_AVAILABILITY_SETTINGS",
            resource="availability_settings",
            details=f"Set use_node_exporter_correlation={updated.get('use_node_exporter_correlation')}"
        )
        return jsonify({"ok": True, "message": "Availability settings saved"})
    return jsonify({"ok": False, "error": "Failed to save settings"}), 500

# fetch_prom_query_map / fetch_prom_range_map / fetch_down_since_prom_map /
# fetch_all_probe_metrics — Prometheus response adapters — live in
# prom_queries.py (re-imported above; callers use bare names so
# patch.object(alarm_app, 'fetch_all_probe_metrics', ...) still works).

# The canonical monitoring-state engine (_derive_probe_readings,
# build_canonical_monitoring_state) and the instance/job/cadence maps live
# in monitoring_state.py (re-imported above; callers use bare names).

# ── Instances & Real-time Metrics API ─────────────────────────────────────────
@app.route('/instances')
@app.route('/api/instances')
@rate_limit(120, 60)
def instances():
    query = FleetQuery.from_request(request.args, default_job=DEFAULT_JOB_FILTER)
    state = fleet_state_engine.get_fleet_state(query)
    if not state.ok and state.error:
        return jsonify(state.to_dict()), 503
    return jsonify(state.to_dict())

# ── Availability (historical uptime %) ────────────────────────────────────────
# _attach_sla_budgets(), _availability_status_counts() and _build_fleet_trend()
# (+ _FLEET_TREND_CACHE / _FLEET_TREND_CACHE_TTL) live in availability.py —
# re-imported above so `alarm_app._build_fleet_trend` etc. stay patchable. The
# /api/availability route below (cache / single-flight / hybrid orchestration)
# calls them as bare names.


@app.route('/api/availability')
@rate_limit(120, 60)
def api_availability():
    query = AvailabilityQuery.from_request(
        request.args,
        default_job=DEFAULT_JOB_FILTER,
        default_sla_target_pct=get_sla_target_pct(),
    )
    report = availability_engine.get_availability(query)
    return jsonify(report.to_dict())

@app.route('/api/target-history')
@rate_limit(120, 60)
def target_history_api():
    target_url = (request.args.get('target') or request.args.get('instance') or '').strip()
    minutes_param = request.args.get('minutes', '1440')
    try:
        minutes = int(float(minutes_param))
    except (ValueError, TypeError):
        minutes = 1440
    minutes = max(1, min(minutes, 366 * 1440))  # ceiling matches /api/availability

    if not target_url:
        return jsonify({"ok": False, "error": "Target parameter is required"}), 400

    # PromQL Injection prevention: reject invalid characters
    clean_target = target_url.replace('http://', '').replace('https://', '').rstrip('/')
    if not re.match(r'^[a-zA-Z0-9.:_\-\/]+$', target_url):
        return jsonify({"ok": False, "error": "Invalid target format"}), 400

    safe_target_url = target_url.replace('"', '\\"')
    safe_clean_target = clean_target.replace('"', '\\"')

    now_ts = int(time.time())
    end_param = request.args.get('end')
    if end_param is not None:
        try:
            end_ts = int(float(end_param))
        except (TypeError, ValueError):
            end_ts = now_ts
    else:
        end_ts = now_ts

    start_ts = max(0, end_ts - (minutes * 60))
    step = max(15, min(300, int((end_ts - start_ts) / 3000)))
    
    # Every candidate is scoped by instance label — no bare 'probe_success'/'up'
    # fallback, which would pull every target's whole series just to find one.
    candidate_queries = [
        f'probe_success{{instance="{safe_target_url}"}}',
        f'probe_success{{instance=~".*{re.escape(clean_target)}.*"}}',
        f'up{{instance="{safe_target_url}"}}',
        f'up{{instance=~".*{re.escape(clean_target)}.*"}}',
    ]

    values = []
    for q in candidate_queries:
        path = f"/api/v1/query_range?query={quote(q)}&start={start_ts}&end={end_ts}&step={step}"
        raw, base = promclient.fetch_prometheus_json(path)
        if raw and raw.get('status') == 'success':
            results = raw.get('data', {}).get('result', [])
            for r in results:
                metric = r.get('metric', {})
                inst = metric.get('instance') or metric.get('target') or metric.get('url') or ''
                if inst == target_url or clean_target in inst:
                    values = r.get('values', [])
                    if values:
                        break
            if values:
                break

    events = []
    intervals_summary = None
    if values:
        intervals_summary = reconstruct_time_series_intervals(
            values,
            window_start_ts=start_ts,
            window_end_ts=end_ts,
            expected_interval_sec=step
        )

        current_state = None
        state_start_ts = None
        max_gap_sec = step * 3.0

        for i, (ts, val_str) in enumerate(values):
            val = 1 if str(val_str) in ('1', '1.0', 'up', 'true', 'True') else 0
            if current_state is None:
                current_state = val
                state_start_ts = ts
            else:
                prev_ts = values[i - 1][0]
                delta_t = ts - prev_ts
                if delta_t > max_gap_sec:
                    # Excess gap is UNKNOWN
                    duration_sec = int(prev_ts - state_start_ts + step)
                    events.append({
                        "status": "ONLINE" if current_state == 1 else "OFFLINE",
                        "start_ts": int(state_start_ts),
                        "end_ts": int(prev_ts + step),
                        "duration_seconds": max(0, duration_sec),
                        "ongoing": False
                    })
                    events.append({
                        "status": "UNKNOWN",
                        "start_ts": int(prev_ts + step),
                        "end_ts": int(ts),
                        "duration_seconds": int(ts - (prev_ts + step)),
                        "ongoing": False
                    })
                    current_state = val
                    state_start_ts = ts
                elif val != current_state:
                    duration_sec = int(ts - state_start_ts)
                    events.append({
                        "status": "ONLINE" if current_state == 1 else "OFFLINE",
                        "start_ts": int(state_start_ts),
                        "end_ts": int(ts),
                        "duration_seconds": max(0, duration_sec),
                        "ongoing": False
                    })
                    current_state = val
                    state_start_ts = ts

        if current_state is not None and state_start_ts is not None:
            is_ongoing = end_ts >= now_ts - 60
            if is_ongoing and current_state == 0:
                down_map = fetch_down_since_prom_map()
                down_ts = down_map.get(target_url)
                if not down_ts:
                    for k, v in down_map.items():
                        if clean_target in k:
                            down_ts = v
                            break
                if down_ts and down_ts > 0:
                    state_start_ts = int(down_ts)

                # A firing incident's started_at is the authoritative outage
                # start when it predates whatever the (lookback-bounded)
                # Prometheus estimate or the range samples imply — otherwise a
                # multi-week outage's "current outage" duration reads as capped
                # at the query lookback while Incident History shows the real
                # age (audit M1).
                try:
                    for a in active_incident_list():
                        a_inst = a.get('instance') or ''
                        if a_inst == target_url or (clean_target and clean_target in a_inst):
                            a_ts = _sane_epoch(a.get('time'))
                            if a_ts and a_ts < state_start_ts:
                                state_start_ts = int(a_ts)
                except Exception:
                    pass

            duration_sec = int(end_ts - state_start_ts)
            events.append({
                "status": "ONLINE" if current_state == 1 else "OFFLINE",
                "start_ts": int(state_start_ts),
                "end_ts": end_ts,
                "duration_seconds": max(0, duration_sec),
                "ongoing": is_ongoing
            })

    # Always merge all sources: Prometheus range states + logs.json + history.json
    raw_events = list(events)
    logs = json_store.load_json(json_store.LOGS_FILE, [])
    history_records = json_store.load_json(json_store.HISTORY_FILE, [])
    combined_sources = logs + history_records

    for item in combined_sources:
        inst = item.get('instance') or ''
        ts = item.get('time')
        if ts and (inst == target_url or clean_target in inst) and start_ts <= ts <= end_ts:
            ev_type = item.get('event') or item.get('status')
            is_up = ev_type in ('resolved', 'ONLINE', 'up')
            dur = item.get('duration_seconds') or 0
            start_t = int(ts - dur if dur else ts)
            
            raw_events.append({
                "status": "ONLINE" if is_up else "OFFLINE",
                "start_ts": start_t,
                "end_ts": int(ts),
                "duration_seconds": int(dur),
                "ongoing": False,
                "summary": item.get('summary') or (f"Target {'ONLINE' if is_up else 'OFFLINE'}")
            })

    # Deduplicate using composite key matching: instance + status + start_ts/end_ts window + summary
    deduped_events = []
    for candidate in raw_events:
        c_status = candidate['status']
        c_start = candidate['start_ts']
        c_end = candidate.get('end_ts', c_start)
        c_ongoing = candidate.get('ongoing', False)
        c_summary = candidate.get('summary', '')

        match = None
        for existing in deduped_events:
            if existing['status'] == c_status:
                time_close = abs(existing['start_ts'] - c_start) <= 15 or abs(existing['end_ts'] - c_end) <= 15
                summary_match = (
                    not c_summary or not existing.get('summary') or 
                    c_summary == existing.get('summary') or 
                    'Target ONLINE' in c_summary or 'Target OFFLINE' in c_summary or
                    'Target ONLINE' in existing.get('summary', '') or 'Target OFFLINE' in existing.get('summary', '')
                )
                if time_close and summary_match:
                    match = existing
                    break
                if c_ongoing and existing.get('ongoing'):
                    match = existing
                    break

        if match:
            if c_ongoing and not match.get('ongoing'):
                match['ongoing'] = True
                match['end_ts'] = candidate['end_ts']
            if c_summary and (not match.get('summary') or 'Target ONLINE' in match.get('summary', '') or 'Target OFFLINE' in match.get('summary', '')):
                match['summary'] = c_summary
            if candidate.get('duration_seconds', 0) > match.get('duration_seconds', 0):
                match['duration_seconds'] = candidate['duration_seconds']
        else:
            deduped_events.append(candidate)

    # Sort descending: Ongoing/Current event at very top (NOW), followed by historical events by end_ts / start_ts
    def get_event_sort_key(ev):
        is_ongoing = 2 if ev.get('ongoing') else 1
        end_t = ev.get('end_ts') or ev.get('start_ts') or 0
        start_t = ev.get('start_ts') or 0
        return (is_ongoing, end_t, start_t)

    deduped_events.sort(key=get_event_sort_key, reverse=True)
    events = deduped_events

    # Fetch latency range history for sparkline graph
    latency_points = []
    dur_step = max(15, int((end_ts - start_ts) / 300))
    dur_queries = [
        f'probe_duration_seconds{{instance="{target_url}"}} * 1000',
        f'probe_duration_seconds{{instance=~".*{re.escape(clean_target)}.*"}} * 1000',
        'probe_duration_seconds * 1000'
    ]
    for dq in dur_queries:
        dur_path = f"/api/v1/query_range?query={quote(dq)}&start={start_ts}&end={end_ts}&step={dur_step}"
        raw_dur, _ = promclient.fetch_prometheus_json(dur_path)
        if raw_dur and raw_dur.get('status') == 'success':
            for r in raw_dur.get('data', {}).get('result', []):
                metric = r.get('metric', {})
                inst = metric.get('instance') or metric.get('target') or metric.get('url') or ''
                if dq != 'probe_duration_seconds * 1000' or inst == target_url or clean_target in inst:
                    for tv in r.get('values', []):
                        try:
                            latency_points.append([int(tv[0]), round(float(tv[1]), 1)])
                        except (ValueError, TypeError):
                            pass
                    if latency_points:
                        break
            if latency_points:
                break

    if not latency_points:
        logs = json_store.load_json(json_store.LOGS_FILE, [])
        log_points = []
        for l in logs:
            inst = l.get('instance') or ''
            ts = l.get('time')
            lat = l.get('latency_ms')
            if ts and lat is not None and (inst == target_url or clean_target in inst):
                if start_ts <= ts <= end_ts:
                    log_points.append([int(ts), round(float(lat), 1)])
        log_points.sort(key=lambda x: x[0])
        latency_points = log_points

    return jsonify({
        "ok": True,
        "target": target_url,
        "period_minutes": minutes,
        "events": events,
        "latency_points": latency_points,
        "intervals_summary": intervals_summary
    })



@app.route('/health/live')
def health_live():
    """Liveness probe: returns 200 if the Flask process is running and able to handle HTTP requests."""
    return jsonify({"ok": True, "status": "alive"}), 200

@app.route('/health/ready')
def health_ready():
    """Readiness probe: returns 200 if storage files are writable and application is ready."""
    storage_ok = all(
        os.access(p, os.W_OK) for p in (json_store.STATUS_FILE, json_store.LOGS_FILE, json_store.HISTORY_FILE)
        if os.path.exists(p)
    )
    if not storage_ok:
        return jsonify({"ok": False, "status": "storage_unwritable"}), 503
    return jsonify({"ok": True, "status": "ready"}), 200

@app.route('/health')
def health():
    """Self-monitoring for InfraWatch's own components — Phase 13. Reused as
    both the container healthcheck (docker-compose) and the dashboard's
    self-status widget, so it stays a single source of truth.
    Returns HTTP 503 only if local storage is unwritable."""
    now = time.time()

    raw, prom_base = promclient.fetch_prometheus_json('/api/v1/targets', use_cache=True)
    prometheus_ok = raw is not None and raw.get('status') == 'success'

    storage_ok = all(
        os.access(p, os.W_OK) for p in (json_store.STATUS_FILE, json_store.LOGS_FILE, json_store.HISTORY_FILE)
        if os.path.exists(p)
    )

    components = {
        "prometheus":     {"ok": prometheus_ok, "url": prom_base},
        "monitoring_api":  {"ok": True},
        "alarm_service":   target_poller.get_health(now=now),
        "availability_aggregator": availability_aggregator.get_health(now=now),
        "storage":         {"ok": storage_ok},
    }
    overall_ok = all(c["ok"] for c in components.values())

    status_code = 503 if not storage_ok else 200

    return jsonify({
        "ok": overall_ok,
        "server_time": now,
        "components": components,
    }), status_code

# ── Background workers ──────────────────────────────────────────────────────
# The Prometheus-native alert poller (compute_state_transitions /
# compute_slow_response_transitions / _poll_targets_once / _poller_loop) and
# the availability aggregator (_availability_aggregation_windows /
# _aggregate_availability_cycle / its loop), plus start_alert_poller /
# start_availability_aggregator, the _poller_state / _slow_poller_state /
# _maintenance_active_prev dicts, the _LAST_*_TICK heartbeats and the
# ALERT_POLL_INTERVAL_SECONDS / AVAIL_* constants all live in
# background_workers.py (re-imported above; callers and tests use the bare
# names). The DISABLE_ALERT_POLLER boot gate stays here.


_reconcile_status_json_into_sqlite()

if os.environ.get("DISABLE_ALERT_POLLER") != "1":
    start_alert_poller()

if os.environ.get("DISABLE_AVAILABILITY_AGGREGATOR") != "1" and os.environ.get("DISABLE_ALERT_POLLER") != "1":
    start_availability_aggregator()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, threaded=True)
