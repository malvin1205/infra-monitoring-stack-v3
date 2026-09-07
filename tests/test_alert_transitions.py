import unittest
import sys
import os
import tempfile
import shutil
import time
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
os.environ["DISABLE_ALERT_POLLER"] = "1"  # don't spin up the live network poller during tests

try:
    import app as alarm_app
    import json_store
except ImportError:
    from alarm import app as alarm_app
    from alarm import json_store

from conftest import TEST_API_KEY, TEST_WEBHOOK_SECRET


class MaintenanceWindowsByInstanceTests(unittest.TestCase):
    """maintenance_windows_by_instance() carves planned downtime out of the
    SLA denominator (/api/availability) — a false match here silently hides
    real downtime from a target's availability figure."""

    def test_malformed_job_window_with_no_target_matches_nothing(self):
        # A window with no target/scope_target at all (legacy/corrupt record)
        # must not match every instance missing from job_map via None == None.
        windows = [{"scope": "job", "start": 0, "end": 9999999999}]
        result = alarm_app.maintenance_windows_by_instance(["host-a", "host-b"], job_map={}, windows=windows)
        self.assertEqual(result, {})

    def test_job_scope_still_matches_real_targets(self):
        windows = [{"scope": "job", "target": "blackbox", "start": 0, "end": 9999999999}]
        result = alarm_app.maintenance_windows_by_instance(
            ["host-a", "host-b"], job_map={"host-a": "blackbox", "host-b": "node"}, windows=windows)
        self.assertIn("host-a", result)
        self.assertNotIn("host-b", result)

    def test_instance_scope_unaffected(self):
        windows = [{"scope": "instance", "target": "host-a", "start": 0, "end": 9999999999}]
        result = alarm_app.maintenance_windows_by_instance(["host-a", "host-b"], job_map={}, windows=windows)
        self.assertIn("host-a", result)
        self.assertNotIn("host-b", result)


class ComputeStateTransitionsTests(unittest.TestCase):
    """Pure-function tests for the poller's transition detector — no network,
    no files. Covers all cold-start and steady-state transition cases:
      - Case A: Target first observed UP -> baseline established, no false alert.
      - Case B: Target first observed DOWN -> active outage transition emitted.
      - Case C: Restart while DOWN -> prior seeded state prevents duplicate incident.
      - Case D: Target remains DOWN across multiple cycles -> exactly one active incident.
      - Case E: Target recovers -> resolves incident with accurate duration.
      - Case F: Target goes DOWN again after recovery -> creates new distinct incident.
    """

    def test_case_a_first_observed_up_seeds_baseline_without_firing(self):
        transitions, state = alarm_app.compute_state_transitions({"host-a": "1"}, {})
        self.assertEqual(transitions, [])
        self.assertEqual(state, {"host-a": "up"})

    def test_case_b_first_observed_down_emits_cold_start_outage(self):
        transitions, state = alarm_app.compute_state_transitions({"host-a": "0"}, {})
        self.assertEqual(transitions, [("host-a", False)])
        self.assertEqual(state, {"host-a": "down"})

    def test_case_c_restart_while_target_down_with_seeded_state_emits_no_duplicate(self):
        # When seeded as 'down' from status.json at restart, next poll sees is_up=False
        # and emits no duplicate transition
        transitions, state = alarm_app.compute_state_transitions({"host-a": "0"}, {"host-a": "down"})
        self.assertEqual(transitions, [])
        self.assertEqual(state["host-a"], "down")

    def test_case_d_repeated_down_across_cycles_emits_no_transition(self):
        transitions, state = alarm_app.compute_state_transitions({"host-a": "0"}, {"host-a": "down"})
        self.assertEqual(transitions, [])
        self.assertEqual(state["host-a"], "down")

    def test_case_e_down_to_up_transition_detected_for_recovery(self):
        transitions, state = alarm_app.compute_state_transitions({"host-a": "1"}, {"host-a": "down"})
        self.assertEqual(transitions, [("host-a", True)])
        self.assertEqual(state["host-a"], "up")

    def test_case_f_target_goes_down_again_after_recovery(self):
        transitions, state = alarm_app.compute_state_transitions({"host-a": "0"}, {"host-a": "up"})
        self.assertEqual(transitions, [("host-a", False)])
        self.assertEqual(state["host-a"], "down")

    def test_no_change_when_up_yields_no_transition(self):
        transitions, state = alarm_app.compute_state_transitions({"host-a": "1"}, {"host-a": "up"})
        self.assertEqual(transitions, [])
        self.assertEqual(state["host-a"], "up")

    def test_missing_instance_this_tick_keeps_prior_state(self):
        # Prometheus temporarily has no series for an instance — must not be
        # treated as a transition or dropped from tracking.
        transitions, state = alarm_app.compute_state_transitions({}, {"host-a": "down"})
        self.assertEqual(transitions, [])
        self.assertEqual(state, {"host-a": "down"})


class ComputeSlowResponseTransitionsTests(unittest.TestCase):
    """Pure-function tests for the SlowResponse (warning) debounce gate.
    Response time is noisy — a single slow or single fast sample must never
    fire/resolve on its own, only debounce_n CONSECUTIVE samples one way.
    Same shape as ComputeStateTransitionsTests above, one alert type over."""

    THRESHOLD = {}  # empty -> every instance falls back to the 500ms default

    def _fn(self, readings, state, debounce_n=3):
        return alarm_app.compute_slow_response_transitions(readings, state, self.THRESHOLD, debounce_n)

    def test_single_slow_sample_does_not_fire(self):
        transitions, state = self._fn({"host-a": (True, 900)}, {})
        self.assertEqual(transitions, [])
        self.assertEqual(state["host-a"]["consec_slow"], 1)
        self.assertFalse(state["host-a"]["firing"])

    def test_two_slow_then_one_normal_then_two_slow_does_not_fire(self):
        # Exact scenario from the task: 2x slow, 1x normal (breaks the streak),
        # 2x slow again — never reaches 3 CONSECUTIVE, so never fires.
        state = {}
        sequence = [900, 900, 100, 900, 900]  # ms; threshold defaults to 500
        for rt in sequence:
            transitions, state = self._fn({"host-a": (True, rt)}, state)
            self.assertEqual(transitions, [], f"unexpected transition after sample {rt}ms")
        self.assertFalse(state["host-a"]["firing"])
        self.assertEqual(state["host-a"]["consec_slow"], 2)  # the trailing 2x slow, not reset to 0

    def test_three_consecutive_slow_fires_exactly_once(self):
        state = {}
        fires = []
        for rt in (900, 900, 900):
            transitions, state = self._fn({"host-a": (True, rt)}, state)
            fires.extend(transitions)
        self.assertEqual(fires, [("host-a", True)])  # exactly one fire, not three
        self.assertTrue(state["host-a"]["firing"])

        # A 4th consecutive slow sample must not re-fire (already firing).
        transitions, state = self._fn({"host-a": (True, 900)}, state)
        self.assertEqual(transitions, [])

    def test_resolve_also_needs_debounce_n_consecutive_fast_samples(self):
        state = {}
        for rt in (900, 900, 900):
            _, state = self._fn({"host-a": (True, rt)}, state)
        self.assertTrue(state["host-a"]["firing"])

        # 2x fast (not yet 3) must not resolve.
        for rt in (100, 100):
            transitions, state = self._fn({"host-a": (True, rt)}, state)
            self.assertEqual(transitions, [])
        self.assertTrue(state["host-a"]["firing"])

        # 3rd consecutive fast sample resolves.
        transitions, state = self._fn({"host-a": (True, 100)}, state)
        self.assertEqual(transitions, [("host-a", False)])
        self.assertFalse(state["host-a"]["firing"])

    def test_target_going_down_resolves_slow_response_immediately(self):
        # Down supersedes slow — no need to wait for 3 consecutive "fast"
        # samples that will never come while the target is down.
        state = {}
        for rt in (900, 900, 900):
            _, state = self._fn({"host-a": (True, rt)}, state)
        self.assertTrue(state["host-a"]["firing"])

        transitions, state = self._fn({"host-a": (False, None)}, state)
        self.assertEqual(transitions, [("host-a", False)])
        self.assertFalse(state["host-a"]["firing"])
        self.assertEqual(state["host-a"]["consec_slow"], 0)

    def test_per_instance_threshold_override(self):
        # A host with a 2000ms override isn't "slow" at 900ms even though
        # that's well above the 500ms global default.
        thresholds = {"host-a": 2000}
        state = {}
        for rt in (900, 900, 900):
            transitions, state = alarm_app.compute_slow_response_transitions(
                {"host-a": (True, rt)}, state, thresholds
            )
            self.assertEqual(transitions, [])
        self.assertFalse(state["host-a"]["firing"])


class RecordAlertEventTests(unittest.TestCase):
    """Exercises record_alert_event() — the function shared by /webhook and
    the poller — against real temp JSON files (not mocks), matching how it's
    actually used: read-modify-write status/logs/history.json."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig = (json_store.STATUS_FILE, json_store.HISTORY_FILE,
                      json_store.HISTORY_ARCHIVE_FILE, json_store.LOGS_FILE)
        json_store.STATUS_FILE = os.path.join(self.tmpdir, "status.json")
        json_store.HISTORY_FILE = os.path.join(self.tmpdir, "history.json")
        json_store.HISTORY_ARCHIVE_FILE = os.path.join(self.tmpdir, "history_archive.json")
        json_store.LOGS_FILE = os.path.join(self.tmpdir, "logs.json")
        # Isolate SQLite too — record_alert_event() also writes the incidents
        # table, and tests here (e.g. test_repeat_firing_notification_is_deduped)
        # intentionally leave an incident firing. Without this it leaked into
        # the shared session DB and broke count assertions elsewhere (audit F2).
        from storage import init_db
        self.db_path = os.path.join(self.tmpdir, "test.db")
        init_db(self.db_path)
        self._orig_db_env = os.environ.get("INFRAWATCH_DB_PATH")
        os.environ["INFRAWATCH_DB_PATH"] = self.db_path

    def tearDown(self):
        if self._orig_db_env is not None:
            os.environ["INFRAWATCH_DB_PATH"] = self._orig_db_env
        else:
            os.environ.pop("INFRAWATCH_DB_PATH", None)
        (json_store.STATUS_FILE, json_store.HISTORY_FILE,
         json_store.HISTORY_ARCHIVE_FILE, json_store.LOGS_FILE) = self._orig
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_firing_then_resolved_lifecycle(self):
        ok = alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="down", job="blackbox", event_time=1000.0, is_now_firing=True)
        self.assertTrue(ok)

        status = json_store.load_json(json_store.STATUS_FILE, {})
        self.assertEqual(status["status"], "CRITICAL")
        self.assertEqual(len(status["alerts"]), 1)

        history = json_store.load_json(json_store.HISTORY_FILE, [])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "firing")

        ok = alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="recovered", job="blackbox", event_time=1090.0, is_now_firing=False)
        self.assertTrue(ok)

        status = json_store.load_json(json_store.STATUS_FILE, {})
        self.assertEqual(status["status"], "NORMAL")
        self.assertEqual(status["alerts"], [])

        history = json_store.load_json(json_store.HISTORY_FILE, [])
        self.assertEqual(history[0]["status"], "resolved")
        self.assertEqual(history[0]["duration_seconds"], 90)

        logs = json_store.load_json(json_store.LOGS_FILE, [])
        self.assertEqual(len(logs), 2)
        self.assertEqual(logs[0]["event"], "resolved")   # newest first
        self.assertEqual(logs[0]["duration_seconds"], 90)
        self.assertEqual(logs[1]["event"], "firing")

    def test_repeat_firing_notification_is_deduped(self):
        alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="down", job="blackbox", event_time=1000.0, is_now_firing=True)
        again = alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="still down", job="blackbox", event_time=1010.0, is_now_firing=True)
        self.assertFalse(again)

        logs = json_store.load_json(json_store.LOGS_FILE, [])
        self.assertEqual(len(logs), 1)  # no duplicate row for the repeat notification

    def test_repeat_resolved_notification_is_deduped(self):
        ok = alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="down", job="blackbox", event_time=1000.0, is_now_firing=False)
        self.assertFalse(ok)  # was never firing — nothing to resolve
        logs = json_store.load_json(json_store.LOGS_FILE, [])
        self.assertEqual(logs, [])


class IncidentOccurrenceDedupTests(unittest.TestCase):
    """Incident History task #1: a host flapping N times must collapse into
    ONE incidents row with occurrences=N, not N separate rows all reading
    'occurrences x1'. Isolated SQLite (own INFRAWATCH_DB_PATH) since this
    exercises IncidentRepository directly, unlike RecordAlertEventTests above
    which only checks the JSON dual-write."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_files = (json_store.STATUS_FILE, json_store.HISTORY_FILE,
                             json_store.HISTORY_ARCHIVE_FILE, json_store.LOGS_FILE)
        json_store.STATUS_FILE = os.path.join(self.tmpdir, "status.json")
        json_store.HISTORY_FILE = os.path.join(self.tmpdir, "history.json")
        json_store.HISTORY_ARCHIVE_FILE = os.path.join(self.tmpdir, "history_archive.json")
        json_store.LOGS_FILE = os.path.join(self.tmpdir, "logs.json")

        self.db_path = os.path.join(self.tmpdir, "test.db")
        from storage import init_db
        init_db(self.db_path)
        self._orig_db_env = os.environ.get("INFRAWATCH_DB_PATH")
        os.environ["INFRAWATCH_DB_PATH"] = self.db_path

    def tearDown(self):
        (json_store.STATUS_FILE, json_store.HISTORY_FILE,
         json_store.HISTORY_ARCHIVE_FILE, json_store.LOGS_FILE) = self._orig_files
        if self._orig_db_env is not None:
            os.environ["INFRAWATCH_DB_PATH"] = self._orig_db_env
        else:
            os.environ.pop("INFRAWATCH_DB_PATH", None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_flapping_host_collapses_into_one_row_with_occurrences_count(self):
        from storage import IncidentRepository
        t = 1000.0
        for _ in range(5):
            ok_fire = alarm_app.record_alert_event(
                name="TargetDown", severity="critical", instance="10.0.0.5",
                summary="down", job="blackbox", event_time=t, is_now_firing=True)
            self.assertTrue(ok_fire)
            t += 60
            ok_resolve = alarm_app.record_alert_event(
                name="TargetDown", severity="critical", instance="10.0.0.5",
                summary="recovered", job="blackbox", event_time=t, is_now_firing=False)
            self.assertTrue(ok_resolve)
            t += 60  # gap of UP status before the next DOWN — a real new occurrence

        history = IncidentRepository.get_history(limit=10, db_path=self.db_path)
        self.assertEqual(len(history), 1)  # one row, not five
        self.assertEqual(history[0]["occurrences"], 5)
        self.assertEqual(history[0]["first_seen"], 1000.0)  # origin of the very first fire, not the last
        # "Total Down" is a cumulative aggregate (task #3), not just the
        # last occurrence's duration: 5 occurrences x 60s each = 300s total,
        # not 60s (which is all duration_seconds alone would show).
        self.assertEqual(history[0]["total_down_seconds"], 300.0)
        self.assertEqual(history[0]["duration_seconds"], 60.0)  # still just the last occurrence

    def test_repeat_firing_without_intervening_resolve_does_not_bump_occurrences(self):
        # Same DOWN condition re-reported (no UP gap in between) must stay
        # deduped, not counted as a second occurrence — matches the existing
        # idempotent-dedupe rule this repository already enforces.
        from storage import IncidentRepository
        alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.9",
            summary="down", job="blackbox", event_time=1000.0, is_now_firing=True)
        alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.9",
            summary="still down", job="blackbox", event_time=1010.0, is_now_firing=True)

        active = IncidentRepository.get_active_incidents(db_path=self.db_path)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["occurrences"], 1)

    def test_ongoing_incident_is_included_in_history_as_firing(self):
        from storage import IncidentRepository
        alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.7",
            summary="down", job="blackbox", event_time=1000.0, is_now_firing=True)

        history = IncidentRepository.get_history(limit=10, db_path=self.db_path)
        self.assertEqual(len(history), 1)  # previously: only resolved rows showed up here
        self.assertEqual(history[0]["status"], "firing")

    def test_ongoing_incident_survives_the_limit_even_when_stale(self):
        # A single "ORDER BY updated_at DESC LIMIT N" query would let enough
        # newer resolved incidents push a quiet-but-still-open incident out
        # of the window. get_history() must fetch firing rows unconditionally
        # and only cap the resolved ones — Ongoing must never just disappear.
        from storage import IncidentRepository
        alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="stale-ongoing",
            summary="down", job="blackbox", event_time=1000.0, is_now_firing=True)

        # 3 newer resolved incidents, all with a later updated_at than the
        # still-open one above.
        for i in range(3):
            inst = f"resolved-{i}"
            alarm_app.record_alert_event(
                name="TargetDown", severity="critical", instance=inst,
                summary="down", job="blackbox", event_time=2000.0 + i, is_now_firing=True)
            alarm_app.record_alert_event(
                name="TargetDown", severity="critical", instance=inst,
                summary="recovered", job="blackbox", event_time=2010.0 + i, is_now_firing=False)

        # limit=1 -> without the fix, "stale-ongoing" (oldest updated_at)
        # would be evicted by the 3 newer resolved rows.
        history = IncidentRepository.get_history(limit=1, db_path=self.db_path)
        keys = [h["key"] for h in history]
        self.assertIn("TargetDown|stale-ongoing", keys)
        ongoing_row = next(h for h in history if h["key"] == "TargetDown|stale-ongoing")
        self.assertEqual(ongoing_row["status"], "firing")
        # Resolved rows still respect the limit.
        resolved_rows = [h for h in history if h["status"] == "resolved"]
        self.assertEqual(len(resolved_rows), 1)


class AcknowledgmentPersistenceTests(unittest.TestCase):
    """'Acknowledged by <user>' must show up in both Incident History (durable
    incidents.acknowledged_by/at) and Live Alert Log (live-joined onto
    still-firing rows) — see AcknowledgmentRepository and
    _annotate_logs_with_acknowledgment in app.py."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_files = (json_store.STATUS_FILE, json_store.HISTORY_FILE,
                             json_store.HISTORY_ARCHIVE_FILE, json_store.LOGS_FILE)
        json_store.STATUS_FILE = os.path.join(self.tmpdir, "status.json")
        json_store.HISTORY_FILE = os.path.join(self.tmpdir, "history.json")
        json_store.HISTORY_ARCHIVE_FILE = os.path.join(self.tmpdir, "history_archive.json")
        json_store.LOGS_FILE = os.path.join(self.tmpdir, "logs.json")

        self.db_path = os.path.join(self.tmpdir, "test.db")
        from storage import init_db
        init_db(self.db_path)
        self._orig_db_env = os.environ.get("INFRAWATCH_DB_PATH")
        os.environ["INFRAWATCH_DB_PATH"] = self.db_path

    def tearDown(self):
        (json_store.STATUS_FILE, json_store.HISTORY_FILE,
         json_store.HISTORY_ARCHIVE_FILE, json_store.LOGS_FILE) = self._orig_files
        if self._orig_db_env is not None:
            os.environ["INFRAWATCH_DB_PATH"] = self._orig_db_env
        else:
            os.environ.pop("INFRAWATCH_DB_PATH", None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_acknowledge_persists_onto_the_firing_incident_row(self):
        from storage import IncidentRepository, AcknowledgmentRepository
        alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="down", job="blackbox", event_time=1000.0, is_now_firing=True)

        AcknowledgmentRepository.acknowledge_instances(["10.0.0.5"], username="alice", now=1005.0, db_path=self.db_path)

        active = IncidentRepository.get_active_incidents(db_path=self.db_path)
        self.assertEqual(active[0]["acknowledged_by"], "alice")
        self.assertEqual(active[0]["acknowledged_at"], 1005.0)

    def test_acknowledgment_survives_resolve_after_live_record_is_cleared(self):
        from storage import IncidentRepository, AcknowledgmentRepository
        alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="down", job="blackbox", event_time=1000.0, is_now_firing=True)
        AcknowledgmentRepository.acknowledge_instances(["10.0.0.5"], username="alice", now=1005.0, db_path=self.db_path)

        alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="recovered", job="blackbox", event_time=1100.0, is_now_firing=False)
        # The live table is what clear_resolved() would purge once the
        # instance is confirmed up elsewhere — simulate that happening.
        AcknowledgmentRepository.clear_resolved(active_down_instances=set(), db_path=self.db_path)
        self.assertEqual(AcknowledgmentRepository.get_active_acknowledgments(db_path=self.db_path), {})

        history = IncidentRepository.get_history(limit=10, db_path=self.db_path)
        self.assertEqual(history[0]["status"], "resolved")
        self.assertEqual(history[0]["acknowledged_by"], "alice")  # still there
        self.assertEqual(history[0]["acknowledged_at"], 1005.0)

    def test_unacknowledge_clears_the_incident_row(self):
        from storage import IncidentRepository, AcknowledgmentRepository
        alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="down", job="blackbox", event_time=1000.0, is_now_firing=True)
        AcknowledgmentRepository.acknowledge_instances(["10.0.0.5"], username="alice", db_path=self.db_path)
        AcknowledgmentRepository.unacknowledge_instance("10.0.0.5", db_path=self.db_path)

        active = IncidentRepository.get_active_incidents(db_path=self.db_path)
        self.assertIsNone(active[0]["acknowledged_by"])
        self.assertIsNone(active[0]["acknowledged_at"])

    def test_new_occurrence_starts_unacknowledged(self):
        # A fresh DOWN->UP->DOWN occurrence needs its own ack — it must not
        # silently inherit "already acked" from a previous occurrence.
        from storage import IncidentRepository, AcknowledgmentRepository
        alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="down", job="blackbox", event_time=1000.0, is_now_firing=True)
        AcknowledgmentRepository.acknowledge_instances(["10.0.0.5"], username="alice", db_path=self.db_path)
        alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="recovered", job="blackbox", event_time=1100.0, is_now_firing=False)

        alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="down again", job="blackbox", event_time=1200.0, is_now_firing=True)

        active = IncidentRepository.get_active_incidents(db_path=self.db_path)
        self.assertEqual(active[0]["occurrences"], 2)
        self.assertIsNone(active[0]["acknowledged_by"])

    def test_resolve_clears_the_live_ack_immediately_without_needing_a_poll(self):
        # The live alert_acknowledgments table used to only get cleared by a
        # poll-driven sweep (AcknowledgmentRepository.clear_resolved, called
        # from the /instances handler) — if nothing was polling /instances
        # (an unattended wallboard, say), a resolved instance's ack just sat
        # there. record_alert_event's resolve branch must clear it directly.
        from storage import AcknowledgmentRepository
        alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="down", job="blackbox", event_time=1000.0, is_now_firing=True)
        AcknowledgmentRepository.acknowledge_instances(["10.0.0.5"], username="alice", db_path=self.db_path)
        self.assertIn("10.0.0.5", AcknowledgmentRepository.get_active_acknowledgments(db_path=self.db_path))

        alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="recovered", job="blackbox", event_time=1100.0, is_now_firing=False)
        # No clear_resolved() call here — resolving alone must be enough.
        self.assertEqual(AcknowledgmentRepository.get_active_acknowledgments(db_path=self.db_path), {})

    def test_stale_ack_does_not_leak_into_a_brand_new_occurrence(self):
        # The exact race: ack -> resolve -> re-fire, with no /instances poll
        # (no clear_resolved()) running in between. Without the fix, the live
        # ack row from the first occurrence survives and silently marks a
        # nobody's-seen-it-yet new outage as already acknowledged.
        from storage import AcknowledgmentRepository, IncidentRepository
        alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="down", job="blackbox", event_time=1000.0, is_now_firing=True)
        AcknowledgmentRepository.acknowledge_instances(["10.0.0.5"], username="alice", db_path=self.db_path)
        alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="recovered", job="blackbox", event_time=1100.0, is_now_firing=False)

        alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="down again", job="blackbox", event_time=1200.0, is_now_firing=True)

        self.assertEqual(AcknowledgmentRepository.get_active_acknowledgments(db_path=self.db_path), {})
        active = IncidentRepository.get_active_incidents(db_path=self.db_path)
        self.assertIsNone(active[0]["acknowledged_by"])

    def test_ack_on_one_alert_type_does_not_leak_into_another_for_the_same_instance(self):
        # alert_acknowledgments is keyed by instance only, not by incident
        # key — a TargetDown ack must not silently mark an unrelated
        # SlowResponse incident on the SAME host as already acknowledged.
        from storage import AcknowledgmentRepository, IncidentRepository
        alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="down", job="blackbox", event_time=1000.0, is_now_firing=True)
        AcknowledgmentRepository.acknowledge_instances(["10.0.0.5"], username="alice", db_path=self.db_path)
        alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="recovered", job="blackbox", event_time=1100.0, is_now_firing=False)

        alarm_app.record_alert_event(
            name="SlowResponse", severity="warning", instance="10.0.0.5",
            summary="degraded", job="blackbox", event_time=1200.0, is_now_firing=True)

        active = IncidentRepository.get_active_incidents(db_path=self.db_path)
        slow = next(a for a in active if a["name"] == "SlowResponse")
        self.assertIsNone(slow["acknowledged_by"])

    def test_resolving_one_alert_type_does_not_unacknowledge_another_still_firing_one(self):
        # The other direction of the leak above: TWO alert types firing at
        # once on the same host, both covered by one instance-scoped ack.
        # Resolving one of them must not wipe the ack out from under the
        # other, still-firing, already-handled one.
        from storage import AcknowledgmentRepository, IncidentRepository
        alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="down", job="blackbox", event_time=1000.0, is_now_firing=True)
        alarm_app.record_alert_event(
            name="HighMemoryUsage", severity="warning", instance="10.0.0.5",
            summary="memory high", job="blackbox", event_time=1000.0, is_now_firing=True)
        AcknowledgmentRepository.acknowledge_instances(["10.0.0.5"], username="alice", db_path=self.db_path)

        # HighMemoryUsage resolves; TargetDown is still down.
        alarm_app.record_alert_event(
            name="HighMemoryUsage", severity="warning", instance="10.0.0.5",
            summary="memory normal", job="blackbox", event_time=1100.0, is_now_firing=False)

        self.assertIn("10.0.0.5", AcknowledgmentRepository.get_active_acknowledgments(db_path=self.db_path))
        active = IncidentRepository.get_active_incidents(db_path=self.db_path)
        target_down = next(a for a in active if a["name"] == "TargetDown")
        self.assertEqual(target_down["acknowledged_by"], "alice")  # untouched

    def test_logs_annotate_marks_only_still_firing_rows(self):
        with patch.object(alarm_app.AcknowledgmentRepository, 'get_active_acknowledgments',
                           return_value={"10.0.0.5": {"acknowledged_by": "alice", "acknowledged_at": 1005.0}}):
            rows = [
                {"event": "firing", "instance": "10.0.0.5"},
                {"event": "firing", "instance": "10.0.0.9"},  # not acked -> untouched
                {"event": "resolved", "instance": "10.0.0.5"},  # past event -> untouched even though instance matches
            ]
            alarm_app._annotate_logs_with_acknowledgment(rows)

        self.assertEqual(rows[0].get("acknowledged_by"), "alice")
        self.assertNotIn("acknowledged_by", rows[1])
        self.assertNotIn("acknowledged_by", rows[2])

    def test_logs_annotate_does_not_stamp_an_older_already_closed_episode(self):
        # event_logs is append-only — an instance that's flapped has several
        # historical 'firing' rows, only the newest of which (if not yet
        # followed by an even-newer 'resolved') is the actually-open one.
        # Rows arrive newest-first, so: firing (now, open) -> resolved (an
        # earlier close) -> firing (an even earlier, already-closed episode).
        with patch.object(alarm_app.AcknowledgmentRepository, 'get_active_acknowledgments',
                           return_value={"10.0.0.5": {"acknowledged_by": "alice", "acknowledged_at": 1005.0}}):
            rows = [
                {"event": "firing", "instance": "10.0.0.5", "time": 3000},    # current, open episode
                {"event": "resolved", "instance": "10.0.0.5", "time": 2000},  # closed a previous episode
                {"event": "firing", "instance": "10.0.0.5", "time": 1000},    # that previous episode's start
            ]
            alarm_app._annotate_logs_with_acknowledgment(rows)

        self.assertEqual(rows[0].get("acknowledged_by"), "alice")
        self.assertNotIn("acknowledged_by", rows[1])
        self.assertNotIn("acknowledged_by", rows[2])


class MaintenanceRecoveryReconciliationTests(unittest.TestCase):
    """A TargetDown incident that recovers WHILE under maintenance must not
    stay stuck 'Ongoing' forever once the window ends — see the comment in
    app.py's _poll_targets_once() maintenance-end loop for why
    compute_state_transitions() alone can't catch this case."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_files = (json_store.STATUS_FILE, json_store.HISTORY_FILE,
                             json_store.HISTORY_ARCHIVE_FILE, json_store.LOGS_FILE)
        json_store.STATUS_FILE = os.path.join(self.tmpdir, "status.json")
        json_store.HISTORY_FILE = os.path.join(self.tmpdir, "history.json")
        json_store.HISTORY_ARCHIVE_FILE = os.path.join(self.tmpdir, "history_archive.json")
        json_store.LOGS_FILE = os.path.join(self.tmpdir, "logs.json")

        self.db_path = os.path.join(self.tmpdir, "test.db")
        from storage import init_db
        init_db(self.db_path)
        self._orig_db_env = os.environ.get("INFRAWATCH_DB_PATH")
        os.environ["INFRAWATCH_DB_PATH"] = self.db_path

        # Isolate poller module-state from any other test/tick that may have
        # run in this process.
        self._orig_last_webhook = alarm_app._LAST_WEBHOOK_AT[0]
        alarm_app._LAST_WEBHOOK_AT[0] = 0.0
        self._orig_poller_state = dict(alarm_app._poller_state)
        self._orig_slow_state = dict(alarm_app._slow_poller_state)
        self._orig_maint_prev = set(alarm_app._maintenance_active_prev)
        alarm_app._poller_state.clear()
        alarm_app._slow_poller_state.clear()
        alarm_app._maintenance_active_prev.clear()

    def tearDown(self):
        (json_store.STATUS_FILE, json_store.HISTORY_FILE,
         json_store.HISTORY_ARCHIVE_FILE, json_store.LOGS_FILE) = self._orig_files
        if self._orig_db_env is not None:
            os.environ["INFRAWATCH_DB_PATH"] = self._orig_db_env
        else:
            os.environ.pop("INFRAWATCH_DB_PATH", None)
        alarm_app._LAST_WEBHOOK_AT[0] = self._orig_last_webhook
        alarm_app._poller_state.clear()
        alarm_app._poller_state.update(self._orig_poller_state)
        alarm_app._slow_poller_state.clear()
        alarm_app._slow_poller_state.update(self._orig_slow_state)
        alarm_app._maintenance_active_prev.clear()
        alarm_app._maintenance_active_prev.update(self._orig_maint_prev)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_recovery_during_maintenance_resolves_once_window_ends(self):
        from storage import IncidentRepository

        # Outage started BEFORE maintenance — a real firing incident exists.
        alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="down", job="blackbox", event_time=1000.0, is_now_firing=True)
        self.assertEqual(len(IncidentRepository.get_active_incidents(db_path=self.db_path)), 1)

        # Simulate: it was under maintenance last tick (poller never touched
        # it while maintained) and it quietly came back up during the window.
        alarm_app._maintenance_active_prev.add("10.0.0.5")

        with patch.object(alarm_app, 'get_monitored_instances', return_value=["10.0.0.5"]), \
             patch.object(alarm_app, 'fetch_all_probe_metrics', return_value=({"10.0.0.5": "1"}, {}, {})), \
             patch.object(alarm_app, 'load_maintenance_windows', return_value=[]):
            alarm_app._poll_targets_once()  # maintenance window has now ended

        active = IncidentRepository.get_active_incidents(db_path=self.db_path)
        self.assertEqual(active, [], "TargetDown must not stay stuck firing after a silent in-maintenance recovery")

        history = IncidentRepository.get_history(limit=10, db_path=self.db_path)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "resolved")

    def test_still_down_after_maintenance_does_not_duplicate_the_incident(self):
        from storage import IncidentRepository

        alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.6",
            summary="down", job="blackbox", event_time=1000.0, is_now_firing=True)
        alarm_app._maintenance_active_prev.add("10.0.0.6")

        with patch.object(alarm_app, 'get_monitored_instances', return_value=["10.0.0.6"]), \
             patch.object(alarm_app, 'fetch_all_probe_metrics', return_value=({"10.0.0.6": "0"}, {}, {})), \
             patch.object(alarm_app, 'load_maintenance_windows', return_value=[]), \
             patch.object(alarm_app, 'fetch_down_since_prom_map', return_value={"10.0.0.6": 0.0}):
            alarm_app._poll_targets_once()

        active = IncidentRepository.get_active_incidents(db_path=self.db_path)
        self.assertEqual(len(active), 1)  # still exactly one incident, not two
        self.assertEqual(active[0]["occurrences"], 1)  # no phantom re-fire counted

    def test_slow_response_recovering_during_maintenance_resolves_after_debounce(self):
        # SlowResponse's own version of the bug above: popping its debounce
        # state entirely at maintenance-end would make
        # compute_slow_response_transitions believe nothing was firing, so
        # it could never observe a firing->resolved change for an incident
        # that's genuinely still 'firing' in SQLite. Seeding firing=True from
        # the DB instead lets the normal debounce (3 consecutive fast polls)
        # resolve it correctly, no different from any other resolve.
        from storage import IncidentRepository
        alarm_app.record_alert_event(
            name="SlowResponse", severity="warning", instance="10.0.0.7",
            summary="degraded", job="blackbox", event_time=1000.0, is_now_firing=True)
        alarm_app._maintenance_active_prev.add("10.0.0.7")

        # 50ms — comfortably under the 500ms default threshold ("fast").
        with patch.object(alarm_app, 'get_monitored_instances', return_value=["10.0.0.7"]), \
             patch.object(alarm_app, 'fetch_all_probe_metrics', return_value=({"10.0.0.7": "1"}, {"10.0.0.7": 0.05}, {})), \
             patch.object(alarm_app, 'load_maintenance_windows', return_value=[]):
            alarm_app._poll_targets_once()  # maintenance ends; tick #1 fast
            self.assertEqual(len(IncidentRepository.get_active_incidents(db_path=self.db_path)), 1,
                              "must not resolve on the very first fast sample")

            alarm_app._poll_targets_once()  # tick #2 fast
            self.assertEqual(len(IncidentRepository.get_active_incidents(db_path=self.db_path)), 1)

            alarm_app._poll_targets_once()  # tick #3 fast -> debounce satisfied
            self.assertEqual(len(IncidentRepository.get_active_incidents(db_path=self.db_path)), 0)

        history = IncidentRepository.get_history(limit=10, db_path=self.db_path)
        self.assertEqual(history[0]["status"], "resolved")


class TelegramSeverityGateTests(unittest.TestCase):
    """record_alert_event() dispatches to Telegram for every severity —
    the min_severity gate (SlowResponse warnings held back by default) lives
    inside telegram_notifier._async_send_worker instead (see
    TelegramMinSeverityTests in test_telegram_alert.py), specifically so
    this shared function can't silently swallow a real Alertmanager
    severity="warning" alert too. Dashboard/history get every severity
    regardless of what Telegram does with it."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_files = (json_store.STATUS_FILE, json_store.HISTORY_FILE,
                             json_store.HISTORY_ARCHIVE_FILE, json_store.LOGS_FILE)
        json_store.STATUS_FILE = os.path.join(self.tmpdir, "status.json")
        json_store.HISTORY_FILE = os.path.join(self.tmpdir, "history.json")
        json_store.HISTORY_ARCHIVE_FILE = os.path.join(self.tmpdir, "history_archive.json")
        json_store.LOGS_FILE = os.path.join(self.tmpdir, "logs.json")

        self.db_path = os.path.join(self.tmpdir, "test.db")
        from storage import init_db
        init_db(self.db_path)
        self._orig_db_env = os.environ.get("INFRAWATCH_DB_PATH")
        os.environ["INFRAWATCH_DB_PATH"] = self.db_path

    def tearDown(self):
        (json_store.STATUS_FILE, json_store.HISTORY_FILE,
         json_store.HISTORY_ARCHIVE_FILE, json_store.LOGS_FILE) = self._orig_files
        if self._orig_db_env is not None:
            os.environ["INFRAWATCH_DB_PATH"] = self._orig_db_env
        else:
            os.environ.pop("INFRAWATCH_DB_PATH", None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_warning_severity_still_reaches_dispatch_alert_async(self):
        # Whether it actually SENDS is telegram_notifier's call (min_severity),
        # not record_alert_event's — it must not be filtered out this early.
        with patch.object(alarm_app, 'dispatch_alert_async') as mock_dispatch:
            alarm_app.record_alert_event(
                name="SlowResponse", severity="warning", instance="10.0.0.8",
                summary="degraded", job="blackbox", event_time=1000.0, is_now_firing=True)
            mock_dispatch.assert_called_once()

    def test_critical_severity_still_dispatches_telegram(self):
        with patch.object(alarm_app, 'dispatch_alert_async') as mock_dispatch:
            alarm_app.record_alert_event(
                name="TargetDown", severity="critical", instance="10.0.0.9",
                summary="down", job="blackbox", event_time=1000.0, is_now_firing=True)
            mock_dispatch.assert_called_once()

    def test_warning_incident_still_reaches_history(self):
        from storage import IncidentRepository
        alarm_app.record_alert_event(
            name="SlowResponse", severity="warning", instance="10.0.0.8",
            summary="degraded", job="blackbox", event_time=1000.0, is_now_firing=True)
        history = IncidentRepository.get_history(limit=10, db_path=self.db_path)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["severity"], "warning")


class MaintenanceModeTests(unittest.TestCase):
    """get_active_maintenance() matching + record_alert_event() suppression —
    the whole point of Phase 9 is that a maintenance window must not leave a
    trace in status/logs/history."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig = (json_store.STATUS_FILE, json_store.HISTORY_FILE,
                      json_store.HISTORY_ARCHIVE_FILE, json_store.LOGS_FILE)
        json_store.STATUS_FILE = os.path.join(self.tmpdir, "status.json")
        json_store.HISTORY_FILE = os.path.join(self.tmpdir, "history.json")
        json_store.HISTORY_ARCHIVE_FILE = os.path.join(self.tmpdir, "history_archive.json")
        json_store.LOGS_FILE = os.path.join(self.tmpdir, "logs.json")
        # get_active_maintenance()/record_alert_event() read maintenance
        # windows from SQLite (MaintenanceRepository) — isolate it too.
        self.db_path = os.path.join(self.tmpdir, "test.db")
        from storage import init_db
        init_db(self.db_path)
        self._orig_db_env = os.environ.get("INFRAWATCH_DB_PATH")
        os.environ["INFRAWATCH_DB_PATH"] = self.db_path

    def tearDown(self):
        (json_store.STATUS_FILE, json_store.HISTORY_FILE,
         json_store.HISTORY_ARCHIVE_FILE, json_store.LOGS_FILE) = self._orig
        if self._orig_db_env is not None:
            os.environ["INFRAWATCH_DB_PATH"] = self._orig_db_env
        else:
            os.environ.pop("INFRAWATCH_DB_PATH", None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_instance_scope_matches_only_that_instance(self):
        now = time.time()
        windows = [{"scope": "instance", "target": "10.0.0.5", "start": now - 60, "end": now + 60}]
        self.assertIsNotNone(alarm_app.get_active_maintenance("10.0.0.5", "blackbox", windows=windows))
        self.assertIsNone(alarm_app.get_active_maintenance("10.0.0.6", "blackbox", windows=windows))

    def test_job_scope_matches_any_instance_in_that_job(self):
        now = time.time()
        windows = [{"scope": "job", "target": "blackbox", "start": now - 60, "end": now + 60}]
        self.assertIsNotNone(alarm_app.get_active_maintenance("10.0.0.5", "blackbox", windows=windows))
        self.assertIsNone(alarm_app.get_active_maintenance("10.0.0.5", "other-job", windows=windows))

    def test_expired_window_does_not_match(self):
        now = time.time()
        windows = [{"scope": "instance", "target": "10.0.0.5", "start": now - 120, "end": now - 60}]
        self.assertIsNone(alarm_app.get_active_maintenance("10.0.0.5", "blackbox", windows=windows))

    def test_firing_during_maintenance_leaves_no_trace(self):
        from storage import MaintenanceRepository
        now = time.time()
        MaintenanceRepository.create_window(scope="instance", target="10.0.0.5", reason="", start=now - 60, end=now + 60)
        ok = alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="down for maintenance", job="blackbox", event_time=now, is_now_firing=True)
        self.assertFalse(ok)
        self.assertEqual(json_store.load_json(json_store.LOGS_FILE, []), [])
        self.assertEqual(json_store.load_json(json_store.HISTORY_FILE, []), [])
        status = json_store.load_json(json_store.STATUS_FILE, {"status": "NORMAL"})
        self.assertEqual(status.get("status", "NORMAL"), "NORMAL")

    def test_firing_resumes_normally_once_window_ends(self):
        from storage import MaintenanceRepository
        past = time.time() - 3600
        MaintenanceRepository.create_window(scope="instance", target="10.0.0.5", reason="", start=past - 60, end=past)
        ok = alarm_app.record_alert_event(
            name="TargetDown", severity="critical", instance="10.0.0.5",
            summary="down", job="blackbox", event_time=time.time(), is_now_firing=True)
        self.assertTrue(ok)
        self.assertEqual(len(json_store.load_json(json_store.LOGS_FILE, [])), 1)


class HealthEndpointTests(unittest.TestCase):
    """Phase 13 self-monitoring: /health must report per-component status,
    not just a bare ok:true."""

    def setUp(self):
        self.client = alarm_app.app.test_client()
        self.client.environ_base = {"HTTP_X_API_KEY": TEST_API_KEY, "HTTP_X_WEBHOOK_SECRET": TEST_WEBHOOK_SECRET}

    def test_health_reports_component_breakdown(self):
        resp = self.client.get('/health')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIn('components', body)
        for key in ('prometheus', 'monitoring_api', 'alarm_service', 'storage'):
            self.assertIn(key, body['components'])
            self.assertIn('ok', body['components'][key])


class MaintenanceApiTests(unittest.TestCase):
    """Phase 9 CRUD surface: create/list/delete a maintenance window."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        from storage import init_db
        init_db(self.db_path)
        self._orig_db_env = os.environ.get("INFRAWATCH_DB_PATH")
        os.environ["INFRAWATCH_DB_PATH"] = self.db_path
        self.client = alarm_app.app.test_client()
        self.client.environ_base = {"HTTP_X_API_KEY": TEST_API_KEY, "HTTP_X_WEBHOOK_SECRET": TEST_WEBHOOK_SECRET}

    def tearDown(self):
        if self._orig_db_env is not None:
            os.environ["INFRAWATCH_DB_PATH"] = self._orig_db_env
        else:
            os.environ.pop("INFRAWATCH_DB_PATH", None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_create_list_delete_roundtrip(self):
        now = time.time()
        resp = self.client.post('/api/maintenance', json={
            "target": "10.0.0.9", "scope": "instance", "reason": "reboot",
            "start": now - 60, "end": now + 3600,
        })
        self.assertEqual(resp.status_code, 200)
        window = resp.get_json()['window']
        self.assertTrue(window['id'])

        listed = self.client.get('/api/maintenance').get_json()['windows']
        self.assertEqual(len(listed), 1)
        self.assertTrue(listed[0]['active'])

        deleted = self.client.delete(f'/api/maintenance/{window["id"]}')
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.client.get('/api/maintenance').get_json()['windows'], [])

    def test_create_rejects_end_before_start(self):
        now = time.time()
        resp = self.client.post('/api/maintenance', json={
            "target": "10.0.0.9", "scope": "instance", "reason": "reboot",
            "start": now + 3600, "end": now,
        })
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()['ok'])


class CorrelationSuppressionTests(unittest.TestCase):
    """Phase 12: apply_correlation_suppression() is display-only — it must
    never change health/downSince, only tag suppressedBy."""

    def test_child_suppressed_when_parent_also_down(self):
        targets = [
            {"instance": "gateway", "health": "down"},
            {"instance": "host-behind-gateway", "health": "down"},
        ]
        alarm_app.apply_correlation_suppression(targets, {"host-behind-gateway": "gateway"})
        self.assertIsNone(targets[0]["suppressedBy"])
        self.assertEqual(targets[1]["suppressedBy"], "gateway")
        self.assertEqual(targets[1]["dependsOn"], "gateway")
        self.assertEqual(targets[1]["health"], "down")  # untouched

    def test_child_not_suppressed_when_parent_is_up(self):
        targets = [
            {"instance": "gateway", "health": "up"},
            {"instance": "host-behind-gateway", "health": "down"},
        ]
        alarm_app.apply_correlation_suppression(targets, {"host-behind-gateway": "gateway"})
        self.assertIsNone(targets[1]["suppressedBy"])

    def test_no_dependency_declared_is_never_suppressed(self):
        targets = [{"instance": "standalone-host", "health": "down"}]
        alarm_app.apply_correlation_suppression(targets, {})
        self.assertIsNone(targets[0]["suppressedBy"])
        self.assertIsNone(targets[0]["dependsOn"])


class DependencyApiTests(unittest.TestCase):
    """Phase 12 CRUD surface: create/list/delete a dependency link."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        from storage import init_db
        init_db(self.db_path)
        self._orig_db_env = os.environ.get("INFRAWATCH_DB_PATH")
        os.environ["INFRAWATCH_DB_PATH"] = self.db_path
        self.client = alarm_app.app.test_client()
        self.client.environ_base = {"HTTP_X_API_KEY": TEST_API_KEY, "HTTP_X_WEBHOOK_SECRET": TEST_WEBHOOK_SECRET}

    def tearDown(self):
        if self._orig_db_env is not None:
            os.environ["INFRAWATCH_DB_PATH"] = self._orig_db_env
        else:
            os.environ.pop("INFRAWATCH_DB_PATH", None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_create_list_delete_roundtrip(self):
        resp = self.client.post('/api/dependencies', json={"child": "10.0.0.9", "parent": "10.0.0.1"})
        self.assertEqual(resp.status_code, 200)
        dep = resp.get_json()['dependency']

        listed = self.client.get('/api/dependencies').get_json()['dependencies']
        self.assertEqual(len(listed), 1)

        deleted = self.client.delete(f'/api/dependencies/{dep["id"]}')
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.client.get('/api/dependencies').get_json()['dependencies'], [])

    def test_self_dependency_rejected(self):
        resp = self.client.post('/api/dependencies', json={"child": "10.0.0.9", "parent": "10.0.0.9"})
        self.assertEqual(resp.status_code, 400)

    def test_second_link_for_same_child_replaces_first(self):
        self.client.post('/api/dependencies', json={"child": "c", "parent": "p1"})
        self.client.post('/api/dependencies', json={"child": "c", "parent": "p2"})
        listed = self.client.get('/api/dependencies').get_json()['dependencies']
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]['parent'], 'p2')


if __name__ == "__main__":
    unittest.main()
