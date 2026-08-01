from flask import Flask, request, jsonify, render_template
import json, time, os, math, re, shutil, gzip, threading, sys
from datetime import datetime
from urllib.request import urlopen, Request
from urllib.error import URLError

sys.path.insert(0, os.path.dirname(__file__))
try:
    from fleet_availability import summarize_entries
except ImportError:
    from alarm.fleet_availability import summarize_entries

# Matches prometheus/prometheus.yml `global.scrape_interval`. Used to turn a
# Prometheus `count_over_time(...)` sample count back into a monitored-duration
# estimate for weighted (fleet_aggregate) availability math.
SCRAPE_INTERVAL_SECONDS = 2

app = Flask(__name__)
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

STATUS_FILE  = os.path.join(os.path.dirname(__file__), "status.json")
HISTORY_FILE = os.path.join(os.path.dirname(__file__), "history.json")
HISTORY_ARCHIVE_FILE = os.path.join(os.path.dirname(__file__), "history_archive.json")
LOGS_FILE    = os.path.join(os.path.dirname(__file__), "logs.json")
DELETED_TARGETS_FILE = os.path.join(os.path.dirname(__file__), "deleted_targets.json")
ENDPOINTS_FILE = os.path.join(os.path.dirname(__file__), "endpoints.json")
MAINTENANCE_FILE = os.path.join(os.path.dirname(__file__), "maintenance.json")

# dependencies.json — kept as its own sidecar file rather than folded into
# "target configuration". Audited (Phase 12 revision) whether that's still
# the least-intrusive option; it is, for two concrete reasons:
#   1. Most targets aren't owned by this app at all — they're discovered
#      live from Prometheus's own scrape config (job=blackbox-ping-internal,
#      fetched via /api/v1/targets from the remote Prometheus at
#      PROMETHEUS_URL). There is no file in this repo to attach a
#      dependsOn field to for those hosts.
#   2. The one target file this app *does* own, targets/websites.yml (see
#      load_website_targets/save_website_targets above), isn't an app config
#      object — it's a Prometheus file_sd YAML snippet, parsed with a
#      regex into a flat list of URL strings and written back the same way.
#      Adding relational metadata there would either break Prometheus's
#      ingestion of that file or require a parallel structure anyway,
#      and would still miss the Prometheus-native hosts from point 1.
# A tiny sidecar JSON (load_json/save_json, identical shape to
# maintenance.json) is therefore the smallest change that covers every
# target regardless of where it came from, and keeps "which host depends
# on which" — a purely operator-editable, independently-changing piece of
# state — decoupled from target add/remove lifecycle.
DEPENDENCIES_FILE = os.path.join(os.path.dirname(__file__), "dependencies.json")
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

def save_with_retention(main_path, archive_path, data, limit):
    if len(data) > limit:
        overflow = data[limit:]
        archive = load_json(archive_path, [])
        archive = overflow + archive
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
        tmp_path = path + ".tmp"
        os.makedirs(os.path.dirname(os.path.abspath(tmp_path)), exist_ok=True)
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception as e:
        print(f"Error saving {path}: {e}", flush=True)

PROMETHEUS_CANDIDATES = [
    os.environ.get("PROMETHEUS_URL", "http://192.168.9.16:9090"),
    "http://192.168.9.16:9090",
    "http://prometheus:9090",
    "http://host.docker.internal:9090",
    "http://localhost:9090",
    "http://127.0.0.1:9090"
]

_ENDPOINTS_CACHE = {"ts": 0.0, "data": None}
_ENDPOINTS_CACHE_TTL = 2.0  # endpoints.json rarely changes; avoid a disk read on every hot-path call

def load_endpoints():
    now = time.time()
    cached = _ENDPOINTS_CACHE["data"]
    if cached is not None and now - _ENDPOINTS_CACHE["ts"] < _ENDPOINTS_CACHE_TTL:
        return cached

    default_url = os.environ.get("PROMETHEUS_URL", "http://192.168.9.16:9090")
    default_data = {
        "active": default_url,
        "endpoints": [default_url]
    }
    data = load_json(ENDPOINTS_FILE, default_data)
    if not isinstance(data, dict) or "endpoints" not in data:
        data = default_data
    if not isinstance(data.get("endpoints"), list) or not data["endpoints"]:
        data["endpoints"] = [default_url]
    if not data.get("active") or data["active"] not in data["endpoints"]:
        data["active"] = data["endpoints"][0]

    _ENDPOINTS_CACHE["data"] = data
    _ENDPOINTS_CACHE["ts"] = now
    return data

def save_endpoints(data):
    save_json(ENDPOINTS_FILE, data)
    _ENDPOINTS_CACHE["data"] = None  # invalidate so the next read picks up the change immediately

DEFAULT_JOB_FILTER = os.environ.get("JOB_FILTER", "all")

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

def load_website_targets():
    target_file = get_targets_file()
    urls = []
    if os.path.exists(target_file):
        try:
            with open(target_file, 'r') as f:
                content = f.read()
                matches = re.findall(r'-\s*"([^"]+)"', content)
                if not matches:
                    matches = re.findall(r"-\s*'([^']+)'", content)
                if not matches:
                    matches = re.findall(r'-\s*([^\s#]+)', content)
                for u in matches:
                    u = u.strip()
                    if u and u != 'targets:' and u != 'blackbox_http' and u not in urls:
                        urls.append(u)
        except Exception as e:
            print(f"Error loading targets: {e}", flush=True)
    if not os.path.exists(target_file) and not urls:
        urls = ["http://nginx"]
        save_website_targets(urls)
    return urls

def save_website_targets(urls):
    target_file = get_targets_file()
    try:
        os.makedirs(os.path.dirname(target_file), exist_ok=True)
        yaml_lines = ["- targets:"]
        for u in urls:
            yaml_lines.append(f'    - "{u}"')
        yaml_lines.append('  labels:')
        yaml_lines.append('    job: "blackbox_http"')
        yaml_lines.append('')
        
        tmp_file = target_file + ".tmp"
        with open(tmp_file, 'w') as f:
            f.write("\n".join(yaml_lines))
        os.replace(tmp_file, target_file)
    except Exception as e:
        print(f"Error saving targets {target_file}: {e}", flush=True)

LAST_WORKING_PROMETHEUS_URL = None
PROMETHEUS_CACHE = {}
PROMETHEUS_CACHE_LOCK = threading.Lock()
PROMETHEUS_CACHE_TTL_DEFAULT = 8.0  # backend cache window for identical PromQL responses (5-15s band)

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

def _fetch_lock_for(key):
    with _FETCH_LOCKS_GUARD:
        lock = _FETCH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _FETCH_LOCKS[key] = lock
        return lock

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

def fetch_url(url, timeout=1.5):
    try:
        req = Request(url, headers={"User-Agent": "InfraWatch/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return resp.read().decode('utf-8', errors='ignore')
    except Exception:
        pass
    return None

def fetch_prometheus_json(path, use_cache=True, cache_ttl=None):
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

        candidates = []
        if active_url:
            candidates.append(active_url)
        if LAST_WORKING_PROMETHEUS_URL and LAST_WORKING_PROMETHEUS_URL not in candidates:
            candidates.append(LAST_WORKING_PROMETHEUS_URL)
        for ep in endpoints_data.get("endpoints", []):
            if ep not in candidates:
                candidates.append(ep)
        for base_url in PROMETHEUS_CANDIDATES:
            if base_url not in candidates:
                candidates.append(base_url)

        for base_url in candidates:
            timeout = 0.8 if (base_url == active_url or base_url == LAST_WORKING_PROMETHEUS_URL) else 0.3
            raw = fetch_url(f"{base_url.rstrip('/')}{path}", timeout=timeout)
            if raw:
                try:
                    data = json.loads(raw)
                    if data.get('status') == 'success' or 'data' in data:
                        LAST_WORKING_PROMETHEUS_URL = base_url
                        if use_cache:
                            with PROMETHEUS_CACHE_LOCK:
                                PROMETHEUS_CACHE[cache_key] = (time.time(), data, base_url)
                        return data, base_url
                except Exception:
                    continue
        return None, None

@app.route('/')
def index():
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
    data = load_json(MAINTENANCE_FILE, [])
    return data if isinstance(data, list) else []

def get_active_maintenance(instance, job=None, windows=None):
    """First maintenance window currently covering this instance/job, or None."""
    if windows is None:
        windows = load_maintenance_windows()
    now = time.time()
    for w in windows:
        if not (w.get('start', 0) <= now <= w.get('end', 0)):
            continue
        if w.get('scope') == 'job':
            if job and w.get('target') == job:
                return w
        elif w.get('target') == instance:
            return w
    return None

def record_alert_event(name, severity, instance, summary, job, event_time, is_now_firing,
                        receiver='', generatorURL='', key=None, latency_ms=None):
    """Applies one alert firing/resolved transition to status.json/logs.json/
    history.json. Shared by the Alertmanager webhook and the Prometheus-poller
    fallback (see start_alert_poller below) so both get identical
    transition-only dedupe and incident-history reconciliation.
    Returns False if this was a repeat notification with no actual state
    change (already firing, or already resolved) — the caller should treat
    that as a no-op, matching Alertmanager's own re-notify behavior.
    """
    key = key or f"{name}|{instance}"

    if is_now_firing and get_active_maintenance(instance, job):
        return False  # suppressed: instance/job is under an active maintenance window.
        # A recovery for the same key naturally no-ops too — it was never
        # added to active_alerts, so the existing was_firing==is_now_firing
        # dedupe below already treats it as "no transition".

    with _WEBHOOK_LOCK:
        status_data = load_json(STATUS_FILE, {"status": "NORMAL", "alerts": [], "updated": event_time})
        active_alerts = {}
        for a in status_data.get('alerts', []):
            k = a.get('key') or f"{a.get('name')}|{a.get('instance')}"
            active_alerts[k] = a

        was_firing = key in active_alerts
        if was_firing == is_now_firing:
            return False  # no state transition — dedupe repeat notifications

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
        return True

# ── Webhook ───────────────────────────────────────────────────────────────────
@app.route('/webhook', methods=['POST'])
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
    return jsonify(load_json(STATUS_FILE, {
        "status": "NORMAL", "alerts": [], "updated": time.time()
    }))

@app.route('/history')
def history():
    return jsonify(load_json(HISTORY_FILE, []))

@app.route('/logs')
def logs():
    try:
        limit = int(request.args.get('limit', 50))
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, MAX_LOGS))
    data  = load_json(LOGS_FILE, [])
    return jsonify(data[:limit])

def load_deleted_targets():
    return load_json(DELETED_TARGETS_FILE, [])

def save_deleted_targets(deleted_list):
    save_json(DELETED_TARGETS_FILE, deleted_list)

_EP_STATUS_CACHE = {"ts": 0.0, "key": "", "data": None}

# ── Prometheus Endpoint Management API ───────────────────────────────────────
@app.route('/api/endpoints', methods=['GET'])
def get_endpoints_api():
    from concurrent.futures import ThreadPoolExecutor
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
def add_endpoint_api():
    global LAST_WORKING_PROMETHEUS_URL
    body = request.json or {}
    url = body.get('url', '').strip()
    if not url:
        return jsonify({"ok": False, "error": "URL Endpoint Prometheus wajib diisi"}), 400

    if not url.startswith("http://") and not url.startswith("https://"):
        url = f"http://{url}"
    url = url.rstrip('/')

    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return jsonify({"ok": False, "error": "Format URL Endpoint Prometheus tidak valid"}), 400

    data = load_endpoints()
    if url not in data["endpoints"]:
        data["endpoints"].append(url)
    
    if body.get('set_active', True):
        data["active"] = url
        LAST_WORKING_PROMETHEUS_URL = url
        with PROMETHEUS_CACHE_LOCK:
            PROMETHEUS_CACHE.clear()

    save_endpoints(data)
    _EP_STATUS_CACHE["data"] = None
    return jsonify({"ok": True, "active": data["active"], "endpoints": data["endpoints"]})

@app.route('/api/endpoints/select', methods=['POST'])
def select_endpoint_api():
    global LAST_WORKING_PROMETHEUS_URL
    body = request.json or {}
    url = body.get('url', '').strip()
    if not url:
        return jsonify({"ok": False, "error": "URL Endpoint Prometheus wajib diisi"}), 400

    data = load_endpoints()
    if url not in data["endpoints"]:
        data["endpoints"].append(url)

    data["active"] = url
    save_endpoints(data)
    _EP_STATUS_CACHE["data"] = None

    # Immediately clear cache and force selected URL as primary
    LAST_WORKING_PROMETHEUS_URL = url
    with PROMETHEUS_CACHE_LOCK:
        PROMETHEUS_CACHE.clear()

    return jsonify({"ok": True, "active": url})

@app.route('/api/endpoints', methods=['DELETE'])
def delete_endpoint_api():
    global LAST_WORKING_PROMETHEUS_URL
    body = request.json or {}
    url = body.get('url', '').strip()
    data = load_endpoints()
    
    if url not in data["endpoints"]:
        return jsonify({"ok": False, "error": "Endpoint Prometheus tidak ditemukan"}), 404

    if len(data["endpoints"]) <= 1:
        return jsonify({"ok": False, "error": "Tidak bisa menghapus endpoint terakhir"}), 400
    data["endpoints"].remove(url)
    if data["active"] == url:
        data["active"] = data["endpoints"][0]
        LAST_WORKING_PROMETHEUS_URL = data["active"]
        with PROMETHEUS_CACHE_LOCK:
            PROMETHEUS_CACHE.clear()
    save_endpoints(data)
    _EP_STATUS_CACHE["data"] = None

    return jsonify({"ok": True, "active": data["active"], "endpoints": data["endpoints"]})

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
def add_target_api():
    data = request.json or {}
    url = data.get('url', '').strip()
    if not url:
        return jsonify({"ok": False, "error": "IP / Target host is required"}), 400
    
    # Remove from deleted_targets if previously deleted
    deleted = load_deleted_targets()
    if url in deleted:
        deleted.remove(url)
        save_deleted_targets(deleted)

    current = load_website_targets()
    if url not in current:
        current.append(url)
        save_website_targets(current)

    return jsonify({"ok": True, "targets": current})

@app.route('/api/targets', methods=['DELETE'])
def delete_target_api():
    data = request.json or {}
    url = data.get('url', '').strip()
    if not url:
        return jsonify({"ok": False, "error": "IP / Target host is required"}), 400
    
    current = load_website_targets()
    if url in current:
        current.remove(url)
        save_website_targets(current)

    deleted = load_deleted_targets()
    if url not in deleted:
        deleted.append(url)
        save_deleted_targets(deleted)

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
def create_maintenance_api():
    data = request.json or {}
    target = (data.get('target') or '').strip()
    scope = data.get('scope') if data.get('scope') in ('instance', 'job') else 'instance'
    reason = (data.get('reason') or '').strip()[:200]

    if not target:
        return jsonify({"ok": False, "error": "Target wajib diisi"}), 400
    try:
        start = int(float(data.get('start')))
        end = int(float(data.get('end')))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "start dan end wajib berupa epoch timestamp"}), 400
    if end <= start:
        return jsonify({"ok": False, "error": "end harus setelah start"}), 400

    window = {
        "id": f"mw_{int(time.time() * 1000)}",
        "scope": scope,
        "target": target,
        "reason": reason,
        "start": start,
        "end": end,
        "created_at": int(time.time()),
    }
    windows = load_maintenance_windows()
    windows.append(window)
    save_json(MAINTENANCE_FILE, windows)
    return jsonify({"ok": True, "window": window})

@app.route('/api/maintenance/<window_id>', methods=['DELETE'])
def delete_maintenance_api(window_id):
    windows = load_maintenance_windows()
    remaining = [w for w in windows if w.get('id') != window_id]
    if len(remaining) == len(windows):
        return jsonify({"ok": False, "error": "Maintenance window tidak ditemukan"}), 404
    save_json(MAINTENANCE_FILE, remaining)
    return jsonify({"ok": True})

# ── Alert Correlation (Phase 12) ─────────────────────────────────────────────
# A "depends on" link between two instances, used only to decide what to
# visually de-emphasize on the wallboard when both ends are down at once.
# Never touches status/logs/history — the underlying alert for a suppressed
# child still fires and is recorded exactly as if this feature didn't exist.
def load_dependencies():
    data = load_json(DEPENDENCIES_FILE, [])
    return data if isinstance(data, list) else []

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
def create_dependency_api():
    data = request.json or {}
    child = (data.get('child') or '').strip()
    parent = (data.get('parent') or '').strip()
    if not child or not parent:
        return jsonify({"ok": False, "error": "child dan parent wajib diisi"}), 400
    if child == parent:
        return jsonify({"ok": False, "error": "Host tidak bisa bergantung pada dirinya sendiri"}), 400

    # One parent per child (a tree, not a general graph) — upsert.
    deps = [d for d in load_dependencies() if d.get('child') != child]
    dep = {"id": f"dep_{int(time.time() * 1000)}", "child": child, "parent": parent, "created_at": int(time.time())}
    deps.append(dep)
    save_json(DEPENDENCIES_FILE, deps)
    return jsonify({"ok": True, "dependency": dep})

@app.route('/api/dependencies/<dep_id>', methods=['DELETE'])
def delete_dependency_api(dep_id):
    deps = load_dependencies()
    remaining = [d for d in deps if d.get('id') != dep_id]
    if len(remaining) == len(deps):
        return jsonify({"ok": False, "error": "Dependency tidak ditemukan"}), 404
    save_json(DEPENDENCIES_FILE, remaining)
    return jsonify({"ok": True})

def fetch_prom_query_map(query_expr, cache_ttl=5.0):
    from urllib.parse import quote
    raw, base = fetch_prometheus_json(f"/api/v1/query?query={quote(query_expr)}", use_cache=True, cache_ttl=cache_ttl)
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

def fetch_down_since_prom_map():
    from urllib.parse import quote
    last_up_map = {}

    # 1. PromQL query for exact last UP timestamp for targets that were previously UP
    query_up = 'max_over_time(timestamp(probe_success == 1)[1d:15s])'
    raw_up, _ = fetch_prometheus_json(f"/api/v1/query?query={quote(query_up)}", use_cache=True, cache_ttl=5.0)
    if not raw_up or not raw_up.get('data', {}).get('result'):
        query_up = 'max_over_time(timestamp(up == 1)[1d:15s])'
        raw_up, _ = fetch_prometheus_json(f"/api/v1/query?query={quote(query_up)}", use_cache=True, cache_ttl=5.0)

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
    raw_down, _ = fetch_prometheus_json(f"/api/v1/query?query={quote(query_down)}", use_cache=True, cache_ttl=5.0)
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

def fetch_all_probe_metrics():
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=3) as executor:
        f_succ = executor.submit(fetch_prom_query_map, "probe_success")
        f_dur = executor.submit(fetch_prom_query_map, "probe_duration_seconds")
        f_code = executor.submit(fetch_prom_query_map, "probe_http_status_code")
        
        success_map = f_succ.result()
        duration_map = f_dur.result()
        status_code_map = f_code.result()

    if not success_map:
        success_map = fetch_prom_query_map("up")

    return success_map, duration_map, status_code_map

# ── Instances & Real-time Metrics API ─────────────────────────────────────────
@app.route('/instances')
def instances():
    job_param = request.args.get('job', DEFAULT_JOB_FILTER)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=3) as executor:
        f_targets = executor.submit(fetch_prometheus_json, '/api/v1/targets')
        f_metrics = executor.submit(fetch_all_probe_metrics)
        f_down = executor.submit(fetch_down_since_prom_map)
        
        raw_targets, active_base = f_targets.result()
        probe_success_map, probe_duration_map, probe_status_code_map = f_metrics.result()
        down_since_prom_map = f_down.result()

    config_web_targets = load_website_targets()
    deleted_targets = set(load_deleted_targets())
    now_ts = int(time.time())

    if raw_targets is None and not config_web_targets:
        return jsonify({"ok": False, "error": "Engine Prometheus tidak dapat dijangkau"}), 503

    available_jobs = set()
    if raw_targets and raw_targets.get('status') == 'success':
        for t in raw_targets.get('data', {}).get('activeTargets', []):
            j = t.get('labels', {}).get('job') or t.get('scrapePool')
            if j and j != 'prometheus':
                available_jobs.add(j)

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

                # Determine health from probe_success metric
                p_success = probe_success_map.get(inst_name) or probe_success_map.get(scrape_url)
                if p_success is not None:
                    health = 'up' if str(p_success) in ('1', '1.0') else 'down'
                else:
                    health = t.get('health', 'unknown')

                # Pure PromQL continuous downSince calculation
                if health != 'up':
                    last_up = down_since_prom_map.get(inst_name) or down_since_prom_map.get(scrape_url)
                    down_since_val = int(last_up) if last_up else 0
                else:
                    down_since_val = 0

                # Determine response time (in ms) from probe_duration_seconds metric
                p_duration = probe_duration_map.get(inst_name) or probe_duration_map.get(scrape_url)
                if p_duration is not None:
                    try:
                        response_time_ms = round(float(p_duration) * 1000, 1)
                    except ValueError:
                        response_time_ms = 0
                else:
                    scrape_dur = t.get('lastScrapeDuration')
                    if scrape_dur is not None:
                        try:
                            response_time_ms = round(float(scrape_dur) * 1000, 1)
                        except ValueError:
                            response_time_ms = 0
                    else:
                        response_time_ms = 0

                # Determine HTTP status code / ping status
                p_code = probe_status_code_map.get(inst_name) or probe_status_code_map.get(scrape_url)
                if p_code is not None:
                    try:
                        http_code = int(float(p_code))
                    except ValueError:
                        http_code = 200 if health == 'up' else 0
                else:
                    http_code = 200 if health == 'up' else 0

                result.append({
                    "instance":        inst_name,
                    "job":             job or "blackbox",
                    "health":          health,
                    "responseTimeMs":  response_time_ms,
                    "httpStatusCode":  http_code,
                    "lastScrape":      t.get('lastScrape', ''),
                    "scrapeUrl":       scrape_url,
                    "lastError":       t.get('lastError', ''),
                    "labels":          labels,
                    "isWeb":           False,
                    "downSince":       down_since_val
                })

    # Merge custom user-added targets
    for target_url in config_web_targets:
        if matches_job_filter("custom", "custom", job_param) and target_url not in seen_instances and target_url not in deleted_targets:
            p_success = probe_success_map.get(target_url)
            if p_success is not None:
                health = 'up' if str(p_success) in ('1', '1.0') else 'down'
            else:
                health = 'up'

            if health != 'up':
                last_up = down_since_prom_map.get(target_url)
                down_since_val = int(last_up) if last_up else 0
            else:
                down_since_val = 0

            p_duration = probe_duration_map.get(target_url)
            try:
                response_time_ms = round(float(p_duration) * 1000, 1) if p_duration else 0.8
            except ValueError:
                response_time_ms = 0.8

            p_code = probe_status_code_map.get(target_url)
            try:
                http_code = int(float(p_code)) if p_code else (200 if health == 'up' else 0)
            except ValueError:
                http_code = 0

            result.append({
                "instance":        target_url,
                "job":             "custom",
                "health":          health,
                "responseTimeMs":  response_time_ms,
                "httpStatusCode":  http_code,
                "lastScrape":      "—",
                "scrapeUrl":       target_url,
                "lastError":       "",
                "labels":          {"job": "custom", "instance": target_url},
                "isWeb":           False,
                "downSince":       down_since_val
            })
            seen_instances.add(target_url)

    # Attach maintenance state — one pass, one file read, so it's not
    # re-derived per-target inside either loop above.
    maintenance_windows = load_maintenance_windows()
    for item in result:
        mw = get_active_maintenance(item['instance'], item.get('job'), windows=maintenance_windows)
        item['maintenance'] = bool(mw)
        item['maintenanceId'] = mw.get('id') if mw else None
        item['maintenanceUntil'] = mw.get('end') if mw else None
        item['maintenanceReason'] = mw.get('reason', '') if mw else ''

    # Alert correlation (Phase 12) — display-only: the child's own
    # health/downSince/alert stays exactly as computed above.
    deps = load_dependencies()
    parent_map = {d['child']: d['parent'] for d in deps}
    apply_correlation_suppression(result, parent_map)
    dep_id_map = {d['child']: d['id'] for d in deps}
    for item in result:
        item['dependencyId'] = dep_id_map.get(item['instance'])

    return jsonify({
        "ok": True,
        "targets": result,
        "available_jobs": sorted(list(available_jobs)),
        "job_filter": job_param,
        "source": "prometheus" if active_base else "local",
        "prometheus_url": active_base
    })

def get_monitored_instances(job_filter=None):
    if job_filter is None:
        job_filter = DEFAULT_JOB_FILTER
    raw_targets, _ = fetch_prometheus_json('/api/v1/targets')
    config_web_targets = load_website_targets()
    deleted_targets = set(load_deleted_targets())

    instances = set()
    if raw_targets and raw_targets.get('status') == 'success':
        targets = raw_targets.get('data', {}).get('activeTargets', [])
        for t in targets:
            labels = t.get('labels', {})
            job = labels.get('job') or t.get('scrapePool') or ''
            scrape_pool = t.get('scrapePool', '')
            inst_name = labels.get('instance', t.get('scrapeUrl', '?'))
            if job != 'prometheus' and matches_job_filter(job, scrape_pool, job_filter):
                instances.add(inst_name)

    for target_url in config_web_targets:
        instances.add(target_url)

    return instances - deleted_targets

# ── Availability (historical uptime %) ────────────────────────────────────────
@app.route('/api/availability')
def api_availability():
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

    at_suffix = f" @ {end_ts}" if end_ts is not None else ""

    monitored_instances = get_monitored_instances(job_filter=job_filter)

    from concurrent.futures import ThreadPoolExecutor

    queries = {
        'avail': f"avg_over_time(probe_success[{minutes_int}m]{at_suffix}) * 100",
        'count': f"count_over_time(probe_success[{minutes_int}m]{at_suffix})",
        'duration': f"avg_over_time(probe_duration_seconds[{minutes_int}m]{at_suffix}) * 1000",
        'incidents': f"changes(probe_success[{minutes_int}m]{at_suffix})",
        'live': "probe_success" if end_ts is None else None
    }

    results = {}
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {k: executor.submit(fetch_prom_query_map, q) for k, q in queries.items() if q}
        for k, f in futures.items():
            try:
                results[k] = f.result()
            except Exception:
                results[k] = {}

    avail_map = results.get('avail') or fetch_prom_query_map(f"avg_over_time(up[{minutes_int}m]{at_suffix}) * 100")
    count_map = results.get('count', {})
    duration_map = results.get('duration', {})
    incidents_map = results.get('incidents', {})
    live_map = results.get('live', {})
    if not live_map and end_ts is None:
        live_map = fetch_prom_query_map("up")

    entries = []
    counts = {"online": 0, "warning": 0, "offline": 0}

    for inst in monitored_instances:
        raw_avail = avail_map.get(inst)
        raw_count = count_map.get(inst)
        raw_duration = duration_map.get(inst)
        raw_incidents = incidents_map.get(inst)

        availability_pct = None
        if raw_avail is not None:
            try:
                availability_pct = round(max(0.0, min(100.0, float(raw_avail))), 2)
            except (TypeError, ValueError):
                availability_pct = None

        denominator_minutes = 0.0
        if raw_count is not None:
            try:
                sample_count = max(0.0, float(raw_count))
                denominator_minutes = min(sample_count * SCRAPE_INTERVAL_SECONDS / 60.0, minutes)
            except (TypeError, ValueError):
                denominator_minutes = 0.0

        downtime_minutes = (
            round(denominator_minutes * (1 - availability_pct / 100.0), 2)
            if availability_pct is not None else 0.0
        )

        incidents_count = 0
        if raw_incidents is not None:
            try:
                incidents_count = int(round(float(raw_incidents) / 2.0))
            except (TypeError, ValueError):
                incidents_count = 0

        avg_latency_ms = 0.0
        if raw_duration is not None:
            try:
                avg_latency_ms = round(float(raw_duration), 1)
            except (TypeError, ValueError):
                avg_latency_ms = 0.0

        entries.append({
            "id": inst,
            "name": inst,
            "availability_pct": availability_pct,
            "denominator_minutes": denominator_minutes,
            "downtime_minutes": downtime_minutes,
            "incidents": incidents_count,
            "avg_latency_ms": avg_latency_ms
        })

        if availability_pct is not None:
            if availability_pct >= 99.9:
                status = 'online'
            elif availability_pct >= 95.0:
                status = 'warning'
            else:
                status = 'offline'
        else:
            live_val = live_map.get(inst)
            if live_val is not None:
                status = 'online' if str(live_val) in ('1', '1.0') else 'offline'
            else:
                status = 'warning'  # unknown / no data yet
        counts[status] += 1

    summary = summarize_entries(entries, minutes)

    lowest_availability = sorted(
        [e for e in summary['per_server']['values'] if e['availability_pct'] is not None],
        key=lambda e: e['availability_pct']
    )[:5]

    # Fleet analytics (Phase 11) — pure aggregation over `entries`, already
    # computed above from the same Prometheus queries this endpoint already
    # makes. No extra polling.
    total_incidents = sum(e['incidents'] for e in entries)
    total_downtime_minutes = sum(e['downtime_minutes'] for e in entries)
    mean_outage_minutes = round(total_downtime_minutes / total_incidents, 1) if total_incidents else None
    most_unstable = sorted(entries, key=lambda e: (-e['incidents'], -e['downtime_minutes']))[:5]
    most_unstable = [e for e in most_unstable if e['incidents'] > 0]

    return jsonify({
        "ok": True,
        "period_minutes": round(minutes, 2),
        "end": end_ts,
        "counts": {
            "total": len(monitored_instances),
            "online": counts['online'],
            "warning": counts['warning'],
            "offline": counts['offline'],
        },
        # Headline SLA number shown big on the card = fleet_aggregate (weighted)
        "overall": summary['fleet_aggregate']['value'],
        "fleet_aggregate": summary['fleet_aggregate'],
        "fleet_average": summary['fleet_average'],
        "health_ratio": summary['health_ratio'],
        "per_server": summary['per_server'],
        "lowest_availability": lowest_availability,
        "entries": entries,
        "targets": {e['id']: e['availability_pct'] for e in summary['per_server']['values']},
        "analytics": {
            "total_incidents": total_incidents,
            "mean_outage_minutes": mean_outage_minutes,
            "most_unstable": most_unstable,
        },
    })

@app.route('/api/target-history')
def target_history_api():
    target_url = request.args.get('target', '').strip()
    minutes_param = request.args.get('minutes', '1440')
    try:
        minutes = int(float(minutes_param))
    except (ValueError, TypeError):
        minutes = 1440

    if not target_url:
        return jsonify({"ok": False, "error": "Target parameter is required"}), 400

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

    from urllib.parse import quote
    
    clean_target = target_url.replace('http://', '').replace('https://', '').rstrip('/')
    candidate_queries = [
        f'probe_success{{instance="{target_url}"}}',
        f'probe_success{{instance=~".*{re.escape(clean_target)}.*"}}',
        f'up{{instance="{target_url}"}}',
        f'up{{instance=~".*{re.escape(clean_target)}.*"}}',
        'probe_success',
        'up'
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
                if q not in ('probe_success', 'up') or inst == target_url or clean_target in inst:
                    values = r.get('values', [])
                    if values:
                        break
            if values:
                break

    events = []
    if values:
        current_state = None
        state_start_ts = None

        for ts, val_str in values:
            val = 1 if str(val_str) in ('1', '1.0') else 0
            if current_state is None:
                current_state = val
                state_start_ts = ts
            elif val != current_state:
                duration_sec = int(ts - state_start_ts)
                events.append({
                    "status": "ONLINE" if current_state == 1 else "OFFLINE",
                    "start_ts": int(state_start_ts),
                    "end_ts": int(ts),
                    "duration_seconds": duration_sec,
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
                "duration_seconds": duration_sec,
                "ongoing": is_ongoing
            })

    # Fetch latency range history for sparkline graph
    latency_points = []
    dur_queries = [
        f'probe_duration_seconds{{instance="{target_url}"}} * 1000',
        f'probe_duration_seconds{{instance=~".*{re.escape(clean_target)}.*"}} * 1000',
        'probe_duration_seconds * 1000'
    ]
    for dq in dur_queries:
        dur_path = f"/api/v1/query_range?query={quote(dq)}&start={start_ts}&end={end_ts}&step={step}"
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

    events.reverse()

    return jsonify({
        "ok": True,
        "target": target_url,
        "period_minutes": minutes,
        "events": events,
        "latency_points": latency_points
    })



@app.route('/health')
def health():
    """Self-monitoring for InfraWatch's own components — Phase 13. Reused as
    both the container healthcheck (docker-compose) and the dashboard's
    self-status widget, so it stays a single source of truth."""
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

    return jsonify({
        "ok": overall_ok,
        "server_time": now,
        "components": components,
    })

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
ALERTNAME_TARGET_DOWN = "TargetDown"

_poller_state = {}  # instance -> 'up' | 'down', seeded from status.json at startup
_maintenance_active_prev = set()  # instance-scoped maintenance windows active as of the last poll tick
_LAST_POLLER_TICK = [0.0]  # heartbeat for /health — set every tick, whether or not it did work

def compute_state_transitions(success_map, prev_state):
    """Pure function: given instance->probe_success value map and the last
    known state per instance, return (transitions, updated_state).
    An instance seen for the first time only seeds a baseline — it never
    emits a synthetic transition, so a fresh poller/backend start can't
    fabricate a "just recovered"/"just went down" event out of nothing.
    """
    transitions = []
    new_state = dict(prev_state)
    for inst, val in success_map.items():
        is_up = str(val) in ('1', '1.0')
        prev = prev_state.get(inst)
        new_state[inst] = 'up' if is_up else 'down'
        if prev is not None and (prev == 'up') != is_up:
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

def _poll_targets_once():
    _LAST_POLLER_TICK[0] = time.time()

    if time.time() - _LAST_WEBHOOK_AT[0] < WEBHOOK_ACTIVE_WINDOW_SECONDS:
        return  # Alertmanager delivered a webhook recently — it's authoritative, don't double-fire

    instances = get_monitored_instances()
    if not instances:
        return

    success_map, duration_map, _code_map = fetch_all_probe_metrics()
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

        record_alert_event(
            name=ALERTNAME_TARGET_DOWN,
            severity="critical",
            instance=inst,
            summary=f"{inst} recovered" if is_up else f"{inst} is unreachable (probe_success=0)",
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

# Skipped during unit tests (test_alert_transitions.py sets this before
# importing app) so tests don't race a live background thread hitting the
# network and rewriting whichever JSON files happen to be configured.
if os.environ.get("DISABLE_ALERT_POLLER") != "1":
    start_alert_poller()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, threaded=True)
