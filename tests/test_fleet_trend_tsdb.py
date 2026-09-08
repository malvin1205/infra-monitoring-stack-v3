"""_build_fleet_trend reads the Prometheus TSDB (one query_range), scoped to the
exact monitored-instance set. Check slot sizing, NaN gaps, the probe_success->up
fallback, and that the selector is pinned to the passed instances."""
import time
import unittest
from urllib.parse import unquote
from unittest.mock import patch

import app as alarm_app

INSTS = ["192.168.9.87", "192.168.9.24", "10.0.0.5:9100"]


def _range_resp(values):
    return ({"status": "success", "data": {"result": [{"metric": {}, "values": values}]}}, "http://prom")


class FleetTrendTsdbTests(unittest.TestCase):
    def setUp(self):
        alarm_app._FLEET_TREND_CACHE.clear()

    def test_hourly_slots_and_pct_conversion(self):
        now = int(time.time())
        vals = [[now - 7200, "0.99"], [now - 3600, "0.955"], [now, "1"]]
        with patch.object(alarm_app.promclient, "fetch_prometheus_json", return_value=_range_resp(vals)) as m:
            series, slot = alarm_app._build_fleet_trend(now, 24 * 3600, INSTS)
        self.assertEqual(slot, 3600)
        self.assertEqual([p["availability_pct"] for p in series], [99.0, 95.5, 100.0])
        self.assertIn("step=3600", m.call_args[0][0])
        self.assertIn("query_range", m.call_args[0][0])

    def test_selector_pinned_to_passed_instances(self):
        now = int(time.time())
        with patch.object(alarm_app.promclient, "fetch_prometheus_json", return_value=_range_resp([[now, "1"]])) as m:
            alarm_app._build_fleet_trend(now, 24 * 3600, INSTS)
        q = unquote(m.call_args[0][0])
        self.assertIn("instance=~`", q)          # backtick raw string (re.escape emits \\.)
        self.assertIn(r"192\.168\.9\.87", q)
        self.assertIn(r"10\.0\.0\.5:9100", q)
        self.assertNotIn("probe_success[", q)    # never the unscoped metric

    def test_empty_instances_returns_empty_without_querying(self):
        now = int(time.time())
        with patch.object(alarm_app.promclient, "fetch_prometheus_json") as m:
            series, slot = alarm_app._build_fleet_trend(now, 24 * 3600, [])
        self.assertEqual(series, [])
        self.assertEqual(slot, 3600)
        m.assert_not_called()

    def test_long_window_widens_slot_under_max_points(self):
        now = int(time.time())
        with patch.object(alarm_app.promclient, "fetch_prometheus_json", return_value=_range_resp([[now, "1"]])):
            _, slot = alarm_app._build_fleet_trend(now, 30 * 86400, INSTS, max_points=180)
        self.assertGreater(slot, 3600)
        self.assertEqual(slot % 3600, 0)
        self.assertLessEqual(30 * 86400 / slot, 180)

    def test_nan_slot_is_dropped_as_gap(self):
        now = int(time.time())
        vals = [[now - 3600, "NaN"], [now, "0.5"]]
        with patch.object(alarm_app.promclient, "fetch_prometheus_json", return_value=_range_resp(vals)):
            series, _ = alarm_app._build_fleet_trend(now, 24 * 3600, INSTS)
        self.assertEqual(len(series), 1)
        self.assertEqual(series[0]["availability_pct"], 50.0)

    def test_falls_back_to_up_when_probe_success_empty(self):
        now = int(time.time())
        empty = ({"status": "success", "data": {"result": []}}, "http://prom")
        good = _range_resp([[now, "1"]])
        with patch.object(alarm_app.promclient, "fetch_prometheus_json", side_effect=[empty, good]) as m:
            series, _ = alarm_app._build_fleet_trend(now, 24 * 3600, INSTS)
        self.assertEqual(len(series), 1)
        self.assertIn("probe_success", m.call_args_list[0][0][0])
        self.assertIn("sum(sum_over_time(up", unquote(m.call_args_list[1][0][0]))

    def test_query_failure_returns_empty(self):
        now = int(time.time())
        with patch.object(alarm_app.promclient, "fetch_prometheus_json", side_effect=RuntimeError("boom")):
            series, slot = alarm_app._build_fleet_trend(now, 24 * 3600, INSTS)
        self.assertEqual(series, [])
        self.assertEqual(slot, 3600)


if __name__ == "__main__":
    unittest.main()
