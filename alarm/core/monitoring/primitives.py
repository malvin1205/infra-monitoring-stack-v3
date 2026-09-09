"""Small pure helpers shared across InfraWatch's request handlers, poller and
aggregator: timestamp coercion, target-string validation/normalisation, job
filtering, scrape-failure classification, Node Exporter host matching, and the
outage-debounce gate.

No Flask, no I/O, no app state — extracted verbatim from app.py so the big file
holds engines and routes, not utilities. Every name is re-imported by app.py.
"""
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse


def parse_alert_timestamp(value, fallback):
    if value:
        try:
            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            # A timezone-naive RFC3339 value (no 'Z', no offset) would otherwise
            # be read as server-local by .timestamp(), shifting the stored
            # incident time by the UTC offset. Alertmanager always sends 'Z',
            # so this only bites a non-standard sender — assume UTC for it.
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (ValueError, TypeError):
            pass
    return fallback


def alert_key(a, labels):
    return a.get('fingerprint') or f"{labels.get('alertname', 'Unknown')}|{labels.get('instance', '-')}"


# Minimal shape check for the Add Target form — not full RFC validation, just
# enough to reject obvious garbage (e.g. a bare number). Bare Docker/internal
# hostnames without a dot (e.g. "webapp") are intentionally allowed — that's
# a real, valid target shape here.
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


def normalize_target(url):
    """Canonical form for dedup/compare only (not for storage): lowercase host,
    drop scheme and a default :80/:443, strip trailing slash. Two entries that
    normalize equal are the same target — 'foo', 'FOO:80', 'http://foo/' all
    collapse to 'foo'."""
    s = (url or "").strip()
    s = re.sub(r'^https?://', '', s, flags=re.I).rstrip('/')
    m = re.match(r'^([^/]+?)(?::(\d+))?(/.*)?$', s)
    if not m:
        return s.lower()
    host, port, path = m.group(1).lower(), m.group(2), m.group(3) or ''
    if port in ('80', '443'):
        port = None
    return host + (f':{port}' if port else '') + path


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


def _sane_epoch(ts):
    """Coerce a down-since value to a plausible Unix-seconds timestamp, else 0.
    The wallboard ages an outage as `now - downSince`; a junk small/negative
    value there rendered as "496859h" (i.e. counting from 1970). ~2001..~2286
    is the accepted window; anything else becomes 0 (the UI then times the
    outage from when it first saw it)."""
    try:
        v = int(float(ts))
    except (TypeError, ValueError):
        return 0
    return v if 1_000_000_000 <= v <= 10_000_000_000 else 0


def _earliest_outage_start(prom_down_since, matched_alerts, health):
    """downSince for the wallboard's "Down for X" aging and the drawer's ongoing
    -outage duration. fetch_down_since_prom_map() is bounded by its subquery
    lookback, so a genuinely long outage still reads as roughly that bound. A
    firing incident row carries the authoritative outage start (poller stamps it
    at first-observed-down; a webhook carries Alertmanager's startsAt) — when one
    exists for this DOWN target, take whichever start is EARLIER, i.e. the
    longer, truer outage. Returns prom_down_since untouched for an up target or
    when there is no usable incident timestamp."""
    if health == 'up':
        return prom_down_since
    starts = []
    if prom_down_since:
        starts.append(prom_down_since)
    for a in matched_alerts or []:
        nm = (a.get('name') or '').lower()
        if a.get('severity') == 'critical' or 'down' in nm or 'unreachable' in nm or 'probe' in nm:
            ts = _sane_epoch(a.get('time'))
            if ts:
                starts.append(ts)
    return min(starts) if starts else prom_down_since


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


# ── Node Exporter infrastructure correlation ─────────────────────────────────
# Only active when the "Use Node Exporter for infrastructure-aware
# availability" setting is ON (see get_availability_settings). Matches a
# blackbox/custom target to a Node Exporter `up` reading by HOST alone,
# ignoring scheme/port on both sides -- the blackbox `instance` label (a URL
# or bare address) and Node Exporter's (host:9100) essentially never share a
# literal string. classify_probe_failure() (fleet_availability.py) then turns
# that match into a service-vs-infrastructure label; this function only does
# the host lookup.
def _extract_host(addr):
    if not addr:
        return ""
    a = str(addr).strip()
    if "://" in a:
        a = urlparse(a).netloc or a
    a = a.split("@")[-1].split("/")[0]
    # Strip a trailing :port. ponytail: naive for bracketed IPv6 literals,
    # fine for the IPv4/hostname targets this app actually manages.
    if a.count(":") == 1:
        host, _, port = a.rpartition(":")
        if port.isdigit():
            a = host
    return a.lower()


def find_node_exporter_status(target_addr, node_exporter_up_map):
    """Returns (found, healthy) for the Node Exporter instance matching
    target_addr's host, or (False, False) if this target has none."""
    target_host = _extract_host(target_addr)
    if not target_host:
        return False, False
    for ne_instance, val in node_exporter_up_map.items():
        if _extract_host(ne_instance) == target_host:
            return True, str(val) in ('1', '1.0')
    return False, False


# ── Outage debounce ────────────────────────────────────────────────────────────
# A target must be continuously down for at least this long before it counts as
# an outage — i.e. before it drives system_status to CRITICAL, sounds the
# frontend alarm, or fires a Telegram alert. Suppresses false alarms from
# momentary blips (one failed scrape, a brief network hiccup) that recover on
# their own. Webhook-delivered alerts are NOT gated here — Alertmanager has its
# own `for:` delay.
OUTAGE_GRACE_SECONDS = float(os.environ.get("OUTAGE_GRACE_SECONDS", "15"))


def _outage_past_grace(down_since_ts, now=None):
    """False while a target has been down for less than OUTAGE_GRACE_SECONDS.
    down_since_ts is Prometheus's last-seen-up timestamp (item['downSince']).
    Fail-open: a missing/zero start time (Prometheus has no last-up sample at
    all) is treated as past grace so a real outage is never hidden."""
    if not down_since_ts:
        return True
    return ((now or time.time()) - down_since_ts) >= OUTAGE_GRACE_SECONDS


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
