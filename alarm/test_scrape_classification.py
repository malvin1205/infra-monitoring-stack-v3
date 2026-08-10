import unittest
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
os.environ["DISABLE_ALERT_POLLER"] = "1"  # don't spin up the live network poller during tests

try:
    import app as alarm_app
except ImportError:
    from alarm import app as alarm_app


class ClassifyScrapeFailureTests(unittest.TestCase):
    """Pure-function tests for the single scrape-failure classifier — no
    network, no files. Covers the categories from the scrape-classification
    audit: DNS, No Route, Refused, Timeout, TLS, HTTP <code>, Unknown."""

    def test_up_has_no_category(self):
        result = alarm_app.classify_scrape_failure('up', '', None)
        self.assertIsNone(result['category'])

    def test_no_probe_data_is_unknown_not_up(self):
        result = alarm_app.classify_scrape_failure('unknown', '', None)
        self.assertEqual(result['category'], 'Unknown')

    def test_dns_failure(self):
        result = alarm_app.classify_scrape_failure(
            'down', 'dial tcp: lookup nodeexporter on 127.0.0.11:53: no such host', None)
        self.assertEqual(result['category'], 'DNS')

    def test_no_route_to_host(self):
        result = alarm_app.classify_scrape_failure(
            'down', 'dial tcp 192.168.9.101:9100: connect: no route to host', None)
        self.assertEqual(result['category'], 'No Route')

    def test_connection_refused(self):
        result = alarm_app.classify_scrape_failure(
            'down', 'dial tcp 192.168.9.99:9100: connect: connection refused', None)
        self.assertEqual(result['category'], 'Refused')

    def test_timeout(self):
        result = alarm_app.classify_scrape_failure('down', 'context deadline exceeded', None)
        self.assertEqual(result['category'], 'Timeout')

    def test_tls_error(self):
        result = alarm_app.classify_scrape_failure(
            'down', 'x509: certificate signed by unknown authority', None)
        self.assertEqual(result['category'], 'TLS')

    def test_http_error_from_status_code_when_no_last_error(self):
        # Typical blackbox/probe-job shape: Prometheus->blackbox_exporter
        # scrape succeeds (lastError empty), the real failure only shows up
        # as a non-2xx probe_http_status_code.
        result = alarm_app.classify_scrape_failure('down', '', 500)
        self.assertEqual(result['category'], 'HTTP 500')

    def test_unknown_when_nothing_to_go_on(self):
        result = alarm_app.classify_scrape_failure('down', '', None)
        self.assertEqual(result['category'], 'Unknown')


class IsValidTargetTests(unittest.TestCase):
    def test_bare_docker_hostname_is_valid(self):
        self.assertTrue(alarm_app.is_valid_target('http://nginx'))

    def test_ipv4_is_valid(self):
        self.assertTrue(alarm_app.is_valid_target('192.168.9.16'))

    def test_domain_is_valid(self):
        self.assertTrue(alarm_app.is_valid_target('https://example.com'))

    def test_bare_number_is_rejected(self):
        self.assertFalse(alarm_app.is_valid_target('129391283912'))

    def test_empty_is_rejected(self):
        self.assertFalse(alarm_app.is_valid_target(''))


if __name__ == '__main__':
    unittest.main()
