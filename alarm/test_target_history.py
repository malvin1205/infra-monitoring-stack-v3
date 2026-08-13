import json
import time
import pytest
from app import app, save_json

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

def test_target_history_missing_target(client):
    res = client.get('/api/target-history')
    assert res.status_code == 400
    data = json.loads(res.data)
    assert data['ok'] is False

def test_target_history_ranges(client, tmp_path, monkeypatch):
    now = time.time()
    dummy_logs = [
        {"time": now - 3600, "instance": "192.168.1.1", "latency_ms": 12.5, "event": "resolved"},
        {"time": now - 86400 * 2, "instance": "192.168.1.1", "latency_ms": 25.0, "event": "resolved"},
        {"time": now - 86400 * 15, "instance": "192.168.1.1", "latency_ms": 40.0, "event": "resolved"},
    ]
    test_logs_file = str(tmp_path / "logs.json")
    save_json(test_logs_file, dummy_logs)
    monkeypatch.setattr('app.LOGS_FILE', test_logs_file)
    monkeypatch.setattr('app.fetch_prometheus_json', lambda path, **kwargs: (None, ''))

    # Test 24h (1440m)
    res24h = client.get('/api/target-history?target=192.168.1.1&minutes=1440')
    assert res24h.status_code == 200
    data24h = json.loads(res24h.data)
    assert data24h['ok'] is True
    assert data24h['period_minutes'] == 1440
    # Should only include points within 24h (1 hour ago)
    assert len(data24h['latency_points']) == 1

    # Test 7d (10080m)
    res7d = client.get('/api/target-history?target=192.168.1.1&minutes=10080')
    assert res7d.status_code == 200
    data7d = json.loads(res7d.data)
    assert data7d['ok'] is True
    assert data7d['period_minutes'] == 10080
    # Should include 1 hour ago and 2 days ago
    assert len(data7d['latency_points']) == 2

    # Test 30d (43200m)
    res30d = client.get('/api/target-history?target=192.168.1.1&minutes=43200')
    assert res30d.status_code == 200
    data30d = json.loads(res30d.data)
    assert data30d['ok'] is True
    assert data30d['period_minutes'] == 43200
    # Should include all 3 points
    assert len(data30d['latency_points']) == 3
