"""Settings: built-in defaults, overridden by a TOML file.

The file is the first of: --config, $JFRIPPER_CONFIG, ~/.config/jfripper/config.toml,
/etc/jfripper/config.toml. Every key is optional; see config.example.toml.
"""
import copy
import os
import sys

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

DEFAULTS = {
    "library": {
        "root": "/mnt/media",
        "movies": "Movies",
        "tv": "TV",
        "music": "Music",
        "incoming": "_incoming",
        "review": "_needs_review",
        "work": "_rip",
    },
    "rip": {
        "drives": [],               # empty = every optical drive found
        "makemkvcon": "",           # empty = look on PATH and in /Applications/MakeMKV.app
        "min_movie_minutes": 20,    # a movie's main title is at least this long
        "scan_min_minutes": 15,     # titles shorter than this are ignored (TV episodes can be ~21 min)
        "episode_max_minutes": 70,  # longer than this is a feature (or a TV disc's "Play All")
        "stall_minutes": 10,        # no rip progress for this long = give up that try
        "slow_speed": 32,           # retry speed in CD "x" (Linux only); 32 is about 4x DVD
        "refresh_beta_key": False,  # fetch MakeMKV's public beta key from its forum before each rip
        "rip_on_start": True,       # rip a disc that's already in the drive when JFRipper starts
        "poll_seconds": 5,
    },
    "encode": {
        "encoder": "auto",          # auto | qsv | vaapi | nvenc | videotoolbox | x264
        "quality": 21,              # CRF-style: lower = better and bigger; 18-23 is sensible
        "ffmpeg": "",               # empty = jellyfin-ffmpeg if installed, else ffmpeg on PATH
        "ffprobe": "",
        "render_device": "/dev/dri/renderD128",  # Linux QSV / VA-API
    },
    "sorter": {
        "tmdb_api_key": "",         # or $TMDB_API_KEY
        "interval_seconds": 60,
        "settle_seconds": 60,       # leave files alone until they've stopped changing this long
    },
    "status": {
        "enabled": True,
        "host": "0.0.0.0",
        "port": 8097,
        "allow_naming": True,       # let the page name rips that are waiting for one
        "public_url": "",           # put in notifications, e.g. http://mediaserver.local:8097
    },
    "notify": {
        "ntfy_url": "",             # e.g. https://ntfy.sh/my-secret-topic
        "ntfy_token": "",
        "webhook_url": "",          # gets JSON {title, message, text, content}
        "command": "",              # run as: <command> "<subject>", body on stdin
        "smtp_host": "",
        "smtp_port": 587,
        "smtp_starttls": True,
        "smtp_user": "",
        "smtp_password": "",
        "smtp_from": "",
        "smtp_to": "",
    },
    "state_dir": "",                # empty = a per-user default (see default_state_dir)
}


def default_state_dir():
    if IS_MAC:
        return os.path.expanduser("~/Library/Application Support/JFRipper")
    if os.geteuid() == 0:
        return "/var/lib/jfripper"
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "jfripper")


def search_paths():
    paths = []
    if os.environ.get("JFRIPPER_CONFIG"):
        paths.append(os.environ["JFRIPPER_CONFIG"])
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    paths += [os.path.join(base, "jfripper", "config.toml"), "/etc/jfripper/config.toml"]
    return paths


def merge(base, override, where=""):
    """Deep-merge override into base, refusing unknown keys so a typo isn't silently ignored."""
    for key, value in override.items():
        if key not in base:
            raise ValueError(f"unknown setting '{where}{key}' in the config file")
        if isinstance(base[key], dict):
            if not isinstance(value, dict):
                raise ValueError(f"[{where}{key}] should be a section in the config file")
            merge(base[key], value, f"{where}{key}.")
        else:
            base[key] = value
    return base


class Config:
    def __init__(self, data, path=None):
        self.path = path
        self.data = data
        lib = data["library"]
        self.root = os.path.expanduser(lib["root"])
        # A relative name lives under root; an absolute path is used as-is.
        sub = lambda key: os.path.join(self.root, os.path.expanduser(lib[key]))
        self.movies, self.tv, self.music = sub("movies"), sub("tv"), sub("music")
        self.incoming, self.review, self.work = sub("incoming"), sub("review"), sub("work")
        self.rip = data["rip"]
        self.encode = data["encode"]
        self.sorter = data["sorter"]
        self.status = data["status"]
        self.notify = data["notify"]
        self.state_dir = os.path.expanduser(data["state_dir"] or default_state_dir())
        self.jobs_dir = os.path.join(self.state_dir, "jobs")
        self.cd_logs = os.path.join(self.state_dir, "cd-logs")
        self.log_file = os.path.join(self.state_dir, "jfripper.log")

    def tmdb_key(self):
        return (self.sorter["tmdb_api_key"] or os.environ.get("TMDB_API_KEY", "")).strip()

    def ensure_dirs(self):
        for d in (self.state_dir, self.jobs_dir, self.incoming, self.review, self.work):
            os.makedirs(d, exist_ok=True)


_cfg = None


def load(path=None):
    """Load settings (once per process unless called again) and return them."""
    global _cfg
    data = copy.deepcopy(DEFAULTS)
    if path is None:
        path = next((p for p in search_paths() if os.path.exists(p)), None)
    if path:
        with open(path, "rb") as f:
            merge(data, tomllib.load(f))
    _cfg = Config(data, path)
    return _cfg


def conf():
    return _cfg if _cfg is not None else load()


def use(config):
    """Swap in a Config directly (tests)."""
    global _cfg
    _cfg = config
