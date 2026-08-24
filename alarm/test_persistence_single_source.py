import json
import time
import os
import tempfile
import shutil
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

import app as alarm_app
from app import app, record_alert_event, is_safe_endpoint_url
from storage import (
    init_db, get_db, IncidentRepository, EventLogRepository,
    MaintenanceRepository, DependencyRepository, EndpointRepository,
    DeletedTargetRepository
)
from fleet_availability import calculate_fleet_availability, summarize_entries
from conftest import TEST_API_KEY, TEST_WEBHOOK_SECRET


class SingleSourcePersistenceTests(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()
        self.client.environ_base = {"HTTP_X_API_KEY": TEST_API_KEY, "HTTP_X_WEBHOOK_SECRET": TEST_WEBHOOK_SECRET}
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_infrawatch.db")
        init_db(self.db_path)
        self._orig_db_env = os.environ.get("INFRAWATCH_DB_PATH")
        os.environ["INFRAWATCH_DB_PATH"] = self.db_path

        self._orig_files = (
            alarm_app.STATUS_FILE, alarm_app.HISTORY_FILE,
            alarm_app.LOGS_FILE
        )
        alarm_app.STATUS_FILE = os.path.join(self.tmpdir, "status.json")
        alarm_app.HISTORY_FILE = os.path.join(self.tmpdir, "history.json")
        alarm_app.LOGS_FILE = os.path.join(self.tmpdir, "logs.json")
        alarm_app._ENDPOINTS_CACHE["data"] = None

    def tearDown(self):
        if self._orig_db_env is not None:
            os.environ["INFRAWATCH_DB_PATH"] = self._orig_db_env
        else:
            os.environ.pop("INFRAWATCH_DB_PATH", None)
        (
            alarm_app.STATUS_FILE, alarm_app.HISTORY_FILE,
            alarm_app.LOGS_FILE
        ) = self._orig_files
        alarm_app._ENDPOINTS_CACHE["data"] = None
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_single_source_maintenance_persistence(self):
        """Maintenance windows created via API are written to SQLite and returned in list and active checks."""
        now = time.time()
        res = self.client.post('/api/maintenance', json={
            "target": "srv-prod-db-01",
            "scope": "instance",
            "reason": "Scheduled DB migration",
            "start": now - 30,
            "end": now + 3600
        })
        self.assertEqual(res.status_code, 200)
        win = json.loads(res.data)["window"]
        win_id = win["id"]

        # Directly query SQLite repository
        active = MaintenanceRepository.get_active_maintenance("srv-prod-db-01", db_path=self.db_path)
        self.assertIsNotNone(active)
        self.assertEqual(active["target"], "srv-prod-db-01")
        self.assertEqual(active["reason"], "Scheduled DB migration")

        # GET API must list this active window
        list_res = self.client.get('/api/maintenance')
        self.assertEqual(list_res.status_code, 200)
        windows = json.loads(list_res.data)["windows"]
        self.assertEqual(len(windows), 1)
        self.assertTrue(windows[0]["active"])

        # DELETE through API deletes from SQLite
        del_res = self.client.delete(f'/api/maintenance/{win_id}')
        self.assertEqual(del_res.status_code, 200)
        self.assertIsNone(MaintenanceRepository.get_active_maintenance("srv-prod-db-01", db_path=self.db_path))

    def test_single_source_dependency_persistence(self):
        """Dependencies created via API are written to SQLite DependencyRepository."""
        res = self.client.post('/api/dependencies', json={
            "child": "srv-web-01",
            "parent": "gw-core-01"
        })
        self.assertEqual(res.status_code, 200)
        dep_id = json.loads(res.data)["dependency"]["id"]

        # Check SQLite parent map
        parent_map = DependencyRepository.get_parent_map(db_path=self.db_path)
        self.assertEqual(parent_map.get("srv-web-01"), "gw-core-01")

        # Upsert: changing parent updates single source
        res2 = self.client.post('/api/dependencies', json={
            "child": "srv-web-01",
            "parent": "gw-backup-02"
        })
        self.assertEqual(res2.status_code, 200)
        parent_map2 = DependencyRepository.get_parent_map(db_path=self.db_path)
        self.assertEqual(parent_map2.get("srv-web-01"), "gw-backup-02")
        self.assertEqual(len(parent_map2), 1)

        # Delete removes from SQLite (supports deleting by child instance)
        del_res = self.client.delete('/api/dependencies/srv-web-01')
        self.assertEqual(del_res.status_code, 200)
        self.assertEqual(len(DependencyRepository.get_parent_map(db_path=self.db_path)), 0)

    def test_single_source_endpoint_persistence(self):
        """Endpoints are safely validated for SSRF and persisted to SQLite EndpointRepository."""
        # Valid endpoint creation
        res = self.client.post('/api/endpoints', json={
            "url": "http://prometheus-backup:9090",
            "set_active": True
        })
        self.assertEqual(res.status_code, 200)

        # Query SQLite
        active_ep = EndpointRepository.get_active_endpoint(db_path=self.db_path)
        self.assertIsNotNone(active_ep)
        self.assertEqual(active_ep["url"], "http://prometheus-backup:9090")

        # SSRF checks
        safe, _ = is_safe_endpoint_url("http://169.254.169.254/latest")
        self.assertFalse(safe)
        safe6, _ = is_safe_endpoint_url("http://[fd00:ec2::254]/latest")
        self.assertFalse(safe6)
        safe_meta, _ = is_safe_endpoint_url("http://metadata.google.internal")
        self.assertFalse(safe_meta)

    def test_zero_coverage_mathematical_invariants(self):
        """A target with 0 duration coverage returns None availability and INSUFFICIENT_DATA."""
        from datetime import datetime, timezone
        now = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc)
        servers = [
            {"id": "new-srv", "name": "new-srv", "downtime_minutes": 0, "created_at": now}
        ]
        result = calculate_fleet_availability(servers, 1440.0, now=now)
        val = result["per_server"]["values"][0]
        self.assertIsNone(val["availability_pct"])
        self.assertTrue(val["is_no_data"])
        self.assertEqual(val["sla_status"], "INSUFFICIENT_DATA")
        self.assertFalse(val["sla_eligible"])

    def test_lifecycle_and_duration_calculation(self):
        """Full lifecycle: firing -> duration -> resolve -> history query."""
        start_t = 1700000000.0
        resolve_t = start_t + 125.4

        ok_fire = record_alert_event(
            name="TargetDown", severity="critical", instance="srv-app-10",
            summary="Port 80 unreachable", job="blackbox", event_time=start_t,
            is_now_firing=True
        )
        self.assertTrue(ok_fire)

        # Active incident exists in SQLite
        active = IncidentRepository.get_active_incidents(db_path=self.db_path)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["instance"], "srv-app-10")

        # Resolve
        ok_res = record_alert_event(
            name="TargetDown", severity="critical", instance="srv-app-10",
            summary="Port 80 unreachable", job="blackbox", event_time=resolve_t,
            is_now_firing=False
        )
        self.assertTrue(ok_res)

        # Active incident is cleared, history is recorded with exact duration
        active_after = IncidentRepository.get_active_incidents(db_path=self.db_path)
        self.assertEqual(len(active_after), 0)

        history = IncidentRepository.get_history(limit=10, db_path=self.db_path)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["duration_seconds"], 125.4)


if __name__ == '__main__':
    unittest.main()
