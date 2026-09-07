import json
import time
import os
import tempfile
import shutil
import unittest
from unittest.mock import patch, MagicMock
from concurrent.futures import ThreadPoolExecutor

import app as alarm_app
import json_store
from app import (
    app, fetch_prometheus_json, PROMETHEUS_CACHE, PROMETHEUS_CACHE_LOCK,
    _FETCH_LOCKS, _FAILED_CANDIDATES, _FAILED_CANDIDATES_LOCK,
    build_canonical_monitoring_state, fetch_all_probe_metrics, fetch_down_since_prom_map
)
from conftest import TEST_API_KEY, TEST_WEBHOOK_SECRET


class PerformanceCachingTests(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()
        self.client.environ_base = {"HTTP_X_API_KEY": TEST_API_KEY, "HTTP_X_WEBHOOK_SECRET": TEST_WEBHOOK_SECRET}
        self.tmpdir = tempfile.mkdtemp()
        self._orig = (json_store.STATUS_FILE,)
        json_store.STATUS_FILE = os.path.join(self.tmpdir, "status.json")
        self.db_path = os.path.join(self.tmpdir, "test_infrawatch.db")
        self._orig_db_env = os.environ.get("INFRAWATCH_DB_PATH")
        os.environ["INFRAWATCH_DB_PATH"] = self.db_path
        from storage import init_db
        init_db(self.db_path)

        with PROMETHEUS_CACHE_LOCK:
            PROMETHEUS_CACHE.clear()
        with _FAILED_CANDIDATES_LOCK:
            _FAILED_CANDIDATES.clear()
        alarm_app.clear_availability_cache(clear_db=True)
        # _ENDPOINTS_CACHE is a short-TTL (2s) process-global cache — an
        # adjacent test file's still-warm entry (pointing at its own
        # already-torn-down tmp DB state) can otherwise leak into this test's
        # avail_cache_key derivation (api_availability keys its cache on
        # active_url) within that window.
        alarm_app._ENDPOINTS_CACHE["data"] = None

    def tearDown(self):
        if self._orig_db_env is not None:
            os.environ["INFRAWATCH_DB_PATH"] = self._orig_db_env
        else:
            os.environ.pop("INFRAWATCH_DB_PATH", None)
        (json_store.STATUS_FILE,) = self._orig
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        with PROMETHEUS_CACHE_LOCK:
            PROMETHEUS_CACHE.clear()
        with _FAILED_CANDIDATES_LOCK:
            _FAILED_CANDIDATES.clear()
        alarm_app.clear_availability_cache()

    def test_cache_correctness_and_expiry(self):
        """fetch_prometheus_json caches response for TTL duration and refreshes after expiry."""
        call_count = [0]

        def fake_fetch_url(url, timeout=1.5):
            call_count[0] += 1
            return json.dumps({"status": "success", "data": {"result": [{"count": call_count[0]}]}})

        with patch('prometheus_client.fetch_url', side_effect=fake_fetch_url), \
             patch('prometheus_client.load_endpoints', return_value={"active": "http://prom:9090", "endpoints": ["http://prom:9090"]}):

            # Call 1: Miss -> fetches
            data1, _ = fetch_prometheus_json('/api/v1/query?query=test', use_cache=True, cache_ttl=1.0)
            self.assertEqual(call_count[0], 1)
            self.assertEqual(data1['data']['result'][0]['count'], 1)

            # Call 2: Hit within TTL -> does NOT fetch
            data2, _ = fetch_prometheus_json('/api/v1/query?query=test', use_cache=True, cache_ttl=1.0)
            self.assertEqual(call_count[0], 1)
            self.assertEqual(data2['data']['result'][0]['count'], 1)

            # Expire cache entry
            time.sleep(1.05)

            # Call 3: Expired -> fetches fresh data
            data3, _ = fetch_prometheus_json('/api/v1/query?query=test', use_cache=True, cache_ttl=1.0)
            self.assertEqual(call_count[0], 2)
            self.assertEqual(data3['data']['result'][0]['count'], 2)

    def test_concurrent_identical_requests_single_flight_coalescing(self):
        """Multiple concurrent identical PromQL requests only trigger 1 upstream HTTP call."""
        call_count = [0]

        def fake_fetch_url(url, timeout=1.5):
            time.sleep(0.05)  # Simulate network latency
            call_count[0] += 1
            return json.dumps({"status": "success", "data": {"result": [{"worker": call_count[0]}]}})

        with patch('prometheus_client.fetch_url', side_effect=fake_fetch_url), \
             patch('prometheus_client.load_endpoints', return_value={"active": "http://prom:9090", "endpoints": ["http://prom:9090"]}):

            def make_request(_):
                data, _ = fetch_prometheus_json('/api/v1/query?query=probe_success', use_cache=True, cache_ttl=5.0)
                return data

            with ThreadPoolExecutor(max_workers=10) as executor:
                results = list(executor.map(make_request, range(10)))

            # Exactly 1 upstream fetch should have occurred
            self.assertEqual(call_count[0], 1)
            for res in results:
                self.assertEqual(res['status'], 'success')

    def test_endpoint_failover_and_candidate_circuit_breaker(self):
        """When primary endpoint is down, failover probes secondary and circuit-breaks dead candidates."""
        attempted_urls = []

        def fake_fetch_url(url, timeout=1.5):
            attempted_urls.append(url)
            if "backup-prom" in url:
                return json.dumps({"status": "success", "data": {"activeTargets": []}})
            return None

        with patch('prometheus_client.fetch_url', side_effect=fake_fetch_url), \
             patch('prometheus_client.load_endpoints', return_value={
                 "active": "http://dead-prom-1:9090",
                 "endpoints": ["http://dead-prom-1:9090", "http://backup-prom:9090"]
             }):

            # Query 1: Discovers backup-prom
            data1, base1 = fetch_prometheus_json('/api/v1/targets', use_cache=False)
            self.assertEqual(base1, "http://backup-prom:9090")
            self.assertEqual(data1['status'], 'success')

            # Dead endpoints should be recorded in circuit breaker
            with _FAILED_CANDIDATES_LOCK:
                self.assertIn("http://dead-prom-1:9090", _FAILED_CANDIDATES)

            # Query 2: Subsequent query should directly reuse working endpoint without probing dead endpoints
            attempted_urls.clear()
            data2, base2 = fetch_prometheus_json('/api/v1/targets', use_cache=False)
            self.assertEqual(base2, "http://backup-prom:9090")
            self.assertEqual(len(attempted_urls), 1)
            self.assertIn("backup-prom", attempted_urls[0])

    def test_down_since_conditional_optimization(self):
        """When all targets are healthy, expensive 1-day range subqueries are completely bypassed."""
        query_paths = []

        def fake_fetch(path, use_cache=True, cache_ttl=None, timeout=None):
            query_paths.append(path)
            if path == '/api/v1/targets':
                return {
                    "status": "success",
                    "data": {
                        "activeTargets": [
                            {"labels": {"instance": "srv-1", "job": "node"}, "health": "up", "scrapeUrl": "srv-1"},
                            {"labels": {"instance": "srv-2", "job": "node"}, "health": "up", "scrapeUrl": "srv-2"}
                        ]
                    }
                }, "http://prom:9090"
            elif "probe_success" in path:
                return {"status": "success", "data": {"result": [
                    {"metric": {"instance": "srv-1"}, "value": [1000, "1"]},
                    {"metric": {"instance": "srv-2"}, "value": [1000, "1"]}
                ]}}, "http://prom:9090"
            elif "probe_duration_seconds" in path or "probe_http_status_code" in path:
                return {"status": "success", "data": {"result": []}}, "http://prom:9090"
            return None, None

        with patch('app.fetch_prometheus_json', side_effect=fake_fetch), \
             patch('app.load_website_targets', return_value=[]):

            state = build_canonical_monitoring_state("all")
            self.assertEqual(state['system_status'], 'NORMAL')

            # Ensure max_over_time 1-day range query was NOT executed
            range_queries = [p for p in query_paths if 'max_over_time' in p or 'min_over_time' in p]
            self.assertEqual(len(range_queries), 0)

    def test_prometheus_unavailable_graceful_degradation(self):
        """When Prometheus is completely unreachable, /instances returns 503 with structured CRITICAL payload."""
        with patch('app.fetch_prometheus_json', return_value=(None, None)), \
             patch('app.load_website_targets', return_value=[]):

            res = self.client.get('/instances')
            self.assertEqual(res.status_code, 503)
            data = json.loads(res.data)
            self.assertFalse(data['ok'])
            self.assertEqual(data['system_status'], 'CRITICAL')

    def test_availability_route_cache_hit_and_expiry(self):
        """/api/availability caches the computed response for the TTL window and avoids redundant PromQL queries."""
        alarm_app.clear_availability_cache()
        query_count = [0]

        def fake_query_map(expr, cache_ttl=5.0, timeout=None):
            query_count[0] += 1
            if expr in ('probe_success', 'up'):
                return {'srv-cache-1': '1'}
            return {'srv-cache-1': '99.9'}

        # The route's in-memory cache key buckets live requests by
        # int(time.time() // 15) * 15 (see avail_cache_key in app.py). Two
        # back-to-back calls landing in different 15s buckets — entirely
        # possible under load, since nothing else here controls wall-clock
        # timing — would each recompute independently and could legitimately
        # disagree, which isn't what this test means to exercise (a genuine
        # cache *hit*). Freeze time so both calls fall in the same bucket.
        with patch.object(alarm_app, 'get_monitored_instances', return_value=['srv-cache-1']), \
             patch.object(json_store, 'load_json', return_value=[]), \
             patch.object(alarm_app, 'fetch_prom_query_map', side_effect=fake_query_map), \
             patch('app.time.time', return_value=time.time()):

            # Call 1: Cache miss -> queries Prometheus
            res1 = self.client.get('/api/availability?minutes=1440')
            self.assertEqual(res1.status_code, 200)
            data1 = json.loads(res1.data)
            self.assertTrue(data1['ok'])
            initial_queries = query_count[0]
            self.assertGreater(initial_queries, 0)

            # Call 2: Cache hit -> does NOT re-query Prometheus
            res2 = self.client.get('/api/availability?minutes=1440')
            self.assertEqual(res2.status_code, 200)
            data2 = json.loads(res2.data)
            self.assertTrue(data2['ok'])
            self.assertEqual(query_count[0], initial_queries)  # 0 additional queries!
            self.assertEqual(data1['overall'], data2['overall'])

    def test_availability_route_single_flight_coalescing(self):
        """10 concurrent requests to /api/availability coalesce into a single backend computation."""
        alarm_app.clear_availability_cache()
        calc_count = [0]

        def fake_query_map(expr, cache_ttl=5.0, timeout=None):
            time.sleep(0.04)  # Simulate network/PromQL latency
            calc_count[0] += 1
            if expr in ('probe_success', 'up'):
                return {'srv-sf-1': '1', 'srv-sf-2': '0'}
            return {'srv-sf-1': '100.0', 'srv-sf-2': '90.0'}

        with patch.object(alarm_app, 'get_monitored_instances', return_value=['srv-sf-1', 'srv-sf-2']), \
             patch.object(json_store, 'load_json', return_value=[]), \
             patch.object(alarm_app, 'fetch_prom_query_map', side_effect=fake_query_map):

            def make_avail_call(_):
                # Use a separate test client or app context for thread-safety
                with app.test_client() as c:
                    r = c.get('/api/availability?minutes=1440')
                    return r.status_code, json.loads(r.data)

            with ThreadPoolExecutor(max_workers=10) as executor:
                results = list(executor.map(make_avail_call, range(10)))

            # All 10 requests must succeed with identical data
            first_body = results[0][1]
            for status, body in results:
                self.assertEqual(status, 200)
                self.assertTrue(body['ok'])
                self.assertEqual(body['counts']['total'], 2)
                self.assertEqual(body['overall'], first_body['overall'])

            # Queries should have coalesced to 1 batch (at most 13 PromQL queries, not 130!)
            self.assertLessEqual(calc_count[0], 13)

    def test_availability_rapid_range_switching_warm_cache(self):
        """Switching 24h -> 7d -> 30d -> 24h gives instant warm cache hits for previously loaded ranges."""
        alarm_app.clear_availability_cache()
        query_log = []

        def fake_query_map(expr, cache_ttl=5.0, timeout=None):
            query_log.append(expr)
            if expr in ('probe_success', 'up'):
                return {'srv-warm': '1'}
            return {'srv-warm': '99.0'}

        with patch.object(alarm_app, 'get_monitored_instances', return_value=['srv-warm']), \
             patch.object(json_store, 'load_json', return_value=[]), \
             patch.object(alarm_app, 'fetch_prom_query_map', side_effect=fake_query_map):

            # 1. 24h initial load (cold start -> Prometheus queries -> materialized to SQLite)
            r24h = self.client.get('/api/availability?minutes=1440')
            self.assertEqual(r24h.status_code, 200)
            data24h = json.loads(r24h.data)
            self.assertTrue(data24h['ok'])
            count_after_24h = len(query_log)
            self.assertGreater(count_after_24h, 0)

            # 2. Repeated 24h load -> Served directly from warm cache/SQLite without hitting Prometheus!
            r24h_repeat = self.client.get('/api/availability?minutes=1440')
            self.assertEqual(r24h_repeat.status_code, 200)
            self.assertEqual(len(query_log), count_after_24h)

            # 3. 7d load -> Cold for 7d (Prometheus queried since SQLite only had 24h) and materialized
            r7d = self.client.get('/api/availability?minutes=10080')
            self.assertEqual(r7d.status_code, 200)
            data7d = json.loads(r7d.data)
            self.assertTrue(data7d['ok'])
            count_after_7d = len(query_log)
            self.assertGreater(count_after_7d, count_after_24h)

            # 4. 30d load -> Cold for 30d (Prometheus queried since SQLite only had 7d) and materialized
            r30d = self.client.get('/api/availability?minutes=43200')
            self.assertEqual(r30d.status_code, 200)
            data30d = json.loads(r30d.data)
            self.assertTrue(data30d['ok'])
            count_after_30d = len(query_log)
            self.assertGreater(count_after_30d, count_after_7d)

            # 5. Switch back to 24h, 7d, 30d -> instant warm cache / SQLite hits with zero new queries!
            r24h_again = self.client.get('/api/availability?minutes=1440')
            self.assertEqual(r24h_again.status_code, 200)
            self.assertEqual(len(query_log), count_after_30d)

            r7d_again = self.client.get('/api/availability?minutes=10080')
            self.assertEqual(r7d_again.status_code, 200)
            self.assertEqual(len(query_log), count_after_30d)

            r30d_again = self.client.get('/api/availability?minutes=43200')
            self.assertEqual(r30d_again.status_code, 200)
            self.assertEqual(len(query_log), count_after_30d)

    def test_availability_correctness_and_invariants_preserved(self):
        """Mathematical invariants (Coverage = Uptime + Downtime, Window = Coverage + Unknown) are strictly preserved."""
        alarm_app.clear_availability_cache()

        def fake_query_map(expr, cache_ttl=5.0, timeout=None):
            if 'count_over_time' in expr:
                return {'srv-perfect': '720', 'srv-partial': '360', 'srv-down': '720'}
            if 'min_over_time(timestamp' in expr:
                return {'srv-perfect': '1000', 'srv-partial': '1000', 'srv-down': '1000'}
            if 'max_over_time(timestamp' in expr:
                # srv-partial observed only 30 min out of 60 min
                return {'srv-perfect': '4600', 'srv-partial': '2800', 'srv-down': '4600'}
            if 'avg_over_time' in expr and 'probe_duration' not in expr:
                return {'srv-perfect': '100.0', 'srv-partial': '80.0', 'srv-down': '0.0'}
            if 'changes(' in expr:
                return {'srv-perfect': '0', 'srv-partial': '2', 'srv-down': '0'}
            if expr in ('probe_success', 'up'):
                return {'srv-perfect': '1', 'srv-partial': '1', 'srv-down': '0'}
            return {}

        with patch.object(alarm_app, 'get_monitored_instances', return_value=['srv-perfect', 'srv-partial', 'srv-down']), \
             patch.object(json_store, 'load_json', return_value=[]), \
             patch.object(alarm_app, 'fetch_prom_query_map', side_effect=fake_query_map):

            res = self.client.get('/api/availability?minutes=60')
            self.assertEqual(res.status_code, 200)
            data = json.loads(res.data)

            entries_by_id = {e['id']: e for e in data['entries']}

            # srv-perfect: 100% up, 60m coverage, 0 downtime, 0 unknown
            p = entries_by_id['srv-perfect']
            self.assertEqual(p['availability_pct'], 100.0)
            self.assertAlmostEqual(p['uptime_minutes'] + p['downtime_minutes'], p['coverage_minutes'], places=1)
            self.assertAlmostEqual(p['coverage_minutes'] + p['unknown_minutes'], 60.0, places=1)
            self.assertEqual(p['sla_status'], 'COMPLIANT')

            # srv-down: 0% up, 60m downtime, 0 unknown
            d = entries_by_id['srv-down']
            self.assertEqual(d['availability_pct'], 0.0)
            self.assertAlmostEqual(d['downtime_minutes'], 60.0, places=1)
            self.assertEqual(d['sla_status'], 'NON_COMPLIANT')

            # Fleet Aggregate (weighted) and Health ratio
            self.assertIsNotNone(data['fleet_aggregate']['value'])
            self.assertIsNotNone(data['health_ratio']['value'])
            self.assertEqual(data['counts']['online'], 1)
            self.assertEqual(data['counts']['offline'], 2)


if __name__ == '__main__':
    unittest.main()
