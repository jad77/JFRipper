"""Job state files, one JSON per disc, read by the status page and the name command."""
import datetime as dt
import glob
import json
import os
import threading
import time

from .config import conf

KEEP_JOBS = 20
ACTIVE = {"ripping", "queued", "encoding", "filing"}
_lock = threading.RLock()
_last_stamp = None


def path(stamp):
    return os.path.join(conf().jobs_dir, f"{stamp}.json")


def new_stamp():
    """A job id like 20261120-093000, unique even when two drives finish the same second."""
    global _last_stamp
    with _lock:
        while True:
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            if stamp != _last_stamp and not os.path.exists(path(stamp)):
                _last_stamp = stamp
                return stamp
            time.sleep(1)


def load(stamp):
    try:
        with open(path(stamp)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def update(stamp, **fields):
    """Merge fields into the job's state file."""
    with _lock:
        os.makedirs(conf().jobs_dir, exist_ok=True)
        job = load(stamp) or {"stamp": stamp}
        if "stage" in fields and fields["stage"] != job.get("stage"):
            fields.setdefault("stage_started", time.time())
        job.update(fields)
        tmp = path(stamp) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(job, f)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path(stamp))
        prune()


def prune():
    """Keep the newest KEEP_JOBS; never drop one that's running or still waiting for a name."""
    files = sorted(glob.glob(os.path.join(conf().jobs_dir, "*.json")))
    for old in files[:-KEEP_JOBS]:
        try:
            with open(old) as f:
                stage = json.load(f).get("stage")
        except (OSError, ValueError):
            stage = None
        if stage not in ACTIVE and stage != "needs_name":
            try:
                os.remove(old)
            except FileNotFoundError:
                pass


def all_jobs():
    """Every job, newest first."""
    out = []
    for p in sorted(glob.glob(os.path.join(conf().jobs_dir, "*.json")), reverse=True):
        try:
            with open(p) as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            continue
    return out
