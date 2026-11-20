"""Audio CD -> AccurateRip-checked FLAC, tagged from MusicBrainz, filed under Music/ (Linux).

Uses whipper. Discs MusicBrainz doesn't know wait in the review folder as
"Unknown CD <stamp>/" until you name them.
"""
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import jobs, sorter
from .config import conf
from .notify import notify
from .util import log, under


def whipper_conf():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "whipper", "whipper.conf")


def read_disc_id(dev):
    """(MusicBrainz disc id, tracks, seconds) using python-discid or python-libdiscid."""
    try:
        import discid
        d = discid.read(dev)
        return d.id, d.last_track_num - d.first_track_num + 1, d.sectors // 75
    except ImportError:
        import libdiscid
        d = libdiscid.read(dev)
        return d.id, d.last_track - d.first_track + 1, d.sectors // 75


def lookup_cd(dev):
    """(tracks or None, seconds, "Artist - Album" or None) from the disc's TOC and MusicBrainz."""
    try:
        disc_id, tracks, secs = read_disc_id(dev)
    except Exception as exc:  # module not installed, or the TOC couldn't be read
        log(f"{dev}: disc lookup skipped ({exc!r}); whipper will still tag it if it can")
        return None, 0, None
    try:
        import musicbrainzngs
        from . import __version__
        musicbrainzngs.set_useragent("JFRipper", __version__)
        res = musicbrainzngs.get_releases_by_discid(disc_id, includes=["artists"])
        rel = res["disc"]["release-list"][0]
        return tracks, secs, f"{rel['artist-credit-phrase']} - {rel['title']}"
    except Exception:  # not in MusicBrainz (404), only a CD stub, no network, or not installed
        return tracks, secs, None


def calibrated():
    try:
        with open(whipper_conf()) as f:
            return "read_offset" in f.read()
    except FileNotFoundError:
        return False


def calibrate(dev):
    """whipper won't rip until it knows the drive's read offset. Find it once, on the first
    audio CD (needs a disc that's in AccurateRip, i.e. most pressed CDs)."""
    if calibrated():
        return True
    log(f"{dev}: first audio CD: calibrating the drive (cache test + read offset)")
    subprocess.run(["whipper", "drive", "analyze", "-d", dev], capture_output=True, timeout=3600)
    res = subprocess.run(["whipper", "offset", "find", "-d", dev], capture_output=True,
                         text=True, timeout=4 * 3600)
    if calibrated():
        log(f"{dev}: drive offset found and saved")
        return True
    why = (res.stderr.strip().splitlines() or ["no output"])[-1]
    log(f"{dev}: drive offset not found ({why}); ripping at offset 0, will retry next CD")
    return False


def rip_cd(drive):
    dev = drive.path
    stamp = jobs.new_stamp()
    tracks, secs, name = lookup_cd(dev)
    label = name or "Unknown CD"
    work = os.path.join(conf().work, f"{stamp}-cd")
    os.makedirs(work)
    progress = os.path.join(work, "progress.txt")
    jobs.update(stamp, label=label, name=label, started=time.time(), stage="ripping", work=work,
                progress_file=progress, media="Audio CD", out_codec="FLAC")
    ok_offset = calibrate(dev)
    log(f"{dev}: ripping audio CD '{label}' ({tracks or '?'} tracks, {secs // 60} min) -> {work}")

    # -U: rip unknown discs too (they go to review); tags, not these names, decide the filing.
    cmd = ["whipper", "cd", "-d", dev, "rip", "-U", "--cdr", "-C", "embed", "-O", work,
           "--track-template", "%t %n", "--disc-template", "%A - %d"]
    if not ok_offset:
        cmd += ["-o", "0"]
    with open(os.path.join(work, "whipper.out"), "w") as out:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            out.write(line)
            m = re.search(r"ripping track (\d+) of (\d+)", line)
            if m:
                with open(progress, "a") as p:
                    p.write(f"PRGV:0,{int(m[1]) - 1},{m[2]}\n")
        rc = proc.wait()
    drive.eject()
    flacs = sorted(os.path.join(d, f) for d, _, fs in os.walk(work) for f in fs if f.endswith(".flac"))
    tracks = tracks or len(flacs)
    if rc != 0 or not flacs or len(flacs) < tracks:
        with open(os.path.join(work, "whipper.out")) as f:
            tail = f.read().strip().splitlines()[-5:]
        log(f"{dev}: rip of audio CD '{label}' failed (whipper exit {rc}, {len(flacs)}/{tracks} tracks)")
        notify(f"CD rip failed: {label}",
               "A track couldn't be read cleanly. Wipe the disc center-to-edge with a soft cloth "
               "and put it back in; it will start again on its own.\n\nwhipper said:\n\n"
               + "\n".join(tail))
        jobs.update(stamp, stage="failed", finished=time.time(),
                    message="Couldn't read a track - clean and reinsert")
        shutil.rmtree(work, ignore_errors=True)
        return

    os.makedirs(conf().cd_logs, exist_ok=True)
    ar = "no rip log"
    for d, _, fs in os.walk(work):
        for f in fs:
            if f.endswith(".log"):
                shutil.copy(os.path.join(d, f), os.path.join(conf().cd_logs, f"{stamp}.log"))
                with open(os.path.join(d, f), errors="replace") as fh:
                    text = fh.read()
                ar = (re.search(r"AccurateRip summary: (.*)", text) or [None, "unknown"])[1].strip()
    if not ok_offset:
        ar += " (drive offset not calibrated yet)"

    from mutagen import File as MutagenFile
    tags = MutagenFile(flacs[0], easy=True) or {}
    known = bool(tags.get("musicbrainz_albumid"))
    jobs.update(stamp, stage="filing")

    if not known:
        dest = os.path.join(conf().review, f"Unknown CD {stamp}")
        os.makedirs(dest)
        for f in flacs:
            shutil.move(f, os.path.join(dest, os.path.basename(f)))
        shutil.rmtree(work)
        log(f"audio CD not in MusicBrainz; {tracks} tracks waiting at {dest}")
        jobs.update(stamp, stage="needs_name", finished=time.time(), path=dest)
        notify("Ripped a CD, needs a name",
               f"MusicBrainz didn't know this disc, so its {tracks} tracks are waiting at\n{dest}\n\n"
               "Name it on the status page, or run:\n"
               f'  jfripper name cd {stamp} --artist "Artist" --album "Album"\n\n'
               f"AccurateRip: {ar}")
        return

    landed = [str(sorter.handle_music(Path(f))) for f in flacs]
    shutil.rmtree(work)
    review = [p for p in landed if not under(p, conf().music)]
    album = os.path.dirname(landed[0])
    if not review:
        log(f"audio CD '{label}' filed in {album} (AccurateRip: {ar})")
        jobs.update(stamp, stage="done", finished=time.time(), title=label, path=album)
        notify(f"{label} is in Jellyfin",
               f"Ripped {tracks} tracks to FLAC and filed them in\n{album}\n\nAccurateRip: {ar}")
    else:
        where = os.path.dirname(review[0])
        log(f"audio CD '{label}': {len(review)} tracks went to review ({where})")
        jobs.update(stamp, stage="failed", finished=time.time(), path=where,
                    message="Some tracks need a look")
        notify(f"Ripped {label}, some tracks need a look",
               f"{len(review)} of {tracks} tracks didn't file cleanly (often a second "
               f"disc with the same track names). See\n{where}\n\nAccurateRip: {ar}")
