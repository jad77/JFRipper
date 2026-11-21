# Changelog

JFRipper is in beta. Versions follow [semantic versioning](https://semver.org/); while it's
0.x, a minor version (0.2 → 0.3) can add features and change settings.

## 0.2.0 (Beta) - 2026-11-21

### Added
- **Settings tab** in the web dashboard, next to Status, on the same port.
  - **Basics** tab for a new setup: library folder, TMDB key, CD format, notifications.
  - **Advanced** tab for everything else. Each setting is tagged *default* until changed;
    a changed one shows its default and a **Reset**. The tab counts changed settings.
  - Values are checked before saving and take effect immediately; the port, listen address
    and drive list are marked "after restart".
  - **Send test notification** button.
  - Mail password, ntfy token and TMDB key are never sent to the browser; a blank field keeps
    the saved value. The config file is written owner-only (0600) and the previous version is
    kept as `config.toml.bak`.
  - The notification command and the makemkvcon / ffmpeg paths are read-only on the page
    (config file only), so the page can't be used to run a program on the server.
- **Audio CDs on macOS**, using the built-in `afconvert`; nothing extra to install. The album
  is looked up on MusicBrainz from the disc's table of contents. (No AccurateRip check on
  macOS; Linux still uses whipper.)
- **FLAC or MP3 for CDs** (`cd.format`, `cd.mp3_quality`). MP3s are made with ffmpeg's LAME
  encoder on both platforms. On Linux, whipper still reads and AccurateRip-checks the disc
  first; tags and cover art carry over.
- `jfripper check` reports the CD tools (`afconvert` / whipper) and whether ffmpeg can write
  MP3 when that's selected.

### Changed
- MusicBrainz lookups use Python's standard library, so the `musicbrainzngs` module is no
  longer needed. The optional `[cd]` extra is now just `discid` (Linux).
- A bad `cd.format` in the config file is refused at startup instead of during a rip.
- The dashboard's tab bar replaces the old "JFRipper" heading.

## 0.1.0 (Beta) - 2026-11-20

First public release.

- DVDs and Blu-rays: rip with MakeMKV, with a retry ladder for scratched discs and stall
  detection; convert MPEG-2 / VC-1 to H.264; name from TMDB; file into the Jellyfin library.
- TV season discs: every episode ripped, then named from the status page or `jfripper name tv`.
- Audio CDs on Linux: AccurateRip-checked FLAC via whipper, tagged from MusicBrainz.
- Encoder picked automatically: VideoToolbox (macOS), Quick Sync / VA-API / NVENC (Linux),
  or x264.
- Status page on port 8097 with naming forms; ntfy / webhook / email / command notifications.
- `jfripper check`, `drives`, `jobs`, `rip`, `sort`, `encode`, `config`, `service` commands.
