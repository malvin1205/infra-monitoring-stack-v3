"""API key / webhook secret auth for state-changing endpoints.

Automatic first-run provisioning:
If INFRAWATCH_API_KEY / WEBHOOK_SECRET are not provided via environment variables,
cryptographically secure 64-character hex tokens are automatically generated and
persisted to local key files (.api_key and .webhook_secret) inside the alarm
storage directory. This ensures zero-setup fresh installations while preserving
full authentication protection across container restarts and reboots.

Precedence:
  1. Explicit environment variable (INFRAWATCH_API_KEY / API_KEY, WEBHOOK_SECRET)
  2. Persisted token file (.api_key, .webhook_secret)
  3. Automatically generated secure token (secrets.token_hex(32)) persisted to file
"""
import hmac
import logging
import os
import secrets
from functools import wraps
from typing import Optional
from flask import request, jsonify

logger = logging.getLogger("infrawatch.auth")

DEFAULT_API_KEY_FILE = os.path.join(os.path.dirname(__file__), ".api_key")
DEFAULT_WEBHOOK_SECRET_FILE = os.path.join(os.path.dirname(__file__), ".webhook_secret")


def _load_env_file():
    env_override = os.environ.get("INFRAWATCH_ENV_FILE")
    if env_override is not None:
        candidates = [env_override] if env_override else []
    else:
        candidates = [
            os.path.join(os.path.dirname(__file__), "..", ".env"),
            os.path.join(os.path.dirname(__file__), ".env"),
            os.path.join(os.getcwd(), ".env")
        ]
    for p in candidates:
        if p and os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip().strip("'\"")
                        if k and k not in os.environ and v:
                            os.environ[k] = v
            except Exception:
                pass
            break


_load_env_file()


def _load_persisted_token(filepath: str) -> Optional[str]:
    """Read a persisted credential from file if it exists and is non-empty."""
    if os.path.isfile(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    return content
        except Exception as e:
            logger.warning("Failed to read persisted token from %s: %s", filepath, e)
    return None


def _persist_token(filepath: str, token: str) -> bool:
    """Safely persist credential to file with restricted permissions (0600)."""
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        mode = 0o600
        fd = os.open(filepath, flags, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token.strip() + "\n")
        try:
            os.chmod(filepath, mode)
        except Exception:
            pass
        return True
    except Exception as e:
        logger.error("Failed to persist token to %s: %s", filepath, e)
        return False


def get_api_key_filepath() -> str:
    """Return the configured path for the persisted API key file."""
    return os.environ.get("INFRAWATCH_API_KEY_FILE") or DEFAULT_API_KEY_FILE


def get_webhook_secret_filepath() -> str:
    """Return the configured path for the persisted webhook secret file."""
    return os.environ.get("INFRAWATCH_WEBHOOK_SECRET_FILE") or DEFAULT_WEBHOOK_SECRET_FILE


def get_or_create_api_key() -> str:
    """Resolve API key: Environment -> Persisted file -> Auto-generate."""
    # 1. Explicit environment variable
    env_key = os.environ.get("INFRAWATCH_API_KEY") or os.environ.get("API_KEY")
    if env_key and env_key.strip():
        return env_key.strip()

    # 2. Persisted key file
    key_path = get_api_key_filepath()
    persisted = _load_persisted_token(key_path)
    if persisted:
        return persisted

    # 3. Auto-generate secure 32-byte (64 hex characters) token
    generated = secrets.token_hex(32)
    _persist_token(key_path, generated)
    logger.info("Automatic API key provisioning: generated new API key and saved to %s", key_path)
    return generated


def get_or_create_webhook_secret() -> str:
    """Resolve Webhook secret: Environment -> Persisted file -> Auto-generate."""
    # 1. Explicit environment variable
    env_secret = os.environ.get("WEBHOOK_SECRET")
    if env_secret and env_secret.strip():
        return env_secret.strip()

    # 2. Persisted secret file
    secret_path = get_webhook_secret_filepath()
    persisted = _load_persisted_token(secret_path)
    if persisted:
        return persisted

    # 3. Auto-generate secure 32-byte (64 hex characters) token
    generated = secrets.token_hex(32)
    _persist_token(secret_path, generated)
    logger.info("Automatic webhook secret provisioning: generated new webhook secret and saved to %s", secret_path)
    return generated


def get_api_key() -> str:
    """Get active API key."""
    global API_KEY
    key = get_or_create_api_key()
    API_KEY = key
    return key


def get_webhook_secret() -> str:
    """Get active Webhook secret."""
    global WEBHOOK_SECRET
    secret = get_or_create_webhook_secret()
    WEBHOOK_SECRET = secret
    return secret


# Module-level variables for backwards compatibility
API_KEY = get_api_key()
WEBHOOK_SECRET = get_webhook_secret()


def _provided_key() -> str:
    header = request.headers.get("X-API-Key") or request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        header = header[len("Bearer "):]
    return header


def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        current_key = get_api_key()
        if not current_key:
            return jsonify({"ok": False, "error": "Server auth not configured (INFRAWATCH_API_KEY unset)"}), 500
        provided = _provided_key()
        if not provided or not hmac.compare_digest(provided, current_key):
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


def require_webhook_secret(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        current_secret = get_webhook_secret()
        if not current_secret:
            return jsonify({"ok": False, "error": "Server auth not configured (WEBHOOK_SECRET unset)"}), 500
        # Header only — a query-string fallback (?secret=...) is prone to
        # leaking via reverse-proxy access logs, browser/proxy history, and
        # Referer headers, none of which a header is exposed to.
        provided = request.headers.get("X-Webhook-Secret") or ""
        if not provided or not hmac.compare_digest(provided, current_secret):
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated
