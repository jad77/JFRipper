"""Tests. Run with:  python -m unittest discover -s tests -v

The end-to-end tests need ffmpeg and guessit/mutagen installed; they use a fake makemkvcon
and a fake drive, so no disc or drive is needed and nothing outside a temp folder is touched.
"""
import copy
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from jfripper import config  # noqa: E402

HAVE_FFMPEG = shutil.which("ffmpeg") is not None or os.path.exists("/usr/lib/jellyfin-ffmpeg/ffmpeg")
HAVE_DEPS = all(importlib.util.find_spec(m) for m in ("guessit", "mutagen"))


def make_config(root, **overrides):
    data = copy.deepcopy(config.DEFAULTS)
    data["library"]["root"] = root
    data["state_dir"] = os.path.join(root, ".state")
    data["sorter"]["tmdb_api_key"] = ""
    for section, values in overrides.items():
        data[section].update(values)
    c = config.Config(data)
    config.use(c)
    os.environ.pop("TMDB_API_KEY", None)
    c.ensure_dirs()
    return c


class TempLibrary(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="jfripper-test-")
        self.cfg = make_config(self.root, encode={"encoder": "x264"})

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)


class ConfigTests(unittest.TestCase):
    def test_unknown_key_is_refused(self):
        with self.assertRaises(ValueError):
            config.merge(copy.deepcopy(config.DEFAULTS), {"rip": {"min_movie_minuts": 5}})

    def test_load_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('[library]\nroot = "/srv/media"\nmovies = "/elsewhere/Films"\n[encode]\nquality = 19\n')
        try:
            c = config.load(f.name)
            self.assertEqual(c.movies, "/elsewhere/Films")
            self.assertEqual(c.tv, "/srv/media/TV")
            self.assertEqual(c.encode["quality"], 19)
        finally:
            os.remove(f.name)

    def test_bad_cd_format_is_refused(self):
        data = copy.deepcopy(config.DEFAULTS)
        data["cd"]["format"] = "mp4"
        with self.assertRaises(ValueError):
            config.Config(data)

    def test_example_config_matches_defaults(self):
        path = os.path.join(os.path.dirname(config.__file__), "config.example.toml")
        with open(path, "rb") as f:
            data = config.tomllib.load(f)
        merged = config.merge(copy.deepcopy(config.DEFAULTS), data)
        self.assertEqual(merged, config.DEFAULTS)


class DiscLogicTests(TempLibrary):
    def test_nice_name(self):
        from jfripper.rip import nice_name
        self.assertEqual(nice_name("THE_MATRIX_WS"), "The Matrix")
        self.assertEqual(nice_name("YOU'VE_GOT_MAIL"), "You've Got Mail")
        self.assertIsNone(nice_name("DVD_VIDEO"))
        self.assertIsNone(nice_name("12345"))

    def test_tv_hint(self):
        from jfripper.rip import tv_hint
        self.assertEqual(tv_hint("GILMORE_GIRLS_S1_D2"), "Gilmore Girls, season 1, disc 2")
        self.assertEqual(tv_hint("the BIG BANG THEORY S1-1"), "The Big Bang Theory, season 1, disc 1")
        self.assertEqual(tv_hint("AB60NNW1#3120"), "")
        self.assertEqual(tv_hint("ALIEN"), "")

    def test_tv_episodes(self):
        from jfripper.rip import tv_episodes
        m = 60
        # movie with extras
        self.assertIsNone(tv_episodes({0: 120 * m, 1: 20 * m, 2: 18 * m}, {}))
        # six sitcom episodes + a Play All + a duplicate
        titles = {i: (21 * m + i * 10) for i in range(6)}
        titles[6] = sum(titles.values())
        titles[7] = titles[0]
        sizes = {7: 0, 0: 0}
        self.assertEqual(tv_episodes(titles, sizes), [0, 1, 2, 3, 4, 5])
        # short kids' movie + two featurettes is not TV
        self.assertIsNone(tv_episodes({0: 68 * m, 1: 16 * m, 2: 16 * m}, {}))
        # drop a short featurette
        self.assertEqual(tv_episodes({0: 44 * m, 1: 45 * m, 2: 15 * m}, {}), [0, 1])

    def test_drutil_parsing(self):
        from jfripper.drives import parse_drutil_list, parse_drutil_status
        listing = textwrap.dedent("""\
               Vendor   Product           Rev   Bus       SupportLevel
            1  HL-DT-ST BD-RE  BU40N      1.03  USB       Unsupported
        """)
        self.assertEqual(parse_drutil_list(listing), [(1, "HL-DT-ST BD-RE BU40N 1.03 USB Unsupported")])
        status = textwrap.dedent("""\
             Vendor   Product           Rev
             HL-DT-ST BD-RE  BU40N      1.03

                       Type: DVD-ROM              Name: /dev/disk4
                   Sessions: 1                  Tracks: 1
        """)
        self.assertEqual(parse_drutil_status(status), ("DVD-ROM", "/dev/disk4"))
        self.assertEqual(parse_drutil_status("           Type: No Media Inserted\n"), ("", ""))

    def test_makemkv_messages(self):
        from jfripper import makemkv
        out = ('MSG:5010,0,0,"Failed to open disc","",""\n'
               'MSG:2003,0,3,"Error \\"x\\" Read Error at offset 7964590080","",""\n')
        self.assertEqual(len(makemkv.messages(out)), 2)
        self.assertEqual(len(makemkv.read_errors(out)), 1)

    def test_encoder_args_valid(self):
        from jfripper import encode
        for name in encode.ENCODER_NAMES:
            pre, vf, args = encode.encoder_args(name)
            self.assertIn("-c:v", args)
            self.assertTrue(vf.startswith("bwdif"))
        with self.assertRaises(ValueError):
            encode.encoder_args("nope")


# ---------------------------------------------------------------- end to end

FAKE_MAKEMKV = r'''#!{python}
"""Stands in for makemkvcon: `info` describes the disc in $FAKE_DISC; `mkv` copies its file."""
import json, os, shutil, sys
disc = json.load(open(os.environ["FAKE_DISC"]))
args = [a for a in sys.argv[1:] if not a.startswith("-")]
progress = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--progress=")), None)
if args[0] == "info":
    if args[1] == "disc:9999":
        sys.exit(0)
    print('CINFO:1,6206,"DVD disc"')
    print('CINFO:2,0,"%s"' % disc["label"])
    for i, secs in enumerate(disc["titles"]):
        print('TINFO:%d,9,0,"%d:%02d:%02d"' % (i, secs // 3600, secs % 3600 // 60, secs % 60))
        print('TINFO:%d,11,0,"%d"' % (i, 1000000))
        print('SINFO:%d,0,19,0,"720x480"' % i)
        print('SINFO:%d,0,30,0,"Mpeg2"' % i)
elif args[0] == "mkv":
    title, folder = args[2], args[3]
    shutil.copy(disc["file"], os.path.join(folder, "title_t%02d.mkv" % int(title)))
    if progress:
        open(progress, "w").write("PRGV:0,1,1\n")
    print('MSG:5036,0,1,"Copy complete. 1 titles saved.","",""')
'''


class FakeDrive:
    name = "fake"
    path = "/dev/fake"
    audio_cd_supported = True

    def __init__(self):
        self.ejected = 0

    def __repr__(self):
        return self.path

    def has_disc(self):
        return True

    def is_audio_cd(self):
        return False

    def media_kind(self):
        return "dvd"

    def eject(self):
        self.ejected += 1

    def set_speed(self, speed):
        pass

    def makemkv_target(self):
        return "dev:/dev/fake"


@unittest.skipUnless(HAVE_FFMPEG and HAVE_DEPS, "needs ffmpeg, guessit and mutagen")
class EndToEndTests(TempLibrary):
    def setUp(self):
        super().setUp()
        from jfripper import encode, makemkv, notify
        self.cfg.rip["makemkvcon"] = os.path.join(self.root, "makemkvcon")
        with open(self.cfg.rip["makemkvcon"], "w") as f:
            f.write(FAKE_MAKEMKV.replace("{python}", sys.executable))
        os.chmod(self.cfg.rip["makemkvcon"], 0o755)
        makemkv.WATCH_POLL = 0.2
        self._set_settings = makemkv.set_settings
        makemkv.set_settings = lambda settings: None  # never touch the real MakeMKV settings
        self.sent = []
        self._notify = notify.notify
        import jfripper.rip, jfripper.cd
        for mod in (encode, jfripper.rip, jfripper.cd):
            mod.notify = lambda s, b: self.sent.append((s, b))
        # A 6-second interlaced-flagged MPEG-2 "DVD" title with stereo AC-3.
        self.src = os.path.join(self.root, "source.mkv")
        subprocess.run([encode.tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=720x480:rate=30000/1001", "-f", "lavfi", "-i",
                        "sine=frequency=440:sample_rate=48000", "-t", "6", "-c:v", "mpeg2video",
                        "-flags", "+ilme+ildct", "-c:a", "ac3", "-ac", "2", self.src], check=True)

    def tearDown(self):
        from jfripper import makemkv
        makemkv.set_settings = self._set_settings
        super().tearDown()

    def disc(self, label, titles):
        path = os.path.join(self.root, "disc.json")
        with open(path, "w") as f:
            json.dump({"label": label, "titles": titles, "file": self.src}, f)
        os.environ["FAKE_DISC"] = path

    def test_movie_disc_needs_name_then_files(self):
        from jfripper import encode, jobs, naming, rip
        self.disc("SOME_FILM_WS", [95 * 60, 3 * 60 * 0 + 16 * 60])
        drive = FakeDrive()
        work = rip.rip_video(drive)
        self.assertTrue(work and drive.ejected)
        encode.encode(work)
        job = jobs.all_jobs()[0]
        # No TMDB key and no year in the label, so it waits for a name.
        self.assertEqual(job["stage"], "needs_name", job)
        self.assertTrue(job["path"].startswith(self.cfg.review))
        self.assertIn("needs a name", self.sent[-1][0])
        self.assertFalse(os.path.exists(work))
        # The output really is H.264 now, with the audio kept.
        out = subprocess.run([encode.tool("ffprobe"), "-v", "error", "-show_entries",
                              "stream=codec_name", "-of", "csv=p=0", job["path"]],
                             capture_output=True, text=True).stdout.split()
        self.assertEqual(out[:2], ["h264", "ac3"])

        bad = naming.movie(job["stamp"], "Some Film", "19x9")
        self.assertFalse(bad["ok"])
        res = naming.movie(job["stamp"], "Some Film", "1999")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["path"], os.path.join(self.cfg.movies, "Some Film (1999)",
                                                   "Some Film (1999).mkv"))
        self.assertEqual(jobs.load(job["stamp"])["stage"], "done")
        again = naming.movie(job["stamp"], "Some Film", "1999")
        self.assertFalse(again["ok"])

    def test_label_with_year_files_itself_even_if_it_looks_like_tv(self):
        from jfripper import encode, jobs, rip
        self.disc("CASABLANCA_1942", [102 * 60])
        encode.encode(rip.rip_video(FakeDrive()))
        job = jobs.all_jobs()[0]
        self.assertEqual(job["stage"], "done", job)
        self.assertEqual(job["path"], os.path.join(self.cfg.movies, "Casablanca (1942)",
                                                   "Casablanca (1942).mkv"))

    def test_tv_disc(self):
        from jfripper import encode, jobs, naming, rip
        self.disc("TEST_SHOW_S1_D2", [22 * 60, 21 * 60 + 30, 22 * 60 + 10])
        encode.encode(rip.rip_video(FakeDrive()))
        job = jobs.all_jobs()[0]
        self.assertEqual((job["stage"], job["kind"], job["episodes"]), ("needs_name", "tv", 3))
        self.assertEqual(sorted(os.listdir(job["path"])),
                         ["Episode 01.mkv", "Episode 02.mkv", "Episode 03.mkv"])
        self.assertIn("Test Show, season 1, disc 2", self.sent[-1][1])
        res = naming.tv(job["stamp"], "Test Show", "1", "5", "2004", ["3"])
        self.assertTrue(res["ok"], res)
        season = os.path.join(self.cfg.tv, "Test Show (2004)", "Season 01")
        self.assertEqual(sorted(os.listdir(season)),
                         ["Test Show S01E05.mkv", "Test Show S01E06.mkv"])

    def test_no_movie_length_title(self):
        from jfripper import jobs, rip
        self.disc("BONUS", [16 * 60])
        drive = FakeDrive()
        self.assertIsNone(rip.rip_video(drive))
        self.assertEqual(jobs.all_jobs()[0]["stage"], "failed")
        self.assertEqual(drive.ejected, 1)


# ---------------------------------------------------------------- audio CDs

ABBEY_ROAD = {"id": "CK7Xuml3r3x0Xcde31RT2Uy.2cU-", "sectors": 213430,
              "offsets": [183, 19680, 33408, 48953, 64453, 77290, 112325, 126245, 138648, 156845,
                          167820, 172795, 178243, 187073, 193940, 201175, 211690]}


def toc_plist(offsets, leadout, lead_in_included=True, data_track=None):
    """A .TOC.plist like the one macOS puts on a mounted audio CD."""
    import plistlib
    shift = 0 if lead_in_included else 150
    tracks = [{"Point": i + 1, "Start Block": o - shift, "Data": False}
              for i, o in enumerate(offsets)]
    sessions = [{"First Track": 1, "Last Track": len(offsets), "Leadout Block": leadout - shift,
                 "Session Number": 1, "Track Array": tracks}]
    if data_track:
        sessions.append({"First Track": len(offsets) + 1, "Last Track": len(offsets) + 1,
                         "Leadout Block": data_track + 30000, "Session Number": 2,
                         "Track Array": [{"Point": len(offsets) + 1, "Start Block": data_track,
                                          "Data": True}]})
    return plistlib.dumps({"Format": "CDDA", "Sessions": sessions})


class CDLogicTests(TempLibrary):
    def test_disc_id_matches_musicbrainz(self):
        from jfripper.cd import disc_id
        o = ABBEY_ROAD["offsets"]
        self.assertEqual(disc_id(1, len(o), ABBEY_ROAD["sectors"], o), ABBEY_ROAD["id"])

    def test_read_toc_either_numbering(self):
        from jfripper.cd import disc_id, read_toc
        for included in (True, False):
            path = os.path.join(self.root, f"toc-{included}.plist")
            with open(path, "wb") as f:
                f.write(toc_plist(ABBEY_ROAD["offsets"], ABBEY_ROAD["sectors"], included))
            self.assertEqual(disc_id(*read_toc(path)), ABBEY_ROAD["id"], included)

    def test_read_toc_enhanced_cd(self):
        from jfripper.cd import read_toc
        path = os.path.join(self.root, "toc.plist")
        with open(path, "wb") as f:
            f.write(toc_plist([150, 20000, 40000], 60000, data_track=72000))
        first, last, leadout, offsets = read_toc(path)
        self.assertEqual((first, last, leadout, offsets), (1, 3, 72000 - 11400, [150, 20000, 40000]))

    def test_musicbrainz_parse(self):
        import io
        from jfripper import cd
        reply = {"releases": [{"id": "rel-1", "title": "Abbey Road", "date": "1987-04-30",
                               "artist-credit": [{"name": "The Beatles", "joinphrase": ""}],
                               "media": [{"position": 1, "discs": [{"id": ABBEY_ROAD["id"]}],
                                          "tracks": [{"title": "Come Together"},
                                                     {"title": "Something"}]}]}]}
        real = cd.urllib.request.urlopen
        cd.urllib.request.urlopen = lambda req, timeout: io.BytesIO(json.dumps(reply).encode())
        try:
            album = cd.musicbrainz_lookup(ABBEY_ROAD["id"])
        finally:
            cd.urllib.request.urlopen = real
        self.assertEqual((album["artist"], album["album"], album["date"], album["discs"]),
                         ("The Beatles", "Abbey Road", "1987", 1))
        self.assertEqual(album["tracks"][1], {"title": "Something", "artist": "The Beatles"})


@unittest.skipUnless(HAVE_FFMPEG and HAVE_DEPS, "needs ffmpeg, guessit and mutagen")
class Mp3ConversionTests(TempLibrary):
    def test_flac_to_mp3_keeps_tags(self):
        from jfripper import cd, encode
        flac = os.path.join(self.root, "01.flac")
        subprocess.run([encode.tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "sine=d=2",
                        "-ac", "2", "-ar", "44100", flac], check=True)
        t = cd.open_tags(flac)
        t.update({"title": "Song", "artist": "Band", "albumartist": "Band", "album": "LP",
                  "tracknumber": "1/9", "discnumber": "1/2", "date": "1999",
                  "musicbrainz_albumid": "rel-9"})
        t.save()
        mp3 = cd.to_mp3(flac, os.path.join(self.root, "01"))
        cd.copy_tags(flac, mp3)
        got = cd.open_tags(mp3)
        for key in cd.TAG_KEYS:
            self.assertEqual(got[key][0], t[key][0], key)


class FakeMacCD(FakeDrive):
    def __init__(self, mount):
        super().__init__()
        self.mount = mount

    def is_audio_cd(self):
        return True

    def mount_point(self, wait=20):
        return self.mount


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("afconvert") and HAVE_FFMPEG
                     and HAVE_DEPS, "needs macOS afconvert, ffmpeg, guessit and mutagen")
class MacCDTests(TempLibrary):
    def setUp(self):
        super().setUp()
        from jfripper import cd
        self.cd = cd
        self.sent = []
        cd.notify = lambda s, b: self.sent.append((s, b))
        # What macOS shows for a mounted 3-track audio CD.
        self.mount = os.path.join(self.root, "Audio CD")
        os.makedirs(self.mount)
        for n in (1, 2, 3):
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                            f"sine=frequency={220 * n}:sample_rate=44100", "-t", "2", "-ac", "2",
                            "-c:a", "pcm_s16be", os.path.join(self.mount, f"{n} Track {n}.aiff")],
                           check=True)
        with open(os.path.join(self.mount, ".TOC.plist"), "wb") as f:
            f.write(toc_plist([150, 300, 450], 600))
        self._lookup = cd.musicbrainz_lookup

    def tearDown(self):
        self.cd.musicbrainz_lookup = self._lookup
        super().tearDown()

    def test_known_cd_is_filed(self):
        self.cd.musicbrainz_lookup = lambda discid: {
            "artist": "Some Band", "album": "First Album", "date": "2001", "album_id": "rel-1",
            "disc": 1, "discs": 1,
            "tracks": [{"title": t, "artist": "Some Band"} for t in ("One", "Two", "Three")]}
        drive = FakeMacCD(self.mount)
        self.cd.rip_cd(drive)
        album = os.path.join(self.cfg.music, "Some Band", "First Album")
        self.assertEqual(sorted(os.listdir(album)), ["01 One.flac", "02 Two.flac", "03 Three.flac"])
        self.assertEqual(drive.ejected, 1)
        from mutagen import File as MutagenFile
        tags = MutagenFile(os.path.join(album, "02 Two.flac"), easy=True)
        self.assertEqual((tags["album"][0], tags["tracknumber"][0], tags["date"][0]),
                         ("First Album", "2/3", "2001"))
        self.assertIn("is in Jellyfin", self.sent[-1][0])

    def test_known_cd_as_mp3(self):
        self.cfg.cd["format"] = "mp3"
        self.cd.musicbrainz_lookup = lambda discid: {
            "artist": "Some Band", "album": "First Album", "date": "2001", "album_id": "rel-1",
            "disc": 1, "discs": 1,
            "tracks": [{"title": t, "artist": "Some Band"} for t in ("One", "Two", "Three")]}
        self.cd.rip_cd(FakeMacCD(self.mount))
        album = os.path.join(self.cfg.music, "Some Band", "First Album")
        self.assertEqual(sorted(os.listdir(album)), ["01 One.mp3", "02 Two.mp3", "03 Three.mp3"])
        from mutagen import File as MutagenFile
        f = MutagenFile(os.path.join(album, "03 Three.mp3"), easy=True)
        self.assertEqual((f["title"][0], f["albumartist"][0], f["musicbrainz_albumid"][0]),
                         ("Three", "Some Band", "rel-1"))
        self.assertGreater(f.info.bitrate, 0)

    def test_unknown_cd_waits_then_names(self):
        from jfripper import jobs, naming
        self.cd.musicbrainz_lookup = lambda discid: None
        self.cd.rip_cd(FakeMacCD(self.mount))
        job = jobs.all_jobs()[0]
        self.assertEqual(job["stage"], "needs_name")
        self.assertEqual(len(os.listdir(job["path"])), 3)
        res = naming.cd(job["stamp"], "Mystery Band", "Demo", "1999", ["A", "B", "C"])
        self.assertTrue(res["ok"], res)
        self.assertEqual(sorted(os.listdir(os.path.join(self.cfg.music, "Mystery Band", "Demo"))),
                         ["01 A.flac", "02 B.flac", "03 C.flac"])


@unittest.skipUnless(HAVE_FFMPEG and HAVE_DEPS, "needs ffmpeg, guessit and mutagen")
class SorterTests(TempLibrary):
    def test_music_and_unknown(self):
        from jfripper import encode, sorter
        flac = os.path.join(self.cfg.incoming, "x.flac")
        subprocess.run([encode.tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "sine=d=1",
                        "-metadata", "artist=Some Band", "-metadata", "album=First: Album",
                        "-metadata", "title=Song/One", "-metadata", "track=3/9", flac], check=True)
        junk = os.path.join(self.cfg.incoming, "notes.xyz")
        open(junk, "w").close()
        old = time.time() - 3600
        for p in (flac, junk):
            os.utime(p, (old, old))
        sorter.process()
        self.assertTrue(os.path.exists(os.path.join(self.cfg.music, "Some Band", "First Album",
                                                    "03 SongOne.flac")))
        self.assertTrue(os.path.exists(os.path.join(self.cfg.review, "notes.xyz")))


class SettingsTests(TempLibrary):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.root, "config.toml")
        self.cfg.path = self.path

    def test_secrets_never_leave_and_blank_keeps_them(self):
        from jfripper import settings
        res = settings.save({"notify.smtp_host": "mail.example.com",
                             "notify.smtp_password": "hunter2", "cd.format": "mp3"})
        self.assertTrue(res["ok"], res)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        view = settings.view()
        self.assertNotIn("hunter2", json.dumps(view))
        pw = next(f for f in view["fields"] if f["key"] == "smtp_password")
        self.assertEqual((pw["is_set"], "value" in pw), (True, False))
        # Saving again with the password left blank keeps it; the new values are live.
        self.assertTrue(settings.save({"notify.smtp_password": "", "cd.mp3_quality": "2"})["ok"])
        c = config.conf()
        self.assertEqual((c.notify["smtp_password"], c.cd["format"], c.cd["mp3_quality"]),
                         ("hunter2", "mp3", 2))
        # The file only holds what differs from the defaults, and loads back the same.
        self.assertEqual(config.load(self.path).data, c.data)
        self.assertTrue(settings.save({"notify.smtp_password": {"clear": True}})["ok"])
        self.assertEqual(config.conf().notify["smtp_password"], "")

    def test_basics_and_defaults(self):
        from jfripper import settings
        v = settings.view()
        basic = {f["key"] for f in v["fields"] if f["basic"]}
        self.assertEqual(basic, {"root", "tmdb_api_key", "format", "ntfy_url", "webhook_url",
                                 "smtp_host", "smtp_user", "smtp_password", "smtp_to"})
        q = next(f for f in v["fields"] if f["key"] == "quality")
        self.assertEqual((q["is_default"], q["default"], q["default_value"]), (True, "21", 21))
        self.assertTrue(settings.save({"encode.quality": "19"})["ok"])
        q = next(f for f in settings.view()["fields"] if f["key"] == "quality")
        self.assertEqual((q["is_default"], q["value"]), (False, 19))

    def test_refusals(self):
        from jfripper import settings
        for changes in ({"notify.command": "rm -rf /"}, {"encode.ffmpeg": "/tmp/evil"},
                        {"status.port": "99999"}, {"cd.format": "ogg"},
                        {"library.root": "/no/such/folder"}, {"notify.webhook_url": "file:///etc"},
                        {"rip.drives": "sr0; reboot"}, {"nope.nothing": 1}):
            res = settings.save(changes)
            self.assertFalse(res["ok"], changes)
        self.assertFalse(os.path.exists(self.path))

    def test_restart_flag_and_drives(self):
        from jfripper import settings
        res = settings.save({"status.port": "8100", "rip.drives": "/dev/sr0, /dev/sr1"})
        self.assertTrue(res["ok"], res)
        self.assertEqual(sorted(res["restart"]), ["Drives", "Port"])
        self.assertEqual(config.conf().rip["drives"], ["/dev/sr0", "/dev/sr1"])


class StatusPageTests(TempLibrary):
    def test_api(self):
        from http.server import ThreadingHTTPServer
        from jfripper import jobs, status
        jobs.update("20260101-120000", label="X", stage="needs_name", path=self.cfg.review + "/X.mkv",
                    finished=time.time())
        server = ThreadingHTTPServer(("127.0.0.1", 0), status.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            data = json.load(urllib.request.urlopen(base + "/api/status"))
            self.assertEqual([j["stamp"] for j in data["needs_name"]], ["20260101-120000"])
            self.assertIn(b"JFRipper", urllib.request.urlopen(base + "/").read())
            # Not JSON -> refused (protects against cross-site form posts).
            req = urllib.request.Request(base + "/api/name", data=b"stamp=1", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(req)
            self.assertEqual(err.exception.code, 403)
            req = urllib.request.Request(base + "/api/name", method="POST",
                                         data=json.dumps({"stamp": "20260101-120000",
                                                          "title": "X", "year": "2000"}).encode(),
                                         headers={"Content-Type": "application/json"})
            res = json.load(urllib.request.urlopen(req))
            self.assertFalse(res["ok"])  # the file doesn't exist
            self.assertIn("isn't there", res["message"])
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
