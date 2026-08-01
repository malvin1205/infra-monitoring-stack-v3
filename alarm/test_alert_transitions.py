import unittest
import sys
import os
import tempfile
import shutil
import time

sys.path.insert(0, os.path.dirname(__file__))
os.environ["DISABLE_ALERT_POLLER"] = "1"  # don't spin up the live network poller during tests

try:
    import app as alarm_app
except ImportError:
    from alarm import app as alarm_app


class ComputeStateTransitionsTests(unittest.TestCase):
    """Pure-function tests for the poller's transition detector — no network,
    no files. Covers the fallback path used when no Alertmanager is present."""

    def test_first_observation_seeds_baseline_without_firing(self):
        transitions, state = alarm_app.compute_state_transitions({"host-a": "1"}, {})
        self.assertEqual(transitions, [])
        self.assertEqual(state, {"host-a": "up"})

    def test_up_to_down_transition_detected(self):
        transitions, state = alarm_app.compute_state_transitions(
            {"host-a": "0"}, {"host-a": "up"})
        self.assertEqual(transitions, [("host-a", False)])
        self.assertEqual(state["host-a"], "down")

    def test_down_to_up_transition_detected(self):
        transitions, state = alarm_app.compute_state_transitions(
            {"host-a": "1"}, {"host-a": "down"})
        self.assertEqual(transitions, [("host-a", True)])
        self.assertEqual(state["host-a"], "up")

    def test_no_change_yields_no_transition(self):
        transitions, state = alarm_app.compute_state_transitions(
            {"host-a": "1"}, {"host-a": "up"})
        self.assertEqual(transitions, [])

    def test_missing_instance_this_tick_keeps_prior_state(self):
        # Prometheus temporarily has no series for an instance — must not be
        # treated as a transition or dropped from tracking.
        transitions, state = alarm_app.compute_state_transitions({}, {"host-a": "down"})
        self.assertEqual(transitions, [])
        self.assertEqual(state, {"host-a": "down"})


class RecordAlertEventTests(unittest.TestCase):
    """Exercises record_alert_event() — the function shared by /webhook and
    the poller — against real temp JSON files (not mocks), matching how it's
    actually used: read-modify-write status/logs/history.json."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig = (alarm_app.STATUS_FILE, alarm_app.HISTORY_FILE,
                      alarm_app.HISTORY_ARCHIVE_FILE, alarm_app.LOGS_FILE)
        alarm_app.STATUS_FILE = os.path.join(self.tmpdir, "status.json")
        alarm_app.HISTORY_FILE = os.path.join(self.tmpdir, "history.json")
        alarm_app.HISTORY_ARCHIVE_FILE = os.path.join(self.tmpdir, "history_archive.json")
        alarm_app.LOGS_FILE = os.path.join(self.tmpdir, "logs.json")

    def tearDown(self):
        (alarm_app.STATUS_FILE, alarm_app.HISTORY_FILE,
         alarm_app.HISTORY_ARCHIVE_FILE, alarm_app.LOGS_FILE) = self._orig
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_firing_then_resolved_lifecycle(self):
        ok = alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="down", job="blackbox", event_time=1000.0, is_now_firing=True)
        self.assertTrue(ok)

        status = alarm_app.load_json(alarm_app.STATUS_FILE, {})
        self.assertEqual(status["status"], "CRITICAL")
        self.assertEqual(len(status["alerts"]), 1)

        history = alarm_app.load_json(alarm_app.HISTORY_FILE, [])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "firing")

        ok = alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="recovered", job="blackbox", event_time=1090.0, is_now_firing=False)
        self.assertTrue(ok)

        status = alarm_app.load_json(alarm_app.STATUS_FILE, {})
        self.assertEqual(status["status"], "NORMAL")
        self.assertEqual(status["alerts"], [])

        history = alarm_app.load_json(alarm_app.HISTORY_FILE, [])
        self.assertEqual(history[0]["status"], "resolved")
        self.assertEqual(history[0]["duration_seconds"], 90)

        logs = alarm_app.load_json(alarm_app.LOGS_FILE, [])
        self.assertEqual(len(logs), 2)
        self.assertEqual(logs[0]["event"], "resolved")   # newest first
        self.assertEqual(logs[0]["duration_seconds"], 90)
        self.assertEqual(logs[1]["event"], "firing")

    def test_repeat_firing_notification_is_deduped(self):
        alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="down", job="blackbox", event_time=1000.0, is_now_firing=True)
        again = alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="still down", job="blackbox", event_time=1010.0, is_now_firing=True)
        self.assertFalse(again)

        logs = alarm_app.load_json(alarm_app.LOGS_FILE, [])
        self.assertEqual(len(logs), 1)  # no duplicate row for the repeat notification

    def test_repeat_resolved_notification_is_deduped(self):
        ok = alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="down", job="blackbox", event_time=1000.0, is_now_firing=False)
        self.assertFalse(ok)  # was never firing — nothing to resolve
        logs = alarm_app.load_json(alarm_app.LOGS_FILE, [])
        self.assertEqual(logs, [])


class MaintenanceModeTests(unittest.TestCase):
    """get_active_maintenance() matching + record_alert_event() suppression —
    the whole point of Phase 9 is that a maintenance window must not leave a
    trace in status/logs/history."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig = (alarm_app.STATUS_FILE, alarm_app.HISTORY_FILE,
                      alarm_app.HISTORY_ARCHIVE_FILE, alarm_app.LOGS_FILE,
                      alarm_app.MAINTENANCE_FILE)
        alarm_app.STATUS_FILE = os.path.join(self.tmpdir, "status.json")
        alarm_app.HISTORY_FILE = os.path.join(self.tmpdir, "history.json")
        alarm_app.HISTORY_ARCHIVE_FILE = os.path.join(self.tmpdir, "history_archive.json")
        alarm_app.LOGS_FILE = os.path.join(self.tmpdir, "logs.json")
        alarm_app.MAINTENANCE_FILE = os.path.join(self.tmpdir, "maintenance.json")

    def tearDown(self):
        (alarm_app.STATUS_FILE, alarm_app.HISTORY_FILE,
         alarm_app.HISTORY_ARCHIVE_FILE, alarm_app.LOGS_FILE,
         alarm_app.MAINTENANCE_FILE) = self._orig
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_instance_scope_matches_only_that_instance(self):
        now = time.time()
        windows = [{"scope": "instance", "target": "10.0.0.5", "start": now - 60, "end": now + 60}]
        self.assertIsNotNone(alarm_app.get_active_maintenance("10.0.0.5", "blackbox", windows=windows))
        self.assertIsNone(alarm_app.get_active_maintenance("10.0.0.6", "blackbox", windows=windows))

    def test_job_scope_matches_any_instance_in_that_job(self):
        now = time.time()
        windows = [{"scope": "job", "target": "blackbox", "start": now - 60, "end": now + 60}]
        self.assertIsNotNone(alarm_app.get_active_maintenance("10.0.0.5", "blackbox", windows=windows))
        self.assertIsNone(alarm_app.get_active_maintenance("10.0.0.5", "other-job", windows=windows))

    def test_expired_window_does_not_match(self):
        now = time.time()
        windows = [{"scope": "instance", "target": "10.0.0.5", "start": now - 120, "end": now - 60}]
        self.assertIsNone(alarm_app.get_active_maintenance("10.0.0.5", "blackbox", windows=windows))

    def test_firing_during_maintenance_leaves_no_trace(self):
        now = time.time()
        alarm_app.save_json(alarm_app.MAINTENANCE_FILE, [
            {"scope": "instance", "target": "10.0.0.5", "start": now - 60, "end": now + 60}
        ])
        ok = alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="down for maintenance", job="blackbox", event_time=now, is_now_firing=True)
        self.assertFalse(ok)
        self.assertEqual(alarm_app.load_json(alarm_app.LOGS_FILE, []), [])
        self.assertEqual(alarm_app.load_json(alarm_app.HISTORY_FILE, []), [])
        status = alarm_app.load_json(alarm_app.STATUS_FILE, {"status": "NORMAL"})
        self.assertEqual(status.get("status", "NORMAL"), "NORMAL")

    def test_firing_resumes_normally_once_window_ends(self):
        past = time.time() - 3600
        alarm_app.save_json(alarm_app.MAINTENANCE_FILE, [
            {"scope": "instance", "target": "10.0.0.5", "start": past - 60, "end": past}
        ])
        ok = alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="down", job="blackbox", event_time=time.time(), is_now_firing=True)
        self.assertTrue(ok)
        self.assertEqual(len(alarm_app.load_json(alarm_app.LOGS_FILE, [])), 1)


class HealthEndpointTests(unittest.TestCase):
    """Phase 13 self-monitoring: /health must report per-component status,
    not just a bare ok:true."""

    def setUp(self):
        self.client = alarm_app.app.test_client()

    def test_health_reports_component_breakdown(self):
        resp = self.client.get('/health')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIn('components', body)
        for key in ('prometheus', 'monitoring_api', 'alarm_service', 'storage'):
            self.assertIn(key, body['components'])
            self.assertIn('ok', body['components'][key])


class MaintenanceApiTests(unittest.TestCase):
    """Phase 9 CRUD surface: create/list/delete a maintenance window."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig = alarm_app.MAINTENANCE_FILE
        alarm_app.MAINTENANCE_FILE = os.path.join(self.tmpdir, "maintenance.json")
        self.client = alarm_app.app.test_client()

    def tearDown(self):
        alarm_app.MAINTENANCE_FILE = self._orig
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_create_list_delete_roundtrip(self):
        now = time.time()
        resp = self.client.post('/api/maintenance', json={
            "target": "10.0.0.9", "scope": "instance", "reason": "reboot",
            "start": now - 60, "end": now + 3600,
        })
        self.assertEqual(resp.status_code, 200)
        window = resp.get_json()['window']
        self.assertTrue(window['id'])

        listed = self.client.get('/api/maintenance').get_json()['windows']
        self.assertEqual(len(listed), 1)
        self.assertTrue(listed[0]['active'])

        deleted = self.client.delete(f'/api/maintenance/{window["id"]}')
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.client.get('/api/maintenance').get_json()['windows'], [])

    def test_create_rejects_end_before_start(self):
        now = time.time()
        resp = self.client.post('/api/maintenance', json={
            "target": "10.0.0.9", "start": now, "end": now - 10,
        })
        self.assertEqual(resp.status_code, 400)


class CorrelationSuppressionTests(unittest.TestCase):
    """Phase 12: apply_correlation_suppression() is display-only — it must
    never change health/downSince, only tag suppressedBy."""

    def test_child_suppressed_when_parent_also_down(self):
        targets = [
            {"instance": "gateway", "health": "down"},
            {"instance": "host-behind-gateway", "health": "down"},
        ]
        alarm_app.apply_correlation_suppression(targets, {"host-behind-gateway": "gateway"})
        self.assertIsNone(targets[0]["suppressedBy"])
        self.assertEqual(targets[1]["suppressedBy"], "gateway")
        self.assertEqual(targets[1]["dependsOn"], "gateway")
        self.assertEqual(targets[1]["health"], "down")  # untouched

    def test_child_not_suppressed_when_parent_is_up(self):
        targets = [
            {"instance": "gateway", "health": "up"},
            {"instance": "host-behind-gateway", "health": "down"},
        ]
        alarm_app.apply_correlation_suppression(targets, {"host-behind-gateway": "gateway"})
        self.assertIsNone(targets[1]["suppressedBy"])

    def test_no_dependency_declared_is_never_suppressed(self):
        targets = [{"instance": "standalone-host", "health": "down"}]
        alarm_app.apply_correlation_suppression(targets, {})
        self.assertIsNone(targets[0]["suppressedBy"])
        self.assertIsNone(targets[0]["dependsOn"])


class DependencyApiTests(unittest.TestCase):
    """Phase 12 CRUD surface: create/list/delete a dependency link."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig = alarm_app.DEPENDENCIES_FILE
        alarm_app.DEPENDENCIES_FILE = os.path.join(self.tmpdir, "dependencies.json")
        self.client = alarm_app.app.test_client()

    def tearDown(self):
        alarm_app.DEPENDENCIES_FILE = self._orig
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_create_list_delete_roundtrip(self):
        resp = self.client.post('/api/dependencies', json={"child": "10.0.0.9", "parent": "10.0.0.1"})
        self.assertEqual(resp.status_code, 200)
        dep = resp.get_json()['dependency']

        listed = self.client.get('/api/dependencies').get_json()['dependencies']
        self.assertEqual(len(listed), 1)

        deleted = self.client.delete(f'/api/dependencies/{dep["id"]}')
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.client.get('/api/dependencies').get_json()['dependencies'], [])

    def test_self_dependency_rejected(self):
        resp = self.client.post('/api/dependencies', json={"child": "10.0.0.9", "parent": "10.0.0.9"})
        self.assertEqual(resp.status_code, 400)

    def test_second_link_for_same_child_replaces_first(self):
        self.client.post('/api/dependencies', json={"child": "c", "parent": "p1"})
        self.client.post('/api/dependencies', json={"child": "c", "parent": "p2"})
        listed = self.client.get('/api/dependencies').get_json()['dependencies']
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]['parent'], 'p2')


if __name__ == "__main__":
    unittest.main()
