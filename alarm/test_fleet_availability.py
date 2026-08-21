import unittest
import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))

try:
    from fleet_availability import calculate_fleet_availability, summarize_entries, reconstruct_time_series_intervals
except ImportError:
    from alarm.fleet_availability import calculate_fleet_availability, summarize_entries, reconstruct_time_series_intervals

NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)
DAY = 24 * 60
PERIOD_7D = 7 * DAY  # 10080 minutes


class FleetAvailabilityTests(unittest.TestCase):

    def test_perfect_uptime_server(self):
        servers = [{"id": "s1", "name": "web-01", "downtime_minutes": 0,
                    "created_at": "2026-01-01T00:00:00Z"}]
        result = calculate_fleet_availability(servers, PERIOD_7D, now=NOW)
        s1 = result["per_server"]["values"][0]
        self.assertEqual(s1["availability_pct"], 100.0)
        self.assertEqual(result["fleet_average"]["value"], 100.0)
        self.assertEqual(result["fleet_aggregate"]["value"], 100.0)
        self.assertEqual(result["health_ratio"]["value"], 100.0)

    def test_full_downtime_server(self):
        servers = [{"id": "s1", "name": "web-01", "downtime_minutes": PERIOD_7D,
                    "created_at": "2026-01-01T00:00:00Z"}]
        result = calculate_fleet_availability(servers, PERIOD_7D, now=NOW)
        s1 = result["per_server"]["values"][0]
        self.assertEqual(s1["availability_pct"], 0.0)
        self.assertEqual(result["health_ratio"]["value"], 0.0)

    def test_known_percentage_formula(self):
        # 60 min downtime over a 10080 min period -> 99.40...%
        servers = [{"id": "s1", "name": "web-01", "downtime_minutes": 60,
                    "created_at": "2026-01-01T00:00:00Z"}]
        result = calculate_fleet_availability(servers, PERIOD_7D, now=NOW)
        expected = round((PERIOD_7D - 60) / PERIOD_7D * 100, 2)
        self.assertEqual(result["per_server"]["values"][0]["availability_pct"], expected)

    def test_new_server_uses_age_as_denominator(self):
        # created 2 days ago -> denominator should be 2 days, not the full 7-day period
        servers = [{"id": "s1", "name": "new-01", "downtime_minutes": 60,
                    "created_at": "2026-07-28T00:00:00Z"}]
        result = calculate_fleet_availability(servers, PERIOD_7D, now=NOW)
        s1 = result["per_server"]["values"][0]
        self.assertEqual(s1["denominator_minutes"], 2 * DAY)
        expected = round((2 * DAY - 60) / (2 * DAY) * 100, 2)
        self.assertEqual(s1["availability_pct"], expected)

    def test_brand_new_server_zero_age(self):
        # created exactly "now" -> denominator 0, no downtime yet -> 100%
        servers = [{"id": "s1", "name": "brand-new", "downtime_minutes": 0, "created_at": NOW}]
        result = calculate_fleet_availability(servers, PERIOD_7D, now=NOW)
        self.assertEqual(result["per_server"]["values"][0]["availability_pct"], 100.0)

    def test_fleet_average_vs_aggregate_diverge(self):
        # One old server mostly down (small weight isn't a factor here since
        # both are full-period, so use a new low-downtime server + an old
        # heavily-down server to show weighting differs from simple averaging).
        servers = [
            {"id": "old", "name": "old-01", "downtime_minutes": PERIOD_7D,  # 0% avail, full period
             "created_at": "2026-01-01T00:00:00Z"},
            {"id": "new", "name": "new-01", "downtime_minutes": 0,  # 100% avail, only 1 day old
             "created_at": "2026-07-29T00:00:00Z"},
        ]
        result = calculate_fleet_availability(servers, PERIOD_7D, now=NOW)
        # unweighted average of [0, 100] = 50
        self.assertEqual(result["fleet_average"]["value"], 50.0)
        # weighted: total_capacity = 10080 + 1440 = 11520, total_downtime = 10080
        expected_aggregate = round((11520 - 10080) / 11520 * 100, 2)
        self.assertEqual(result["fleet_aggregate"]["value"], expected_aggregate)
        self.assertNotEqual(result["fleet_average"]["value"], result["fleet_aggregate"]["value"])

    def test_health_ratio_counts_only_never_down_servers(self):
        servers = [
            {"id": "s1", "name": "a", "downtime_minutes": 0, "created_at": "2026-01-01T00:00:00Z"},
            {"id": "s2", "name": "b", "downtime_minutes": 5, "created_at": "2026-01-01T00:00:00Z"},
            {"id": "s3", "name": "c", "downtime_minutes": 0, "created_at": "2026-01-01T00:00:00Z"},
            {"id": "s4", "name": "d", "downtime_minutes": 100, "created_at": "2026-01-01T00:00:00Z"},
        ]
        result = calculate_fleet_availability(servers, PERIOD_7D, now=NOW)
        # 2 of 4 servers never went down -> 50%
        self.assertEqual(result["health_ratio"]["value"], 50.0)

    def test_downtime_exceeding_denominator_is_clamped_not_negative(self):
        # Bad/inconsistent data: reported downtime longer than the window itself
        servers = [{"id": "s1", "name": "bad-data", "downtime_minutes": PERIOD_7D * 2,
                    "created_at": "2026-01-01T00:00:00Z"}]
        result = calculate_fleet_availability(servers, PERIOD_7D, now=NOW)
        self.assertEqual(result["per_server"]["values"][0]["availability_pct"], 0.0)
        self.assertGreaterEqual(result["fleet_aggregate"]["value"], 0.0)

    def test_empty_fleet_returns_none_metrics_not_error(self):
        result = calculate_fleet_availability([], PERIOD_7D, now=NOW)
        self.assertEqual(result["server_count"], 0)
        self.assertEqual(result["per_server"]["values"], [])
        self.assertIsNone(result["fleet_average"]["value"])
        self.assertIsNone(result["fleet_aggregate"]["value"])
        self.assertIsNone(result["health_ratio"]["value"])

    def test_created_at_accepts_epoch_seconds_and_iso_string_equally(self):
        epoch_ts = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
        servers_epoch = [{"id": "s1", "name": "a", "downtime_minutes": 60, "created_at": epoch_ts}]
        servers_iso = [{"id": "s1", "name": "a", "downtime_minutes": 60, "created_at": "2026-01-01T00:00:00Z"}]
        r1 = calculate_fleet_availability(servers_epoch, PERIOD_7D, now=NOW)
        r2 = calculate_fleet_availability(servers_iso, PERIOD_7D, now=NOW)
        self.assertEqual(
            r1["per_server"]["values"][0]["availability_pct"],
            r2["per_server"]["values"][0]["availability_pct"],
        )

    def test_sla_compliance_ratio(self):
        # 10080 min period:
        # s1: 0 downtime -> 100% (>=99.9% -> compliant)
        # s2: 5 min downtime -> (10080-5)/10080 = 99.95% (>=99.9% -> compliant)
        # s3: 60 min downtime -> (10080-60)/10080 = 99.40% (<99.9% -> non-compliant)
        # s4: 1000 min downtime -> (<99.9% -> non-compliant)
        servers = [
            {"id": "s1", "name": "a", "downtime_minutes": 0, "created_at": "2026-01-01T00:00:00Z"},
            {"id": "s2", "name": "b", "downtime_minutes": 5, "created_at": "2026-01-01T00:00:00Z"},
            {"id": "s3", "name": "c", "downtime_minutes": 60, "created_at": "2026-01-01T00:00:00Z"},
            {"id": "s4", "name": "d", "downtime_minutes": 1000, "created_at": "2026-01-01T00:00:00Z"},
        ]
        result = calculate_fleet_availability(servers, PERIOD_7D, now=NOW)
        # 2 compliant out of 4 -> 50.0%
        self.assertEqual(result["sla_compliance_ratio"]["value"], 50.0)
        self.assertEqual(result["sla_compliance"]["value"], 50.0)
        self.assertEqual(result["zero_downtime_ratio"]["value"], 25.0)  # only s1 has 0 downtime

    def test_mathematical_invariants(self):
        # Window: 1440 minutes (24h)
        window = 1440.0
        entries = [
            {"id": "s1", "name": "normal", "availability_pct": 98.5, "denominator_minutes": 1440.0, "downtime_minutes": 21.6, "incidents": 2},
            {"id": "s2", "name": "partial-new", "availability_pct": 100.0, "denominator_minutes": 360.0, "downtime_minutes": 0.0, "incidents": 0},
            {"id": "s3", "name": "heavy-down", "availability_pct": 20.0, "denominator_minutes": 1000.0, "downtime_minutes": 800.0, "incidents": 4},
            {"id": "s4", "name": "zero-coverage", "availability_pct": None, "denominator_minutes": 0.0, "downtime_minutes": 0.0, "incidents": 0},
        ]
        result = summarize_entries(entries, window, min_sla_coverage_pct=50.0)

        for s in result["per_server"]["values"]:
            cov = s["coverage_minutes"]
            up = s["uptime_minutes"]
            down = s["downtime_minutes"]
            unk = s["unknown_minutes"]
            cov_pct = s["coverage_pct"]
            unk_pct = s["unknown_pct"]
            avail = s["availability_pct"]

            # Invariant 1: Coverage = Uptime + Downtime
            self.assertAlmostEqual(cov, up + down, places=2)
            # Invariant 2: Requested Window = Coverage + Unknown
            self.assertAlmostEqual(window, cov + unk, places=2)
            # Invariant 3: Coverage % + Unknown % = 100 %
            self.assertAlmostEqual(cov_pct + unk_pct, 100.0, places=2)
            # Invariant 4: Bounds
            if avail is not None:
                self.assertTrue(0.0 <= avail <= 100.0)
            self.assertTrue(0.0 <= cov_pct <= 100.0)
            self.assertTrue(0.0 <= unk_pct <= 100.0)

        # Invariant 5: Fleet Aggregate = Total Uptime / Total Coverage * 100
        total_up = sum(s["uptime_minutes"] for s in result["per_server"]["values"] if s["availability_pct"] is not None)
        total_cov = sum(s["coverage_minutes"] for s in result["per_server"]["values"] if s["availability_pct"] is not None)
        expected_agg = round((total_up / total_cov) * 100.0, 2)
        self.assertEqual(result["fleet_aggregate"]["value"], expected_agg)

        # Invariant 6: SLA Eligibility filtering
        # s1 (100% cov) -> eligible (not compliant: 98.5 < 99.9)
        # s2 (25% cov < 50% min) -> ineligible
        # s3 (69.4% cov >= 50%) -> eligible (not compliant: 20 < 99.9)
        # s4 (0% cov) -> ineligible
        self.assertEqual(result["eligible_count"], 2)
        self.assertEqual(result["sla_compliance_ratio"]["value"], 0.0)  # 0 compliant of 2 eligible

    def test_percentile_analytics(self):
        # 4 incidents with outages: 10m, 20m, 30m, 100m
        entries = [
            {"id": "s1", "name": "a", "availability_pct": 90.0, "denominator_minutes": 1000.0, "downtime_minutes": 10.0, "incidents": 1},
            {"id": "s2", "name": "b", "availability_pct": 90.0, "denominator_minutes": 1000.0, "downtime_minutes": 20.0, "incidents": 1},
            {"id": "s3", "name": "c", "availability_pct": 90.0, "denominator_minutes": 1000.0, "downtime_minutes": 30.0, "incidents": 1},
            {"id": "s4", "name": "d", "availability_pct": 90.0, "denominator_minutes": 1000.0, "downtime_minutes": 100.0, "incidents": 1},
        ]
        result = summarize_entries(entries, 1000.0)
        analytics = result["analytics"]
        self.assertEqual(analytics["total_incidents"], 4)
        self.assertEqual(analytics["total_downtime_minutes"], 160.0)
        self.assertEqual(analytics["mean_outage_minutes"], 40.0)  # 160 / 4
        self.assertEqual(analytics["max_outage_minutes"], 100.0)
        self.assertIsNotNone(analytics["median_outage_minutes"])
        self.assertIsNotNone(analytics["p95_outage_minutes"])

    def test_continuous_down_server_outage_analytics(self):
        # Target down the entire window (0 changes, but 100% downtime)
        entries = [
            {"id": "s1", "name": "dead-host", "availability_pct": 0.0, "denominator_minutes": 1440.0, "downtime_minutes": 1440.0, "incidents": 0}
        ]
        result = summarize_entries(entries, 1440.0)
        # Should be counted as 1 continuous incident with 1440.0m mean outage, NOT None
        self.assertEqual(result["analytics"]["total_incidents"], 1)
        self.assertEqual(result["analytics"]["mean_outage_minutes"], 1440.0)
        self.assertEqual(result["analytics"]["max_outage_minutes"], 1440.0)

    def test_reconstruct_time_series_intervals_regular(self):
        # 1 hour window = 3600 seconds, scrape interval = 2s, all UP
        start_ts = 1000000.0
        end_ts = 1003600.0
        samples = [(start_ts + i * 2.0, 1) for i in range(1801)]
        res = reconstruct_time_series_intervals(samples, start_ts, end_ts, expected_interval_sec=2.0)
        self.assertEqual(res["coverage_percent"], 100.0)
        self.assertEqual(res["unknown_percent"], 0.0)
        self.assertEqual(res["availability_pct"], 100.0)
        self.assertEqual(res["incident_count"], 0)
        self.assertEqual(res["sla_status"], "COMPLIANT")

    def test_reconstruct_time_series_intervals_with_large_gap(self):
        # 3600s window: 1000s UP, 2000s MISSING GAP, 600s UP
        start_ts = 1000000.0
        end_ts = 1003600.0
        samples_part1 = [(start_ts + i * 2.0, 1) for i in range(500)]  # 0 to 1000s
        samples_part2 = [(start_ts + 3000.0 + i * 2.0, 1) for i in range(301)]  # 3000 to 3600s
        samples = samples_part1 + samples_part2

        res = reconstruct_time_series_intervals(samples, start_ts, end_ts, expected_interval_sec=2.0, gap_tolerance=3.0)
        # Invariants: Coverage + Unknown == 3600s, Coverage % + Unknown % == 100%
        self.assertAlmostEqual(res["coverage_seconds"] + res["unknown_seconds"], 3600.0, places=1)
        self.assertAlmostEqual(res["coverage_percent"] + res["unknown_percent"], 100.0, places=1)
        # Gap of ~2000s must be classified as UNKNOWN
        self.assertGreater(res["unknown_seconds"], 1900.0)
        # Coverage is ~1600s (~44.4%), which is < 50% MIN_SLA_COVERAGE -> INSUFFICIENT_DATA
        self.assertEqual(res["sla_eligible"], False)
        self.assertEqual(res["sla_status"], "INSUFFICIENT_DATA")

    def test_reconstruct_outages_and_percentiles(self):
        # 3 outages: 60s, 120s, 300s
        start_ts = 1000000.0
        end_ts = 1005000.0
        # 0-500 UP, 500-560 DOWN (60s), 560-1500 UP, 1500-1620 DOWN (120s), 1620-3000 UP, 3000-3300 DOWN (300s), 3300-5000 UP
        samples = []
        for t in range(0, 5000, 10):
            ts = start_ts + t
            if 500 <= t < 560 or 1500 <= t < 1620 or 3000 <= t < 3300:
                samples.append((ts, 0))
            else:
                samples.append((ts, 1))

        res = reconstruct_time_series_intervals(samples, start_ts, end_ts, expected_interval_sec=10.0)
        self.assertEqual(res["incident_count"], 3)
        self.assertEqual(len(res["outage_durations_sec"]), 3)
        self.assertEqual(res["max_outage_minutes"], 5.0)  # 300s = 5m
        self.assertIsNotNone(res["mean_outage_minutes"])
        self.assertIsNotNone(res["median_outage_minutes"])

    def test_zero_samples_handling(self):
        start_ts = 1000000.0
        end_ts = 1003600.0
        res = reconstruct_time_series_intervals([], start_ts, end_ts)
        self.assertEqual(res["coverage_seconds"], 0.0)
        self.assertEqual(res["unknown_seconds"], 3600.0)
        self.assertEqual(res["coverage_percent"], 0.0)
        self.assertEqual(res["unknown_percent"], 100.0)
        self.assertIsNone(res["availability_pct"])
        self.assertEqual(res["sla_eligible"], False)
        self.assertEqual(res["sla_status"], "INSUFFICIENT_DATA")

    def test_invalid_period_raises(self):
        with self.assertRaises(ValueError):
            calculate_fleet_availability([{"id": "s1", "downtime_minutes": 0,
                                            "created_at": "2026-01-01T00:00:00Z"}], 0, now=NOW)

    def test_min_sla_coverage_percent_env_override(self):
        """MIN_SLA_COVERAGE_PERCENT must be tunable at runtime (no code edit /
        redeploy needed) instead of baked in as a hardcoded constant — and the
        override must actually flip eligibility, not just be read and ignored."""
        try:
            from fleet_availability import get_min_sla_coverage_percent
        except ImportError:
            from alarm.fleet_availability import get_min_sla_coverage_percent

        # 60% observed coverage, 100% availability over what it did observe.
        entries = [{"id": "s1", "name": "s1", "availability_pct": 100.0,
                    "denominator_minutes": 60.0, "downtime_minutes": 0.0, "incidents": 0}]

        old = os.environ.pop("MIN_SLA_COVERAGE_PERCENT", None)
        try:
            os.environ["MIN_SLA_COVERAGE_PERCENT"] = "50"
            self.assertEqual(get_min_sla_coverage_percent(), 50.0)
            result = summarize_entries(entries, 100.0)  # 60/100 = 60% coverage
            self.assertEqual(result["per_server"]["values"][0]["sla_status"], "COMPLIANT")

            os.environ["MIN_SLA_COVERAGE_PERCENT"] = "90"
            self.assertEqual(get_min_sla_coverage_percent(), 90.0)
            result = summarize_entries(entries, 100.0)  # same 60% coverage, now below the 90% bar
            self.assertEqual(result["per_server"]["values"][0]["sla_status"], "INSUFFICIENT_DATA")
            self.assertFalse(result["per_server"]["values"][0]["sla_eligible"])
        finally:
            if old is None:
                os.environ.pop("MIN_SLA_COVERAGE_PERCENT", None)
            else:
                os.environ["MIN_SLA_COVERAGE_PERCENT"] = old

    def test_insufficient_coverage_never_reports_compliant(self):
        """A target with almost no observed coverage but a perfect availability
        number over that sliver must never render as COMPLIANT — that would be
        a false SLA claim built on data the system barely has."""
        entries = [{"id": "new-host", "name": "new-host", "availability_pct": 100.0,
                    "denominator_minutes": 5.0, "downtime_minutes": 0.0, "incidents": 0}]
        result = summarize_entries(entries, 1440.0)  # 5 / 1440 minutes observed
        server = result["per_server"]["values"][0]
        self.assertEqual(server["sla_status"], "INSUFFICIENT_DATA")
        self.assertFalse(server["sla_eligible"])
        self.assertNotEqual(server["sla_status"], "COMPLIANT")

    def test_observed_vs_coverage_decoupling_and_limited_data_flags(self):
        """Availability must be computed from observed duration (not full window),
        coverage must be separate, and limited/no data flags must be correct."""
        # 7-day window (10080 min):
        # host-1: observed 2 days (2880 min), 1 day up, 1 day down -> 50% avail, coverage = 2880/10080 = 28.57% (<50% -> is_limited_data)
        # host-2: observed 7 days (10080 min), 7 days up -> 100% avail, coverage = 100%, 0 downtime (healthy)
        # host-3: observed 0 min -> no data
        window = 10080.0
        entries = [
            {"id": "h1", "name": "host-1", "denominator_minutes": 2880.0, "downtime_minutes": 1440.0, "incidents": 1},
            {"id": "h2", "name": "host-2", "denominator_minutes": 10080.0, "downtime_minutes": 0.0, "incidents": 0},
            {"id": "h3", "name": "host-3", "denominator_minutes": 0.0, "downtime_minutes": 0.0, "incidents": 0, "availability_pct": None},
        ]
        result = summarize_entries(entries, window, min_sla_coverage_pct=50.0)

        h1 = result["per_server"]["values"][0]
        self.assertEqual(h1["availability_pct"], 50.0)
        self.assertEqual(h1["observed_minutes"], 2880.0)
        self.assertEqual(h1["observed_seconds"], 2880.0 * 60.0)
        self.assertEqual(h1["downtime_seconds"], 1440.0 * 60.0)
        self.assertAlmostEqual(h1["coverage_percent"], 28.57, places=1)
        self.assertTrue(h1["is_limited_data"])
        self.assertFalse(h1["is_no_data"])

        h2 = result["per_server"]["values"][1]
        self.assertEqual(h2["availability_pct"], 100.0)
        self.assertFalse(h2["is_limited_data"])
        self.assertFalse(h2["is_no_data"])

        h3 = result["per_server"]["values"][2]
        self.assertIsNone(h3["availability_pct"])
        self.assertTrue(h3["is_no_data"])
        self.assertFalse(h3["is_limited_data"])

        # Health ratio: 1 healthy (h2) out of 2 scored (h1, h2) -> 50%
        self.assertEqual(result["healthy_hosts_count"], 1)
        self.assertEqual(result["health_ratio"]["healthy_count"], 1)
        self.assertEqual(result["health_ratio"]["total_count"], 2)
        self.assertEqual(result["health_ratio"]["server_count"], 3)
        self.assertEqual(result["health_ratio"]["value"], 50.0)

        # Fleet aggregate: total uptime = 1440 + 10080 = 11520, total observed = 2880 + 10080 = 12960
        expected_fleet_avail = round((11520 / 12960) * 100.0, 2)
        self.assertEqual(result["fleet_aggregate"]["value"], expected_fleet_avail)
        self.assertEqual(result["fleet_aggregate"]["uptime_percent"], expected_fleet_avail)
        self.assertEqual(result["fleet_aggregate"]["downtime_percent"], round(100.0 - expected_fleet_avail, 2))


if __name__ == "__main__":
    unittest.main()
