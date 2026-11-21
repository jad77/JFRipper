"""The settings page's back end: what can be edited from the web, checking new values, and
writing the config file.

Secrets (mail password, ntfy token, TMDB key) are never sent to the browser; the page only
learns whether one is set, and a blank field keeps the saved value. Settings that would let
someone run a program on this machine (the notification command and the makemkvcon /
ffmpeg paths) are shown read-only: change those in the config file.
"""
import copy
import json
import os
import re
import shutil

from . import config
from .util import log

SECRET = "secret"
READONLY = "readonly"
RESTART = "restart"

# (section, key, label, help, flags)
FIELDS = [
    ("library", "root", "Library folder", "Your Jellyfin media folder. Must already exist.", ()),
    ("library", "movies", "Movies folder", "Inside the library folder unless it starts with /.", ()),
    ("library", "tv", "TV folder", "", ()),
    ("library", "music", "Music folder", "", ()),
    ("library", "incoming", "Incoming folder", "Drop your own files here to have them sorted.", ()),
    ("library", "review", "Needs-review folder", "Rips that couldn't be named wait here.", ()),
    ("library", "work", "Work folder", "Rips in progress. Keep it on the same disk as the library.", ()),

    ("rip", "drives", "Drives", "Blank = every drive. Linux: /dev/sr0, macOS: 1. Comma-separated.", (RESTART,)),
    ("rip", "min_movie_minutes", "Shortest movie (minutes)", "", ()),
    ("rip", "scan_min_minutes", "Ignore titles shorter than (minutes)", "", ()),
    ("rip", "episode_max_minutes", "Longest TV episode (minutes)", "", ()),
    ("rip", "stall_minutes", "Give up a stuck rip after (minutes)", "", ()),
    ("rip", "slow_speed", "Retry read speed (Linux)", "In CD \"x\"; 32 is about 4x DVD.", ()),
    ("rip", "refresh_beta_key", "Fetch MakeMKV's free beta key", "From the MakeMKV forum, before each rip.", ()),
    ("rip", "rip_on_start", "Rip a disc already in the drive at startup", "", ()),
    ("rip", "poll_seconds", "Check drives every (seconds)", "", ()),
    ("rip", "makemkvcon", "makemkvcon program", "Change in the config file.", (READONLY,)),

    ("encode", "encoder", "Video encoder", "auto picks the best one this machine has.", ()),
    ("encode", "quality", "Video quality", "Lower = better and bigger; 18-23 is sensible.", ()),
    ("encode", "render_device", "GPU device (Linux)", "For Quick Sync / VA-API.", ()),
    ("encode", "ffmpeg", "ffmpeg program", "Change in the config file.", (READONLY,)),
    ("encode", "ffprobe", "ffprobe program", "Change in the config file.", (READONLY,)),

    ("cd", "format", "CD format", "FLAC is lossless; MP3 is smaller.", ()),
    ("cd", "mp3_quality", "MP3 quality", "LAME VBR level: 0 = best (~245 kbps), 2 = ~190, 4 = ~165.", ()),

    ("sorter", "tmdb_api_key", "TMDB API key", "Free at themoviedb.org. Strongly recommended.", (SECRET,)),
    ("sorter", "interval_seconds", "Sort incoming every (seconds)", "", ()),
    ("sorter", "settle_seconds", "Wait for copies to finish (seconds)", "", ()),

    ("status", "host", "Listen address", "0.0.0.0 = whole network, 127.0.0.1 = this machine.", (RESTART,)),
    ("status", "port", "Port", "", (RESTART,)),
    ("status", "allow_naming", "Allow naming rips from the page", "", ()),
    ("status", "public_url", "Page address for notifications", "e.g. http://mediaserver.local:8097", ()),

    ("notify", "ntfy_url", "ntfy topic URL", "e.g. https://ntfy.sh/a-long-random-topic", ()),
    ("notify", "ntfy_token", "ntfy access token", "Only for protected topics.", (SECRET,)),
    ("notify", "webhook_url", "Webhook URL", "Discord, Slack, Home Assistant, anything that takes JSON.", ()),
    ("notify", "smtp_host", "Mail server", "Leave blank to turn email off.", ()),
    ("notify", "smtp_port", "Mail port", "587 with STARTTLS is usual.", ()),
    ("notify", "smtp_starttls", "Use STARTTLS", "Keep on unless your server says otherwise.", ()),
    ("notify", "smtp_user", "Mail login", "", ()),
    ("notify", "smtp_password", "Mail password", "Stored only in the config file (owner-only).", (SECRET,)),
    ("notify", "smtp_from", "From address", "Blank = the mail login.", ()),
    ("notify", "smtp_to", "Send to", "", ()),
    ("notify", "command", "Notification command", "Change in the config file.", (READONLY,)),
]

# What a new user needs; everything else is on the Advanced tab with its default shown.
BASICS = {("library", "root"), ("sorter", "tmdb_api_key"), ("cd", "format"),
          ("notify", "ntfy_url"), ("notify", "webhook_url"), ("notify", "smtp_host"),
          ("notify", "smtp_user"), ("notify", "smtp_password"), ("notify", "smtp_to")}

SECTIONS = {"library": "Library", "rip": "Ripping", "encode": "Video", "cd": "Audio CDs",
            "sorter": "Naming", "status": "This page", "notify": "Notifications"}
CHOICES = {("encode", "encoder"): ["auto", "qsv", "vaapi", "nvenc", "videotoolbox", "x264"],
           ("cd", "format"): ["flac", "mp3"]}
RANGES = {("encode", "quality"): (0, 51), ("cd", "mp3_quality"): (0, 9),
          ("status", "port"): (1, 65535), ("notify", "smtp_port"): (1, 65535)}


def kind(section, key):
    default = config.DEFAULTS[section][key]
    if (section, key) in CHOICES:
        return "choice"
    return {bool: "bool", int: "int", list: "list"}.get(type(default), "text")


def shown(value):
    """A setting as words, for 'Default: ...'."""
    if isinstance(value, bool):
        return "On" if value else "Off"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "blank"
    return str(value) if value != "" else "blank"


def view():
    """Everything the page needs, with secrets reduced to 'is one saved?'."""
    c = config.conf()
    fields = []
    for section, key, label, help_text, flags in FIELDS:
        value = c.data[section][key]
        default = config.DEFAULTS[section][key]
        f = {"section": section, "key": key, "label": label, "help": help_text,
             "kind": kind(section, key), "readonly": READONLY in flags,
             "restart": RESTART in flags, "secret": SECRET in flags,
             "basic": (section, key) in BASICS, "is_default": value == default,
             "default": "not set" if SECRET in flags else shown(default)}
        if not f["secret"]:
            f["default_value"] = ", ".join(str(v) for v in default) if f["kind"] == "list" else default
        if f["secret"]:
            f["is_set"] = bool(value)
        else:
            f["value"] = ", ".join(str(v) for v in value) if f["kind"] == "list" else value
        if f["kind"] == "choice":
            f["choices"] = CHOICES[(section, key)]
        fields.append(f)
    return {"fields": fields, "sections": SECTIONS, "config_path": target_path(),
            "writable": writable()}


def target_path():
    c = config.conf()
    return c.path or config.search_paths()[0]


def writable():
    path = target_path()
    if os.path.exists(path):
        return os.access(path, os.W_OK)
    parent = os.path.dirname(path)
    while parent and not os.path.exists(parent):
        parent = os.path.dirname(parent)
    return os.access(parent or "/", os.W_OK)


def parse(section, key, raw):
    """A value from the page -> the right type, or ValueError with a message for the page."""
    k = kind(section, key)
    label = next(f[2] for f in FIELDS if f[:2] == (section, key))
    if k == "bool":
        if isinstance(raw, bool):
            return raw
        raise ValueError(f"{label}: should be on or off")
    if k == "int":
        try:
            value = int(str(raw).strip())
        except ValueError:
            raise ValueError(f"{label}: should be a whole number")
        lo, hi = RANGES.get((section, key), (0, 10 ** 6))
        if not lo <= value <= hi:
            raise ValueError(f"{label}: should be between {lo} and {hi}")
        return value
    if k == "list":
        items = [x.strip() for x in str(raw).split(",") if x.strip()]
        if section == "rip" and key == "drives":
            for item in items:
                if not (item.isdigit() or re.fullmatch(r"/dev/[A-Za-z0-9]+", item)):
                    raise ValueError(f"{label}: {item!r} isn't a drive (like /dev/sr0, or 1 on macOS)")
            return [int(i) if i.isdigit() else i for i in items]
        return items
    value = str(raw).strip()
    if "\n" in value or "\r" in value:
        raise ValueError(f"{label}: should be one line")
    if k == "choice" and value not in CHOICES[(section, key)]:
        raise ValueError(f"{label}: pick one of {', '.join(CHOICES[(section, key)])}")
    if key in ("ntfy_url", "webhook_url", "public_url") and value and \
            not re.match(r"https?://", value):
        raise ValueError(f"{label}: should start with http:// or https://")
    if section == "library" and key == "root" and not os.path.isdir(os.path.expanduser(value)):
        raise ValueError(f"{label}: {value} isn't a folder on this machine")
    if section == "library" and not value:
        raise ValueError(f"{label}: can't be blank")
    return value


def toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, list)):
        return json.dumps(v)
    return json.dumps(str(v), ensure_ascii=False)  # a JSON string is a valid TOML basic string


def dump(data):
    """Settings -> TOML text, writing only what differs from the defaults."""
    lines = ["# JFRipper settings, saved by the settings page.",
             "# Every option, with notes: jfripper config --example", ""]
    top = [k for k, v in data.items() if not isinstance(v, dict) and v != config.DEFAULTS[k]]
    for key in top:
        lines.append(f"{key} = {toml_value(data[key])}")
    if top:
        lines.append("")
    for section, values in data.items():
        if not isinstance(values, dict):
            continue
        changed = [(k, v) for k, v in values.items() if v != config.DEFAULTS[section][k]]
        if changed:
            lines.append(f"[{section}]")
            lines += [f"{k} = {toml_value(v)}" for k, v in changed]
            lines.append("")
    return "\n".join(lines)


def save(changes):
    """Check and apply {"section.key": value} from the page; write the file and reload.
    Returns {"ok", "message", "restart": [labels needing a restart]}."""
    c = config.conf()
    data = copy.deepcopy(c.data)
    restart = []
    try:
        for name, raw in (changes or {}).items():
            section, _, key = str(name).partition(".")
            field = next((f for f in FIELDS if f[:2] == (section, key)), None)
            if not field:
                raise ValueError(f"unknown setting {name}")
            if READONLY in field[4]:
                raise ValueError(f"{field[2]} can only be changed in the config file")
            if SECRET in field[4]:
                if raw is None or raw == "":
                    continue  # blank = keep what's saved
                value = "" if raw == {"clear": True} else parse(section, key, raw)
            else:
                value = parse(section, key, raw)
            if value != data[section][key]:
                data[section][key] = value
                if RESTART in field[4]:
                    restart.append(field[2])
        config.Config(data)  # whole-config checks (e.g. cd.format)
    except ValueError as exc:
        return {"ok": False, "message": str(exc), "restart": []}

    path = target_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            shutil.copy2(path, path + ".bak")
            os.chmod(path + ".bak", 0o600)
        tmp = path + ".tmp"
        # Owner-only from the first byte: the file can hold the mail password.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(dump(data))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as exc:
        return {"ok": False, "message": f"couldn't write {path}: {exc}", "restart": []}

    new = config.load(path)
    from . import encode
    if new.encode != c.encode:
        encode.reset()  # pick the encoder again with the new settings
    new.ensure_dirs()
    log(f"settings saved to {path}" + (f" ({', '.join(restart)} after a restart)" if restart else ""))
    return {"ok": True, "message": "Saved." + (" Restart JFRipper for: " + ", ".join(restart)
                                               if restart else ""), "restart": restart}
