"""API key / webhook secret auth for state-changing endpoints.

Fail-closed: if INFRAWATCH_API_KEY / WEBHOOK_SECRET aren't set in the
environment, protected routes reject every request instead of opening up.
"""
import hmac
import os
from functools import wraps
from flask import request, jsonify

API_KEY = os.environ.get("INFRAWATCH_API_KEY", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")


def _provided_key():
    header = request.headers.get("X-API-Key") or request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        header = header[len("Bearer "):]
    return header


def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not API_KEY:
            return jsonify({"ok": False, "error": "Server auth not configured (INFRAWATCH_API_KEY unset)"}), 500
        provided = _provided_key()
        if not provided or not hmac.compare_digest(provided, API_KEY):
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


def require_webhook_secret(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not WEBHOOK_SECRET:
            return jsonify({"ok": False, "error": "Server auth not configured (WEBHOOK_SECRET unset)"}), 500
        provided = request.headers.get("X-Webhook-Secret") or request.args.get("secret", "")
        if not provided or not hmac.compare_digest(provided, WEBHOOK_SECRET):
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated
