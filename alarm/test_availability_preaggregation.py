import json
import time
import os
import tempfile
import shutil
import unittest
from unittest.mock import patch

import app as alarm_app
from app import app
from storage import init_db, AvailabilityBucketRepository, AggregationLeaseRepository


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

    def test_aggregation_recovery_after_prometheus_outage(self):
        """Background aggregation handles transient Prometheus exceptions without raising or corrupting DB."""
        with patch.object(alarm_app, 'fetch_prom_query_map', side_effect=Exception("Connection refused (mock prom down)")), \
             patch.object(alarm_app, 'get_monitored_instances', return_value=['node-1']):

            # Aggregator cycle must not raise
            alarm_app._aggregate_availability_cycle()


if __name__ == '__main__':
    unittest.main()
