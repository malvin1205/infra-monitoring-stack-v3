"""Set the auth env vars before any test module imports app/auth, since
auth.API_KEY / auth.WEBHOOK_SECRET are read once at import time."""
import os
import sys
from pathlib import Path

# Add alarm directory to sys.path so tests can import alarm modules directly
ALARM_DIR = Path(__file__).resolve().parent.parent / "alarm"
if str(ALARM_DIR) not in sys.path:
    sys.path.insert(0, str(ALARM_DIR))

TEST_API_KEY = "test-api-key"
TEST_WEBHOOK_SECRET = "test-webhook-secret"

os.environ.setdefault("INFRAWATCH_API_KEY", TEST_API_KEY)
os.environ.setdefault("WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)

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
