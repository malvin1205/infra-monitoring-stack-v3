import json
import time
import os
import tempfile
import shutil
import unittest
from unittest.mock import patch

import app as alarm_app
from app import app, _WEBHOOK_LOCK
from conftest import TEST_API_KEY, TEST_WEBHOOK_SECRET


class ApiContractsTests(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()
        self.client.environ_base = {"HTTP_X_API_KEY": TEST_API_KEY, "HTTP_X_WEBHOOK_SECRET": TEST_WEBHOOK_SECRET}
        self.tmpdir = tempfile.mkdtemp()
        self._orig = (
            alarm_app.STATUS_FILE, alarm_app.MAINTENANCE_FILE,
            alarm_app.DEPENDENCIES_FILE, alarm_app.DELETED_TARGETS_FILE,
            alarm_app.ENDPOINTS_FILE
        )
        self._orig_targets_env = os.environ.get("TARGETS_FILE")
        alarm_app.STATUS_FILE = os.path.join(self.tmpdir, "status.json")
        alarm_app.MAINTENANCE_FILE = os.path.join(self.tmpdir, "maintenance.json")
        alarm_app.DEPENDENCIES_FILE = os.path.join(self.tmpdir, "dependencies.json")
        alarm_app.DELETED_TARGETS_FILE = os.path.join(self.tmpdir, "deleted_targets.json")
        alarm_app.ENDPOINTS_FILE = os.path.join(self.tmpdir, "endpoints.json")
        os.environ["TARGETS_FILE"] = os.path.join(self.tmpdir, "websites.yml")
        alarm_app._ENDPOINTS_CACHE["data"] = None
        alarm_app._EP_STATUS_CACHE["data"] = None

    def tearDown(self):
        alarm_app._ENDPOINTS_CACHE["data"] = None
        alarm_app._EP_STATUS_CACHE["data"] = None
        (
            alarm_app.STATUS_FILE, alarm_app.MAINTENANCE_FILE,
            alarm_app.DEPENDENCIES_FILE, alarm_app.DELETED_TARGETS_FILE,
            alarm_app.ENDPOINTS_FILE
        ) = self._orig
        if self._orig_targets_env is not None:
            os.environ["TARGETS_FILE"] = self._orig_targets_env
        else:
            os.environ.pop("TARGETS_FILE", None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ── 1. Target Management Contracts ───────────────────────────────────────
    def test_api_targets_crud_contracts(self):
        # GET empty targets
        res = self.client.get('/api/targets')
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertTrue(data['ok'])
        self.assertIsInstance(data['targets'], list)

        # POST invalid target (empty)
        res = self.client.post('/api/targets', json={"url": ""})
        self.assertEqual(res.status_code, 400)
        data = json.loads(res.data)
        self.assertFalse(data['ok'])
        self.assertIn("error", data)

        # POST invalid target format
        res = self.client.post('/api/targets', json={"url": "123"})
        self.assertEqual(res.status_code, 400)
        data = json.loads(res.data)
        self.assertFalse(data['ok'])

        # POST valid target
        res = self.client.post('/api/targets', json={"url": "example.com"})
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertTrue(data['ok'])
        self.assertIn("example.com", data['targets'])

        # DELETE empty target
        res = self.client.delete('/api/targets', json={"url": ""})
        self.assertEqual(res.status_code, 400)

        # DELETE valid target
        res = self.client.delete('/api/targets', json={"url": "example.com"})
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertTrue(data['ok'])

    # ── 2. Endpoint Management Contracts ─────────────────────────────────────
    def test_api_endpoints_crud_contracts(self):
        # Seed initial endpoints
        with open(alarm_app.ENDPOINTS_FILE, 'w') as f:
            json.dump({"active": "http://prom-1:9090", "endpoints": ["http://prom-1:9090", "http://prom-2:9090"]}, f)

        # GET endpoints
        with patch('app.fetch_url', return_value='{}'):
            res = self.client.get('/api/endpoints')
            self.assertEqual(res.status_code, 200)
            data = json.loads(res.data)
            self.assertTrue(data['ok'])
            self.assertEqual(data['active'], 'http://prom-1:9090')
            self.assertEqual(len(data['endpoints']), 2)

        # POST invalid URL format
        res = self.client.post('/api/endpoints', json={"url": ""})
        self.assertEqual(res.status_code, 400)
        self.assertFalse(json.loads(res.data)['ok'])

        # POST restricted network URL
        res = self.client.post('/api/endpoints', json={"url": "http://169.254.169.254/latest"})
        self.assertEqual(res.status_code, 400)
        self.assertFalse(json.loads(res.data)['ok'])

        # POST valid URL
        res = self.client.post('/api/endpoints', json={"url": "http://prom-3:9090", "set_active": True})
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertTrue(data['ok'])
        self.assertEqual(data['active'], "http://prom-3:9090")
        self.assertIn("http://prom-3:9090", data['endpoints'])

        # POST select endpoint
        res = self.client.post('/api/endpoints/select', json={"url": "http://prom-1:9090"})
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertTrue(data['ok'])
        self.assertEqual(data['active'], "http://prom-1:9090")

        # DELETE non-existent endpoint
        res = self.client.delete('/api/endpoints', json={"url": "http://unknown:9090"})
        self.assertEqual(res.status_code, 404)
        self.assertFalse(json.loads(res.data)['ok'])

        # DELETE endpoint
        res = self.client.delete('/api/endpoints', json={"url": "http://prom-3:9090"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(json.loads(res.data)['ok'])

    # ── 3. Maintenance Windows Contracts ─────────────────────────────────────
    def test_api_maintenance_crud_contracts(self):
        # GET empty windows
        res = self.client.get('/api/maintenance')
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertTrue(data['ok'])
        self.assertEqual(data['windows'], [])

        # POST missing target
        res = self.client.post('/api/maintenance', json={"target": ""})
        self.assertEqual(res.status_code, 400)
        self.assertFalse(json.loads(res.data)['ok'])

        # POST invalid timestamps (end <= start)
        res = self.client.post('/api/maintenance', json={"target": "srv-1", "start": 2000, "end": 1000})
        self.assertEqual(res.status_code, 400)
        self.assertFalse(json.loads(res.data)['ok'])

        # POST valid window
        now = int(time.time())
        res = self.client.post('/api/maintenance', json={"target": "srv-1", "start": now, "end": now + 3600, "reason": "Upgrade"})
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertTrue(data['ok'])
        window_id = data['window']['id']

        # DELETE valid window
        res = self.client.delete(f'/api/maintenance/{window_id}')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(json.loads(res.data)['ok'])

        # DELETE non-existent window
        res = self.client.delete(f'/api/maintenance/mw_nonexistent')
        self.assertEqual(res.status_code, 404)
        self.assertFalse(json.loads(res.data)['ok'])

    # ── 4. Dependency Contracts ──────────────────────────────────────────────
    def test_api_dependencies_crud_contracts(self):
        # GET dependencies
        res = self.client.get('/api/dependencies')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(json.loads(res.data)['ok'])

        # POST self-dependency (forbidden)
        res = self.client.post('/api/dependencies', json={"child": "srv-1", "parent": "srv-1"})
        self.assertEqual(res.status_code, 400)
        self.assertFalse(json.loads(res.data)['ok'])

        # POST valid dependency
        res = self.client.post('/api/dependencies', json={"child": "srv-app", "parent": "srv-db"})
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertTrue(data['ok'])
        dep_id = data['dependency']['id']

        # DELETE dependency
        res = self.client.delete(f'/api/dependencies/{dep_id}')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(json.loads(res.data)['ok'])

        # DELETE non-existent dependency
        res = self.client.delete(f'/api/dependencies/dep_nonexistent')
        self.assertEqual(res.status_code, 404)
        self.assertFalse(json.loads(res.data)['ok'])

    # ── 5. Target History Contracts ──────────────────────────────────────────
    def test_api_target_history_contracts(self):
        # Missing target parameter
        res = self.client.get('/api/target-history')
        self.assertEqual(res.status_code, 400)
        self.assertFalse(json.loads(res.data)['ok'])

        # Invalid target parameter with injection attempt
        res = self.client.get('/api/target-history?target=foo"}%20or%20up%20{')
        self.assertEqual(res.status_code, 400)
        self.assertFalse(json.loads(res.data)['ok'])

        # Valid target parameter with mocked empty Prometheus
        with patch('app.fetch_prometheus_json', return_value=({"status": "success", "data": {"result": []}}, "http://prom:9090")):
            res = self.client.get('/api/target-history?target=srv-node-1&minutes=60')
            self.assertEqual(res.status_code, 200)
            data = json.loads(res.data)
            self.assertTrue(data['ok'])
            self.assertEqual(data['target'], 'srv-node-1')
            self.assertIsInstance(data['events'], list)
            self.assertIsInstance(data['latency_points'], list)

    # ── 6. Health & Diagnostics Contracts ────────────────────────────────────
    def test_health_endpoints_contracts(self):
        # /health/live
        res = self.client.get('/health/live')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(json.loads(res.data)['status'], 'alive')

        # /health/ready
        res = self.client.get('/health/ready')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(json.loads(res.data)['status'], 'ready')

        # /health
        with patch('app.fetch_prometheus_json', return_value=({"status": "success"}, "http://prom:9090")):
            res = self.client.get('/health')
            self.assertEqual(res.status_code, 200)
            data = json.loads(res.data)
            self.assertTrue(data['ok'])
            self.assertIn('components', data)
            self.assertIn('prometheus', data['components'])
            self.assertIn('monitoring_api', data['components'])
            self.assertIn('storage', data['components'])

    # ── 7. Jobs & Discovered Targets Diagnostics ─────────────────────────────
    def test_jobs_and_discovered_targets_contracts(self):
        # /api/jobs
        with patch('app.fetch_prometheus_json', return_value=({
            "status": "success",
            "data": {"activeTargets": [{"labels": {"job": "node_exporter"}}]}
        }, "http://prom:9090")):
            res = self.client.get('/api/jobs')
            self.assertEqual(res.status_code, 200)
            data = json.loads(res.data)
            self.assertTrue(data['ok'])
            self.assertIn("node_exporter", data['jobs'])

        # /api/prometheus-targets
        with patch('app.fetch_prometheus_json', return_value=({
            "status": "success",
            "data": {"activeTargets": [{"labels": {"instance": "10.0.0.1:9100", "job": "node"}}]}
        }, "http://prom:9090")):
            res = self.client.get('/api/prometheus-targets')
            self.assertEqual(res.status_code, 200)
            data = json.loads(res.data)
            self.assertTrue(data['ok'])
            self.assertIsInstance(data['targets'], list)
            self.assertEqual(data['targets'][0]['instance'], '10.0.0.1:9100')

    # ── 8. Security Headers & Global Error Handlers ──────────────────────────
    def test_security_headers_and_error_handlers(self):
        # Security headers
        res = self.client.get('/api/targets')
        self.assertEqual(res.headers.get('X-Content-Type-Options'), 'nosniff')
        self.assertEqual(res.headers.get('X-Frame-Options'), 'SAMEORIGIN')
        self.assertEqual(res.headers.get('Referrer-Policy'), 'strict-origin-when-cross-origin')

        # 404 for API route returns JSON
        res = self.client.get('/api/nonexistent-endpoint')
        self.assertEqual(res.status_code, 404)
        data = json.loads(res.data)
        self.assertFalse(data['ok'])
        self.assertEqual(data['error'], 'Resource not found')

        # 405 for Method Not Allowed on API route
        res = self.client.put('/api/targets')
        self.assertEqual(res.status_code, 405)
        data = json.loads(res.data)
        self.assertFalse(data['ok'])
        self.assertEqual(data['error'], 'Method not allowed')

    def test_ssrf_extended_vectors_rejected(self):
        dangerous_urls = [
            "http://169.254.169.254/latest/meta-data/",
            "http://100.100.100.200/latest",
            "http://metadata.google.internal/computeMetadata/v1/",
            "http://[fd00:ec2::254]/latest",
            "http://user:pass@192.168.1.1:9090",
            "ftp://prometheus.internal:9090",
            "gopher://prometheus.internal:9090",
            "http://instance-data/latest",
        ]
        for url in dangerous_urls:
            res = self.client.post('/api/endpoints', json={"url": url})
            self.assertEqual(res.status_code, 400, f"Expected 400 rejection for SSRF URL: {url}")
            data = json.loads(res.data)
            self.assertFalse(data['ok'])

    def test_sqlite_persistence_api_integration(self):
        from storage import MaintenanceRepository, DependencyRepository, EndpointRepository
        # Test that POST /api/maintenance writes to SQLite MaintenanceRepository
        now = time.time()
        res = self.client.post('/api/maintenance', json={
            "target": "srv-db-prod-01",
            "scope": "instance",
            "reason": "kernel upgrade",
            "start": now - 10,
            "end": now + 600
        })
        self.assertEqual(res.status_code, 200)
        win_id = json.loads(res.data)['window']['id']

        # Verify record exists in SQLite
        active_maint = MaintenanceRepository.get_active_maintenance("srv-db-prod-01")
        self.assertIsNotNone(active_maint)
        self.assertEqual(active_maint['target'], "srv-db-prod-01")

        # Delete through API and verify removed from SQLite
        del_res = self.client.delete(f'/api/maintenance/{win_id}')
        self.assertEqual(del_res.status_code, 200)
        self.assertIsNone(MaintenanceRepository.get_active_maintenance("srv-db-prod-01"))


if __name__ == '__main__':
    unittest.main()
