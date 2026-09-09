"""Availability thread-safe response cache and single-flight lock coalescer."""
import threading
import time
from typing import Optional, Tuple, Any, Dict


class AvailabilityCache:
    """In-memory cache with single-flight request coalescing and TTL pruning."""

    def __init__(self, prune_interval: float = 30.0, max_age: float = 120.0):
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._flight_locks: Dict[str, threading.Lock] = {}
        self._flight_guard = threading.Lock()
        self._last_prune = 0.0
        self._prune_interval = prune_interval
        self._max_age = max_age

    def flight_lock_for(self, key: str) -> threading.Lock:
        with self._flight_guard:
            lock = self._flight_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._flight_locks[key] = lock
            return lock

    def get(self, key: str, effective_ttl: float, now: Optional[float] = None) -> Optional[Any]:
        curr_time = now if now is not None else time.time()
        self._maybe_prune(curr_time)
        with self._lock:
            if key in self._cache:
                cached_ts, cached_payload = self._cache[key]
                if curr_time - cached_ts < effective_ttl:
                    return cached_payload
        return None

    def set(self, key: str, payload: Any, now: Optional[float] = None) -> None:
        curr_time = now if now is not None else time.time()
        with self._lock:
            self._cache[key] = (curr_time, payload)

    def invalidate(self, job: Optional[str] = None, instance: Optional[str] = None) -> int:
        """Evict cached entries matching job or instance substring, or all if none given."""
        evicted = 0
        with self._lock:
            if job is None and instance is None:
                evicted = len(self._cache)
                self._cache.clear()
            else:
                to_delete = []
                norm_job = job.strip().lower() if job else None
                for k in self._cache:
                    if norm_job and f":{norm_job}:" in k:
                        to_delete.append(k)
                    elif instance and f":{instance}:" in k:
                        to_delete.append(k)
                for k in to_delete:
                    del self._cache[k]
                    evicted += 1

        with self._flight_guard:
            if job is None and instance is None:
                self._flight_locks.clear()
            else:
                to_remove = [k for k in self._flight_locks if (norm_job and f":{norm_job}:" in k) or (instance and f":{instance}:" in k)]
                for k in to_remove:
                    self._flight_locks.pop(k, None)

        return evicted

    def clear(self) -> None:
        self.invalidate()

    def _maybe_prune(self, now: float) -> None:
        if now - self._last_prune < self._prune_interval:
            return
        with self._lock:
            if now - self._last_prune < self._prune_interval:
                return
            self._last_prune = now
            expired_keys = [k for k, (ts, _) in self._cache.items() if (now - ts) > self._max_age]
            for k in expired_keys:
                del self._cache[k]

        with self._flight_guard:
            # Only reap a flight lock that is not currently held — popping a
            # lock a worker is mid-computation on lets the next identical
            # request build a fresh lock and run concurrently, defeating
            # single-flight coalescing.
            stale_flight = [
                k for k, lock in list(self._flight_locks.items())
                if k not in self._cache and not lock.locked()
            ]
            for k in stale_flight:
                self._flight_locks.pop(k, None)

    @staticmethod
    def derive_cache_key(
        endpoint: str,
        job: str,
        minutes_int: int,
        end_ts: Optional[float],
        sla_target_pct: float,
        sla_days: int,
        sla_map_sig: int,
        now: float,
    ) -> Tuple[str, float]:
        norm_job = (job or "node").strip().lower()
        avail_cache_ttl = 15.0 if minutes_int >= 1440 else 5.0

        if end_ts is not None:
            norm_end = f"hist_{end_ts}"
            effective_ttl = 60.0  # Fixed historical range is immutable
        else:
            bucket_sec = 15 if minutes_int >= 1440 else 5
            bucket_ts = int(now // bucket_sec) * bucket_sec
            norm_end = f"live_{bucket_ts}"
            effective_ttl = avail_cache_ttl

        key = f"avail:{endpoint}:{norm_job}:{minutes_int}:{norm_end}:sla{sla_target_pct}/{sla_days}/{sla_map_sig}"
        return key, effective_ttl
