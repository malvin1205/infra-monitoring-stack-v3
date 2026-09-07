"""'/api/availability/data-range' — resolves the "Max history" range to a
minutes span from the oldest bucket still in the archive to now, capped at
bucket retention (AVAIL_BUCKET_RETENTION_SECONDS)."""
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

    def test_clamps_to_bucket_retention(self):
        # Archive is pruned to AVAIL_BUCKET_RETENTION_SECONDS, so a since_ts
        # older than that (stale clock, created_at fallback) must not stretch
        # the window past what the data can back.
        two_years_ago = time.time() - 730 * 86400
        retention_min = alarm_app.AVAIL_BUCKET_RETENTION_SECONDS // 60
        with patch.object(storage.AvailabilityBucketRepository, "get_earliest_bucket_start", return_value=two_years_ago):
            d = self.client.get("/api/availability/data-range").get_json()
        self.assertLessEqual(d["minutes"], retention_min)
        self.assertGreaterEqual(d["minutes"], retention_min - 2)
        self.assertAlmostEqual(d["since_ts"], time.time() - alarm_app.AVAIL_BUCKET_RETENTION_SECONDS, delta=120)


if __name__ == "__main__":
    unittest.main()
