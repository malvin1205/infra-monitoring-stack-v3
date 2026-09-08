"""Authentication & Role-Based Authorization for InfraWatch.

Three Authentication Domains:
1. HUMAN (Browser):
   - Secure Session Cookie (HttpOnly, SameSite=Lax, Secure)
   - First-run Admin Setup -> Username + Password -> Login/Logout
   - Role-Based Access Control (Admin / Viewer)
2. MACHINE / AUTOMATION (M2M):
   - X-API-Key or Authorization: Bearer <key>
   - Auto-provisioned in .api_key or INFRAWATCH_API_KEY
   - Grants full administrative access for automated tooling/scripts
3. WEBHOOK (Alertmanager):
   - X-Webhook-Secret
   - Auto-provisioned in .webhook_secret or WEBHOOK_SECRET

Precedence for credentials:
  1. Explicit environment variable
  2. Persisted token file
  3. Automatically generated secure token persisted to file
"""
import hmac
import logging
import os
import secrets
from functools import wraps
from typing import Optional, Dict, Any, Set
from flask import request, jsonify, session, g
from werkzeug.security import generate_password_hash, check_password_hash

logger = logging.getLogger("infrawatch.auth")

try:
    from config import DATA_DIR, ALARM_DIR
except ImportError:
    from alarm.config import DATA_DIR, ALARM_DIR

DEFAULT_API_KEY_FILE = os.path.join(DATA_DIR, ".api_key")
DEFAULT_WEBHOOK_SECRET_FILE = os.path.join(DATA_DIR, ".webhook_secret")
DEFAULT_SESSION_SECRET_FILE = os.path.join(DATA_DIR, ".session_secret")

# Safe migration: if secrets exist in legacy ALARM_DIR and not in DATA_DIR, copy them over.
try:
    for _fn in (".api_key", ".webhook_secret", ".session_secret"):
        _legacy = os.path.join(ALARM_DIR, _fn)
        _target = os.path.join(DATA_DIR, _fn)
        if os.path.exists(_legacy) and not os.path.exists(_target):
            import shutil
            shutil.copy2(_legacy, _target)
except Exception:
    pass

# ── Role & Permission Definitions ─────────────────────────────────────────────
# 'owner' is the founding account created by first-boot /api/auth/setup. It
# has every permission 'admin' has; what sets it apart is enforced in the user
# -management routes, not here: an admin cannot assign the owner role or modify
# the owner account, only the owner can. The role is permanent and there is
# exactly one (see storage.init_db's backfill for pre-existing deployments).
ROLE_PERMISSIONS: Dict[str, Set[str]] = {
    "admin": {
        "dashboard.read",
        "targets.read",
        "targets.write",
        "endpoints.read",
        "endpoints.write",
        "maintenance.read",
        "maintenance.write",
        "dependencies.read",
        "dependencies.write",
        "telegram.read",
        "telegram.write",
        "availability.read",
        "availability.write",
        "users.manage",
        "alerts.ack",
        "audit.read",
    },
    "viewer": {
        "dashboard.read",
        "targets.read",
        "endpoints.read",
        "maintenance.read",
        "dependencies.read",
        "availability.read",
    }
}
ROLE_PERMISSIONS["owner"] = set(ROLE_PERMISSIONS["admin"])

# Roles with full administrative access (permission checks short-circuit).
PRIVILEGED_ROLES = ("owner", "admin")


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
    return os.environ.get("INFRAWATCH_API_KEY_FILE") or DEFAULT_API_KEY_FILE


def get_webhook_secret_filepath() -> str:
    return os.environ.get("INFRAWATCH_WEBHOOK_SECRET_FILE") or DEFAULT_WEBHOOK_SECRET_FILE


def get_session_secret_filepath() -> str:
    return os.environ.get("INFRAWATCH_SESSION_SECRET_FILE") or DEFAULT_SESSION_SECRET_FILE


def get_or_create_api_key() -> str:
    """Resolve API key: Environment -> Persisted file -> Auto-generate."""
    env_key = os.environ.get("INFRAWATCH_API_KEY") or os.environ.get("API_KEY")
    if env_key and env_key.strip():
        return env_key.strip()

    key_path = get_api_key_filepath()
    persisted = _load_persisted_token(key_path)
    if persisted:
        return persisted

    generated = secrets.token_hex(32)
    _persist_token(key_path, generated)
    logger.info("Automatic API key provisioning: generated new API key and saved to %s", key_path)
    return generated


def get_or_create_webhook_secret() -> str:
    """Resolve Webhook secret: Environment -> Persisted file -> Auto-generate."""
    env_secret = os.environ.get("WEBHOOK_SECRET")
    if env_secret and env_secret.strip():
        return env_secret.strip()

    secret_path = get_webhook_secret_filepath()
    persisted = _load_persisted_token(secret_path)
    if persisted:
        return persisted

    generated = secrets.token_hex(32)
    _persist_token(secret_path, generated)
    logger.info("Automatic webhook secret provisioning: generated new webhook secret and saved to %s", secret_path)
    return generated


def get_or_create_session_secret() -> str:
    """Resolve Session secret for Flask cookie signing: Environment -> Persisted file -> Auto-generate."""
    env_secret = os.environ.get("INFRAWATCH_SESSION_SECRET") or os.environ.get("SECRET_KEY")
    if env_secret and env_secret.strip():
        return env_secret.strip()

    secret_path = get_session_secret_filepath()
    persisted = _load_persisted_token(secret_path)
    if persisted:
        return persisted

    generated = secrets.token_hex(32)
    _persist_token(secret_path, generated)
    logger.info("Automatic session secret provisioning: generated new session secret and saved to %s", secret_path)
    return generated


def get_api_key() -> str:
    global API_KEY
    key = get_or_create_api_key()
    API_KEY = key
    return key


def get_webhook_secret() -> str:
    global WEBHOOK_SECRET
    secret = get_or_create_webhook_secret()
    WEBHOOK_SECRET = secret
    return secret


def get_session_secret() -> str:
    global SESSION_SECRET
    sec = get_or_create_session_secret()
    SESSION_SECRET = sec
    return sec


# Module-level variables for backwards compatibility
API_KEY = get_api_key()
WEBHOOK_SECRET = get_webhook_secret()
SESSION_SECRET = get_session_secret()


# ── Password Hashing Helpers ──────────────────────────────────────────────────
def hash_password(password: str) -> str:
    """Generate a secure cryptographic password hash using standard Werkzeug security."""
    return generate_password_hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a plaintext password against a stored password hash."""
    if not password or not password_hash:
        return False
    return check_password_hash(password_hash, password)


# ── Credential Extraction & Current User Resolution ───────────────────────────
def _provided_key() -> str:
    header = request.headers.get("X-API-Key") or request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        header = header[len("Bearer "):]
    return header.strip()


def get_current_authenticated_user() -> Optional[Dict[str, Any]]:
    """Resolves the current authenticated user from either:
    1. Machine API Key (X-API-Key / Authorization: Bearer) -> Admin identity
    2. Human Session (Flask session cookie user_id) -> Database user record
    """
    # 1. Check Machine API Key
    provided = _provided_key()
    if provided:
        current_key = get_api_key()
        if current_key and hmac.compare_digest(provided, current_key):
            return {
                "id": 0,
                "username": "m2m:api_key",
                "display_name": "API Key Automation",
                "role": "admin",
                "is_active": 1,
                "is_m2m": True,
                "permissions": sorted(list(ROLE_PERMISSIONS["admin"]))
            }

    # 2. Check Human Session Cookie
    user_id = session.get("user_id")
    if user_id is not None:
        try:
            try:
                from storage import UserRepository
            except ImportError:
                from alarm.storage import UserRepository
            user = UserRepository.get_by_id(user_id, include_password_hash=False)
            if user and user.get("is_active"):
                # Signed-cookie sessions have no server-side store. A password
                # change bumps users.session_epoch; a cookie minted before that
                # (possibly on another device) no longer matches and is
                # rejected here. Sessions minted before this field existed
                # carry no "epoch" and are left alone until they expire.
                sess_epoch = session.get("epoch")
                if sess_epoch is not None and sess_epoch != user.get("session_epoch", 0):
                    return None
                role = user.get("role", "viewer")
                perms = ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS["viewer"])
                user_dict = dict(user)
                user_dict.pop("session_epoch", None)
                user_dict["is_m2m"] = False
                user_dict["permissions"] = sorted(list(perms))
                return user_dict
        except Exception as e:
            logger.error("Failed to load user from session: %s", e)

    return None


def has_permission(user: Optional[Dict[str, Any]], permission: Optional[str] = None) -> bool:
    """Check if the user has the required permission."""
    if not user:
        return False
    if not permission:
        return True
    if user.get("role") in PRIVILEGED_ROLES:
        return True
    role = user.get("role", "viewer")
    perms = ROLE_PERMISSIONS.get(role, set())
    return permission in perms


# ── Unified Authorization Decorators ──────────────────────────────────────────
def require_permission(permission: Optional[str] = None):
    """Enforces that the incoming request is authenticated (via session or API key)
    and has the specified permission. Injects g.current_user for the route handler.
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            user = get_current_authenticated_user()
            if not user:
                return jsonify({"ok": False, "error": "Unauthorized"}), 401
            if permission and not has_permission(user, permission):
                return jsonify({"ok": False, "error": f"Forbidden: '{permission}' permission required"}), 403
            g.current_user = user
            return f(*args, **kwargs)
        return decorated
    return decorator


def require_auth(f):
    """Requires authentication (session or API key) with any valid active role."""
    return require_permission(None)(f)


def require_admin(f):
    """Requires full admin role."""
    return require_permission("users.manage")(f)


def require_api_key(f):
    """Backward-compatible decorator for existing endpoints.
    Accepts valid Machine API Key OR authenticated human session with required permission.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        current_key = get_api_key()
        if not current_key:
            return jsonify({"ok": False, "error": "Server auth not configured (INFRAWATCH_API_KEY unset)"}), 500

        user = get_current_authenticated_user()
        if not user:
            return jsonify({"ok": False, "error": "Unauthorized"}), 401

        # Check if caller has write permissions for mutations (or is admin/owner)
        if user.get("role") not in PRIVILEGED_ROLES:
            return jsonify({"ok": False, "error": "Forbidden: Admin permission required"}), 403

        g.current_user = user
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

