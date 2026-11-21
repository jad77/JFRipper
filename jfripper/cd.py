"""Audio CD -> FLAC, tagged from MusicBrainz, filed under Music/.

Linux: whipper (secure reads, AccurateRip-checked).
macOS: the system mounts an audio CD as a folder of AIFF files; each is converted with the
built-in afconvert, and the album is looked up on MusicBrainz from the disc's table of
contents (the hidden .TOC.plist on the mounted CD). No AccurateRip check on macOS.

Discs MusicBrainz doesn't know wait in the review folder as "Unknown CD <stamp>/" until you
name them.
"""
import base64
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from . import __version__, jobs, sorter
from .config import IS_MAC, conf
from .notify import notify
from .util import log, under

AUDIO_EXT = (".flac", ".m4a", ".mp3")
TAG_KEYS = ("title", "artist", "albumartist", "album", "tracknumber", "discnumber", "date",
            "musicbrainz_albumid")
MB_API = "https://musicbrainz.org/ws/2"
MB_AGENT = f"JFRipper/{__version__} ( https://github.com/jad77/JFRipper )"


def rip_cd(drive):
    if IS_MAC:
        rip_cd_mac(drive)
    else:
        rip_cd_whipper(drive)


def failed(stamp, work, label, why, detail):
    log(f"rip of audio CD '{label}' failed: {why}")
    notify(f"CD rip failed: {label}",
           "A track couldn't be read cleanly. Wipe the disc center-to-edge with a soft cloth "
           "and put it back in; it will start again on its own.\n\n" + detail)
    jobs.update(stamp, stage="failed", finished=time.time(),
                message="Couldn't read a track - clean and reinsert")
    shutil.rmtree(work, ignore_errors=True)


def file_cd(stamp, work, files, label, known, check):
    """File ripped tracks: into Music/ if MusicBrainz knew the disc, else into review.
    check = the line about rip verification for the notification."""
    tracks = len(files)
    jobs.update(stamp, stage="filing")
    if not known:
        dest = os.path.join(conf().review, f"Unknown CD {stamp}")
        os.makedirs(dest)
        for f in files:
            shutil.move(f, os.path.join(dest, os.path.basename(f)))
        shutil.rmtree(work)
        log(f"audio CD not in MusicBrainz; {tracks} tracks waiting at {dest}")
        jobs.update(stamp, stage="needs_name", finished=time.time(), path=dest)
        notify("Ripped a CD, needs a name",
               f"MusicBrainz didn't know this disc, so its {tracks} tracks are waiting at\n{dest}\n\n"
               "Name it on the status page, or run:\n"
               f'  jfripper name cd {stamp} --artist "Artist" --album "Album"\n\n{check}')
        return

    landed = [str(sorter.handle_music(Path(f))) for f in files]
    shutil.rmtree(work)
    review = [p for p in landed if not under(p, conf().music)]
    album = os.path.dirname(landed[0])
    if not review:
        log(f"audio CD '{label}' filed in {album} ({check})")
        jobs.update(stamp, stage="done", finished=time.time(), title=label, path=album)
        notify(f"{label} is in Jellyfin",
               f"Ripped {tracks} tracks and filed them in\n{album}\n\n{check}")
    else:
        where = os.path.dirname(review[0])
        log(f"audio CD '{label}': {len(review)} tracks went to review ({where})")
        jobs.update(stamp, stage="failed", finished=time.time(), path=where,
                    message="Some tracks need a look")
        notify(f"Ripped {label}, some tracks need a look",
               f"{len(review)} of {tracks} tracks didn't file cleanly (often a second "
               f"disc with the same track names). See\n{where}\n\n{check}")


# ---------------------------------------------------------------- output format

def wants_mp3():
    return str(conf().cd["format"]).lower() == "mp3"  # checked when the config loads


def to_mp3(src, dest_stem):
    """Any audio file -> MP3 with ffmpeg's LAME encoder (VBR; cd.mp3_quality 0 = best).
    Embedded cover art is carried over. Returns the new file's path."""
    from .encode import tool
    out = dest_stem + ".mp3"
    res = subprocess.run([tool("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error", "-y",
                          "-i", src, "-map", "0:a", "-map", "0:v?", "-c:a", "libmp3lame",
                          "-q:a", str(int(conf().cd["mp3_quality"])), "-c:v", "copy",
                          "-id3v2_version", "3", "-map_metadata", "-1", out],
                         capture_output=True, text=True)
    if res.returncode != 0:
        raise OSError(res.stderr.strip()[-500:] or f"ffmpeg exit {res.returncode}")
    return out


def open_tags(path):
    """Easy (plain key) tags for FLAC / M4A / MP3, adding an empty tag block if there's none."""
    from mutagen import File as MutagenFile
    tags = MutagenFile(path, easy=True)
    if tags.tags is None:
        tags.add_tags()
    return tags


def copy_tags(src, dest):
    old, new = open_tags(src), open_tags(dest)
    for key in TAG_KEYS:
        if old.get(key):
            new[key] = old[key]
    new.save()


# ---------------------------------------------------------------- MusicBrainz

def disc_id(first, last, leadout, offsets):
    """MusicBrainz disc id from a table of contents (offsets in sectors, including the
    150-sector lead-in)."""
    data = f"{first:02X}{last:02X}{leadout:08X}"
    data += "".join(f"{(offsets[i] if i < len(offsets) else 0):08X}" for i in range(99))
    digest = base64.b64encode(hashlib.sha1(data.encode("ascii")).digest()).decode("ascii")
    return digest.replace("+", ".").replace("/", "_").replace("=", "-")


def credit(artist_credit):
    return "".join(a["name"] + a.get("joinphrase", "") for a in artist_credit or [])


def musicbrainz_lookup(discid):
    """The disc's album from MusicBrainz, or None if it isn't known (or there's no network):
    {"artist", "album", "date", "album_id", "disc", "discs", "tracks": [{"title", "artist"}]}"""
    url = f"{MB_API}/discid/{discid}?inc=artist-credits+recordings&fmt=json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": MB_AGENT})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
    except Exception as exc:  # 404 = not in MusicBrainz
        log(f"MusicBrainz lookup of {discid}: {exc}")
        return None
    for rel in data.get("releases") or []:
        for medium in rel.get("media") or []:
            if any(d.get("id") == discid for d in medium.get("discs") or []):
                album_artist = credit(rel.get("artist-credit"))
                return {"artist": album_artist, "album": rel.get("title") or "",
                        "date": (rel.get("date") or "")[:4], "album_id": rel.get("id"),
                        "disc": medium.get("position") or 1, "discs": len(rel["media"]),
                        "tracks": [{"title": t.get("title") or "",
                                    "artist": credit(t.get("artist-credit")) or album_artist}
                                   for t in medium.get("tracks") or []]}
    return None


# ---------------------------------------------------------------- macOS

def read_toc(path):
    """A mounted audio CD's .TOC.plist -> (first, last, leadout, offsets) for the audio tracks.
    An enhanced CD's data track is left out, with the lead-out moved back the way MusicBrainz
    expects (data track start - 11400)."""
    with open(path, "rb") as f:
        toc = plistlib.load(f)
    session = toc["Sessions"][0]
    tracks = [t for t in session["Track Array"] if not t.get("Data")]
    starts = [int(t["Start Block"]) for t in tracks]
    leadout = int(session["Leadout Block"])
    data = [int(t["Start Block"]) for s in toc["Sessions"] for t in s["Track Array"] if t.get("Data")]
    if data:
        leadout = data[0] - 11400
    # Disc ids count the 150-sector lead-in; add it if this TOC starts track 1 at zero.
    lead_in = 150 if starts and starts[0] < 150 else 0
    numbers = [int(t["Point"]) for t in tracks]
    return min(numbers), max(numbers), leadout + lead_in, [s + lead_in for s in starts]


def track_files(mount):
    """The CD's AIFF tracks in disc order ("1 Track 1.aiff", "2 Song Name.aiff", ...)."""
    files = [f for f in os.listdir(mount) if f.lower().endswith((".aiff", ".aif"))]
    number = lambda f: int(re.match(r"\d+", f).group(0)) if re.match(r"\d+", f) else 999
    return [os.path.join(mount, f) for f in sorted(files, key=number)]


_flac_ok = None


def afconvert_flac():
    """Can this macOS's afconvert write FLAC? (Newer ones can; older ones get Apple Lossless.)"""
    global _flac_ok
    if _flac_ok is None:
        out = subprocess.run(["afconvert", "-hf"], capture_output=True, text=True)
        _flac_ok = "'flac' = FLAC" in out.stdout + out.stderr
    return _flac_ok


def afconvert(src, dest_stem):
    """AIFF -> FLAC (or Apple Lossless). Returns the new file's path."""
    fmt, codec, ext = ("flac", "flac", ".flac") if afconvert_flac() else ("m4af", "alac", ".m4a")
    out = dest_stem + ext
    res = subprocess.run(["afconvert", "-f", fmt, "-d", codec, src, out],
                         capture_output=True, text=True)
    if res.returncode != 0:
        raise OSError(res.stderr.strip() or res.stdout.strip() or f"afconvert exit {res.returncode}")
    return out


def tag(path, n, total, album):
    tags = open_tags(path)
    tags["tracknumber"] = f"{n}/{total}"
    if album:
        info = album["tracks"][n - 1] if n <= len(album["tracks"]) else {}
        tags["title"] = info.get("title") or f"Track {n:02d}"
        tags["artist"] = info.get("artist") or album["artist"]
        tags["albumartist"] = album["artist"]
        tags["album"] = album["album"]
        tags["discnumber"] = f"{album['disc']}/{album['discs']}"
        if album["date"]:
            tags["date"] = album["date"]
        tags["musicbrainz_albumid"] = album["album_id"]
    else:
        tags["title"] = f"Track {n:02d}"
    tags.save()


def rip_cd_mac(drive):
    stamp = jobs.new_stamp()
    mount = drive.mount_point()
    toc = os.path.join(mount, ".TOC.plist") if mount else ""
    album, secs = None, 0
    if toc and os.path.exists(toc):
        first, last, leadout, offsets = read_toc(toc)
        secs = (leadout - offsets[0]) // 75
        album = musicbrainz_lookup(disc_id(first, last, leadout, offsets))
    label = f"{album['artist']} - {album['album']}" if album else "Unknown CD"
    sources = track_files(mount) if mount else []
    work = os.path.join(conf().work, f"{stamp}-cd")
    os.makedirs(work)
    progress = os.path.join(work, "progress.txt")
    mp3 = wants_mp3()
    jobs.update(stamp, label=label, name=label, started=time.time(), stage="ripping", work=work,
                progress_file=progress, media="Audio CD", duration=secs or None,
                out_codec="MP3" if mp3 else "FLAC" if afconvert_flac() else "Apple Lossless")
    if not sources:
        drive.eject()
        failed(stamp, work, label, "no tracks", "macOS didn't show any audio tracks on the disc.")
        return
    log(f"{drive}: ripping audio CD '{label}' ({len(sources)} tracks, {secs // 60} min) -> {work}")
    files = []
    try:
        for n, src in enumerate(sources, 1):
            with open(progress, "a") as p:
                p.write(f"PRGV:0,{n - 1},{len(sources)}\n")
            convert = to_mp3 if mp3 else afconvert
            files.append(convert(src, os.path.join(work, f"{n:02d}")))
    except OSError as exc:
        drive.eject()
        failed(stamp, work, label, f"track {len(files) + 1}: {exc}",
               f"{'ffmpeg' if mp3 else 'afconvert'} said, on track {len(files) + 1}:\n\n{exc}")
        return
    drive.eject()
    for n, f in enumerate(files, 1):
        tag(f, n, len(files), album)
    file_cd(stamp, work, files, label, bool(album),
            "Read with macOS's own CD reader (no AccurateRip check on macOS).")


# ---------------------------------------------------------------- Linux (whipper)

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
        discid, tracks, secs = read_disc_id(dev)
    except Exception as exc:  # module not installed, or the TOC couldn't be read
        log(f"{dev}: disc lookup skipped ({exc!r}); whipper will still tag it if it can")
        return None, 0, None
    album = musicbrainz_lookup(discid)
    return tracks, secs, f"{album['artist']} - {album['album']}" if album else None


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


def rip_cd_whipper(drive):
    dev = drive.path
    stamp = jobs.new_stamp()
    tracks, secs, name = lookup_cd(dev)
    label = name or "Unknown CD"
    work = os.path.join(conf().work, f"{stamp}-cd")
    os.makedirs(work)
    progress = os.path.join(work, "progress.txt")
    mp3 = wants_mp3()
    jobs.update(stamp, label=label, name=label, started=time.time(), stage="ripping", work=work,
                progress_file=progress, media="Audio CD", out_codec="MP3" if mp3 else "FLAC")
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
        failed(stamp, work, label, f"whipper exit {rc}, {len(flacs)}/{tracks} tracks",
               "whipper said:\n\n" + "\n".join(tail))
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

    known = bool(open_tags(flacs[0]).get("musicbrainz_albumid"))
    files = flacs
    if mp3:  # whipper only writes FLAC; convert the verified rip, tags and cover art included
        try:
            files = []
            for f in flacs:
                files.append(to_mp3(f, os.path.splitext(f)[0]))
                copy_tags(f, files[-1])
                os.remove(f)
        except OSError as exc:
            failed(stamp, work, label, f"MP3 conversion: {exc}", f"ffmpeg said:\n\n{exc}")
            return
    file_cd(stamp, work, files, label, known, f"AccurateRip: {ar}")
