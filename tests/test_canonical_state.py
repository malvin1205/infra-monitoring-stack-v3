import json
import time
import os
import tempfile
import shutil
import unittest
from unittest.mock import patch

import app as alarm_app
import json_store
from app import app, record_alert_event, load_json, save_json
from conftest import TEST_API_KEY, TEST_WEBHOOK_SECRET


class CanonicalMonitoringStateTests(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()
        self.client.environ_base = {"HTTP_X_API_KEY": TEST_API_KEY, "HTTP_X_WEBHOOK_SECRET": TEST_WEBHOOK_SECRET}
        self.tmpdir = tempfile.mkdtemp()
        self._orig = (
            json_store.STATUS_FILE,
            json_store.HISTORY_FILE,
            json_store.HISTORY_ARCHIVE_FILE,
            json_store.LOGS_FILE
        )
        json_store.STATUS_FILE = os.path.join(self.tmpdir, "status.json")
        json_store.HISTORY_FILE = os.path.join(self.tmpdir, "history.json")
        json_store.HISTORY_ARCHIVE_FILE = os.path.join(self.tmpdir, "history_archive.json")
        json_store.LOGS_FILE = os.path.join(self.tmpdir, "logs.json")
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
            json_store.STATUS_FILE,
            json_store.HISTORY_FILE,
            json_store.HISTORY_ARCHIVE_FILE,
            json_store.LOGS_FILE
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
        """A real restart preserves the active incident in SQLite (the single
        source of truth for active-alert state, audit F1); canonical state must
        still surface it. status.json is only a denormalized cache now."""
        now = time.time()
        from storage import IncidentRepository
        IncidentRepository.record_alert_event(
            name="TargetDown", severity="critical", instance="server-prod-01",
            summary="Target server-prod-01 is unreachable", job="blackbox",
            event_time=now - 300, is_now_firing=True, key="TargetDown|server-prod-01",
        )
        # Write the status.json cache too — the assertions below must pass
        # regardless of whether it is present, proving canonical state no
        # longer depends on it.
        save_json(json_store.STATUS_FILE, {
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


    def test_outage_grace_debounces_transient_down(self):
        """A target down for less than OUTAGE_GRACE_SECONDS is rendered as down
        but does NOT drive CRITICAL / has_alarm / is_alarmable. Once it's been
        down past the grace window, it becomes a confirmed alarmable outage."""
        raw_targets = {
            "status": "success",
            "data": {"activeTargets": [
                {"labels": {"instance": "blip-1", "job": "blackbox"}, "health": "down", "scrapeUrl": "blip-1"}
            ]},
        }
        common = [
            patch.object(alarm_app, 'fetch_prometheus_json', return_value=(raw_targets, 'http://prom:9090')),
            patch.object(alarm_app, 'fetch_all_probe_metrics', return_value=({"blip-1": "0"}, {"blip-1": "0.05"}, {"blip-1": "0"})),
            patch.object(alarm_app, 'load_website_targets', return_value=[]),
        ]

        # Down for 5s — inside the 15s grace window.
        with patch.object(alarm_app, 'fetch_down_since_prom_map', return_value={"blip-1": time.time() - 5}), \
             common[0], common[1], common[2]:
            data = json.loads(self.client.get('/instances').data)
            self.assertEqual(data['system_status'], 'NORMAL')
            self.assertFalse(data['summary']['has_alarm'])
            self.assertEqual(data['summary']['alarmable_down'], 0)
            t = data['targets'][0]
            self.assertEqual(t['health'], 'down')
            self.assertTrue(t['pending_outage'])
            self.assertFalse(t['is_alarmable'])

        # Down for 30s — past the grace window.
        with patch.object(alarm_app, 'fetch_down_since_prom_map', return_value={"blip-1": time.time() - 30}), \
             common[0], common[1], common[2]:
            data = json.loads(self.client.get('/instances').data)
            self.assertEqual(data['system_status'], 'CRITICAL')
            self.assertTrue(data['summary']['has_alarm'])
            self.assertEqual(data['summary']['alarmable_down'], 1)
            t = data['targets'][0]
            self.assertFalse(t['pending_outage'])
            self.assertTrue(t['is_alarmable'])

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

    def test_unscraped_custom_target_is_no_data_not_alarm(self):
        """A websites.yml target that no Prometheus job scrapes has health
        'unknown' (no probe sample). It must render as no_data, must NOT be
        alarmable, and must NOT drive CRITICAL / the siren — the phantom
        'Down' + Critical on every startup was this being treated as an
        outage with a fail-open (downSince=0) grace check."""
        empty_prom = {"status": "success", "data": {"activeTargets": []}}
        with patch.object(alarm_app, 'fetch_prometheus_json', return_value=(empty_prom, 'http://prom:9090')), \
             patch.object(alarm_app, 'fetch_all_probe_metrics', return_value=({}, {}, {})), \
             patch.object(alarm_app, 'fetch_down_since_prom_map', return_value={}), \
             patch.object(alarm_app, 'load_website_targets', return_value=["10.0.0.88:9100"]):
            data = json.loads(self.client.get('/instances').data)

        self.assertEqual(data['system_status'], 'NORMAL')
        self.assertEqual(data['summary']['alarmable_down'], 0)
        self.assertFalse(data['summary']['has_alarm'])
        self.assertEqual(data['summary']['no_data'], 1)
        self.assertEqual(data['summary']['down'], 0)
        t = next(x for x in data['targets'] if x['instance'] == "10.0.0.88:9100")
        self.assertEqual(t['health'], 'unknown')
        self.assertEqual(t['effective_status'], 'no_data')
        self.assertFalse(t['is_alarmable'])
        self.assertFalse(t['pending_outage'])

    def test_resolve_alert_api_clears_sqlite_phantom(self):
        """POST /api/alerts/resolve force-resolves an incident that is firing
        in SQLite but absent from the status.json cache — the phantom-CRITICAL
        case (audit F1). record_alert_event's cache-first dedupe alone would
        no-op it, so the endpoint resolves straight against SQLite."""
        from storage import IncidentRepository
        now = time.time()
        IncidentRepository.record_alert_event(
            name="TargetDown", severity="critical", instance="ghost-host:9100",
            summary="unreachable", job="blackbox", event_time=now - 3600,
            is_now_firing=True, key="TargetDown|ghost-host:9100",
        )
        self.assertEqual(len(IncidentRepository.get_active_incidents(db_path=self.db_path)), 1)

        res = self.client.post('/api/alerts/resolve', json={"key": "TargetDown|ghost-host:9100"})
        self.assertEqual(res.status_code, 200)
        body = json.loads(res.data)
        self.assertTrue(body["ok"])
        self.assertTrue(body["changed"])
        self.assertEqual(len(IncidentRepository.get_active_incidents(db_path=self.db_path)), 0)

        # Idempotent: a second resolve is a harmless no-op.
        res2 = self.client.post('/api/alerts/resolve', json={"name": "TargetDown", "instance": "ghost-host:9100"})
        self.assertEqual(res2.status_code, 200)
        self.assertFalse(json.loads(res2.data)["changed"])

    def test_resolve_alert_api_requires_identifier(self):
        res = self.client.post('/api/alerts/resolve', json={})
        self.assertEqual(res.status_code, 400)


if __name__ == '__main__':
    unittest.main()
