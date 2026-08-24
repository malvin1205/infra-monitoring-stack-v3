"""Set the auth env vars before any test module imports app/auth, since
auth.API_KEY / auth.WEBHOOK_SECRET are read once at import time."""
import os

TEST_API_KEY = "test-api-key"
TEST_WEBHOOK_SECRET = "test-webhook-secret"

os.environ.setdefault("INFRAWATCH_API_KEY", TEST_API_KEY)
os.environ.setdefault("WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)
