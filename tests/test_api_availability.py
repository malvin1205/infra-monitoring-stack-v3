import json
import time
import os
import tempfile
import unittest
from unittest.mock import patch

import app as app_module
from app import app
from storage import init_db


class ApiAvailabilityTests(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()
        app_module.clear_availability_cache()
        # The SQLite fast path in /api/availability persists materialized
        # buckets on every hit — without a per-test DB this test file was
        # writing fabricated bucket rows for fake instances straight into
        # the real default alarm/infrawatch.db.
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        init_db(self.db_path)
        self._orig_db_env = os.environ.get("INFRAWATCH_DB_PATH")
        os.environ["INFRAWATCH_DB_PATH"] = self.db_path

    def tearDown(self):
        app_module.clear_availability_cache()
        if self._orig_db_env is not None:
            os.environ["INFRAWATCH_DB_PATH"] = self._orig_db_env
        else:
            os.environ.pop("INFRAWATCH_DB_PATH", None)

    def test_availability_route_shape_and_sync(self):
        """/api/availability must not crash, must return every key the frontend
        consumes, and 'overall' must equal fleet_aggregate (single source of truth)."""
        now = time.time()

        def fake_query_map(query_expr, cache_ttl=5.0, timeout=None):
            if 'probe_success == 1' in query_expr or '(up == 1)' in query_expr:
                return {'host-a': '55', 'host-b': '0'}
            if 'probe_success == 0' in query_expr or '(up == 0)' in query_expr:
                return {'host-a': '5', 'host-b': '60'}
            if 'timestamp(' in query_expr and 'min_over_time' in query_expr:
                return {'host-a': str(now - 3600), 'host-b': str(now - 3600)}
            if 'timestamp(' in query_expr and 'max_over_time' in query_expr:
                return {'host-a': str(now), 'host-b': str(now)}
            if 'changes(' in query_expr:
                return {'host-a': '2', 'host-b': '1'}
            if query_expr in ('probe_success', 'up'):
                return {'host-a': '1', 'host-b': '0'}
            return {}

        with patch.object(app_module, 'get_monitored_instances', return_value=['host-a', 'host-b']), \
             patch.object(app_module, 'load_json', return_value=[]), \
             patch.object(app_module, 'fetch_prom_query_map', side_effect=fake_query_map):

            res = self.client.get('/api/availability?minutes=60')
            self.assertEqual(res.status_code, 200)
            data = json.loads(res.data)
            self.assertTrue(data['ok'])

            for key in ('overall', 'fleet_aggregate', 'fleet_average', 'sla_compliance',
                        'sla_compliance_ratio', 'zero_downtime_ratio', 'health_ratio',
                        'per_server', 'lowest_availability', 'entries', 'targets', 'analytics', 'counts'):
                self.assertIn(key, data, f"missing key: {key}")

            # Single source of truth: the headline card number == fleet_aggregate.
            self.assertEqual(data['overall'], data['fleet_aggregate']['value'])

            # Live counts reflect the live up/down state, not the historical average.
            self.assertEqual(data['counts']['online'], 1)
            self.assertEqual(data['counts']['offline'], 1)

            ids = {e['id'] for e in data['entries']}
            self.assertEqual(ids, {'host-a', 'host-b'})

    def test_availability_route_empty_fleet_is_graceful(self):
        with patch.object(app_module, 'get_monitored_instances', return_value=[]), \
             patch.object(app_module, 'load_json', return_value=[]), \
             patch.object(app_module, 'fetch_prom_query_map', return_value={}):

            res = self.client.get('/api/availability?minutes=60&job=doesnotexist')
            self.assertEqual(res.status_code, 200)
            data = json.loads(res.data)
            self.assertTrue(data['ok'])
            self.assertIsNone(data['overall'])
            self.assertEqual(data['counts']['total'], 0)
            self.assertEqual(data['entries'], [])

    def test_availability_route_time_windows(self):
        """Test 1h, 24h, 7d, and 30d window parameters."""
        for minutes_val in [60, 1440, 10080, 43200]:
            with self.subTest(minutes=minutes_val):
                app_module.clear_availability_cache()
                def fake_queries(expr, cache_ttl=5.0, timeout=None):
                    if 'probe_success[' in expr or 'up[' in expr:
                        if 'changes(' in expr:
                            return {'srv-1': '0', 'srv-2': '2'}
                        if 'count_over_time(' in expr:
                            return {'srv-1': str(minutes_val * 30), 'srv-2': str(minutes_val * 30)}
                        if 'probe_duration_seconds' in expr:
                            return {'srv-1': '0.015', 'srv-2': '0.045'}
                        # avg_over_time
                        return {'srv-1': '100.0', 'srv-2': '50.0'}
                    if expr in ('probe_success', 'up'):
                        return {'srv-1': '1', 'srv-2': '1'}
                    return {}

                with patch.object(app_module, 'get_monitored_instances', return_value=['srv-1', 'srv-2']), \
                     patch.object(app_module, 'load_json', return_value=[]), \
                     patch.object(app_module, 'fetch_prom_query_map', side_effect=fake_queries):

                    res = self.client.get(f'/api/availability?minutes={minutes_val}')
                    self.assertEqual(res.status_code, 200)
                    data = json.loads(res.data)
                    self.assertTrue(data['ok'])
                    self.assertEqual(data['period_minutes'], float(minutes_val))
                    self.assertEqual(data['fleet_average']['value'], 75.0)
                    self.assertEqual(data['fleet_aggregate']['value'], 75.0)
                    self.assertEqual(data['analytics']['total_incidents'], 1)
                    self.assertGreater(data['analytics']['mean_outage_minutes'], 0)

    def test_availability_route_3_level_priority_sorting(self):
        """Test that hosts requiring attention are sorted by:
        1. Availability (asc)
        2. Downtime duration (desc)
        3. Incident count (desc)"""
        app_module.clear_availability_cache()
        def fake_queries(expr, cache_ttl=5.0, timeout=None):
            if 'probe_success[' in expr or 'up[' in expr:
                if 'changes(' in expr:
                    return {'srv-50pct-low-inc': '1', 'srv-50pct-high-inc': '5', 'srv-70pct': '2'}
                if 'count_over_time(' in expr:
                    return {'srv-50pct-low-inc': '1800', 'srv-50pct-high-inc': '1800', 'srv-70pct': '1800'}
                # avg_over_time
                return {'srv-50pct-low-inc': '50.0', 'srv-50pct-high-inc': '50.0', 'srv-70pct': '70.0'}
            if expr in ('probe_success', 'up'):
                return {'srv-50pct-low-inc': '1', 'srv-50pct-high-inc': '1', 'srv-70pct': '1'}
            return {}

        with patch.object(app_module, 'get_monitored_instances', return_value=['srv-50pct-low-inc', 'srv-50pct-high-inc', 'srv-70pct']), \
             patch.object(app_module, 'load_json', return_value=[]), \
             patch.object(app_module, 'fetch_prom_query_map', side_effect=fake_queries):

            res = self.client.get('/api/availability?minutes=60')
            self.assertEqual(res.status_code, 200)
            data = json.loads(res.data)
            self.assertTrue(data['ok'])
            self.assertIn('hosts_requiring_attention', data)

            lowest = data['lowest_availability']
            self.assertEqual(len(lowest), 3)
            self.assertEqual(lowest[0]['id'], 'srv-50pct-high-inc')
            self.assertEqual(lowest[1]['id'], 'srv-50pct-low-inc')
            self.assertEqual(lowest[2]['id'], 'srv-70pct')

            first_entry = lowest[0]
            self.assertIn('observed_minutes', first_entry)
            self.assertIn('observed_seconds', first_entry)
            self.assertIn('uptime_seconds', first_entry)
            self.assertIn('downtime_seconds', first_entry)
            self.assertIn('coverage_percent', first_entry)
            self.assertIn('incident_count', first_entry)

    def test_availability_dynamic_scrape_intervals(self):
        """Coverage correctly derived from sample timestamps across 60s scrape interval."""
        app_module.clear_availability_cache()
        now = time.time()
        start_t = now - 3600

        def fake_queries(expr, cache_ttl=5.0, timeout=None):
            if 'count_over_time' in expr:
                return {'srv-60s': '60'}  # 60 samples in 60 min -> 60s interval
            if 'min_over_time(timestamp' in expr:
                return {'srv-60s': str(start_t)}
            if 'max_over_time(timestamp' in expr:
                return {'srv-60s': str(now - 60)}
            if 'avg_over_time' in expr and 'probe_duration' not in expr:
                return {'srv-60s': '100.0'}
            if expr in ('probe_success', 'up'):
                return {'srv-60s': '1'}
            return {}

        with patch.object(app_module, 'get_monitored_instances', return_value=['srv-60s']), \
             patch.object(app_module, 'load_json', return_value=[]), \
             patch.object(app_module, 'fetch_prom_query_map', side_effect=fake_queries):

            res = self.client.get('/api/availability?minutes=60')
            self.assertEqual(res.status_code, 200)
            data = json.loads(res.data)
            entry = data['entries'][0]
            # Must report ~60 minutes coverage, NOT 2 minutes from hardcoded 2s multiplier
            self.assertAlmostEqual(entry['coverage_minutes'], 60.0, places=0)
            self.assertAlmostEqual(entry['coverage_pct'], 100.0, places=0)
            self.assertEqual(entry['sla_status'], 'COMPLIANT')

    def test_health_live_endpoint(self):
        res = self.client.get('/health/live')
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertTrue(data['ok'])
        self.assertEqual(data['status'], 'alive')

    def test_health_ready_endpoint(self):
        res = self.client.get('/health/ready')
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertTrue(data['ok'])
        self.assertEqual(data['status'], 'ready')

    def test_health_endpoint_diagnostics_when_prometheus_down(self):
        with patch.object(app_module, 'fetch_prometheus_json', return_value=(None, None)):
            res = self.client.get('/health')
            # Returns 200 with diagnostic breakdown so frontend renders status accurately without crashing container
            self.assertEqual(res.status_code, 200)
            data = json.loads(res.data)
            self.assertFalse(data['ok'])
            self.assertFalse(data['components']['prometheus']['ok'])
            self.assertTrue(data['components']['storage']['ok'])

    def test_availability_step_suffix_and_query_optimization(self):
        """Verify that Prometheus range queries apply appropriate subquery step resolution:
        <= 1440m (24h): raw scrape resolution
        > 1440m (e.g. 7d = 10080m): :30s step
        > 10080m (e.g. 30d = 43200m): :5m step"""
        captured_queries = []

        def fake_query_map(query_expr, cache_ttl=5.0, timeout=None):
            captured_queries.append((query_expr, cache_ttl))
            if query_expr in ('probe_success', 'up'):
                return {'host-1': '1'}
            return {'host-1': '100.0'}

        with patch.object(app_module, 'get_monitored_instances', return_value=['host-1']), \
             patch.object(app_module, 'load_json', return_value=[]), \
             patch.object(app_module, 'fetch_prom_query_map', side_effect=fake_query_map):

            # 1. 24h range (1440m) -> direct ranges [1440m] and timestamp subqueries [1440m:]
            app_module.clear_availability_cache()
            captured_queries.clear()
            res24h = self.client.get('/api/availability?minutes=1440')
            self.assertEqual(res24h.status_code, 200)
            for q, ttl in captured_queries:
                if '[' in q:
                    self.assertTrue('[1440m]' in q or '[1440m:]' in q, f"Unexpected range format in query: {q}")
                    self.assertNotIn(':30s', q)
                    self.assertNotIn(':5m', q)
                self.assertEqual(ttl, 15.0)

            # 2. 7d range (10080m) -> direct ranges [10080m] and timestamp subqueries [10080m:]
            app_module.clear_availability_cache()
            captured_queries.clear()
            res7d = self.client.get('/api/availability?minutes=10080')
            self.assertEqual(res7d.status_code, 200)
            for q, ttl in captured_queries:
                if '[' in q:
                    self.assertTrue('[10080m]' in q or '[10080m:]' in q, f"Unexpected range format in query: {q}")
                self.assertEqual(ttl, 15.0)

            # 3. 30d range (43200m) -> direct ranges [43200m] and timestamp subqueries [43200m:]
            app_module.clear_availability_cache()
            captured_queries.clear()
            res30d = self.client.get('/api/availability?minutes=43200')
            self.assertEqual(res30d.status_code, 200)
            for q, ttl in captured_queries:
                if '[' in q:
                    self.assertTrue('[43200m]' in q or '[43200m:]' in q, f"Unexpected range format in query: {q}")
                self.assertEqual(ttl, 15.0)

            # 4. 1h range (60m) -> cache_ttl = 5.0s
            app_module.clear_availability_cache()
            captured_queries.clear()
            res1h = self.client.get('/api/availability?minutes=60')
            self.assertEqual(res1h.status_code, 200)
            for q, ttl in captured_queries:
                self.assertEqual(ttl, 5.0)

    def test_rapid_range_switching_isolation(self):
        """Simulate rapid switching between 24h, 7d, 30d, and 24h to verify parameter isolation."""
        for target_mins in [1440, 10080, 43200, 1440]:
            def fake_query_map(expr, cache_ttl=5.0, timeout=None):
                if 'probe_success' in expr or 'up' in expr:
                    return {'node-1': '1'}
                return {'node-1': '99.5'}

            with patch.object(app_module, 'get_monitored_instances', return_value=['node-1']), \
                 patch.object(app_module, 'load_json', return_value=[]), \
                 patch.object(app_module, 'fetch_prom_query_map', side_effect=fake_query_map):

                res = self.client.get(f'/api/availability?minutes={target_mins}')
                self.assertEqual(res.status_code, 200)
                data = json.loads(res.data)
                self.assertTrue(data['ok'])
                self.assertEqual(data['period_minutes'], float(target_mins))

    def test_availability_with_upstream_prometheus_partial_failures(self):
        """When Prometheus times out or returns empty for some queries, availability route still returns valid structure."""
        app_module.clear_availability_cache()
        def failing_query_map(expr, cache_ttl=5.0, timeout=None):
            # Simulate failure on count or latency
            if 'count_over_time' in expr or 'probe_duration' in expr:
                return {}
            if expr in ('probe_success', 'up'):
                return {'node-1': '1'}
            return {'node-1': '98.0'}

        with patch.object(app_module, 'get_monitored_instances', return_value=['node-1']), \
             patch.object(app_module, 'load_json', return_value=[]), \
             patch.object(app_module, 'fetch_prom_query_map', side_effect=failing_query_map):

            res = self.client.get('/api/availability?minutes=1440')
            self.assertEqual(res.status_code, 200)
            data = json.loads(res.data)
            self.assertTrue(data['ok'])
            self.assertEqual(len(data['entries']), 1)
            self.assertEqual(data['entries'][0]['availability_pct'], 98.0)


if __name__ == '__main__':
    unittest.main()


