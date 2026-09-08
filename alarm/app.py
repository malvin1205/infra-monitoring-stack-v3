from flask import Flask, request, jsonify, render_template, session, g
import time
import os
import math
import re
import threading
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

sys.path.insert(0, os.path.dirname(__file__))
try:
    from fleet_availability import (
        summarize_entries, reconstruct_time_series_intervals, calculate_percentile,
        clip_hourly_bucket, merge_hybrid_target_availability, merge_hybrid_fleet_availability,
        derive_bucket_inputs, estimate_instance_cadence, sla_budget, get_sla_target_pct,
        get_availability_settings, save_availability_settings, classify_probe_failure
    )
except ImportError:
    from alarm.fleet_availability import (
        summarize_entries, reconstruct_time_series_intervals, calculate_percentile,
        clip_hourly_bucket, merge_hybrid_target_availability, merge_hybrid_fleet_availability,
        derive_bucket_inputs, estimate_instance_cadence, sla_budget, get_sla_target_pct,
        get_availability_settings, save_availability_settings, classify_probe_failure
    )

try:
    from storage import (
        init_db, IncidentRepository, EventLogRepository,
        MaintenanceRepository, DependencyRepository, EndpointRepository, DeletedTargetRepository,
        AvailabilityBucketRepository, AggregationLeaseRepository, SlaTargetRepository, SlowThresholdRepository,
        UserRepository, AcknowledgmentRepository, AuditLogRepository
    )
except ImportError:
    from alarm.storage import (
        init_db, IncidentRepository, EventLogRepository,
        MaintenanceRepository, DependencyRepository, EndpointRepository, DeletedTargetRepository,
        AvailabilityBucketRepository, AggregationLeaseRepository, SlaTargetRepository, SlowThresholdRepository,
        UserRepository, AcknowledgmentRepository, AuditLogRepository
    )

try:
    from telegram_notifier import (
        get_telegram_config, save_telegram_config, test_telegram_connection
    )
except ImportError:
    from alarm.telegram_notifier import (
        get_telegram_config, save_telegram_config, test_telegram_connection
    )

try:
    from auth import (
        API_KEY, require_api_key, require_webhook_secret, get_api_key, get_webhook_secret,
        get_session_secret, hash_password, verify_password, get_current_authenticated_user,
        has_permission, require_permission, require_auth, require_admin, ROLE_PERMISSIONS
    )
except ImportError:
    from alarm.auth import (
        API_KEY, require_api_key, require_webhook_secret, get_api_key, get_webhook_secret,
        get_session_secret, hash_password, verify_password, get_current_authenticated_user,
        has_permission, require_permission, require_auth, require_admin, ROLE_PERMISSIONS
    )

try:
    from config import (
        DEFAULT_JOB_FILTER, ALERTNAME_TARGET_DOWN, ALERTNAME_SLOW_RESPONSE,
        DEFAULT_SLOW_RESPONSE_THRESHOLD_MS, SCRAPE_INTERVAL_SECONDS,
        _AVAIL_FRESHNESS_TOLERANCE_SEC, _AVAIL_STALE_BUCKET_TOLERANCE_SEC,
    )
    import json_store
    from json_store import load_json, save_json  # re-export for `from app import …`
    import website_targets
    from website_targets import (
        get_targets_file, _targets_write_lock, WEBSITES_JOB_LABEL,
        _WEBSITE_TARGETS_CACHE, _WEBSITE_TARGETS_CACHE_LOCK,
        load_website_targets, save_website_targets,
        load_deleted_targets, save_deleted_targets,
    )
    from rate_limit import (
        rate_limit, _client_identity, _RATE_BUCKETS, _RATE_BUCKETS_LOCK,
        _RATE_LAST_PRUNE, _RATE_PRUNE_INTERVAL, _LOGIN_FAILS, _LOGIN_FAILS_LOCK,
        _LOGIN_MAX_FAILS, _LOGIN_LOCK_SECONDS, _login_locked, _login_note_failure,
        _login_clear,
    )
    from web_middleware import register_web_middleware
    import ssrf
    from ssrf import _is_blocked_ip, is_safe_endpoint_url
    from monitoring_primitives import (
        parse_alert_timestamp, alert_key, TARGET_HOST_RE, _BLOCKED_TARGET_HOSTS,
        is_valid_target, matches_job_filter, normalize_target, _parse_epoch_ts,
        _sane_epoch, _earliest_outage_start, classify_scrape_failure,
        _extract_host, find_node_exporter_status, OUTAGE_GRACE_SECONDS,
        _outage_past_grace, _PROM_DURATION_RE, _parse_prom_duration_sec,
    )
    import prometheus_client as promclient
    from prometheus_client import (
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
    import prom_queries
    from prom_queries import (
        fetch_prom_query_map, fetch_prom_range_map,
        fetch_down_since_prom_map, fetch_all_probe_metrics,
    )
    import alerts
    from alerts import (
        _WEBHOOK_LOCK, _LAST_WEBHOOK_AT, active_incident_list,
        _request_memo, _invalidate_maint_cache, _invalidate_dep_cache,
        load_maintenance_windows, maintenance_windows_by_instance,
        get_active_maintenance, record_alert_event,
        load_dependencies, apply_correlation_suppression,
    )
    import monitoring_state
    from monitoring_state import (
        _derive_probe_readings, build_canonical_monitoring_state,
        get_instance_job_map, get_instance_cadence_map, get_monitored_instances,
    )
    import availability
    from availability import (
        _attach_sla_budgets, _availability_status_counts, _build_fleet_trend,
        _FLEET_TREND_CACHE, _FLEET_TREND_CACHE_TTL,
    )
except ImportError:
    from alarm.config import (
        DEFAULT_JOB_FILTER, ALERTNAME_TARGET_DOWN, ALERTNAME_SLOW_RESPONSE,
        DEFAULT_SLOW_RESPONSE_THRESHOLD_MS, SCRAPE_INTERVAL_SECONDS,
        _AVAIL_FRESHNESS_TOLERANCE_SEC, _AVAIL_STALE_BUCKET_TOLERANCE_SEC,
    )
    from alarm import json_store
    from alarm.json_store import load_json, save_json
    from alarm import website_targets
    from alarm.website_targets import (
        get_targets_file, _targets_write_lock, WEBSITES_JOB_LABEL,
        _WEBSITE_TARGETS_CACHE, _WEBSITE_TARGETS_CACHE_LOCK,
        load_website_targets, save_website_targets,
        load_deleted_targets, save_deleted_targets,
    )
    from alarm.rate_limit import (
        rate_limit, _client_identity, _RATE_BUCKETS, _RATE_BUCKETS_LOCK,
        _RATE_LAST_PRUNE, _RATE_PRUNE_INTERVAL, _LOGIN_FAILS, _LOGIN_FAILS_LOCK,
        _LOGIN_MAX_FAILS, _LOGIN_LOCK_SECONDS, _login_locked, _login_note_failure,
        _login_clear,
    )
    from alarm.web_middleware import register_web_middleware
    from alarm import ssrf
    from alarm.ssrf import _is_blocked_ip, is_safe_endpoint_url
    from alarm.monitoring_primitives import (
        parse_alert_timestamp, alert_key, TARGET_HOST_RE, _BLOCKED_TARGET_HOSTS,
        is_valid_target, matches_job_filter, normalize_target, _parse_epoch_ts,
        _sane_epoch, _earliest_outage_start, classify_scrape_failure,
        _extract_host, find_node_exporter_status, OUTAGE_GRACE_SECONDS,
        _outage_past_grace, _PROM_DURATION_RE, _parse_prom_duration_sec,
    )
    from alarm import prometheus_client as promclient
    from alarm.prometheus_client import (
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
    from alarm import prom_queries
    from alarm.prom_queries import (
        fetch_prom_query_map, fetch_prom_range_map,
        fetch_down_since_prom_map, fetch_all_probe_metrics,
    )
    from alarm import alerts
    from alarm.alerts import (
        _WEBHOOK_LOCK, _LAST_WEBHOOK_AT, active_incident_list,
        _request_memo, _invalidate_maint_cache, _invalidate_dep_cache,
        load_maintenance_windows, maintenance_windows_by_instance,
        get_active_maintenance, record_alert_event,
        load_dependencies, apply_correlation_suppression,
    )
    from alarm import monitoring_state
    from alarm.monitoring_state import (
        _derive_probe_readings, build_canonical_monitoring_state,
        get_instance_job_map, get_instance_cadence_map, get_monitored_instances,
    )
    from alarm import availability
    from alarm.availability import (
        _attach_sla_budgets, _availability_status_counts, _build_fleet_trend,
        _FLEET_TREND_CACHE, _FLEET_TREND_CACHE_TTL,
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

# websites.yml curation list + deleted-target tombstones — get_targets_file,
# load/save_website_targets, load/save_deleted_targets, _targets_write_lock,
# WEBSITES_JOB_LABEL, _WEBSITE_TARGETS_CACHE — live in website_targets.py
# (re-imported above; callers use the bare names so patch('app.load_website_targets')
# still works).

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
            actor_role="admin",
            action="SYSTEM_SETUP",
            resource="user:admin",
            details="Initial administrator account created"
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

    if role is not None:
        role = str(role).strip().lower()
        if role not in ("admin", "viewer"):
            return jsonify({"ok": False, "error": "Role must be admin or viewer"}), 400
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
        state = build_canonical_monitoring_state("all")
        target_list = [t["instance"] for t in state.get("targets", []) if t.get("health") != "up" and not t.get("maintenance") and not t.get("acknowledged")]

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
    state = build_canonical_monitoring_state()
    return jsonify({
        "status": state.get("system_status", "NORMAL"),
        "system_status": state.get("system_status", "NORMAL"),
        "alerts": state.get("active_alerts", []),
        "summary": state.get("summary", {}),
        "updated": state.get("updated", time.time())
    })

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
    config_web_targets = load_website_targets()
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
    
    for target_url in config_web_targets:
        if target_url not in seen:
            seen.add(target_url)
            prom_list.append({
                "instance": target_url,
                "isDeleted": target_url in deleted_targets,
                "job": "custom"
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
    # tombstoned targets instead of them just vanishing forever (S3). A
    # re-POST of any deleted url clears its tombstone.
    return jsonify({
        "ok": True,
        "targets": load_website_targets(),
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
    added = False
    try:
        with _WEBHOOK_LOCK, _targets_write_lock():
            # Restore from tombstone if it was previously deleted.
            deleted = load_deleted_targets()
            drop = [d for d in deleted if normalize_target(d) == norm]
            if drop:
                for d in drop:
                    deleted.remove(d)
                save_deleted_targets(deleted)

            current = load_website_targets()
            if not any(normalize_target(c) == norm for c in current):
                current.append(url)
                save_website_targets(current)
                added = True
    except Exception as e:
        logger.error("add_target_api failed for %s: %s", url, e)
        return jsonify({"ok": False, "error": "Could not persist target — see server log"}), 500

    AuditLogRepository.record_action(
        actor_username=g.current_user.get("username", "admin"),
        actor_role=g.current_user.get("role", "admin"),
        action="ADD_TARGET",
        resource=url,
        details="Added website/IP target"
    )
    resp = {"ok": True, "targets": current}
    # Tell the caller what actually changed — pinning an already-monitored,
    # non-deleted target is a no-op, and the old code returned a bare success
    # that read as "something happened" (audit F13).
    if drop and added:
        resp["message"] = "Target restored and pinned to the wallboard."
    elif drop:
        resp["message"] = "Target restored — it was previously removed."
    elif added:
        resp["message"] = "Target pinned to the wallboard."
    else:
        resp["message"] = "No change — this target is already monitored."
    # websites.yml is a curation list, not a scrape config: this target only
    # shows live data once the operator's (external) Prometheus actually
    # scrapes it. On a genuine add, warn if we can see it isn't in the current
    # target set (skipped on an idempotent re-add — nothing changed).
    if added:
        discovered = _prometheus_discovered_instances()
        if discovered and url not in discovered and norm not in {normalize_target(d) for d in discovered}:
            resp["warning"] = ("Prometheus is not currently scraping this target — it will show as "
                               "Unknown on the wallboard until your Prometheus scrape config picks it up.")
    return jsonify(resp)

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
        with _WEBHOOK_LOCK, _targets_write_lock():
            current = load_website_targets()
            keep = [c for c in current if normalize_target(c) != norm]
            if len(keep) != len(current):
                save_website_targets(keep)
            current = keep

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
    return jsonify({"ok": True, "restorable": True})

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
    job_param = request.args.get('job', DEFAULT_JOB_FILTER)
    state = build_canonical_monitoring_state(job_param)
    if not state.get('ok') and state.get('error'):
        return jsonify(state), 503
    return jsonify(state)

# ── Availability (historical uptime %) ────────────────────────────────────────
# _attach_sla_budgets(), _availability_status_counts() and _build_fleet_trend()
# (+ _FLEET_TREND_CACHE / _FLEET_TREND_CACHE_TTL) live in availability.py —
# re-imported above so `alarm_app._build_fleet_trend` etc. stay patchable. The
# /api/availability route below (cache / single-flight / hybrid orchestration)
# calls them as bare names.


@app.route('/api/availability')
@rate_limit(120, 60)
def api_availability():
    t_req_start = time.perf_counter()
    job_filter = request.args.get('job', DEFAULT_JOB_FILTER)
    minutes_param = request.args.get('minutes')
    if minutes_param is not None:
        try:
            minutes = float(minutes_param)
        except (TypeError, ValueError):
            minutes = 1440.0
    else:
        try:
            days = float(request.args.get('days', 1))
        except (TypeError, ValueError):
            days = 1.0
        minutes = days * 1440.0
    minutes = max(1.0, min(minutes, 366 * 1440.0))
    minutes_int = int(round(minutes))

    # SLA error-budget target/period (optional overrides; default 99.9% / 30d)
    try:
        sla_target_pct = max(0.0, min(100.0, float(request.args.get('sla_target'))))
    except (TypeError, ValueError):
        sla_target_pct = get_sla_target_pct()
    try:
        sla_days = max(1, min(365, int(float(request.args.get('sla_days', 30)))))
    except (TypeError, ValueError):
        sla_days = 30
    try:
        sla_target_map = SlaTargetRepository.get_all()
    except Exception:
        sla_target_map = {}
    sla_map_sig = hash(tuple(sorted(sla_target_map.items()))) if sla_target_map else 0

    end_ts = None
    end_param = request.args.get('end')
    if end_param is not None:
        try:
            end_ts = int(float(end_param))
        except (TypeError, ValueError):
            end_ts = None

    avail_cache_ttl = 15.0 if minutes_int >= 1440 else 5.0

    # Derive normalized bucketed cache key
    endpoints_data = load_endpoints()
    active_url = endpoints_data.get("active") or _DEFAULT_PROM_URL
    norm_job = (job_filter or DEFAULT_JOB_FILTER).strip().lower()

    now = time.time()
    _maybe_prune_cache(now)

    if end_ts is not None:
        norm_end = f"hist_{end_ts}"
        effective_ttl = 60.0  # Fixed historical range is immutable
    else:
        bucket_sec = 15 if minutes_int >= 1440 else 5
        bucket_ts = int(now // bucket_sec) * bucket_sec
        norm_end = f"live_{bucket_ts}"
        effective_ttl = avail_cache_ttl

    avail_cache_key = f"avail:{active_url}:{norm_job}:{minutes_int}:{norm_end}:sla{sla_target_pct}/{sla_days}/{sla_map_sig}"

    # 1. Fast path: server in-memory availability cache hit
    t_cache_check_start = time.perf_counter()
    with _AVAILABILITY_CACHE_LOCK:
        if avail_cache_key in _AVAILABILITY_CACHE:
            cached_ts, cached_payload = _AVAILABILITY_CACHE[avail_cache_key]
            if now - cached_ts < effective_ttl:
                return jsonify(cached_payload)

    # 2. Single-flight lock: coalesces concurrent identical requests
    t_lock_wait_start = time.perf_counter()
    with _avail_flight_lock_for(avail_cache_key):
        t_lock_acquired = time.perf_counter()
        flight_wait_ms = (t_lock_acquired - t_lock_wait_start) * 1000.0

        # Re-check under lock in case previous thread just computed it
        now_under_lock = time.time()
        with _AVAILABILITY_CACHE_LOCK:
            if avail_cache_key in _AVAILABILITY_CACHE:
                cached_ts, cached_payload = _AVAILABILITY_CACHE[avail_cache_key]
                if now_under_lock - cached_ts < effective_ttl:
                    return jsonify(cached_payload)

        req_end = float(end_ts) if end_ts is not None else now
        req_start = req_end - (minutes * 60.0)
        monitored_instances = get_monitored_instances(job_filter=job_filter)

        if not monitored_instances:
            empty_summary = summarize_entries([], minutes)
            payload = {
                "ok": True,
                "period_minutes": round(minutes, 2),
                "requested_window_seconds": round(minutes * 60.0, 1),
                "coverage_seconds": 0.0,
                "unknown_seconds": round(minutes * 60.0, 1),
                "sqlite_seconds": 0.0,
                "prometheus_seconds": 0.0,
                "overlap_removed_seconds": 0.0,
                "coverage_percent": 0.0,
                "availability_percent": None,
                "data_status": "NO_DATA",
                "end": end_ts,
                "counts": {
                    "total": 0,
                    "scored": 0,
                    "eligible": 0,
                    "online": 0,
                    "warning": 0,
                    "offline": 0,
                },
                "overall": None,
                "sla": sla_budget(0.0, 0.0, sla_target_pct, minutes * 60.0, sla_days),
                "fleet_aggregate": empty_summary["fleet_aggregate"],
                "fleet_average": empty_summary["fleet_average"],
                "health_ratio": empty_summary["health_ratio"],
                "zero_downtime_ratio": empty_summary.get("zero_downtime_ratio"),
                "sla_compliance": empty_summary.get("sla_compliance"),
                "sla_compliance_ratio": empty_summary.get("sla_compliance_ratio"),
                "coverage_ratio": empty_summary.get("coverage_ratio"),
                "per_server": empty_summary["per_server"],
                "lowest_availability": [],
                "hosts_requiring_attention": [],
                "entries": [],
                "targets": {},
                "analytics": empty_summary.get("analytics", {}),
                "trend": [],
                "trend_end_ts": int(req_end),
                "trend_start_ts": int(req_end - minutes * 60.0),
                "trend_bucket_seconds": 3600,
                "source": "nodata"
            }
            with _AVAILABILITY_CACHE_LOCK:
                _AVAILABILITY_CACHE[avail_cache_key] = (now_under_lock, payload)
            return jsonify(payload)

        # Maintenance windows overlapping this query window -> carved out of
        # the SLA denominator downstream. Skip entirely (and skip the job-map
        # lookup) when there are none.
        _maint_windows = [
            w for w in load_maintenance_windows()
            if _parse_epoch_ts(w.get('end_epoch') if w.get('end_epoch') is not None else w.get('end', 0)) > req_start
            and _parse_epoch_ts(w.get('start_epoch') if w.get('start_epoch') is not None else w.get('start', 0)) < req_end
        ]
        maint_by_inst = None
        if _maint_windows:
            _needs_job = any((w.get('scope') or w.get('scope_type')) == 'job' for w in _maint_windows)
            _job_map = get_instance_job_map(job_filter) if _needs_job else {}
            maint_by_inst = maintenance_windows_by_instance(monitored_instances, _job_map, _maint_windows) or None

        # 3. Retrieve SQLite bucket records in the window [req_start, req_end]
        t_sqlite_start = time.perf_counter()
        db_bucket_records = AvailabilityBucketRepository.get_bucket_records(
            job=job_filter,
            start_time=req_start,
            end_time=req_end,
            instances=monitored_instances
        )
        t_sqlite_end = time.perf_counter()
        sqlite_duration_ms = (t_sqlite_end - t_sqlite_start) * 1000.0

        # Check if SQLite completely covers the requested window for all monitored instances
        is_sqlite_fully_complete = False
        if db_bucket_records and monitored_instances:
            instances_in_db = set()
            instance_spans = {}
            newest_bucket_update = 0.0
            for b in db_bucket_records:
                inst = b.get("instance")
                cov_sec = float(b.get("coverage_seconds", 0) or 0)
                if inst in monitored_instances and cov_sec > 0:
                    instances_in_db.add(inst)
                    st = float(b.get("bucket_start", 0))
                    en = float(b.get("bucket_end", 0))
                    try:
                        newest_bucket_update = max(newest_bucket_update, float(b.get("updated_at", 0) or 0))
                    except (TypeError, ValueError):
                        pass
                    cur = instance_spans.get(inst)
                    if cur is None:
                        instance_spans[inst] = (st, en, 1)
                    else:
                        instance_spans[inst] = (min(cur[0], st), max(cur[1], en), cur[2] + 1)

            # For a live window (no ?end=), a bucket's stored span always reads as
            # current — an in-progress hour is written with a future hour-end —
            # so the span check alone can't tell a healthy archive from one the
            # aggregator silently stopped refreshing. Require the newest matched
            # bucket to have been rewritten recently too; otherwise drop to the
            # live Prometheus hybrid path rather than serve an ageing archive as
            # COMPLETE. Fixed historical ranges (end_ts set) are immutable once
            # materialized, so this staleness gate does not apply to them.
            aggregator_fresh = (
                end_ts is not None
                or (newest_bucket_update > 0.0 and (now - newest_bucket_update) < _AVAIL_STALE_BUCKET_TOLERANCE_SEC)
            )

            if len(instances_in_db) == len(monitored_instances) and aggregator_fresh:
                expected_hours = max(1, int(round((req_end - req_start) / 3600.0)))
                all_covered = True
                for inst in monitored_instances:
                    sp = instance_spans.get(inst)
                    if not sp or sp[0] > (req_start + 60.0) or sp[1] < (req_end - _AVAIL_FRESHNESS_TOLERANCE_SEC) or sp[2] < max(1, expected_hours - 1):
                        all_covered = False
                        break
                if all_covered:
                    is_sqlite_fully_complete = True

        if is_sqlite_fully_complete:
            # Full SQLite coverage fast path: compute per-target clipped intervals with 0 Prometheus queries
            t_merge_start = time.perf_counter()
            entries, summary_dict = merge_hybrid_fleet_availability(
                req_start=req_start,
                req_end=req_end,
                monitored_instances=monitored_instances,
                sqlite_buckets=db_bucket_records,
                prom_results_map={},
                expected_interval_sec=SCRAPE_INTERVAL_SECONDS,
                maintenance_by_instance=maint_by_inst,
                sla_threshold_by_instance=sla_target_map,
            )
            t_merge_end = time.perf_counter()
            merge_duration_ms = (t_merge_end - t_merge_start) * 1000.0

            fleet_sla_budget = _attach_sla_budgets(summary_dict, minutes * 60.0, sla_target_pct, sla_days, target_map=sla_target_map)
            hybrid_meta = summary_dict.get("hybrid", {})
            # Same online/warning/offline rule as the hybrid path. Only pay for a
            # live probe_success snapshot if an entry actually has no historical
            # coverage (rare on the fully-materialized fast path) — otherwise
            # this stays a zero-Prometheus-query path.
            if any(e.get("availability_pct") is None for e in entries):
                counts = _availability_status_counts(entries, fetch_prom_query_map("probe_success"))
            else:
                counts = _availability_status_counts(entries)

            lowest_availability = sorted(
                [e for e in summary_dict['per_server']['values'] if e.get('availability_pct') is not None and e['availability_pct'] < 100.0],
                key=lambda e: (
                    e['availability_pct'],
                    -(e.get('downtime_minutes') or 0.0),
                    -(e.get('incidents') or 0)
                )
            )

            trend_series, trend_slot_sec = _build_fleet_trend(req_end, minutes * 60.0, monitored_instances)

            t_ser_start = time.perf_counter()
            total_backend_ms = (t_ser_start - t_req_start) * 1000.0

            trace_data = {
                "route": "/api/availability",
                "path": "sqlite_fast_path",
                "minutes": minutes_int,
                "flight_wait_ms": round(flight_wait_ms, 2),
                "sqlite_duration_ms": round(sqlite_duration_ms, 2),
                "sqlite_record_count": len(db_bucket_records),
                "prom_duration_ms": 0.0,
                "prom_query_count": 0,
                "prom_queries": {},
                "hybrid_merge_ms": round(merge_duration_ms, 2),
                "materialize_ms": 0.0,
                "total_backend_ms": round(total_backend_ms, 2),
            }

            payload = {
                "ok": True,
                "period_minutes": round(minutes, 2),
                "requested_window_seconds": hybrid_meta.get("requested_window_seconds", round(minutes * 60.0, 1)),
                "coverage_seconds": hybrid_meta.get("coverage_seconds", 0.0),
                "unknown_seconds": hybrid_meta.get("unknown_seconds", 0.0),
                "missing_seconds": hybrid_meta.get("missing_seconds", hybrid_meta.get("unknown_seconds", 0.0)),
                "sqlite_seconds": hybrid_meta.get("sqlite_seconds", 0.0),
                "prometheus_seconds": hybrid_meta.get("prometheus_seconds", 0.0),
                "overlap_removed_seconds": hybrid_meta.get("overlap_removed_seconds", 0.0),
                "maintenance_excluded_seconds": hybrid_meta.get("maintenance_excluded_seconds", 0.0),
                "maintenance_scheduled_seconds": hybrid_meta.get("maintenance_scheduled_seconds", 0.0),
                "sla": fleet_sla_budget,
                "coverage_percent": hybrid_meta.get("coverage_percent", 0.0),
                "availability_percent": summary_dict['fleet_aggregate']['value'],
                "data_status": hybrid_meta.get("data_status", "COMPLETE"),
                "telemetry_audit": hybrid_meta.get("telemetry_audit", summary_dict.get("telemetry_audit", {})),
                "end": end_ts,
                "counts": {
                    "total": len(monitored_instances),
                    "scored": summary_dict.get('scored_count', 0),
                    "eligible": summary_dict.get('eligible_count', 0),
                    "online": counts['online'],
                    "warning": counts['warning'],
                    "offline": counts['offline'],
                },
                "overall": summary_dict['fleet_aggregate']['value'],
                "fleet_aggregate": summary_dict['fleet_aggregate'],
                "fleet_average": summary_dict['fleet_average'],
                "health_ratio": summary_dict['health_ratio'],
                "zero_downtime_ratio": summary_dict.get('zero_downtime_ratio'),
                "sla_compliance": summary_dict.get('sla_compliance'),
                "sla_compliance_ratio": summary_dict.get('sla_compliance_ratio'),
                "coverage_ratio": summary_dict.get('coverage_ratio'),
                "per_server": summary_dict['per_server'],
                "lowest_availability": lowest_availability,
                "hosts_requiring_attention": lowest_availability,
                "entries": summary_dict['per_server']['values'],
                "targets": {e['id']: e['availability_pct'] for e in summary_dict['per_server']['values']},
                "analytics": summary_dict.get('analytics', {}),
                "trend": trend_series,
                "trend_end_ts": int(req_end),
                "trend_start_ts": int(req_end - minutes * 60.0),
                "trend_bucket_seconds": trend_slot_sec,
                "source": "materialized",
                "_trace": trace_data,
            }
            # F57: the per-request timing block is a debugging aid, not UI data —
            # keep it out of the cached/served payload unless explicitly asked.
            if not request.args.get("debug"):
                payload.pop("_trace", None)
            with _AVAILABILITY_CACHE_LOCK:
                _AVAILABILITY_CACHE[avail_cache_key] = (now_under_lock, payload)

            resp = jsonify(payload)
            t_ser_end = time.perf_counter()
            trace_data["serialization_ms"] = round((t_ser_end - t_ser_start) * 1000.0, 2)
            trace_data["total_backend_ms"] = round((t_ser_end - t_req_start) * 1000.0, 2)
            return resp

        # 4. Hybrid Path: Fetch raw authoritative telemetry from Prometheus and merge with SQLite
        at_suffix = f" @ {end_ts}" if end_ts is not None else ""
        req_timeout = max(4.0, min(15.0, minutes / 1500.0))

        queries = {
            'probe_avail': f"avg_over_time(probe_success[{minutes_int}m]{at_suffix}) * 100",
            'up_avail': f"avg_over_time(up[{minutes_int}m]{at_suffix}) * 100",
            'probe_count': f"count_over_time(probe_success[{minutes_int}m]{at_suffix})",
            'up_count': f"count_over_time(up[{minutes_int}m]{at_suffix})",
            'duration': f"avg_over_time(probe_duration_seconds[{minutes_int}m]{at_suffix}) * 1000",
            'probe_incidents': f"changes(probe_success[{minutes_int}m]{at_suffix})",
            'up_incidents': f"changes(up[{minutes_int}m]{at_suffix})",
            'live_probe': "probe_success" if end_ts is None else None,
            'live_up': "up" if end_ts is None else None
        }
        if minutes_int <= 60:
            queries['probe_first_ts'] = f"min_over_time(timestamp(probe_success)[{minutes_int}m:]{at_suffix})"
            queries['probe_last_ts'] = f"max_over_time(timestamp(probe_success)[{minutes_int}m:]{at_suffix})"
            queries['up_first_ts'] = f"min_over_time(timestamp(up)[{minutes_int}m:]{at_suffix})"
            queries['up_last_ts'] = f"max_over_time(timestamp(up)[{minutes_int}m:]{at_suffix})"

        def _call_query(expr, ttl, to):
            t_q_start = time.perf_counter()
            res = fetch_prom_query_map(expr, cache_ttl=ttl, timeout=to)
            t_q_end = time.perf_counter()
            return res, (t_q_end - t_q_start) * 1000.0

        t_prom_start = time.perf_counter()
        futures = {k: _SHARED_EXECUTOR.submit(_call_query, q, avail_cache_ttl, req_timeout) for k, q in queries.items() if q}
        results = {}
        prom_query_timings = {}
        for k, f in futures.items():
            try:
                res, q_dur = f.result()
                results[k] = res
                prom_query_timings[k] = round(q_dur, 2)
            except Exception as ex:
                results[k] = {}
                prom_query_timings[k] = -1.0
        t_prom_end = time.perf_counter()
        prom_duration_ms = (t_prom_end - t_prom_start) * 1000.0

        # Classify each instance as probe vs node/exporter-style and merge
        # the matching side's query results — shared with the background
        # aggregator's identical step via derive_bucket_inputs().
        probe_results_raw = {
            "avail": results.get('probe_avail', {}), "count": results.get('probe_count', {}),
            "first_ts": results.get('probe_first_ts', {}), "last_ts": results.get('probe_last_ts', {}),
            "incidents": results.get('probe_incidents', {}), "live": results.get('live_probe', {}),
        }
        up_results_raw = {
            "avail": results.get('up_avail', {}), "count": results.get('up_count', {}),
            "first_ts": results.get('up_first_ts', {}), "last_ts": results.get('up_last_ts', {}),
            "incidents": results.get('up_incidents', {}), "live": results.get('live_up', {}),
        }
        merged_maps = derive_bucket_inputs(monitored_instances, probe_results_raw, up_results_raw)
        avail_map = merged_maps["avail"]
        count_map = merged_maps["count"]
        first_ts_map = merged_maps["first_ts"]
        last_ts_map = merged_maps["last_ts"]
        incidents_map = merged_maps["incidents"]
        live_map = merged_maps["live"]

        duration_map = results.get('duration', {})

        # Per-instance cadence — NOT a fleet-wide median. A mixed fleet has
        # 60s ping targets and 15s exporter targets; collapsing that to one
        # number under-counts whichever job doesn't match it. Prefer the
        # directly observed cadence (span between first/last sample over
        # sample count) when first_ts/last_ts were fetched (windows <= 60m);
        # otherwise — and always as a fallback — use the target's real
        # scrapeInterval from Prometheus's own /api/v1/targets (see
        # get_instance_cadence_map). Previously windows > 60m always skipped
        # first_ts/last_ts and fell back straight to SCRAPE_INTERVAL_SECONDS
        # (2.0s) instead, starving 24h/7d/30d queries of ~97% of their real
        # coverage.
        cadence_map = dict(get_instance_cadence_map(job_filter))
        for inst in monitored_instances:
            estimated = estimate_instance_cadence(inst, count_map, first_ts_map, last_ts_map)
            if estimated is not None:
                cadence_map[inst] = estimated

        prom_results_map = {
            "first_ts": first_ts_map,
            "last_ts": last_ts_map,
            "count": count_map,
            "avail": avail_map,
            "incidents": incidents_map,
            "duration": duration_map,
        }

        # Perform clean hybrid per-target merge
        t_merge_start = time.perf_counter()
        entries, summary_dict = merge_hybrid_fleet_availability(
            req_start=req_start,
            req_end=req_end,
            monitored_instances=monitored_instances,
            sqlite_buckets=db_bucket_records,
            prom_results_map=prom_results_map,
            expected_interval_sec=cadence_map,
            maintenance_by_instance=maint_by_inst,
            sla_threshold_by_instance=sla_target_map,
        )
        t_merge_end = time.perf_counter()
        merge_duration_ms = (t_merge_end - t_merge_start) * 1000.0
        fleet_sla_budget = _attach_sla_budgets(summary_dict, minutes * 60.0, sla_target_pct, sla_days, target_map=sla_target_map)
        hybrid_meta = summary_dict.get("hybrid", {})

        # Materialize completed hourly buckets — TEST-ONLY (audit F3). In
        # production the background aggregator (_aggregate_availability_cycle)
        # is the sole materializer; running this per hybrid request and
        # discarding the result (it is only persisted under TESTING) was pure
        # wasted CPU on the frontend's 15s poll.
        materialized_buckets = []
        h_start = math.floor(req_start / 3600.0) * 3600.0
        h_end = math.ceil(req_end / 3600.0) * 3600.0
        if h_end > h_start and app.config.get('TESTING'):
            num_hours = max(1, int(round((h_end - h_start) / 3600.0)))
            for inst in monitored_instances:
                rc = count_map.get(inst)
                raw_avail_val = avail_map.get(inst)
                if rc is None and raw_avail_val is None:
                    continue
                avail_pct = float(raw_avail_val) if raw_avail_val is not None else None
                if avail_pct is not None:
                    up_rate = min(1.0, max(0.0, float(avail_pct) / 100.0))
                    down_rate = round(1.0 - up_rate, 6)
                else:
                    up_rate = 0.0
                    down_rate = 0.0
                inc_val = incidents_map.get(inst)
                inc_cnt = int(math.ceil(float(inc_val) / 2.0)) if inc_val else 0
                dur_val = duration_map.get(inst)
                lat_ms = round(float(dur_val), 1) if dur_val is not None else 0.0
                s_cnt_int = int(float(rc)) if rc is not None else 0

                cur_h = h_start
                while cur_h < h_end:
                    nxt_h = cur_h + 3600.0
                    if avail_pct is not None:
                        h_cov = 3600.0
                        h_down = round(h_cov * down_rate, 2)
                        h_up = max(0.0, round(h_cov - h_down, 2))
                        h_unk = 0.0
                    else:
                        h_cov = 0.0
                        h_down = 0.0
                        h_up = 0.0
                        h_unk = 3600.0
                    materialized_buckets.append({
                        "instance": inst,
                        "job": job_filter if job_filter != 'all' else 'blackbox',
                        "bucket_start": cur_h,
                        "bucket_end": nxt_h,
                        "uptime_seconds": round(h_up, 2),
                        "downtime_seconds": round(h_down, 2),
                        "unknown_seconds": round(h_unk, 2),
                        "coverage_seconds": round(h_cov, 2),
                        "sample_count": s_cnt_int // num_hours,
                        "availability_pct": avail_pct,
                        "incident_count": inc_cnt if cur_h == h_start else 0,
                        "avg_latency_ms": lat_ms,
                        "updated_at": now_under_lock
                    })
                    cur_h = nxt_h

        if materialized_buckets:
            try:
                AvailabilityBucketRepository.save_buckets(materialized_buckets)
            except Exception:
                pass

        # Live status count resolution (shared rule with the SQLite fast path).
        counts = _availability_status_counts(entries, live_map)

        lowest_availability = sorted(
            [e for e in summary_dict['per_server']['values'] if e.get('availability_pct') is not None and e['availability_pct'] < 100.0],
            key=lambda e: (
                e['availability_pct'],
                -(e.get('downtime_minutes') or 0.0),
                -(e.get('incidents') or 0)
            )
        )

        trend_series, trend_slot_sec = _build_fleet_trend(req_end, minutes * 60.0, monitored_instances)

        t_ser_start = time.perf_counter()
        total_backend_ms = (t_ser_start - t_req_start) * 1000.0

        trace_data = {
            "route": "/api/availability",
            "path": "hybrid_path",
            "minutes": minutes_int,
            "flight_wait_ms": round(flight_wait_ms, 2),
            "sqlite_duration_ms": round(sqlite_duration_ms, 2),
            "sqlite_record_count": len(db_bucket_records),
            "prom_duration_ms": round(prom_duration_ms, 2),
            "prom_query_count": len(queries),
            "prom_queries": prom_query_timings,
            "hybrid_merge_ms": round(merge_duration_ms, 2),
            "materialize_ms": 0.0,
            "materialized_bucket_count": 0,
            "total_backend_ms": round(total_backend_ms, 2),
        }

        payload = {
            "ok": True,
            "period_minutes": round(minutes, 2),
            "requested_window_seconds": hybrid_meta.get("requested_window_seconds", round(minutes * 60.0, 1)),
            "coverage_seconds": hybrid_meta.get("coverage_seconds", 0.0),
            "unknown_seconds": hybrid_meta.get("unknown_seconds", 0.0),
            "missing_seconds": hybrid_meta.get("missing_seconds", hybrid_meta.get("unknown_seconds", 0.0)),
            "sqlite_seconds": hybrid_meta.get("sqlite_seconds", 0.0),
            "prometheus_seconds": hybrid_meta.get("prometheus_seconds", 0.0),
            "overlap_removed_seconds": hybrid_meta.get("overlap_removed_seconds", 0.0),
            "maintenance_excluded_seconds": hybrid_meta.get("maintenance_excluded_seconds", 0.0),
            "maintenance_scheduled_seconds": hybrid_meta.get("maintenance_scheduled_seconds", 0.0),
            "sla": fleet_sla_budget,
            "coverage_percent": hybrid_meta.get("coverage_percent", 0.0),
            "availability_percent": summary_dict['fleet_aggregate']['value'],
            "data_status": hybrid_meta.get("data_status", "PARTIAL"),
            "telemetry_audit": hybrid_meta.get("telemetry_audit", summary_dict.get("telemetry_audit", {})),
            "end": end_ts,
            "counts": {
                "total": len(monitored_instances),
                "scored": summary_dict.get('scored_count', 0),
                "eligible": summary_dict.get('eligible_count', 0),
                "online": counts['online'],
                "warning": counts['warning'],
                "offline": counts['offline'],
            },
            "overall": summary_dict['fleet_aggregate']['value'],
            "fleet_aggregate": summary_dict['fleet_aggregate'],
            "fleet_average": summary_dict['fleet_average'],
            "health_ratio": summary_dict['health_ratio'],
            "zero_downtime_ratio": summary_dict.get('zero_downtime_ratio'),
            "sla_compliance": summary_dict.get('sla_compliance'),
            "sla_compliance_ratio": summary_dict.get('sla_compliance_ratio'),
            "coverage_ratio": summary_dict.get('coverage_ratio'),
            "per_server": summary_dict['per_server'],
            "lowest_availability": lowest_availability,
            "hosts_requiring_attention": lowest_availability,
            "entries": summary_dict['per_server']['values'],
            "targets": {e['id']: e['availability_pct'] for e in summary_dict['per_server']['values']},
            "analytics": summary_dict.get('analytics', {}),
            "trend": trend_series,
            "trend_end_ts": int(req_end),
            "trend_start_ts": int(req_end - minutes * 60.0),
            "trend_bucket_seconds": trend_slot_sec,
            "source": hybrid_meta.get("source", "fallback"),
            "_trace": trace_data,
        }
        if not request.args.get("debug"):
            payload.pop("_trace", None)

        with _AVAILABILITY_CACHE_LOCK:
            _AVAILABILITY_CACHE[avail_cache_key] = (now_under_lock, payload)

        resp = jsonify(payload)
        t_ser_end = time.perf_counter()
        trace_data["serialization_ms"] = round((t_ser_end - t_ser_start) * 1000.0, 2)
        trace_data["total_backend_ms"] = round((t_ser_end - t_req_start) * 1000.0, 2)
        return resp

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

    poller_enabled = os.environ.get("DISABLE_ALERT_POLLER") != "1"
    poller_tick_age = (now - _LAST_POLLER_TICK[0]) if _LAST_POLLER_TICK[0] else None
    # A webhook delivery in the last WEBHOOK_ACTIVE_WINDOW_SECONDS backs the
    # poller off on purpose (see _poll_targets_once) — that's not a stall.
    webhook_recent = (now - _LAST_WEBHOOK_AT[0]) < WEBHOOK_ACTIVE_WINDOW_SECONDS
    alarm_service_ok = (
        webhook_recent or not poller_enabled or
        (poller_tick_age is not None and poller_tick_age < ALERT_POLL_INTERVAL_SECONDS * 3)
    )

    # Availability aggregator heartbeat (audit F5) — a silently dead aggregator
    # stops all hourly bucket materialization.
    aggregator_enabled = (
        os.environ.get("DISABLE_AVAILABILITY_AGGREGATOR") != "1"
        and os.environ.get("DISABLE_ALERT_POLLER") != "1"
    )
    aggregator_tick_age = (now - _LAST_AGGREGATOR_TICK[0]) if _LAST_AGGREGATOR_TICK[0] else None
    aggregator_ok = (
        not aggregator_enabled or
        (aggregator_tick_age is not None and aggregator_tick_age < AVAIL_AGGREGATE_INTERVAL_SECONDS * 3)
    )

    components = {
        "prometheus":     {"ok": prometheus_ok, "url": prom_base},
        "monitoring_api":  {"ok": True},
        "alarm_service":   {"ok": alarm_service_ok, "last_tick_seconds_ago": (
            round(poller_tick_age, 1) if poller_tick_age is not None else None)},
        "availability_aggregator": {"ok": aggregator_ok, "last_tick_seconds_ago": (
            round(aggregator_tick_age, 1) if aggregator_tick_age is not None else None),
            "enabled": aggregator_enabled},
        "storage":         {"ok": storage_ok},
    }
    overall_ok = all(c["ok"] for c in components.values())

    status_code = 503 if not storage_ok else 200

    return jsonify({
        "ok": overall_ok,
        "server_time": now,
        "components": components,
    }), status_code

# ── Prometheus-native alert generation (no-Alertmanager fallback) ───────────
# This stack has no Alertmanager (not in docker-compose.yml, no config file
# anywhere in the repo), so /webhook is never called and logs.json/
# history.json stayed permanently empty even while targets flapped up/down.
# This poller watches probe_success per monitored instance directly and
# synthesizes the same firing/resolved events Alertmanager would have sent,
# through record_alert_event() so both paths share identical dedupe and
# incident-history semantics. If a real webhook delivery arrives, this backs
# off automatically — Alertmanager, when present, is the source of truth.
ALERT_POLL_INTERVAL_SECONDS = float(os.environ.get("ALERT_POLL_INTERVAL", "15"))
WEBHOOK_ACTIVE_WINDOW_SECONDS = 120

# SlowResponse (warning-severity, up-but-degraded) config. See
# compute_slow_response_transitions() for the debounce rule this backs.
# DEFAULT_SLOW_RESPONSE_THRESHOLD_MS is defined near the top of this file so
# build_canonical_monitoring_state() can use it for the live grid too (F4).
SLOW_RESPONSE_DEBOUNCE_N = int(os.environ.get("SLOW_RESPONSE_DEBOUNCE_N", "3"))

_poller_state = {}  # instance -> 'up' | 'down', seeded from status.json at startup
_slow_poller_state = {}  # instance -> {'consec_slow', 'consec_fast', 'firing'} for SlowResponse debounce
_maintenance_active_prev = set()  # instance-scoped maintenance windows active as of the last poll tick
_LAST_POLLER_TICK = [0.0]  # heartbeat for /health — set every tick, whether or not it did work

def compute_state_transitions(success_map, prev_state):
    """Pure function: given instance->probe_success value map and the last
    known state per instance, return (transitions, updated_state).
    - If target is first observed UP: seeds baseline state 'up', emits no transition (no false alert).
    - If target is first observed DOWN (cold-start outage): seeds state 'down' and emits (inst, False)
      so the active outage is immediately recorded instead of being silently ignored.
    - If target was already known: emits transition only when state changes.
    """
    transitions = []
    new_state = dict(prev_state)
    for inst, val in success_map.items():
        is_up = str(val) in ('1', '1.0')
        prev = prev_state.get(inst)
        new_state[inst] = 'up' if is_up else 'down'
        if prev is None:
            if not is_up:
                # Cold-start / first observation: target is DOWN.
                # Emit transition to establish active incident in status/logs/history.
                transitions.append((inst, False))
        elif (prev == 'up') != is_up:
            transitions.append((inst, is_up))
    return transitions, new_state

def compute_slow_response_transitions(readings, prev_state, thresholds, debounce_n=SLOW_RESPONSE_DEBOUNCE_N):
    """Pure function (same shape as compute_state_transitions): given
    instance->(is_up, response_time_ms) readings and the last debounce state
    per instance, return (transitions, updated_state).

    Response time is naturally noisy — firing/resolving on a single sample
    would flap exactly like the occurrence-counting bug Incident History
    task #1 fixed, just for a new alert type. Requires `debounce_n`
    CONSECUTIVE over-threshold samples to fire, and `debounce_n` consecutive
    under-threshold samples to resolve — one bad or one good sample alone
    changes nothing. A target going DOWN supersedes "slow": streaks reset
    and a firing SlowResponse resolves immediately (being down isn't a
    degraded-but-up condition, it's TargetDown's job).

    thresholds: {instance: threshold_ms}, missing -> caller's global default.
    """
    transitions = []
    new_state = {}
    for inst, (is_up, rt_ms) in readings.items():
        st = dict(prev_state.get(inst) or {'consec_slow': 0, 'consec_fast': 0, 'firing': False})

        if not is_up:
            st['consec_slow'] = 0
            st['consec_fast'] = 0
            if st['firing']:
                st['firing'] = False
                transitions.append((inst, False))
            new_state[inst] = st
            continue

        threshold = thresholds.get(inst, DEFAULT_SLOW_RESPONSE_THRESHOLD_MS)
        is_slow_now = rt_ms is not None and rt_ms > threshold
        if is_slow_now:
            st['consec_slow'] += 1
            st['consec_fast'] = 0
        else:
            st['consec_fast'] += 1
            st['consec_slow'] = 0

        if not st['firing'] and st['consec_slow'] >= debounce_n:
            st['firing'] = True
            transitions.append((inst, True))
        elif st['firing'] and st['consec_fast'] >= debounce_n:
            st['firing'] = False
            transitions.append((inst, False))

        new_state[inst] = st
    return transitions, new_state

def _seed_poller_state():
    # Currently-firing incidents (survived from before a backend restart) seed
    # as 'down' so we don't re-fire a duplicate "went offline" for an outage
    # that's already recorded — only its eventual recovery still needs to be
    # observed and resolved. SQLite is the source of truth (audit F1).
    for a in active_incident_list():
        inst = a.get('instance')
        if inst:
            _poller_state[inst] = 'down'

def _reconcile_orphaned_alerts(monitored_instances):
    """Auto-resolves poller-owned alerts (TargetDown, SlowResponse) whose
    instance no longer exists in the monitored set at all — e.g. removed
    from Prometheus's scrape config entirely, not merely down. Without this,
    such an alert can never be observed recovering (compute_state_transitions
    / compute_slow_response_transitions only look at instances still present
    in success_map/instances) and stays firing forever, permanently pinning
    system status to CRITICAL/WARNING.
    Reads the active set from SQLite (single source of truth, audit F1) so a
    phantom incident that only exists in the DB — not in the status.json
    cache — is still reconciled here. Only touches poller-owned alerts, and
    only runs when `instances` is non-empty (Prometheus reachable) — see call
    site."""
    orphaned = [
        a for a in active_incident_list()
        if a.get('name') in (ALERTNAME_TARGET_DOWN, ALERTNAME_SLOW_RESPONSE) and a.get('instance') not in monitored_instances
    ]
    for a in orphaned:
        _slow_poller_state.pop(a.get('instance'), None)
        record_alert_event(
            name=a.get('name'),
            severity=a.get('severity', 'critical'),
            instance=a.get('instance'),
            summary=f"{a.get('instance')} auto-resolved (no longer monitored)",
            job='',
            event_time=time.time(),
            is_now_firing=False,
            receiver="prometheus-poller-reconcile",
            key=a.get('key') or f"{a.get('name')}|{a.get('instance')}",
        )

def _poll_targets_once():
    _LAST_POLLER_TICK[0] = time.time()

    if time.time() - _LAST_WEBHOOK_AT[0] < WEBHOOK_ACTIVE_WINDOW_SECONDS:
        return  # Alertmanager delivered a webhook recently — it's authoritative, don't double-fire

    instances = get_monitored_instances()
    if not instances:
        return

    _reconcile_orphaned_alerts(instances)

    success_map, duration_map, status_code_map = fetch_all_probe_metrics()
    if not success_map:
        return  # Prometheus unreachable this tick — never fabricate a transition from no data

    # Instance-scoped maintenance: freeze this instance's tracked state for
    # the window so no transition is recorded, then force one fresh check the
    # moment the window ends — "auto resume monitoring". If it's still down,
    # that's now a real transition (compute_state_transitions sees the forced
    # 'up' vs the real 'down') and a fresh incident is raised; record_alert_
    # event()'s own dedup no-ops it harmlessly if that incident was already
    # firing from before the window.
    #
    # If it instead recovered WHILE under maintenance, compute_state_
    # transitions sees forced-'up' == actual 'up' and never emits an event at
    # all — so a TargetDown incident that started before the window and
    # recovered silently during it would stay "firing" in SQLite forever
    # (the poller never evaluates a maintained instance, so no resolve is
    # ever recorded any other way). Confirm the real state right here instead
    # and explicitly resolve — record_alert_event() no-ops safely if nothing
    # was actually firing.
    # ponytail: job-scoped maintenance is enforced only in record_alert_event()
    # (still no false incident/alarm) — this resume nudge is instance-only;
    # extend to jobs if job-wide maintenance flapping becomes a problem.
    windows = load_maintenance_windows()
    active_now = {inst for inst in instances if get_active_maintenance(inst, windows=windows)}
    just_ended = _maintenance_active_prev - active_now
    if just_ended:
        try:
            _slow_firing_instances = {
                a['instance'] for a in IncidentRepository.get_active_incidents()
                if a.get('name') == ALERTNAME_SLOW_RESPONSE
            }
        except Exception:
            _slow_firing_instances = set()
    for inst in just_ended:
        _poller_state[inst] = 'up'
        # Seed (not just discard) the debounce state to match what SQLite
        # actually has open — popping this entirely would make
        # compute_slow_response_transitions believe nothing was firing for
        # this instance, so it could never observe a firing->resolved
        # transition for a SlowResponse incident that's genuinely still
        # 'firing' in the DB from before the window (same class of bug as
        # TargetDown above, different mechanism: here it's the poller's own
        # bookkeeping forgetting the DB's state, not a forced probe reading
        # matching the real one). Fresh counters either way — a stale streak
        # from right before maintenance shouldn't count toward the debounce.
        _slow_poller_state[inst] = {
            'consec_slow': 0, 'consec_fast': 0,
            'firing': inst in _slow_firing_instances
        }
        val = success_map.get(inst)
        if val is not None and str(val) in ('1', '1.0'):
            record_alert_event(
                name=ALERTNAME_TARGET_DOWN,
                severity="critical",
                instance=inst,
                summary=f"{inst} recovered (confirmed after maintenance window ended)",
                job="blackbox",
                event_time=time.time(),
                is_now_firing=False,
                receiver="prometheus-poller",
                key=f"{ALERTNAME_TARGET_DOWN}|{inst}",
            )
    _maintenance_active_prev.clear()
    _maintenance_active_prev.update(active_now)

    scoped = {inst: v for inst, v in success_map.items() if inst in instances and inst not in active_now}
    transitions, new_state = compute_state_transitions(scoped, _poller_state)

    now = time.time()

    # Debounce down-transitions: don't fire an outage until the target has been
    # down for OUTAGE_GRACE_SECONDS (same gate as build_canonical_monitoring_state).
    # A held instance is left UNLATCHED in _poller_state so the next tick
    # re-checks it — a blip that recovers inside the window never fires.
    if any(not is_up for _, is_up in transitions):
        down_since_map = fetch_down_since_prom_map()
        held = []
        for inst, is_up in transitions:
            if not is_up and not _outage_past_grace(down_since_map.get(inst), now):
                new_state[inst] = _poller_state.get(inst, 'up')
            else:
                held.append((inst, is_up))
        transitions = held

    _poller_state.update(new_state)

    for inst, is_up in transitions:
        lat = duration_map.get(inst)
        try:
            latency_ms = round(float(lat) * 1000, 1) if lat is not None else None
        except (TypeError, ValueError):
            latency_ms = None

        http_status_code = status_code_map.get(inst)
        last_error = None
        if is_up:
            summary = f"{inst} recovered"
        else:
            # Same classifier as /instances — poller has no per-instance
            # lastError (that lives on /api/v1/targets, which this loop
            # doesn't fetch), so this degrades to HTTP-code-based
            # classification or "Unknown", same as any other probe-only target.
            classification = classify_scrape_failure('down', '', http_status_code)
            summary = f"{inst} is unreachable ({classification['category']})"
            last_error = classification['detail']

        record_alert_event(
            name=ALERTNAME_TARGET_DOWN,
            severity="critical",
            instance=inst,
            summary=summary,
            job="blackbox",
            event_time=now,
            is_now_firing=(not is_up),
            receiver="prometheus-poller",
            key=f"{ALERTNAME_TARGET_DOWN}|{inst}",
            latency_ms=latency_ms,
            http_status_code=http_status_code,
            last_error=last_error,
        )

    # SlowResponse (warning-severity): evaluated every tick for every
    # currently-scoped instance, independent of whether TargetDown had a
    # transition — the debounce needs a consecutive-sample count, not just
    # the ticks where something already changed. See
    # compute_slow_response_transitions() for the debounce/threshold rules.
    readings = {}
    for inst, val in scoped.items():
        lat = duration_map.get(inst)
        try:
            rt_ms = round(float(lat) * 1000, 1) if lat is not None else None
        except (TypeError, ValueError):
            rt_ms = None
        readings[inst] = (str(val) in ('1', '1.0'), rt_ms)

    try:
        slow_thresholds = SlowThresholdRepository.get_all()
    except Exception:
        slow_thresholds = {}

    slow_transitions, new_slow_state = compute_slow_response_transitions(
        readings, _slow_poller_state, slow_thresholds
    )
    _slow_poller_state.update(new_slow_state)

    for inst, is_now_firing in slow_transitions:
        threshold = slow_thresholds.get(inst, DEFAULT_SLOW_RESPONSE_THRESHOLD_MS)
        rt_ms = readings[inst][1]
        summary = (
            f"{inst} response time degraded ({rt_ms}ms > {threshold}ms threshold)" if is_now_firing
            else f"{inst} response time recovered"
        )
        record_alert_event(
            name=ALERTNAME_SLOW_RESPONSE,
            severity="warning",
            instance=inst,
            summary=summary,
            job="blackbox",
            event_time=now,
            is_now_firing=is_now_firing,
            receiver="prometheus-poller",
            key=f"{ALERTNAME_SLOW_RESPONSE}|{inst}",
            latency_ms=rt_ms,
        )

def _poller_loop():
    _seed_poller_state()
    while True:
        try:
            _poll_targets_once()
        except Exception as e:
            logger.error(f"Alert poller error: {e}", exc_info=True)
        time.sleep(ALERT_POLL_INTERVAL_SECONDS)

_poller_thread_started = False

def start_alert_poller():
    global _poller_thread_started
    if _poller_thread_started:
        return
    _poller_thread_started = True
    threading.Thread(target=_poller_loop, daemon=True).start()

# ── Background Availability Aggregator ───────────────────────────────────────
_AVAIL_AGGREGATOR_WORKER_ID = f"{os.getpid()}_{uuid.uuid4().hex[:8]}"
AVAIL_AGGREGATE_INTERVAL_SECONDS = 60.0
# Buckets older than this are pruned every aggregator cycle — the archive holds
# nothing older, so no range query can return data past this horizon.
AVAIL_BUCKET_RETENTION_SECONDS = 35 * 86400
_avail_aggregator_started = False
# Heartbeat for /health — set at the top of every aggregator cycle (leader or
# not), so a wedged/crashed aggregator thread is visible instead of silently
# stopping all bucket materialization (audit F5).
_LAST_AGGREGATOR_TICK = [0.0]

def _availability_aggregation_windows(now, latest_end):
    """Time windows the aggregator should (re)aggregate this cycle.

    Backfill (empty/stale store): 6h windows from an hour-aligned start
    `floor(now/3600) - 7d` up to `now`. 21600 and 86400*7 are whole
    multiples of 3600, so every interior boundary stays hour-aligned; only
    the final window ends at the unaligned `now` to cover the in-progress
    hour. An unaligned start would push every boundary mid-hour (06:16:14,
    12:16:14, ...) and, since the per-window loop floors/ceils each window
    to whole hours, two consecutive windows would then both touch the same
    hourly bucket — each seeing only its own slice of that hour and
    overwriting the other, leaving the rest of that hour unaggregated.

    Steady state: just the last completed hour, plus the in-progress hour
    once it is >= 30s old.
    """
    if latest_end is None or latest_end < (now - 86400 * 7):
        windows = []
        cur_t = math.floor(now / 3600.0) * 3600.0 - 86400 * 7
        while cur_t < now:
            next_t = min(cur_t + 21600, now)
            windows.append((cur_t, next_t))
            cur_t = next_t
        return windows

    hour_end = math.floor(now / 3600.0) * 3600.0
    windows = [(hour_end - 3600.0, hour_end)]
    if now - hour_end >= 30.0:
        windows.append((hour_end, now))
    return windows


def _aggregate_availability_cycle():
    """Incremental availability aggregation run executed only by the elected leader worker."""
    _LAST_AGGREGATOR_TICK[0] = time.time()
    is_leader = AggregationLeaseRepository.acquire_or_renew(
        lease_name="avail_aggregator",
        owner_id=_AVAIL_AGGREGATOR_WORKER_ID,
        ttl_sec=AVAIL_AGGREGATE_INTERVAL_SECONDS * 2.5
    )
    if not is_leader:
        return

    now = time.time()
    instance_job_map = get_instance_job_map('all')
    monitored = sorted(instance_job_map.keys())
    if not monitored:
        return

    latest_end = AvailabilityBucketRepository.get_latest_bucket_end('all')
    windows_to_aggregate = _availability_aggregation_windows(now, latest_end)

    for w_start, w_end in windows_to_aggregate:
        w_minutes = max(1, int(round((w_end - w_start) / 60.0)))
        step_sec = SCRAPE_INTERVAL_SECONDS
        if w_minutes > 10080:
            step_sec = 300.0
        elif w_minutes > 1440:
            step_sec = 30.0

        at_suffix = f" @ {int(w_end)}"
        queries = {
            'probe_avail': f"avg_over_time(probe_success[{w_minutes}m]{at_suffix}) * 100",
            'up_avail': f"avg_over_time(up[{w_minutes}m]{at_suffix}) * 100",
            'probe_count': f"count_over_time(probe_success[{w_minutes}m]{at_suffix})",
            'up_count': f"count_over_time(up[{w_minutes}m]{at_suffix})",
            'probe_first_ts': f"min_over_time(timestamp(probe_success)[{w_minutes}m:]{at_suffix})",
            'probe_last_ts': f"max_over_time(timestamp(probe_success)[{w_minutes}m:]{at_suffix})",
            'up_first_ts': f"min_over_time(timestamp(up)[{w_minutes}m:]{at_suffix})",
            'up_last_ts': f"max_over_time(timestamp(up)[{w_minutes}m:]{at_suffix})",
            'duration': f"avg_over_time(probe_duration_seconds[{w_minutes}m]{at_suffix}) * 1000",
            'probe_incidents': f"changes(probe_success[{w_minutes}m]{at_suffix})",
            'up_incidents': f"changes(up[{w_minutes}m]{at_suffix})",
        }

        futures = {k: _SHARED_EXECUTOR.submit(fetch_prom_query_map, q, 10.0, 15.0) for k, q in queries.items()}
        results = {}
        for k, f in futures.items():
            try:
                results[k] = f.result()
            except Exception:
                results[k] = {}

        # Raw 0/1 sample stream for the exact reconstruction engine. One range
        # query for the whole fleet per window; per instance per hour it is
        # sliced and fed to reconstruct_time_series_intervals(). Step is
        # coarse enough to bound the payload (all instances at once) but fine
        # enough for hour-level accounting — sub-`range_step` blips can be
        # missed here; the live /api/availability path still re-queries fresh
        # and /api/target-history uses a finer step. On any failure the maps
        # come back empty and every instance falls back to the scalar
        # avg_over_time approximation below.
        range_step = 15.0 if w_minutes <= 180 else 30.0
        try:
            probe_range_map = fetch_prom_range_map("probe_success", w_start, w_end, range_step, cache_ttl=10.0, timeout=20.0)
        except Exception:
            probe_range_map = {}
        try:
            up_range_map = fetch_prom_range_map("up", w_start, w_end, range_step, cache_ttl=10.0, timeout=20.0)
        except Exception:
            up_range_map = {}

        # Same classify-and-merge step as api_availability's materialize
        # path, via derive_bucket_inputs() — see its docstring.
        probe_results_raw = {
            "avail": results.get('probe_avail', {}), "count": results.get('probe_count', {}),
            "first_ts": results.get('probe_first_ts', {}), "last_ts": results.get('probe_last_ts', {}),
            "incidents": results.get('probe_incidents', {}),
        }
        up_results_raw = {
            "avail": results.get('up_avail', {}), "count": results.get('up_count', {}),
            "first_ts": results.get('up_first_ts', {}), "last_ts": results.get('up_last_ts', {}),
            "incidents": results.get('up_incidents', {}),
        }
        merged_maps = derive_bucket_inputs(monitored, probe_results_raw, up_results_raw)
        avail_map = merged_maps["avail"]
        count_map = merged_maps["count"]
        first_ts_map = merged_maps["first_ts"]
        last_ts_map = merged_maps["last_ts"]
        incidents_map = merged_maps["incidents"]

        duration_map = results.get('duration', {})

        observed_cadences = []
        for inst in monitored:
            estimated = estimate_instance_cadence(inst, count_map, first_ts_map, last_ts_map)
            if estimated is not None:
                observed_cadences.append(estimated)
        fleet_median_cadence = (
            sorted(observed_cadences)[len(observed_cadences) // 2]
            if observed_cadences else SCRAPE_INTERVAL_SECONDS
        )

        bucket_records = []
        h_start = math.floor(w_start / 3600.0) * 3600.0
        h_end = math.ceil(w_end / 3600.0) * 3600.0
        num_hours = max(1, int(round((h_end - h_start) / 3600.0)))
        w_duration_sec = float(w_end - w_start)

        for inst in monitored:
            raw_avail = avail_map.get(inst)
            raw_count = count_map.get(inst)
            raw_dur = duration_map.get(inst)
            raw_inc = incidents_map.get(inst)

            latency = 0.0
            if raw_dur is not None:
                try:
                    latency = round(float(raw_dur), 1)
                except (ValueError, TypeError):
                    latency = 0.0

            # Exact path: raw 0/1 samples for this instance (probe_success
            # first, fall back to the `up` series for node/exporter targets).
            samples = probe_range_map.get(inst) or up_range_map.get(inst)
            if samples:
                cad = (
                    estimate_instance_cadence(inst, count_map, first_ts_map, last_ts_map)
                    or (fleet_median_cadence if fleet_median_cadence > 0 else range_step)
                )
                cur_h = h_start
                while cur_h < h_end:
                    nxt_h = cur_h + 3600.0
                    if cur_h >= now:
                        break  # never materialize a bucket for an hour that has not started
                    eff_end = min(nxt_h, now)
                    rec = reconstruct_time_series_intervals(
                        samples, window_start_ts=cur_h, window_end_ts=eff_end,
                        expected_interval_sec=cad,
                    )
                    hour_pts = [v for ts, v in samples if cur_h <= ts < eff_end]
                    bucket_records.append({
                        "instance": inst,
                        "job": instance_job_map.get(inst, "blackbox"),
                        "bucket_start": cur_h,
                        "bucket_end": nxt_h,
                        "uptime_seconds": round(rec["uptime_seconds"], 2),
                        "downtime_seconds": round(rec["downtime_seconds"], 2),
                        "unknown_seconds": round(rec["unknown_seconds"], 2),
                        "coverage_seconds": round(rec["coverage_seconds"], 2),
                        "sample_count": len(hour_pts),
                        "availability_pct": rec["availability_pct"],
                        "incident_count": int(rec["incident_count"]),
                        "avg_latency_ms": latency,
                        "updated_at": now,
                        # per-hour outages + still-in-outage-at-hour-start/end
                        # flags: merge_hybrid_target_availability drops the
                        # duplicate incident where one outage straddles the
                        # boundary between two contiguous buckets. "i" carries
                        # each outage's absolute [start, end] so the maintenance
                        # carve-out can intersect the real outage, not the hour.
                        "outage_json": {
                            "d": [round(float(x), 1) for x in rec.get("outage_durations_sec", [])],
                            "i": [[round(float(s), 1), round(float(e), 1)]
                                  for s, e in rec.get("outage_intervals_sec", [])],
                            "ongoing_start": bool(hour_pts and hour_pts[0] == 0),
                            "ongoing_end": bool(rec.get("is_ongoing_outage")),
                        },
                    })
                    cur_h = nxt_h
                continue

            # Fallback path: no raw samples (Prometheus range query failed, or
            # short retention) — approximate from the avg_over_time / count /
            # first-last-timestamp scalars, same as before this pass.
            avail_pct = None
            if raw_avail is not None:
                try:
                    avail_pct = round(max(0.0, min(100.0, float(raw_avail))), 2)
                except (ValueError, TypeError):
                    avail_pct = None

            sample_count = 0
            cov_sec = 0.0
            f_ts = float(first_ts_map.get(inst, 0)) if first_ts_map else 0.0
            l_ts = float(last_ts_map.get(inst, 0)) if last_ts_map else 0.0

            if raw_count is not None:
                try:
                    sample_count = int(float(raw_count))
                    if sample_count >= 2:
                        span = l_ts - f_ts
                        if f_ts > 0 and l_ts > 0 and span > 0:
                            intv = span / (sample_count - 1)
                            lead_in = min(intv, max(0.0, f_ts - w_start)) if (f_ts - w_start) <= intv * 1.5 else 0.0
                            lead_out = min(intv, max(0.0, w_end - l_ts)) if (w_end - l_ts) <= intv * 1.5 else 0.0
                            cov_sec = min(span + lead_in + lead_out, w_duration_sec)
                        else:
                            eff_cad = fleet_median_cadence if fleet_median_cadence > 0 else step_sec
                            cov_sec = min(sample_count * eff_cad, w_duration_sec)
                    elif sample_count == 1:
                        eff_cad = fleet_median_cadence if fleet_median_cadence > 0 else step_sec
                        cov_sec = min(eff_cad, w_duration_sec)
                except (ValueError, TypeError):
                    cov_sec = 0.0
            elif avail_pct is not None:
                cov_sec = w_duration_sec

            if avail_pct is not None:
                up_rate = min(1.0, max(0.0, float(avail_pct) / 100.0))
                down_rate = round(1.0 - up_rate, 6)
            else:
                up_rate = 0.0
                down_rate = 0.0
            down_sec = round(cov_sec * down_rate, 2)

            inc_count = 0
            if raw_inc is not None:
                try:
                    inc_count = int(math.ceil(float(raw_inc) / 2.0))
                except (ValueError, TypeError):
                    inc_count = 0
            if inc_count == 0 and down_sec > 0:
                inc_count = 1

            cur_h = h_start
            while cur_h < h_end:
                nxt_h = cur_h + 3600.0
                if f_ts > 0 and l_ts > 0 and l_ts >= f_ts:
                    overlap_start = max(cur_h, f_ts)
                    overlap_end = min(nxt_h, l_ts)
                    overlap_sec = max(0.0, overlap_end - overlap_start)
                    if overlap_sec > 0:
                        h_cov = min(3600.0, overlap_sec)
                        h_down = round(h_cov * down_rate, 2)
                        h_up = max(0.0, round(h_cov - h_down, 2))
                        h_unk = max(0.0, round(3600.0 - h_cov, 2))
                        h_avail = avail_pct
                    else:
                        h_cov = 0.0
                        h_down = 0.0
                        h_up = 0.0
                        h_unk = 3600.0
                        h_avail = None
                elif avail_pct is not None or cov_sec > 0:
                    h_cov = min(3600.0, cov_sec / num_hours)
                    h_down = round(h_cov * down_rate, 2)
                    h_up = max(0.0, round(h_cov - h_down, 2))
                    h_unk = max(0.0, round(3600.0 - h_cov, 2))
                    h_avail = avail_pct
                else:
                    h_cov = 0.0
                    h_down = 0.0
                    h_up = 0.0
                    h_unk = 3600.0
                    h_avail = None

                bucket_records.append({
                    "instance": inst,
                    "job": instance_job_map.get(inst, "blackbox"),
                    "bucket_start": cur_h,
                    "bucket_end": nxt_h,
                    "uptime_seconds": round(h_up, 2),
                    "downtime_seconds": round(h_down, 2),
                    "unknown_seconds": round(h_unk, 2),
                    "coverage_seconds": round(h_cov, 2),
                    "sample_count": sample_count // num_hours,
                    "availability_pct": h_avail,
                    "incident_count": inc_count if cur_h == h_start else 0,
                    "avg_latency_ms": latency,
                    "updated_at": now
                })
                cur_h = nxt_h

        if bucket_records:
            try:
                AvailabilityBucketRepository.save_buckets(bucket_records)
            except Exception:
                # Was silently swallowed (audit F5) — a persistently failing
                # write silently stops all bucket materialization.
                logger.exception("availability aggregator: save_buckets failed for %d record(s)", len(bucket_records))

    try:
        AvailabilityBucketRepository.prune_old_buckets(AVAIL_BUCKET_RETENTION_SECONDS)
    except Exception:
        logger.exception("availability aggregator: prune_old_buckets failed")


def _availability_aggregator_loop():
    while True:
        try:
            _aggregate_availability_cycle()
        except Exception as e:
            logger.error(f"Availability aggregator error: {e}", exc_info=True)
        time.sleep(AVAIL_AGGREGATE_INTERVAL_SECONDS)


def start_availability_aggregator():
    global _avail_aggregator_started
    if _avail_aggregator_started:
        return
    _avail_aggregator_started = True
    threading.Thread(target=_availability_aggregator_loop, daemon=True, name="avail-aggregator").start()


def _reconcile_status_json_into_sqlite():
    """One-shot at boot: SQLite `incidents` is the source of truth for
    active-alert state (audit F1), but an ops restore that brings back only
    status.json (the denormalized cache) would otherwise lose the active
    incident entirely. Import any firing alert in status.json that SQLite
    doesn't already have as a firing incident, so recovery still works from
    either artefact. No-op when status.json is absent/empty (the common case).
    """
    try:
        status_data = json_store.load_json(json_store.STATUS_FILE, None)
        if not isinstance(status_data, dict):
            return
        alerts = status_data.get("alerts") or []
        if not alerts:
            return
        try:
            active_keys = {a.get("key") for a in IncidentRepository.get_active_incidents()}
        except Exception:
            active_keys = set()
        imported = 0
        for a in alerts:
            key = a.get("key") or f"{a.get('name')}|{a.get('instance')}"
            if key in active_keys:
                continue
            IncidentRepository.record_alert_event(
                name=a.get("name", "Unknown"), severity=a.get("severity", "critical"),
                instance=a.get("instance", "-"), summary=a.get("summary", ""),
                job=a.get("job", ""), event_time=float(a.get("time", time.time())),
                is_now_firing=True, key=key,
            )
            imported += 1
        if imported:
            logger.info("Recovered %d active incident(s) from status.json into SQLite on boot", imported)
    except Exception:
        logger.exception("status.json -> SQLite active-incident reconcile failed")


_reconcile_status_json_into_sqlite()

if os.environ.get("DISABLE_ALERT_POLLER") != "1":
    start_alert_poller()

if os.environ.get("DISABLE_AVAILABILITY_AGGREGATOR") != "1" and os.environ.get("DISABLE_ALERT_POLLER") != "1":
    start_availability_aggregator()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, threaded=True)
