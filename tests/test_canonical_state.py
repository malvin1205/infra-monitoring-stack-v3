import json
import time
import os
import tempfile
import shutil
import unittest
from unittest.mock import patch

import app as alarm_app
from app import app, record_alert_event, load_json, save_json
from conftest import TEST_API_KEY, TEST_WEBHOOK_SECRET


class CanonicalMonitoringStateTests(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()
        self.client.environ_base = {"HTTP_X_API_KEY": TEST_API_KEY, "HTTP_X_WEBHOOK_SECRET": TEST_WEBHOOK_SECRET}
        self.tmpdir = tempfile.mkdtemp()
        self._orig = (
            alarm_app.STATUS_FILE,
            alarm_app.HISTORY_FILE,
            alarm_app.HISTORY_ARCHIVE_FILE,
            alarm_app.LOGS_FILE
        )
        alarm_app.STATUS_FILE = os.path.join(self.tmpdir, "status.json")
        alarm_app.HISTORY_FILE = os.path.join(self.tmpdir, "history.json")
        alarm_app.HISTORY_ARCHIVE_FILE = os.path.join(self.tmpdir, "history_archive.json")
        alarm_app.LOGS_FILE = os.path.join(self.tmpdir, "logs.json")
        self.db_path = os.path.join(self.tmpdir, "test_infrawatch.db")
        from storage import init_db
        init_db(self.db_path)
        self._orig_db_env = os.environ.get("INFRAWATCH_DB_PATH")
        os.environ["INFRAWATCH_DB_PATH"] = self.db_path

    def tearDown(self):
        if self._orig_db_env is not None:
            os.environ["INFRAWATCH_DB_PATH"] = self._orig_db_env
        else:
            os.environ.pop("INFRAWATCH_DB_PATH", None)
        (
            alarm_app.STATUS_FILE,
            alarm_app.HISTORY_FILE,
            alarm_app.HISTORY_ARCHIVE_FILE,
            alarm_app.LOGS_FILE
        ) = self._orig
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_probe_down_scenario(self):
        """When a probe is DOWN, /instances and /status reflect CRITICAL system status and is_alarmable=True."""
        raw_targets = {
            "status": "success",
            "data": {
                "activeTargets": [
                    {"labels": {"instance": "host-1", "job": "blackbox"}, "health": "down", "scrapeUrl": "host-1"}
                ]
            }
        }
        with patch.object(alarm_app, 'fetch_prometheus_json', return_value=(raw_targets, 'http://prom:9090')), \
             patch.object(alarm_app, 'fetch_all_probe_metrics', return_value=({"host-1": "0"}, {"host-1": "0.05"}, {"host-1": "500"})), \
             patch.object(alarm_app, 'fetch_down_since_prom_map', return_value={"host-1": time.time() - 120}), \
             patch.object(alarm_app, 'load_website_targets', return_value=[]):

            res = self.client.get('/instances')
            self.assertEqual(res.status_code, 200)
            data = json.loads(res.data)
            self.assertEqual(data['system_status'], 'CRITICAL')
            self.assertEqual(data['summary']['alarmable_down'], 1)
            self.assertTrue(data['summary']['has_alarm'])
            target = data['targets'][0]
            self.assertEqual(target['health'], 'down')
            self.assertEqual(target['severity'], 'critical')
            self.assertEqual(target['effective_status'], 'down')
            self.assertTrue(target['is_alarmable'])

            status_res = self.client.get('/status')
            self.assertEqual(status_res.status_code, 200)
            status_data = json.loads(status_res.data)
            self.assertEqual(status_data['status'], 'CRITICAL')
            self.assertEqual(status_data['system_status'], 'CRITICAL')

    def test_webhook_non_probe_alert_integrated_into_canonical_state(self):
        """Webhook alerts (e.g. HighCPUUsage, DiskSpaceLow) attach to targets in /instances and drive system severity."""
        raw_targets = {
            "status": "success",
            "data": {
                "activeTargets": [
                    {"labels": {"instance": "node-db-01", "job": "node"}, "health": "up", "scrapeUrl": "node-db-01"}
                ]
            }
        }
        now = time.time()
        # Fire non-probe alert via webhook
        payload = {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "HighCPUUsage", "severity": "warning", "instance": "node-db-01", "job": "node"},
                    "annotations": {"summary": "CPU load above 90%"},
                    "startsAt": "2026-08-23T18:00:00Z"
                }
            ]
        }
        res_hook = self.client.post('/webhook', json=payload)
        self.assertEqual(res_hook.status_code, 200)

        with patch.object(alarm_app, 'fetch_prometheus_json', return_value=(raw_targets, 'http://prom:9090')), \
             patch.object(alarm_app, 'fetch_all_probe_metrics', return_value=({"node-db-01": "1"}, {"node-db-01": "0.01"}, {})), \
             patch.object(alarm_app, 'fetch_down_since_prom_map', return_value={}), \
             patch.object(alarm_app, 'load_website_targets', return_value=[]):

            res = self.client.get('/instances')
            self.assertEqual(res.status_code, 200)
            data = json.loads(res.data)

            # System status is WARNING due to HighCPUUsage
            self.assertEqual(data['system_status'], 'WARNING')
            self.assertTrue(data['summary']['has_alarm'])
            target = data['targets'][0]
            self.assertEqual(target['health'], 'up')  # Probe is still up
            self.assertEqual(target['severity'], 'warning')
            self.assertEqual(target['effective_status'], 'degraded')
            self.assertTrue(target['is_alarmable'])
            self.assertEqual(len(target['active_alerts']), 1)
            self.assertEqual(target['active_alerts'][0]['name'], 'HighCPUUsage')

            # /status must match
            status_res = self.client.get('/status')
            status_data = json.loads(status_res.data)
            self.assertEqual(status_data['status'], 'WARNING')
            self.assertEqual(len(status_data['alerts']), 1)

    def test_webhook_resolved_clears_from_canonical_state(self):
        """When a webhook sends resolved, the alert disappears from target and system status recovers."""
        raw_targets = {
            "status": "success",
            "data": {
                "activeTargets": [
                    {"labels": {"instance": "node-db-01", "job": "node"}, "health": "up", "scrapeUrl": "node-db-01"}
                ]
            }
        }
        # Fire then resolve
        self.client.post('/webhook', json={
            "status": "firing",
            "alerts": [{
                "status": "firing",
                "labels": {"alertname": "DiskSpaceLow", "severity": "critical", "instance": "node-db-01", "job": "node"},
                "annotations": {"summary": "Disk 99% full"}
            }]
        })
        self.client.post('/webhook', json={
            "status": "resolved",
            "alerts": [{
                "status": "resolved",
                "labels": {"alertname": "DiskSpaceLow", "severity": "critical", "instance": "node-db-01", "job": "node"},
                "annotations": {"summary": "Disk 99% full"}
            }]
        })

        with patch.object(alarm_app, 'fetch_prometheus_json', return_value=(raw_targets, 'http://prom:9090')), \
             patch.object(alarm_app, 'fetch_all_probe_metrics', return_value=({"node-db-01": "1"}, {}, {})), \
             patch.object(alarm_app, 'fetch_down_since_prom_map', return_value={}), \
             patch.object(alarm_app, 'load_website_targets', return_value=[]):

            res = self.client.get('/instances')
            data = json.loads(res.data)
            self.assertEqual(data['system_status'], 'NORMAL')
            self.assertFalse(data['summary']['has_alarm'])
            self.assertEqual(data['targets'][0]['active_alerts'], [])
            self.assertEqual(data['targets'][0]['severity'], 'ok')

    def test_maintenance_suppression_for_probe_down(self):
        """Active maintenance window suppresses alarm and severity for down targets."""
        raw_targets = {
            "status": "success",
            "data": {
                "activeTargets": [
                    {"labels": {"instance": "maint-host", "job": "blackbox"}, "health": "down", "scrapeUrl": "maint-host"}
                ]
            }
        }
        now = time.time()
        # Create active maintenance window
        from storage import MaintenanceRepository
        MaintenanceRepository.create_window(
            scope="instance", target="maint-host", reason="Scheduled kernel upgrade",
            start=now - 3600, end=now + 3600
        )

        with patch.object(alarm_app, 'fetch_prometheus_json', return_value=(raw_targets, 'http://prom:9090')), \
             patch.object(alarm_app, 'fetch_all_probe_metrics', return_value=({"maint-host": "0"}, {}, {})), \
             patch.object(alarm_app, 'fetch_down_since_prom_map', return_value={"maint-host": now - 600}), \
             patch.object(alarm_app, 'load_website_targets', return_value=[]):

            res = self.client.get('/instances')
            data = json.loads(res.data)
            self.assertEqual(data['system_status'], 'NORMAL')  # Suppressed -> NORMAL
            self.assertFalse(data['summary']['has_alarm'])
            self.assertEqual(data['summary']['alarmable_down'], 0)
            target = data['targets'][0]
            self.assertTrue(target['maintenance'])
            self.assertTrue(target['is_suppressed'])
            self.assertFalse(target['is_alarmable'])
            self.assertEqual(target['severity'], 'maintenance')
            self.assertEqual(target['effective_status'], 'maintenance')

    def test_dependency_suppression(self):
        """When parent is down, child is marked suppressedBy, is_suppressed=True, is_alarmable=False."""
        raw_targets = {
            "status": "success",
            "data": {
                "activeTargets": [
                    {"labels": {"instance": "router-core", "job": "blackbox"}, "health": "down", "scrapeUrl": "router-core"},
                    {"labels": {"instance": "switch-leaf", "job": "blackbox"}, "health": "down", "scrapeUrl": "switch-leaf"}
                ]
            }
        }
        from storage import DependencyRepository
        DependencyRepository.create_dependency(parent="router-core", child="switch-leaf")

        with patch.object(alarm_app, 'fetch_prometheus_json', return_value=(raw_targets, 'http://prom:9090')), \
             patch.object(alarm_app, 'fetch_all_probe_metrics', return_value=({"router-core": "0", "switch-leaf": "0"}, {}, {})), \
             patch.object(alarm_app, 'fetch_down_since_prom_map', return_value={}), \
             patch.object(alarm_app, 'load_website_targets', return_value=[]):

            res = self.client.get('/instances')
            data = json.loads(res.data)
            targets_by_id = {t['instance']: t for t in data['targets']}

            parent = targets_by_id['router-core']
            child = targets_by_id['switch-leaf']

            self.assertTrue(parent['is_alarmable'])
            self.assertEqual(parent['severity'], 'critical')

            self.assertEqual(child['suppressedBy'], 'router-core')
            self.assertTrue(child['is_suppressed'])
            self.assertFalse(child['is_alarmable'])
            self.assertEqual(child['severity'], 'suppressed')
            self.assertEqual(child['effective_status'], 'suppressed')

            # Exactly 1 alarmable down (the root cause parent)
            self.assertEqual(data['summary']['alarmable_down'], 1)
            self.assertEqual(data['summary']['suppressed'], 1)

    def test_restart_while_active_incident_exists_preserves_canonical_state(self):
        """When backend restarts with status.json containing active alert, canonical state retains it."""
        now = time.time()
        save_json(alarm_app.STATUS_FILE, {
            "status": "CRITICAL",
            "alerts": [
                {
                    "key": "TargetDown|server-prod-01",
                    "name": "TargetDown",
                    "severity": "critical",
                    "instance": "server-prod-01",
                    "summary": "Target server-prod-01 is unreachable",
                    "time": now - 300
                }
            ],
            "updated": now - 300
        })

        raw_targets = {
            "status": "success",
            "data": {
                "activeTargets": [
                    {"labels": {"instance": "server-prod-01", "job": "blackbox"}, "health": "down", "scrapeUrl": "server-prod-01"}
                ]
            }
        }

        with patch.object(alarm_app, 'fetch_prometheus_json', return_value=(raw_targets, 'http://prom:9090')), \
             patch.object(alarm_app, 'fetch_all_probe_metrics', return_value=({"server-prod-01": "0"}, {}, {})), \
             patch.object(alarm_app, 'fetch_down_since_prom_map', return_value={"server-prod-01": now - 300}), \
             patch.object(alarm_app, 'load_website_targets', return_value=[]):

            res = self.client.get('/instances')
            data = json.loads(res.data)
            self.assertEqual(data['system_status'], 'CRITICAL')
            target = data['targets'][0]
            self.assertEqual(target['health'], 'down')
            self.assertEqual(len(target['active_alerts']), 1)
            self.assertEqual(target['active_alerts'][0]['name'], 'TargetDown')

    def test_probe_down_and_webhook_alert_coexistence(self):
        """When a target is probe DOWN and ALSO has an active webhook alert, both are captured in canonical state."""
        self.client.post('/webhook', json={
            "status": "firing",
            "alerts": [{
                "status": "firing",
                "labels": {"alertname": "MemoryPressure", "severity": "critical", "instance": "server-hybrid", "job": "node"},
                "annotations": {"summary": "RAM 98%"}
            }]
        })
        raw_targets = {
            "status": "success",
            "data": {
                "activeTargets": [
                    {"labels": {"instance": "server-hybrid", "job": "node"}, "health": "down", "scrapeUrl": "server-hybrid"}
                ]
            }
        }
        with patch.object(alarm_app, 'fetch_prometheus_json', return_value=(raw_targets, 'http://prom:9090')), \
             patch.object(alarm_app, 'fetch_all_probe_metrics', return_value=({"server-hybrid": "0"}, {}, {})), \
             patch.object(alarm_app, 'fetch_down_since_prom_map', return_value={}), \
             patch.object(alarm_app, 'load_website_targets', return_value=[]):

            res = self.client.get('/instances')
            data = json.loads(res.data)
            target = data['targets'][0]
            self.assertEqual(target['health'], 'down')
            self.assertEqual(target['severity'], 'critical')
            self.assertEqual(len(target['active_alerts']), 1)
            self.assertEqual(target['active_alerts'][0]['name'], 'MemoryPressure')
            self.assertEqual(data['system_status'], 'CRITICAL')

    def test_duplicate_webhook_delivery_idempotency(self):
        """Repeated duplicate firing webhooks do not duplicate alert entries in canonical state."""
        payload = {
            "status": "firing",
            "alerts": [{
                "status": "firing",
                "labels": {"alertname": "HighLoad", "severity": "warning", "instance": "host-dup", "job": "node"},
                "annotations": {"summary": "Load average high"}
            }]
        }
        # Send 3 identical webhooks
        for _ in range(3):
            res = self.client.post('/webhook', json=payload)
            self.assertEqual(res.status_code, 200)

        with patch.object(alarm_app, 'fetch_prometheus_json', return_value=({"status": "success", "data": {"activeTargets": []}}, 'http://prom:9090')), \
             patch.object(alarm_app, 'fetch_all_probe_metrics', return_value=({}, {}, {})), \
             patch.object(alarm_app, 'fetch_down_since_prom_map', return_value={}), \
             patch.object(alarm_app, 'load_website_targets', return_value=[]):

            res = self.client.get('/instances')
            data = json.loads(res.data)
            self.assertEqual(len(data['targets']), 1)
            target = data['targets'][0]
            self.assertEqual(target['instance'], 'host-dup')
            self.assertEqual(len(target['active_alerts']), 1)
            self.assertEqual(len(data['active_alerts']), 1)

    def test_recovery_of_probe_down_target(self):
        """When probe recovers from DOWN to UP without active alerts, system status transitions to NORMAL."""
        raw_targets = {
            "status": "success",
            "data": {
                "activeTargets": [
                    {"labels": {"instance": "host-recovered", "job": "blackbox"}, "health": "up", "scrapeUrl": "host-recovered"}
                ]
            }
        }
        with patch.object(alarm_app, 'fetch_prometheus_json', return_value=(raw_targets, 'http://prom:9090')), \
             patch.object(alarm_app, 'fetch_all_probe_metrics', return_value=({"host-recovered": "1"}, {"host-recovered": "0.012"}, {"host-recovered": "200"})), \
             patch.object(alarm_app, 'fetch_down_since_prom_map', return_value={}), \
             patch.object(alarm_app, 'load_website_targets', return_value=[]):

            res = self.client.get('/instances')
            data = json.loads(res.data)
            self.assertEqual(data['system_status'], 'NORMAL')
            self.assertFalse(data['summary']['has_alarm'])
            target = data['targets'][0]
            self.assertEqual(target['health'], 'up')
            self.assertEqual(target['severity'], 'ok')
            self.assertEqual(target['effective_status'], 'up')
            self.assertFalse(target['is_alarmable'])


    def test_job_filtered_poll_does_not_clear_other_jobs_acks(self):
        """A job-scoped poll (?job=X) must not wipe acks for down instances in
        other jobs -- clear_resolved only has visibility into the job-filtered
        `result`, so anything outside that filter looks "absent" and must not
        be treated as recovered."""
        import storage
        raw_targets = {
            "status": "success",
            "data": {
                "activeTargets": [
                    {"labels": {"instance": "host-a", "job": "job-a"}, "health": "down", "scrapeUrl": "host-a"},
                    {"labels": {"instance": "host-b", "job": "job-b"}, "health": "down", "scrapeUrl": "host-b"},
                ]
            }
        }
        storage.AcknowledgmentRepository.acknowledge_instances(["host-a", "host-b"], username="tester")
        self.assertIn("host-a", storage.AcknowledgmentRepository.get_active_acknowledgments())
        self.assertIn("host-b", storage.AcknowledgmentRepository.get_active_acknowledgments())

        with patch.object(alarm_app, 'fetch_prometheus_json', return_value=(raw_targets, 'http://prom:9090')), \
             patch.object(alarm_app, 'fetch_all_probe_metrics', return_value=({"host-a": "0", "host-b": "0"}, {}, {})), \
             patch.object(alarm_app, 'fetch_down_since_prom_map', return_value={}), \
             patch.object(alarm_app, 'load_website_targets', return_value=[]):

            # Poll scoped to job-a only, like a dashboard tab filter would send
            res = self.client.get('/instances?job=job-a')
            self.assertEqual(res.status_code, 200)

        active_acks = storage.AcknowledgmentRepository.get_active_acknowledgments()
        self.assertIn("host-a", active_acks, "job-a's own ack must survive its own scoped poll")
        self.assertIn("host-b", active_acks, "host-b is still down in job-b -- a job-a-scoped poll must not clear it")


if __name__ == '__main__':
    unittest.main()
