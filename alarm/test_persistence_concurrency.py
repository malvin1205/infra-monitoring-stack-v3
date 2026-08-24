import json
import time
import os
import tempfile
import shutil
import unittest
from concurrent.futures import ThreadPoolExecutor

import storage
from storage import (
    init_db, get_db, IncidentRepository, EventLogRepository,
    MaintenanceRepository, DependencyRepository, EndpointRepository, DeletedTargetRepository
)


def _multi_process_worker(args):
    db_path, worker_id, count = args
    successes = 0
    for i in range(count):
        inst = f"worker-{worker_id}-host-{i}.net"
        ok = storage.IncidentRepository.record_alert_event(
            name="NodeHighMemory",
            severity="warning",
            instance=inst,
            summary=f"Worker {worker_id} reported memory spike",
            job="node",
            event_time=time.time(),
            is_now_firing=True,
            db_path=db_path
        )
        if ok:
            successes += 1
    return successes


class PersistenceConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_infrawatch.db")
        init_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_scenario_1_concurrent_target_updates_no_lost_updates(self):
        """Concurrent target updates from multiple threads execute atomically with zero lost updates."""
        num_targets = 50

        def fire_alert(i):
            inst = f"target-{i}.internal"
            return IncidentRepository.record_alert_event(
                name="TargetDown",
                severity="critical",
                instance=inst,
                summary=f"Host {inst} is unreachable",
                job="blackbox",
                event_time=time.time(),
                is_now_firing=True,
                db_path=self.db_path
            )

        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(fire_alert, range(num_targets)))

        # All 50 should return True (state transitions recorded)
        self.assertEqual(len([r for r in results if r]), num_targets)

        # Database must have exactly 50 active firing incidents
        active = IncidentRepository.get_active_incidents(db_path=self.db_path)
        self.assertEqual(len(active), num_targets)

        # Event logs must contain exactly 50 firing records
        logs = EventLogRepository.get_logs(limit=100, db_path=self.db_path)
        self.assertEqual(len(logs), num_targets)

    def test_scenario_2_duplicate_webhook_idempotency(self):
        """Repeated duplicate firing webhooks are idempotent and do not create duplicate incidents or logs."""
        now = time.time()
        # First delivery -> True
        res1 = IncidentRepository.record_alert_event(
            name="HighCPUUsage",
            severity="warning",
            instance="srv-cpu-01",
            summary="CPU load 95%",
            job="node",
            event_time=now,
            is_now_firing=True,
            db_path=self.db_path
        )
        self.assertTrue(res1)

        # Duplicate delivery 1 -> False
        res2 = IncidentRepository.record_alert_event(
            name="HighCPUUsage",
            severity="warning",
            instance="srv-cpu-01",
            summary="CPU load 95%",
            job="node",
            event_time=now + 5,
            is_now_firing=True,
            db_path=self.db_path
        )
        self.assertFalse(res2)

        # Duplicate delivery 2 -> False
        res3 = IncidentRepository.record_alert_event(
            name="HighCPUUsage",
            severity="warning",
            instance="srv-cpu-01",
            summary="CPU load 95%",
            job="node",
            event_time=now + 10,
            is_now_firing=True,
            db_path=self.db_path
        )
        self.assertFalse(res3)

        # Exactly 1 active incident, exactly 1 event log
        active = IncidentRepository.get_active_incidents(db_path=self.db_path)
        self.assertEqual(len(active), 1)
        logs = EventLogRepository.get_logs(limit=10, db_path=self.db_path)
        self.assertEqual(len(logs), 1)

    def test_scenario_3_firing_followed_immediately_by_resolved(self):
        """Firing followed immediately by resolved correctly resolves incident and computes duration."""
        start_t = 1000000.0
        resolve_t = start_t + 45.5

        # 1. Fire
        res_fire = IncidentRepository.record_alert_event(
            name="DiskFull",
            severity="critical",
            instance="srv-storage-01",
            summary="Disk /data 100% full",
            job="node",
            event_time=start_t,
            is_now_firing=True,
            db_path=self.db_path
        )
        self.assertTrue(res_fire)

        # 2. Resolve
        res_resolve = IncidentRepository.record_alert_event(
            name="DiskFull",
            severity="critical",
            instance="srv-storage-01",
            summary="Disk /data 100% full",
            job="node",
            event_time=resolve_t,
            is_now_firing=False,
            db_path=self.db_path
        )
        self.assertTrue(res_resolve)

        # 0 active firing incidents
        active = IncidentRepository.get_active_incidents(db_path=self.db_path)
        self.assertEqual(len(active), 0)

        # History contains resolved incident with 45.5s duration
        history = IncidentRepository.get_history(limit=10, db_path=self.db_path)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['duration_seconds'], 45.5)

        # Event logs contain firing and resolved
        logs = EventLogRepository.get_logs(limit=10, db_path=self.db_path)
        self.assertEqual(len(logs), 2)
        self.assertEqual(logs[0]['event'], 'resolved')
        self.assertEqual(logs[0]['duration_seconds'], 45.5)
        self.assertEqual(logs[1]['event'], 'firing')

    def test_scenario_4_poller_and_webhook_race_for_same_incident(self):
        """When poller and Alertmanager webhook race to report the same TargetDown outage, exactly one incident is recorded."""
        now = time.time()

        def submit_event(source):
            return IncidentRepository.record_alert_event(
                name="TargetDown",
                severity="critical",
                instance="srv-flapping-01",
                summary=f"Outage reported by {source}",
                job="blackbox",
                event_time=now,
                is_now_firing=True,
                receiver=source,
                db_path=self.db_path
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(submit_event, "alertmanager_webhook")
            f2 = executor.submit(submit_event, "synthetic_poller")
            r1 = f1.result()
            r2 = f2.result()

        # Exactly one should have transitioned state (True), one should be deduplicated (False)
        self.assertEqual(sorted([r1, r2]), [False, True])

        # Exactly one active incident in DB
        active = IncidentRepository.get_active_incidents(db_path=self.db_path)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]['instance'], 'srv-flapping-01')

    def test_scenario_5_restart_during_active_incident(self):
        """Database retains active incident state across simulated application restart."""
        now = time.time()
        # Incident occurs before shutdown
        IncidentRepository.record_alert_event(
            name="TargetDown",
            severity="critical",
            instance="srv-critical-infra",
            summary="Kernel panic",
            job="blackbox",
            event_time=now - 600,
            is_now_firing=True,
            db_path=self.db_path
        )

        # Simulate fresh connection / new worker start
        restarted_active = IncidentRepository.get_active_incidents(db_path=self.db_path)
        self.assertEqual(len(restarted_active), 1)
        self.assertEqual(restarted_active[0]['instance'], 'srv-critical-infra')
        self.assertEqual(restarted_active[0]['time'], now - 600)

        # Target recovers after restart
        res_recover = IncidentRepository.record_alert_event(
            name="TargetDown",
            severity="critical",
            instance="srv-critical-infra",
            summary="Kernel panic",
            job="blackbox",
            event_time=now,
            is_now_firing=False,
            db_path=self.db_path
        )
        self.assertTrue(res_recover)

        # Successfully resolved
        active_after = IncidentRepository.get_active_incidents(db_path=self.db_path)
        self.assertEqual(len(active_after), 0)
        history = IncidentRepository.get_history(limit=5, db_path=self.db_path)
        self.assertEqual(len(history), 1)
        self.assertAlmostEqual(history[0]['duration_seconds'], 600.0, places=0)

    def test_scenario_6_multi_worker_concurrency(self):
        """Simulate multiple Gunicorn worker threads writing to the SQLite database simultaneously."""
        from concurrent.futures import ThreadPoolExecutor

        num_workers = 4
        items_per_worker = 15
        total_expected = num_workers * items_per_worker

        args_list = [(self.db_path, w, items_per_worker) for w in range(num_workers)]

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            results = list(executor.map(_multi_process_worker, args_list))

        self.assertEqual(sum(results), total_expected)

        active = IncidentRepository.get_active_incidents(db_path=self.db_path)
        self.assertEqual(len(active), total_expected)


if __name__ == '__main__':
    unittest.main()
