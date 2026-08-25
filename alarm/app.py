from flask import Flask, request, jsonify, render_template, session, g
import json
import time
import os
import math
import re
import gzip
import threading
import sys
import uuid
import ipaddress
import logging
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from functools import wraps
from urllib.request import urlopen, Request
from urllib.error import URLError
from urllib.parse import urlparse, quote
import yaml

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
        derive_bucket_inputs, estimate_instance_cadence
    )
except ImportError:
    from alarm.fleet_availability import (
        summarize_entries, reconstruct_time_series_intervals, calculate_percentile,
        clip_hourly_bucket, merge_hybrid_target_availability, merge_hybrid_fleet_availability,
        derive_bucket_inputs, estimate_instance_cadence
    )

try:
    from storage import (
        init_db, IncidentRepository, EventLogRepository,
        MaintenanceRepository, DependencyRepository, EndpointRepository, DeletedTargetRepository,
        AvailabilityBucketRepository, AggregationLeaseRepository,
        UserRepository, AcknowledgmentRepository, AuditLogRepository
    )
except ImportError:
    from alarm.storage import (
        init_db, IncidentRepository, EventLogRepository,
        MaintenanceRepository, DependencyRepository, EndpointRepository, DeletedTargetRepository,
        AvailabilityBucketRepository, AggregationLeaseRepository,
        UserRepository, AcknowledgmentRepository, AuditLogRepository
    )

try:
    from telegram_notifier import (
        dispatch_alert_async, get_telegram_config, save_telegram_config, test_telegram_connection
    )
except ImportError:
    from alarm.telegram_notifier import (
        dispatch_alert_async, get_telegram_config, save_telegram_config, test_telegram_connection
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

init_db()

# Matches prometheus/prometheus.yml `global.scrape_interval`. Used to turn a
# Prometheus `count_over_time(...)` sample count back into a monitored-duration
# estimate for weighted (fleet_aggregate) availability math. Override via
# SCRAPE_INTERVAL_SECONDS if prometheus.yml's scrape_interval is ever changed.
try:
    SCRAPE_INTERVAL_SECONDS = float(os.environ.get("SCRAPE_INTERVAL_SECONDS", "2.0"))
except (TypeError, ValueError):
    SCRAPE_INTERVAL_SECONDS = 2.0

# How stale the latest SQLite availability bucket may be, for a *live* window
# (no ?end=), before /api/availability's "is this range fully materialized?"
# check gives up on the fast path and falls back to a live Prometheus query.
# The background aggregator (AVAIL_AGGREGATE_INTERVAL_SECONDS, defined lower
# in this file) only refreshes the in-progress hour's bucket once every 60s,
# and that cycle itself takes real time (Prometheus queries, up to 15s
# timeout). The frontend polls this endpoint every 15s regardless of user
# action — with zero slack between the two 60s numbers, a live range would
# drift past a bare 60s tolerance partway through most aggregation cycles,
# flip-flopping between the SQLite-materialized path and the live-hybrid
# path (two different estimation methods) on its own, with the UI number
# visibly changing every ~15s with no user action. 2.5x mirrors the same
# slack multiplier already used for the aggregator's leader-election lease.
_AVAIL_FRESHNESS_TOLERANCE_SEC = 150.0  # 60.0 * 2.5

app = Flask(__name__)
app.secret_key = get_session_secret()
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_NAME'] = 'infrawatch_session'
if os.environ.get("SESSION_COOKIE_SECURE", "0") == "1":
    app.config['SESSION_COOKIE_SECURE'] = True
# Flask defaults PERMANENT_SESSION_LIFETIME to 31 days; a NOC login on a
# shared workstation shouldn't stay valid that long unattended.
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=int(os.environ.get("INFRAWATCH_SESSION_HOURS", "24")))
# Static assets (JS/CSS/mp3) are safe to let browsers cache briefly — only the
# dynamic/live JSON endpoints need the no-cache headers below.
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 300

@app.context_processor
def inject_asset_version():
    # Appended as ?v=<mtime> on static asset URLs so a code change busts the
    # 5-minute browser cache immediately instead of serving stale JS/CSS.
    def asset_version(filename):
        path = os.path.join(app.static_folder, filename)
        try:
            return int(os.path.getmtime(path))
        except OSError:
            return 0
    return {"asset_version": asset_version}

GZIP_MIN_BYTES = 500

@app.after_request
def add_header(response):
    if request.path.startswith('/static/'):
        return response

    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '-1'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'

    # Compress larger JSON payloads — matters once a fleet reaches hundreds/
    # thousands of targets — without touching response content.
    accept_encoding = request.headers.get('Accept-Encoding', '')
    if (
        'gzip' in accept_encoding
        and not response.direct_passthrough
        and 'Content-Encoding' not in response.headers
    ):
        body = response.get_data()
        if len(body) >= GZIP_MIN_BYTES:
            compressed = gzip.compress(body, compresslevel=6)
            response.set_data(compressed)
            response.headers['Content-Encoding'] = 'gzip'
            response.headers['Content-Length'] = str(len(compressed))
            response.headers['Vary'] = 'Accept-Encoding'

    return response

@app.errorhandler(404)
def handle_404(e):
    if request.path.startswith('/api/') or request.path in ('/instances', '/status', '/logs', '/history', '/webhook', '/health', '/health/live', '/health/ready'):
        return jsonify({"ok": False, "error": "Resource not found"}), 404
    return render_template('alarm.html'), 404

@app.errorhandler(405)
def handle_405(e):
    return jsonify({"ok": False, "error": "Method not allowed"}), 405

@app.errorhandler(500)
def handle_500(e):
    logger.error(f"Internal server error: {e}", exc_info=True)
    return jsonify({"ok": False, "error": "Internal server error"}), 500

STATUS_FILE  = os.path.join(os.path.dirname(__file__), "status.json")
HISTORY_FILE = os.path.join(os.path.dirname(__file__), "history.json")
HISTORY_ARCHIVE_FILE = os.path.join(os.path.dirname(__file__), "history_archive.json")
LOGS_FILE    = os.path.join(os.path.dirname(__file__), "logs.json")

# Dependencies ("which host depends on which", see DependencyRepository /
# load_dependencies below) live in SQLite, independent of target config —
# most targets aren't even owned by this app (discovered live from
# Prometheus's own scrape config), and the one target file this app does
# own (targets/websites.yml) is a Prometheus file_sd YAML snippet, not a
# place to hang relational metadata off of.
MAX_HISTORY  = 1000
MAX_LOGS     = 200

def parse_alert_timestamp(value, fallback):
    if value:
        try:
            return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
        except (ValueError, TypeError):
            pass
    return fallback

def alert_key(a, labels):
    return a.get('fingerprint') or f"{labels.get('alertname', 'Unknown')}|{labels.get('instance', '-')}"

MAX_ARCHIVE_HISTORY = 5000

def save_with_retention(main_path, archive_path, data, limit):
    if len(data) > limit:
        overflow = data[limit:]
        archive = load_json(archive_path, [])
        archive = (overflow + archive)[:MAX_ARCHIVE_HISTORY]
        save_json(archive_path, archive)
        data = data[:limit]
    save_json(main_path, data)

# Serializes the load-modify-save cycle for status.json/logs.json/history.json
# in webhook() — without it, concurrent deliveries (Alertmanager routinely
# fans out several groups at once under threaded=True) each read the same
# stale list and clobber each other's inserts on save.
_WEBHOOK_LOCK = threading.Lock()

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

# The one compiled-in fallback. Used to be a 4-entry topology-guessing list
# (host.docker.internal / localhost / 127.0.0.1) — removed: /api/endpoints
# already lets an operator register exactly the Prometheus URL their
# deployment needs, so guessing at container-networking conventions no
# longer earns its keep. This single default covers the common case (a
# compose service literally named "prometheus") without guessing further.
_DEFAULT_PROM_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")

def _is_blocked_ip(ip):
    return (ip.is_link_local or
            ip.is_multicast or
            ip.is_reserved or
            ip.is_loopback or
            (isinstance(ip, ipaddress.IPv4Address) and str(ip).startswith('169.254.')) or
            (isinstance(ip, ipaddress.IPv6Address) and (ip.is_site_local or ip.ipv4_mapped)))

def is_safe_endpoint_url(url: str):
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme not in ('http', 'https'):
            return False, "Invalid scheme; only http and https are allowed"
        if not parsed.netloc or '@' in parsed.netloc:
            return False, "Invalid URL format or credentials not allowed"
        hostname = (parsed.hostname or '').lower().strip('[]')
        if not hostname:
            return False, "Host missing in URL"

        blocked_hosts = {
            'metadata.google.internal',
            '169.254.169.254',
            '100.100.100.200',
            'instance-data',
            'fd00:ec2::254',
            'localhost',
        }
        if hostname in blocked_hosts or hostname.startswith("169.254."):
            return False, "Endpoint URL is not allowed (restricted network)"

        try:
            ip = ipaddress.ip_address(hostname)
            if _is_blocked_ip(ip):
                return False, "Endpoint URL is not allowed (restricted network)"
            return True, ""
        except ValueError:
            pass  # hostname is a name, not a literal IP - resolve it below

        # Hostname (not a literal IP): resolve and check every address it maps
        # to, so a name pointed at a loopback/link-local/metadata IP (DNS
        # rebinding, or just a misconfigured record) can't slip past the
        # literal-IP checks above. A hostname that fails to resolve here is
        # NOT rejected — it's likely a Docker Compose service name only
        # resolvable from inside the compose network (e.g. added before that
        # container exists), same as the pre-existing behavior for hostnames.
        try:
            addr_infos = socket.getaddrinfo(hostname, None)
        except socket.gaierror:
            return True, ""
        for family, _, _, _, sockaddr in addr_infos:
            ip = ipaddress.ip_address(sockaddr[0])
            if _is_blocked_ip(ip):
                return False, "Endpoint URL is not allowed (restricted network)"

        return True, ""
    except Exception as e:
        return False, f"Invalid URL: {e}"

_ENDPOINTS_CACHE = {"ts": 0.0, "data": None}
_ENDPOINTS_CACHE_TTL = 2.0  # SQLite rarely changes; avoid a query on every hot-path call

# EndpointRepository (SQLite) is the sole source of truth — endpoints.json
# used to be read first (when present) and written on every mutation as a
# parallel copy; every mutating route below now writes only through the
# repository, and load_endpoints() reads only from it (short-TTL cached).
def load_endpoints():
    now = time.time()
    cached = _ENDPOINTS_CACHE["data"]
    if cached is not None and now - _ENDPOINTS_CACHE["ts"] < _ENDPOINTS_CACHE_TTL:
        return cached

    try:
        data = EndpointRepository.load_endpoints_state(_DEFAULT_PROM_URL)
    except Exception:
        data = {"active": _DEFAULT_PROM_URL, "endpoints": [_DEFAULT_PROM_URL]}

    _ENDPOINTS_CACHE["data"] = data
    _ENDPOINTS_CACHE["ts"] = now
    return data

DEFAULT_JOB_FILTER = os.environ.get("JOB_FILTER", "all")
ALERTNAME_TARGET_DOWN = "TargetDown"

# Minimal shape check for the Add Target form — not full RFC validation, just
# enough to reject obvious garbage (e.g. a bare number) before it's written to
# websites.yml. Bare Docker/internal hostnames without a dot (e.g. "webapp")
# are intentionally allowed — that's a real, valid target shape here.
TARGET_HOST_RE = re.compile(r'^[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?)*$')

# Cloud metadata endpoints are never a legitimate monitoring target (unlike
# private/internal IPs, which this tool exists to monitor) — block them
# outright rather than trying to enumerate "safe" internal ranges.
_BLOCKED_TARGET_HOSTS = {
    'metadata.google.internal', '169.254.169.254', '100.100.100.200',
    'instance-data', 'fd00:ec2::254',
}

def is_valid_target(url):
    candidate = re.sub(r'^https?://', '', url.strip()).split('/')[0].split(':')[0]
    if not candidate:
        return False
    if candidate.lower() in _BLOCKED_TARGET_HOSTS or candidate.startswith('169.254.'):
        return False
    if re.match(r'^\d{1,3}(\.\d{1,3}){3}$', candidate):
        return True
    if candidate.isdigit():
        return False  # e.g. "129391283912" — not an IPv4, not a sane hostname
    return bool(TARGET_HOST_RE.match(candidate))

def matches_job_filter(job, scrape_pool, filter_val):
    if not filter_val or filter_val.lower() in ('all', '*'):
        return True
    job_lower = (job or '').lower()
    pool_lower = (scrape_pool or '').lower()
    val_lower = filter_val.lower()
    return job_lower == val_lower or pool_lower == val_lower or val_lower in job_lower or val_lower in pool_lower

TARGETS_PATHS = [
    os.environ.get("TARGETS_FILE"),
    "/app/targets/websites.yml",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "prometheus", "targets", "websites.yml")),
    os.path.abspath(os.path.join(os.path.dirname(__file__), "targets", "websites.yml"))
]

def get_targets_file():
    env_target = os.environ.get("TARGETS_FILE")
    if env_target and os.path.isfile(env_target):
        return env_target
    
    local_target = os.path.abspath(os.path.join(os.path.dirname(__file__), "targets", "websites.yml"))
    if os.path.isfile(local_target):
        return local_target

    for p in TARGETS_PATHS:
        if p and os.path.isfile(p):
            return p

    os.makedirs(os.path.dirname(local_target), exist_ok=True)
    return local_target

# websites.yml is a Prometheus file_sd YAML doc: a list of {targets, labels}
# groups. This app only owns the group labeled job: "blackbox_http" — other
# groups (e.g. a hand-written blackbox_ping group) are read straight through
# to save() untouched, so structure, custom labels, and multi-group targets
# survive an Add/Delete Target round-trip instead of getting flattened.
WEBSITES_JOB_LABEL = "blackbox_http"

def load_website_targets():
    target_file = get_targets_file()
    if not os.path.exists(target_file):
        return []
    try:
        with open(target_file, 'r', encoding='utf-8') as f:
            doc = yaml.safe_load(f)
    except Exception as e:
        print(f"Error loading targets: {e}", flush=True)
        return []

    if not isinstance(doc, list):
        return []

    urls = []
    for group in doc:
        if not isinstance(group, dict):
            continue
        labels = group.get('labels') or {}
        if labels.get('job') != WEBSITES_JOB_LABEL:
            continue
        for u in (group.get('targets') or []):
            u = str(u).strip()
            if u and u not in urls:
                urls.append(u)
    return urls

def save_website_targets(urls):
    target_file = get_targets_file()
    try:
        os.makedirs(os.path.dirname(target_file), exist_ok=True)

        doc = []
        if os.path.exists(target_file):
            try:
                with open(target_file, 'r', encoding='utf-8') as f:
                    loaded = yaml.safe_load(f)
                if isinstance(loaded, list):
                    doc = loaded
            except Exception:
                doc = []

        for group in doc:
            if isinstance(group, dict) and (group.get('labels') or {}).get('job') == WEBSITES_JOB_LABEL:
                group['targets'] = list(urls)
                break
        else:
            doc.append({"targets": list(urls), "labels": {"job": WEBSITES_JOB_LABEL}})

        tmp_file = target_file + ".tmp"
        with open(tmp_file, 'w', encoding='utf-8') as f:
            yaml.safe_dump(doc, f, default_flow_style=False, sort_keys=False)
        os.replace(tmp_file, target_file)
    except Exception as e:
        print(f"Error saving targets {target_file}: {e}", flush=True)

LAST_WORKING_PROMETHEUS_URL = None
PROMETHEUS_CACHE = {}
PROMETHEUS_CACHE_LOCK = threading.Lock()
PROMETHEUS_CACHE_TTL_DEFAULT = 8.0  # backend cache window for identical PromQL responses (5-15s band)

# Shared thread pool executor to prevent continuous thread creation/destruction
_SHARED_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix="infrawatch-worker")

# Separate, small pool for the SSRF DNS-revalidation check (_cached_is_safe_endpoint_url
# below). socket.getaddrinfo() has no portable timeout, so a hung/slow resolver
# can tie up a worker for well past our 1s wait — kept off _SHARED_EXECUTOR so
# that can never queue behind (or starve) actual Prometheus query submission.
_DNS_CHECK_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="infrawatch-dnscheck")

# Cooldown circuit-breaker for unreachable candidate endpoints (prevents timeout cascades)
_FAILED_CANDIDATES = {}
_FAILED_CANDIDATES_LOCK = threading.Lock()
_FAILED_CANDIDATE_TTL = 5.0

# Single-flight locks: when several requests need the same (endpoint, PromQL)
# result at once (e.g. /instances and /api/availability both polling
# probe_success around the same tick), only the first actually calls
# Prometheus — the rest wait and reuse its result instead of duplicating it.
_FETCH_LOCKS = {}
_FETCH_LOCKS_GUARD = threading.Lock()

# Query params like custom time ranges / per-target history embed a live
# timestamp, so their cache keys are effectively unique each call. Bound the
# resulting cache/lock growth with a periodic sweep instead of a per-request
# scan.
_CACHE_LAST_PRUNE = [0.0]
_CACHE_PRUNE_INTERVAL = 30.0
_CACHE_MAX_AGE = 120.0

# Availability result cache & single-flight coalescing
_AVAILABILITY_CACHE = {}
_AVAILABILITY_CACHE_LOCK = threading.Lock()
_AVAILABILITY_FLIGHT_LOCKS = {}
_AVAILABILITY_FLIGHT_LOCKS_GUARD = threading.Lock()

def _fetch_lock_for(key):
    with _FETCH_LOCKS_GUARD:
        lock = _FETCH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _FETCH_LOCKS[key] = lock
        return lock

def _avail_flight_lock_for(key):
    with _AVAILABILITY_FLIGHT_LOCKS_GUARD:
        lock = _AVAILABILITY_FLIGHT_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _AVAILABILITY_FLIGHT_LOCKS[key] = lock
        return lock

def clear_availability_cache(clear_db=True):
    with _AVAILABILITY_CACHE_LOCK:
        _AVAILABILITY_CACHE.clear()
    with _AVAILABILITY_FLIGHT_LOCKS_GUARD:
        _AVAILABILITY_FLIGHT_LOCKS.clear()
    if clear_db:
        try:
            AvailabilityBucketRepository.clear_all_buckets()
        except Exception:
            pass

def _maybe_prune_cache(now):
    if now - _CACHE_LAST_PRUNE[0] < _CACHE_PRUNE_INTERVAL:
        return
    _CACHE_LAST_PRUNE[0] = now
    with PROMETHEUS_CACHE_LOCK:
        stale_keys = [k for k, (ts, _d, _b) in PROMETHEUS_CACHE.items() if now - ts > _CACHE_MAX_AGE]
        for k in stale_keys:
            PROMETHEUS_CACHE.pop(k, None)
    with _FETCH_LOCKS_GUARD:
        for k in stale_keys:
            _FETCH_LOCKS.pop(k, None)
        if len(_FETCH_LOCKS) > 500:
            unlocked = [k for k, lock in list(_FETCH_LOCKS.items()) if not lock.locked() and k not in PROMETHEUS_CACHE]
            for k in unlocked:
                _FETCH_LOCKS.pop(k, None)
    with _AVAILABILITY_CACHE_LOCK:
        stale_avail = [k for k, (ts, _d) in _AVAILABILITY_CACHE.items() if now - ts > _CACHE_MAX_AGE]
        for k in stale_avail:
            _AVAILABILITY_CACHE.pop(k, None)
    with _AVAILABILITY_FLIGHT_LOCKS_GUARD:
        for k in stale_avail:
            _AVAILABILITY_FLIGHT_LOCKS.pop(k, None)
        if len(_AVAILABILITY_FLIGHT_LOCKS) > 500:
            unlocked_avail = [k for k, lock in list(_AVAILABILITY_FLIGHT_LOCKS.items()) if not lock.locked() and k not in _AVAILABILITY_CACHE]
            for k in unlocked_avail:
                _AVAILABILITY_FLIGHT_LOCKS.pop(k, None)

# is_safe_endpoint_url() does a real (blocking) DNS resolution — fine once,
# at /api/endpoints registration time, but fetch_prometheus_json is called
# for every distinct PromQL query, many times a minute. A short TTL cache
# keeps the DNS-rebinding re-check cheap on the hot path while still closing
# the gap within one TTL window of a hostname's record changing (registration
# alone left it trusted forever).
_SAFE_CANDIDATE_CACHE = {}
_SAFE_CANDIDATE_CACHE_LOCK = threading.Lock()
_SAFE_CANDIDATE_CACHE_TTL = 20.0
# A hostname that doesn't resolve at all (e.g. a Compose-internal name whose
# container isn't up yet) can take several seconds to fail via the system
# resolver, with no way to bound socket.getaddrinfo's own timeout portably.
# Run it off-thread with a hard deadline so a slow/hanging lookup can't stall
# an HTTP request or poll cycle; on timeout, fail open — same treatment
# is_safe_endpoint_url already gives an unresolvable hostname, since a name
# that's merely slow to resolve is not the DNS-rebinding case this guards
# against (a rebound name resolves fine, just to a different, blocked IP).
_SAFE_CHECK_TIMEOUT_SEC = 1.0

def _cached_is_safe_endpoint_url(url):
    now = time.time()
    with _SAFE_CANDIDATE_CACHE_LOCK:
        cached = _SAFE_CANDIDATE_CACHE.get(url)
        if cached and now - cached[0] < _SAFE_CANDIDATE_CACHE_TTL:
            return cached[1], cached[2]
    try:
        future = _DNS_CHECK_EXECUTOR.submit(is_safe_endpoint_url, url)
        ok, reason = future.result(timeout=_SAFE_CHECK_TIMEOUT_SEC)
    except TimeoutError:
        # Fail open but do NOT cache it — a slow/hanging lookup is transient;
        # caching "safe" for the full TTL would let a deliberately-stalled
        # DNS response buy an attacker a trusted window instead of being
        # re-checked on the very next call like the comment above promises.
        return True, "DNS check timed out; treated as safe (re-checked next cycle)"
    except Exception:
        return True, "DNS check errored; treated as safe (re-checked next cycle)"
    with _SAFE_CANDIDATE_CACHE_LOCK:
        _SAFE_CANDIDATE_CACHE[url] = (now, ok, reason)
    return ok, reason

def _filter_safe_candidates(urls):
    """Re-validate each URL's currently-resolved IP right before it's used as a
    poll target. is_safe_endpoint_url() is also run once at endpoint
    registration time (/api/endpoints), but a hostname that resolved to a
    public IP then can be repointed via DNS to a loopback/link-local/metadata
    address afterwards (DNS rebinding) — the background poller and aggregator
    would otherwise keep trusting that first check forever. Re-running the
    same check (TTL-cached, see above) closes that gap for anything that came
    from user/operator input: active endpoint, other saved endpoints, and
    PROMETHEUS_URL (_DEFAULT_PROM_URL) are all revalidated here."""
    safe = []
    for u in urls:
        if not u:
            continue
        ok, reason = _cached_is_safe_endpoint_url(u)
        if ok:
            safe.append(u)
        else:
            logger.warning("Skipping Prometheus candidate %s: %s", u, reason)
    return safe

def fetch_url(url, timeout=1.5):
    try:
        req = Request(url, headers={"User-Agent": "InfraWatch/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return resp.read().decode('utf-8', errors='ignore')
    except URLError as e:
        if hasattr(e, 'read'):
            try:
                return e.read().decode('utf-8', errors='ignore')
            except Exception:
                pass
    except Exception:
        pass
    return None

def fetch_prometheus_json(path, use_cache=True, cache_ttl=None, timeout=None):
    global LAST_WORKING_PROMETHEUS_URL, PROMETHEUS_CACHE
    if cache_ttl is None:
        cache_ttl = PROMETHEUS_CACHE_TTL_DEFAULT
    now = time.time()
    _maybe_prune_cache(now)

    endpoints_data = load_endpoints()
    active_url = endpoints_data.get("active")

    cache_key = f"{active_url}:{path}"
    if use_cache:
        with PROMETHEUS_CACHE_LOCK:
            if cache_key in PROMETHEUS_CACHE:
                cached_ts, cached_data, cached_base = PROMETHEUS_CACHE[cache_key]
                if now - cached_ts < cache_ttl:
                    return cached_data, cached_base

    with _fetch_lock_for(cache_key):
        # Re-check: whoever held the lock before us may have just populated it.
        if use_cache:
            with PROMETHEUS_CACHE_LOCK:
                if cache_key in PROMETHEUS_CACHE:
                    cached_ts, cached_data, cached_base = PROMETHEUS_CACHE[cache_key]
                    if time.time() - cached_ts < cache_ttl:
                        return cached_data, cached_base

        # Primary candidates: active endpoint + last known working endpoint
        primary_candidates = []
        if active_url:
            primary_candidates.append(active_url)
        if LAST_WORKING_PROMETHEUS_URL and LAST_WORKING_PROMETHEUS_URL not in primary_candidates:
            primary_candidates.append(LAST_WORKING_PROMETHEUS_URL)
        primary_candidates = _filter_safe_candidates(primary_candidates)

        # Try primary candidates first (2.5s default timeout)
        for base_url in primary_candidates:
            with _FAILED_CANDIDATES_LOCK:
                failed_at = _FAILED_CANDIDATES.get(base_url, 0)
                if now - failed_at < _FAILED_CANDIDATE_TTL and base_url != LAST_WORKING_PROMETHEUS_URL:
                    continue

            req_timeout = 2.5 if timeout is None else timeout
            raw = fetch_url(f"{base_url.rstrip('/')}{path}", timeout=req_timeout)
            if raw:
                try:
                    data = json.loads(raw)
                    # Even if status is error (e.g. invalid query syntax), the server is alive
                    LAST_WORKING_PROMETHEUS_URL = base_url
                    with _FAILED_CANDIDATES_LOCK:
                        _FAILED_CANDIDATES.pop(base_url, None)
                    if data.get('status') == 'success' or 'data' in data:
                        if use_cache:
                            with PROMETHEUS_CACHE_LOCK:
                                PROMETHEUS_CACHE[cache_key] = (time.time(), data, base_url)
                    return data, base_url
                except Exception:
                    with _FAILED_CANDIDATES_LOCK:
                        _FAILED_CANDIDATES[base_url] = time.time()
                    continue
            else:
                with _FAILED_CANDIDATES_LOCK:
                    _FAILED_CANDIDATES[base_url] = time.time()

        # If primary candidates fail (or none exist), fallback to other registered
        # endpoints and _DEFAULT_PROM_URL (PROMETHEUS_URL env, or the
        # prometheus:9090 compose default) — the one an operator actually
        # configured for this deployment, ahead of the other saved endpoints.
        fallback_candidates = []
        if _DEFAULT_PROM_URL not in primary_candidates:
            fallback_candidates.append(_DEFAULT_PROM_URL)
        for ep in endpoints_data.get("endpoints", []):
            if ep not in primary_candidates and ep not in fallback_candidates:
                fallback_candidates.append(ep)
        fallback_candidates = _filter_safe_candidates(fallback_candidates)

        for base_url in fallback_candidates:
            with _FAILED_CANDIDATES_LOCK:
                failed_at = _FAILED_CANDIDATES.get(base_url, 0)
                if now - failed_at < _FAILED_CANDIDATE_TTL:
                    continue

            req_timeout = 0.8 if timeout is None else timeout
            raw = fetch_url(f"{base_url.rstrip('/')}{path}", timeout=req_timeout)
            if raw:
                try:
                    data = json.loads(raw)
                    LAST_WORKING_PROMETHEUS_URL = base_url
                    with _FAILED_CANDIDATES_LOCK:
                        _FAILED_CANDIDATES.pop(base_url, None)
                    if data.get('status') == 'success' or 'data' in data:
                        if use_cache:
                            with PROMETHEUS_CACHE_LOCK:
                                PROMETHEUS_CACHE[cache_key] = (time.time(), data, base_url)
                    return data, base_url
                except Exception:
                    with _FAILED_CANDIDATES_LOCK:
                        _FAILED_CANDIDATES[base_url] = time.time()
                    continue
            else:
                with _FAILED_CANDIDATES_LOCK:
                    _FAILED_CANDIDATES[base_url] = time.time()
        return None, None

# ── Rate limiting ─────────────────────────────────────────────────────────────
# Lightweight in-process fixed-window limiter. No Redis/external store: this
# app runs as a single gunicorn worker (Dockerfile), so a plain dict guarded
# by a lock is sufficient — same pattern as the caches above. A production
# deployment behind a reverse proxy can layer proxy-level rate limiting on
# top of this; this is the floor, not the only line of defense.
_RATE_BUCKETS = {}
_RATE_BUCKETS_LOCK = threading.Lock()
_RATE_LAST_PRUNE = [0.0]
_RATE_PRUNE_INTERVAL = 60.0

def _client_identity():
    # Session user identity, M2M API key, or fallback to remote IP.
    if session.get("user_id"):
        return f"user:{session.get('user_id')}"
    header = request.headers.get("X-API-Key") or request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        header = header[len("Bearer "):]
    if header:
        return f"key:{header}"
    return f"ip:{request.remote_addr or 'unknown'}"

def rate_limit(max_calls, per_seconds):
    """At most max_calls per per_seconds, per (route, client identity).
    Fixed-window, not sliding — a request right at a window boundary can
    momentarily allow close to 2x max_calls; an acceptable trade for a
    NOC-internal tool over a real sliding-window implementation."""
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            now = time.time()
            with _RATE_BUCKETS_LOCK:
                if now - _RATE_LAST_PRUNE[0] > _RATE_PRUNE_INTERVAL:
                    _RATE_LAST_PRUNE[0] = now
                    # Each key carries its own route's per_seconds (k[3]), so
                    # staleness is judged against that window's own end time —
                    # not a window index recomputed from whichever route
                    # happened to trigger this prune pass (that mixed windows
                    # across routes with different per_seconds and could wipe
                    # another route's bucket mid-window, resetting its quota
                    # early).
                    stale = [k for k in _RATE_BUCKETS if (k[2] + 1) * k[3] <= now]
                    for k in stale:
                        _RATE_BUCKETS.pop(k, None)

                window = int(now // per_seconds)
                key = (f.__name__, _client_identity(), window, per_seconds)
                count = _RATE_BUCKETS.get(key, 0) + 1
                _RATE_BUCKETS[key] = count

            if count > max_calls:
                return jsonify({"ok": False, "error": "Rate limit exceeded, try again shortly"}), 429
            return f(*args, **kwargs)
        return wrapped
    return decorator

@app.route('/')
def index():
    # No API key is rendered here. The dashboard is a read-only LAN wallboard;
    # the mutation credential (@require_api_key routes) is entered by an
    # operator client-side (see apiFetch()/authHeaders() in alarm.js) and
    # kept only in that browser's localStorage, never shipped to every viewer.
    return render_template('alarm.html')

# Set whenever /webhook receives a real Alertmanager delivery — the poller
# fallback below checks this and backs off, since Alertmanager (when present)
# is the authoritative source per the "never fabricate events" requirement.
_LAST_WEBHOOK_AT = [0.0]

# ── Maintenance windows ──────────────────────────────────────────────────────
# Planned downtime for a host or a whole job/group. Checked from inside
# record_alert_event() — the single place both the webhook and the poller
# funnel through — so a maintenance window suppresses alerts/alarms without a
# second alert pipeline.
def load_maintenance_windows():
    """SQLite (MaintenanceRepository) is the sole source of truth.
    maintenance.json used to be written alongside every create/delete and
    merged in here by id — a second, non-transactional copy that could
    silently drift from the DB (the exact failure mode this now avoids by
    construction: there's only one write path)."""
    try:
        return MaintenanceRepository.list_windows()
    except Exception:
        return []

def _parse_epoch_ts(val):
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        try:
            dt = datetime.fromisoformat(str(val).replace('Z', '+00:00'))
            return dt.timestamp()
        except Exception:
            return 0.0

def get_active_maintenance(instance, job=None, windows=None):
    """First maintenance window currently covering this instance/job, or None."""
    if windows is None:
        windows = load_maintenance_windows()
    now = time.time()
    for w in windows:
        start_raw = w.get('start_epoch') if w.get('start_epoch') is not None else w.get('start', 0)
        end_raw = w.get('end_epoch') if w.get('end_epoch') is not None else w.get('end', 0)
        start_ts = _parse_epoch_ts(start_raw)
        end_ts = _parse_epoch_ts(end_raw)
        if not (start_ts <= now <= end_ts):
            continue
        scope = w.get('scope') or w.get('scope_type')
        target = w.get('target') or w.get('scope_target')
        if scope == 'job':
            if job and target == job:
                return w
        elif target == instance:
            return w
    return None

def record_alert_event(name, severity, instance, summary, job, event_time, is_now_firing,
                        receiver='', generatorURL='', key=None, latency_ms=None):
    """Applies one alert firing/resolved transition to status.json/logs.json/
    history.json and SQLite database. Shared by the Alertmanager webhook and the Prometheus-poller
    fallback so both get identical transition-only dedupe and incident-history reconciliation.
    Returns False if this was a repeat notification with no actual state
    change (already firing, or already resolved).
    """
    key = key or f"{name}|{instance}"

    with _WEBHOOK_LOCK:
        if is_now_firing and get_active_maintenance(instance, job):
            return False  # suppressed: instance/job is under an active maintenance window.

        status_data = load_json(STATUS_FILE, {"status": "NORMAL", "alerts": [], "updated": event_time})
        active_alerts = {}
        for a in status_data.get('alerts', []):
            k = a.get('key') or f"{a.get('name')}|{a.get('instance')}"
            active_alerts[k] = a

        was_firing = key in active_alerts
        if was_firing == is_now_firing:
            return False  # no state transition — dedupe repeat notifications

        # SQLite keeps its own was_firing/is_now_firing dedup (needed so two
        # worker processes racing on the same transition still land exactly
        # once — see test_persistence_concurrency.py). That means a failed
        # write here isn't just a missed record: it leaves SQLite's row on
        # stale status, so the *next* real transition for this key can look
        # like a no-op to SQLite's own dedup and get silently dropped too —
        # permanently desyncing /history from /status. One retry closes the
        # common transient case (lock contention, disk hiccup) cheaply.
        # ponytail: not a full reconciliation job; a periodic sweep that
        # reconciles SQLite incident status against status.json would close
        # the rest, add if repeated failures show up in the error log.
        for attempt in (1, 2):
            try:
                IncidentRepository.record_alert_event(
                    name=name, severity=severity, instance=instance, summary=summary,
                    job=job, event_time=event_time, is_now_firing=is_now_firing,
                    receiver=receiver, generatorURL=generatorURL, key=key, latency_ms=latency_ms
                )
                break
            except Exception as e:
                if attempt == 2:
                    logger.error(f"Error updating SQLite incident repository (gave up after retry): {e}")

        duration_seconds = None
        if is_now_firing:
            active_alerts[key] = {
                "key":      key,
                "name":     name,
                "severity": severity,
                "instance": instance,
                "summary":  summary,
                "time":     event_time
            }
        else:
            started = active_alerts.pop(key, None)
            if started:
                duration_seconds = max(0, event_time - started.get('time', event_time))

        logs = load_json(LOGS_FILE, [])
        logs.insert(0, {
            "time":            event_time,
            "event":           "firing" if is_now_firing else "resolved",
            "name":            name,
            "severity":        severity,
            "instance":        instance,
            "summary":         summary,
            "job":             job,
            "receiver":        receiver,
            "generatorURL":    generatorURL,
            "latency_ms":      latency_ms,
            "duration_seconds": duration_seconds,
        })

        history = load_json(HISTORY_FILE, [])
        if is_now_firing:
            history.insert(0, {
                "key":          key,
                "name":         name,
                "severity":     severity,
                "instance":     instance,
                "summary":      summary,
                "time":         event_time,
                "status":       "firing",
                "job":          job,
                "receiver":     receiver,
                "generatorURL": generatorURL,
            })
        else:
            for h in history:
                if h.get('key') == key and h.get('status') == 'firing':
                    h['status'] = 'resolved'
                    h['resolved_time'] = event_time
                    h['duration_seconds'] = (
                        duration_seconds if duration_seconds is not None
                        else max(0, event_time - h.get('time', event_time))
                    )
                    break

        firing_list = list(active_alerts.values())
        if any(a.get('severity', 'critical') == 'critical' for a in firing_list):
            status_state = "CRITICAL"
        elif firing_list:
            status_state = "WARNING"
        else:
            status_state = "NORMAL"

        save_json(STATUS_FILE, {"status": status_state, "alerts": firing_list, "updated": time.time()})
        save_json(LOGS_FILE, logs[:MAX_LOGS])
        save_with_retention(HISTORY_FILE, HISTORY_ARCHIVE_FILE, history, MAX_HISTORY)

        # Asynchronously dispatch Telegram notification on verified state transition
        try:
            dispatch_alert_async(
                name=name,
                severity=severity,
                instance=instance,
                summary=summary,
                job=job,
                event_time=event_time,
                is_now_firing=is_now_firing,
                duration_seconds=duration_seconds,
                latency_ms=latency_ms
            )
        except Exception as e:
            logger.error(f"Error dispatching telegram alert: {e}")

        return True

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
    if not password or len(password) < 6:
        return jsonify({"ok": False, "error": "Password must be at least 6 characters"}), 400
    if confirm and password != confirm:
        return jsonify({"ok": False, "error": "Passwords do not match"}), 400

    pw_hash = hash_password(password)
    user = UserRepository.create_first_admin(username=username, password_hash=pw_hash, display_name=display_name)
    if not user:
        return jsonify({"ok": False, "error": "System already initialized with an administrator"}), 409

    # Automatically create session and log in the first admin
    session["user_id"] = user["id"]
    session.permanent = True
    UserRepository.update_last_login(user["id"])

    AuditLogRepository.record_action(
        actor_username=username,
        actor_role="admin",
        action="SYSTEM_SETUP",
        resource="user:admin",
        details="Initial administrator account created"
    )
    user_info = get_current_authenticated_user()
    return jsonify({"ok": True, "user": user_info})

@app.route('/api/auth/login', methods=['POST'])
@rate_limit(20, 60)
def auth_login_api():
    data = request.json or {}
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")

    if not username or not password:
        return jsonify({"ok": False, "error": "Username and password are required"}), 400

    user_with_hash = UserRepository.get_by_username(username, include_password_hash=True)
    if not user_with_hash or not user_with_hash.get("is_active"):
        return jsonify({"ok": False, "error": "Invalid username or password"}), 401

    if not verify_password(password, user_with_hash.get("password_hash", "")):
        return jsonify({"ok": False, "error": "Invalid username or password"}), 401

    session["user_id"] = user_with_hash["id"]
    session.permanent = True
    UserRepository.update_last_login(user_with_hash["id"])

    user_info = get_current_authenticated_user()
    AuditLogRepository.record_action(
        actor_username=user_info["username"],
        actor_role=user_info["role"],
        action="USER_LOGIN",
        resource=f"user:{user_info['username']}",
        details="User logged in via web session"
    )
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
    if not password or len(password) < 6:
        return jsonify({"ok": False, "error": "Password must be at least 6 characters"}), 400
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
    if password is not None and len(password) < 6:
        return jsonify({"ok": False, "error": "Password must be at least 6 characters"}), 400

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
@app.route('/status')
def status():
    state = build_canonical_monitoring_state()
    return jsonify({
        "status": state.get("system_status", "NORMAL"),
        "system_status": state.get("system_status", "NORMAL"),
        "alerts": state.get("active_alerts", []),
        "summary": state.get("summary", {}),
        "updated": state.get("updated", time.time())
    })

@app.route('/history')
def history():
    try:
        data = IncidentRepository.get_history(limit=MAX_HISTORY)
        if data:
            return jsonify(data)
    except Exception:
        pass
    return jsonify(load_json(HISTORY_FILE, []))

@app.route('/logs')
def logs():
    try:
        limit = int(request.args.get('limit', 50))
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, MAX_LOGS))
    try:
        data = EventLogRepository.get_logs(limit=limit)
        if data:
            return jsonify(data)
    except Exception:
        pass
    data = load_json(LOGS_FILE, [])
    return jsonify(data[:limit])

# SQLite (DeletedTargetRepository) is the sole source of truth — deleted_targets.json
# used to be read first (when present) and written on every mutation as a
# parallel copy; both call sites now go straight through SQLite instead.
def load_deleted_targets():
    try:
        return DeletedTargetRepository.list_deleted()
    except Exception:
        return []

def save_deleted_targets(deleted_list):
    """Diffs deleted_list (the caller's full desired list, read-modified-write
    style — see /api/targets POST|DELETE) against SQLite's current state and
    applies add/restore so SQLite ends up matching it."""
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
        raw = fetch_url(f"{ep.rstrip('/')}/api/v1/status/flags", timeout=0.25)
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
    global LAST_WORKING_PROMETHEUS_URL
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
            print(f"Error adding endpoint {url}: {e}", flush=True)
            return jsonify({"ok": False, "error": "Failed to save endpoint"}), 500

        if set_active:
            LAST_WORKING_PROMETHEUS_URL = url
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
    global LAST_WORKING_PROMETHEUS_URL
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
        LAST_WORKING_PROMETHEUS_URL = url
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
    global LAST_WORKING_PROMETHEUS_URL
    body = request.json or {}
    url = body.get('url', '').strip()

    with _WEBHOOK_LOCK:
        data = load_endpoints()

        if url not in data["endpoints"]:
            return jsonify({"ok": False, "error": "Prometheus Endpoint not found"}), 404

        if len(data["endpoints"]) <= 1:
            return jsonify({"ok": False, "error": "Cannot delete the last remaining endpoint"}), 400

        was_active = (data["active"] == url)
        try:
            EndpointRepository.delete_endpoint(url)
        except Exception:
            pass

        _ENDPOINTS_CACHE["data"] = None
        data = load_endpoints()
        if was_active:
            LAST_WORKING_PROMETHEUS_URL = data["active"]
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
    raw_targets, _ = fetch_prometheus_json('/api/v1/targets')
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
    raw_targets, _ = fetch_prometheus_json('/api/v1/targets')
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

@app.route('/api/targets', methods=['GET'])
def get_targets_api():
    return jsonify({"ok": True, "targets": load_website_targets()})

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

    with _WEBHOOK_LOCK:
        # Remove from deleted_targets if previously deleted
        deleted = load_deleted_targets()
        if url in deleted:
            deleted.remove(url)
            save_deleted_targets(deleted)

        current = load_website_targets()
        if url not in current:
            current.append(url)
            save_website_targets(current)

    AuditLogRepository.record_action(
        actor_username=g.current_user.get("username", "admin"),
        actor_role=g.current_user.get("role", "admin"),
        action="ADD_TARGET",
        resource=url,
        details="Added website/IP target"
    )
    return jsonify({"ok": True, "targets": current})

@app.route('/api/targets', methods=['DELETE'])
@rate_limit(20, 60)
@require_permission('targets.write')
def delete_target_api():
    data = request.json or {}
    url = data.get('url', '').strip()
    if not url:
        return jsonify({"ok": False, "error": "IP / Target host is required"}), 400
    
    with _WEBHOOK_LOCK:
        current = load_website_targets()
        if url in current:
            current.remove(url)
            save_website_targets(current)

        deleted = load_deleted_targets()
        if url not in deleted:
            deleted.append(url)
            save_deleted_targets(deleted)

    AuditLogRepository.record_action(
        actor_username=g.current_user.get("username", "admin"),
        actor_role=g.current_user.get("role", "admin"),
        action="DELETE_TARGET",
        resource=url,
        details="Deleted website/IP target"
    )
    return jsonify({"ok": True})

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

    with _WEBHOOK_LOCK:
        try:
            window = MaintenanceRepository.create_window(
                scope=scope, target=target, reason=reason, start=float(start), end=float(end)
            )
        except Exception:
            window = {
                "id": f"mw_{int(time.time() * 1000)}",
                "scope": scope,
                "target": target,
                "reason": reason,
                "start": start,
                "end": end,
                "created_at": int(time.time()),
            }

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

# ── Alert Correlation (Phase 12) ─────────────────────────────────────────────
# A "depends on" link between two instances, used only to decide what to
# visually de-emphasize on the wallboard when both ends are down at once.
# Never touches status/logs/history — the underlying alert for a suppressed
# child still fires and is recorded exactly as if this feature didn't exist.
def load_dependencies():
    """SQLite (DependencyRepository) is the sole source of truth — see
    load_maintenance_windows() earlier in this file for why dependencies.json's
    old dual-write (SQLite + a separate JSON copy) was removed rather than
    kept as a merge-by-id fallback."""
    try:
        return DependencyRepository.list_dependencies()
    except Exception:
        return []

def apply_correlation_suppression(targets, parent_map):
    """Pure function: tags each target in-place with dependsOn/suppressedBy.
    A target is suppressedBy=<parent> only when it's down AND its declared
    parent is also down — never touches health/downSince, so the real alert
    for a suppressed child is unaffected."""
    down_instances = {t['instance'] for t in targets if t['health'] != 'up'}
    for t in targets:
        parent = parent_map.get(t['instance'])
        t['dependsOn'] = parent
        t['suppressedBy'] = parent if (t['health'] != 'up' and parent in down_instances) else None
    return targets

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
        except Exception:
            dep = {"id": f"dep_{int(time.time() * 1000)}", "child": child, "parent": parent, "created_at": int(time.time())}

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

def fetch_prom_query_map(query_expr, cache_ttl=5.0, timeout=None):
    raw, base = fetch_prometheus_json(f"/api/v1/query?query={quote(query_expr)}", use_cache=True, cache_ttl=cache_ttl, timeout=timeout)
    val_map = {}
    if raw and raw.get('status') == 'success':
        results = raw.get('data', {}).get('result', [])
        for r in results:
            labels = r.get('metric', {})
            inst = labels.get('instance') or labels.get('target') or labels.get('url')
            val = r.get('value', [None, None])[1]
            if inst and val is not None:
                val_map[inst] = val
    return val_map

def fetch_down_since_prom_map(cache_ttl=10.0):
    last_up_map = {}

    # 1. PromQL query for exact last UP timestamp for targets that were previously UP
    query_up = 'max_over_time(timestamp(probe_success == 1)[1d:15s])'
    raw_up, _ = fetch_prometheus_json(f"/api/v1/query?query={quote(query_up)}", use_cache=True, cache_ttl=cache_ttl)
    if not raw_up or not raw_up.get('data', {}).get('result'):
        query_up = 'max_over_time(timestamp(up == 1)[1d:15s])'
        raw_up, _ = fetch_prometheus_json(f"/api/v1/query?query={quote(query_up)}", use_cache=True, cache_ttl=cache_ttl)

    if raw_up and raw_up.get('status') == 'success':
        for r in raw_up.get('data', {}).get('result', []):
            metric = r.get('metric', {})
            inst = metric.get('instance') or metric.get('target') or metric.get('url')
            val = r.get('value', [None, None])[1]
            if inst and val is not None:
                try:
                    last_up_map[inst] = float(val)
                except ValueError:
                    pass

    # 2. PromQL query for initial DOWN timestamp from Prometheus log history for targets continuously DOWN
    query_down = 'min_over_time(timestamp(probe_success == 0)[1d:1m])'
    raw_down, _ = fetch_prometheus_json(f"/api/v1/query?query={quote(query_down)}", use_cache=True, cache_ttl=cache_ttl)
    if raw_down and raw_down.get('status') == 'success':
        for r in raw_down.get('data', {}).get('result', []):
            metric = r.get('metric', {})
            inst = metric.get('instance') or metric.get('target') or metric.get('url')
            val = r.get('value', [None, None])[1]
            if inst and val is not None and inst not in last_up_map:
                try:
                    last_up_map[inst] = float(val)
                except ValueError:
                    pass

    return last_up_map

# ── Scrape failure classification ────────────────────────────────────────────
# Single source of truth for turning raw Prometheus/blackbox_exporter data into
# an operator-readable category. Called from /instances, the poller's alert
# summary, and the custom-target branch — nowhere else re-derives this. Never
# replaces lastError/httpStatusCode/probe_success; only adds to them.
def classify_scrape_failure(health, last_error, http_status):
    if health == 'up':
        return {"category": None, "detail": None}
    if health == 'unknown':
        return {"category": "Unknown", "detail": "No probe data yet"}

    err = (last_error or '').lower()
    if err:
        if 'no such host' in err:
            return {"category": "DNS", "detail": last_error}
        if 'no route to host' in err:
            return {"category": "No Route", "detail": last_error}
        if 'connection refused' in err:
            return {"category": "Refused", "detail": last_error}
        if 'context deadline exceeded' in err or 'i/o timeout' in err:
            return {"category": "Timeout", "detail": last_error}
        if 'x509' in err or 'certificate' in err or 'tls' in err:
            return {"category": "TLS", "detail": last_error}

    if http_status:
        try:
            code = int(float(http_status))
            if code > 0:
                return {"category": f"HTTP {code}", "detail": f"HTTP {code}"}
        except (TypeError, ValueError):
            pass

    if last_error:
        return {"category": "Unknown", "detail": last_error}

    return {"category": "Unknown", "detail": "No error detail available (probe_success=0)"}

def fetch_all_probe_metrics(cache_ttl=3.0, timeout=None):
    f_succ = _SHARED_EXECUTOR.submit(fetch_prom_query_map, "probe_success", cache_ttl, timeout)
    f_dur = _SHARED_EXECUTOR.submit(fetch_prom_query_map, "probe_duration_seconds", cache_ttl, timeout)
    f_code = _SHARED_EXECUTOR.submit(fetch_prom_query_map, "probe_http_status_code", cache_ttl, timeout)

    success_map = f_succ.result()
    duration_map = f_dur.result()
    status_code_map = f_code.result()

    if not success_map:
        success_map = fetch_prom_query_map("up", cache_ttl=cache_ttl, timeout=timeout)

    return success_map, duration_map, status_code_map

# ── Canonical Monitoring State Engine ───────────────────────────────────────
def _derive_probe_readings(keys, probe_success_map, probe_duration_map, probe_status_code_map,
                            down_since_prom_map, raw_target=None):
    """Shared by both target-enrichment loops in build_canonical_monitoring_state:
    derive health/down_since/response_time_ms/http_code/last_error from the
    Prometheus probe metric maps for a target identified by one or more
    lookup keys (instance name and/or scrapeUrl — checked in order, first
    match wins).

    raw_target, when given (the /api/v1/targets entry for a target Prometheus
    itself discovered), supplies two extra fallbacks the probe-target path
    always had: its own `health` field when probe_success has no reading yet,
    and `lastScrapeDuration` when probe_duration_seconds has no reading yet —
    plus lastError for classification. Custom (manually-added, non-Prometheus
    -discovered) targets pass raw_target=None and keep the narrower fallbacks
    (health='unknown', response_time_ms=0.0, last_error='') they always had.

    This only extracts what both loops were already computing identically —
    it does not change either path's behavior, including one small existing
    difference the two call sites had for http_code: the probe-target path
    treats probe_status_code_map's raw value with `is not None` (so a real
    "0" — Blackbox's code for "no HTTP response" — comes through as 0), while
    the custom-target path used a truthy check (so "0"/0 there resolves to
    None). Preserved via the raw_target branch below rather than silently
    unified, since nothing here proves which behavior is "correct"."""
    def _first(mapping):
        for k in keys:
            if k in mapping:
                return mapping[k]
        return None

    p_success = _first(probe_success_map)
    if p_success is not None:
        health = 'up' if str(p_success) in ('1', '1.0') else 'down'
    elif raw_target is not None:
        health = raw_target.get('health', 'unknown')
    else:
        health = 'unknown'

    if health != 'up':
        last_up = _first(down_since_prom_map)
        down_since_val = int(last_up) if last_up else 0
    else:
        down_since_val = 0

    p_duration = _first(probe_duration_map)
    if p_duration is not None:
        try:
            response_time_ms = round(float(p_duration) * 1000, 1)
        except ValueError:
            response_time_ms = 0.0
    elif raw_target is not None:
        scrape_dur = raw_target.get('lastScrapeDuration')
        if scrape_dur is not None:
            try:
                response_time_ms = round(float(scrape_dur) * 1000, 1)
            except ValueError:
                response_time_ms = 0.0
        else:
            response_time_ms = 0.0
    else:
        response_time_ms = 0.0

    p_code = _first(probe_status_code_map)
    if raw_target is not None:
        if p_code is not None:
            try:
                http_code = int(float(p_code))
            except ValueError:
                http_code = None
        else:
            http_code = None
    else:
        try:
            http_code = int(float(p_code)) if p_code else None
        except ValueError:
            http_code = None

    last_error = raw_target.get('lastError', '') if raw_target is not None else ''
    return health, down_since_val, response_time_ms, http_code, last_error

def build_canonical_monitoring_state(job_param=None):
    if job_param is None:
        job_param = DEFAULT_JOB_FILTER

    f_targets = _SHARED_EXECUTOR.submit(fetch_prometheus_json, '/api/v1/targets', True, 3.0)
    f_metrics = _SHARED_EXECUTOR.submit(fetch_all_probe_metrics, 3.0)

    raw_targets, active_base = f_targets.result()
    probe_success_map, probe_duration_map, probe_status_code_map = f_metrics.result()

    # Conditional query: only run heavy 1-day subqueries when down targets exist
    has_down = False
    if probe_success_map:
        has_down = any(str(v) in ('0', '0.0') for v in probe_success_map.values())
    if not has_down and raw_targets and raw_targets.get('status') == 'success':
        has_down = any(t.get('health') != 'up' for t in raw_targets.get('data', {}).get('activeTargets', []))

    if has_down:
        down_since_prom_map = fetch_down_since_prom_map(cache_ttl=10.0)
    else:
        down_since_prom_map = {}

    config_web_targets = load_website_targets()
    deleted_targets = set(load_deleted_targets())
    now_ts = int(time.time())

    if raw_targets is None and not config_web_targets:
        return {
            "ok": False,
            "error": "Prometheus engine is unreachable",
            "system_status": "CRITICAL",
            "summary": {
                "total": 0, "up": 0, "down": 0, "slow": 0, "maintenance": 0,
                "suppressed": 0, "alarmable_down": 0, "alarmable_alerts": 0,
                "unacknowledged_down": 0, "acknowledged_down": 0,
                "is_acknowledged": False, "has_alarm": True,
                "has_unacknowledged_alarm": True, "system_status": "CRITICAL"
            },
            "active_alerts": [],
            "targets": []
        }

    available_jobs = set()
    if raw_targets and raw_targets.get('status') == 'success':
        for t in raw_targets.get('data', {}).get('activeTargets', []):
            j = t.get('labels', {}).get('job') or t.get('scrapePool')
            if j and j != 'prometheus':
                available_jobs.add(j)

    # Load active alerts from status.json (populated by webhook & synthetic poller) or SQLite
    status_data = load_json(STATUS_FILE, None)
    if status_data is not None and isinstance(status_data, dict) and "alerts" in status_data:
        active_alerts_list = status_data.get('alerts', [])
    else:
        try:
            active_alerts_list = IncidentRepository.get_active_incidents()
        except Exception:
            active_alerts_list = []

    alerts_by_instance = {}
    for a in active_alerts_list:
        inst = a.get('instance')
        if inst:
            alerts_by_instance.setdefault(inst, []).append(a)

    result = []
    seen_instances = set()

    if raw_targets and raw_targets.get('status') == 'success':
        targets = raw_targets.get('data', {}).get('activeTargets', [])
        for t in targets:
            labels = t.get('labels', {})
            job = labels.get('job') or t.get('scrapePool') or ''
            scrape_pool = t.get('scrapePool', '')
            inst_name = labels.get('instance', t.get('scrapeUrl', '?'))
            scrape_url = t.get('scrapeUrl', inst_name)

            # Filter targets by job matching
            if job != 'prometheus' and matches_job_filter(job, scrape_pool, job_param) and inst_name not in deleted_targets and scrape_url not in deleted_targets:
                seen_instances.add(inst_name)
                seen_instances.add(scrape_url)

                health, down_since_val, response_time_ms, http_code, last_error = _derive_probe_readings(
                    (inst_name, scrape_url), probe_success_map, probe_duration_map,
                    probe_status_code_map, down_since_prom_map, raw_target=t
                )
                classification = classify_scrape_failure(health, last_error, http_code)

                matched_alerts = alerts_by_instance.get(inst_name, []) + [
                    a for a in alerts_by_instance.get(scrape_url, [])
                    if a not in alerts_by_instance.get(inst_name, [])
                ]

                result.append({
                    "instance":        inst_name,
                    "job":             job or "blackbox",
                    "health":          health,
                    "probe_state":     health,
                    "responseTimeMs":  response_time_ms,
                    "httpStatusCode":  http_code,
                    "lastScrape":      t.get('lastScrape', ''),
                    "scrapeUrl":       scrape_url,
                    "lastError":       last_error,
                    "failureCategory": classification["category"],
                    "failureDetail":   classification["detail"],
                    "labels":          labels,
                    "isWeb":           False,
                    "downSince":       down_since_val,
                    "active_alerts":   matched_alerts
                })

    # Merge custom user-added targets
    for target_url in config_web_targets:
        if matches_job_filter("custom", "custom", job_param) and target_url not in seen_instances and target_url not in deleted_targets:
            health, down_since_val, response_time_ms, http_code, last_error = _derive_probe_readings(
                (target_url,), probe_success_map, probe_duration_map,
                probe_status_code_map, down_since_prom_map, raw_target=None
            )
            classification = classify_scrape_failure(health, last_error, http_code)
            matched_alerts = alerts_by_instance.get(target_url, [])

            result.append({
                "instance":        target_url,
                "job":             "custom",
                "health":          health,
                "probe_state":     health,
                "responseTimeMs":  response_time_ms,
                "httpStatusCode":  http_code,
                "lastScrape":      "—",
                "scrapeUrl":       target_url,
                "lastError":       "",
                "failureCategory": classification["category"],
                "failureDetail":   classification["detail"],
                "labels":          {"job": "custom", "instance": target_url},
                "isWeb":           False,
                "downSince":       down_since_val,
                "active_alerts":   matched_alerts
            })
            seen_instances.add(target_url)

    # Merge any non-probed targets with active alerts (e.g. Host/OS-level CPU, Memory, Disk alerts)
    for inst, inst_alerts in alerts_by_instance.items():
        if inst not in seen_instances and inst not in deleted_targets:
            alert_job = inst_alerts[0].get('job', 'alertmanager') if inst_alerts else 'alertmanager'
            if matches_job_filter(alert_job, alert_job, job_param):
                is_any_down = any(a.get('name') == ALERTNAME_TARGET_DOWN for a in inst_alerts)
                health = 'down' if is_any_down else 'up'
                down_since_val = inst_alerts[0].get('time', 0) if is_any_down else 0
                result.append({
                    "instance":        inst,
                    "job":             alert_job,
                    "health":          health,
                    "probe_state":     health,
                    "responseTimeMs":  0.0,
                    "httpStatusCode":  None,
                    "lastScrape":      "—",
                    "scrapeUrl":       inst,
                    "lastError":       "",
                    "failureCategory": "Alertmanager" if health != 'up' else None,
                    "failureDetail":   inst_alerts[0].get('summary', '') if inst_alerts else '',
                    "labels":          {"job": alert_job, "instance": inst},
                    "isWeb":           False,
                    "downSince":       down_since_val,
                    "active_alerts":   inst_alerts
                })
                seen_instances.add(inst)

    # Attach maintenance state
    maintenance_windows = load_maintenance_windows()
    for item in result:
        mw = get_active_maintenance(item['instance'], item.get('job'), windows=maintenance_windows)
        item['maintenance'] = bool(mw)
        item['maintenanceId'] = mw.get('id') if mw else None
        item['maintenanceUntil'] = mw.get('end') if mw else None
        item['maintenanceReason'] = mw.get('reason', '') if mw else ''

    # Alert correlation (dependency suppression)
    deps = load_dependencies()
    parent_map = {d['child']: d['parent'] for d in deps}
    apply_correlation_suppression(result, parent_map)
    dep_id_map = {d['child']: d['id'] for d in deps}
    for item in result:
        item['dependencyId'] = dep_id_map.get(item['instance'])
        item['is_suppressed'] = bool(item.get('suppressedBy') or item.get('maintenance'))

    # Attach global alert acknowledgments from SQLite
    try:
        active_acks = AcknowledgmentRepository.get_active_acknowledgments()
    except Exception:
        active_acks = {}

    for item in result:
        ack_rec = active_acks.get(item['instance'])
        item['acknowledged'] = bool(ack_rec)
        item['acknowledged_by'] = ack_rec['acknowledged_by'] if ack_rec else None
        item['acknowledged_at'] = ack_rec['acknowledged_at'] if ack_rec else None

    # Derive authoritative target-level severity & effective_status
    for item in result:
        is_down = item['health'] != 'up'
        is_maint = item['maintenance']
        is_supp = bool(item.get('suppressedBy'))
        is_slow = (item['health'] == 'up' and item['responseTimeMs'] > 500)
        has_crit_alert = any(a.get('severity') == 'critical' for a in item.get('active_alerts', []))
        has_warn_alert = any(a.get('severity') == 'warning' for a in item.get('active_alerts', []))

        # Is target actionable / alarmable?
        is_alarmable = (is_down or has_crit_alert or has_warn_alert) and not is_maint and not is_supp
        item['is_alarmable'] = is_alarmable

        # Effective severity
        if is_maint:
            item['severity'] = "maintenance"
        elif is_supp:
            item['severity'] = "suppressed"
        elif is_down or has_crit_alert:
            item['severity'] = "critical"
        elif is_slow or has_warn_alert:
            item['severity'] = "warning"
        else:
            item['severity'] = "ok"

        # Effective status
        if is_maint:
            item['effective_status'] = "maintenance"
        elif is_supp:
            item['effective_status'] = "suppressed"
        elif is_down:
            item['effective_status'] = "down"
        elif is_slow or has_warn_alert:
            item['effective_status'] = "degraded"
        else:
            item['effective_status'] = "up"

    # Auto-clean acknowledgments for targets that have recovered (health == 'up').
    # `result` is filtered by job_param, so it only has full down-state visibility
    # on the unfiltered ("all") sweep -- running this on a job-scoped view would
    # see every other job's down instances as "absent" and wipe their acks.
    if not job_param or job_param.lower() in ('all', '*'):
        try:
            active_down_set = {t['instance'] for t in result if t['health'] != 'up'}
            AcknowledgmentRepository.clear_resolved(active_down_set)
        except Exception:
            pass

    # Compute authoritative global system metrics & status
    total = len(result)
    up_count = sum(1 for t in result if t['health'] == 'up')
    down_count = sum(1 for t in result if t['health'] != 'up')
    slow_count = sum(1 for t in result if t['health'] == 'up' and t['responseTimeMs'] > 500)
    maint_count = sum(1 for t in result if t['maintenance'])
    supp_count = sum(1 for t in result if t.get('suppressedBy'))
    alarmable_down = sum(1 for t in result if t['is_alarmable'] and t['health'] != 'up')
    alarmable_alerts = sum(len(t.get('active_alerts', [])) for t in result if t['is_alarmable'])
    unacked_down = sum(1 for t in result if t['is_alarmable'] and t['health'] != 'up' and not t.get('acknowledged'))
    acked_down = sum(1 for t in result if t['is_alarmable'] and t['health'] != 'up' and t.get('acknowledged'))

    has_critical = (alarmable_down > 0 or any(
        a.get('severity') == 'critical' for t in result if t['is_alarmable'] for a in t.get('active_alerts', [])
    ))
    has_warning = (slow_count > 0 or any(
        a.get('severity') == 'warning' for t in result if t['is_alarmable'] for a in t.get('active_alerts', [])
    ))

    if has_critical:
        system_status = "CRITICAL"
    elif has_warning:
        system_status = "WARNING"
    else:
        system_status = "NORMAL"

    has_alarm = (alarmable_down > 0 or alarmable_alerts > 0)
    # Global ACK state: true if all alarmable down targets are acknowledged
    is_globally_acknowledged = (alarmable_down > 0 and unacked_down == 0)

    summary = {
        "total": total,
        "up": up_count,
        "down": down_count,
        "slow": slow_count,
        "maintenance": maint_count,
        "suppressed": supp_count,
        "alarmable_down": alarmable_down,
        "alarmable_alerts": alarmable_alerts,
        "unacknowledged_down": unacked_down,
        "acknowledged_down": acked_down,
        "is_acknowledged": is_globally_acknowledged,
        "has_alarm": has_alarm,
        "has_unacknowledged_alarm": (unacked_down > 0),
        "system_status": system_status
    }

    # Aggregate active alerts across fleet
    fleet_active_alerts = []
    seen_alert_keys = set()
    for t in result:
        for a in t.get('active_alerts', []):
            k = a.get('key') or f"{a.get('name')}|{a.get('instance')}"
            if k not in seen_alert_keys:
                seen_alert_keys.add(k)
                fleet_active_alerts.append(a)

    return {
        "ok": True,
        "system_status": system_status,
        "summary": summary,
        "active_alerts": fleet_active_alerts,
        "targets": result,
        "available_jobs": sorted(list(available_jobs)),
        "job_filter": job_param,
        "source": "prometheus" if active_base else "local",
        "prometheus_url": active_base,
        "updated": now_ts
    }

# ── Instances & Real-time Metrics API ─────────────────────────────────────────
@app.route('/instances')
@rate_limit(120, 60)
def instances():
    job_param = request.args.get('job', DEFAULT_JOB_FILTER)
    state = build_canonical_monitoring_state(job_param)
    if not state.get('ok') and state.get('error'):
        return jsonify(state), 503
    return jsonify(state)

def get_instance_job_map(job_filter=None):
    """Instance -> real Prometheus job name for every monitored target matching
    job_filter. Shared by get_monitored_instances() and the availability
    aggregator so persisted buckets get tagged with the target's actual job
    instead of a hardcoded guess."""
    if job_filter is None:
        job_filter = DEFAULT_JOB_FILTER
    raw_targets, _ = fetch_prometheus_json('/api/v1/targets', use_cache=True, cache_ttl=3.0)
    config_web_targets = load_website_targets()
    deleted_targets = set(load_deleted_targets())

    job_map = {}
    if raw_targets and raw_targets.get('status') == 'success':
        targets = raw_targets.get('data', {}).get('activeTargets', [])
        for t in targets:
            labels = t.get('labels', {})
            job = labels.get('job') or t.get('scrapePool') or ''
            scrape_pool = t.get('scrapePool', '')
            inst_name = labels.get('instance', t.get('scrapeUrl', '?'))
            if job != 'prometheus' and matches_job_filter(job, scrape_pool, job_filter) and inst_name not in deleted_targets:
                job_map[inst_name] = job or scrape_pool or 'blackbox'

    for target_url in config_web_targets:
        if matches_job_filter("custom", "custom", job_filter) and target_url not in deleted_targets:
            job_map.setdefault(target_url, "custom")

    # Also include instances with active alerts
    status_data = load_json(STATUS_FILE, {"alerts": []})
    for a in status_data.get('alerts', []):
        inst = a.get('instance')
        if inst and inst not in deleted_targets and matches_job_filter(a.get('job', 'alertmanager'), 'alertmanager', job_filter):
            job_map.setdefault(inst, a.get('job') or 'alertmanager')

    return job_map


_PROM_DURATION_RE = re.compile(r'(\d+(?:\.\d+)?)(ms|s|m|h)')

def _parse_prom_duration_sec(value):
    """Parses a Prometheus model.Duration string ("60s", "1m0s", "500ms")
    into seconds. Returns None if unparseable/empty."""
    if not value:
        return None
    total = 0.0
    matched = False
    for num, unit in _PROM_DURATION_RE.findall(str(value)):
        matched = True
        n = float(num)
        total += n / 1000.0 if unit == 'ms' else n * {'s': 1.0, 'm': 60.0, 'h': 3600.0}[unit]
    return total if matched else None


def get_instance_cadence_map(job_filter=None):
    """Instance -> real per-target scrape interval (seconds), read straight
    from Prometheus's own /api/v1/targets `scrapeInterval` field.

    This is what expected_interval_sec should always be keyed by: a mixed
    fleet scrapes blackbox-ping targets at 60s and node-exporter/http targets
    at 15s, so one global SCRAPE_INTERVAL_SECONDS (or a fleet-wide median) is
    wrong for whichever job doesn't match it. scrapeInterval is present on
    every active target regardless of requested window size, unlike the
    first_ts/last_ts subqueries (which api_availability skips for windows
    over 60 minutes for cost reasons) — so it works for the 24h/7d/30d cases
    those can't cover.
    """
    if job_filter is None:
        job_filter = DEFAULT_JOB_FILTER
    raw_targets, _ = fetch_prometheus_json('/api/v1/targets', use_cache=True, cache_ttl=3.0)
    deleted_targets = set(load_deleted_targets())

    cadence_map = {}
    if raw_targets and raw_targets.get('status') == 'success':
        for t in raw_targets.get('data', {}).get('activeTargets', []):
            labels = t.get('labels', {})
            job = labels.get('job') or t.get('scrapePool') or ''
            scrape_pool = t.get('scrapePool', '')
            inst_name = labels.get('instance', t.get('scrapeUrl', '?'))
            if job != 'prometheus' and matches_job_filter(job, scrape_pool, job_filter) and inst_name not in deleted_targets:
                sec = _parse_prom_duration_sec(t.get('scrapeInterval'))
                if sec:
                    cadence_map[inst_name] = sec
    return cadence_map


def get_monitored_instances(job_filter=None):
    return sorted(get_instance_job_map(job_filter).keys())

# ── Availability (historical uptime %) ────────────────────────────────────────
# ── Availability (historical uptime %) ────────────────────────────────────────
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
    minutes = max(1.0, min(minutes, 90 * 1440.0))
    minutes_int = int(round(minutes))

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

    avail_cache_key = f"avail:{active_url}:{norm_job}:{minutes_int}:{norm_end}"

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
                "source": "nodata"
            }
            with _AVAILABILITY_CACHE_LOCK:
                _AVAILABILITY_CACHE[avail_cache_key] = (now_under_lock, payload)
            return jsonify(payload)

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
            for b in db_bucket_records:
                inst = b.get("instance")
                cov_sec = float(b.get("coverage_seconds", 0) or 0)
                if inst in monitored_instances and cov_sec > 0:
                    instances_in_db.add(inst)
                    st = float(b.get("bucket_start", 0))
                    en = float(b.get("bucket_end", 0))
                    cur = instance_spans.get(inst)
                    if cur is None:
                        instance_spans[inst] = (st, en, 1)
                    else:
                        instance_spans[inst] = (min(cur[0], st), max(cur[1], en), cur[2] + 1)

            if len(instances_in_db) == len(monitored_instances):
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
            )
            t_merge_end = time.perf_counter()
            merge_duration_ms = (t_merge_end - t_merge_start) * 1000.0

            hybrid_meta = summary_dict.get("hybrid", {})
            counts = {"online": 0, "warning": 0, "offline": 0}
            for e in entries:
                avail_pct = e.get("availability_pct")
                if avail_pct is not None:
                    if avail_pct >= 99.9:
                        st_val = 'online'
                    elif avail_pct >= 95.0:
                        st_val = 'warning'
                    else:
                        st_val = 'offline'
                else:
                    st_val = 'warning'
                counts[st_val] += 1

            lowest_availability = sorted(
                [e for e in summary_dict['per_server']['values'] if e.get('availability_pct') is not None and e['availability_pct'] < 100.0],
                key=lambda e: (
                    e['availability_pct'],
                    -(e.get('downtime_minutes') or 0.0),
                    -(e.get('incidents') or 0)
                )
            )

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
                "sqlite_seconds": hybrid_meta.get("sqlite_seconds", 0.0),
                "prometheus_seconds": hybrid_meta.get("prometheus_seconds", 0.0),
                "overlap_removed_seconds": hybrid_meta.get("overlap_removed_seconds", 0.0),
                "coverage_percent": hybrid_meta.get("coverage_percent", 0.0),
                "availability_percent": summary_dict['fleet_aggregate']['value'],
                "data_status": hybrid_meta.get("data_status", "COMPLETE"),
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
                "source": "materialized",
                "_trace": trace_data,
            }
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
        )
        t_merge_end = time.perf_counter()
        merge_duration_ms = (t_merge_end - t_merge_start) * 1000.0
        hybrid_meta = summary_dict.get("hybrid", {})

        # Materialize completed hourly buckets in tests or background
        materialized_buckets = []
        h_start = math.floor(req_start / 3600.0) * 3600.0
        h_end = math.ceil(req_end / 3600.0) * 3600.0
        if h_end > h_start:
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
            if app.config.get('TESTING'):
                try:
                    AvailabilityBucketRepository.save_buckets(materialized_buckets)
                except Exception:
                    pass

        # Live status count resolution
        counts = {"online": 0, "warning": 0, "offline": 0}
        for e in entries:
            inst = e.get("id")
            avail_pct = e.get("availability_pct")
            if avail_pct is not None:
                if avail_pct >= 99.9:
                    st_val = 'online'
                elif avail_pct >= 95.0:
                    st_val = 'warning'
                else:
                    st_val = 'offline'
            else:
                live_val = live_map.get(inst)
                if live_val is not None:
                    st_val = 'online' if str(live_val) in ('1', '1.0') else 'offline'
                else:
                    st_val = 'warning'
            counts[st_val] += 1

        lowest_availability = sorted(
            [e for e in summary_dict['per_server']['values'] if e.get('availability_pct') is not None and e['availability_pct'] < 100.0],
            key=lambda e: (
                e['availability_pct'],
                -(e.get('downtime_minutes') or 0.0),
                -(e.get('incidents') or 0)
            )
        )

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
            "sqlite_seconds": hybrid_meta.get("sqlite_seconds", 0.0),
            "prometheus_seconds": hybrid_meta.get("prometheus_seconds", 0.0),
            "overlap_removed_seconds": hybrid_meta.get("overlap_removed_seconds", 0.0),
            "coverage_percent": hybrid_meta.get("coverage_percent", 0.0),
            "availability_percent": summary_dict['fleet_aggregate']['value'],
            "data_status": hybrid_meta.get("data_status", "PARTIAL"),
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
            "source": hybrid_meta.get("source", "fallback"),
            "_trace": trace_data,
        }

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
        raw, base = fetch_prometheus_json(path)
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
    logs = load_json(LOGS_FILE, [])
    history_records = load_json(HISTORY_FILE, [])
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
        raw_dur, _ = fetch_prometheus_json(dur_path)
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
        logs = load_json(LOGS_FILE, [])
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
        os.access(p, os.W_OK) for p in (STATUS_FILE, LOGS_FILE, HISTORY_FILE)
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

    raw, prom_base = fetch_prometheus_json('/api/v1/targets', use_cache=True)
    prometheus_ok = raw is not None and raw.get('status') == 'success'

    storage_ok = all(
        os.access(p, os.W_OK) for p in (STATUS_FILE, LOGS_FILE, HISTORY_FILE)
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

    components = {
        "prometheus":     {"ok": prometheus_ok, "url": prom_base},
        "monitoring_api":  {"ok": True},
        "alarm_service":   {"ok": alarm_service_ok, "last_tick_seconds_ago": (
            round(poller_tick_age, 1) if poller_tick_age is not None else None)},
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

_poller_state = {}  # instance -> 'up' | 'down', seeded from status.json at startup
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

def _seed_poller_state():
    # Currently-firing alerts (survived from before a backend restart) seed
    # as 'down' so we don't re-fire a duplicate "went offline" for an outage
    # that's already recorded in history.json — only its eventual recovery
    # still needs to be observed and resolved.
    status_data = load_json(STATUS_FILE, {"alerts": []})
    for a in status_data.get('alerts', []):
        inst = a.get('instance')
        if inst:
            _poller_state[inst] = 'down'

def _reconcile_orphaned_alerts(monitored_instances):
    """Auto-resolves TargetDown alerts whose instance no longer exists in the
    monitored set at all — e.g. removed from Prometheus's scrape config
    entirely, not merely down. Without this, such an alert can never be
    observed recovering (compute_state_transitions only looks at instances
    still present in success_map/instances) and stays firing forever,
    permanently pinning status.json to CRITICAL. See AUDIT.md.
    Only touches poller-owned TargetDown alerts, and only runs when `instances`
    is non-empty (i.e. Prometheus itself is reachable) — see call site."""
    status_data = load_json(STATUS_FILE, {"alerts": []})
    orphaned = [
        a for a in status_data.get('alerts', [])
        if a.get('name') == ALERTNAME_TARGET_DOWN and a.get('instance') not in monitored_instances
    ]
    for a in orphaned:
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
    # that's now a real transition and a fresh incident is raised; if it
    # recovered, prev==new and nothing fires.
    # ponytail: job-scoped maintenance is enforced only in record_alert_event()
    # (still no false incident/alarm) — this resume nudge is instance-only;
    # extend to jobs if job-wide maintenance flapping becomes a problem.
    windows = load_maintenance_windows()
    active_now = {inst for inst in instances if get_active_maintenance(inst, windows=windows)}
    for inst in _maintenance_active_prev - active_now:
        _poller_state[inst] = 'up'
    _maintenance_active_prev.clear()
    _maintenance_active_prev.update(active_now)

    scoped = {inst: v for inst, v in success_map.items() if inst in instances and inst not in active_now}
    transitions, new_state = compute_state_transitions(scoped, _poller_state)
    _poller_state.update(new_state)

    now = time.time()
    for inst, is_up in transitions:
        lat = duration_map.get(inst)
        try:
            latency_ms = round(float(lat) * 1000, 1) if lat is not None else None
        except (TypeError, ValueError):
            latency_ms = None

        if is_up:
            summary = f"{inst} recovered"
        else:
            # Same classifier as /instances — poller has no per-instance
            # lastError (that lives on /api/v1/targets, which this loop
            # doesn't fetch), so this degrades to HTTP-code-based
            # classification or "Unknown", same as any other probe-only target.
            classification = classify_scrape_failure('down', '', status_code_map.get(inst))
            summary = f"{inst} is unreachable ({classification['category']})"

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
        )

def _poller_loop():
    _seed_poller_state()
    while True:
        try:
            _poll_targets_once()
        except Exception as e:
            print(f"Alert poller error: {e}", flush=True)
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
_avail_aggregator_started = False

def _aggregate_availability_cycle():
    """Incremental availability aggregation run executed only by the elected leader worker."""
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
    windows_to_aggregate = []

    if latest_end is None or latest_end < (now - 86400 * 7):
        backfill_start = now - 86400 * 7
        cur_t = backfill_start
        while cur_t < now:
            next_t = min(cur_t + 21600, now)
            windows_to_aggregate.append((cur_t, next_t))
            cur_t = next_t
    else:
        hour_end = math.floor(now / 3600.0) * 3600.0
        hour_start = hour_end - 3600.0
        windows_to_aggregate.append((hour_start, hour_end))
        if now - hour_end >= 30.0:
            windows_to_aggregate.append((hour_end, now))

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
                            cov_sec = min(span + intv, w_duration_sec)
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

            latency = 0.0
            if raw_dur is not None:
                try:
                    latency = round(float(raw_dur), 1)
                except (ValueError, TypeError):
                    latency = 0.0

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
                pass

    try:
        AvailabilityBucketRepository.prune_old_buckets(35 * 86400)
    except Exception:
        pass


def _availability_aggregator_loop():
    while True:
        try:
            _aggregate_availability_cycle()
        except Exception as e:
            print(f"Availability aggregator error: {e}", flush=True)
        time.sleep(AVAIL_AGGREGATE_INTERVAL_SECONDS)


def start_availability_aggregator():
    global _avail_aggregator_started
    if _avail_aggregator_started:
        return
    _avail_aggregator_started = True
    threading.Thread(target=_availability_aggregator_loop, daemon=True, name="avail-aggregator").start()


if os.environ.get("DISABLE_ALERT_POLLER") != "1":
    start_alert_poller()

if os.environ.get("DISABLE_AVAILABILITY_AGGREGATOR") != "1" and os.environ.get("DISABLE_ALERT_POLLER") != "1":
    start_availability_aggregator()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, threaded=True)
