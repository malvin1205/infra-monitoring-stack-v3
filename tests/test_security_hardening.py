"""Regression coverage for the security audit fixes:
  Phase 1 — API key no longer rendered into the public dashboard HTML.
  Phase 2 — Prometheus endpoint SSRF revalidation at request time.
  Phase 5 — GET /api/telegram requires operator auth.

(Phase 4's webhook query-secret removal is covered in
test_auth_provisioning.py::test_webhook_secret_authentication, alongside the
rest of that file's auth-provisioning coverage.)
"""
import ipaddress
import os
import shutil
import tempfile
import time
import unittest

import app as alarm_app
from app import app, is_safe_endpoint_url, _is_blocked_ip, _filter_safe_candidates
from conftest import TEST_API_KEY


class PublicDashboardHasNoCredentialTests(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()
        # POSTing a target below must not touch the real repo checkout's
        # targets/websites.yml — redirect it like the other test files do.
        # get_targets_file() falls back to the real repo file whenever
        # TARGETS_FILE points at a path that doesn't exist yet, so the
        # redirect target needs to actually exist on disk from the start.
        self.tmpdir = tempfile.mkdtemp()
        self._orig_targets_env = os.environ.get("TARGETS_FILE")
        targets_path = os.path.join(self.tmpdir, "websites.yml")
        with open(targets_path, 'w', encoding='utf-8') as f:
            f.write("[]\n")
        os.environ["TARGETS_FILE"] = targets_path

    def tearDown(self):
        if self._orig_targets_env is not None:
            os.environ["TARGETS_FILE"] = self._orig_targets_env
        else:
            os.environ.pop("TARGETS_FILE", None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_index_does_not_leak_api_key(self):
        res = self.client.get('/')
        self.assertEqual(res.status_code, 200)
        body = res.data.decode('utf-8')
        self.assertNotIn('api-key', body)
        self.assertNotIn(TEST_API_KEY, body)

    def test_mutation_endpoint_rejects_anonymous_viewer(self):
        res = self.client.post('/api/targets', json={"url": "example.com"})
        self.assertEqual(res.status_code, 401)

    def test_mutation_endpoint_accepts_operator_key(self):
        res = self.client.post(
            '/api/targets',
            json={"url": "definitely-not-a-real-target-xyz.example"},
            headers={"X-API-Key": TEST_API_KEY},
        )
        # Whatever the outcome (target-add validation may still 400), it must
        # not be rejected purely for lacking credentials.
        self.assertNotEqual(res.status_code, 401)


class TelegramConfigAuthTests(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()

    def test_get_telegram_config_requires_auth(self):
        res = self.client.get('/api/telegram')
        self.assertEqual(res.status_code, 401)

    def test_get_telegram_config_with_key_succeeds(self):
        res = self.client.get('/api/telegram', headers={"X-API-Key": TEST_API_KEY})
        self.assertEqual(res.status_code, 200)


class SsrfRevalidationTests(unittest.TestCase):
    def test_loopback_ip_is_blocked(self):
        ok, _ = is_safe_endpoint_url("http://127.0.0.1:9090")
        self.assertFalse(ok)

    def test_link_local_ip_is_blocked(self):
        ok, _ = is_safe_endpoint_url("http://169.254.169.254/")
        self.assertFalse(ok)

    def test_public_ip_literal_is_allowed(self):
        ok, _ = is_safe_endpoint_url("http://8.8.8.8:9090")
        self.assertTrue(ok)

    def test_is_blocked_ip_flags_loopback_and_link_local(self):
        self.assertTrue(_is_blocked_ip(ipaddress.ip_address("127.0.0.1")))
        self.assertTrue(_is_blocked_ip(ipaddress.ip_address("169.254.1.1")))
        self.assertFalse(_is_blocked_ip(ipaddress.ip_address("8.8.8.8")))

    def test_filter_safe_candidates_drops_rebound_endpoint(self):
        # Simulates the DNS-rebinding scenario: an endpoint that resolves to a
        # blocked (loopback) IP at fetch time must not survive the filter,
        # even though nothing here ever called the registration-time check.
        safe = _filter_safe_candidates(["http://127.0.0.1:9090", "http://8.8.8.8:9090"])
        self.assertNotIn("http://127.0.0.1:9090", safe)
        self.assertIn("http://8.8.8.8:9090", safe)

    def test_filter_safe_candidates_result_is_cached(self):
        # Second call for the same URL must hit the TTL cache, not re-resolve.
        calls = {"n": 0}
        real = alarm_app.is_safe_endpoint_url

        def counting(url):
            calls["n"] += 1
            return real(url)

        alarm_app.is_safe_endpoint_url = counting
        try:
            alarm_app._SAFE_CANDIDATE_CACHE.clear()
            _filter_safe_candidates(["http://8.8.8.8:9090"])
            _filter_safe_candidates(["http://8.8.8.8:9090"])
        finally:
            alarm_app.is_safe_endpoint_url = real
        self.assertEqual(calls["n"], 1)


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()
        with alarm_app._RATE_BUCKETS_LOCK:
            alarm_app._RATE_BUCKETS.clear()

    def test_mutation_route_returns_429_over_limit(self):
        # GET /api/telegram is rate_limit(20, 60) and does no network I/O.
        last_status = None
        for _ in range(21):
            res = self.client.get('/api/telegram', headers={"X-API-Key": TEST_API_KEY})
            last_status = res.status_code
        self.assertEqual(last_status, 429)

    def test_rate_limit_is_scoped_per_route(self):
        # Exhausting one route's budget must not affect a different route.
        for _ in range(21):
            self.client.get('/api/telegram', headers={"X-API-Key": TEST_API_KEY})
        res = self.client.get('/instances')
        self.assertNotEqual(res.status_code, 429)

    def test_pruning_does_not_evict_other_routes_active_window(self):
        # A route with a long per_seconds window must survive a prune pass
        # triggered by a *different* route with a short per_seconds -- the
        # prune must judge each bucket's own staleness, not recompute a
        # window index using whichever route happened to trigger it.
        now = time.time()
        long_key = ("slow_route", "ip:test", int(now // 3600), 3600)  # 1h window, fresh
        with alarm_app._RATE_BUCKETS_LOCK:
            alarm_app._RATE_BUCKETS.clear()
            alarm_app._RATE_BUCKETS[long_key] = 5
            alarm_app._RATE_LAST_PRUNE[0] = 0.0  # force the next call to prune

        @alarm_app.rate_limit(100, 10)  # a short-window route
        def fast_route():
            return "ok"
        with app.test_request_context('/'):
            fast_route()

        with alarm_app._RATE_BUCKETS_LOCK:
            self.assertIn(long_key, alarm_app._RATE_BUCKETS, "still-active long-window bucket must survive a short-window route's prune")
            self.assertEqual(alarm_app._RATE_BUCKETS[long_key], 5)


if __name__ == '__main__':
    unittest.main()
