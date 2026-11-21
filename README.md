# JFRipper

**Put a disc in the drive. A few minutes later it's in your Jellyfin library, properly named.**

JFRipper is a small background service for Linux and macOS that turns a disc drive into a
"drop slot" for your media server:

- **DVDs and Blu-rays** → the main feature is ripped with MakeMKV, converted to H.264 if it
  needs it (DVDs), named from [TMDB](https://www.themoviedb.org/), and filed as
  `Movies/Title (Year)/Title (Year).mkv`.
- **TV season discs** → every episode is ripped and converted, then waits for you to say
  which show, season and starting episode it is (a disc can't tell you), and is filed as
  `TV/Show (Year)/Season 01/Show S01E05.mkv`.
- **Audio CDs** → FLAC or MP3 (your choice), tagged from MusicBrainz, filed as
  `Music/Artist/Album/01 Track.flac`. On Linux via whipper (secure reads,
  AccurateRip-verified); on macOS with the built-in `afconvert`, nothing extra to install.
- **A status page** at `http://your-server:8097` shows what's ripping, when it'll finish,
  and lets you name anything it couldn't identify.
- **Notifications** when a disc is done, needs a name, or failed: ntfy, Discord/Slack or any
  webhook, email, or your own command.

It ejects the disc as soon as the rip is done, so you can feed it the next one while the
previous one converts.

> **Beta.** JFRipper grew out of a home setup that has ripped a shelf of discs on Linux.
> The macOS support is newer: the encoding, the status page and the CD conversion are
> tested on a Mac, but ripping from a real drive on a Mac isn't yet. Bug reports welcome.

> Only rip discs you own, and check the law where you live. MakeMKV is separate,
> closed-source software with its own license; JFRipper just drives it.

## What it does with a disc

1. **Detects the insert** (it polls the drive every few seconds; no udev rules needed).
2. **Scans it with MakeMKV.** One long title = a movie. Several episode-length titles of
   similar length and no feature = a TV disc ("Play All" titles and duplicate titles are
   recognised and skipped).
3. **Rips it, with a retry ladder for scratched discs.** If the drive hits read errors, it
   tries again slowed down with more retries, and finally slowed down *and* skipping
   unreadable spots the way a DVD player does. A rip that makes no progress for 10 minutes
   is given up rather than hanging for hours. The notification tells you if anything was
   skipped.
4. **Ejects** and queues the conversion (one at a time, so discs don't fight over the GPU).
5. **Converts the video** only when it has to:
   - MPEG-2 / VC-1 (DVDs, some old Blu-rays) → H.264, deinterlacing only frames that are
     flagged interlaced. About half the size, and it plays everywhere without transcoding.
   - H.264 / HEVC (most Blu-rays) → copied untouched.
   - The encoder is picked automatically: **VideoToolbox** on a Mac; **Intel Quick Sync**,
     **VA-API** or **NVIDIA NVENC** on Linux; software **x264** if there's no hardware encoder.
6. **Keeps all audio and subtitle tracks**, with two fixes for Jellyfin:
   - DTS / TrueHD / PCM discs get a Dolby Digital 5.1 track added as the default (phones and
     browsers can't play DTS, and Jellyfin's live DTS conversion can stall playback).
   - Subtitles are switched to "off by default". Discs mark them default, which makes
     Jellyfin burn them into the picture, a full re-encode on every play.
7. **Names and files it.** The disc label (`THE_MATRIX_WS` → "The Matrix") is looked up on
   TMDB; studio prefixes ("Marvel's", "Disney") are stripped and unreleased films ignored.
   If it can't be matched confidently, it goes to `_needs_review/` and you're asked for the
   name. It never overwrites a file.

## Requirements

| | Linux | macOS |
|---|---|---|
| Python | 3.9+ | 3.9+ (Homebrew `python` recommended) |
| [MakeMKV](https://www.makemkv.com/) | `makemkvcon` ([build guide](https://forum.makemkv.com/forum/viewtopic.php?f=3&t=224)) | the normal app in /Applications |
| ffmpeg | `ffmpeg` package, or Jellyfin's `jellyfin-ffmpeg` (used automatically if installed) | `brew install ffmpeg` |
| Audio CDs | `whipper` (optional) | built in (`afconvert`); no AccurateRip check |
| Drive | any DVD/Blu-ray drive, USB is fine | same |

A free [TMDB API key](https://www.themoviedb.org/settings/api) is strongly recommended.
Without one, a disc is only named automatically if its label includes the year.

## Install

```sh
# pipx keeps it in its own environment (apt install pipx / brew install pipx)
pipx install "git+https://github.com/jad77/JFRipper.git"

# Linux: also name CDs before whipper starts (needs libdiscid)
pipx install "jfripper[cd] @ git+https://github.com/jad77/JFRipper.git"
```

Then check what's missing:

```sh
jfripper check
```

### Configure

```sh
mkdir -p ~/.config/jfripper
jfripper config --example > ~/.config/jfripper/config.toml
```

Edit at least `library.root` (your Jellyfin media folder) and `sorter.tmdb_api_key`. Every
setting is documented in the file. The defaults put things here:

```
/mnt/media/
├── Movies/          ← add this folder to Jellyfin as a Movies library
├── TV/              ← ... as a Shows library
├── Music/           ← ... as a Music library
├── _incoming/       ← drop your own files here and they get sorted too
├── _needs_review/   ← things waiting for a name
└── _rip/            ← work in progress
```

Keep `_rip` on the same disk as the library so finished files move instantly.

Try one disc by hand before setting up the service:

```sh
jfripper rip          # rips, converts and files whatever is in the drive
```

### Run it as a service

**Linux (systemd).** The user it runs as needs to be in the `cdrom` group (to read the
drive) and `render`/`video` (for hardware encoding), and must be able to write to the library:

```sh
jfripper service | sudo tee /etc/systemd/system/jfripper.service
sudo systemctl daemon-reload
sudo systemctl enable --now jfripper
journalctl -u jfripper -f
```

**macOS (launchd).**

```sh
jfripper service > ~/Library/LaunchAgents/local.jfripper.plist
launchctl load -w ~/Library/LaunchAgents/local.jfripper.plist
```

In **System Settings → CDs & DVDs**, set "When you insert a video DVD" to *Ignore*, so DVD
Player doesn't grab the disc.

### FLAC or MP3 for CDs

```toml
[cd]
format = "mp3"      # default "flac"
mp3_quality = 0     # LAME VBR: 0 = best (~245 kbps), 2 = ~190 kbps, 4 = ~165 kbps
```

FLAC is lossless and the safe archive choice; MP3 is smaller and plays on anything. MP3s
are made with ffmpeg's LAME encoder (macOS has no MP3 encoder of its own). On Linux the
disc is still read and AccurateRip-checked by whipper first, then converted.

## Naming a disc it couldn't identify

You'll get a "needs a name" notification. Either use the form on the status page, or:

```sh
jfripper jobs                                         # find the job id
jfripper name movie 20261120-093000 "The Shop Around the Corner" 1940
jfripper name tv 20261120-093000 --show "Gilmore Girls" --season 1 --first 5
jfripper name tv 20261120-093000 --show "Gilmore Girls" --season 1 --first 5 --skip 7   # title 7 is an extra
jfripper name cd 20261120-093000 --artist "Artist" --album "Album" --year 1999
```

## Notifications

Set any of these in the `[notify]` section:

- `ntfy_url = "https://ntfy.sh/some-long-random-topic"`: push notifications via the ntfy app.
- `webhook_url = "..."`: POSTs JSON with `title`, `message`, `text` (Slack) and `content`
  (Discord). Works with Home Assistant webhooks too.
- `smtp_*`: plain email.
- `command = "/path/to/script"`: runs `script "<subject>"` with the message on stdin.

## Commands

| | |
|---|---|
| `jfripper run` | the service: watch drives, rip, convert, sort, status page |
| `jfripper rip [drive]` | rip, convert and file the disc in a drive right now |
| `jfripper name ...` | name a rip that's waiting for one |
| `jfripper jobs` | recent discs and their state |
| `jfripper drives` | optical drives and what's in them |
| `jfripper sort` | sort `_incoming` once |
| `jfripper encode <folder>` | redo the conversion of a kept rip |
| `jfripper check` | what's installed, what's missing |
| `jfripper config [--example]` | the settings in use / a commented example |
| `jfripper service` | print a systemd or launchd service file for this machine |

## Troubleshooting

- **Nothing happens when I insert a disc.** Run `jfripper drives`. On Linux, "can't open"
  means the user isn't in the `cdrom` group (log out and back in after adding it).
- **It uses x264 when I have an Intel/AMD GPU.** The user needs the `render` group. Check
  with `jfripper check`.
- **"MakeMKV beta expired" / no titles found.** MakeMKV needs a registration key. During its
  open beta a free key is posted
  [on the forum](https://forum.makemkv.com/forum/viewtopic.php?t=1053); set
  `refresh_beta_key = true` to have JFRipper fetch it before each rip, or buy a key.
- **A scratched disc keeps failing.** Clean it from the centre outwards and reinsert; the
  retry ladder starts over. A disc whose outer edge is unreadable may just be too damaged
  for that drive.
- **Logs:** `~/.local/state/jfripper/jfripper.log` (Linux), `~/Library/Application
  Support/JFRipper/jfripper.log` (macOS). Each rip folder also keeps MakeMKV's output.

## Development

```sh
python -m unittest discover -s tests -v
```

The end-to-end tests use a fake `makemkvcon` and a fake drive, so they need ffmpeg,
guessit and mutagen but no disc.

## License

MIT © Jonathan Davis
