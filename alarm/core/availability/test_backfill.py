"""Self-check for availability depth backfill and head window planning.

Run: python -m alarm.core.availability.test_backfill

Guards the bug where the aggregator only ever rolled up the trailing hour, so a
fleet Prometheus held 30d of telemetry for rendered as "only 1.7% of this
window observed" in the 7d/30d views.
"""
import math
import time

from alarm.core.availability.engine import (
    AvailabilityEngine,
    AVAIL_BACKFILL_CHUNK_SECONDS,
    AVAIL_HEAD_REPAIR_SECONDS,
)

HOUR = 3600.0
DAY = 86400.0
DEPTH = 35 * DAY


class FakeRepo:
    def __init__(self, starts):
        self.starts = dict(starts)

    def get_instance_bucket_starts(self, instances=None):
        if not instances:
            return dict(self.starts)
        return {k: v for k, v in self.starts.items() if k in set(instances)}

    def get_latest_bucket_end(self, job="all"):
        return None


def _engine(starts):
    return AvailabilityEngine(bucket_repo=FakeRepo(starts))


def _walk(eng, insts, now, limit=500):
    """Drive the cursor to exhaustion, returning every window it emitted."""
    out = []
    for _ in range(limit):
        w = eng._next_depth_backfill_window(insts, now)
        if w is None:
            return out
        out.append(w)
    raise AssertionError("depth backfill never terminated")


def test_depth_walk_reaches_retention_floor():
    """The reported state: every host materialized, but only 3h deep."""
    now = time.time()
    hour_end = math.floor(now / HOUR) * HOUR
    insts = ["host-a", "host-b"]
    eng = _engine({i: hour_end - 3 * HOUR for i in insts})

    wins = _walk(eng, insts, now)
    assert wins, "shallow history must trigger a depth walk"
    assert wins[0][1] == hour_end - 3 * HOUR, "walk starts at the shallowest instance"
    assert wins[-1][0] <= hour_end - DEPTH + 1.0, "walk must reach the retention floor"
    # Each chunk ends exactly where the previous one started: backwards, no gaps.
    for (start, _), (_, next_end) in zip(wins, wins[1:]):
        assert next_end == start, "chunks must tile backwards without gaps"


def test_depth_walk_starts_at_the_thinnest_instance():
    """Mixed depths: 13 hosts have 7d, the rest have 3h. Starting from the deep
    ones would skip the 3h..7d span the thin ones are missing."""
    now = time.time()
    hour_end = math.floor(now / HOUR) * HOUR
    insts = ["deep", "thin"]
    eng = _engine({"deep": hour_end - 7 * DAY, "thin": hour_end - 3 * HOUR})

    wins = _walk(eng, insts, now)
    assert wins[0][1] == hour_end - 3 * HOUR, "walk must start at the thinnest history"
    covered_from = min(s for s, _ in wins)
    covered_to = max(e for _, e in wins)
    assert covered_to >= hour_end - 3 * HOUR
    assert covered_from <= hour_end - DEPTH + 1.0


def test_depth_walk_is_one_chunk_per_cycle():
    now = time.time()
    hour_end = math.floor(now / HOUR) * HOUR
    insts = ["host-a"]
    eng = _engine({"host-a": hour_end - 3 * HOUR})

    first = eng._next_depth_backfill_window(insts, now)
    assert first is not None
    assert first[1] - first[0] <= AVAIL_BACKFILL_CHUNK_SECONDS + 1.0


def test_depth_walk_terminates_and_stays_quiet():
    """Once depth is satisfied the walk must stop, not re-run every cycle."""
    now = time.time()
    hour_end = math.floor(now / HOUR) * HOUR
    insts = ["host-a"]
    eng = _engine({"host-a": hour_end - 3 * HOUR})

    _walk(eng, insts, now)
    assert eng._next_depth_backfill_window(insts, now) is None
    assert eng._next_depth_backfill_window(insts, now + 60.0) is None


def test_already_deep_fleet_does_no_work():
    now = time.time()
    hour_end = math.floor(now / HOUR) * HOUR
    insts = ["host-a", "host-b"]
    eng = _engine({i: hour_end - DEPTH - DAY for i in insts})
    assert eng._next_depth_backfill_window(insts, now) is None


def test_new_target_restarts_the_walk():
    """A host added after the fleet was already deep still needs its history."""
    now = time.time()
    hour_end = math.floor(now / HOUR) * HOUR
    eng = _engine({"host-a": hour_end - DEPTH - DAY})

    assert eng._next_depth_backfill_window(["host-a"], now) is None
    # host-new is monitored but has no buckets yet.
    w = eng._next_depth_backfill_window(["host-a", "host-new"], now)
    assert w is not None, "a newly monitored target must retrigger the depth walk"


def test_head_windows_steady_state():
    now = time.time()
    hour_end = math.floor(now / HOUR) * HOUR
    wins = AvailabilityEngine._availability_aggregation_windows(now, hour_end - 60.0)
    assert wins[0] == (hour_end - HOUR, hour_end)
    assert all(e - s <= HOUR + 1.0 for s, e in wins)


def test_head_repair_is_capped():
    """A long aggregator outage must not emit hundreds of windows in one cycle."""
    now = time.time()
    wins = AvailabilityEngine._availability_aggregation_windows(now, now - 90 * DAY)
    assert wins, "a gap must produce repair windows"
    assert now - wins[0][0] <= AVAIL_HEAD_REPAIR_SECONDS + HOUR
    assert abs(wins[-1][1] - now) < 1.0
    for (_, prev_end), (nxt_start, _) in zip(wins, wins[1:]):
        assert prev_end == nxt_start, "windows must tile without gaps"


def test_head_cold_start_is_bounded():
    now = time.time()
    wins = AvailabilityEngine._availability_aggregation_windows(now, None)
    assert now - wins[0][0] <= AVAIL_HEAD_REPAIR_SECONDS + HOUR


def demo():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all checks passed")


if __name__ == "__main__":
    demo()
