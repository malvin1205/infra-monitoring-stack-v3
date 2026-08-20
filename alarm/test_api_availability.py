import json
import time
import pytest
import app as app_module
from app import app


@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client


def test_availability_route_shape_and_sync(client, monkeypatch):
    """/api/availability must not crash, must return every key the frontend
    consumes, and 'overall' must equal fleet_aggregate (single source of truth)."""
    now = time.time()
    monkeypatch.setattr(app_module, 'get_monitored_instances', lambda job_filter=None: ['host-a', 'host-b'])
    monkeypatch.setattr(app_module, 'load_json', lambda path, default=None: [])

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

    monkeypatch.setattr(app_module, 'fetch_prom_query_map', fake_query_map)

    res = client.get('/api/availability?minutes=60')
    assert res.status_code == 200
    data = json.loads(res.data)
    assert data['ok'] is True

    for key in ('overall', 'fleet_aggregate', 'fleet_average', 'sla_compliance',
                'sla_compliance_ratio', 'zero_downtime_ratio', 'health_ratio',
                'per_server', 'lowest_availability', 'entries', 'targets', 'analytics', 'counts'):
        assert key in data, f"missing key: {key}"

    # Single source of truth: the headline card number == fleet_aggregate.
    assert data['overall'] == data['fleet_aggregate']['value']

    # Live counts reflect the live up/down state, not the historical average.
    assert data['counts']['online'] == 1
    assert data['counts']['offline'] == 1

    ids = {e['id'] for e in data['entries']}
    assert ids == {'host-a', 'host-b'}


def test_availability_route_empty_fleet_is_graceful(client, monkeypatch):
    monkeypatch.setattr(app_module, 'get_monitored_instances', lambda job_filter=None: [])
    monkeypatch.setattr(app_module, 'load_json', lambda path, default=None: [])
    monkeypatch.setattr(app_module, 'fetch_prom_query_map', lambda *a, **k: {})

    res = client.get('/api/availability?minutes=60&job=doesnotexist')
    assert res.status_code == 200
    data = json.loads(res.data)
    assert data['ok'] is True
    assert data['overall'] is None
    assert data['counts']['total'] == 0
    assert data['entries'] == []


@pytest.mark.parametrize("minutes_val", [60, 1440, 10080, 43200])
def test_availability_route_time_windows(client, monkeypatch, minutes_val):
    """Test 1h, 24h, 7d, and 30d window parameters."""
    monkeypatch.setattr(app_module, 'get_monitored_instances', lambda job_filter=None: ['srv-1', 'srv-2'])
    monkeypatch.setattr(app_module, 'load_json', lambda path, default=None: [])

    # srv-1 100% up (30 samples/min for 2s scrape), srv-2 50% up
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

    monkeypatch.setattr(app_module, 'fetch_prom_query_map', fake_queries)

    res = client.get(f'/api/availability?minutes={minutes_val}')
    assert res.status_code == 200
    data = json.loads(res.data)
    assert data['ok'] is True
    assert data['period_minutes'] == float(minutes_val)
    # srv-1: 100%, srv-2: 50% -> unweighted avg = 75%, aggregate = 75%
    assert data['fleet_average']['value'] == 75.0
    assert data['fleet_aggregate']['value'] == 75.0
    assert data['analytics']['total_incidents'] == 1
    assert data['analytics']['mean_outage_minutes'] > 0

