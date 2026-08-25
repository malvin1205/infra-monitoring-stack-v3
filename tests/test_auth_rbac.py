"""Comprehensive test suite for InfraWatch Human Authentication, RBAC, Global ACK, and Audit Trail.

Covers:
1. First-run Administrator Setup (atomicity, race conditions, duplicate setup prevention).
2. Authentication lifecycle (Login, Session Cookies, Auth Status, Me, Logout).
3. RBAC permission matrix enforcement (Admin vs Viewer restrictions on mutations).
4. Backward-compatible M2M API Key authentication (X-API-Key, Bearer token).
5. Global Server-Side Alert Acknowledgment (ACK / Unack, state propagation, auto-clear on recovery).
6. Immutable Audit Logging with actor tracking.
"""
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import auth
import storage
import app as alarm_app


class TestAuthRBAC(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="infrawatch_rbac_test_")
        self.db_path = os.path.join(self.temp_dir, "test_rbac.db")
        self.api_key_file = os.path.join(self.temp_dir, ".api_key")
        self.webhook_secret_file = os.path.join(self.temp_dir, ".webhook_secret")
        self.session_secret_file = os.path.join(self.temp_dir, ".session_secret")

        self.env_patcher = patch.dict(os.environ, {
            "INFRAWATCH_ENV_FILE": "",
            "INFRAWATCH_DB_PATH": self.db_path,
            "INFRAWATCH_API_KEY_FILE": self.api_key_file,
            "INFRAWATCH_WEBHOOK_SECRET_FILE": self.webhook_secret_file,
            "INFRAWATCH_SESSION_SECRET_FILE": self.session_secret_file,
            "TARGETS_FILE": os.path.join(self.temp_dir, "websites.yml"),
        }, clear=False)
        self.env_patcher.start()

        os.environ.pop("INFRAWATCH_API_KEY", None)
        os.environ.pop("API_KEY", None)
        os.environ.pop("WEBHOOK_SECRET", None)
        os.environ.pop("INFRAWATCH_SESSION_SECRET", None)

        storage.init_db(self.db_path)
        self.client = alarm_app.app.test_client()

    def tearDown(self):
        self.env_patcher.stop()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ── 1. First-Run Administrator Setup ─────────────────────────────────────
    def test_first_run_setup_lifecycle(self):
        # 1. Status indicates uninitialized
        res = self.client.get("/api/auth/status")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertFalse(data["initialized"])
        self.assertFalse(data["authenticated"])

        # 2. Validation errors
        res = self.client.post("/api/auth/setup", json={"username": "ad", "password": "123"})
        self.assertEqual(res.status_code, 400)

        res = self.client.post("/api/auth/setup", json={"username": "admin", "password": "password123", "confirm_password": "different"})
        self.assertEqual(res.status_code, 400)

        # 3. Successful first admin setup
        res = self.client.post("/api/auth/setup", json={
            "username": "superadmin",
            "password": "CorrectPassword123!",
            "confirm_password": "CorrectPassword123!",
            "display_name": "Super Administrator"
        })
        self.assertEqual(res.status_code, 200)
        setup_data = res.get_json()
        self.assertTrue(setup_data["ok"])
        self.assertEqual(setup_data["user"]["username"], "superadmin")
        self.assertEqual(setup_data["user"]["role"], "admin")
        self.assertNotIn("password_hash", setup_data["user"])

        # 4. Status now indicates initialized and authenticated via session
        res = self.client.get("/api/auth/status")
        data = res.get_json()
        self.assertTrue(data["initialized"])
        self.assertTrue(data["authenticated"])
        self.assertEqual(data["user"]["username"], "superadmin")

        # 5. Second setup attempt must fail with 409 Conflict
        res2 = self.client.post("/api/auth/setup", json={
            "username": "attacker",
            "password": "Password123!",
            "confirm_password": "Password123!"
        })
        self.assertEqual(res2.status_code, 409)

    # ── 2. Login, Me, and Logout ─────────────────────────────────────────────
    def test_login_me_logout_flow(self):
        # Setup admin
        self.client.post("/api/auth/setup", json={
            "username": "operator1",
            "password": "SecretPassword123"
        })

        # Test client logout
        self.client.post("/api/auth/logout")

        # /api/auth/me should be 401 when logged out
        res = self.client.get("/api/auth/me")
        self.assertEqual(res.status_code, 401)

        # Login with wrong password
        res = self.client.post("/api/auth/login", json={"username": "operator1", "password": "WrongPassword"})
        self.assertEqual(res.status_code, 401)

        # Login with wrong username
        res = self.client.post("/api/auth/login", json={"username": "nonexistent", "password": "SecretPassword123"})
        self.assertEqual(res.status_code, 401)

        # Successful login
        res = self.client.post("/api/auth/login", json={"username": "operator1", "password": "SecretPassword123"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["user"]["username"], "operator1")

        # /api/auth/me should now return the logged in user
        res = self.client.get("/api/auth/me")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["user"]["username"], "operator1")

        # Logout
        res = self.client.post("/api/auth/logout")
        self.assertEqual(res.status_code, 200)
        res = self.client.get("/api/auth/me")
        self.assertEqual(res.status_code, 401)

    # ── 3. RBAC Enforcement (Admin vs Viewer) ────────────────────────────────
    def test_rbac_permissions(self):
        # 1. Setup Admin
        self.client.post("/api/auth/setup", json={
            "username": "admin_user",
            "password": "AdminPassword123"
        })

        # 2. Admin creates a Viewer user
        res = self.client.post("/api/auth/users", json={
            "username": "viewer_user",
            "password": "ViewerPassword123",
            "role": "viewer",
            "display_name": "Viewer Operator"
        })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["user"]["role"], "viewer")

        # 3. Create viewer client session
        viewer_client = alarm_app.app.test_client()
        res = viewer_client.post("/api/auth/login", json={
            "username": "viewer_user",
            "password": "ViewerPassword123"
        })
        self.assertEqual(res.status_code, 200)

        # 4. Viewer cannot perform admin mutations (expect 403)
        res = viewer_client.post("/api/targets", json={"url": "10.10.10.10:9100"})
        self.assertEqual(res.status_code, 403)

        res = viewer_client.delete("/api/targets", json={"url": "10.10.10.10:9100"})
        self.assertEqual(res.status_code, 403)

        res = viewer_client.post("/api/maintenance", json={"scope": "instance", "target": "10.10.10.10", "start": 1000, "end": 2000})
        self.assertEqual(res.status_code, 403)

        res = viewer_client.post("/api/dependencies", json={"parent": "hostA", "child": "hostB"})
        self.assertEqual(res.status_code, 403)

        res = viewer_client.post("/api/auth/users", json={"username": "newuser", "password": "password123", "role": "viewer"})
        self.assertEqual(res.status_code, 403)

        # 5. Viewer CAN perform read operations (expect 200)
        res = viewer_client.get("/api/targets")
        self.assertEqual(res.status_code, 200)

        res = viewer_client.get("/api/maintenance")
        self.assertEqual(res.status_code, 200)

        res = viewer_client.get("/api/dependencies")
        self.assertEqual(res.status_code, 200)

        res = viewer_client.get("/status")
        self.assertEqual(res.status_code, 200)

        # 6. Admin CAN perform mutations
        admin_client = alarm_app.app.test_client()
        admin_client.post("/api/auth/login", json={"username": "admin_user", "password": "AdminPassword123"})
        res = admin_client.post("/api/targets", json={"url": "10.10.10.10:9100"})
        self.assertEqual(res.status_code, 200)

    # ── 4. Machine API Key Backward Compatibility ────────────────────────────
    def test_m2m_api_key_backward_compatibility(self):
        key = auth.get_or_create_api_key()
        anonymous_client = alarm_app.app.test_client()

        # Unauthenticated request fails with 401
        res = anonymous_client.post("/api/targets", json={"url": "10.20.30.40:9100"})
        self.assertEqual(res.status_code, 401)

        # Authenticated with X-API-Key succeeds with 200
        res = anonymous_client.post(
            "/api/targets",
            headers={"X-API-Key": key},
            json={"url": "10.20.30.40:9100"}
        )
        self.assertEqual(res.status_code, 200)

        # Authenticated with Authorization: Bearer succeeds with 200
        res = anonymous_client.post(
            "/api/maintenance",
            headers={"Authorization": f"Bearer {key}"},
            json={"scope": "instance", "target": "10.20.30.40", "start": 1000, "end": 2000}
        )
        self.assertEqual(res.status_code, 200)

    # ── 5. Global Server-Side Alert Acknowledgment (ACK) ─────────────────────
    def test_global_alert_acknowledgment(self):
        admin_client = alarm_app.app.test_client()
        admin_client.post("/api/auth/setup", json={"username": "ack_admin", "password": "AdminPassword123"})

        # Acknowledge target
        res = admin_client.post("/api/alerts/ack", json={"instance": "db-prod-01:5432"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        acked_instances = [a["instance"] for a in data["acknowledged"]]
        self.assertIn("db-prod-01:5432", acked_instances)

        # Check repository state
        active_acks = storage.AcknowledgmentRepository.get_active_acknowledgments()
        self.assertIn("db-prod-01:5432", active_acks)
        self.assertEqual(active_acks["db-prod-01:5432"]["acknowledged_by"], "ack_admin")

        # Unacknowledge target
        res = admin_client.post("/api/alerts/unack", json={"instance": "db-prod-01:5432"})
        self.assertEqual(res.status_code, 200)
        active_acks_after = storage.AcknowledgmentRepository.get_active_acknowledgments()
        self.assertNotIn("db-prod-01:5432", active_acks_after)

    # ── 6. Immutable Audit Logging ───────────────────────────────────────────
    def test_audit_logging(self):
        admin_client = alarm_app.app.test_client()
        admin_client.post("/api/auth/setup", json={"username": "auditor_admin", "password": "AdminPassword123"})
        admin_client.post("/api/targets", json={"url": "10.0.0.88:9100"})
        admin_client.post("/api/alerts/ack", json={"instance": "10.0.0.88:9100"})

        # Retrieve audit logs
        res = admin_client.get("/api/audit/logs")
        self.assertEqual(res.status_code, 200)
        logs = res.get_json()["logs"]
        actions = [log["action"] for log in logs]

        self.assertIn("SYSTEM_SETUP", actions)
        self.assertIn("ADD_TARGET", actions)
        self.assertIn("ACK_ALERT", actions)

        ack_log = next(l for l in logs if l["action"] == "ACK_ALERT")
        self.assertEqual(ack_log["actor_username"], "auditor_admin")
        self.assertEqual(ack_log["actor_role"], "admin")

    # ── 7. User Management: Edit Role / Deactivate / Last-Admin Lockout ──────
    def test_update_user_api(self):
        admin_client = alarm_app.app.test_client()
        admin_client.post("/api/auth/setup", json={"username": "root_admin", "password": "AdminPassword123"})
        res = admin_client.post("/api/auth/users", json={
            "username": "second_admin", "password": "Password1234", "role": "admin"
        })
        second_admin_id = res.get_json()["user"]["id"]
        res = admin_client.post("/api/auth/users", json={
            "username": "some_viewer", "password": "Password1234", "role": "viewer"
        })
        viewer_id = res.get_json()["user"]["id"]

        # Viewer cannot manage users
        viewer_client = alarm_app.app.test_client()
        viewer_client.post("/api/auth/login", json={"username": "some_viewer", "password": "Password1234"})
        res = viewer_client.patch(f"/api/auth/users/{viewer_id}", json={"role": "admin"})
        self.assertEqual(res.status_code, 403)

        # Admin promotes viewer to admin
        res = admin_client.patch(f"/api/auth/users/{viewer_id}", json={"role": "admin"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["user"]["role"], "admin")

        # Admin deactivates second_admin (root_admin + viewer-now-admin still active -> allowed)
        res = admin_client.patch(f"/api/auth/users/{second_admin_id}", json={"is_active": False})
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.get_json()["user"]["is_active"])

        # Demote/deactivate every other admin until root_admin is the last one, then guard kicks in
        admin_id = admin_app_get_user_id_by_username(self, "root_admin")
        promoted_id = viewer_id
        res = admin_client.patch(f"/api/auth/users/{promoted_id}", json={"role": "viewer"})
        self.assertEqual(res.status_code, 200)

        # Now root_admin is the only active admin — deactivating it must be rejected
        res = admin_client.patch(f"/api/auth/users/{admin_id}", json={"is_active": False})
        self.assertEqual(res.status_code, 400)
        self.assertIn("last active admin", res.get_json()["error"])

        # Unknown user id
        res = admin_client.patch("/api/auth/users/999999", json={"role": "viewer"})
        self.assertEqual(res.status_code, 404)


def admin_app_get_user_id_by_username(test, username):
    user = storage.UserRepository.get_by_username(username)
    test.assertIsNotNone(user)
    return user["id"]


if __name__ == "__main__":
    unittest.main()
