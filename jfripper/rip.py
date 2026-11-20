"""Disc in -> raw rip on disk, ready for encode.

A movie disc rips its longest title. A TV disc (several episode-length titles, no feature)
rips every episode; a disc can't say which show, season or episode numbers it holds, so
those wait in the review folder until you name them. Audio CDs go to cd.py instead.
"""
import fcntl
import json
import os
import re
import shutil
import string
import time

from . import cd, jobs, makemkv
from .config import conf
from .notify import notify
from .util import log

GENERIC_LABELS = {"", "DVD", "DVD_VIDEO", "DVDVIDEO", "VIDEO_TS", "BDMV", "BLURAY",
                  "BD_ROM", "DISC", "MOVIE", "LOGICAL_VOLUME_ID"}
# Trailing junk on volume labels: THE_MATRIX_WS, ALIEN_16X9, LOTR_DISC_1 ...
LABEL_JUNK = re.compile(r"(?:[ _](?:WS|FS|16X9|4X3|WIDESCREEN|FULLSCREEN|SE|CE|"
                        r"DISC[ _]?\d+|D\d|NTSC|PAL|R\d))+$", re.I)


def seconds(hms):
    h, m, s = (int(x) for x in hms.split(":"))
    return h * 3600 + m * 60 + s


def nice_name(label):
    label = LABEL_JUNK.sub("", label.strip())
    if label.upper().replace(" ", "_") in GENERIC_LABELS or label.isdigit():
        return None
    # capwords, not title(): "YOU'VE" -> "You've", not "You'Ve"
    return string.capwords(re.sub(r"\s+", " ", label.replace("_", " ")).strip())


def tv_hint(label):
    """'GILMORE_GIRLS_S1_D2' -> 'Gilmore Girls, season 1, disc 2' (or '' if the label says nothing)."""
    season = re.search(r"(?:^|[ _])(?:S|SEASON[ _]?)(\d{1,2})(?=$|[ _-])", label, re.I)
    disc = re.search(r"(?:^|[ _])(?:D|DISC[ _]?)(\d{1,2})(?=$|[ _])", label, re.I)
    if not disc and season:  # "S1-2" = season 1, disc 2
        disc = re.search(r"(?:^|[ _])S\d{1,2}-(\d{1,2})(?=$|[ _])", label, re.I)
    show = re.sub(r"(?:^|[ _])(?:S\d{1,2}-\d{1,2}|(?:S|SEASON[ _]?|D|DISC[ _]?)\d{1,2})(?=$|[ _])",
                  " ", label, flags=re.I)
    show = nice_name(show) or ""
    if not season or not show or re.search(r"\d", show):  # studio codes like AB60NNW1 aren't names
        return ""
    return f"{show}, season {int(season[1])}" + (f", disc {int(disc[1])}" if disc else "")


def tv_episodes(titles, sizes):
    """Title numbers, in disc order, if this looks like a TV disc: two or more episode-length
    titles of similar length and no feature. Many TV DVDs also carry a "Play All" title (about
    the sum of the episodes) and duplicate titles (same length and size); both are dropped.
    Anything else (a movie, even with long extras) returns None and rips as a movie."""
    episode_max = int(conf().rip["episode_max_minutes"]) * 60
    uniq, seen = [], set()
    for t in sorted(titles):
        key = (titles[t], sizes.get(t, 0))  # a duplicate is the very same cells: exact match
        if key not in seen:
            seen.add(key)
            uniq.append(t)
    longest = max(uniq, key=titles.get)
    rest = [t for t in uniq if t != longest and titles[t] <= episode_max]
    if titles[longest] > episode_max and len(rest) >= 2:
        total = sum(titles[t] for t in rest)
        if abs(titles[longest] - total) <= max(90, total * 0.03):
            uniq.remove(longest)  # Play All
    if not uniq or max(titles[t] for t in uniq) > episode_max:
        return None
    lengths = sorted(titles[t] for t in uniq)
    median = lengths[len(lengths) // 2]
    if lengths[-1] > 1.6 * median:  # e.g. a 68-min kids' movie plus two 16-min extras
        return None
    eps = [t for t in uniq if titles[t] >= 0.6 * median]  # drops a short featurette
    return eps if len(eps) >= 2 else None


def media_type(info, resolution, drive):
    kind = (re.search(r'^CINFO:1,\d+,"(.*)"$', info, re.M) or [None, ""])[1]
    kind = kind or drive.media_kind()
    if "blu" in kind.lower():
        return "4K UHD Blu-ray" if resolution.startswith("3840") else "Blu-ray"
    return "DVD" if "dvd" in kind.lower() else (kind or "Disc")


def stream_attr(info, title, attr):
    """First video stream's attribute: 19 = resolution, 30 = codec short name."""
    m = re.search(rf'^SINFO:{title},0,{attr},\d+,"(.*)"$', info, re.M)
    return m.group(1) if m else ""


def work_dir(stamp, label):
    work = os.path.join(conf().work, f"{stamp}-{re.sub(r'[^A-Za-z0-9]+', '_', label) or 'disc'}")
    os.makedirs(work)
    return work


def rip(drive):
    """Rip whatever is in the drive. Returns a work folder to encode, or None."""
    os.makedirs(conf().state_dir, exist_ok=True)
    lock = open(os.path.join(conf().state_dir, f"rip-{drive.name}.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log(f"{drive}: already being ripped")
        lock.close()
        return None
    try:
        time.sleep(5)  # let the drive settle after the media change
        if drive.is_audio_cd():
            if not drive.audio_cd_supported:
                log(f"{drive}: audio CD, but CD ripping needs whipper (Linux only); ejecting")
                notify("Audio CD not supported here",
                       "JFRipper rips audio CDs with whipper, which only runs on Linux.")
                drive.eject()
                return None
            cd.rip_cd(drive)
            return None
        return rip_video(drive)
    finally:
        lock.close()


def rip_video(drive):
    makemkv.refresh_beta_key()
    target = drive.makemkv_target()
    info = makemkv.run("info", target)
    label = (re.search(r'^CINFO:2,0,"(.*)"$', info, re.M) or [None, ""])[1]
    titles = {int(i): seconds(t) for i, t in
              re.findall(r'^TINFO:(\d+),9,0,"(\d+:\d\d:\d\d)"$', info, re.M)}
    sizes = {int(i): int(b) for i, b in re.findall(r'^TINFO:(\d+),11,0,"(\d+)"$', info, re.M)}
    episodes = tv_episodes(titles, sizes) if titles else None
    if episodes:
        return rip_tv(drive, target, info, label, titles, sizes, episodes)
    min_length = int(conf().rip["min_movie_minutes"]) * 60
    if not titles or max(titles.values()) < min_length:
        jobs.update(jobs.new_stamp(), label=label, started=time.time(), stage="failed",
                    finished=time.time(), message="No movie-length title found")
        log(f"{drive}: no title over {min_length // 60} min on '{label}'; ejecting")
        tail = "\n".join(makemkv.messages(info)[-5:])
        notify(f"Couldn't rip '{label or 'disc'}'",
               f"I couldn't find a movie-length title on the disc. MakeMKV said:\n\n{tail}")
        drive.eject()
        return None
    best = max(titles, key=titles.get)
    name = nice_name(label)
    stamp = jobs.new_stamp()
    work = work_dir(stamp, label)
    log(f"{drive}: ripping '{label}' title {best} ({titles[best] // 60} min) -> {work}")
    resolution = stream_attr(info, best, 19)
    jobs.update(stamp, label=label, name=name, started=time.time(), stage="ripping",
                work=work, duration=titles[best], expected_bytes=sizes.get(best),
                progress_file=os.path.join(work, "progress.txt"),
                media=media_type(info, resolution, drive), src_res=resolution,
                src_codec=stream_attr(info, best, 30))

    rip_dir = os.path.join(work, "rip")
    os.makedirs(rip_dir)
    src, step, skipped, out = makemkv.rip_title(
        drive, target, best, rip_dir, os.path.join(work, "progress.txt"),
        lambda how: jobs.update(stamp, rip_note=how), os.path.join(work, "makemkv.log"))
    drive.eject()
    if not src:
        rip_failed(drive, stamp, label, name, out, work)
        return None
    note = makemkv.rip_note(step, skipped)
    if note:
        log(f"{drive}: '{label}': {note}")
    return ready(stamp, work, {"label": label, "name": name, "source": src, "stamp": stamp,
                               "rip_note": note})


def rip_tv(drive, target, info, label, titles, sizes, episodes):
    """Rip every episode on a TV disc, in disc order, one MakeMKV run per title."""
    name = nice_name(label)
    stamp = jobs.new_stamp()
    work = work_dir(stamp, label)
    lengths = ", ".join(str(titles[t] // 60) for t in episodes)
    log(f"{drive}: TV disc '{label}': ripping {len(episodes)} episodes ({lengths} min) -> {work}")
    resolution = stream_attr(info, episodes[0], 19)
    jobs.update(stamp, label=label, name=name, kind="tv", episodes=len(episodes),
                started=time.time(), stage="ripping", work=work,
                media=media_type(info, resolution, drive), src_res=resolution,
                src_codec=stream_attr(info, episodes[0], 30))
    sources, notes = [], []
    for n, t in enumerate(episodes, 1):
        folder = os.path.join(work, f"title{t:02d}")
        os.makedirs(folder)
        progress = os.path.join(work, f"progress-{t:02d}.txt")
        jobs.update(stamp, episode=n, duration=titles[t], expected_bytes=sizes.get(t),
                    progress_file=progress, rip_note=None)
        src, step, skipped, out = makemkv.rip_title(
            drive, target, t, folder, progress, lambda how: jobs.update(stamp, rip_note=how),
            os.path.join(work, "makemkv.log"))
        if not src:
            drive.eject()
            rip_failed(drive, stamp, label, name, out, work)
            return None
        if step:
            notes.append(f"Episode {n:02d}: {makemkv.rip_note(step, skipped)}")
        sources.append(src)
    drive.eject()
    if notes:
        log(f"{drive}: '{label}': " + " / ".join(notes))
    return ready(stamp, work, {"kind": "tv", "label": label, "name": name, "stamp": stamp,
                               "sources": sources, "durations": [titles[t] for t in episodes],
                               "rip_note": "\n".join(notes)})


def ready(stamp, work, meta):
    """Leave meta.json for the encoder (it also lets an interrupted encode restart)."""
    with open(os.path.join(work, "meta.json"), "w") as f:
        json.dump(meta, f)
    jobs.update(stamp, stage="queued")
    log(f"ripped '{meta['label']}', ejected; queued for encode")
    return work


def rip_failed(drive, stamp, label, name, out, work):
    msgs = makemkv.messages(out)
    errors = makemkv.read_errors(out)
    why = ("The drive couldn't read part of the disc, even slowed right down and told to skip "
           "unreadable spots. Wipe it center-to-edge with a soft cloth and put it back in; it "
           "will start again on its own." if errors else "MakeMKV said:\n\n" + "\n".join(msgs[-5:]))
    log(f"{drive}: rip of '{label}' failed ({len(errors)} read errors)")
    notify(f"Rip failed: {name or label}", why)
    jobs.update(stamp, stage="failed", finished=time.time(),
                message="Disc read error - clean and reinsert" if errors else "Rip failed")
    shutil.rmtree(work, ignore_errors=True)
