"""Command line: `jfripper run` is the service; the rest are one-off helpers."""
import argparse
import getpass
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import VERSION_LABEL, config


def cmd_run(args):
    from . import daemon
    daemon.run()


def find_drive(name):
    from . import drives
    found = drives.find_drives()
    if not name:
        if not found:
            sys.exit("no optical drive found")
        return found[0]
    for d in found:
        if name in (d.name, getattr(d, "path", None), str(getattr(d, "index", ""))):
            return d
    sys.exit(f"no drive {name!r}; `jfripper drives` lists them")


def cmd_rip(args):
    """Rip the disc that's in the drive now, then encode and file it (no service needed)."""
    from . import encode, rip
    config.conf().ensure_dirs()
    work = rip.rip(find_drive(args.drive))
    if work:
        encode.encode(work)


def cmd_encode(args):
    from . import encode
    encode.encode(os.path.abspath(args.work))


def cmd_sort(args):
    from . import sorter
    config.conf().ensure_dirs()
    sorter.process()


def cmd_name(args):
    from . import naming
    if args.kind == "movie":
        out = naming.movie(args.job, args.title, args.year)
    elif args.kind == "tv":
        out = naming.tv(args.job, args.show, args.season, args.first, args.year, args.skip)
    else:
        out = naming.cd(args.job, args.artist, args.album, args.year, args.track)
    print(("Done: " if out["ok"] else "Not filed: ") + out["message"])
    sys.exit(0 if out["ok"] else 1)


def cmd_jobs(args):
    from . import jobs
    for j in jobs.all_jobs():
        name = j.get("title") or j.get("name") or j.get("label") or "Disc"
        when = time.strftime("%b %d %H:%M", time.localtime(j.get("finished") or j.get("started") or 0))
        extra = j.get("message") or j.get("path") or ""
        print(f"{j['stamp']}  {j.get('stage', '?'):<10}  {when}  {name}" + (f"  ({extra})" if extra else ""))


def cmd_drives(args):
    from . import drives
    found = drives.find_drives()
    if not found:
        print("no optical drives found")
    for d in found:
        state = "empty"
        if d.has_disc():
            state = "audio CD" if d.is_audio_cd() else (d.media_kind() or "disc") + " inserted"
        print(f"{d.name:<8} {d.path:<40} {state}")


def check_line(ok, what, detail=""):
    """ok: True, False, or None for an optional piece that's missing."""
    mark = {True: "ok", False: "!!", None: "--"}[ok]
    print(f"  [{mark}] {what}" + (f": {detail}" if detail else ""))
    return ok


def cmd_check(args):
    """Look for everything JFRipper needs and say what's missing."""
    from . import drives, encode, makemkv
    c = config.conf()
    print(f"JFRipper {VERSION_LABEL} on {sys.platform}, Python {sys.version.split()[0]}")
    print(f"  config: {c.path or 'none found, using defaults'}")
    print(f"  state:  {c.state_dir}")
    print("Library")
    check_line(os.path.isdir(c.root) and os.access(c.root, os.W_OK), "library root writable", c.root)
    print("Programs")
    mk = makemkv.binary()
    check_line(bool(shutil.which(mk) or os.path.exists(mk)), "makemkvcon",
               mk if shutil.which(mk) or os.path.exists(mk) else "not found; install MakeMKV from makemkv.com")
    for name in ("ffmpeg", "ffprobe"):
        path = encode.tool(name)
        check_line(bool(shutil.which(path) or os.path.exists(path)), name, path)
    if shutil.which(encode.tool("ffmpeg")) or os.path.exists(encode.tool("ffmpeg")):
        enc = encode.encoder()
        check_line(encode.encoder_works(enc), "video encoder", encode.ENCODER_NAMES[enc]
                   + (" (no hardware encoder found; works, just slower)" if enc == "x264" else ""))
    if str(c.cd["format"]).lower() == "mp3":
        lame = "libmp3lame" in subprocess.run([encode.tool("ffmpeg"), "-hide_banner", "-encoders"],
                                              capture_output=True, text=True).stdout
        check_line(lame, "MP3 encoder (cd.format = mp3)", "ffmpeg has LAME" if lame else
                   "this ffmpeg has no libmp3lame; CDs will fail")
    if config.IS_LINUX:
        check_line(bool(shutil.which("whipper")), "whipper (audio CDs)",
                   shutil.which("whipper") or "not installed; audio CDs will fail")
    if config.IS_MAC:
        from . import cd
        check_line(bool(shutil.which("afconvert")), "afconvert (audio CDs)",
                   "built in; writes " + ("FLAC" if cd.afconvert_flac() else "Apple Lossless"))
    print("Python modules")
    for mod, why in (("guessit", "naming"), ("mutagen", "music tags"),
                     ("discid", "Linux CD lookup, optional")):
        found = importlib.util.find_spec(mod) is not None
        if mod == "discid" and not found:
            found = importlib.util.find_spec("libdiscid") is not None
        check_line(True if found else None if "optional" in why else False, f"{mod} ({why})",
                   "" if found else "not installed")
    print("Lookups and notifications")
    check_line(True, "TMDB key", "set" if c.tmdb_key() else
               "not set; discs are only named when the label includes a year")
    channels = [k for k, v in (("ntfy", c.notify["ntfy_url"]), ("webhook", c.notify["webhook_url"]),
                               ("command", c.notify["command"]), ("email", c.notify["smtp_host"])) if v]
    check_line(True, "notifications", ", ".join(channels) or "none; only the log and status page")
    print("Drives")
    try:
        found = drives.find_drives()
    except Exception as exc:
        found = []
        check_line(False, "drive scan", repr(exc))
    if not found:
        check_line(False, "optical drive", "none found")
    for d in found:
        readable = config.IS_MAC or os.access(d.path, os.R_OK)
        check_line(readable, d.path, "ok" if readable else
                   "can't open it; add this user to the 'cdrom' group and log in again")


def cmd_config(args):
    if args.example:
        print(EXAMPLE_CONFIG.read_text() if EXAMPLE_CONFIG.exists() else
              "config.example.toml isn't included in this install; see the project README")
        return
    c = config.conf()
    print(f"config file: {c.path or '(none; using defaults)'}")
    print("searched:    " + ", ".join(config.search_paths()))
    print(json.dumps(c.data, indent=2))


LAUNCHD = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.jfripper</string>
  <key>ProgramArguments</key>
  <array>{args}<string>run</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""

SYSTEMD = """[Unit]
Description=JFRipper - rip discs into the Jellyfin library
After=network-online.target
Wants=network-online.target
RequiresMountsFor={root}

[Service]
User={user}
SupplementaryGroups=cdrom video render
ExecStart={exe} run
Restart=on-failure
RestartSec=10
Nice=5

[Install]
WantedBy=multi-user.target
"""


def cmd_service(args):
    """Print a service file for this machine, filled in for this install."""
    argv = [shutil.which("jfripper")] if shutil.which("jfripper") else [sys.executable, "-m", "jfripper"]
    exe = " ".join(argv)
    c = config.conf()
    if config.IS_MAC:
        args = "".join(f"<string>{a}</string>" for a in argv)
        print(LAUNCHD.format(args=args, log=os.path.join(c.state_dir, "service.log")), end="")
        print("\n<!-- Save as ~/Library/LaunchAgents/local.jfripper.plist, then:\n"
              "     launchctl load -w ~/Library/LaunchAgents/local.jfripper.plist -->",
              file=sys.stderr)
    else:
        print(SYSTEMD.format(exe=exe, user=getpass.getuser(), root=c.root), end="")
        print("\n# Save as /etc/systemd/system/jfripper.service, then:\n"
              "#   sudo systemctl daemon-reload && sudo systemctl enable --now jfripper",
              file=sys.stderr)


EXAMPLE_CONFIG = Path(__file__).with_name("config.example.toml")


def main(argv=None):
    p = argparse.ArgumentParser(prog="jfripper", description="Put a disc in, get it in Jellyfin.")
    p.add_argument("--version", action="version", version=f"JFRipper {VERSION_LABEL}")
    p.add_argument("-c", "--config", help="config file (default: ~/.config/jfripper/config.toml)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="the service: watch drives, rip, encode, sort, status page") \
        .set_defaults(fn=cmd_run)
    s = sub.add_parser("rip", help="rip, encode and file the disc in a drive right now")
    s.add_argument("drive", nargs="?", help="e.g. sr0 or /dev/sr0 (Linux), 1 (macOS)")
    s.set_defaults(fn=cmd_rip)
    s = sub.add_parser("encode", help="encode and file a finished rip folder again")
    s.add_argument("work")
    s.set_defaults(fn=cmd_encode)
    sub.add_parser("sort", help="sort the incoming folder once").set_defaults(fn=cmd_sort)

    s = sub.add_parser("name", help="name a rip that's waiting for one")
    kinds = s.add_subparsers(dest="kind", required=True)
    m = kinds.add_parser("movie")
    m.add_argument("job")
    m.add_argument("title")
    m.add_argument("year")
    t = kinds.add_parser("tv")
    t.add_argument("job")
    t.add_argument("--show", required=True)
    t.add_argument("--season", required=True)
    t.add_argument("--first", required=True, help="episode number of the first title on the disc")
    t.add_argument("--year", default="")
    t.add_argument("--skip", nargs="*", default=[], help="disc positions that aren't episodes")
    a = kinds.add_parser("cd")
    a.add_argument("job")
    a.add_argument("--artist", required=True)
    a.add_argument("--album", required=True)
    a.add_argument("--year", default="")
    a.add_argument("--track", action="append", default=[], help="track name (repeat, in order)")
    s.set_defaults(fn=cmd_name)

    sub.add_parser("jobs", help="list recent discs and their state").set_defaults(fn=cmd_jobs)
    sub.add_parser("drives", help="list optical drives").set_defaults(fn=cmd_drives)
    sub.add_parser("check", help="check that everything needed is installed") \
        .set_defaults(fn=cmd_check)
    s = sub.add_parser("config", help="show the settings in use")
    s.add_argument("--example", action="store_true", help="print a commented example config")
    s.set_defaults(fn=cmd_config)
    sub.add_parser("service", help="print a systemd/launchd service file for this machine") \
        .set_defaults(fn=cmd_service)

    args = p.parse_args(argv)
    try:
        config.load(args.config)
    except (OSError, ValueError) as exc:
        sys.exit(f"config: {exc}")
    args.fn(args)


if __name__ == "__main__":
    main()
