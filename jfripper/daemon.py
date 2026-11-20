"""`jfripper run`: watch the drives, rip on insert, encode one disc at a time, keep the
incoming folder sorted, and serve the status page."""
import glob
import json
import os
import queue
import re
import shutil
import threading
import time
import traceback

from . import VERSION_LABEL, drives, encode, jobs, rip, sorter, status
from .config import conf
from .notify import notify
from .util import log

DRIVE_RESCAN = 30  # seconds between looks for newly plugged-in drives


def recover(q):
    """After a restart: finished rips get their encode again; half-done rips are dropped."""
    for job in jobs.all_jobs():
        if job.get("stage") == "ripping":
            jobs.update(job["stamp"], stage="failed", finished=time.time(),
                        message="Interrupted - JFRipper stopped mid-rip; reinsert the disc")
    for work in sorted(glob.glob(os.path.join(conf().work, "*"))):
        name = os.path.basename(work)
        if not os.path.isdir(work) or not re.match(r"\d{8}-\d{6}-", name):
            continue
        meta = os.path.join(work, "meta.json")
        if os.path.exists(meta):
            with open(meta) as f:
                stamp = json.load(f)["stamp"]
            log(f"resuming encode of {name}")
            jobs.update(stamp, stage="queued")
            q.put(work)
        else:
            log(f"removing unfinished rip {name}")
            shutil.rmtree(work, ignore_errors=True)


def encoder_worker(q):
    while True:
        work = q.get()
        try:
            encode.encode(work)
        except Exception:
            log(f"encode of {work} crashed:\n{traceback.format_exc()}")
            stamp = os.path.basename(work)[:15]
            if jobs.load(stamp):
                jobs.update(stamp, stage="failed", finished=time.time(), message="Encode crashed")
            notify("Encode crashed", f"The rip is kept at {work}.\n\n{traceback.format_exc()[-1500:]}")


def watch_drive(drive, q):
    poll = float(conf().rip["poll_seconds"])
    present = False if conf().rip["rip_on_start"] else drive.has_disc()
    log(f"watching {drive}")
    while True:
        now = drive.has_disc()
        if now and not present:
            try:
                work = rip.rip(drive)
                if work:
                    q.put(work)
            except Exception:
                log(f"{drive}: rip crashed:\n{traceback.format_exc()}")
                notify("Rip crashed", traceback.format_exc()[-1500:])
                drive.eject()
            # Normally ejected by now; if the eject failed, don't rip the same disc again.
            now = drive.has_disc()
        present = now
        time.sleep(poll)


def drive_manager(q):
    watched, warned = set(), False
    while True:
        try:
            for drive in drives.find_drives():
                if drive.name not in watched:
                    watched.add(drive.name)
                    threading.Thread(target=watch_drive, args=(drive, q), daemon=True,
                                     name=f"drive-{drive.name}").start()
        except Exception as exc:
            log(f"drive scan failed: {exc!r}")
        if not watched and not warned:
            log("no optical drive found yet; will keep checking")
            warned = True
        time.sleep(DRIVE_RESCAN)


def sorter_loop():
    while True:
        try:
            sorter.process()
        except Exception:
            log(f"sorter crashed:\n{traceback.format_exc()}")
        time.sleep(float(conf().sorter["interval_seconds"]))


def run():
    c = conf()
    c.ensure_dirs()
    log(f"JFRipper {VERSION_LABEL} starting; library {c.root}, config {c.path or '(defaults)'}")
    encode.encoder()  # pick the encoder now, so a problem shows at startup
    q = queue.Queue()
    recover(q)
    for target, args in ((encoder_worker, (q,)), (drive_manager, (q,)), (sorter_loop, ())):
        threading.Thread(target=target, args=args, daemon=True, name=target.__name__).start()
    if c.status["enabled"]:
        status.serve()
    else:
        threading.Event().wait()
