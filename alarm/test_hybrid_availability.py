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


if __name__ == '__main__':
    unittest.main()
