"""Request-layer throttling for InfraWatch.

A lightweight in-process fixed-window rate limiter (`rate_limit` decorator) and
a per-username login lockout. Cross-cutting, not a domain — pulled out of
app.py so the route module stays route definitions. flask only; no InfraWatch
imports. app.py re-imports every name, so `alarm_app._RATE_BUCKETS` /
`alarm_app.rate_limit` in the tests are unchanged.
"""
import hashlib
import threading
import time
from functools import wraps

from flask import request, session, jsonify, current_app

# ── Rate limiting ─────────────────────────────────────────────────────────────
# Lightweight in-process fixed-window limiter. No Redis/external store: this
# app runs as a single gunicorn worker (Dockerfile), so a plain dict guarded
# by a lock is sufficient — same pattern as the caches elsewhere. A production
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
        # Bucket by a short digest, not the raw secret — the limiter dict is
        # process-global and gets logged/inspected during debugging.
        return "key:" + hashlib.sha256(header.encode("utf-8", "ignore")).hexdigest()[:16]
    return f"ip:{request.remote_addr or 'unknown'}"


# ── Per-username login throttle ──────────────────────────────────────────────
# The IP-based rate_limit() on /api/auth/login is the outer floor; this adds a
# per-account lockout so a single target account can't be sprayed 20/min/IP
# from a rotating IP pool. Disabled under TESTING so the suite's negative-path
# login tests don't trip it.
_LOGIN_FAILS = {}
_LOGIN_FAILS_LOCK = threading.Lock()
_LOGIN_MAX_FAILS = 10
_LOGIN_LOCK_SECONDS = 300.0

def _login_locked(username):
    if current_app.config.get("TESTING"):
        return False
    now = time.time()
    with _LOGIN_FAILS_LOCK:
        rec = _LOGIN_FAILS.get(username)
        if not rec:
            return False
        fails, first_ts = rec
        if now - first_ts > _LOGIN_LOCK_SECONDS:
            _LOGIN_FAILS.pop(username, None)
            return False
        return fails >= _LOGIN_MAX_FAILS

def _login_note_failure(username):
    now = time.time()
    with _LOGIN_FAILS_LOCK:
        fails, first_ts = _LOGIN_FAILS.get(username, (0, now))
        if now - first_ts > _LOGIN_LOCK_SECONDS:
            fails, first_ts = 0, now
        _LOGIN_FAILS[username] = (fails + 1, first_ts)

def _login_clear(username):
    with _LOGIN_FAILS_LOCK:
        _LOGIN_FAILS.pop(username, None)

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
