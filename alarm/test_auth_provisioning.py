"""Tests for automatic first-run API key and webhook secret provisioning.

Covers:
1. Generates credential when none exists.
2. Generated credential has 64 hex characters (32 random bytes).
3. Existing credential is reused.
4. Explicit environment credential takes precedence.
5. Restart does not regenerate credential.
6. Invalid/missing API credentials remain unauthorized (401).
7. Valid generated credential authenticates successfully (200).
8. Credential is not printed in normal logs.
9. Persistence survives application/container restart.
10. Webhook secret validation and persistence.
"""
import importlib
import json
import logging
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import auth


class TestAuthProvisioning(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="infrawatch_auth_test_")
        self.api_key_file = os.path.join(self.temp_dir, ".api_key")
        self.webhook_secret_file = os.path.join(self.temp_dir, ".webhook_secret")

        self.db_path = os.path.join(self.temp_dir, "test_infrawatch.db")

        # Configure auth to use isolated test paths and no host .env interference
        self.env_patcher = patch.dict(os.environ, {
            "INFRAWATCH_ENV_FILE": "",
            "INFRAWATCH_API_KEY_FILE": self.api_key_file,
            "INFRAWATCH_WEBHOOK_SECRET_FILE": self.webhook_secret_file,
            "INFRAWATCH_DB_PATH": self.db_path,
        }, clear=False)
        self.env_patcher.start()

        # Ensure no env keys interfere
        os.environ.pop("INFRAWATCH_API_KEY", None)
        os.environ.pop("API_KEY", None)
        os.environ.pop("WEBHOOK_SECRET", None)

    def tearDown(self):
        self.env_patcher.stop()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_generates_credential_when_none_exists(self):
        """1. Generates credential when none exists."""
        self.assertFalse(os.path.exists(self.api_key_file))
        key = auth.get_or_create_api_key()
        self.assertTrue(bool(key))
        self.assertTrue(os.path.exists(self.api_key_file))

        with open(self.api_key_file, "r", encoding="utf-8") as f:
            persisted_key = f.read().strip()
        self.assertEqual(key, persisted_key)

    def test_generated_credential_has_64_hex_characters(self):
        """2. Generated credential has exactly 64 hexadecimal characters."""
        key = auth.get_or_create_api_key()
        self.assertEqual(len(key), 64)
        # Verify it consists of valid hexadecimal characters
        int(key, 16)

        secret = auth.get_or_create_webhook_secret()
        self.assertEqual(len(secret), 64)
        int(secret, 16)

    def test_existing_persisted_credential_is_reused(self):
        """3. Existing persisted credential is reused."""
        custom_key = "a" * 64
        with open(self.api_key_file, "w", encoding="utf-8") as f:
            f.write(custom_key + "\n")

        loaded_key = auth.get_or_create_api_key()
        self.assertEqual(loaded_key, custom_key)

    def test_explicit_environment_credential_takes_precedence(self):
        """4. Explicit environment credential takes precedence over persisted file without overwriting."""
        persisted_key = "b" * 64
        env_key = "c" * 64

        with open(self.api_key_file, "w", encoding="utf-8") as f:
            f.write(persisted_key + "\n")

        # Test INFRAWATCH_API_KEY
        with patch.dict(os.environ, {"INFRAWATCH_API_KEY": env_key}):
            active_key = auth.get_or_create_api_key()
            self.assertEqual(active_key, env_key)
            # Verify file was not overwritten
            with open(self.api_key_file, "r", encoding="utf-8") as f:
                self.assertEqual(f.read().strip(), persisted_key)

        # Test fallback API_KEY env var
        with patch.dict(os.environ, {"API_KEY": env_key}):
            os.environ.pop("INFRAWATCH_API_KEY", None)
            active_key = auth.get_or_create_api_key()
            self.assertEqual(active_key, env_key)

        # Test WEBHOOK_SECRET env var
        env_secret = "d" * 64
        with patch.dict(os.environ, {"WEBHOOK_SECRET": env_secret}):
            active_secret = auth.get_or_create_webhook_secret()
            self.assertEqual(active_secret, env_secret)

    def test_restart_does_not_regenerate_credential(self):
        """5. Restart does not regenerate credential (persisted key is retained)."""
        initial_key = auth.get_or_create_api_key()
        initial_secret = auth.get_or_create_webhook_secret()

        # Simulate second start / process cycle
        second_key = auth.get_or_create_api_key()
        second_secret = auth.get_or_create_webhook_secret()

        self.assertEqual(initial_key, second_key)
        self.assertEqual(initial_secret, second_secret)

    def test_invalid_missing_api_credentials_unauthorized(self):
        """6. Missing or invalid credentials return 401 Unauthorized."""
        import app as alarm_app
        client = alarm_app.app.test_client()

        generated_key = auth.get_or_create_api_key()

        # Missing key -> 401
        res_missing = client.post("/api/maintenance", json={
            "scope": "instance",
            "target": "10.0.0.1",
            "start": 1000,
            "end": 2000
        })
        self.assertEqual(res_missing.status_code, 401)
        data = json.loads(res_missing.data)
        self.assertFalse(data.get("ok"))
        self.assertEqual(data.get("error"), "Unauthorized")

        # Invalid key in X-API-Key header -> 401
        res_invalid = client.post(
            "/api/maintenance",
            headers={"X-API-Key": "invalid_key_value"},
            json={"scope": "instance", "target": "10.0.0.1", "start": 1000, "end": 2000}
        )
        self.assertEqual(res_invalid.status_code, 401)

        # Invalid Bearer token in Authorization header -> 401
        res_invalid_bearer = client.post(
            "/api/maintenance",
            headers={"Authorization": "Bearer wrong_token"},
            json={"scope": "instance", "target": "10.0.0.1", "start": 1000, "end": 2000}
        )
        self.assertEqual(res_invalid_bearer.status_code, 401)

    def test_valid_generated_credential_authenticates_successfully(self):
        """7. Valid generated credential authenticates successfully."""
        import app as alarm_app
        client = alarm_app.app.test_client()

        generated_key = auth.get_or_create_api_key()

        # Authenticate with X-API-Key
        res_header = client.post(
            "/api/maintenance",
            headers={"X-API-Key": generated_key},
            json={"scope": "instance", "target": "10.0.0.99", "start": 1000, "end": 2000}
        )
        self.assertEqual(res_header.status_code, 200)
        data = json.loads(res_header.data)
        self.assertTrue(data.get("ok"))

        # Authenticate with Bearer token
        res_bearer = client.post(
            "/api/maintenance",
            headers={"Authorization": f"Bearer {generated_key}"},
            json={"scope": "instance", "target": "10.0.0.98", "start": 1000, "end": 2000}
        )
        self.assertEqual(res_bearer.status_code, 200)
        data = json.loads(res_bearer.data)
        self.assertTrue(data.get("ok"))

    def test_credential_is_not_printed_in_normal_logs(self):
        """8. Credential plaintext is never emitted in normal logs."""
        logger = logging.getLogger("infrawatch.auth")
        with self.assertLogs(logger, level="INFO") as captured:
            key = auth.get_or_create_api_key()
            secret = auth.get_or_create_webhook_secret()

        log_output = " ".join(captured.output)
        self.assertNotIn(key, log_output)
        self.assertNotIn(secret, log_output)
        self.assertTrue(any("generated new API key and saved to" in line for line in captured.output))

    def test_persistence_survives_application_restart(self):
        """9. Persistence survives application/container restart."""
        # Initial run: key is generated and persisted
        key1 = auth.get_or_create_api_key()
        secret1 = auth.get_or_create_webhook_secret()

        # Simulate process termination and fresh import
        reloaded_auth = importlib.reload(auth)
        key2 = reloaded_auth.get_or_create_api_key()
        secret2 = reloaded_auth.get_or_create_webhook_secret()

        self.assertEqual(key1, key2)
        self.assertEqual(secret1, secret2)

    def test_webhook_secret_authentication(self):
        """10. Webhook secret validation: header-only (query-string fallback removed)."""
        import app as alarm_app
        client = alarm_app.app.test_client()

        generated_secret = auth.get_or_create_webhook_secret()

        sample_alertmanager_payload = {
            "version": "4",
            "groupKey": "test",
            "status": "resolved",
            "receiver": "webhook",
            "alerts": []
        }

        # Missing secret -> 401
        res_missing = client.post("/webhook", json=sample_alertmanager_payload)
        self.assertEqual(res_missing.status_code, 401)

        # Invalid secret via header -> 401
        res_invalid = client.post(
            "/webhook",
            headers={"X-Webhook-Secret": "wrong"},
            json=sample_alertmanager_payload
        )
        self.assertEqual(res_invalid.status_code, 401)

        # Valid secret via query param is REJECTED — query-string fallback was
        # removed (prone to leaking via proxy access logs / Referer headers).
        res_query = client.post(
            f"/webhook?secret={generated_secret}",
            json=sample_alertmanager_payload
        )
        self.assertEqual(res_query.status_code, 401)

        # Valid secret via X-Webhook-Secret header -> 200
        res_header = client.post(
            "/webhook",
            headers={"X-Webhook-Secret": generated_secret},
            json=sample_alertmanager_payload
        )
        self.assertEqual(res_header.status_code, 200)


if __name__ == "__main__":
    unittest.main()
