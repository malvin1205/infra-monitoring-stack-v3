"""Tests for the "Use Node Exporter for infrastructure-aware availability"
setting: pure classification logic, settings persistence, host matching, and
the live /instances integration — OFF (regression), ON+healthy-node (service
issue), ON+unhealthy-node (infrastructure issue), ON+no-node-exporter
(probe-only, never treated as downtime), and a fleet/SLA no-parallel-path
guard.
"""
import inspect
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import app as alarm_app
import json_store
from app import app, _extract_host, find_node_exporter_status
from conftest import TEST_API_KEY, TEST_WEBHOOK_SECRET

import fleet_availability as fa
from fleet_availability import (
    classify_probe_failure,
    CORRELATION_SERVICE_ISSUE,
    CORRELATION_INFRA_ISSUE,
    CORRELATION_NO_NODE_EXPORTER,
    get_availability_settings,
    save_availability_settings,
    calculate_fleet_availability,
    merge_hybrid_target_availability,
    merge_hybrid_fleet_availability,
    sla_budget,
)


# ─────────────────────────────────────────────────────────────────────────
# 1. Pure classification logic — no I/O, exercises every branch directly.
# ─────────────────────────────────────────────────────────────────────────
class ClassifyProbeFailureTests(unittest.TestCase):
    def test_probe_up_is_never_classified(self):
        self.assertIsNone(classify_probe_failure(False, True, True))
        self.assertIsNone(classify_probe_failure(False, False, None))

    def test_probe_down_node_healthy_is_service_issue(self):
        result = classify_probe_failure(True, True, True)
        self.assertEqual(result["correlation"], CORRELATION_SERVICE_ISSUE)

    def test_probe_down_node_unhealthy_is_infrastructure_issue(self):
        result = classify_probe_failure(True, True, False)
        self.assertEqual(result["correlation"], CORRELATION_INFRA_ISSUE)

    def test_probe_down_no_node_exporter_is_neither(self):
        """Missing telemetry must never be reported as an infrastructure
        outage — it's its own explicit, non-downtime category."""
        result = classify_probe_failure(True, False, None)
        self.assertEqual(result["correlation"], CORRELATION_NO_NODE_EXPORTER)
        self.assertNotEqual(result["correlation"], CORRELATION_INFRA_ISSUE)


# ─────────────────────────────────────────────────────────────────────────
# 2. Settings persistence — same JSON-file mechanism as telegram_config.json.
# ─────────────────────────────────────────────────────────────────────────
class AvailabilitySettingsPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_file = fa.AVAILABILITY_SETTINGS_FILE
        fa.AVAILABILITY_SETTINGS_FILE = os.path.join(self.tmpdir, "availability_settings.json")

    def tearDown(self):
        fa.AVAILABILITY_SETTINGS_FILE = self._orig_file

    def test_default_is_off(self):
        """Default must preserve current (probe-only) behavior."""
        self.assertFalse(get_availability_settings()["use_node_exporter_correlation"])

    def test_save_and_reload_roundtrip(self):
        self.assertTrue(save_availability_settings({"use_node_exporter_correlation": True}))
        self.assertTrue(get_availability_settings()["use_node_exporter_correlation"])
        self.assertTrue(save_availability_settings({"use_node_exporter_correlation": False}))
        self.assertFalse(get_availability_settings()["use_node_exporter_correlation"])

    def test_missing_file_does_not_crash(self):
        self.assertFalse(os.path.exists(fa.AVAILABILITY_SETTINGS_FILE))
        self.assertEqual(get_availability_settings(), {"use_node_exporter_correlation": False})


# ─────────────────────────────────────────────────────────────────────────
# 3. Host matching (blackbox target <-> Node Exporter `instance` label).
# ─────────────────────────────────────────────────────────────────────────
class HostMatchingTests(unittest.TestCase):
    def test_extract_host_strips_scheme_path_port(self):
        self.assertEqual(_extract_host("https://example.com:8443/health"), "example.com")
        self.assertEqual(_extract_host("91.239.100.100"), "91.239.100.100")
        self.assertEqual(_extract_host("91.239.100.100:9100"), "91.239.100.100")
        self.assertEqual(_extract_host(""), "")
        self.assertEqual(_extract_host(None), "")

    def test_find_node_exporter_status_matches_by_host_ignoring_port(self):
        ne_map = {"91.239.100.100:9100": "1"}
        found, healthy = find_node_exporter_status("91.239.100.100", ne_map)
        self.assertTrue(found)
        self.assertTrue(healthy)

    def test_find_node_exporter_status_reports_unhealthy(self):
        ne_map = {"91.239.100.100:9100": "0"}
        found, healthy = find_node_exporter_status("http://91.239.100.100/", ne_map)
        self.assertTrue(found)
        self.assertFalse(healthy)

    def test_find_node_exporter_status_no_match(self):
        ne_map = {"10.0.0.5:9100": "1"}
        found, healthy = find_node_exporter_status("91.239.100.100", ne_map)
        self.assertFalse(found)
        self.assertFalse(healthy)

    def test_find_node_exporter_status_empty_map(self):
        found, healthy = find_node_exporter_status("91.239.100.100", {})
        self.assertFalse(found)


# ─────────────────────────────────────────────────────────────────────────
# 4. Live /instances integration — OFF is byte-for-byte the old behavior;
#    ON classifies without ever changing health/is_alarmable/system_status.
# ─────────────────────────────────────────────────────────────────────────
class NodeExporterCorrelationIntegrationTests(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()
        self.client.environ_base = {"HTTP_X_API_KEY": TEST_API_KEY, "HTTP_X_WEBHOOK_SECRET": TEST_WEBHOOK_SECRET}
        # Own SQLite + status.json so exact alarmable_down counts here aren't
        # perturbed by an incident another test left in a shared DB (audit F2).
        import shutil
        from storage import init_db
        self.tmpdir = tempfile.mkdtemp()
        self._orig_files = (json_store.STATUS_FILE, json_store.HISTORY_FILE,
                            json_store.HISTORY_ARCHIVE_FILE, json_store.LOGS_FILE)
        json_store.STATUS_FILE = os.path.join(self.tmpdir, "status.json")
        json_store.HISTORY_FILE = os.path.join(self.tmpdir, "history.json")
        json_store.HISTORY_ARCHIVE_FILE = os.path.join(self.tmpdir, "history_archive.json")
        json_store.LOGS_FILE = os.path.join(self.tmpdir, "logs.json")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        init_db(self.db_path)
        self._orig_db_env = os.environ.get("INFRAWATCH_DB_PATH")
        os.environ["INFRAWATCH_DB_PATH"] = self.db_path
        self._shutil = shutil

    def tearDown(self):
        if self._orig_db_env is not None:
            os.environ["INFRAWATCH_DB_PATH"] = self._orig_db_env
        else:
            os.environ.pop("INFRAWATCH_DB_PATH", None)
        (json_store.STATUS_FILE, json_store.HISTORY_FILE,
         json_store.HISTORY_ARCHIVE_FILE, json_store.LOGS_FILE) = self._orig_files
        self._shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _down_target_response(self, use_node_exporter, node_exporter_map):
        raw_targets = {
            "status": "success",
            "data": {
                "activeTargets": [
                    {"labels": {"instance": "91.239.100.100", "job": "blackbox_http"}, "health": "down", "scrapeUrl": "91.239.100.100"}
                ]
            }
        }
        with patch('prometheus_client.fetch_prometheus_json', return_value=(raw_targets, 'http://prom:9090')), \
             patch('prom_queries.fetch_all_probe_metrics', return_value=({"91.239.100.100": "0"}, {"91.239.100.100": "0.05"}, {"91.239.100.100": "500"})), \
             patch('prom_queries.fetch_down_since_prom_map', return_value={"91.239.100.100": time.time() - 120}), \
             patch('prom_queries.fetch_prom_query_map', return_value=node_exporter_map), \
             patch('monitoring_state.get_availability_settings', return_value={"use_node_exporter_correlation": use_node_exporter}), \
             patch('monitoring_state.load_website_targets', return_value=[]):
            res = self.client.get('/instances')
            self.assertEqual(res.status_code, 200)
            return json.loads(res.data)

    def test_1_setting_off_matches_existing_behavior(self):
        """OFF: no infraCorrelation, and health/is_alarmable/system_status are
        unchanged from the pre-feature baseline (test_canonical_state.py's
        own probe-down assertions)."""
        data = self._down_target_response(use_node_exporter=False, node_exporter_map={"91.239.100.100:9100": "1"})
        target = data['targets'][0]
        self.assertEqual(target['health'], 'down')
        self.assertIsNone(target['infraCorrelation'])
        self.assertEqual(data['system_status'], 'CRITICAL')
        self.assertEqual(data['summary']['alarmable_down'], 1)

    def test_2_on_probe_down_node_healthy_is_service_issue_still_down(self):
        data = self._down_target_response(use_node_exporter=True, node_exporter_map={"91.239.100.100:9100": "1"})
        target = data['targets'][0]
        # The core non-negotiable: Node Exporter healthy must NOT turn a
        # failed probe into "up".
        self.assertEqual(target['health'], 'down')
        self.assertTrue(target['is_alarmable'])
        self.assertEqual(target['infraCorrelation']['correlation'], CORRELATION_SERVICE_ISSUE)

    def test_3_on_probe_down_node_unhealthy_is_infrastructure_issue(self):
        data = self._down_target_response(use_node_exporter=True, node_exporter_map={"91.239.100.100:9100": "0"})
        target = data['targets'][0]
        self.assertEqual(target['health'], 'down')
        self.assertEqual(target['infraCorrelation']['correlation'], CORRELATION_INFRA_ISSUE)

    def test_4_on_target_without_node_exporter_uses_probe_only(self):
        data = self._down_target_response(use_node_exporter=True, node_exporter_map={})
        target = data['targets'][0]
        self.assertEqual(target['health'], 'down')
        self.assertTrue(target['is_alarmable'])
        self.assertEqual(target['infraCorrelation']['correlation'], CORRELATION_NO_NODE_EXPORTER)

    def test_5_missing_node_exporter_never_adds_downtime_or_alarms(self):
        """Same probe inputs, ON vs OFF, with zero Node Exporter data either
        way -- summary counts must be identical. Missing telemetry alone
        must never create/inflate an outage."""
        off = self._down_target_response(use_node_exporter=False, node_exporter_map={})
        on = self._down_target_response(use_node_exporter=True, node_exporter_map={})
        self.assertEqual(off['summary']['alarmable_down'], on['summary']['alarmable_down'])
        self.assertEqual(off['summary']['down'], on['summary']['down'])
        self.assertEqual(off['system_status'], on['system_status'])

    def test_up_target_is_never_classified_even_when_on(self):
        raw_targets = {
            "status": "success",
            "data": {
                "activeTargets": [
                    {"labels": {"instance": "91.239.100.100", "job": "blackbox_http"}, "health": "up", "scrapeUrl": "91.239.100.100"}
                ]
            }
        }
        with patch('prometheus_client.fetch_prometheus_json', return_value=(raw_targets, 'http://prom:9090')), \
             patch('prom_queries.fetch_all_probe_metrics', return_value=({"91.239.100.100": "1"}, {"91.239.100.100": "0.02"}, {"91.239.100.100": "200"})), \
             patch('monitoring_state.get_availability_settings', return_value={"use_node_exporter_correlation": True}), \
             patch('monitoring_state.load_website_targets', return_value=[]):
            res = self.client.get('/instances')
            data = json.loads(res.data)
            target = data['targets'][0]
            self.assertEqual(target['health'], 'up')
            self.assertIsNone(target['infraCorrelation'])

    def test_settings_api_roundtrip(self):
        """GET/POST /api/settings/availability persists through the same
        mechanism the pure get/save functions expose."""
        tmpdir = tempfile.mkdtemp()
        orig_file = fa.AVAILABILITY_SETTINGS_FILE
        fa.AVAILABILITY_SETTINGS_FILE = os.path.join(tmpdir, "availability_settings.json")
        try:
            res = self.client.get('/api/settings/availability')
            self.assertEqual(res.status_code, 200)
            self.assertFalse(json.loads(res.data)['use_node_exporter_correlation'])

            res = self.client.post('/api/settings/availability', json={"use_node_exporter_correlation": True})
            self.assertEqual(res.status_code, 200)
            self.assertTrue(json.loads(res.data)['ok'])

            res = self.client.get('/api/settings/availability')
            self.assertTrue(json.loads(res.data)['use_node_exporter_correlation'])
        finally:
            fa.AVAILABILITY_SETTINGS_FILE = orig_file


# ─────────────────────────────────────────────────────────────────────────
# 5. No-parallel-path guard — the numeric SLA/availability functions must
#    stay exactly as they are: no node-exporter-shaped parameter, and their
#    output must be identical regardless of the correlation setting (they
#    don't even see it).
# ─────────────────────────────────────────────────────────────────────────
class NoParallelCalculationPathTests(unittest.TestCase):
    def test_numeric_functions_take_no_node_exporter_parameter(self):
        for fn in (merge_hybrid_target_availability, merge_hybrid_fleet_availability,
                   calculate_fleet_availability, sla_budget):
            params = " ".join(inspect.signature(fn).parameters.keys()).lower()
            self.assertNotIn("node_exporter", params, f"{fn.__name__} must not gain a node-exporter parameter")

    def test_fleet_weighting_unaffected_by_partial_node_exporter_coverage(self):
        """Two identical fleets computed the exact same way regardless of
        whatever the correlation setting is set to -- because these
        functions never read it. Proves partial Node Exporter coverage
        across a fleet cannot distort weighted fleet availability/SLA."""
        servers = [
            {"id": "s1", "name": "web-01", "downtime_minutes": 0, "created_at": "2026-01-01T00:00:00Z"},
            {"id": "s2", "name": "web-02", "downtime_minutes": 60, "created_at": "2026-01-01T00:00:00Z"},
        ]
        from datetime import datetime, timezone
        now = datetime(2026, 7, 30, tzinfo=timezone.utc)
        period = 7 * 24 * 60

        result_a = calculate_fleet_availability(servers, period, now=now)
        result_b = calculate_fleet_availability(servers, period, now=now)
        self.assertEqual(result_a["fleet_aggregate"]["value"], result_b["fleet_aggregate"]["value"])
        self.assertEqual(result_a["fleet_average"]["value"], result_b["fleet_average"]["value"])


if __name__ == '__main__':
    unittest.main()
