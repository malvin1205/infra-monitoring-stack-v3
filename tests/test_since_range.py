"""'/api/availability/data-range' — resolves the "Since start" range to a
minutes span from the oldest recorded telemetry to now."""
import os
import time
import unittest
from unittest.mock import patch

import storage
import app as alarm_app


class TestSinceRange(unittest.TestCase):
    def setUp(self):
        self.client = alarm_app.app.test_client()

    def test_null_when_nothing_recorded(self):
        with patch.object(storage.AvailabilityBucketRepository, "get_earliest_bucket_start", return_value=None), \
             patch.object(storage.EndpointRepository, "get_active_endpoint", return_value=None):
            d = self.client.get("/api/availability/data-range").get_json()
        self.assertTrue(d["ok"])
        self.assertIsNone(d["since_ts"])
        self.assertIsNone(d["minutes"])

    def test_span_from_earliest_bucket(self):
        three_h_ago = time.time() - 3 * 3600
        with patch.object(storage.AvailabilityBucketRepository, "get_earliest_bucket_start", return_value=three_h_ago):
            d = self.client.get("/api/availability/data-range").get_json()
        self.assertTrue(d["ok"])
        self.assertAlmostEqual(d["minutes"], 180, delta=2)

    def test_endpoint_created_at_fallback_when_no_buckets(self):
        two_h_ago = time.time() - 2 * 3600
        with patch.object(storage.AvailabilityBucketRepository, "get_earliest_bucket_start", return_value=None), \
             patch.object(storage.EndpointRepository, "get_active_endpoint",
                          return_value={"created_at": two_h_ago}):
            d = self.client.get("/api/availability/data-range").get_json()
        self.assertAlmostEqual(d["minutes"], 120, delta=2)

    def test_ceiling_clamps_to_366d(self):
        two_years_ago = time.time() - 730 * 86400
        with patch.object(storage.AvailabilityBucketRepository, "get_earliest_bucket_start", return_value=two_years_ago):
            d = self.client.get("/api/availability/data-range").get_json()
        self.assertLessEqual(d["minutes"], 366 * 1440)


if __name__ == "__main__":
    unittest.main()
