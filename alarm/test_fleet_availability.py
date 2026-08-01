import unittest
import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))

try:
    from fleet_availability import calculate_fleet_availability
except ImportError:
    from alarm.fleet_availability import calculate_fleet_availability

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

    def test_invalid_period_raises(self):
        with self.assertRaises(ValueError):
            calculate_fleet_availability([{"id": "s1", "downtime_minutes": 0,
                                            "created_at": "2026-01-01T00:00:00Z"}], 0, now=NOW)


if __name__ == "__main__":
    unittest.main()
