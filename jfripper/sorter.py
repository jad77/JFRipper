"""File media into a Jellyfin-friendly layout.

Movies -> Movies/Title (Year)/Title (Year).ext
TV     -> TV/Show (Year)/Season 01/Show S01E02 - Episode Title.ext
Music  -> Music/Artist/Album/01 Track.ext   (from tags); images in the same folder
          (cover.jpg, folder.jpg ...) follow the tracks into that album
Anything it can't confidently identify -> the review folder (never guessed or overwritten).

Rips are filed directly by the ripper; process() also sorts anything you drop into the
incoming folder yourself.
"""
import json
import re
import shutil
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .config import conf
from .util import log as _log
from .util import sanitize

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts",
             ".mpg", ".mpeg", ".flv", ".webm"}
MUSIC_EXT = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma",
             ".alac", ".aiff"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
SUB_EXT = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt"}
PARTIAL_EXT = {".part", ".!qb", ".tmp", ".crdownload", ".downloading", ".partial"}

# Source folder in incoming -> album folders its tracks were filed into.
ALBUM_DIRS: dict = {}
_lock = threading.RLock()


def log(msg):
    _log(f"sorter: {msg}")


def P(attr) -> Path:
    return Path(getattr(conf(), attr))


def rel(p: Path) -> str:
    try:
        return str(p.relative_to(P("root")))
    except ValueError:
        return str(p)


# --- optional TMDB enrichment -------------------------------------------------
# With a TMDB v3 API key, titles/years are canonicalized and names a filename alone can't
# resolve (a movie with no year) are found. With no key, everything below no-ops and we
# fall back to filename-only parsing.
TMDB_BASE = "https://api.themoviedb.org/3"


def _tmdb_get(path: str, params: dict):
    q = urllib.parse.urlencode({**params, "api_key": conf().tmdb_key()})
    with urllib.request.urlopen(f"{TMDB_BASE}{path}?{q}", timeout=10) as r:
        return json.load(r)


# Disc labels often lead with the studio ("Marvel's The Avengers"), which TMDB matched to the
# wrong film (an unreleased sequel). Search without it.
STUDIO_PREFIX = re.compile(r"^(?:walt\s+)?(?:disney[\s•-]*pixar|disney|pixar|marvel|dreamworks|"
                           r"lucasfilm|universal)(?:'s|s'|’s)?\s+", re.I)


def released(result) -> bool:
    """A disc can't hold a film that isn't out yet; TMDB lists announced ones too."""
    rd = result.get("release_date") or ""
    return bool(rd) and rd <= time.strftime("%Y-%m-%d")


def tmdb_movie(title: str, year):
    """Return (canonical_title, year_str) from TMDB, or None."""
    if not conf().tmdb_key() or not title:
        return None
    title = STUDIO_PREFIX.sub("", title).strip() or title
    try:
        params = {"query": title}
        if year:
            params["year"] = year
        results = (_tmdb_get("/search/movie", params).get("results") or [])
        if not results and year:  # retry without the year constraint
            results = (_tmdb_get("/search/movie", {"query": title}).get("results") or [])
        results = [r for r in results if released(r)]
        if not results:
            return None
        best = results[0]
        if year:  # prefer an exact year match if present
            best = next((r for r in results
                         if (r.get("release_date") or "")[:4] == str(year)), best)
        rt, rd = best.get("title"), (best.get("release_date") or "")[:4]
        return (rt, rd) if rt and rd else None
    except Exception as e:
        log(f"TMDB movie lookup failed for {title!r}: {e}")
        return None


def tmdb_tv(title: str, year):
    """Return (canonical_name, first_air_year_or_None) from TMDB, or None."""
    if not conf().tmdb_key() or not title:
        return None
    try:
        params = {"query": title}
        if year:
            params["first_air_date_year"] = year
        results = (_tmdb_get("/search/tv", params).get("results") or [])
        if not results and year:
            results = (_tmdb_get("/search/tv", {"query": title}).get("results") or [])
        if not results:
            return None
        best = results[0]
        name, air = best.get("name"), (best.get("first_air_date") or "")[:4]
        return (name, air or None) if name else None
    except Exception as e:
        log(f"TMDB tv lookup failed for {title!r}: {e}")
        return None
# ------------------------------------------------------------------------------


def is_settled(p: Path) -> bool:
    try:
        return (time.time() - p.stat().st_mtime) > int(conf().sorter["settle_seconds"])
    except FileNotFoundError:
        return False


def is_review(p: Path) -> bool:
    return P("review").resolve() in p.resolve().parents


def move_to_review(src: Path, reason: str) -> Path:
    review = P("review")
    review.mkdir(parents=True, exist_ok=True)
    if src.parent.resolve() == review.resolve():
        log(f"review ({reason}): {src.name} stays where it is")
        return src
    dest = review / src.name
    i = 1
    while dest.exists():
        dest = review / f"{src.stem}.{i}{src.suffix}"
        i += 1
    shutil.move(str(src), str(dest))
    log(f"review ({reason}): {src.name}")
    return dest


def move_into(src: Path, dest_dir: Path, dest_name: str):
    """Move src to dest_dir/dest_name. Returns (where it went, filed?): an existing file there
    is never overwritten; src goes to review instead."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / dest_name
    if dest.exists():
        return move_to_review(src, "target-exists"), False
    shutil.move(str(src), str(dest))
    log(f"moved {src.name} -> {rel(dest)}")
    return dest, True


def move_sibling_subs(orig_parent: Path, stem: str, dest_dir: Path, base: str) -> None:
    """Carry .srt/.ass subs that share the video's stem into the same target folder."""
    if not orig_parent.exists():
        return
    for sib in orig_parent.iterdir():
        if sib.is_file() and sib.suffix.lower() in SUB_EXT and sib.stem.startswith(stem):
            tail = sib.name[len(stem):]          # e.g. ".en.srt" or ".srt"
            dest = dest_dir / (base + tail)
            if not dest.exists():
                shutil.move(str(sib), str(dest))
                log(f"moved subtitle {sib.name} -> {rel(dest)}")


def handle_video(src: Path, force_type=None) -> Path:
    """File one video; returns where it ended up. force_type ('movie' or 'episode') overrides
    the guess: disc rips are always one or the other, and a code-like label such as
    AB60NNW1#3120 would otherwise parse as a TV episode (S31E20)."""
    from guessit import guessit
    with _lock:
        if re.search(r"\bsample\b", src.name, re.I):
            return move_to_review(src, "sample")
        g = guessit(src.name, {"type": force_type} if force_type else None)
        typ = g.get("type")
        ext = src.suffix.lower()
        parent, stem = src.parent, src.stem

        if typ == "episode":
            title = g.get("title")
            season = g.get("season")
            episode = g.get("episode")
            if not title or season is None or episode is None:
                return move_to_review(src, "tv-incomplete")
            if isinstance(episode, list):
                ep_str = "".join(f"E{int(e):02d}" for e in episode)
            else:
                ep_str = f"E{int(episode):02d}"
            show = sanitize(title)
            year = g.get("year")
            enriched = tmdb_tv(show, year)  # canonical series name + first-air year
            if enriched:
                cname, cyear = enriched
                show = sanitize(cname)
                year = cyear or year
            show_folder = f"{show} ({year})" if year else show
            base = f"{show} S{int(season):02d}{ep_str}"
            ep_title = g.get("episode_title")
            if ep_title:
                base += f" - {sanitize(ep_title)}"
            dest_dir = P("tv") / show_folder / f"Season {int(season):02d}"
            dest, filed = move_into(src, dest_dir, base + ext)
            if filed:
                move_sibling_subs(parent, stem, dest_dir, base)
            return dest

        if typ == "movie":
            title = g.get("title")
            year = g.get("year")
            # TMDB (if configured) canonicalizes the name and can supply a missing year.
            enriched = tmdb_movie(sanitize(title), year) if title else None
            if enriched:
                ctitle, cyear = enriched
                folder = f"{sanitize(ctitle)} ({cyear})"
            elif title and year:
                # No TMDB match (or no key): fall back, but require a year as the
                # confidence signal since guessit invents a title from any filename.
                folder = f"{sanitize(title)} ({year})"
            else:
                return move_to_review(src, "movie-unresolved")
            dest_dir = P("movies") / folder
            dest, filed = move_into(src, dest_dir, folder + ext)
            if filed:
                move_sibling_subs(parent, stem, dest_dir, folder)
            return dest

        return move_to_review(src, "unknown-type")


def handle_music(src: Path) -> Path:
    with _lock:
        ext = src.suffix.lower()
        try:
            from mutagen import File as MutagenFile
            m = MutagenFile(str(src), easy=True)
        except Exception:
            m = None
        if not m:
            return move_to_review(src, "music-no-tags")

        def first(*keys):
            for k in keys:
                v = m.get(k)
                if v:
                    return v[0]
            return None

        artist = first("albumartist", "artist")
        album = first("album")
        title = first("title")
        track = first("tracknumber")
        if not artist or not album or not title:
            return move_to_review(src, "music-incomplete")
        try:
            tno = int(str(track).split("/")[0]) if track else 0
        except ValueError:
            tno = 0
        fname = (f"{tno:02d} " if tno else "") + sanitize(title) + ext
        album_dir = P("music") / sanitize(artist) / sanitize(album)
        dest, filed = move_into(src, album_dir, fname)
        if filed:
            ALBUM_DIRS.setdefault(src.parent, set()).add(album_dir)
        return dest


def handle_art(src: Path) -> None:
    """Album art goes with the album its folder's tracks were filed into, name kept."""
    if any(p.is_file() and p.suffix.lower() in MUSIC_EXT for p in src.parent.iterdir()):
        log(f"skip (art waiting for its tracks): {src.name}")
        return
    albums = ALBUM_DIRS.get(src.parent, set())
    if len(albums) != 1:
        move_to_review(src, "art-several-albums" if albums else "art-no-album")
        return
    move_into(src, next(iter(albums)), src.name)  # art already there -> review


def process() -> None:
    """Sort everything that has settled in the incoming folder."""
    incoming = P("incoming")
    if not incoming.exists():
        return
    with _lock:
        files = [p for p in sorted(incoming.rglob("*")) if p.is_file()]
        # Process videos/music first so a video can claim its sibling subtitle and album art
        # knows where its tracks went; a subtitle left over afterward is a true orphan.
        later = SUB_EXT | IMAGE_EXT
        ordered = [p for p in files if p.suffix.lower() not in later] + \
                  [p for p in files if p.suffix.lower() in IMAGE_EXT] + \
                  [p for p in files if p.suffix.lower() in SUB_EXT]
        for src in ordered:
            if not src.exists() or not src.is_file():  # already carried off as a sibling sub
                continue
            name, ext = src.name, src.suffix.lower()
            if name == ".DS_Store" or name.startswith("._"):
                src.unlink(missing_ok=True)
                continue
            if ext in PARTIAL_EXT or not is_settled(src):
                continue
            try:
                if ext in VIDEO_EXT:
                    handle_video(src)
                elif ext in MUSIC_EXT:
                    handle_music(src)
                elif ext in IMAGE_EXT:
                    handle_art(src)
                elif ext in SUB_EXT:
                    move_to_review(src, "subtitle-standalone")
                else:
                    move_to_review(src, f"unhandled-ext{ext}")
            except Exception as e:  # never let one bad file stop the batch
                log(f"error on {name}: {e!r}")
                try:
                    move_to_review(src, "exception")
                except Exception:
                    pass

        # prune now-empty subdirectories left behind in incoming
        for d in sorted(incoming.rglob("*"), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                try:
                    d.rmdir()
                except OSError:
                    pass
