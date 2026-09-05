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
from storage import init_db, AvailabilityBucketRepository, AggregationLeaseRepository
from fleet_availability import (
    clip_hourly_bucket,
    merge_hybrid_target_availability,
    merge_hybrid_fleet_availability,
    summarize_entries,
)


class HybridAvailabilityRegressionTests(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_hybrid.db")
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

    def test_a_full_sqlite_and_prometheus_overlap(self):
        """Test A: Prometheus has recent 2d of data, SQLite has 30d of data.
        Prometheus must override the recent 2d, SQLite provides earlier 28d.
        No double counting: coverage must be <= 30d, coverage + unknown == 30d."""
        now = time.time()
        req_start = now - 30 * 86400.0
        req_end = now
        prom_p_start = now - 2 * 86400.0

        # Pre-populate 30 days of SQLite hourly buckets (720 hours)
        sqlite_buckets = []
        cur_t = req_start
        while cur_t < req_end:
            nxt_t = cur_t + 3600.0
            sqlite_buckets.append({
                "instance": "srv-a",
                "job": "blackbox",
                "bucket_start": cur_t,
                "bucket_end": nxt_t,
                "uptime_seconds": 3600.0,
                "downtime_seconds": 0.0,
                "unknown_seconds": 0.0,
                "coverage_seconds": 3600.0,
                "sample_count": 1800,
                "availability_pct": 100.0,
                "incident_count": 0,
                "avg_latency_ms": 12.0,
                "updated_at": now,
            })
            cur_t = nxt_t

        # Prometheus provides 2 days of data with 50% uptime
        prom_metrics = {
            "first_ts": prom_p_start,
            "last_ts": req_end,
            "count": str(2 * 86400 // 2),  # 2s cadence
            "avail": "50.0",
            "incidents": "2",
            "duration": "15.0",
        }

        entry = merge_hybrid_target_availability(
            req_start=req_start,
            req_end=req_end,
            target_id="srv-a",
            target_name="srv-a",
            job="blackbox",
            sqlite_buckets=sqlite_buckets,
            prom_metrics=prom_metrics,
            expected_interval_sec=2.0,
        )

        expected_window = 30 * 86400.0
        # Invariants
        self.assertAlmostEqual(entry["observed_seconds"], expected_window, delta=5.0)
        self.assertAlmostEqual(entry["observed_seconds"] + entry["unknown_seconds"], expected_window, delta=5.0)
        self.assertAlmostEqual(entry["uptime_seconds"] + entry["downtime_seconds"], entry["observed_seconds"], delta=5.0)
        # SQLite should contribute 28 days (28 * 86400 = 2,419,200s)
        self.assertAlmostEqual(entry["sqlite_seconds"], 28 * 86400.0, delta=5.0)
        # Prometheus should contribute 2 days (2 * 86400 = 172,800s)
        self.assertAlmostEqual(entry["prometheus_seconds"], 2 * 86400.0, delta=5.0)
        # Overlap removed is 2 days
        self.assertAlmostEqual(entry["overlap_removed_seconds"], 2 * 86400.0, delta=5.0)
        # Availability: (28d * 100% + 2d * 50%) / 30d = (28 + 1)/30 = 29/30 = 96.67%
        self.assertAlmostEqual(entry["availability_pct"], 96.67, places=1)
        self.assertEqual(entry["source"], "hybrid")

    def test_b_sqlite_gap_prometheus(self):
        """Test B: SQLite has data from Day -30 to Day -4, Gap from Day -4 to Day -2.5,
        Prometheus has data from Day -2.5 to Now.
        Result: 26d SQLite + 1.5d UNKNOWN + 2.5d Prometheus.
        Invariant: coverage + unknown == 30d."""
        now = time.time()
        req_start = now - 30 * 86400.0
        req_end = now
        sqlite_end = now - 4 * 86400.0
        prom_p_start = now - 2.5 * 86400.0

        sqlite_buckets = []
        cur_t = req_start
        while cur_t < sqlite_end:
            nxt_t = cur_t + 3600.0
            sqlite_buckets.append({
                "instance": "srv-b",
                "job": "blackbox",
                "bucket_start": cur_t,
                "bucket_end": nxt_t,
                "uptime_seconds": 3600.0,
                "downtime_seconds": 0.0,
                "unknown_seconds": 0.0,
                "coverage_seconds": 3600.0,
                "sample_count": 1800,
                "availability_pct": 100.0,
                "incident_count": 0,
                "avg_latency_ms": 10.0,
                "updated_at": now,
            })
            cur_t = nxt_t

        prom_metrics = {
            "first_ts": prom_p_start,
            "last_ts": req_end,
            "count": str(int(2.5 * 86400 / 2.0)),
            "avail": "100.0",
            "incidents": "0",
            "duration": "10.0",
        }

        entry = merge_hybrid_target_availability(
            req_start=req_start,
            req_end=req_end,
            target_id="srv-b",
            target_name="srv-b",
            job="blackbox",
            sqlite_buckets=sqlite_buckets,
            prom_metrics=prom_metrics,
            expected_interval_sec=2.0,
        )

        expected_window = 30 * 86400.0
        expected_sqlite = 26 * 86400.0
        expected_prom = 2.5 * 86400.0
        expected_unknown = 1.5 * 86400.0

        self.assertAlmostEqual(entry["sqlite_seconds"], expected_sqlite, delta=5.0)
        self.assertAlmostEqual(entry["prometheus_seconds"], expected_prom, delta=5.0)
        self.assertAlmostEqual(entry["unknown_seconds"], expected_unknown, delta=5.0)
        self.assertAlmostEqual(entry["observed_seconds"] + entry["unknown_seconds"], expected_window, delta=5.0)
        self.assertAlmostEqual(entry["coverage_percent"], ((26 + 2.5) / 30.0) * 100.0, places=1)
        self.assertEqual(entry["source"], "hybrid")

    def test_c_prometheus_only_retention_limited(self):
        """Test C: 7d request, SQLite empty, Prometheus has ~59.6h of retention.
        Result: ~59.6h coverage, ~108.4h UNKNOWN.
        Invariant: coverage + unknown == 7d. Coverage percent ≈ 35.5%."""
        now = time.time()
        req_start = now - 7 * 86400.0
        req_end = now
        retention_sec = 59.6 * 3600.0
        prom_p_start = now - retention_sec

        prom_metrics = {
            "first_ts": prom_p_start,
            "last_ts": req_end,
            "count": str(int(retention_sec / 2.0)),
            "avail": "99.0",
            "incidents": "1",
            "duration": "14.0",
        }

        entry = merge_hybrid_target_availability(
            req_start=req_start,
            req_end=req_end,
            target_id="srv-c",
            target_name="srv-c",
            job="blackbox",
            sqlite_buckets=[],
            prom_metrics=prom_metrics,
            expected_interval_sec=2.0,
        )

        expected_window = 7 * 86400.0
        expected_cov = retention_sec
        expected_unk = expected_window - expected_cov

        self.assertAlmostEqual(entry["sqlite_seconds"], 0.0, delta=1.0)
        self.assertAlmostEqual(entry["prometheus_seconds"], expected_cov, delta=5.0)
        self.assertAlmostEqual(entry["observed_seconds"], expected_cov, delta=5.0)
        self.assertAlmostEqual(entry["unknown_seconds"], expected_unk, delta=5.0)
        self.assertAlmostEqual(entry["observed_seconds"] + entry["unknown_seconds"], expected_window, delta=5.0)
        self.assertAlmostEqual(entry["coverage_percent"], (59.6 / 168.0) * 100.0, places=1)
        self.assertEqual(entry["source"], "fallback")

    def test_d_sqlite_only_historical_range(self):
        """Test D: Historical range (Day -60 to Day -30), Prometheus has no data,
        SQLite has complete data.
        Result: 100% SQLite, 0% Prometheus, 0 UNKNOWN, source = 'materialized'."""
        now = time.time()
        req_start = now - 60 * 86400.0
        req_end = now - 30 * 86400.0

        sqlite_buckets = []
        cur_t = req_start
        while cur_t < req_end:
            nxt_t = cur_t + 3600.0
            sqlite_buckets.append({
                "instance": "srv-d",
                "job": "blackbox",
                "bucket_start": cur_t,
                "bucket_end": nxt_t,
                "uptime_seconds": 3564.0,  # 99.0%
                "downtime_seconds": 36.0,
                "unknown_seconds": 0.0,
                "coverage_seconds": 3600.0,
                "sample_count": 1800,
                "availability_pct": 99.0,
                "incident_count": 1,
                "avg_latency_ms": 11.0,
                "updated_at": now,
            })
            cur_t = nxt_t

        entry = merge_hybrid_target_availability(
            req_start=req_start,
            req_end=req_end,
            target_id="srv-d",
            target_name="srv-d",
            job="blackbox",
            sqlite_buckets=sqlite_buckets,
            prom_metrics={},
            expected_interval_sec=2.0,
        )

        expected_window = 30 * 86400.0
        self.assertAlmostEqual(entry["sqlite_seconds"], expected_window, delta=5.0)
        self.assertAlmostEqual(entry["prometheus_seconds"], 0.0, delta=1.0)
        self.assertAlmostEqual(entry["unknown_seconds"], 0.0, delta=1.0)
        self.assertAlmostEqual(entry["observed_seconds"], expected_window, delta=5.0)
        self.assertAlmostEqual(entry["coverage_percent"], 100.0, places=1)
        self.assertAlmostEqual(entry["availability_pct"], 99.0, places=1)
        self.assertEqual(entry["source"], "materialized")

    def test_e_overlap_removal_exact_duration(self):
        """Test E: SQLite bucket: 12:00 to 15:00. Prometheus telemetry: 14:00 to 16:00.
        Merged coverage must be exactly 4 hours (12:00 to 16:00), not 5 hours.
        Overlap removed is exactly 1 hour."""
        t_12 = 100000.0
        t_13 = t_12 + 3600.0
        t_14 = t_13 + 3600.0
        t_15 = t_14 + 3600.0
        t_16 = t_15 + 3600.0

        sqlite_buckets = [
            {"instance": "srv-e", "bucket_start": t_12, "bucket_end": t_13, "uptime_seconds": 3600.0, "downtime_seconds": 0.0, "coverage_seconds": 3600.0, "sample_count": 1800, "availability_pct": 100.0, "incident_count": 0, "avg_latency_ms": 10.0},
            {"instance": "srv-e", "bucket_start": t_13, "bucket_end": t_14, "uptime_seconds": 3600.0, "downtime_seconds": 0.0, "coverage_seconds": 3600.0, "sample_count": 1800, "availability_pct": 100.0, "incident_count": 0, "avg_latency_ms": 10.0},
            {"instance": "srv-e", "bucket_start": t_14, "bucket_end": t_15, "uptime_seconds": 3600.0, "downtime_seconds": 0.0, "coverage_seconds": 3600.0, "sample_count": 1800, "availability_pct": 100.0, "incident_count": 0, "avg_latency_ms": 10.0},
        ]

        prom_metrics = {
            "first_ts": t_14,
            "last_ts": t_16,
            "count": str(7200 // 2),
            "avail": "100.0",
            "incidents": "0",
            "duration": "10.0",
        }

        entry = merge_hybrid_target_availability(
            req_start=t_12,
            req_end=t_16,
            target_id="srv-e",
            target_name="srv-e",
            job="blackbox",
            sqlite_buckets=sqlite_buckets,
            prom_metrics=prom_metrics,
            expected_interval_sec=2.0,
        )

        expected_window = 4 * 3600.0  # 14,400s
        self.assertAlmostEqual(entry["observed_seconds"], expected_window, delta=2.0)
        self.assertAlmostEqual(entry["sqlite_seconds"], 2 * 3600.0, delta=2.0)
        self.assertAlmostEqual(entry["prometheus_seconds"], 2 * 3600.0, delta=2.0)
        self.assertAlmostEqual(entry["overlap_removed_seconds"], 1 * 3600.0, delta=2.0)
        self.assertAlmostEqual(entry["unknown_seconds"], 0.0, delta=2.0)

    def test_f_partial_boundary_clipping(self):
        """Test F: Requested window: 12:34 to 13:17 (43 minutes = 2580 seconds).
        SQLite has full hourly buckets for 12:00-13:00 and 13:00-14:00.
        Result: 12:34-13:00 (26 min) + 13:00-13:17 (17 min) = 43 minutes exact."""
        t_12 = 1700000000.0
        t_13 = t_12 + 3600.0
        t_14 = t_13 + 3600.0

        req_start = t_12 + (34 * 60.0)  # 12:34
        req_end = t_13 + (17 * 60.0)    # 13:17
        expected_dur = (26 + 17) * 60.0  # 43 min = 2580s

        b1 = {"instance": "srv-f", "bucket_start": t_12, "bucket_end": t_13, "uptime_seconds": 3600.0, "downtime_seconds": 0.0, "coverage_seconds": 3600.0, "sample_count": 1800, "availability_pct": 100.0, "incident_count": 0, "avg_latency_ms": 10.0}
        b2 = {"instance": "srv-f", "bucket_start": t_13, "bucket_end": t_14, "uptime_seconds": 3600.0, "downtime_seconds": 0.0, "coverage_seconds": 3600.0, "sample_count": 1800, "availability_pct": 100.0, "incident_count": 0, "avg_latency_ms": 10.0}

        clip1 = clip_hourly_bucket(b1, req_start, req_end)
        clip2 = clip_hourly_bucket(b2, req_start, req_end)

        self.assertIsNotNone(clip1)
        self.assertIsNotNone(clip2)
        self.assertAlmostEqual(clip1["coverage_seconds"], 26 * 60.0, delta=1.0)
        self.assertAlmostEqual(clip2["coverage_seconds"], 17 * 60.0, delta=1.0)
        self.assertAlmostEqual(clip1["coverage_seconds"] + clip2["coverage_seconds"], expected_dur, delta=1.0)

        entry = merge_hybrid_target_availability(
            req_start=req_start,
            req_end=req_end,
            target_id="srv-f",
            target_name="srv-f",
            job="blackbox",
            sqlite_buckets=[b1, b2],
            prom_metrics={},
            expected_interval_sec=2.0,
        )
        self.assertAlmostEqual(entry["observed_seconds"], expected_dur, delta=1.0)
        self.assertAlmostEqual(entry["unknown_seconds"], 0.0, delta=1.0)
        self.assertAlmostEqual(entry["coverage_percent"], 100.0, places=1)

    def test_g_telemetry_gap_within_prometheus(self):
        """Test G: Prometheus query returns samples where estimated cadence exceeds 3x expected.
        The excess gap is tracked as UNKNOWN."""
        t_start = 100000.0
        t_end = t_start + 3600.0  # 1 hour window

        # Only 10 samples observed across 3600s with 2s cadence expected -> gap exists
        prom_metrics = {
            "first_ts": t_start,
            "last_ts": t_end,
            "count": "10",  # 10 samples across 1 hour = 360s cadence >> 3 * 2s
            "avail": "100.0",
            "incidents": "0",
            "duration": "10.0",
        }

        entry = merge_hybrid_target_availability(
            req_start=t_start,
            req_end=t_end,
            target_id="srv-g",
            target_name="srv-g",
            job="blackbox",
            sqlite_buckets=[],
            prom_metrics=prom_metrics,
            expected_interval_sec=2.0,
            gap_tolerance=3.0,
        )

        # 10 samples * 2s = 20s coverage
        self.assertAlmostEqual(entry["observed_seconds"], 20.0, delta=1.0)
        self.assertAlmostEqual(entry["unknown_seconds"], 3600.0 - 20.0, delta=1.0)
        self.assertAlmostEqual(entry["observed_seconds"] + entry["unknown_seconds"], 3600.0, delta=1.0)

    def test_h_mixed_target_boundaries(self):
        """Test H: Target A Prometheus starts at Day -2, Target B Prometheus starts at Day -1.
        Hybrid merging operates independently per target without cross-target bleeding."""
        now = time.time()
        req_start = now - 7 * 86400.0
        req_end = now

        p_start_a = now - 2 * 86400.0
        p_start_b = now - 1 * 86400.0

        sqlite_a = []
        cur_t = req_start
        while cur_t < p_start_a:
            nxt_t = cur_t + 3600.0
            sqlite_a.append({"instance": "srv-h-a", "bucket_start": cur_t, "bucket_end": nxt_t, "uptime_seconds": 3600.0, "downtime_seconds": 0.0, "coverage_seconds": 3600.0, "sample_count": 1800, "availability_pct": 100.0, "incident_count": 0, "avg_latency_ms": 10.0})
            cur_t = nxt_t

        sqlite_b = []
        cur_t = req_start
        while cur_t < p_start_b:
            nxt_t = cur_t + 3600.0
            sqlite_b.append({"instance": "srv-h-b", "bucket_start": cur_t, "bucket_end": nxt_t, "uptime_seconds": 3600.0, "downtime_seconds": 0.0, "coverage_seconds": 3600.0, "sample_count": 1800, "availability_pct": 100.0, "incident_count": 0, "avg_latency_ms": 10.0})
            cur_t = nxt_t

        prom_map = {
            "first_ts": {"srv-h-a": str(p_start_a), "srv-h-b": str(p_start_b)},
            "last_ts": {"srv-h-a": str(req_end), "srv-h-b": str(req_end)},
            "count": {"srv-h-a": str(2 * 86400 // 2), "srv-h-b": str(1 * 86400 // 2)},
            "avail": {"srv-h-a": "100.0", "srv-h-b": "100.0"},
            "incidents": {"srv-h-a": "0", "srv-h-b": "0"},
            "duration": {"srv-h-a": "10.0", "srv-h-b": "10.0"},
        }

        entries, summary = merge_hybrid_fleet_availability(
            req_start=req_start,
            req_end=req_end,
            monitored_instances=["srv-h-a", "srv-h-b"],
            sqlite_buckets=sqlite_a + sqlite_b,
            prom_results_map=prom_map,
            expected_interval_sec=2.0,
        )

        entries_by_id = {e["id"]: e for e in entries}
        ea = entries_by_id["srv-h-a"]
        eb = entries_by_id["srv-h-b"]

        self.assertAlmostEqual(ea["sqlite_seconds"], 5 * 86400.0, delta=5.0)
        self.assertAlmostEqual(ea["prometheus_seconds"], 2 * 86400.0, delta=5.0)

        self.assertAlmostEqual(eb["sqlite_seconds"], 6 * 86400.0, delta=5.0)
        self.assertAlmostEqual(eb["prometheus_seconds"], 1 * 86400.0, delta=5.0)

        self.assertAlmostEqual(ea["observed_seconds"], 7 * 86400.0, delta=5.0)
        self.assertAlmostEqual(eb["observed_seconds"], 7 * 86400.0, delta=5.0)

    def test_i_corrupt_legacy_sqlite_rows_sanitization(self):
        """Test I: SQLite rows with coverage > 3600s, negative unknown, or inverted start/end timestamps.
        Clipping and validation sanitize without crashes or mathematical violations."""
        corrupt_bucket_1 = {
            "instance": "srv-i",
            "bucket_start": 1000.0,
            "bucket_end": 4600.0,
            "uptime_seconds": 5000.0,       # Corrupt: > bucket duration (3600s)
            "downtime_seconds": 2000.0,     # Corrupt
            "coverage_seconds": 7000.0,     # Corrupt
            "unknown_seconds": -3400.0,     # Corrupt: negative unknown
            "sample_count": 100,
            "availability_pct": 150.0,      # Corrupt: > 100%
            "incident_count": 0,
            "avg_latency_ms": 10.0,
        }

        clipped = clip_hourly_bucket(corrupt_bucket_1, 1000.0, 4600.0)
        self.assertIsNotNone(clipped)
        self.assertLessEqual(clipped["coverage_seconds"], 3600.0)
        self.assertGreaterEqual(clipped["unknown_seconds"], 0.0)
        self.assertAlmostEqual(clipped["coverage_seconds"] + clipped["unknown_seconds"], 3600.0, delta=0.1)
        self.assertLessEqual(clipped["availability_pct"], 100.0)

        corrupt_bucket_inverted = {
            "instance": "srv-i",
            "bucket_start": 5000.0,
            "bucket_end": 1000.0,  # Inverted
            "uptime_seconds": 3600.0,
            "coverage_seconds": 3600.0,
        }
        self.assertIsNone(clip_hourly_bucket(corrupt_bucket_inverted, 1000.0, 5000.0))

    def test_j_idempotent_aggregation(self):
        """Test J: Running aggregation cycles multiple times on the same time window
        is strictly idempotent and does not accumulate duplicate rows in SQLite."""
        now = time.time()
        h_start = (int(now // 3600) - 2) * 3600.0
        h_end = h_start + 3600.0

        bucket = {
            "instance": "srv-j",
            "job": "blackbox",
            "bucket_start": h_start,
            "bucket_end": h_end,
            "uptime_seconds": 3600.0,
            "downtime_seconds": 0.0,
            "unknown_seconds": 0.0,
            "coverage_seconds": 3600.0,
            "sample_count": 1800,
            "availability_pct": 100.0,
            "incident_count": 0,
            "avg_latency_ms": 10.0,
            "updated_at": now,
        }

        # Save once
        AvailabilityBucketRepository.save_buckets([bucket], db_path=self.db_path)
        count1 = AvailabilityBucketRepository.get_bucket_count_in_range("blackbox", h_start - 10, h_end + 10, db_path=self.db_path)
        self.assertEqual(count1, 1)

        # Save same hour again (idempotent overwrite)
        AvailabilityBucketRepository.save_buckets([bucket], db_path=self.db_path)
        count2 = AvailabilityBucketRepository.get_bucket_count_in_range("blackbox", h_start - 10, h_end + 10, db_path=self.db_path)
        self.assertEqual(count2, 1)

    def test_k_range_consistency(self):
        """Test K: 24h, 7d, and 30d queries return monotonically decreasing coverage %
        when Prometheus retention is ~59h and older historical data is missing in SQLite."""
        now = time.time()
        retention_sec = 59.6 * 3600.0
        prom_p_start = now - retention_sec

        def mock_query(expr, cache_ttl=5.0, timeout=None):
            if 'min_over_time(timestamp' in expr:
                return {'srv-k': str(prom_p_start)}
            if 'max_over_time(timestamp' in expr:
                return {'srv-k': str(now)}
            if 'count_over_time' in expr:
                return {'srv-k': str(int(retention_sec / 2.0))}
            if 'avg_over_time' in expr and 'probe_duration' not in expr:
                return {'srv-k': '99.5'}
            if 'changes(' in expr:
                return {'srv-k': '0'}
            if 'probe_duration_seconds' in expr:
                return {'srv-k': '10.0'}
            if expr in ('probe_success', 'up'):
                return {'srv-k': '1'}
            return {}

        with patch.object(alarm_app, 'get_monitored_instances', return_value=['srv-k']), \
             patch.object(alarm_app, 'load_json', return_value=[]), \
             patch.object(alarm_app, 'fetch_prom_query_map', side_effect=mock_query):

            # 24h
            alarm_app.clear_availability_cache()
            r_24h = self.client.get('/api/availability?minutes=1440')
            d_24h = json.loads(r_24h.data)

            # 7d
            alarm_app.clear_availability_cache()
            r_7d = self.client.get('/api/availability?minutes=10080')
            d_7d = json.loads(r_7d.data)

            # 30d
            alarm_app.clear_availability_cache()
            r_30d = self.client.get('/api/availability?minutes=43200')
            d_30d = json.loads(r_30d.data)

            cov_24h = d_24h["coverage_percent"]
            cov_7d = d_7d["coverage_percent"]
            cov_30d = d_30d["coverage_percent"]

            # 24h is within 59.6h retention -> ~100% coverage
            self.assertGreaterEqual(cov_24h, 95.0)
            # 7d (168h) -> ~59.6/168 ≈ 35.5%
            self.assertAlmostEqual(cov_7d, (59.6 / 168.0) * 100.0, delta=5.0)
            # 30d (720h) -> ~59.6/720 ≈ 8.3%
            self.assertAlmostEqual(cov_30d, (59.6 / 720.0) * 100.0, delta=2.0)

            # Monotonic decrease
            self.assertGreater(cov_24h, cov_7d)
            self.assertGreater(cov_7d, cov_30d)


class OngoingHourBucketTests(unittest.TestCase):
    """Regression: an in-progress hour is stored with a nominal hour end
    (bucket_start=13:00, bucket_end=14:00) but its coverage/uptime/downtime
    are only ever accumulated over the elapsed part [13:00, now]. Consuming
    code must treat coverage_seconds as an already-observed duration and not
    proportionally scale it a second time against the full nominal hour, nor
    turn the not-yet-elapsed remainder of the hour into "unknown"."""

    H13 = 1_700_000_000.0 - (1_700_000_000.0 % 3600.0)  # some exact hour
    H14 = H13 + 3600.0
    NOW_1320 = H13 + 20 * 60.0  # 13:20, 1200s into the hour

    def _bucket(self, **over):
        b = {
            "instance": "srv-ongoing",
            "job": "blackbox",
            "bucket_start": self.H13,
            "bucket_end": self.H14,       # nominal hour end, in the future
            "uptime_seconds": 1200.0,
            "downtime_seconds": 0.0,
            "unknown_seconds": 0.0,
            "coverage_seconds": 1200.0,
            "sample_count": 600,
            "availability_pct": 100.0,
            "incident_count": 0,
            "avg_latency_ms": 10.0,
        }
        b.update(over)
        return b

    def test_ongoing_bucket_coverage_not_double_scaled(self):
        clipped = clip_hourly_bucket(
            self._bucket(), self.H13 - 86400.0, self.NOW_1320, now=self.NOW_1320
        )
        self.assertIsNotNone(clipped)
        self.assertAlmostEqual(clipped["coverage_seconds"], 1200.0, delta=1.0)
        self.assertNotAlmostEqual(clipped["coverage_seconds"], 400.0, delta=50.0)
        self.assertAlmostEqual(clipped["uptime_seconds"], 1200.0, delta=1.0)
        # elapsed interval only; the future 2400s of the hour is not "unknown"
        self.assertAlmostEqual(clipped["unknown_seconds"], 0.0, delta=1.0)
        self.assertAlmostEqual(
            clipped["coverage_seconds"] + clipped["unknown_seconds"], 1200.0, delta=1.0
        )

    def test_ongoing_bucket_partial_telemetry_preserved(self):
        b = self._bucket(uptime_seconds=900.0, downtime_seconds=0.0,
                         coverage_seconds=900.0, unknown_seconds=300.0)
        clipped = clip_hourly_bucket(
            b, self.H13 - 86400.0, self.NOW_1320, now=self.NOW_1320
        )
        self.assertIsNotNone(clipped)
        self.assertAlmostEqual(clipped["uptime_seconds"], 900.0, delta=1.0)
        self.assertAlmostEqual(clipped["downtime_seconds"], 0.0, delta=1.0)
        self.assertAlmostEqual(clipped["unknown_seconds"], 300.0, delta=1.0)  # genuine gap
        self.assertAlmostEqual(
            clipped["coverage_seconds"] + clipped["unknown_seconds"], 1200.0, delta=1.0
        )

    def test_ongoing_bucket_with_downtime_split_preserved(self):
        b = self._bucket(uptime_seconds=1080.0, downtime_seconds=120.0,
                         coverage_seconds=1200.0, availability_pct=90.0,
                         incident_count=1)
        clipped = clip_hourly_bucket(
            b, self.H13 - 86400.0, self.NOW_1320, now=self.NOW_1320
        )
        self.assertAlmostEqual(clipped["uptime_seconds"], 1080.0, delta=1.0)
        self.assertAlmostEqual(clipped["downtime_seconds"], 120.0, delta=1.0)
        self.assertAlmostEqual(clipped["coverage_seconds"], 1200.0, delta=1.0)
        self.assertAlmostEqual(clipped["availability_pct"], 90.0, delta=0.5)

    def test_completed_bucket_unchanged_when_now_past_hour_end(self):
        b = self._bucket(uptime_seconds=3600.0, coverage_seconds=3600.0)
        for now in (self.H14, self.H14 + 5.0, self.H14 + 3600.0):
            clipped = clip_hourly_bucket(b, self.H13 - 86400.0, now, now=now)
            self.assertAlmostEqual(clipped["coverage_seconds"], 3600.0, delta=1.0)
            self.assertAlmostEqual(clipped["unknown_seconds"], 0.0, delta=1.0)

    def test_early_in_hour_only_elapsed_seconds_count(self):
        now = self.H13 + 5.0  # 13:00:05
        b = self._bucket(uptime_seconds=5.0, coverage_seconds=5.0)
        clipped = clip_hourly_bucket(b, self.H13 - 86400.0, now, now=now)
        self.assertIsNotNone(clipped)
        self.assertLessEqual(clipped["coverage_seconds"], 5.0 + 0.5)
        self.assertLessEqual(clipped["unknown_seconds"], 0.5)

    def test_backward_compatible_without_now(self):
        # No `now` -> old proportional behaviour against the nominal hour.
        b = self._bucket(uptime_seconds=3600.0, coverage_seconds=3600.0)
        clipped = clip_hourly_bucket(b, self.H13, self.NOW_1320)
        self.assertAlmostEqual(clipped["coverage_seconds"], 1200.0, delta=1.0)

    def test_ongoing_bucket_through_merge_hybrid_target(self):
        """End-to-end: fully-materialized path (no Prometheus), a 40-minute
        window ending mid-hour. The target's observed seconds must reflect
        the elapsed+observed time, not one third of it."""
        req_end = self.NOW_1320
        req_start = self.H13 - 20 * 60.0  # 12:40 -> 40 min window
        # completed 12:00-13:00 bucket + ongoing 13:00-14:00 bucket
        b_prev = {
            "instance": "srv-ongoing", "job": "blackbox",
            "bucket_start": self.H13 - 3600.0, "bucket_end": self.H13,
            "uptime_seconds": 3600.0, "downtime_seconds": 0.0,
            "unknown_seconds": 0.0, "coverage_seconds": 3600.0,
            "sample_count": 1800, "availability_pct": 100.0,
            "incident_count": 0, "avg_latency_ms": 10.0,
        }
        entry = merge_hybrid_target_availability(
            req_start=req_start,
            req_end=req_end,
            target_id="srv-ongoing",
            target_name="srv-ongoing",
            job="blackbox",
            sqlite_buckets=[b_prev, self._bucket()],
            prom_metrics={},
            expected_interval_sec=2.0,
        )
        # 20 min from the completed hour + 20 min elapsed from the ongoing hour
        self.assertAlmostEqual(entry["observed_seconds"], 40 * 60.0, delta=5.0)
        self.assertAlmostEqual(entry["unknown_seconds"], 0.0, delta=5.0)
        self.assertAlmostEqual(entry["coverage_percent"], 100.0, places=1)


class BackfillWindowAlignmentTests(unittest.TestCase):
    """Regression: 6h backfill windows must sit on real hour boundaries so
    the per-window hourly loop never has two windows fighting over one
    hourly bucket (which leaves part of that hour unaggregated)."""

    def _windows(self, now, latest_end=None):
        return alarm_app._availability_aggregation_windows(now, latest_end)

    def test_backfill_boundaries_are_hour_aligned(self):
        now = 1_700_000_000.0 + 13 * 3600.0 + 16 * 60.0 + 14.0  # ...13:16:14
        windows = self._windows(now, latest_end=None)
        self.assertGreater(len(windows), 1)
        # every boundary except the final `now` lands exactly on an hour
        for i, (w_start, w_end) in enumerate(windows):
            self.assertAlmostEqual(w_start % 3600.0, 0.0, delta=1e-6)
            if i < len(windows) - 1:
                self.assertAlmostEqual(w_end % 3600.0, 0.0, delta=1e-6)
        self.assertEqual(windows[-1][1], now)

    def test_backfill_no_offset_boundaries(self):
        now = 1_700_000_000.0 + 13 * 3600.0 + 16 * 60.0 + 14.0
        windows = self._windows(now, latest_end=None)
        bad = now - (now % 3600.0) - 6 * 3600.0  # e.g. 07:16:14-style anchor
        starts = [w[0] for w in windows]
        self.assertNotIn(now, starts[1:])          # no unaligned interior start
        self.assertNotIn(now - 6 * 3600.0, starts)  # not the raw now-6h either
        self.assertAlmostEqual(min(starts) % 3600.0, 0.0, delta=1e-6)

    def test_backfill_contiguous_no_gaps_or_overlap(self):
        now = 1_700_000_000.0 + 5 * 3600.0 + 47 * 60.0 + 9.0
        windows = self._windows(now, latest_end=None)
        for (a_start, a_end), (b_start, b_end) in zip(windows, windows[1:]):
            self.assertEqual(a_end, b_start)  # no gap, no overlap
        self.assertLessEqual(windows[0][0], now - 86400 * 7)
        self.assertEqual(windows[-1][1], now)

    def test_backfill_covers_full_seven_days(self):
        now = 1_700_000_000.0 + 9 * 3600.0 + 3 * 60.0
        windows = self._windows(now, latest_end=None)
        self.assertEqual(windows[0][0], math.floor(now / 3600.0) * 3600.0 - 86400 * 7)

    def test_steady_state_windows_hour_aligned(self):
        now = 1_700_000_000.0 + 20 * 3600.0 + 40 * 60.0  # mid-hour, fresh store
        fresh_latest_end = now - 120.0
        windows = self._windows(now, latest_end=fresh_latest_end)
        hour_end = math.floor(now / 3600.0) * 3600.0
        self.assertEqual(windows[0], (hour_end - 3600.0, hour_end))
        self.assertEqual(windows[1], (hour_end, now))  # in-progress hour, up to now

    def test_steady_state_on_exact_hour_skips_partial(self):
        now = math.floor((1_700_000_000.0 + 20 * 3600.0) / 3600.0) * 3600.0
        windows = self._windows(now, latest_end=now - 120.0)
        self.assertEqual(len(windows), 1)  # no 0-length in-progress window


class BucketInvariantSweep(unittest.TestCase):
    """Phase 1: pin the BUCKET & MERGE INVARIANTS (see fleet_availability
    module docstring, I1-I6) across a matrix of bucket shapes and clip
    positions. Value-level correctness is covered by the tests above; this
    is the structural guard — a future edit that breaks
    uptime+downtime==coverage, lets coverage exceed elapsed time, or turns
    future time into "unknown" must fail here regardless of the numbers."""

    H = 1_700_000_000.0 - (1_700_000_000.0 % 3600.0)  # an exact hour boundary
    EPS = 0.06

    def _bucket(self, start, end, up, down, cov=None, unk=0.0):
        return {
            "instance": "srv", "job": "blackbox",
            "bucket_start": start, "bucket_end": end,
            "uptime_seconds": up, "downtime_seconds": down,
            "coverage_seconds": (up + down) if cov is None else cov,
            "unknown_seconds": unk,
            "sample_count": 100, "availability_pct": (100.0 * up / (up + down)) if (up + down) > 0 else None,
            "incident_count": 1 if down > 0 else 0, "avg_latency_ms": 10.0,
        }

    def _assert_clip_invariants(self, clipped, clip_end, now):
        if clipped is None:
            return
        up = clipped["uptime_seconds"]
        down = clipped["downtime_seconds"]
        cov = clipped["coverage_seconds"]
        unk = clipped["unknown_seconds"]
        dur = clipped["duration_seconds"]
        # I1 uptime + downtime == coverage
        self.assertAlmostEqual(up + down, cov, delta=self.EPS, msg=f"I1 {clipped}")
        # I2 coverage <= duration
        self.assertLessEqual(cov, dur + self.EPS, msg=f"I2 {clipped}")
        # I3 no future: the clipped interval ends at/before now (and clip_end)
        self.assertLessEqual(clipped["bucket_end"], min(clip_end, now) + self.EPS, msg=f"I3 {clipped}")
        # I4 unknown == duration - coverage, non-negative
        self.assertGreaterEqual(unk, -self.EPS, msg=f"I4 {clipped}")
        self.assertAlmostEqual(unk, max(0.0, dur - cov), delta=self.EPS, msg=f"I4 {clipped}")
        # I5 availability_pct None iff coverage 0, else 0..100
        ap = clipped["availability_pct"]
        if cov <= 0:
            self.assertIsNone(ap, msg=f"I5 {clipped}")
        else:
            self.assertIsNotNone(ap)
            self.assertGreaterEqual(ap, 0.0)
            self.assertLessEqual(ap, 100.0)
            self.assertAlmostEqual(ap, 100.0 * up / cov, delta=0.5, msg=f"I5 {clipped}")

    def test_clip_invariants_matrix(self):
        H, HH = self.H, self.H + 3600.0
        far = H - 30 * 86400.0
        cases = [
            # (label, bucket, now)
            ("completed full-up",      self._bucket(H, HH, 3600.0, 0.0),           HH + 10.0),
            ("completed with downtime", self._bucket(H, HH, 3000.0, 600.0),        HH + 10.0),
            ("completed gappy",        self._bucket(H, HH, 2400.0, 0.0, cov=2400.0, unk=1200.0), HH + 10.0),
            ("ongoing 20m full-up",    self._bucket(H, HH, 1200.0, 0.0),           H + 1200.0),
            ("ongoing 20m partial tel", self._bucket(H, HH, 900.0, 0.0, cov=900.0, unk=300.0),   H + 1200.0),
            ("ongoing 20m w/ downtime", self._bucket(H, HH, 1080.0, 120.0),        H + 1200.0),
            ("ongoing 5s early",       self._bucket(H, HH, 5.0, 0.0),              H + 5.0),
            ("on exact hour boundary", self._bucket(H, HH, 3600.0, 0.0),          HH),
            ("zero-data bucket",       self._bucket(H, HH, 0.0, 0.0, cov=0.0, unk=3600.0),       HH + 10.0),
        ]
        clip_windows = [
            ("full range", far, H + 4000.0),
            ("tight to now-ish", far, H + 1200.0),
            ("sub-interval [H+5m, H+15m]", H + 300.0, H + 900.0),
            ("starts after bucket", HH + 100.0, HH + 200.0),  # -> None
        ]
        for blabel, bucket, now in cases:
            for wlabel, cs, ce in clip_windows:
                clipped = clip_hourly_bucket(bucket, cs, ce, now=now)
                with self.subTest(bucket=blabel, window=wlabel):
                    self._assert_clip_invariants(clipped, ce, now)

    def test_merge_target_invariants(self):
        """merge_hybrid_target_availability output honours I1-I6 for a
        window that ends mid-hour (ongoing bucket in play), fully-materialized
        path (no Prometheus)."""
        H, HH = self.H, self.H + 3600.0
        now = H + 1500.0  # 25 min into the hour
        req_start = H - 6 * 3600.0
        req_end = now
        buckets = [
            self._bucket(H - i * 3600.0, H - (i - 1) * 3600.0, 3600.0, 0.0)
            for i in range(1, 7)
        ] + [self._bucket(H, HH, 1200.0, 0.0, cov=1200.0, unk=300.0)]  # ongoing, partial telemetry

        e = merge_hybrid_target_availability(
            req_start=req_start, req_end=req_end,
            target_id="srv", target_name="srv", job="blackbox",
            sqlite_buckets=buckets, prom_metrics={}, expected_interval_sec=2.0,
        )
        win = req_end - req_start
        cov = e["observed_seconds"]
        up = e["uptime_seconds"]
        down = e["downtime_seconds"]
        unk = e["unknown_seconds"]
        self.assertAlmostEqual(up + down, cov, delta=1.0)                 # I1
        self.assertLessEqual(cov, win + 1.0)                             # I2 / I3 (req_end == now)
        self.assertAlmostEqual(unk, max(0.0, win - cov), delta=1.0)      # I4
        self.assertAlmostEqual(e["coverage_percent"] + e["unknown_percent"], 100.0, delta=0.1)  # I6
        ap = e["availability_pct"]                                        # I5
        self.assertIsNotNone(ap)
        self.assertGreaterEqual(ap, 0.0)
        self.assertLessEqual(ap, 100.0)
        # 6 completed hours (21600s) + 1200s elapsed-and-observed from the
        # ongoing hour = 22800s; the ongoing hour's 300s gap is genuine unknown.
        self.assertAlmostEqual(cov, 6 * 3600.0 + 1200.0, delta=5.0)


from fleet_availability import maintenance_overlap_seconds, sla_budget  # noqa: E402


class SlaBudgetTests(unittest.TestCase):
    """Phase 4: SLA error-budget accounting. allowed downtime =
    (1 - target/100) * period — the idn.id table, computed."""

    MONTH = 30 * 86400.0

    def test_matches_the_article_budget_table(self):
        # 99.9% / 30d -> ~43.2 min ; 99% -> ~7.2h ; 99.99% -> ~4.32 min
        self.assertAlmostEqual(sla_budget(0, self.MONTH, 99.9, self.MONTH)["window"]["allowed_downtime_seconds"], 2592.0, delta=1)
        self.assertAlmostEqual(sla_budget(0, self.MONTH, 99.0, self.MONTH)["window"]["allowed_downtime_seconds"], 25920.0, delta=1)
        self.assertAlmostEqual(sla_budget(0, self.MONTH, 99.99, self.MONTH)["window"]["allowed_downtime_seconds"], 259.2, delta=0.5)

    def test_within_budget(self):
        b = sla_budget(1000.0, self.MONTH, 99.9, self.MONTH)
        w = b["window"]
        self.assertFalse(w["breached"])
        self.assertAlmostEqual(w["remaining_seconds"], 2592.0 - 1000.0, delta=1)
        self.assertAlmostEqual(w["used_percent"], round(1000.0 / 2592.0 * 100.0, 1), delta=0.2)
        self.assertFalse(b["projected"]["breach"])

    def test_over_budget_and_projection(self):
        # 24h window, 100s downtime, target 99.9 -> 24h budget is 86.4s
        b = sla_budget(100.0, 86400.0, 99.9, 86400.0, project_days=30)
        self.assertTrue(b["window"]["breached"])
        self.assertLess(b["window"]["remaining_seconds"], 0.0)
        # rate 100/86400 over 30d = 3000s vs 2592s budget -> projected breach
        self.assertAlmostEqual(b["projected"]["downtime_seconds"], 3000.0, delta=5.0)
        self.assertTrue(b["projected"]["breach"])

    def test_healthy_rate_projects_clean(self):
        # 7d window, 60s downtime, target 99.9 -> rate stays well under budget
        b = sla_budget(60.0, 7 * 86400.0, 99.9, 7 * 86400.0, project_days=30)
        self.assertFalse(b["window"]["breached"])
        self.assertFalse(b["projected"]["breach"])

    def test_no_data(self):
        b = sla_budget(0.0, 0.0, 99.9, 86400.0)
        self.assertFalse(b["has_data"])
        self.assertEqual(b["window"]["observed_downtime_seconds"], 0.0)

    def test_target_100_any_downtime_breaches(self):
        self.assertFalse(sla_budget(0.0, 86400.0, 100.0, 86400.0)["window"]["breached"])
        self.assertTrue(sla_budget(5.0, 86400.0, 100.0, 86400.0)["window"]["breached"])

    def test_window_defaults_to_observed(self):
        b = sla_budget(50.0, 3600.0, 99.0, None)  # window omitted -> 3600s
        self.assertAlmostEqual(b["window"]["seconds"], 3600.0, delta=0.1)
        self.assertAlmostEqual(b["window"]["allowed_downtime_seconds"], 36.0, delta=0.5)


class PerTargetSlaTargetTests(unittest.TestCase):
    """Phase 4.5: a per-target SLA target overrides the fleet default for that
    target's compliance status and error budget."""

    H = 1_700_000_000.0 - (1_700_000_000.0 % 3600.0)

    def _buckets(self, inst):
        # 24h, ~99.5% up: one hour is 3168s up / 432s down
        out = []
        for i in range(24):
            s = self.H + i * 3600.0
            up, down = (3168.0, 432.0) if i == 3 else (3600.0, 0.0)
            out.append({
                "instance": inst, "job": "blackbox",
                "bucket_start": s, "bucket_end": s + 3600.0,
                "uptime_seconds": up, "downtime_seconds": down,
                "coverage_seconds": up + down, "unknown_seconds": 0.0,
                "sample_count": 240, "availability_pct": round(up / (up + down) * 100.0, 2),
                "incident_count": 1 if down else 0, "avg_latency_ms": 10.0,
            })
        return out

    def test_per_target_threshold_changes_compliance(self):
        entries, summary = merge_hybrid_fleet_availability(
            req_start=self.H, req_end=self.H + 24 * 3600.0,
            monitored_instances=["a", "b"],
            sqlite_buckets=self._buckets("a") + self._buckets("b"),
            prom_results_map={}, expected_interval_sec=2.0,
            sla_threshold=99.9,
            sla_threshold_by_instance={"b": 99.0},   # b has a looser SLA
        )
        by_id = {e["id"]: e for e in summary["per_server"]["values"]}
        self.assertAlmostEqual(by_id["a"]["availability_pct"], 99.5, delta=0.1)
        self.assertAlmostEqual(by_id["b"]["availability_pct"], 99.5, delta=0.1)
        # a measured against 99.9 -> NON_COMPLIANT ; b against 99.0 -> COMPLIANT
        self.assertEqual(by_id["a"]["sla_status"], "NON_COMPLIANT")
        self.assertEqual(by_id["a"]["sla_target_pct"], 99.9)
        self.assertEqual(by_id["b"]["sla_status"], "COMPLIANT")
        self.assertEqual(by_id["b"]["sla_target_pct"], 99.0)


class IncidentDedupTests(unittest.TestCase):
    """Phase 2.5: one outage straddling an hour boundary is counted once per
    hourly bucket by the per-hour reconstruction. Where a fully-contained
    bucket ends still-in-outage and the next contiguous bucket begins
    still-in-outage, merge_hybrid_target_availability drops the duplicate."""

    H = 1_700_000_000.0 - (1_700_000_000.0 % 3600.0)

    def _bucket(self, i, inc, oj, up=3300.0, down=300.0):
        s = self.H + i * 3600.0
        return {
            "instance": "srv-d", "job": "blackbox",
            "bucket_start": s, "bucket_end": s + 3600.0,
            "uptime_seconds": up, "downtime_seconds": down,
            "coverage_seconds": up + down, "unknown_seconds": 0.0,
            "sample_count": 240,
            "availability_pct": round(up / (up + down) * 100.0, 2),
            "incident_count": inc, "avg_latency_ms": 10.0,
            "outage_json": oj,
        }

    def _merge(self, buckets):
        return merge_hybrid_target_availability(
            req_start=self.H, req_end=self.H + 3 * 3600.0,
            target_id="srv-d", target_name="srv-d", job="blackbox",
            sqlite_buckets=buckets, prom_metrics={}, expected_interval_sec=2.0,
        )

    def test_straddling_outage_counted_once(self):
        a = self._bucket(0, 1, {"d": [900.0], "ongoing_start": False, "ongoing_end": True})
        b = self._bucket(1, 1, {"d": [600.0], "ongoing_start": True, "ongoing_end": False})
        e = self._merge([a, b])
        self.assertEqual(e["incidents"], 1)
        # downtime still sums normally — dedup only touches the incident count
        self.assertAlmostEqual(e["downtime_seconds"], 600.0, delta=1.0)

    def test_two_separate_outages_not_merged(self):
        a = self._bucket(0, 1, {"d": [300.0], "ongoing_start": False, "ongoing_end": False})
        b = self._bucket(1, 1, {"d": [300.0], "ongoing_start": False, "ongoing_end": False})
        self.assertEqual(self._merge([a, b])["incidents"], 2)

    def test_non_contiguous_buckets_not_merged(self):
        a = self._bucket(0, 1, {"d": [900.0], "ongoing_start": False, "ongoing_end": True})
        c = self._bucket(2, 1, {"d": [600.0], "ongoing_start": True, "ongoing_end": False})  # hour 1 missing
        self.assertEqual(self._merge([a, c])["incidents"], 2)

    def test_legacy_buckets_without_outage_json_unchanged(self):
        a = self._bucket(0, 1, None)
        b = self._bucket(1, 1, None)
        self.assertEqual(self._merge([a, b])["incidents"], 2)

    def test_outage_json_as_string_is_parsed(self):
        a = self._bucket(0, 1, json.dumps({"d": [900.0], "ongoing_start": False, "ongoing_end": True}))
        b = self._bucket(1, 1, json.dumps({"d": [600.0], "ongoing_start": True, "ongoing_end": False}))
        self.assertEqual(self._merge([a, b])["incidents"], 1)

    def test_three_hour_straddle_counted_once(self):
        a = self._bucket(0, 1, {"d": [600.0], "ongoing_start": False, "ongoing_end": True})
        b = self._bucket(1, 1, {"d": [3600.0], "ongoing_start": True, "ongoing_end": True}, up=0.0, down=3600.0)
        c = self._bucket(2, 1, {"d": [300.0], "ongoing_start": True, "ongoing_end": False})
        self.assertEqual(self._merge([a, b, c])["incidents"], 1)


class MaintenanceExclusionTests(unittest.TestCase):
    """Phase 3: planned-downtime windows are carved out of the SLA
    denominator. `availability_pct` stays raw; `availability_pct_excl_maintenance`
    and the SLA compliance status exclude the maintenance-covered downtime."""

    H = 1_700_000_000.0 - (1_700_000_000.0 % 3600.0)

    def test_overlap_union_and_clip(self):
        f = maintenance_overlap_seconds
        self.assertEqual(f(None, 0, 100), 0.0)
        self.assertEqual(f([], 0, 100), 0.0)
        self.assertEqual(f([(10, 40)], 0, 100), 30.0)
        self.assertEqual(f([(10, 40)], 20, 100), 20.0)          # clipped to req_start
        self.assertEqual(f([(10, 40)], 0, 25), 15.0)            # clipped to req_end
        self.assertEqual(f([(10, 40), (30, 60)], 0, 100), 50.0)  # overlapping -> union
        self.assertEqual(f([(10, 20), (50, 70)], 0, 100), 30.0)  # disjoint -> sum
        self.assertEqual(f([(10, 90), (30, 40)], 0, 100), 80.0)  # nested
        self.assertEqual(f([(40, 10)], 0, 100), 0.0)             # reversed -> ignored

    def _day_buckets(self, bad_hour_index):
        """24 hourly buckets, all 100% up except hour `bad_hour_index` which
        is 1800s up / 1800s down (one real outage)."""
        out = []
        for i in range(24):
            s = self.H + i * 3600.0
            if i == bad_hour_index:
                up, down = 1800.0, 1800.0
            else:
                up, down = 3600.0, 0.0
            out.append({
                "instance": "srv-m", "job": "blackbox",
                "bucket_start": s, "bucket_end": s + 3600.0,
                "uptime_seconds": up, "downtime_seconds": down,
                "coverage_seconds": up + down, "unknown_seconds": 0.0,
                "sample_count": 240, "availability_pct": round(up / (up + down) * 100.0, 2),
                "incident_count": 1 if down else 0, "avg_latency_ms": 10.0,
            })
        return out

    def _merge(self, maintenance_windows):
        return merge_hybrid_target_availability(
            req_start=self.H, req_end=self.H + 24 * 3600.0,
            target_id="srv-m", target_name="srv-m", job="blackbox",
            sqlite_buckets=self._day_buckets(5), prom_metrics={},
            expected_interval_sec=2.0, maintenance_windows=maintenance_windows,
        )

    def test_no_windows_is_a_no_op(self):
        e = self._merge(None)
        self.assertAlmostEqual(e["availability_pct"], 97.92, delta=0.1)
        self.assertEqual(e["availability_pct_excl_maintenance"], e["availability_pct"])
        self.assertEqual(e["maintenance_excluded_seconds"], 0.0)
        self.assertEqual(e["sla_status"], "NON_COMPLIANT")   # 97.92 < 99.9

    def test_window_over_the_outage_hour_excuses_it(self):
        bad_start = self.H + 5 * 3600.0
        e = self._merge([(bad_start, bad_start + 3600.0)])
        # raw availability unchanged
        self.assertAlmostEqual(e["availability_pct"], 97.92, delta=0.1)
        # SLA view: the planned hour is carved out -> ~100%
        self.assertAlmostEqual(e["availability_pct_excl_maintenance"], 100.0, delta=0.1)
        self.assertAlmostEqual(e["maintenance_excluded_seconds"], 3600.0, delta=5.0)
        self.assertEqual(e["sla_status"], "COMPLIANT")
        self.assertEqual(e["sla_compliant"], True)
        self.assertAlmostEqual(e["sla_downtime_seconds"], 0.0, delta=1.0)
        # invariants still hold on the SLA trio
        self.assertAlmostEqual(e["sla_uptime_seconds"] + e["sla_downtime_seconds"],
                               e["sla_observed_seconds"], delta=1.0)

    def test_window_elsewhere_does_not_help(self):
        e = self._merge([(self.H + 20 * 3600.0, self.H + 21 * 3600.0)])  # a healthy hour
        self.assertAlmostEqual(e["availability_pct_excl_maintenance"], e["availability_pct"], delta=0.2)
        self.assertEqual(e["sla_status"], "NON_COMPLIANT")   # outage still counts

    def test_fleet_aggregate_uses_maintenance_excluded(self):
        bad_start = self.H + 5 * 3600.0
        buckets = self._day_buckets(5)
        entries, summary = merge_hybrid_fleet_availability(
            req_start=self.H, req_end=self.H + 24 * 3600.0,
            monitored_instances=["srv-m"], sqlite_buckets=buckets,
            prom_results_map={}, expected_interval_sec=2.0,
            maintenance_by_instance={"srv-m": [(bad_start, bad_start + 3600.0)]},
        )
        self.assertAlmostEqual(summary["fleet_aggregate"]["value"], 100.0, delta=0.1)
        self.assertGreater(summary["hybrid"]["maintenance_excluded_seconds"], 3000.0)
        # without the window the fleet aggregate reflects the real outage
        entries2, summary2 = merge_hybrid_fleet_availability(
            req_start=self.H, req_end=self.H + 24 * 3600.0,
            monitored_instances=["srv-m"], sqlite_buckets=buckets,
            prom_results_map={}, expected_interval_sec=2.0, maintenance_by_instance=None,
        )
        self.assertAlmostEqual(summary2["fleet_aggregate"]["value"], 97.92, delta=0.2)


if __name__ == '__main__':
    unittest.main()
