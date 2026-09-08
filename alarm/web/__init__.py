"""Web layer utilities: middleware, SSRF security, and sliding window rate limiting.
"""
from .middleware import register_web_middleware
from .rate_limit import (
    rate_limit, _client_identity, _RATE_BUCKETS, _RATE_BUCKETS_LOCK,
    _RATE_LAST_PRUNE, _RATE_PRUNE_INTERVAL, _LOGIN_FAILS, _LOGIN_FAILS_LOCK,
    _LOGIN_MAX_FAILS, _LOGIN_LOCK_SECONDS, _login_locked, _login_note_failure,
    _login_clear
)
from .ssrf import _is_blocked_ip, is_safe_endpoint_url
