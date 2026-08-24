import json
import time
import os
import tempfile
import shutil
import unittest
from unittest.mock import patch

import app as app_module
from app import app, save_json


class TargetHistoryTests(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()
        self.tmpdir = tempfile.mkdtemp()
        self._orig_logs = app_module.LOGS_FILE
        self.test_logs_file = os.path.join(self.tmpdir, "logs.json")
        app_module.LOGS_FILE = self.test_logs_file

    def tearDown(self):
        app_module.LOGS_FILE = self._orig_logs
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_target_history_missing_target(self):
        res = self.client.get('/api/target-history')
        self.assertEqual(res.status_code, 400)
        data = json.loads(res.data)
        self.assertFalse(data['ok'])

    def test_target_history_ranges(self):
        now = time.time()
        dummy_logs = [
            {"time": now - 3600, "instance": "192.168.1.1", "latency_ms": 12.5, "event": "resolved"},
            {"time": now - 86400 * 2, "instance": "192.168.1.1", "latency_ms": 25.0, "event": "resolved"},
            {"time": now - 86400 * 15, "instance": "192.168.1.1", "latency_ms": 40.0, "event": "resolved"},
        ]
        save_json(self.test_logs_file, dummy_logs)

        with patch.object(app_module, 'fetch_prometheus_json', return_value=(None, '')):
            # Test 24h (1440m)
            res24h = self.client.get('/api/target-history?target=192.168.1.1&minutes=1440')
            self.assertEqual(res24h.status_code, 200)
            data24h = json.loads(res24h.data)
            self.assertTrue(data24h['ok'])
            self.assertEqual(data24h['period_minutes'], 1440)
            # Should only include points within 24h (1 hour ago)
            self.assertEqual(len(data24h['latency_points']), 1)

            # Test 7d (10080m)
            res7d = self.client.get('/api/target-history?target=192.168.1.1&minutes=10080')
            self.assertEqual(res7d.status_code, 200)
            data7d = json.loads(res7d.data)
            self.assertTrue(data7d['ok'])
            self.assertEqual(data7d['period_minutes'], 10080)
            # Should include 1 hour ago and 2 days ago
            self.assertEqual(len(data7d['latency_points']), 2)

            # Test 30d (43200m)
            res30d = self.client.get('/api/target-history?target=192.168.1.1&minutes=43200')
            self.assertEqual(res30d.status_code, 200)
            data30d = json.loads(res30d.data)
            self.assertTrue(data30d['ok'])
            self.assertEqual(data30d['period_minutes'], 43200)
            # Should include all 3 points
            self.assertEqual(len(data30d['latency_points']), 3)

    def test_target_history_injection_rejection(self):
        # Injection attempts with quotes, brackets, semicolons, etc.
        malicious_targets = [
            'test" or probe_success or {',
            'http://example.com/api?q=1; DROP TABLE',
            'host`whoami`',
            'host$(id)',
            'foo{}'
        ]
        for bad_target in malicious_targets:
            res = self.client.get(f'/api/target-history?target={bad_target}')
            self.assertEqual(res.status_code, 400)
            data = json.loads(res.data)
            self.assertFalse(data['ok'])


if __name__ == '__main__':
    unittest.main()
