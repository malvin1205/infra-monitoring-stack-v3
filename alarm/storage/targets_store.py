"""InfraWatch's own curated target list (targets/websites.yml) and the
deleted-target tombstones.

websites.yml is the subset of Prometheus-discovered instances an operator has
pinned for the wallboard. It keeps the Prometheus file_sd shape (a list of
{targets, labels} groups) so it round-trips cleanly; InfraWatch owns only the
group labeled job: "blackbox_http" and passes every other group through
untouched. Deleted-target state lives in SQLite (DeletedTargetRepository).

Depends only on monitoring_primitives + storage; nothing imports app.
"""
import contextlib
import logging
import os
import shutil
import threading

import yaml

try:
    from core.monitoring.primitives import normalize_target
    from .db import DeletedTargetRepository
except (ImportError, ValueError):
    from alarm.core.monitoring.primitives import normalize_target
    from alarm.storage import DeletedTargetRepository

logger = logging.getLogger("infrawatch")

# websites.yml keeps the Prometheus file_sd shape (a list of {targets, labels}
# groups) purely so it round-trips cleanly and could be pointed at a Prometheus
# by an operator who wires it up themselves. InfraWatch itself only owns the
# group labeled job: "blackbox_http"; any other group (e.g. a hand-written
# blackbox_ping group) is read straight through to save() untouched, so
# structure and multi-group files survive an Add/Delete round-trip. That
# passthrough guarantee is why save_website_targets() must NEVER fall back to
# an empty doc when the existing file fails to parse (K3) — doing so would
# silently delete every sibling group.
WEBSITES_JOB_LABEL = "blackbox_http"

# mtime-keyed parse cache: get_instance_job_map(), get_monitored_instances()
# and get_instance_cadence_map() each call this within one /instances request,
# re-reading and re-parsing the same YAML file every time. Keyed on (path,
# mtime) so save_website_targets() — which rewrites the file — invalidates it
# automatically with no explicit bump needed.
_WEBSITE_TARGETS_CACHE = {"key": None, "data": None}
_WEBSITE_TARGETS_CACHE_LOCK = threading.Lock()


def get_targets_file():
    # websites.yml is InfraWatch's own curation list — the subset of
    # Prometheus-discovered instances an operator has pinned for the wallboard
    # (see load_website_targets consumers). It is NOT a Prometheus scrape
    # config: nothing here provisions Prometheus from it. The old
    # ../prometheus/targets/ and /app/targets/ lookup paths were remnants of
    # the pre-v3 era when this project bundled its own Prometheus — dropped.
    env_target = os.environ.get("TARGETS_FILE")
    if env_target:
        return env_target

    try:
        from config import ALARM_DIR
    except ImportError:
        try:
            from alarm.config import ALARM_DIR
        except ImportError:
            ALARM_DIR = os.path.dirname(os.path.abspath(__file__))

    local_target = os.path.abspath(os.path.join(ALARM_DIR, "targets", "websites.yml"))
    if not os.path.isfile(local_target):
        # Fallback if called from a subpackage where ALARM_DIR was not resolved
        alt_target = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "targets", "websites.yml"))
        if os.path.isfile(alt_target):
            local_target = alt_target
    if os.path.isfile(local_target):
        return local_target

    os.makedirs(os.path.dirname(local_target), exist_ok=True)
    example_target = local_target + ".example"
    if os.path.isfile(example_target):
        try:
            shutil.copyfile(example_target, local_target)
        except Exception:
            pass
    return local_target


@contextlib.contextmanager
def _targets_write_lock():
    """Cross-process advisory lock around the websites.yml read-modify-write.
    In-process threads are already serialized by _WEBHOOK_LOCK at the call
    sites; this additionally guards a deployment scaled past one gunicorn
    worker/process, where that threading.Lock no longer helps and two
    concurrent Add/Delete cycles would lose an update. Best-effort: if the
    platform lock primitive is unavailable it proceeds unlocked (prior
    behavior). ponytail: coarse whole-file lock, fine at /api/targets' 20/60
    rate limit; revisit only if target churn ever gets hot.
    """
    lock_path = get_targets_file() + ".lock"
    f = None
    try:
        try:
            os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
            f = open(lock_path, "a+")
            f.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        except OSError:
            pass
        yield
    finally:
        if f is not None:
            try:
                f.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            f.close()


def load_website_targets():
    target_file = get_targets_file()
    if not os.path.exists(target_file):
        return []
    try:
        mtime = os.path.getmtime(target_file)
    except OSError:
        mtime = 0.0
    cache_key = (target_file, mtime)
    with _WEBSITE_TARGETS_CACHE_LOCK:
        if _WEBSITE_TARGETS_CACHE["key"] == cache_key and _WEBSITE_TARGETS_CACHE["data"] is not None:
            return list(_WEBSITE_TARGETS_CACHE["data"])
    try:
        with open(target_file, 'r', encoding='utf-8') as f:
            doc = yaml.safe_load(f)
    except Exception as e:
        logger.error(f"Error loading targets: {e}")
        return []

    if not isinstance(doc, list):
        if doc is not None:
            logger.warning("targets file %s is not a file_sd list — ignoring", target_file)
        return []

    urls = []
    seen_norm = set()
    for group in doc:
        if not isinstance(group, dict):
            logger.warning("targets file %s has a non-mapping group entry — skipped", target_file)
            continue
        labels = group.get('labels') or {}
        if labels.get('job') != WEBSITES_JOB_LABEL:
            continue
        for u in (group.get('targets') or []):
            u = str(u).strip()
            n = normalize_target(u)
            if u and n not in seen_norm:
                seen_norm.add(n)
                urls.append(u)
    with _WEBSITE_TARGETS_CACHE_LOCK:
        _WEBSITE_TARGETS_CACHE["key"] = cache_key
        _WEBSITE_TARGETS_CACHE["data"] = list(urls)
    return urls


def save_website_targets(urls):
    """Rewrite the blackbox_http group in websites.yml to `urls`, preserving
    every other group. Returns True on success; raises on a corrupt existing
    file or a failed write so the caller can report the failure instead of
    silently claiming success (K3/S6)."""
    target_file = get_targets_file()
    tmp_file = target_file + ".tmp"
    try:
        os.makedirs(os.path.dirname(target_file) or ".", exist_ok=True)

        doc = []
        if os.path.exists(target_file) and os.path.getsize(target_file) > 0:
            with open(target_file, 'r', encoding='utf-8') as f:
                loaded = yaml.safe_load(f)
            if isinstance(loaded, list):
                doc = loaded
            elif loaded is not None:
                # Parsed to a non-list (or would have raised above): refuse to
                # overwrite — an operator's hand-written multi-group file must
                # not be clobbered by a bad parse.
                raise RuntimeError(f"{target_file} is not a file_sd list; refusing to overwrite")

        for group in doc:
            if isinstance(group, dict) and (group.get('labels') or {}).get('job') == WEBSITES_JOB_LABEL:
                group['targets'] = list(urls)
                break
        else:
            doc.append({"targets": list(urls), "labels": {"job": WEBSITES_JOB_LABEL}})

        if os.path.exists(target_file):
            try:
                shutil.copyfile(target_file, target_file + ".bak")
            except OSError as e:
                logger.warning("could not write %s.bak: %s", target_file, e)

        with open(tmp_file, 'w', encoding='utf-8') as f:
            yaml.safe_dump(doc, f, default_flow_style=False, sort_keys=False)
        os.replace(tmp_file, target_file)
        return True
    except Exception as e:
        logger.error(f"Error saving targets {target_file}: {e}")
        try:
            if os.path.exists(tmp_file):
                os.unlink(tmp_file)
        except OSError:
            pass
        raise


# SQLite (DeletedTargetRepository) is the sole source of truth — deleted_targets.json
# used to be read first (when present) and written on every mutation as a
# parallel copy; both call sites now go straight through SQLite instead.
def load_deleted_targets():
    try:
        return DeletedTargetRepository.list_deleted()
    except Exception:
        return []


def save_deleted_targets(deleted_list):
    """Diffs deleted_list (the caller's full desired list, read-modified-write
    style — see /api/targets POST|DELETE) against SQLite's current state and
    applies add/restore so SQLite ends up matching it."""
    try:
        current_deleted = set(DeletedTargetRepository.list_deleted())
    except Exception:
        current_deleted = set()
    for d in deleted_list:
        if d not in current_deleted:
            DeletedTargetRepository.add_deleted(d)
    for d in current_deleted:
        if d not in deleted_list:
            DeletedTargetRepository.restore_target(d)
