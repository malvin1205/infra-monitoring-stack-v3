"""Set the auth env vars before any test module imports app/auth, since
auth.API_KEY / auth.WEBHOOK_SECRET are read once at import time."""
import atexit
import os
import sys
import tempfile
from pathlib import Path

# Add alarm directory to sys.path so tests can import alarm modules directly
ALARM_DIR = Path(__file__).resolve().parent.parent / "alarm"
if str(ALARM_DIR) not in sys.path:
    sys.path.insert(0, str(ALARM_DIR))

TEST_API_KEY = "test-api-key"
TEST_WEBHOOK_SECRET = "test-webhook-secret"

os.environ.setdefault("INFRAWATCH_API_KEY", TEST_API_KEY)
os.environ.setdefault("WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)

# Isolate the whole test session onto a throwaway SQLite DB (audit F2). Several
# integration tests hit app routes without pointing INFRAWATCH_DB_PATH at their
# own temp file — they were reading AND mutating the real alarm/infrawatch.db,
# so the suite polluted the dev DB and one test (test_availability_node_exporter)
# failed on any non-pristine DB. Tests that set INFRAWATCH_DB_PATH themselves
# still override this; their tearDown just restores it to this path instead of
# unsetting it.
_SESSION_DB_FD, _SESSION_DB_PATH = tempfile.mkstemp(prefix="infrawatch_pytest_", suffix=".db")
os.close(_SESSION_DB_FD)
os.unlink(_SESSION_DB_PATH)  # let SQLite create it fresh on first connect
os.environ.setdefault("INFRAWATCH_DB_PATH", _SESSION_DB_PATH)


@atexit.register
def _cleanup_session_db():
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(_SESSION_DB_PATH + suffix)
        except OSError:
            pass

# The Flask test client talks plain http://localhost, so a Secure session
# cookie (now the production default) would never round-trip and every
# session-auth test would 401. Opt the test process back out.
os.environ.setdefault("SESSION_COOKIE_SECURE", "0")

# app.py starts the background alert poller and availability aggregator at
# import time (module-level `if ... : start_alert_poller()`), and Python only
# imports a module once per process. Whichever test file pytest happens to
# collect first decides this for the *entire* session — a couple of test
# files set DISABLE_ALERT_POLLER themselves, but only setting it here (in
# conftest.py, which pytest always loads before any test module in this
# directory) guarantees it regardless of collection order. Without this,
# those background threads run for real during the suite and can race a
# test's own mocks/state (observed: a leftover poller cycle mid-test
# inflating a mocked fetch call counter).
os.environ.setdefault("DISABLE_ALERT_POLLER", "1")
