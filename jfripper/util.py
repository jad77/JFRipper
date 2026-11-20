"""Small helpers shared by every part of JFRipper."""
import datetime as dt
import os
import re
import threading

from .config import conf

_log_lock = threading.Lock()


def log(msg):
    line = f"{dt.datetime.now():%F %T} {msg}"
    print(line, flush=True)
    with _log_lock:
        try:
            os.makedirs(os.path.dirname(conf().log_file), exist_ok=True)
            with open(conf().log_file, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass


def sanitize(name):
    """Make a string safe as a file or folder name (the same rules everywhere, so the name
    one part chooses is the name the next part sees)."""
    name = re.sub(r'[<>:"/\\|?*]', "", str(name)).strip().strip(".")
    return re.sub(r"\s+", " ", name)


def under(path, folder):
    """True if path is inside folder."""
    return os.path.realpath(path).startswith(os.path.realpath(folder) + os.sep)
