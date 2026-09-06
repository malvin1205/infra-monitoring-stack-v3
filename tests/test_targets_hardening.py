"""Regression checks for the websites.yml curation-list handling:
normalization/idempotency, corrupt-file safety, and honest API failure.
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import yaml

import app as alarm_app
from app import app, normalize_target, save_website_targets, load_website_targets
from conftest import TEST_API_KEY, TEST_WEBHOOK_SECRET


class NormalizeTargetTests(unittest.TestCase):
    def test_equivalent_forms_collapse(self):
        for a, b in [
            ("example.com", "EXAMPLE.com"),
            ("example.com", "http://example.com"),
            ("example.com", "https://example.com/"),
            ("example.com", "example.com:80"),
            ("example.com", "example.com:443"),
            ("10.0.0.1:9100", "10.0.0.1:9100"),
        ]:
            self.assertEqual(normalize_target(a), normalize_target(b), f"{a!r} vs {b!r}")

    def test_distinct_targets_stay_distinct(self):
        self.assertNotEqual(normalize_target("example.com"), normalize_target("example.org"))
        self.assertNotEqual(normalize_target("host:9100"), normalize_target("host:9090"))


class SaveWebsiteTargetsSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_env = os.environ.get("TARGETS_FILE")
        self.path = os.path.join(self.tmpdir, "websites.yml")
        os.environ["TARGETS_FILE"] = self.path
        with alarm_app._WEBSITE_TARGETS_CACHE_LOCK:
            alarm_app._WEBSITE_TARGETS_CACHE["key"] = None
            alarm_app._WEBSITE_TARGETS_CACHE["data"] = None

    def tearDown(self):
        if self._orig_env is not None:
            os.environ["TARGETS_FILE"] = self._orig_env
        else:
            os.environ.pop("TARGETS_FILE", None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_preserves_sibling_group(self):
        self._write(
            "- labels: {job: blackbox_http}\n  targets: [old]\n"
            "- labels: {job: blackbox_ping}\n  targets: [10.0.0.9]\n"
        )
        self.assertTrue(save_website_targets(["new"]))
        doc = yaml.safe_load(open(self.path, encoding="utf-8"))
        groups = {g["labels"]["job"]: g["targets"] for g in doc}
        self.assertEqual(groups["blackbox_http"], ["new"])
        self.assertEqual(groups["blackbox_ping"], ["10.0.0.9"])

    def test_non_list_file_is_not_clobbered(self):
        self._write("this is not a file_sd list\n")
        with self.assertRaises(Exception):
            save_website_targets(["x"])
        self.assertEqual(open(self.path, encoding="utf-8").read(), "this is not a file_sd list\n")

    def test_corrupt_yaml_is_not_clobbered(self):
        self._write("job: [unclosed\n")
        with self.assertRaises(Exception):
            save_website_targets(["x"])
        self.assertIn("unclosed", open(self.path, encoding="utf-8").read())

    def test_empty_file_self_heals(self):
        self._write("")
        self.assertTrue(save_website_targets(["a"]))
        self.assertEqual(load_website_targets(), ["a"])

    def test_backup_written(self):
        self._write("- labels: {job: blackbox_http}\n  targets: [old]\n")
        save_website_targets(["new"])
        self.assertTrue(os.path.exists(self.path + ".bak"))
        self.assertIn("old", open(self.path + ".bak", encoding="utf-8").read())


class AddTargetApiTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.client.environ_base = {"HTTP_X_API_KEY": TEST_API_KEY, "HTTP_X_WEBHOOK_SECRET": TEST_WEBHOOK_SECRET}
        self.tmpdir = tempfile.mkdtemp()
        self._orig_env = os.environ.get("TARGETS_FILE")
        os.environ["TARGETS_FILE"] = os.path.join(self.tmpdir, "websites.yml")
        with alarm_app._WEBSITE_TARGETS_CACHE_LOCK:
            alarm_app._WEBSITE_TARGETS_CACHE["key"] = None
            alarm_app._WEBSITE_TARGETS_CACHE["data"] = None
        self._p = patch.object(alarm_app, "fetch_prometheus_json", return_value=(None, None))
        self._p.start()

    def tearDown(self):
        self._p.stop()
        if self._orig_env is not None:
            os.environ["TARGETS_FILE"] = self._orig_env
        else:
            os.environ.pop("TARGETS_FILE", None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_add_is_idempotent_across_equivalent_forms(self):
        for u in ["example.com", "http://example.com", "EXAMPLE.com:80"]:
            res = self.client.post("/api/targets", json={"url": u})
            self.assertEqual(res.status_code, 200, res.data)
        targets = json.loads(self.client.get("/api/targets").data)["targets"]
        self.assertEqual(len([t for t in targets if normalize_target(t) == "example.com"]), 1)

    def test_delete_matches_equivalent_form(self):
        self.client.post("/api/targets", json={"url": "example.com"})
        res = self.client.delete("/api/targets", json={"url": "HTTP://example.com/"})
        self.assertEqual(res.status_code, 200)
        body = json.loads(self.client.get("/api/targets").data)
        self.assertNotIn("example.com", [normalize_target(t) for t in body["targets"]])
        self.assertIn("example.com", [normalize_target(d) for d in body["deleted"]])

    def test_api_reports_failure_when_save_raises(self):
        with patch.object(alarm_app, "save_website_targets", side_effect=RuntimeError("disk full")):
            res = self.client.post("/api/targets", json={"url": "newhost.example"})
        self.assertEqual(res.status_code, 500)
        self.assertFalse(json.loads(res.data)["ok"])

    def test_get_targets_exposes_deleted_list(self):
        self.assertIn("deleted", json.loads(self.client.get("/api/targets").data))


if __name__ == "__main__":
    unittest.main()
