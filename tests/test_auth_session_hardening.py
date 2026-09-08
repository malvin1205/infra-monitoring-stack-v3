"""Session/lockout hardening regressions (auth audit).

1. A correct password is never blocked by the per-username failed-login
   lockout — the lockout must not be usable as an account-denial DoS.
2. Changing a user's password invalidates that user's other signed-cookie
   sessions (session_epoch bump).
"""
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

import storage
import app as alarm_app


class TestAuthSessionHardening(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="infrawatch_authhard_")
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.env_patcher = patch.dict(os.environ, {
            "INFRAWATCH_ENV_FILE": "",
            "INFRAWATCH_DB_PATH": self.db_path,
            "INFRAWATCH_API_KEY_FILE": os.path.join(self.temp_dir, ".api_key"),
            "INFRAWATCH_WEBHOOK_SECRET_FILE": os.path.join(self.temp_dir, ".webhook_secret"),
            "INFRAWATCH_SESSION_SECRET_FILE": os.path.join(self.temp_dir, ".session_secret"),
        }, clear=False)
        self.env_patcher.start()
        # The lockout self-disables under TESTING; keep it live for test 1.
        alarm_app.app.config["TESTING"] = False
        storage.init_db(self.db_path)
        with alarm_app._LOGIN_FAILS_LOCK:
            alarm_app._LOGIN_FAILS.clear()
        # Fixed-window IP rate-limit buckets are process-global and never
        # expire inside a fast test run — a prior test file's /api/auth/setup
        # calls otherwise trip rate_limit(10, 60) here and the un-asserted
        # setup/login silently 429s.
        alarm_app._RATE_BUCKETS.clear()

    def tearDown(self):
        with alarm_app._LOGIN_FAILS_LOCK:
            alarm_app._LOGIN_FAILS.clear()
        self.env_patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_correct_password_bypasses_account_lockout(self):
        c = alarm_app.app.test_client()
        c.post("/api/auth/setup", json={"username": "victim", "password": "CorrectHorse123"})
        c.post("/api/auth/logout")

        # Attacker has tripped the per-username lockout with bad guesses.
        with alarm_app._LOGIN_FAILS_LOCK:
            alarm_app._LOGIN_FAILS["victim"] = (alarm_app._LOGIN_MAX_FAILS, time.time())

        # Wrong password while locked -> 429.
        r = c.post("/api/auth/login", json={"username": "victim", "password": "wrong-guess"})
        self.assertEqual(r.status_code, 429)

        # Correct password while locked -> still admitted.
        r = c.post("/api/auth/login", json={"username": "victim", "password": "CorrectHorse123"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])

    def test_password_change_invalidates_other_sessions(self):
        admin = alarm_app.app.test_client()
        admin.post("/api/auth/setup", json={"username": "boss", "password": "InitialPass123"})
        admin.post("/api/auth/users", json={
            "username": "worker", "password": "WorkerPass123", "role": "viewer"
        })

        worker = alarm_app.app.test_client()
        worker.post("/api/auth/login", json={"username": "worker", "password": "WorkerPass123"})
        self.assertEqual(worker.get("/api/auth/me").status_code, 200)

        wid = storage.UserRepository.get_by_username("worker")["id"]
        r = admin.patch(f"/api/auth/users/{wid}", json={"password": "RotatedPass456"})
        self.assertEqual(r.status_code, 200)

        # The worker's still-held cookie must no longer authenticate.
        self.assertEqual(worker.get("/api/auth/me").status_code, 401)
        # The admin's own session (password unchanged) is untouched.
        self.assertEqual(admin.get("/api/auth/me").status_code, 200)


if __name__ == "__main__":
    unittest.main()
