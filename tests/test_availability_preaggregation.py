import json
import math
import time
import os
import tempfile
import shutil
import unittest
from unittest.mock import patch

import app as alarm_app
from app import app
from storage import init_db, AvailabilityBucketRepository, AggregationLeaseRepository, SlaTargetRepository


class AvailabilityPreaggregationTests(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_preaggregation.db")
        init_db(self.db_path)
        self._orig_db_env = os.environ.get("INFRAWATCH_DB_PATH")
        os.environ["INFRAWATCH_DB_PATH"] = self.db_path
        alarm_app.clear_availability_cache(clear_db=True)

    def tearDown(self):
        alarm_app.clear_availability_cache(clear_db=True)
        if self._orig_db_env is not None:
            os.environ["INFRAWATCH_DB_PATH"] = self._orig_db_env
        else:
            os.environ.pop("INFRAWATCH_DB_PATH", None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_availability_bucket_creation_and_pruning(self):
        """Test bucket saving, retrieval, and retention pruning in SQLite."""
        now = time.time()
        buckets = [
            {
                "instance": "node-1",
                "job": "blackbox",
                "bucket_start": now - 3600,
                "bucket_end": now,
                "uptime_seconds": 3540.0,
                "downtime_seconds": 60.0,
                "unknown_seconds": 0.0,
                "coverage_seconds": 3600.0,
                "sample_count": 1800,
                "availability_pct": 98.33,
                "incident_count": 1,
                "avg_latency_ms": 15.2,
                "updated_at": now
            },
            {
                "instance": "node-2",
                "job": "blackbox",
                "bucket_start": now - 3600,
                "bucket_end": now,
                "uptime_seconds": 3600.0,
                "downtime_seconds": 0.0,
                "unknown_seconds": 0.0,
                "coverage_seconds": 3600.0,
                "sample_count": 1800,
                "availability_pct": 100.0,
                "incident_count": 0,
                "avg_latency_ms": 22.4,
                "updated_at": now
            },
            # Old bucket to test pruning
            {
                "instance": "node-1",
                "job": "blackbox",
                "bucket_start": now - (40 * 86400),
                "bucket_end": now - (40 * 86400) + 3600,
                "uptime_seconds": 3600.0,
                "downtime_seconds": 0.0,
                "unknown_seconds": 0.0,
                "coverage_seconds": 3600.0,
                "sample_count": 1800,
                "availability_pct": 100.0,
                "incident_count": 0,
                "avg_latency_ms": 12.0,
                "updated_at": now - (40 * 86400)
            }
        ]
        AvailabilityBucketRepository.save_buckets(buckets, db_path=self.db_path)

        # Retrieve aggregated
        rows = AvailabilityBucketRepository.get_aggregated_availability(
            job="blackbox",
            start_time=now - 7200,
            end_time=now + 10,
            db_path=self.db_path
        )
        self.assertEqual(len(rows), 2)
        row_map = {r["instance"]: r for r in rows}
        self.assertIn("node-1", row_map)
        self.assertAlmostEqual(row_map["node-1"]["total_uptime_sec"], 3540.0, places=1)
        self.assertAlmostEqual(row_map["node-1"]["total_downtime_sec"], 60.0, places=1)
        self.assertEqual(row_map["node-1"]["total_incidents"], 1)

        # Test retention pruning (35 days retention)
        pruned = AvailabilityBucketRepository.prune_old_buckets(retention_seconds=35 * 86400, db_path=self.db_path)
        self.assertEqual(pruned, 1)

    def test_no_duplicate_buckets(self):
        """UNIQUE constraint prevents duplicate entries for the same target, job, and bucket_start."""
        now = time.time()
        bucket = {
            "instance": "unique-node",
            "job": "blackbox",
            "bucket_start": 1700000000.0,
            "bucket_end": 1700003600.0,
            "uptime_seconds": 3600.0,
            "downtime_seconds": 0.0,
            "unknown_seconds": 0.0,
            "coverage_seconds": 3600.0,
            "sample_count": 1800,
            "availability_pct": 100.0,
            "incident_count": 0,
            "avg_latency_ms": 10.0,
            "updated_at": now
        }
        # Insert twice
        AvailabilityBucketRepository.save_buckets([bucket, bucket], db_path=self.db_path)
        count = AvailabilityBucketRepository.get_bucket_count_in_range("blackbox", 1699990000, 1700010000, db_path=self.db_path)
        self.assertEqual(count, 1)

    def test_multi_worker_aggregation_lease(self):
        """Only one worker acquires the aggregation lease; second worker backs off."""
        worker_1 = "worker_pid_1001"
        worker_2 = "worker_pid_1002"

        # Worker 1 acquires lease
        acquired_1 = AggregationLeaseRepository.acquire_or_renew("avail_aggregator", worker_1, ttl_sec=10.0, db_path=self.db_path)
        self.assertTrue(acquired_1)

        # Worker 2 attempts to acquire same lease -> must fail
        acquired_2 = AggregationLeaseRepository.acquire_or_renew("avail_aggregator", worker_2, ttl_sec=10.0, db_path=self.db_path)
        self.assertFalse(acquired_2)

        # Worker 1 renews lease -> must succeed
        renewed_1 = AggregationLeaseRepository.acquire_or_renew("avail_aggregator", worker_1, ttl_sec=10.0, db_path=self.db_path)
        self.assertTrue(renewed_1)

        # Worker 1 releases lease
        AggregationLeaseRepository.release("avail_aggregator", worker_1, db_path=self.db_path)

        # Worker 2 can now acquire lease
        acquired_2_after = AggregationLeaseRepository.acquire_or_renew("avail_aggregator", worker_2, ttl_sec=10.0, db_path=self.db_path)
        self.assertTrue(acquired_2_after)

    def test_api_does_not_query_prometheus_when_buckets_exist(self):
        """When pre-aggregated buckets exist in SQLite, /api/availability makes 0 Prometheus queries."""
        now = time.time()
        # Seed 24h worth of hourly buckets for 2 nodes
        buckets = []
        for h in range(24):
            b_start = now - (h + 1) * 3600
            b_end = now - h * 3600
            buckets.append({
                "instance": "srv-sql-1",
                "job": "blackbox",
                "bucket_start": b_start,
                "bucket_end": b_end,
                "uptime_seconds": 3600.0,
                "downtime_seconds": 0.0,
                "unknown_seconds": 0.0,
                "coverage_seconds": 3600.0,
                "sample_count": 1800,
                "availability_pct": 100.0,
                "incident_count": 0,
                "avg_latency_ms": 12.0,
                "updated_at": now
            })
            buckets.append({
                "instance": "srv-sql-2",
                "job": "blackbox",
                "bucket_start": b_start,
                "bucket_end": b_end,
                "uptime_seconds": 3564.0,
                "downtime_seconds": 36.0,
                "unknown_seconds": 0.0,
                "coverage_seconds": 3600.0,
                "sample_count": 1800,
                "availability_pct": 99.0,
                "incident_count": 1,
                "avg_latency_ms": 25.0,
                "updated_at": now
            })

        AvailabilityBucketRepository.save_buckets(buckets, db_path=self.db_path)

        query_tracker = []

        def failing_query_map(expr, cache_ttl=5.0, timeout=None):
            query_tracker.append(expr)
            raise AssertionError("Prometheus must NOT be queried when SQLite buckets exist!")

        with patch.object(alarm_app, 'get_monitored_instances', return_value=['srv-sql-1', 'srv-sql-2']), \
             patch.object(alarm_app, 'load_json', return_value=[]), \
             patch.object(alarm_app, 'fetch_prom_query_map', side_effect=failing_query_map):

            # Clear in-memory cache to ensure it reads from SQLite
            with alarm_app._AVAILABILITY_CACHE_LOCK:
                alarm_app._AVAILABILITY_CACHE.clear()

            res = self.client.get('/api/availability?minutes=1440')
            self.assertEqual(res.status_code, 200)
            data = json.loads(res.data)
            self.assertTrue(data['ok'])
            self.assertEqual(data['source'], 'materialized')
            self.assertEqual(len(query_tracker), 0)  # EXACTLY ZERO PROMETHEUS QUERIES!
            self.assertEqual(data['counts']['total'], 2)
            self.assertAlmostEqual(data['overall'], 99.5, places=1)

    def test_cold_start_fallback_and_materialization(self):
        """When SQLite is empty, cold start queries Prometheus once and writes buckets to SQLite."""
        query_count = [0]

        def mock_prom_query(expr, cache_ttl=5.0, timeout=None):
            query_count[0] += 1
            if expr in ('probe_success', 'up'):
                return {'srv-cold-1': '1'}
            return {'srv-cold-1': '99.5'}

        with patch.object(alarm_app, 'get_monitored_instances', return_value=['srv-cold-1']), \
             patch.object(alarm_app, 'load_json', return_value=[]), \
             patch.object(alarm_app, 'fetch_prom_query_map', side_effect=mock_prom_query):

            # 1. Cold start request (SQLite empty)
            res1 = self.client.get('/api/availability?minutes=1440')
            self.assertEqual(res1.status_code, 200)
            data1 = json.loads(res1.data)
            self.assertTrue(data1['ok'])
            self.assertEqual(data1['source'], 'fallback')
            self.assertGreater(query_count[0], 0)

            # Check that buckets were materialized to SQLite
            saved_count = AvailabilityBucketRepository.get_bucket_count_in_range("blackbox", 0, time.time() + 1000, db_path=self.db_path)
            self.assertGreaterEqual(saved_count, 1)

            # 2. Subsequent request with in-memory cache cleared -> must read from SQLite (0 Prometheus queries!)
            with alarm_app._AVAILABILITY_CACHE_LOCK:
                alarm_app._AVAILABILITY_CACHE.clear()

            queries_before_second = query_count[0]
            res2 = self.client.get('/api/availability?minutes=1440')
            self.assertEqual(res2.status_code, 200)
            data2 = json.loads(res2.data)
            self.assertTrue(data2['ok'])
            self.assertEqual(data2['source'], 'materialized')
            self.assertEqual(query_count[0], queries_before_second)  # 0 new queries!

    def test_mathematical_invariants_preserved(self):
        """Verify Coverage = Uptime + Downtime, Window = Coverage + Unknown across 24h, 7d, 30d."""
        now = time.time()
        # Create a 7-day scenario with known downtime
        buckets = []
        for h in range(168):  # 168 hours = 7 days
            b_start = now - (h + 1) * 3600
            b_end = now - h * 3600
            # 10 hours of partial downtime
            if h < 10:
                up = 1800.0
                down = 1800.0
            else:
                up = 3600.0
                down = 0.0
            buckets.append({
                "instance": "inv-node",
                "job": "blackbox",
                "bucket_start": b_start,
                "bucket_end": b_end,
                "uptime_seconds": up,
                "downtime_seconds": down,
                "unknown_seconds": 0.0,
                "coverage_seconds": up + down,
                "sample_count": 1800,
                "availability_pct": round((up / (up + down)) * 100.0, 2),
                "incident_count": 1 if down > 0 else 0,
                "avg_latency_ms": 15.0,
                "updated_at": now
            })

        AvailabilityBucketRepository.save_buckets(buckets, db_path=self.db_path)

        with patch.object(alarm_app, 'get_monitored_instances', return_value=['inv-node']), \
             patch.object(alarm_app, 'load_json', return_value=[]):

            res = self.client.get('/api/availability?minutes=10080')
            self.assertEqual(res.status_code, 200)
            data = json.loads(res.data)
            entry = data['entries'][0]

            # Invariant 1: Coverage = Uptime + Downtime
            self.assertAlmostEqual(entry['coverage_minutes'], entry['uptime_minutes'] + entry['downtime_minutes'], places=1)
            # Invariant 2: Window = Coverage + Unknown
            self.assertAlmostEqual(10080.0, entry['coverage_minutes'] + entry['unknown_minutes'], places=1)
            # Invariant 3: Availability % = Uptime / Coverage * 100
            expected_avail = round((entry['uptime_minutes'] / entry['coverage_minutes']) * 100.0, 2)
            self.assertAlmostEqual(entry['availability_pct'], expected_avail, places=2)
            # Invariant 4: Fleet aggregate equals single target availability
            self.assertEqual(data['overall'], entry['availability_pct'])

    def test_sla_target_repository_crud(self):
        self.assertEqual(SlaTargetRepository.get_all(db_path=self.db_path), {})
        self.assertIsNone(SlaTargetRepository.get_target("web-1", db_path=self.db_path))

        saved = SlaTargetRepository.set_target("web-1", 99.5, updated_by="alice", db_path=self.db_path)
        self.assertEqual(saved, 99.5)
        self.assertEqual(SlaTargetRepository.get_target("web-1", db_path=self.db_path), 99.5)

        # upsert
        SlaTargetRepository.set_target("web-1", 99.95, db_path=self.db_path)
        SlaTargetRepository.set_target("api-2", 99.0, db_path=self.db_path)
        self.assertEqual(SlaTargetRepository.get_all(db_path=self.db_path), {"web-1": 99.95, "api-2": 99.0})

        # clamp
        self.assertEqual(SlaTargetRepository.set_target("x", 150.0, db_path=self.db_path), 100.0)

        self.assertTrue(SlaTargetRepository.delete_target("web-1", db_path=self.db_path))
        self.assertFalse(SlaTargetRepository.delete_target("web-1", db_path=self.db_path))
        self.assertNotIn("web-1", SlaTargetRepository.get_all(db_path=self.db_path))

    def test_aggregation_recovery_after_prometheus_outage(self):
        """Background aggregation handles transient Prometheus exceptions without raising or corrupting DB."""
        with patch.object(alarm_app, 'fetch_prom_query_map', side_effect=Exception("Connection refused (mock prom down)")), \
             patch.object(alarm_app, 'fetch_prom_range_map', side_effect=Exception("Connection refused (mock prom down)")), \
             patch.object(alarm_app, 'get_monitored_instances', return_value=['node-1']):

            # Aggregator cycle must not raise
            alarm_app._aggregate_availability_cycle()

    def test_outage_json_column_roundtrip(self):
        """outage_json accepts a dict on write, comes back as a JSON string;
        legacy rows without it round-trip as NULL."""
        now = time.time()
        base = {
            "instance": "oj-node", "job": "blackbox",
            "uptime_seconds": 3480.0, "downtime_seconds": 120.0, "unknown_seconds": 0.0,
            "coverage_seconds": 3600.0, "sample_count": 240, "availability_pct": 96.67,
            "incident_count": 2, "avg_latency_ms": 11.0, "updated_at": now,
        }
        with_oj = {**base, "bucket_start": now - 3600, "bucket_end": now,
                   "outage_json": {"d": [40.0, 80.0], "ongoing_end": False}}
        legacy = {**base, "bucket_start": now - 7200, "bucket_end": now - 3600}
        AvailabilityBucketRepository.save_buckets([with_oj, legacy], db_path=self.db_path)

        rows = AvailabilityBucketRepository.get_bucket_records(
            "blackbox", now - 10800, now + 10, instances=["oj-node"], db_path=self.db_path)
        by_start = {r["bucket_start"]: r for r in rows}
        self.assertEqual(json.loads(by_start[now - 3600]["outage_json"]),
                         {"d": [40.0, 80.0], "ongoing_end": False})
        self.assertIsNone(by_start[now - 7200]["outage_json"])

    def test_aggregator_exact_reconstruction_path(self):
        """With raw 0/1 range samples available, hourly buckets are built by
        reconstruct_time_series_intervals(): uptime+downtime == coverage
        exactly, downtime reflects the real outage (not an avg_over_time
        smear), incidents come from edges, outage_json is populated, and no
        bucket is written for an hour that has not started."""
        now = time.time()
        hour = math.floor(now / 3600.0) * 3600.0
        if now - hour < 120:              # need a meaningful slice of the ongoing hour
            hour -= 3600.0
        prev_h = hour - 3600.0

        # seed one older bucket so the aggregator takes the steady-state path
        # (last hour + ongoing hour) rather than a 7-day backfill
        AvailabilityBucketRepository.save_buckets([{
            "instance": "srv-exact", "job": "blackbox",
            "bucket_start": prev_h - 3600.0, "bucket_end": prev_h,
            "uptime_seconds": 3600.0, "downtime_seconds": 0.0, "unknown_seconds": 0.0,
            "coverage_seconds": 3600.0, "sample_count": 240, "availability_pct": 100.0,
            "incident_count": 0, "avg_latency_ms": 10.0, "updated_at": now,
        }], db_path=self.db_path)

        # prev hour: up, except one ~120s outage; current hour: up so far
        samples = []
        for t in range(int(prev_h), int(now), 15):
            down = (prev_h + 1000.0) <= t < (prev_h + 1120.0)
            samples.append((float(t), 0 if down else 1))

        def range_map(expr, s, e, step, cache_ttl=5.0, timeout=None):
            if expr != "probe_success":
                return {}
            return {"srv-exact": [(ts, v) for ts, v in samples if s <= ts <= e]}

        with patch.object(alarm_app, 'get_instance_job_map', return_value={"srv-exact": "blackbox"}), \
             patch.object(alarm_app, 'fetch_prom_range_map', side_effect=range_map), \
             patch.object(alarm_app, 'fetch_prom_query_map', side_effect=lambda *a, **k: {}), \
             patch.object(alarm_app.AggregationLeaseRepository, 'acquire_or_renew', return_value=True):
            alarm_app._aggregate_availability_cycle()

        rows = AvailabilityBucketRepository.get_bucket_records(
            "blackbox", prev_h - 10, now + 10, instances=["srv-exact"], db_path=self.db_path)
        by_start = {r["bucket_start"]: r for r in rows}

        self.assertIn(prev_h, by_start)
        pr = by_start[prev_h]
        self.assertAlmostEqual(pr["uptime_seconds"] + pr["downtime_seconds"], pr["coverage_seconds"], delta=0.1)
        self.assertGreater(pr["downtime_seconds"], 60.0)     # the real ~120s outage
        self.assertLess(pr["downtime_seconds"], 220.0)
        self.assertGreaterEqual(pr["incident_count"], 1)
        oj = json.loads(pr["outage_json"])
        self.assertIn("d", oj)
        self.assertIn("ongoing_end", oj)

        self.assertIn(hour, by_start)
        cur = by_start[hour]
        self.assertLessEqual(cur["coverage_seconds"], (now - hour) + 30.0)   # no future time
        self.assertAlmostEqual(cur["uptime_seconds"] + cur["downtime_seconds"], cur["coverage_seconds"], delta=0.1)

        self.assertNotIn(hour + 3600.0, by_start)   # next hour has not started


if __name__ == '__main__':
    unittest.main()
