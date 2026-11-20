"""File a rip JFRipper couldn't name, using the name you give it.

Used by `jfripper name ...` and the status page. Only touches what a needs_name job points
at, inside the review folder. Each call returns {"ok": bool, "path": ..., "message": ...}.
A miss leaves the job at needs_name (pointing at wherever the file is now) so you can retry.
"""
import os
import re
import shutil
from pathlib import Path

from . import jobs, sorter
from .config import conf
from .util import log, sanitize, under


class Refused(Exception):
    pass


def result(ok, message, path=None):
    return {"ok": ok, "path": str(path) if path else None, "message": message}


def waiting_job(stamp, want_dir):
    """The needs_name job and its path, refusing anything outside the review folder."""
    if not re.fullmatch(r"\d{8}-\d{6}", str(stamp)):
        raise Refused(f"bad job id {stamp!r}")
    job = jobs.load(stamp)
    if not job:
        raise Refused("that rip's job record is gone")
    where = os.path.realpath(job.get("path") or "")
    if job.get("stage") != "needs_name" or not under(where, conf().review):
        raise Refused("that rip isn't waiting for a name any more")
    if not os.path.exists(where):
        raise Refused(f"{where} isn't there any more; it may have been filed by hand")
    if os.path.isdir(where) != want_dir:
        raise Refused(f"{where} isn't the kind of rip I expected")
    return job, where


def guarded(fn):
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Refused as exc:
            return result(False, str(exc))
    wrapper.__doc__ = fn.__doc__
    return wrapper


@guarded
def movie(stamp, title, year):
    job, src = waiting_job(stamp, want_dir=False)
    year = str(year).strip()
    if not re.fullmatch(r"(18|19|20)\d\d", year):
        raise Refused(f"{year!r} doesn't look like a year")
    name = sanitize(f"{title} ({year})")
    if not name or name.startswith("("):
        raise Refused(f"{title!r} doesn't work as a title")
    named = os.path.join(os.path.dirname(src), name + os.path.splitext(src)[1])
    if os.path.exists(named) and named != src:
        raise Refused(f"something named {os.path.basename(named)} is already waiting for review")
    os.rename(src, named)
    log(f"name: '{job.get('label')}' named {name}")
    landed = str(sorter.handle_video(Path(named), force_type="movie"))
    if under(landed, conf().movies):
        filed = os.path.splitext(os.path.basename(landed))[0]
        jobs.update(stamp, stage="done", title=filed, path=landed)
        return result(True, f"filed as {filed}", landed)
    jobs.update(stamp, path=landed)
    return result(False, f"couldn't file {name} (it may already be in the library), so it's "
                         f"still at {landed}", landed)


@guarded
def cd(stamp, artist, album, year="", tracks=None):
    from mutagen import File as MutagenFile
    job, src = waiting_job(stamp, want_dir=True)
    if job.get("kind") == "tv":
        raise Refused("that rip is a TV disc, not a CD")
    artist, album = (artist or "").strip(), (album or "").strip()
    if not sanitize(artist) or not sanitize(album):
        raise Refused("I need both the artist and the album")
    year = str(year or "").strip()
    names = [str(t).strip() for t in tracks or []]
    flacs = sorted(os.path.join(src, f) for f in os.listdir(src) if f.endswith(".flac"))
    if not flacs:
        raise Refused(f"no FLAC files in {src}")
    if names and len(names) != len(flacs):
        raise Refused(f"you gave {len(names)} track names but the disc has {len(flacs)} tracks")
    for i, f in enumerate(flacs, 1):
        tags = MutagenFile(f, easy=True)
        tags["artist"] = tags["albumartist"] = artist
        tags["album"] = album
        tags["tracknumber"] = f"{i}/{len(flacs)}"
        if re.fullmatch(r"\d{4}", year):
            tags["date"] = year
        if names:
            tags["title"] = names[i - 1]
        elif not tags.get("title"):
            tags["title"] = f"Track {i:02d}"
        tags.save()
    log(f"name: audio CD {stamp} tagged as {artist} - {album}")
    landed = [str(sorter.handle_music(Path(f))) for f in flacs]
    review = [p for p in landed if not under(p, conf().music)]
    if os.path.isdir(src) and not any(f.endswith(".flac") for _, _, fs in os.walk(src) for f in fs):
        shutil.rmtree(src)  # only cover art / logs left behind
    if not review:
        album_dir = os.path.dirname(landed[0])
        jobs.update(stamp, stage="done", title=f"{artist} - {album}", path=album_dir)
        return result(True, f"filed {len(flacs)} tracks", album_dir)
    where = os.path.dirname(review[0])
    # Loose tracks in the review folder can't be told apart by folder, so no automatic retry.
    jobs.update(stamp, stage="failed", message="Some tracks need a look", path=where)
    return result(False, f"{len(review)} of {len(flacs)} tracks didn't file cleanly; see {where}",
                  where)


@guarded
def tv(stamp, show, season, first_episode, year="", skip=None):
    """Name a TV disc's episodes 'Show (Year) S01E05.mkv' and file them."""
    job, src = waiting_job(stamp, want_dir=True)
    if job.get("kind") != "tv":
        raise Refused("that rip isn't a TV disc")
    show = sanitize(show or "")
    year = str(year or "").strip()
    season, first = str(season or "").strip(), str(first_episode or "").strip()
    if not show:
        raise Refused("I need the show's name")
    if not season.isdigit() or not first.isdigit():
        raise Refused("I need the season and the first episode number")
    files = sorted(f for f in os.listdir(src) if f.endswith(".mkv"))
    if not files:
        raise Refused(f"no episodes in {src}")
    skip = {int(n) for n in skip or [] if str(n).strip().isdigit()}
    if any(n < 1 or n > len(files) for n in skip):
        raise Refused(f"the disc has {len(files)} titles, so I can't skip {sorted(skip)}")
    keep = [f for i, f in enumerate(files, 1) if i not in skip]
    if not keep:
        raise Refused("that would skip every title")
    base = f"{show} ({year})" if re.fullmatch(r"(19|20)\d\d", year) else show
    numbers = range(int(first), int(first) + len(keep))
    names = [f"{base} S{int(season):02d}E{n:02d}.mkv" for n in numbers]
    episodes = f"S{int(season):02d}E{numbers[0]:02d}" + (f"-E{numbers[-1]:02d}" if len(keep) > 1 else "")
    log(f"name: TV disc '{job.get('label')}' named {base} {episodes}"
        + (f", skipped titles {sorted(skip)}" if skip else ""))
    landed = []
    for f, n in zip(keep, names):
        named = os.path.join(src, n)
        os.rename(os.path.join(src, f), named)
        landed.append(str(sorter.handle_video(Path(named), force_type="episode")))
    filed = [p for p in landed if under(p, conf().tv)]
    if os.path.isdir(src) and not os.listdir(src):
        os.rmdir(src)
    if len(filed) == len(keep):
        season_dir = os.path.dirname(filed[0])
        title = f"{os.path.basename(os.path.dirname(season_dir))} {episodes}"
        note = f" ({len(skip)} skipped title(s) left in {src})" if skip else ""
        jobs.update(stamp, stage="done", title=title, path=season_dir)
        return result(True, f"filed {len(keep)} episodes as {title}{note}", season_dir)
    left = [p for p in landed if not under(p, conf().tv)]
    jobs.update(stamp, stage="failed", message="Some episodes need a look",
                path=os.path.dirname(left[0]))
    return result(False, f"{len(left)} of {len(keep)} episodes didn't file cleanly (often an "
                         f"episode that's already in the library); see {os.path.dirname(left[0])}",
                  os.path.dirname(left[0]))
